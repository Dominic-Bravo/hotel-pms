from datetime import date
from typing import Any

from django.db import transaction
from django.utils import timezone

# Adjust this import to `apps.core.models` if these models live in `core`.
from apps.users.models import AuditLog, InvoiceSequence, TaxSetting


def record_audit_log(
    *,
    actor: Any,
    action: str,
    model_label: str,
    object_id: str | int,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    ip_address: str | None = None,
) -> AuditLog:
    return AuditLog.objects.create(
        actor=actor,
        action=action,
        model_label=model_label,
        object_id=str(object_id),
        before=before,
        after=after,
        reason=reason,
        ip_address=ip_address,
    )


@transaction.atomic
def next_invoice_number(series: str) -> str:
    """Lock the sequence row, increment `next_number`, and return the formatted number."""
    seq = InvoiceSequence.objects.select_for_update().get(series=series)
    number = seq.next_number
    seq.next_number = number + 1
    seq.save(update_fields=["next_number", "updated_at"])
    return f"{seq.prefix}{number:0{seq.padding}d}"


def get_tax_setting(on_date: date | None = None) -> TaxSetting | None:
    target_date = on_date or timezone.localdate()
    return (
        TaxSetting.objects.filter(effective_from__lte=target_date)
        .order_by("-effective_from")
        .first()
    )