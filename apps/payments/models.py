import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.users.fields import money
from apps.users.models import SoftDeleteModel


class Payment(SoftDeleteModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        GCASH = "gcash", "GCash"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        PARTIALLY_REFUNDED = "partially_refunded", "Partially refunded"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    method = models.CharField(max_length=20, choices=Method.choices)
    amount = money()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=30, default="simulated")
    provider_ref = models.CharField(max_length=100, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)

    # Cash only
    tendered_amount = money(null=True, blank=True)
    change_amount = money(null=True, blank=True)

    # What this paid for, as plain text so `payments` never imports other apps
    source_app = models.CharField(max_length=30, blank=True)  # "property" or "pos"
    source_ref = models.CharField(max_length=64, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        indexes = (models.Index(fields=("source_app", "source_ref")),)
        constraints = (
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
        )

    def __str__(self):
        return f"{self.method} {self.amount} ({self.status})"


class Refund(SoftDeleteModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = money()
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider_ref = models.CharField(max_length=100, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = (
            models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive"),
        )