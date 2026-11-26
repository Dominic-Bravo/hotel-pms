import pytest

from apps.users.models import InvoiceSequence
from apps.users.services import next_invoice_number


@pytest.mark.django_db
def test_next_invoice_number_increments():
    InvoiceSequence.objects.create(
        series="ROOM",
        prefix="INV-",
        next_number=1,
        padding=4,
    )

    assert next_invoice_number("ROOM") == "INV-0001"
    assert next_invoice_number("ROOM") == "INV-0002"