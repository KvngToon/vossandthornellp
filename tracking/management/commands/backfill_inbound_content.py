from django.core.management.base import BaseCommand, CommandError

from tracking.models import EmailMessage
from tracking.webhooks import _derive_text, _fetch_full_email


class Command(BaseCommand):
    help = (
        'Backfill the text/html body of an inbound EmailMessage that was '
        'saved with no content before the "(no content)" fix. Find both '
        'IDs in the Resend dashboard: Webhooks -> click the email.received '
        'event -> Message payload -> "message_id" and "email_id" fields.'
    )

    def add_arguments(self, parser):
        parser.add_argument('message_id', help="The email's Message-ID header, e.g. <abc@mail.gmail.com>")
        parser.add_argument('email_id', help="Resend's internal email_id (UUID) from the same webhook payload")

    def handle(self, *args, **options):
        try:
            msg = EmailMessage.objects.get(message_id=options['message_id'], direction='inbound')
        except EmailMessage.DoesNotExist:
            raise CommandError(f"No inbound message found with message_id={options['message_id']!r}")
        except EmailMessage.MultipleObjectsReturned:
            raise CommandError(f"Multiple messages share message_id={options['message_id']!r} — can't pick one")

        full = _fetch_full_email(options['email_id'])
        if not full:
            raise CommandError(
                'Could not fetch that email from Resend — check RESEND_API_KEY is set '
                'and the email_id is correct.'
            )

        text = full.get('text') or ''
        html = full.get('html') or ''
        headers = full.get('headers') or {}

        msg.text_body = _derive_text(text, html)
        msg.html_body = html
        msg.in_reply_to = headers.get('in-reply-to') or headers.get('In-Reply-To') or msg.in_reply_to
        msg.references = headers.get('references') or headers.get('References') or msg.references
        msg.save()

        self.stdout.write(self.style.SUCCESS(
            f'Updated message #{msg.pk} ({msg.from_email}) — {len(msg.text_body)} chars of body text.'
        ))
