import logging

import resend
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes

from tracking.emails import FROM_ADDRESS, _footer, _get_api_key, _header, _wrap

logger = logging.getLogger(__name__)


def portal_url(path_name, **kwargs):
    return settings.SITE_URL.rstrip('/') + reverse(f'investors:{path_name}', kwargs=kwargs or None)


def set_password_url(user):
    return portal_url(
        'password_reset_confirm',
        uidb64=urlsafe_base64_encode(force_bytes(user.pk)),
        token=default_token_generator.make_token(user),
    )


def _body_html(headline, paragraphs, url, button_label):
    paras = ''.join(
        f'<p style="margin:0 0 14px;font-family:Georgia,serif;font-size:15px;color:#333;line-height:1.6;">{p}</p>'
        for p in paragraphs
    )
    return _wrap(f"""
    {_header()}
    <tr>
      <td style="background:#ffffff;padding:40px 48px;">
        <p style="margin:0 0 18px;font-family:Arial,sans-serif;font-size:20px;font-weight:bold;color:#07070d;">{headline}</p>
        {paras}
        <table cellpadding="0" cellspacing="0" style="margin-top:24px;">
          <tr><td style="background:#c9a84c;border-radius:2px;">
            <a href="{url}" style="display:inline-block;padding:14px 36px;font-family:Arial,sans-serif;
               font-size:12px;font-weight:bold;letter-spacing:2px;text-transform:uppercase;
               color:#07070d;text-decoration:none;">{button_label}</a>
          </td></tr>
        </table>
      </td>
    </tr>
    {_footer()}""")


def _send(to_email, subject, html):
    """Send via Resend. Returns True on success. Never raises."""
    key = _get_api_key()
    if not key or not to_email:
        return False
    resend.api_key = key
    try:
        resend.Emails.send({'from': FROM_ADDRESS, 'to': [to_email], 'subject': subject, 'html': html})
        logger.info('Investor email sent: %s', subject)
        return True
    except Exception as exc:
        logger.error('Failed to send investor email (%s): %s', subject, exc)
        return False


def send_invite_email(user, profile, temp_password):
    html = _body_html(
        'Your investor portal access',
        [f'Dear {profile.display_name},',
         'An account has been created for you on the Voss &amp; Thorne investor portal, where you can '
         'follow your investments and their growth over time.',
         f'Username: <strong>{user.username}</strong><br>'
         f'Temporary password: <strong style="font-family:monospace;font-size:17px;">{temp_password}</strong>',
         'Sign in with these details. You will be asked to choose your own password straight away. '
         'Please delete this email once you have done so.'],
        portal_url('login'), 'Sign in',
    )
    return _send(user.email, 'Your Voss & Thorne investor portal access', html)


def send_password_reset_email(user):
    html = _body_html(
        'Reset your password',
        ['We received a request to reset the password for your investor portal account '
         f'(username: <strong>{user.username}</strong>).',
         'If this was not you, you can safely ignore this email.'],
        set_password_url(user), 'Choose a new password',
    )
    return _send(user.email, 'Reset your Voss & Thorne portal password', html)


def send_update_email(user, profile, kind, detail=''):
    """Generic 'something changed' email. Deliberately contains no figures,
    plan names or document titles, only a link into the signed-in portal."""
    if kind == 'document':
        subject = 'A new document is available in your investor portal'
        headline = 'A new document is available'
        paragraphs = [f'Dear {profile.display_name},',
                      f'A new {detail or "document"} has been added to your investor portal.']
        url, label = portal_url('documents'), 'View documents'
    else:
        subject = 'There is new activity on your Voss & Thorne portfolio'
        headline = 'Your portfolio has been updated'
        paragraphs = [f'Dear {profile.display_name},',
                      'New activity has been recorded on your account. Sign in to your investor portal to review it.']
        url, label = portal_url('home'), 'View portfolio'
    return _send(user.email, subject, _body_html(headline, paragraphs, url, label))


def _contact_url():
    return settings.SITE_URL.rstrip('/') + '/contact/'


def send_withdrawal_submitted_email(profile, wr):
    """Sent to the investor on submission. The 'wasn't you?' line is the account-takeover tripwire."""
    html = _body_html(
        'We received your withdrawal request',
        [f'Dear {profile.display_name},',
         f'Your withdrawal request <strong>{wr.reference}</strong> has been received and will be reviewed by our team. '
         'We will call you on the number we hold on file before anything is paid.',
         '<strong>If you did not make this request, contact us immediately</strong> and change your password.'],
        _contact_url(), 'Contact us',
    )
    return _send(profile.user.email, f'Withdrawal request {wr.reference} received', html)


def send_withdrawal_update_email(profile, wr):
    html = _body_html(
        'Update on your withdrawal request',
        [f'Dear {profile.display_name},',
         f'There is an update on your withdrawal request <strong>{wr.reference}</strong>. '
         'Sign in to your investor portal to see its status.'],
        portal_url('withdrawal_detail', pk=wr.pk), 'View request',
    )
    return _send(profile.user.email, f'Update on withdrawal request {wr.reference}', html)


def send_withdrawal_staff_alert(wr):
    """Heads-up to the team inbox; details stay in the admin."""
    to = getattr(settings, 'ACTIVITY_NOTIFY_EMAIL', '')
    admin_url = settings.SITE_URL.rstrip('/') + reverse('admin:investors_withdrawalrequest_change', args=[wr.pk])
    html = _body_html(
        'New withdrawal request',
        [f'<strong>{wr.reference}</strong> from {wr.investor.display_name} needs review.',
         'Remember: call the investor back on the number on file before approving.'],
        admin_url, 'Open in admin',
    )
    return _send(to, f'New withdrawal request {wr.reference}', html)
