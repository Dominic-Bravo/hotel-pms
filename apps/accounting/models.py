from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.users.fields import money
from apps.users.models import SoftDeleteModel, TimeStampedModel


class Account(TimeStampedModel):
    class Type(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"
        EQUITY = "equity", "Equity"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=10, choices=Type.choices)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} {self.name}"


class AccountMapping(TimeStampedModel):
    """Lets the export find accounts by key instead of hard-coded codes.

    Keys to seed: cash, receivables, room_revenue, fnb_revenue, output_vat,
    service_charge_payable, sales_discounts, inventory.
    """

    key = models.CharField(max_length=50, unique=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="mappings")


class JournalEntry(SoftDeleteModel):
    class Source(models.TextChoices):
        NIGHT_AUDIT = "night_audit", "Night audit"
        POS_EOD = "pos_eod", "POS end of day"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        EXPORTED = "exported", "Exported"

    entry_date = models.DateField()
    source = models.CharField(max_length=20, choices=Source.choices)
    reference = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    exported_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "journal entries"
        constraints = (
            # Generating the same day twice cannot create duplicates
            models.UniqueConstraint(
                fields=("entry_date", "source"),
                condition=Q(deleted_at__isnull=True),
                name="uniq_journal_per_day_per_source",
            ),
        )

    # Rule for services.py and its test: sum(debit) == sum(credit) before status leaves DRAFT.


class JournalLine(TimeStampedModel):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    debit = money(default=Decimal(0))
    credit = money(default=Decimal(0))
    memo = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = (
            # Exactly one side is used per line, and it is positive
            models.CheckConstraint(
                condition=(Q(debit=0) & Q(credit__gt=0)) | (Q(credit=0) & Q(debit__gt=0)),
                name="journalline_one_side_positive",
            ),
        )


class ExportBatch(TimeStampedModel):
    """Records what was handed to the accountant."""

    date_from = models.DateField()
    date_to = models.DateField()
    format = models.CharField(max_length=10, default="csv")
    file = models.FileField(upload_to="exports/")
    checksum = models.CharField(max_length=64, blank=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )