"""apps/pos/models.py

Recipes (MenuItemIngredient) live here, not in inventory, because pos depends on
inventory and the reverse would be a circular dependency.

Migration note: needs BtreeGistExtension (already added by the property app's first
migration) for the InvoiceBlock exclusion constraint. Make sure pos migrates after property.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.fields import IntegerRangeField
from django.db import models
from django.db.models import F, Func, Q

from apps.users.fields import money, qty_field
from apps.users.models import SoftDeleteModel, TimeStampedModel


# ------------------------------------------------------------ outlets, terminals
class Outlet(TimeStampedModel):
    class Type(models.TextChoices):
        RESTAURANT = "restaurant", "Restaurant"
        BAR = "bar", "Bar"
        ROOM_SERVICE = "room_service", "Room service"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.RESTAURANT)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Terminal(TimeStampedModel):
    outlet = models.ForeignKey(Outlet, on_delete=models.PROTECT, related_name="terminals")
    code = models.CharField(max_length=20, unique=True)  # e.g. "T01"
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.code


class IntRangeFunc(Func):
    function = "int4range"

    def __init__(self, *expressions, **extra):
        extra.setdefault("output_field", IntegerRangeField())
        super().__init__(*expressions, **extra)


class InvoiceBlock(TimeStampedModel):
    """A range of invoice numbers pre-allocated to one terminal for offline use."""

    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="invoice_blocks")
    series = models.CharField(max_length=30)  # matches core.InvoiceSequence.series
    start_number = models.PositiveIntegerField()
    end_number = models.PositiveIntegerField()
    next_number = models.PositiveIntegerField()
    allocated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(end_number__gte=F("start_number")), name="invoiceblock_end_gte_start"
            ),
            models.CheckConstraint(
                condition=Q(next_number__gte=F("start_number")) & Q(next_number__lte=F("end_number") + 1),
                name="invoiceblock_next_in_range",
            ),
        )


# ----------------------------------------------------------------------- menu
class DiningTable(TimeStampedModel):
    outlet = models.ForeignKey(Outlet, on_delete=models.PROTECT, related_name="tables")
    name = models.CharField(max_length=30)
    seats = models.PositiveSmallIntegerField(default=2)

    class Meta:
        constraints = (models.UniqueConstraint(fields=("outlet", "name"), name="uniq_table_per_outlet"),)


class MenuCategory(TimeStampedModel):
    outlet = models.ForeignKey(Outlet, on_delete=models.PROTECT, related_name="categories")
    name = models.CharField(max_length=100)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "name")
        verbose_name_plural = "menu categories"


class MenuItem(TimeStampedModel):
    class Station(models.TextChoices):
        KITCHEN = "kitchen", "Kitchen"
        BAR = "bar", "Bar"

    category = models.ForeignKey(MenuCategory, on_delete=models.PROTECT, related_name="items")
    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=150)
    price = money()
    is_vatable = models.BooleanField(default=True)
    senior_pwd_eligible = models.BooleanField(default=True)
    station = models.CharField(max_length=10, choices=Station.choices, default=Station.KITCHEN)
    is_available = models.BooleanField(default=True)
    # updated_at (from TimeStampedModel) feeds the menu snapshot ETag for offline terminals

    class Meta:
        constraints = (models.CheckConstraint(condition=Q(price__gte=0), name="menuitem_price_gte_0"),)

    def __str__(self):
        return self.name


class MenuItemIngredient(TimeStampedModel):
    """The recipe. Paid orders deduct these quantities from inventory."""

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="ingredients")
    item = models.ForeignKey("inventory.Item", on_delete=models.PROTECT, related_name="+")
    quantity = qty_field()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("menu_item", "item"), name="uniq_recipe_ingredient"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="recipe_quantity_positive"),
        )


# --------------------------------------------------------------------- orders
class Order(SoftDeleteModel):
    class Type(models.TextChoices):
        DINE_IN = "dine_in", "Dine in"
        TAKEOUT = "takeout", "Takeout"
        ROOM_SERVICE = "room_service", "Room service"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SENT = "sent", "Sent to kitchen"
        SERVED = "served", "Served"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    class DiscountType(models.TextChoices):
        NONE = "none", "None"
        SENIOR = "senior", "Senior citizen"
        PWD = "pwd", "PWD"

    class SyncStatus(models.TextChoices):
        SYNCED = "synced", "Synced"
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        CONFLICT = "conflict", "Conflict"

    # Client-generated on the terminal, so syncing the same order twice is harmless
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    outlet = models.ForeignKey(Outlet, on_delete=models.PROTECT, related_name="orders")
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="orders")
    table = models.ForeignKey(
        DiningTable, null=True, blank=True, on_delete=models.PROTECT, related_name="orders"
    )
    order_type = models.CharField(max_length=20, choices=Type.choices, default=Type.DINE_IN)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    invoice_number = models.CharField(max_length=40, blank=True)

    # Stored snapshots: totals are never recomputed from today's prices
    subtotal = money(default=Decimal(0))
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_id_number = models.CharField(max_length=50, blank=True)
    discount_total = money(default=Decimal(0))
    service_charge = money(default=Decimal(0))
    vat_amount = money(default=Decimal(0))
    total = money(default=Decimal(0))

    folio = models.ForeignKey(
        "property.Folio", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )  # set when charged to a room

    # Offline sync
    sync_status = models.CharField(max_length=25, choices=SyncStatus.choices, default=SyncStatus.SYNCED)
    created_offline = models.BooleanField(default=False)
    client_created_at = models.DateTimeField(null=True, blank=True)  # device clock
    synced_at = models.DateTimeField(null=True, blank=True)

    business_date = models.DateField()
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = (
            models.Index(fields=("business_date", "status")),
            models.Index(fields=("terminal", "business_date")),
        )
        constraints = (
            models.UniqueConstraint(
                fields=("invoice_number",), condition=~Q(invoice_number=""), name="uniq_order_invoice_number"
            ),
            # Senior/PWD discount needs an ID number
            models.CheckConstraint(
                condition=Q(discount_type="none") | ~Q(discount_id_number=""),
                name="order_discount_requires_id",
            ),
        )


class OrderLine(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        SERVED = "served", "Served"
        VOID = "void", "Void"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="+")
    # Snapshots protect history when the menu changes
    name_snapshot = models.CharField(max_length=150)
    unit_price_snapshot = money()
    quantity = models.PositiveSmallIntegerField(default=1)
    line_total = money()
    is_vatable = models.BooleanField(default=True)
    discount_eligible = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = (models.CheckConstraint(condition=Q(quantity__gte=1), name="orderline_quantity_gte_1"),)


class OrderPayment(TimeStampedModel):
    """Split payments are several rows on one order."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        GCASH = "gcash", "GCash"
        ROOM_CHARGE = "room_charge", "Charge to room"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=20, choices=Method.choices)
    amount = money()
    payment = models.ForeignKey(
        "payments.Payment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    folio_charge = models.ForeignKey(
        "property.FolioCharge", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        constraints = (
            models.CheckConstraint(condition=Q(amount__gt=0), name="orderpayment_amount_positive"),
            # A room charge must point at the folio charge it created
            models.CheckConstraint(
                condition=~Q(method="room_charge") | Q(folio_charge__isnull=False),
                name="roomcharge_requires_folio_charge",
            ),
        )


# ----------------------------------------------------------------------- sync
class SyncBatch(TimeStampedModel):
    """Audit trail for each offline upload, and a second layer of idempotency."""

    batch_id = models.UUIDField(unique=True)  # generated by the terminal
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="sync_batches")
    received_at = models.DateTimeField(auto_now_add=True)
    order_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    conflict_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    results = models.JSONField(default=list, blank=True)  # per-order outcome


class ZReading(SoftDeleteModel):
    """End-of-day record per terminal (BIR-style Z-reading)."""

    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, related_name="z_readings")
    business_date = models.DateField()
    gross_sales = money(default=Decimal(0))
    vatable_sales = money(default=Decimal(0))
    vat_exempt_sales = money(default=Decimal(0))
    vat_amount = money(default=Decimal(0))
    discounts = money(default=Decimal(0))
    service_charge = money(default=Decimal(0))
    totals_by_method = models.JSONField(default=dict, blank=True)
    first_invoice_no = models.CharField(max_length=40, blank=True)
    last_invoice_no = models.CharField(max_length=40, blank=True)
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("terminal", "business_date"), name="uniq_zreading_per_day"),
        )