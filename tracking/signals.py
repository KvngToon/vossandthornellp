import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _register():
    """Called from TrackingConfig.ready() to wire up signals."""
    from tracking.models import ShipmentEvent

    @receiver(post_save, sender=ShipmentEvent, dispatch_uid='shipmentevent_email')
    def shipmentevent_post_save(sender, instance, created, **kwargs):
        # Skip events flagged during seeding / management commands
        if getattr(instance, '_skip_email', False):
            return

        # Skip if no receiver email on the parent shipment
        if not instance.shipment.receiver_email:
            return

        try:
            from tracking.emails import send_event_update_email
            send_event_update_email(instance)
        except Exception as exc:
            logger.error(
                'Event email dispatch error for %s (event %s): %s',
                instance.shipment.tracking_number, instance.pk, exc,
            )
