from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.inventory.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    StockMovement,
)


@transaction.atomic
def receive_goods(
    *,
    purchase_order: PurchaseOrder,
    receipts_data: list[dict],  # [{"po_line": PurchaseOrderLine, "quantity": Decimal, "unit_cost": Decimal}]
    received_by=None,
    supplier_invoice_no: str = "",
) -> GoodsReceipt:
    po = PurchaseOrder.objects.select_for_update().get(pk=purchase_order.pk)
    if po.status == PurchaseOrder.Status.CANCELLED:
        raise ValidationError("Cannot receive goods for a cancelled purchase order.")

    receipt = GoodsReceipt.objects.create(
        purchase_order=po,
        received_by=received_by,
        supplier_invoice_no=supplier_invoice_no,
    )

    any_received = False

    for entry in receipts_data:
        po_line = (
            PurchaseOrderLine.objects.select_for_update()
            .select_related("item")
            .get(pk=entry["po_line"].pk, purchase_order=po)
        )
        qty = Decimal(str(entry["quantity"]))
        unit_cost = Decimal(str(entry.get("unit_cost", po_line.unit_cost)))

        if qty <= Decimal(0):
            raise ValidationError("Received quantity must be greater than zero.")

        if po_line.qty_received + qty > po_line.qty_ordered:
            raise ValidationError(
                f"Cannot receive {qty} of {po_line.item.sku}: exceeds ordered quantity."
            )

        GoodsReceiptLine.objects.create(
            receipt=receipt,
            po_line=po_line,
            quantity=qty,
            unit_cost=unit_cost,
        )

        PurchaseOrderLine.objects.filter(pk=po_line.pk).update(
            qty_received=F("qty_received") + qty,
        )

        StockMovement.objects.create(
            item=po_line.item,
            movement_type=StockMovement.MovementType.RECEIPT,
            quantity=qty,
            unit_cost=unit_cost,
            reference_type="goods_receipt",
            reference_id=str(receipt.pk),
            occurred_at=timezone.now(),
            created_by=received_by,
            notes=f"PO {po.po_number}",
        )
        any_received = True

    all_fully_received = all(
        qty_received >= qty_ordered
        for qty_received, qty_ordered in PurchaseOrderLine.objects.filter(
            purchase_order=po
        ).values_list("qty_received", "qty_ordered")
    )

    if all_fully_received:
        po.status = PurchaseOrder.Status.RECEIVED
    elif any_received:
        po.status = PurchaseOrder.Status.PARTIALLY_RECEIVED

    po.save(update_fields=["status", "updated_at"])
    return receipt