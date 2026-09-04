import json
import logging
import re

import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

_HEADER_JUNK_RE = re.compile(r'[\r\n\x00]+')
RESEND_API_BASE = 'https://api.resend.com'


def _sanitize(value):
    """Strip CR/LF/NUL before a client-controlled header value is stored (and
    later echoed back into an outbound send's In-Reply-To/References)."""
    if not value:
        return value
    return _HEADER_JUNK_RE.sub(' ', value).strip()


def _verify_signature(request):
    """Verify the Resend inbound webhook is signed with our Svix secret.

    Fails CLOSED outside of DEBUG: an unconfigured secret in production must
    not silently accept unsigned requests, since anyone who finds this URL
    could otherwise inject fake "client replies" — including ones spoofing a
    real tracking number — into the staff inbox."""
    secret = getattr(settings, 'RESEND_WEBHOOK_SECRET', '')
    if not secret:
        if settings.DEBUG:
            logger.warning('RESEND_WEBHOOK_SECRET not set — skipping signature verification (DEBUG only)')
            return True
        logger.error('RESEND_WEBHOOK_SECRET not set — rejecting inbound webhook')
        return False

    try:
        from svix.webhooks import Webhook, WebhookVerificationError
    except ImportError:
        logger.error('svix package not installed — cannot verify webhook signature')
        return False

    headers = {
        'svix-id': request.headers.get('svix-id', ''),
        'svix-timestamp': request.headers.get('svix-timestamp', ''),
        'svix-signature': request.headers.get('svix-signature', ''),
    }
    try:
        Webhook(secret).verify(request.body, headers)
        return True
    except WebhookVerificationError as exc:
        logger.error('Inbound webhook signature verification failed: %s', exc)
        return False


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _address_of(entry):
    if isinstance(entry, dict):
        return entry.get('email', '')
    if isinstance(entry, str):
        match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', entry)
        return match.group(0) if match else entry
    return ''


def _name_of(entry):
    if isinstance(entry, dict):
        return entry.get('name', '')
    if isinstance(entry, str):
        match = re.match(r'^\s*"?([^"<]+?)"?\s*<', entry)
        return match.group(1).strip() if match else ''
    return ''


def _first_address(value):
    entries = _as_list(value)
    return _address_of(entries[0]) if entries else ''


def _first_name(value):
    entries = _as_list(value)
    return _name_of(entries[0]) if entries else ''


def _match_shipment(to_value):
    """The reply address for a shipment is just <tracking_number>@REPLY_DOMAIN
    (see shipment_reply_address() in emails.py) — check every recipient (a
    client can CC more than one address) by taking the local part verbatim
    and looking it up directly, skipping the general catch-all mailbox."""
    from tracking.models import Shipment

    for entry in _as_list(to_value):
        address = _address_of(entry)
        if not address or '@' not in address:
            continue
        local_part = address.split('@', 1)[0]
        if local_part.lower() == 'inbox':
            continue
        shipment = Shipment.objects.filter(tracking_number__iexact=local_part).first()
        if shipment:
            return shipment
    return None


def _derive_text(text_body, html_body):
    """Some inbound mail has no text/plain part — fall back to a readable
    plain-text rendering of the HTML rather than leaving the conversation
    view to strip a full HTML document at display time."""
    if text_body:
        return text_body
    if not html_body:
        return ''
    without_style = re.sub(r'(?is)<(script|style).*?</\1>', '', html_body)
    return strip_tags(without_style).strip()


def _fetch_full_email(email_id):
    """The email.received webhook only carries metadata (from/to/subject/
    message_id) — Resend explicitly does not include the body, headers, or
    attachments in the webhook payload. The actual text/html content and the
    In-Reply-To/References headers have to be fetched from the Received
    Emails API using the email_id. Returns None on any failure so the
    caller can still file the message from webhook metadata alone."""
    api_key = getattr(settings, 'RESEND_API_KEY', '')
    if not api_key or not email_id:
        return None
    try:
        response = requests.get(
            f'{RESEND_API_BASE}/emails/receiving/{email_id}',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error('Failed to fetch full inbound email %s: %s', email_id, exc)
        return None


@csrf_exempt
@require_POST
def resend_inbound(request):
    """Receives Resend's inbound-email webhook for client replies and files
    them against the matching shipment by its dedicated reply address
    (<tracking_number>@REPLY_DOMAIN), or as a general/unmatched message
    otherwise (e.g. inbox@REPLY_DOMAIN)."""
    if not _verify_signature(request):
        return HttpResponseForbidden('invalid signature')

    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest('invalid json')

    if payload.get('type') not in ('email.received', 'inbound.email'):
        return HttpResponse(status=204)

    data = payload.get('data', {})
    to_value = data.get('to')
    from_value = data.get('from')
    subject = data.get('subject', '')
    message_id = _sanitize(data.get('message_id', ''))

    from tracking.models import EmailMessage

    # Idempotency check first — before spending an API call fetching the
    # body — since Resend retries on non-2xx (and can double-deliver).
    if message_id and EmailMessage.objects.filter(message_id=message_id, direction='inbound').exists():
        logger.info('Duplicate inbound webhook delivery ignored (message_id=%s)', message_id)
        return HttpResponse(status=200)

    full = _fetch_full_email(data.get('email_id', ''))
    if full:
        text_body = full.get('text') or ''
        html_body = full.get('html') or ''
        full_headers = full.get('headers') or {}
        in_reply_to = full_headers.get('in-reply-to') or full_headers.get('In-Reply-To') or ''
        references = full_headers.get('references') or full_headers.get('References') or ''
    else:
        text_body = ''
        html_body = ''
        in_reply_to = ''
        references = ''

    text_body = _derive_text(text_body, html_body)
    in_reply_to = _sanitize(in_reply_to)
    references = _sanitize(references)

    from_address = _first_address(from_value)
    from_name = _first_name(from_value)
    to_address = _first_address(to_value)
    shipment = _match_shipment(to_value)

    EmailMessage.objects.create(
        shipment=shipment,
        direction='inbound',
        from_email=from_address,
        from_name=from_name,
        to_email=to_address,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
    )

    if shipment:
        logger.info('Inbound reply filed → %s (%s)', from_address, shipment.tracking_number)
    else:
        logger.warning('Inbound reply from %s could not be matched to a shipment (to=%s)', from_address, to_address)

    return HttpResponse(status=200)
