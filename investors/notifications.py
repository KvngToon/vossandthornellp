"""Decides whether an investor should be emailed, and records the attempt.

Rules, so data entry never spams anyone:
- only for newly created records (signals check `created`)
- records dated more than RECENT_DAYS ago are history being loaded, not news
- at most one email per investor per kind per COOLDOWN
- failures are logged but don't start a cooldown; nothing here ever raises
"""
import logging
from datetime import timedelta

from django.utils import timezone

from .emails import send_update_email
from .models import NotificationLog

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(minutes=30)
RECENT_DAYS = 45


def is_recent(record_date):
    return record_date >= timezone.localdate() - timedelta(days=RECENT_DAYS)


def notify(profile, kind, detail='', respect_cooldown=True):
    """Email an investor about an update. Returns True if an email was sent."""
    try:
        user = profile.user
        if profile.is_demo or not profile.is_active or not user.is_active or user.is_staff or not user.email:
            return False  # demo accounts are fictional and are never emailed
        if respect_cooldown:
            since = timezone.now() - COOLDOWN
            if NotificationLog.objects.filter(
                investor=profile, kind=kind, success=True, sent_at__gte=since,
            ).exists():
                return False
        ok = send_update_email(user, profile, kind if kind != 'manual' else 'valuation', detail)
        NotificationLog.objects.create(investor=profile, kind=kind, success=bool(ok))
        return bool(ok)
    except Exception:
        logger.exception('Notification failed for investor %s (%s)', getattr(profile, 'pk', '?'), kind)
        return False
