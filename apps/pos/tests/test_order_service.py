import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.inventory.models import Item as InventoryItem
from apps.inventory.models import StockMovement
from apps.pos.models import (
    DiningTable,
    MenuCategory,
    MenuItem,
    MenuItemIngredient,
    Order,
    OrderLine,
    OrderPayment,
    Outlet,
    SyncBatch,
    Terminal,
    ZReading,
)
from apps.pos.services import (
    calculate_order_totals,
    create_order,
    generate_z_reading,
    quantize_money,
    send_order_to_station,
    settle_order,
    sync_batch,
    void_order,
)

User = get_user_model()


@pytest.fixture
def pos_context():
    staff = User.objects.create_user(
        email="cashier@hotel.local",
        full_name="Jamie Cruz",
        password="StrongPassword123!",
    )
    outlet = Outlet.objects.create(code="REST-01", name="Main Dining", type=Outlet.Type.RESTAURANT)
    terminal = Terminal.objects.create(outlet=outlet, code="T01", name="Terminal 1")
    table = DiningTable.objects.create(outlet=outlet, name="Table 4", seats=4)
    category = MenuCategory.objects.create(outlet=outlet, name="Mains", sort_order=1)
    steak = MenuItem.objects.create(
        category=category,
        sku="STK-01",
        name="Ribeye Steak",
        price=Decimal("800.00"),
        is_vatable=True,
        senior_pwd_eligible=True,
        station=MenuItem.Station.KITCHEN,
    )
    soda = MenuItem.objects.create(
        category=category,
        sku="DRK-01",
        name="Iced Tea",
        price=Decimal("100.00"),
        is_vatable=False,
        senior_pwd_eligible=True,
        station=MenuItem.Station.BAR,
    )
    return {
        "staff": staff,
        "outlet": outlet,
        "terminal": terminal,
        "table": table,
        "steak": steak,
        "soda": soda,
        "business_date": date(2026, 11, 27),
    }


def test_calculate_order_totals_basic_vatable_and_non_vatable():
    lines = [
        {"unit_price": Decimal("800.00"), "quantity": 1, "is_vatable": True, "discount_eligible": True},
        {"unit_price": Decimal("100.00"), "quantity": 2, "is_vatable": False, "discount_eligible": True},
    ]

    totals = calculate_order_totals(lines=lines, discount_type=Order.DiscountType.NONE)

    assert totals["subtotal"] == Decimal("1000.00")
    assert totals["discount_total"] == Decimal("0.00")
    assert totals["total"] == Decimal("1000.00")


@pytest.mark.django_db
def test_create_order_persists_order_and_snapshots_lines(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        table=pos_context["table"],
        order_type=Order.Type.DINE_IN,
        items=[
            {"menu_item": pos_context["steak"], "quantity": 1, "notes": "Medium rare"},
            {"menu_item": pos_context["soda"], "quantity": 2},
        ],
    )

    assert order.pk is not None
    assert order.status == Order.Status.OPEN
    assert order.subtotal == Decimal("1000.00")
    assert order.total == Decimal("1000.00")
    assert order.lines.count() == 2

   
    pos_context["steak"].price = Decimal("999.00")
    pos_context["steak"].save(update_fields=["price"])

    steak_line = order.lines.get(menu_item=pos_context["steak"])
    assert steak_line.name_snapshot == "Ribeye Steak"
    assert steak_line.unit_price_snapshot == Decimal("800.00")
    assert steak_line.line_total == Decimal("800.00")


@pytest.mark.django_db
def test_create_order_requires_id_when_discount_applied(pos_context):
    with pytest.raises(ValidationError):
        create_order(
            outlet=pos_context["outlet"],
            terminal=pos_context["terminal"],
            opened_by=pos_context["staff"],
            business_date=date(2026, 11, 27),
            discount_type=Order.DiscountType.SENIOR,
            discount_id_number="",
            items=[{"menu_item": pos_context["steak"], "quantity": 1}],
        )




@pytest.mark.django_db
def test_send_order_to_station_marks_pending_lines_and_order_sent(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        table=pos_context["table"],
        items=[
            {"menu_item": pos_context["steak"], "quantity": 1},
            {"menu_item": pos_context["soda"], "quantity": 1},
        ],
    )

    updated = send_order_to_station(order=order)

    assert updated.status == Order.Status.SENT
    assert updated.lines.filter(status=OrderLine.Status.SENT).count() == 2
    assert updated.lines.filter(status=OrderLine.Status.PENDING).count() == 0


@pytest.mark.django_db
def test_send_order_to_station_rejects_void_order(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )
    order.status = Order.Status.VOID
    order.save(update_fields=["status"])

    with pytest.raises(ValidationError):
        send_order_to_station(order=order)


@pytest.mark.django_db
def test_send_order_to_station_rejects_order_with_no_sendable_lines(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )
    order.lines.update(status=OrderLine.Status.VOID)

    with pytest.raises(ValidationError):
        send_order_to_station(order=order)



# tax and vat

def test_calculate_order_totals_regular_vat_inclusive_with_service_charge():

    lines = [
        {"unit_price": Decimal("1120.00"), "quantity": 1, "is_vatable": True, "discount_eligible": True},
    ]

    totals = calculate_order_totals(
        lines=lines,
        discount_type=Order.DiscountType.NONE,
        vat_rate=Decimal("0.12"),
        service_charge_rate=Decimal("0.10"),
    )

    assert totals["subtotal"] == Decimal("1120.00")
    assert totals["vatable_sales"] == Decimal("1000.00")
    assert totals["vat_exempt_sales"] == Decimal("0.00")
    assert totals["vat_amount"] == Decimal("120.00")
    assert totals["discount_total"] == Decimal("0.00")
    # 10% service charge on net vatable sales (1,000.00 * 0.10 = 100.00)
    assert totals["service_charge"] == Decimal("100.00")
    assert totals["total"] == Decimal("1220.00")


def test_calculate_order_totals_senior_pwd_removes_vat_and_applies_20_percent():

    lines = [
        {"unit_price": Decimal("1120.00"), "quantity": 1, "is_vatable": True, "discount_eligible": True},
        {"unit_price": Decimal("224.00"), "quantity": 1, "is_vatable": True, "discount_eligible": False},
    ]

    totals = calculate_order_totals(
        lines=lines,
        discount_type=Order.DiscountType.SENIOR,
        vat_rate=Decimal("0.12"),
        service_charge_rate=Decimal("0.00"),
    )

    assert totals["subtotal"] == Decimal("1344.00")
    assert totals["vat_exempt_sales"] == Decimal("1000.00")
    assert totals["vatable_sales"] == Decimal("200.00")
    assert totals["vat_amount"] == Decimal("24.00")
    assert totals["discount_total"] == Decimal("200.00")

    assert totals["total"] == Decimal("1024.00")

@pytest.mark.django_db
def test_settle_order_rejects_underpayment(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=pos_context["business_date"],
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )

    with pytest.raises(ValidationError):
        settle_order(
            order=order,
            payments=[{"method": OrderPayment.Method.CASH, "amount": Decimal("100.00")}],
            invoice_number="INV-2026-0002",
        )


@pytest.mark.django_db
def test_settle_order_room_charge_requires_folio_charge(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )

    with pytest.raises(ValidationError):
        settle_order(
            order=order,
            payments=[
                {
                    "method": OrderPayment.Method.ROOM_CHARGE,
                    "amount": order.total,
                    "folio_charge": None,
                }
            ],
            invoice_number="INV-2026-0003",
        )




@pytest.mark.django_db
def test_settle_order_records_payments_sets_paid_and_deducts_ingredients(pos_context):
    beef = InventoryItem.objects.create(
        sku="INV-BEEF",
        name="Beef Ribeye",
        unit=InventoryItem.Unit.KG,
        standard_cost=Decimal("400.00"),
        track_stock=True,
    )
    StockMovement.objects.create(
        item=beef,
        movement_type=StockMovement.MovementType.RECEIPT,
        quantity=Decimal("10.000"),
        unit_cost=Decimal("400.00"),
        reference_type="goods_receipt",
        reference_id="GR-001",
    )
    MenuItemIngredient.objects.create(
        menu_item=pos_context["steak"],
        item=beef,
        quantity=Decimal("0.500"),
    )

    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 2}],
    )

    settled = settle_order(
        order=order,
        payments=[
            {"method": OrderPayment.Method.CASH, "amount": Decimal("500.00")},
            {"method": OrderPayment.Method.CARD, "amount": Decimal("1100.00")},
        ],
        invoice_number="INV-2026-0001",
    )

    assert settled.status == Order.Status.PAID
    assert settled.invoice_number == "INV-2026-0001"
    assert settled.paid_at is not None
    assert settled.payments.count() == 2
    assert settled.lines.filter(status=OrderLine.Status.VOID).count() == 0

    movement = StockMovement.objects.get(
        item=beef,
        movement_type=StockMovement.MovementType.SALE_DEDUCTION,
        reference_type="pos_order",
        reference_id=str(order.pk),
    )
    assert movement.quantity == Decimal("-1.000")
    assert beef.stock_on_hand == Decimal("9.000")







@pytest.mark.django_db
def test_void_open_order_marks_lines_void_without_stock_movement(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )

    voided = void_order(
        order=order,
        voided_by=pos_context["staff"],
        void_reason="Customer cancelled before prep",
    )

    assert voided.status == Order.Status.VOID
    assert voided.voided_by == pos_context["staff"]
    assert voided.void_reason == "Customer cancelled before prep"
    assert voided.lines.filter(status=OrderLine.Status.VOID).count() == 1
    assert StockMovement.objects.filter(reference_id=str(order.pk)).count() == 0


@pytest.mark.django_db
def test_void_paid_order_restores_deducted_inventory(pos_context):
    beef = InventoryItem.objects.create(
        sku="INV-BEEF-VOID",
        name="Beef Ribeye",
        unit=InventoryItem.Unit.KG,
        standard_cost=Decimal("400.00"),
        track_stock=True,
    )
    StockMovement.objects.create(
        item=beef,
        movement_type=StockMovement.MovementType.RECEIPT,
        quantity=Decimal("10.000"),
        unit_cost=Decimal("400.00"),
        reference_type="goods_receipt",
        reference_id="GR-VOID-001",
    )
    MenuItemIngredient.objects.create(
        menu_item=pos_context["steak"],
        item=beef,
        quantity=Decimal("0.500"),
    )

    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 2}],
    )
    settle_order(
        order=order,
        payments=[{"method": OrderPayment.Method.CASH, "amount": Decimal("1600.00")}],
        invoice_number="INV-2026-VOID-01",
    )
    assert beef.stock_on_hand == Decimal("9.000")

    voided = void_order(
        order=order,
        voided_by=pos_context["staff"],
        void_reason="Wrong order billed",
    )

    assert voided.status == Order.Status.VOID
    restore = StockMovement.objects.get(
        item=beef,
        movement_type=StockMovement.MovementType.VOID_RESTORE,
        reference_type="pos_order",
        reference_id=str(order.pk),
    )
    assert restore.quantity == Decimal("1.000")
    assert beef.stock_on_hand == Decimal("10.000")


@pytest.mark.django_db
def test_void_order_requires_reason_and_rejects_already_void(pos_context):
    order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=date(2026, 11, 27),
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )

    with pytest.raises(ValidationError):
        void_order(order=order, voided_by=pos_context["staff"], void_reason="   ")

    void_order(order=order, voided_by=pos_context["staff"], void_reason="Duplicate ticket")

    with pytest.raises(ValidationError):
        void_order(order=order, voided_by=pos_context["staff"], void_reason="Second void attempt")




@pytest.mark.django_db
def test_sync_batch_creates_offline_order_and_handles_duplicates(pos_context):
    order_uuid = uuid.uuid4()
    batch_uuid = uuid.uuid4()
    payload_orders = [
        {
            "id": str(order_uuid),
            "business_date": "2026-11-27",
            "order_type": Order.Type.DINE_IN,
            "table_id": pos_context["table"].pk,
            "discount_type": Order.DiscountType.NONE,
            "discount_id_number": "",
            "client_created_at": "2026-11-27T08:15:00Z",
            "items": [
                {"menu_item_id": pos_context["steak"].pk, "quantity": 1, "notes": "Offline order"}
            ],
        }
    ]

    first_batch = sync_batch(
        batch_id=batch_uuid,
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        uploaded_by=pos_context["staff"],
        orders_payload=payload_orders,
    )

    assert isinstance(first_batch, SyncBatch)
    assert first_batch.created_count == 1
    assert first_batch.duplicate_count == 0
    assert first_batch.conflict_count == 0
    assert first_batch.rejected_count == 0
    assert Order.objects.filter(pk=order_uuid, created_offline=True).count() == 1

    # Re-upload the same order in a retry batch
    retry_batch = sync_batch(
        batch_id=uuid.uuid4(),
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        uploaded_by=pos_context["staff"],
        orders_payload=payload_orders,
    )

    assert retry_batch.created_count == 0
    assert retry_batch.duplicate_count == 1
    assert Order.objects.filter(pk=order_uuid).count() == 1


@pytest.mark.django_db
def test_sync_batch_detects_conflict_on_mismatched_order_data(pos_context):
    order_uuid = uuid.uuid4()
    base_order = {
        "id": str(order_uuid),
        "business_date": "2026-11-27",
        "order_type": Order.Type.DINE_IN,
        "table_id": pos_context["table"].pk,
        "discount_type": Order.DiscountType.NONE,
        "discount_id_number": "",
        "items": [{"menu_item_id": pos_context["steak"].pk, "quantity": 1}],
    }

    sync_batch(
        batch_id=uuid.uuid4(),
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        uploaded_by=pos_context["staff"],
        orders_payload=[base_order],
    )

    conflicting_order = {
        **base_order,
        "items": [{"menu_item_id": pos_context["soda"].pk, "quantity": 1}],
    }

    conflict_batch = sync_batch(
        batch_id=uuid.uuid4(),
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        uploaded_by=pos_context["staff"],
        orders_payload=[conflicting_order],
    )

    assert conflict_batch.created_count == 0
    assert conflict_batch.conflict_count == 1
    assert conflict_batch.results[0]["status"] == "conflict"





@pytest.mark.django_db
def test_generate_z_reading_aggregates_paid_orders_and_payments(pos_context):
    biz_date = date(2026, 11, 27)

    order_1 = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=biz_date,
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )
    settle_order(
        order=order_1,
        payments=[{"method": OrderPayment.Method.CASH, "amount": order_1.total}],
        invoice_number="INV-2026-0101",
    )

    order_2 = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=biz_date,
        items=[{"menu_item": pos_context["soda"], "quantity": 2}],
    )
    settle_order(
        order=order_2,
        payments=[{"method": OrderPayment.Method.CARD, "amount": order_2.total}],
        invoice_number="INV-2026-0102",
    )

    voided_order = create_order(
        outlet=pos_context["outlet"],
        terminal=pos_context["terminal"],
        opened_by=pos_context["staff"],
        business_date=biz_date,
        items=[{"menu_item": pos_context["steak"], "quantity": 1}],
    )
    void_order(
        order=voided_order,
        voided_by=pos_context["staff"],
        void_reason="Cancelled by guest",
    )

    z = generate_z_reading(
        terminal=pos_context["terminal"],
        business_date=biz_date,
        generated_by=pos_context["staff"],
    )

    assert isinstance(z, ZReading)
    assert z.terminal == pos_context["terminal"]
    assert z.business_date == biz_date
    assert z.generated_by == pos_context["staff"]
    assert z.first_invoice_no == "INV-2026-0101"
    assert z.last_invoice_no == "INV-2026-0102"
    assert z.gross_sales == quantize_money(order_1.subtotal + order_2.subtotal)
    assert z.vat_amount == quantize_money(order_1.vat_amount + order_2.vat_amount)
    assert z.service_charge == quantize_money(order_1.service_charge + order_2.service_charge)
    assert z.discounts == quantize_money(order_1.discount_total + order_2.discount_total)
    assert Decimal(str(z.totals_by_method[OrderPayment.Method.CASH])) == order_1.total
    assert Decimal(str(z.totals_by_method[OrderPayment.Method.CARD])) == order_2.total


@pytest.mark.django_db
def test_generate_z_reading_prevents_duplicate_for_terminal_and_date(pos_context):
    biz_date = date(2026, 11, 27)
    generate_z_reading(
        terminal=pos_context["terminal"],
        business_date=biz_date,
        generated_by=pos_context["staff"],
    )

    with pytest.raises(ValidationError):
        generate_z_reading(
            terminal=pos_context["terminal"],
            business_date=biz_date,
            generated_by=pos_context["staff"],
        )