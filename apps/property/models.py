from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.users.fields import money
from apps.users.models import SoftDeleteModel, TimeStampedModel


class RoomType(TimeStampedModel):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    base_occupancy = models.PositiveSmallIntegerField(default=2)
    max_occupancy = models.PositiveSmallIntegerField(default=2)
    base_rate = money()
    extra_guest_fee = money(default=Decimal("0.00"))
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(max_occupancy__gte=F("base_occupancy")),
                name="roomtype_max_gte_base_occupancy",
            ),
        )

    def __str__(self):
        return self.name


class Room(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        OCCUPIED = "occupied", "Occupied"
        DIRTY = "dirty", "Dirty"
        OUT_OF_ORDER = "out_of_order", "Out of order"

    number = models.CharField(max_length=10, unique=True)
    floor = models.SmallIntegerField(default=1)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.number


class Rate(TimeStampedModel):
    class Kind(models.TextChoices):
        WEEKEND = "weekend", "Weekend"
        SEASONAL = "seasonal", "Seasonal"
        HOLIDAY = "holiday", "Holiday"

    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rates")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = money()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    # 0 = Monday ... 6 = Sunday; empty list means every day. Validated in services.py.
    days_of_week = models.JSONField(default=list, blank=True)
    priority = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = (
            models.CheckConstraint(
                condition=Q(start_date__isnull=True)
                | Q(end_date__isnull=True)
                | Q(end_date__gte=F("start_date")),
                name="rate_end_gte_start",
            ),
        )


class Guest(TimeStampedModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.CharField(max_length=255, blank=True)
    id_type = models.CharField(max_length=50, blank=True)
    id_number = models.CharField(max_length=50, blank=True)
    is_senior = models.BooleanField(default=False)
    is_pwd = models.BooleanField(default=False)
    discount_id_number = models.CharField(max_length=50, blank=True)

    class Meta:
        indexes = (models.Index(fields=["email"]), models.Index(fields=["phone"]),)
        constraints = (
            models.CheckConstraint(
                condition=(Q(is_senior=False) & Q(is_pwd=False)) | ~Q(discount_id_number=""),
                name="guest_discount_requires_id",
            ),
        )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Reservation(SoftDeleteModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked in"
        CHECKED_OUT = "checked_out", "Checked out"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No show"
        EXPIRED = "expired", "Expired"

    class Source(models.TextChoices):
        DIRECT = "direct", "Direct (website)"
        WALK_IN = "walk_in", "Walk-in"
        PHONE = "phone", "Phone"

    ACTIVE_STATUSES = ("pending", "confirmed", "checked_in",)

    code = models.CharField(max_length=20, unique=True)
    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name="reservations")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="+")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="reservations")
    check_in = models.DateField()
    check_out = models.DateField()
    adults = models.PositiveSmallIntegerField(default=1)
    children = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.DIRECT)

    total_amount = money(default=Decimal("0.00"))
    rate_snapshot = models.JSONField(default=dict, blank=True)

    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    cancellation_fee = money(default=Decimal("0.00"))
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        indexes = (models.Index(fields=["status", "check_in"]),)
        constraints = (
            models.CheckConstraint(
                condition=Q(check_out__gt=F("check_in")),
                name="reservation_checkout_after_checkin",
            ),
        )

    def __str__(self):
        return self.code


class Folio(SoftDeleteModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    reservation = models.OneToOneField(Reservation, on_delete=models.PROTECT, related_name="folio")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    credit_limit = money(default=Decimal("0.00"))
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)


class FolioCharge(SoftDeleteModel):
    class ChargeType(models.TextChoices):
        ROOM = "room", "Room"
        FNB = "fnb", "Food and beverage"
        MISC = "misc", "Miscellaneous"
        TAX = "tax", "Tax"
        SERVICE_CHARGE = "service_charge", "Service charge"
        DISCOUNT = "discount", "Discount"
        ADJUSTMENT = "adjustment", "Adjustment"

    class Source(models.TextChoices):
        NIGHT_AUDIT = "night_audit", "Night audit"
        POS = "pos", "POS"
        MANUAL = "manual", "Manual"

    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="charges")
    charge_type = models.CharField(max_length=20, choices=ChargeType.choices)
    description = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField(default=1)
    unit_amount = money()
    amount = money()
    business_date = models.DateField()
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    source_ref = models.CharField(max_length=64, blank=True)

    is_voided = models.BooleanField(default=False)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=255, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        indexes = (models.Index(fields=["folio", "business_date"]),)
        constraints = (
            models.UniqueConstraint(
                fields=["folio", "business_date"],
                condition=Q(charge_type="room", is_voided=False),
                name="uniq_room_charge_per_folio_per_night",
            ),
            models.UniqueConstraint(
                fields=["source", "source_ref", "charge_type"],
                condition=~Q(source_ref="") & Q(is_voided=False),
                name="uniq_charge_per_source_ref",
            ),
            models.CheckConstraint(condition=Q(quantity__gte=1), name="foliocharge_quantity_gte_1"),
        )


class FolioPayment(TimeStampedModel):
    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="payments")
    payment = models.OneToOneField("payments.Payment", on_delete=models.PROTECT, related_name="+")


class Invoice(SoftDeleteModel):
    number = models.CharField(max_length=40, unique=True)
    folio = models.ForeignKey(Folio, on_delete=models.PROTECT, related_name="invoices")
    issued_at = models.DateTimeField(default=timezone.now)
    vatable_sales = money(default=Decimal("0.00"))
    vat_exempt_sales = money(default=Decimal("0.00"))
    vat_amount = money(default=Decimal("0.00"))
    discount_total = money(default=Decimal("0.00"))
    service_charge = money(default=Decimal("0.00"))
    total = money(default=Decimal("0.00"))
    customer_snapshot = models.JSONField(default=dict, blank=True)
    is_void = models.BooleanField(default=False)


class NightAuditRun(TimeStampedModel):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    business_date = models.DateField(unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    charges_posted = models.PositiveIntegerField(default=0)
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )