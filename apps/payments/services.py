from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from apps.payments.models import Payment, Refund


@transaction.atomic
def create_payment(
    *,
    method: str,
    amount: Decimal,
    idempotency_key: str | None = None,
    tendered_amount: Decimal | None = None,
    source_app: str = "",
    source_ref: str = "",
    provider: str = "simulated",
    provider_ref: str = "",
    created_by=None,
) -> Payment:
    if idempotency_key:
        existing = (
            Payment.objects.select_for_update()
            .filter(idempotency_key=idempotency_key)
            .first()
        )
        if existing:
            return existing

    amt = Decimal(str(amount))
    if amt <= Decimal(0):
        raise ValidationError("Payment amount must be greater than zero.")

    tendered: Decimal | None = None
    change_amount: Decimal | None = None
    if method == Payment.Method.CASH and tendered_amount is not None:
        tendered = Decimal(str(tendered_amount))
        if tendered < amt:
            raise ValidationError("Tendered amount cannot be less than payment amount.")
        change_amount = tendered - amt

    return Payment.objects.create(
        method=method,
        amount=amt,
        idempotency_key=idempotency_key,
        tendered_amount=tendered,
        change_amount=change_amount,
        source_app=source_app,
        source_ref=source_ref,
        provider=provider,
        provider_ref=provider_ref,
        created_by=created_by,
    )


@transaction.atomic
def create_refund(
    *,
    payment: Payment,
    amount: Decimal,
    reason: str,
    approved_by=None,
    provider_ref: str = "",
) -> Refund:
    locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
    refund_amt = Decimal(str(amount))

    if refund_amt <= Decimal(0):
        raise ValidationError("Refund amount must be greater than zero.")

    existing_total = (
        Refund.objects.filter(payment=locked_payment)
        .exclude(status=Refund.Status.FAILED)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal(0)
    )

    if existing_total + refund_amt > locked_payment.amount:
        raise ValidationError("Total refunds cannot exceed the original payment amount.")

    refund = Refund.objects.create(
        payment=locked_payment,
        amount=refund_amt,
        reason=reason,
        approved_by=approved_by,
        provider_ref=provider_ref,
        status=Refund.Status.SUCCEEDED,
    )

    new_total = existing_total + refund_amt
    locked_payment.status = (
        Payment.Status.REFUNDED
        if new_total == locked_payment.amount
        else Payment.Status.PARTIALLY_REFUNDED
    )
    locked_payment.save(update_fields=["status", "updated_at"])
    return refund