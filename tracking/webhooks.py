import json
import logging
import re

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

TRACKING_RE = re.compile(r'shipment-([A-Za-z0-9-]+)@', re.IGNORECASE)
_HEADER_JUNK_RE = re.compile(r'[\r\n\x00]+')


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


def _extract_header(headers, name):
    if not headers:
        return ''
    if isinstance(headers, dict):
        return headers.get(name, '') or headers.get(name.lower(), '')
    for h in headers:
        if isinstance(h, dict) and h.get('name', '').lower() == name.lower():
            return h.get('value', '')
    return ''


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
    """Check every recipient (To can carry more than one address — e.g. a
    client CCs our noreply address alongside the shipment reply address) for
    the shipment-<tracking_number>@ pattern, not just the first."""
    from tracking.models import Shipment

    for entry in _as_list(to_value):
        address = _address_of(entry)
        match = TRACKING_RE.search(address or '')
        if not match:
            continue
        shipment = Shipment.objects.filter(tracking_number__iexact=match.group(1)).first()
        if shipment:
            return shipment
    return None


def _derive_text(text_body, html_body):
    """Inbound mail doesn't always include a text/plain part — fall back to
    a readable plain-text rendering of the HTML rather than leaving the
    conversation view to strip a full HTML document at display time."""
    if text_body:
        return text_body
    if not html_body:
        return ''
    without_style = re.sub(r'(?is)<(script|style).*?</\1>', '', html_body)
    return strip_tags(without_style).strip()


@csrf_exempt
@require_POST
def resend_inbound(request):
    """Receives Resend's inbound-email webhook for client replies and files
    them against the matching shipment by the dedicated reply address
    (shipment-<tracking_number>@REPLY_DOMAIN), or as a general/unmatched
    message otherwise."""
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
    to_address = _first_address(to_value)
    from_address = _first_address(data.get('from'))
    from_name = _first_name(data.get('from'))
    subject = data.get('subject', '')
    text_body = _derive_text(data.get('text', ''), data.get('html', ''))
    html_body = data.get('html', '') or ''
    headers = data.get('headers')
    message_id = _sanitize(data.get('message_id') or _extract_header(headers, 'Message-ID'))
    in_reply_to = _sanitize(_extract_header(headers, 'In-Reply-To'))
    references = _sanitize(_extract_header(headers, 'References'))

    from tracking.models import EmailMessage

    # Idempotency: Resend retries on non-2xx (and can double-deliver), which
    # would otherwise create duplicate bubbles and inflate unread counts.
    if message_id and EmailMessage.objects.filter(message_id=message_id, direction='inbound').exists():
        logger.info('Duplicate inbound webhook delivery ignored (message_id=%s)', message_id)
        return HttpResponse(status=200)

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
