from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from apps.accounting.models import JournalEntry, JournalLine


@transaction.atomic
def post_journal_entry(entry_id) -> JournalEntry:
    entry = JournalEntry.objects.select_for_update().get(pk=entry_id)

    if entry.status != JournalEntry.Status.DRAFT:
        raise ValidationError(f"Cannot post journal entry in '{entry.status}' status.")

    totals = JournalLine.objects.filter(entry=entry).aggregate(
        total_debit=Sum("debit"),
        total_credit=Sum("credit"),
    )
    total_debit = totals["total_debit"] or Decimal(0)
    total_credit = totals["total_credit"] or Decimal(0)

    if total_debit <= Decimal(0) or total_debit != total_credit:
        raise ValidationError(
            f"Journal entry is unbalanced: debit={total_debit}, credit={total_credit}."
        )

    entry.status = JournalEntry.Status.POSTED
    entry.save(update_fields=("status", "updated_at"))
    return entry