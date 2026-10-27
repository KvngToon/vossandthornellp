from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Document, Transaction, ValuationSnapshot
from .notifications import is_recent, notify


def _after_commit(fn):
    # Send only once the row is really saved, and never inside the request's DB transaction.
    transaction.on_commit(fn)


@receiver(post_save, sender=ValuationSnapshot)
def snapshot_posted(sender, instance, created, raw=False, **kwargs):
    if created and not raw and is_recent(instance.date):
        profile = instance.investment.investor
        _after_commit(lambda: notify(profile, 'valuation'))


@receiver(post_save, sender=Transaction)
def transaction_recorded(sender, instance, created, raw=False, **kwargs):
    if created and not raw and is_recent(instance.date):
        profile = instance.investment.investor
        _after_commit(lambda: notify(profile, 'transaction'))


@receiver(post_save, sender=Document)
def document_added(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        profile = instance.investor
        detail = instance.get_kind_display().lower()
        _after_commit(lambda: notify(profile, 'document', detail))


@receiver(post_delete, sender=Document)
def document_removed(sender, instance, **kwargs):
    # Don't leave a deleted financial document sitting in the bucket.
    if instance.file:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))
