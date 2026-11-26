from django.core.exceptions import ValidationError
from django.db import transaction

from apps.pos.models import InvoiceBlock, Terminal


@transaction.atomic
def allocate_invoice_block(
    *,
    terminal: Terminal,
    series: str,
    start_number: int,
    end_number: int,
) -> InvoiceBlock:
    if end_number < start_number:
        raise ValidationError("end_number must be greater than or equal to start_number.")

    # Serialize block allocation for this series
    list(InvoiceBlock.objects.select_for_update().filter(series=series))

    overlapping = InvoiceBlock.objects.filter(
        series=series,
        start_number__lte=end_number,
        end_number__gte=start_number,
    ).exists()

    if overlapping:
        raise ValidationError(
            f"Invoice range {start_number}-{end_number} overlaps an existing block in series '{series}'."
        )

    return InvoiceBlock.objects.create(
        terminal=terminal,
        series=series,
        start_number=start_number,
        end_number=end_number,
        next_number=start_number,
    )