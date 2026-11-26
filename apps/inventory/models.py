from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from apps.users.fields import money, qty_field
from apps.users.models import TimeStampedModel


class ItemCategory(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "item categories"

    def __str__(self):
        return self.name


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=150)
    contact_person = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    tin = models.CharField(max_length=20, blank=True)  # BIR Tax Identification Number
    address = models.CharField(max_length=255, blank=True)
    payment_terms = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Item(TimeStampedModel):
    class Unit(models.TextChoices):
        KG = "kg", "Kilogram"
        G = "g", "Gram"
        L = "l", "Liter"
        ML = "ml", "Milliliter"
        PC = "pc", "Piece"

    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    category = models.ForeignKey(
        ItemCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="items"
    )
    unit = models.CharField(max_length=5, choices=Unit.choices, default=Unit.PC)
    reorder_level = qty_field(default=Decimal(0))
    reorder_qty = qty_field(default=Decimal(0))
    standard_cost = money(default=Decimal(0))
    track_stock = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    @property
    def stock_on_hand(self) -> Decimal:
        """The ledger is the truth: stock equals the sum of its movements."""
        total = StockMovement.objects.filter(item=self).aggregate(total=Sum("quantity"))["total"]
        return total or Decimal(0)

    def __str__(self):
        return f"{self.sku} {self.name}"


class StockMovement(TimeStampedModel):
    """Append-only ledger. Quantities are signed: receipts positive, deductions negative."""

    class MovementType(models.TextChoices):
        RECEIPT = "receipt", "Receipt"
        SALE_DEDUCTION = "sale_deduction", "Sale deduction"
        VOID_RESTORE = "void_restore", "Void restore"
        ADJUSTMENT = "adjustment", "Adjustment"
        WASTE = "waste", "Waste"

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    quantity = qty_field()
    unit_cost = money(default=Decimal(0))
    reference_type = models.CharField(max_length=30, blank=True)  # "pos_order", "goods_receipt"
    reference_id = models.CharField(max_length=64, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = (models.Index(fields=("item", "occurred_at")),)
        constraints = (
            models.CheckConstraint(condition=~Q(quantity=0), name="stockmovement_quantity_nonzero"),
            # Deducting for the same order and item twice is impossible
            models.UniqueConstraint(
                fields=("reference_type", "reference_id", "item", "movement_type"),
                condition=~Q(reference_id=""),
                name="uniq_movement_per_reference",
            ),
        )


class PurchaseOrder(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        PARTIALLY_RECEIVED = "partially_received", "Partially received"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    po_number = models.CharField(max_length=30, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    order_date = models.DateField(default=timezone.localdate)
    expected_date = models.DateField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    total = money(default=Decimal(0))


class PurchaseOrderLine(TimeStampedModel):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    qty_ordered = qty_field()
    qty_received = qty_field(default=Decimal(0))
    unit_cost = money()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("purchase_order", "item"), name="uniq_po_item"),
            models.CheckConstraint(condition=Q(qty_ordered__gt=0), name="poline_qty_ordered_positive"),
            models.CheckConstraint(condition=Q(qty_received__gte=0), name="poline_qty_received_gte_0"),
        )


class GoodsReceipt(TimeStampedModel):
    """One per delivery, so partial receiving works."""

    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    received_at = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    supplier_invoice_no = models.CharField(max_length=50, blank=True)


class GoodsReceiptLine(TimeStampedModel):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    quantity = qty_field()
    unit_cost = money()

    # Saving a line creates a StockMovement(type=receipt) in services.py.