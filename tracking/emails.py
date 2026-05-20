import logging
import resend
from django.conf import settings

logger = logging.getLogger(__name__)

FROM_ADDRESS = 'Voss & Thorne LLP <noreply@shipment.vossandthornellp.org>'

STATUS_COLORS = {
    'Pending':          '#6b7280',
    'In Transit':       '#2d6be4',
    'Out for Delivery': '#c9a84c',
    'Delivered':        '#22c55e',
    'On Hold':          '#f97316',
    'Exception':        '#e84545',
}


# ── HTML building blocks ───────────────────────────────────────────────────────

def _header():
    return """
    <tr>
      <td style="background:#07070d;padding:40px 48px 32px;">
        <p style="margin:0;font-family:Georgia,serif;font-size:10px;letter-spacing:5px;
                  color:#c9a84c;text-transform:uppercase;">Voss &amp; Thorne</p>
        <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:19px;font-weight:bold;
                  color:#dde0ee;letter-spacing:3px;text-transform:uppercase;">Logistics Partners LLP</p>
        <table cellpadding="0" cellspacing="0" style="margin-top:22px;">
          <tr><td style="background:#c9a84c;width:48px;height:2px;font-size:0;line-height:0;">&nbsp;</td></tr>
        </table>
      </td>
    </tr>"""


def _footer():
    return """
    <tr>
      <td style="background:#07070d;padding:24px 48px;">
        <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;
                  color:#555570;letter-spacing:2px;text-transform:uppercase;">
          Voss &amp; Thorne LLP &nbsp;&middot;&nbsp; Confidential
        </p>
        <p style="margin:8px 0 0;font-family:Arial,sans-serif;font-size:10px;color:#2e2e45;line-height:1.5;">
          This message contains privileged and confidential information intended only for the named recipient.
          If you have received this in error, please notify us immediately and delete this communication.
        </p>
      </td>
    </tr>"""


def _track_button(tracking_number):
    url = f'https://vossandthornellp.org/track/{tracking_number}/'
    return f"""
    <table cellpadding="0" cellspacing="0" style="margin-top:32px;">
      <tr>
        <td style="background:#c9a84c;border-radius:2px;">
          <a href="{url}"
             style="display:inline-block;padding:14px 36px;font-family:Arial,sans-serif;
                    font-size:11px;font-weight:bold;letter-spacing:3px;text-transform:uppercase;
                    color:#07070d;text-decoration:none;">
            Track Shipment &rarr;
          </a>
        </td>
      </tr>
    </table>"""


def _detail_row(label, value, last=False):
    border = '' if last else 'border-bottom:1px solid #ebebе8;'
    return f"""
    <tr>
      <td style="padding:11px 0;{border}font-family:Arial,sans-serif;font-size:10px;
                 color:#999990;letter-spacing:1px;text-transform:uppercase;width:38%;vertical-align:top;">
        {label}
      </td>
      <td style="padding:11px 0 11px 16px;{border}font-family:Georgia,serif;
                 font-size:14px;color:#1a1a2e;vertical-align:top;">
        {value}
      </td>
    </tr>"""


def _wrap(inner_rows_html):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#eeeee8;font-family:Georgia,serif;">
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#eeeee8;padding:40px 20px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="max-width:600px;width:100%;border-radius:6px;overflow:hidden;
                      box-shadow:0 6px 32px rgba(0,0,0,0.14);">
          {inner_rows_html}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ── Email: Shipment Created ────────────────────────────────────────────────────

def get_shipment_created_html(shipment):
    eta = (shipment.estimated_delivery.strftime('%B %d, %Y')
           if shipment.estimated_delivery else 'To be confirmed')

    details = (
        _detail_row('Tracking Number', f'<strong style="letter-spacing:2px;">{shipment.tracking_number}</strong>') +
        _detail_row('Route', f'{shipment.origin_city}, {shipment.origin_country} &rarr; {shipment.destination_city}, {shipment.destination_country}') +
        _detail_row('Sender', shipment.sender_name) +
        _detail_row('Delivery Address', shipment.receiver_address) +
        _detail_row('Cargo Type', shipment.cargo_type) +
        _detail_row('Weight', f'{shipment.weight} kg') +
        _detail_row('Dimensions', shipment.dimensions) +
        _detail_row('Estimated Delivery', eta, last=True)
    )

    body = f"""
    <tr>
      <td style="background:#ffffff;padding:44px 48px 48px;">
        <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:4px;
                  color:#c9a84c;text-transform:uppercase;">Booking Confirmed</p>
        <h1 style="margin:14px 0 0;font-family:Georgia,serif;font-size:30px;color:#07070d;
                   font-weight:normal;line-height:1.25;">Your shipment has<br>been registered.</h1>
        <p style="margin:20px 0 0;font-family:Arial,sans-serif;font-size:14px;
                  color:#55556a;line-height:1.7;">
          Dear {shipment.receiver_name},<br><br>
          Voss &amp; Thorne LLP has received and registered an incoming shipment from
          <strong>{shipment.sender_name}</strong>.
          Your tracking number is now active — use it to monitor every milestone in real time.
        </p>

        <table width="100%" cellpadding="0" cellspacing="0"
               style="margin-top:32px;background:#f7f7f2;border-left:3px solid #c9a84c;padding:20px 24px;">
          <tr>
            <td>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:3px;
                        color:#c9a84c;text-transform:uppercase;">Tracking Number</p>
              <p style="margin:10px 0 0;font-family:Georgia,serif;font-size:30px;
                        color:#07070d;letter-spacing:5px;">{shipment.tracking_number}</p>
            </td>
          </tr>
        </table>

        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;">
          {details}
        </table>

        {_track_button(shipment.tracking_number)}
      </td>
    </tr>"""

    return _wrap(_header() + body + _footer())


# ── Email: Status Update ───────────────────────────────────────────────────────

def get_status_update_html(shipment, old_status):
    eta = (shipment.estimated_delivery.strftime('%B %d, %Y')
           if shipment.estimated_delivery else 'To be confirmed')
    new_color = STATUS_COLORS.get(shipment.status, '#6b7280')

    latest_event = shipment.events.order_by('-timestamp').first()
    event_note_html = ''
    if latest_event:
        event_note_html = f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="margin-top:24px;background:#f7f7f2;border-left:3px solid #ddddd8;padding:18px 24px;">
          <tr>
            <td>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:2px;
                        color:#999;text-transform:uppercase;">Latest Update</p>
              <p style="margin:8px 0 0;font-family:Georgia,serif;font-size:14px;
                        color:#333340;line-height:1.65;">{latest_event.description}</p>
              <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:11px;
                        color:#aaa;">{latest_event.location}</p>
            </td>
          </tr>
        </table>"""

    body = f"""
    <tr>
      <td style="background:#ffffff;padding:44px 48px 48px;">
        <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:4px;
                  color:#c9a84c;text-transform:uppercase;">Status Update</p>
        <h1 style="margin:14px 0 0;font-family:Georgia,serif;font-size:30px;color:#07070d;
                   font-weight:normal;line-height:1.25;">Your shipment<br>status has changed.</h1>
        <p style="margin:20px 0 0;font-family:Arial,sans-serif;font-size:14px;
                  color:#55556a;line-height:1.7;">Dear {shipment.receiver_name},</p>

        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;">
          <tr>
            <td width="46%" style="padding:20px;background:#f4f4f0;border-radius:3px;text-align:center;vertical-align:middle;">
              <p style="margin:0;font-family:Arial,sans-serif;font-size:9px;letter-spacing:2px;
                        color:#aaa;text-transform:uppercase;">Previous Status</p>
              <p style="margin:8px 0 0;font-family:Georgia,serif;font-size:15px;color:#888;">{old_status}</p>
            </td>
            <td width="8%" style="text-align:center;font-size:18px;color:#c9a84c;vertical-align:middle;">&rarr;</td>
            <td width="46%" style="padding:20px;background:{new_color};border-radius:3px;text-align:center;vertical-align:middle;">
              <p style="margin:0;font-family:Arial,sans-serif;font-size:9px;letter-spacing:2px;
                        color:rgba(255,255,255,0.65);text-transform:uppercase;">Current Status</p>
              <p style="margin:8px 0 0;font-family:Georgia,serif;font-size:15px;
                        color:#fff;font-weight:bold;">{shipment.status}</p>
            </td>
          </tr>
        </table>

        {event_note_html}

        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;">
          {_detail_row('Tracking Number', shipment.tracking_number)}
          {_detail_row('Route', f'{shipment.origin_city} &rarr; {shipment.destination_city}')}
          {_detail_row('Estimated Delivery', eta, last=True)}
        </table>

        {_track_button(shipment.tracking_number)}
      </td>
    </tr>"""

    return _wrap(_header() + body + _footer())


# ── Email: Contact form enquiry ────────────────────────────────────────────────

def get_contact_enquiry_html(name, organisation, email, subject, message):
    body = f"""
    <tr>
      <td style="background:#ffffff;padding:44px 48px 48px;">
        <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:4px;
                  color:#c9a84c;text-transform:uppercase;">New Enquiry</p>
        <h1 style="margin:14px 0 0;font-family:Georgia,serif;font-size:28px;color:#07070d;
                   font-weight:normal;line-height:1.25;">Contact form<br>submission received.</h1>

        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:32px;">
          {_detail_row('Name', name)}
          {_detail_row('Organisation', organisation)}
          {_detail_row('Email', f'<a href="mailto:{email}" style="color:#2d6be4;">{email}</a>')}
          {_detail_row('Subject', subject, last=True)}
        </table>

        <table width="100%" cellpadding="0" cellspacing="0"
               style="margin-top:24px;background:#f7f7f2;border-left:3px solid #c9a84c;padding:20px 24px;">
          <tr>
            <td>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;letter-spacing:2px;
                        color:#c9a84c;text-transform:uppercase;">Message</p>
              <p style="margin:12px 0 0;font-family:Georgia,serif;font-size:14px;
                        color:#1a1a2e;line-height:1.75;white-space:pre-line;">{message}</p>
            </td>
          </tr>
        </table>

        <table cellpadding="0" cellspacing="0" style="margin-top:32px;">
          <tr>
            <td style="background:#07070d;border-radius:2px;">
              <a href="mailto:{email}"
                 style="display:inline-block;padding:14px 36px;font-family:Arial,sans-serif;
                        font-size:11px;font-weight:bold;letter-spacing:3px;text-transform:uppercase;
                        color:#c9a84c;text-decoration:none;">
                Reply to {name} &rarr;
              </a>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""

    return _wrap(_header() + body + _footer())


# ── Send helpers ───────────────────────────────────────────────────────────────

def _get_api_key():
    key = getattr(settings, 'RESEND_API_KEY', '')
    if not key:
        logger.warning('RESEND_API_KEY is not set — email skipped')
    return key


def send_shipment_created_email(shipment):
    key = _get_api_key()
    if not key or not shipment.receiver_email:
        return
    resend.api_key = key
    try:
        resend.Emails.send({
            'from': FROM_ADDRESS,
            'to': [shipment.receiver_email],
            'subject': f'Shipment {shipment.tracking_number} — Booking Confirmed',
            'html': get_shipment_created_html(shipment),
        })
        logger.info('Confirmation email sent → %s (%s)', shipment.receiver_email, shipment.tracking_number)
    except Exception as exc:
        logger.error('Failed to send confirmation email for %s: %s', shipment.tracking_number, exc)


def send_status_update_email(shipment, old_status):
    key = _get_api_key()
    if not key or not shipment.receiver_email:
        return
    resend.api_key = key
    try:
        resend.Emails.send({
            'from': FROM_ADDRESS,
            'to': [shipment.receiver_email],
            'subject': f'Shipment {shipment.tracking_number} — Status Update: {shipment.status}',
            'html': get_status_update_html(shipment, old_status),
        })
        logger.info('Status update email sent → %s (%s → %s)', shipment.receiver_email, old_status, shipment.status)
    except Exception as exc:
        logger.error('Failed to send status update email for %s: %s', shipment.tracking_number, exc)


def send_contact_enquiry_email(name, organisation, email, subject, message):
    key = _get_api_key()
    if not key:
        return False
    resend.api_key = key
    try:
        resend.Emails.send({
            'from': FROM_ADDRESS,
            'to': ['enquiries@vossandthornellp.org'],
            'reply_to': email,
            'subject': f'Enquiry: {subject} — {organisation}',
            'html': get_contact_enquiry_html(name, organisation, email, subject, message),
        })
        logger.info('Contact enquiry email sent from %s (%s)', email, organisation)
        return True
    except Exception as exc:
        logger.error('Failed to send contact enquiry email: %s', exc)
        return False
