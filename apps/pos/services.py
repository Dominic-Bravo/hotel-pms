import uuid
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.pos.models import (
    DiningTable,
    MenuItem,
    Order,
    OrderLine,
    OrderPayment,
    Outlet,
    SyncBatch,
    Terminal,
    ZReading,
)

TWO_PLACES = Decimal("0.01")


def quantize_money(amount: Decimal) -> Decimal:
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def calculate_order_totals(
    *,
    lines: list[dict[str, Any]],
    discount_type: str = Order.DiscountType.NONE,
    service_charge_rate: Decimal = Decimal("0.00"),
    vat_rate: Decimal = Decimal("0.00"),
) -> dict[str, Decimal]:
    """Compute BIR-compliant VAT-inclusive totals, Senior/PWD discounts, and service charge."""
    subtotal = Decimal("0.00")
    vatable_gross = Decimal("0.00")
    vat_exempt_sales = Decimal("0.00")
    discount_total = Decimal("0.00")

    is_statutory_discount = discount_type in (
        Order.DiscountType.SENIOR,
        Order.DiscountType.PWD,
    )
    vat_divisor = Decimal("1.00") + vat_rate

    for line in lines:
        unit_price = Decimal(str(line["unit_price"]))
        quantity = int(line["quantity"])
        line_gross = quantize_money(unit_price * quantity)
        is_vatable = line.get("is_vatable", True)
        discount_eligible = line.get("discount_eligible", True)

        subtotal += line_gross

        if is_statutory_discount and discount_eligible:
            # Strip VAT first if item was priced VAT-inclusive, then apply 20% discount
            net_base = (
                quantize_money(line_gross / vat_divisor)
                if (is_vatable and vat_rate > Decimal("0.00"))
                else line_gross
            )
            vat_exempt_sales += net_base
            discount_total += quantize_money(net_base * Decimal("0.20"))
        elif is_vatable:
            vatable_gross += line_gross
        else:
            vat_exempt_sales += line_gross

    subtotal = quantize_money(subtotal)
    vat_exempt_sales = quantize_money(vat_exempt_sales)
    discount_total = quantize_money(discount_total)

    if vatable_gross > Decimal("0.00") and vat_rate > Decimal("0.00"):
        vatable_sales = quantize_money(vatable_gross / vat_divisor)
        vat_amount = quantize_money(vatable_gross - vatable_sales)
    else:
        vatable_sales = quantize_money(vatable_gross)
        vat_amount = Decimal("0.00")

    net_sales_before_sc = vatable_sales + vat_exempt_sales
    service_charge = quantize_money(net_sales_before_sc * service_charge_rate)
    total = quantize_money(
        vatable_sales + vat_amount + vat_exempt_sales - discount_total + service_charge
    )

    return {
        "subtotal": subtotal,
        "vatable_sales": vatable_sales,
        "vat_exempt_sales": vat_exempt_sales,
        "vat_amount": vat_amount,
        "discount_total": discount_total,
        "service_charge": service_charge,
        "total": total,
    }


@transaction.atomic
def create_order(
    *,
    outlet: Outlet,
    terminal: Terminal,
    opened_by: Any,
    business_date: date,
    items: list[dict[str, Any]],
    table: DiningTable | None = None,
    order_type: str = Order.Type.DINE_IN,
    discount_type: str = Order.DiscountType.NONE,
    discount_id_number: str = "",
    created_offline: bool = False,
    client_created_at: Any = None,
) -> Order:
    """Create an Order and snapshot MenuItem attributes into OrderLine rows."""
    if not items:
        raise ValidationError("Order must contain at least one item.")

    if discount_type != Order.DiscountType.NONE and not discount_id_number.strip():
        raise ValidationError("Discount ID number is required when a discount is applied.")

    order = Order.objects.create(
        outlet=outlet,
        terminal=terminal,
        table=table,
        order_type=order_type,
        status=Order.Status.OPEN,
        discount_type=discount_type,
        discount_id_number=discount_id_number.strip(),
        created_offline=created_offline,
        client_created_at=client_created_at,
        business_date=business_date,
        opened_by=opened_by,
    )

    line_payloads: list[dict[str, Any]] = []
    order_lines: list[OrderLine] = []

    for entry in items:
        menu_item: MenuItem = entry["menu_item"]
        quantity = int(entry.get("quantity", 1))
        if quantity < 1:
            raise ValidationError("Item quantity must be at least 1.")

        unit_price = quantize_money(Decimal(str(menu_item.price)))
        line_total = quantize_money(unit_price * quantity)

        line_payloads.append(
            {
                "unit_price": unit_price,
                "quantity": quantity,
                "is_vatable": menu_item.is_vatable,
                "discount_eligible": menu_item.senior_pwd_eligible,
            }
        )
        order_lines.append(
            OrderLine(
                order=order,
                menu_item=menu_item,
                name_snapshot=menu_item.name,
                unit_price_snapshot=unit_price,
                quantity=quantity,
                line_total=line_total,
                is_vatable=menu_item.is_vatable,
                discount_eligible=menu_item.senior_pwd_eligible,
                status=OrderLine.Status.PENDING,
                notes=entry.get("notes", ""),
            )
        )

    OrderLine.objects.bulk_create(order_lines)

    totals = calculate_order_totals(lines=line_payloads, discount_type=discount_type)
    order.subtotal = totals["subtotal"]
    order.discount_total = totals["discount_total"]
    order.service_charge = totals["service_charge"]
    order.vat_amount = totals["vat_amount"]
    order.total = totals["total"]
    order.save(
        update_fields=[
            "subtotal",
            "discount_total",
            "service_charge",
            "vat_amount",
            "total",
        ]
    )

    return order



@transaction.atomic
def send_order_to_station(
    *,
    order: Order,
    station: str | None = None,
) -> Order:
    """Transition pending OrderLine rows to SENT and update Order status."""
    locked_order = Order.objects.select_for_update().get(pk=order.pk)

    if locked_order.status == Order.Status.VOID:
        raise ValidationError("Cannot send a voided order to kitchen or bar.")

    if locked_order.status == Order.Status.PAID:
        raise ValidationError("Cannot send an already paid order to kitchen or bar.")

    pending_qs = locked_order.lines.select_related("menu_item").filter(
        status=OrderLine.Status.PENDING
    )
    if station is not None:
        pending_qs = pending_qs.filter(menu_item__station=station)

    updated_count = pending_qs.update(status=OrderLine.Status.SENT)
    if updated_count == 0:
        raise ValidationError("No pending order lines available to send.")

    if locked_order.status == Order.Status.OPEN:
        locked_order.status = Order.Status.SENT
        locked_order.save(update_fields=["status"])

    return locked_order



@transaction.atomic
def settle_order(
    *,
    order: Order,
    payments: list[dict[str, Any]],
    invoice_number: str,
    paid_at: Any = None,
    created_by: Any = None,
) -> Order:
    """Record payments, mark order PAID, assign invoice_number, and record sale deductions."""
    locked_order = Order.objects.select_for_update().get(pk=order.pk)

    if locked_order.status == Order.Status.PAID:
        raise ValidationError("Order is already paid.")

    if locked_order.status == Order.Status.VOID:
        raise ValidationError("Cannot settle a voided order.")

    cleaned_invoice = invoice_number.strip()
    if not cleaned_invoice:
        raise ValidationError("Invoice number is required to settle an order.")

    if not payments:
        raise ValidationError("At least one payment is required to settle an order.")

    payment_rows: list[OrderPayment] = []
    paid_total = Decimal("0.00")

    for entry in payments:
        method = entry["method"]
        amount = quantize_money(Decimal(str(entry["amount"])))
        if amount <= Decimal("0.00"):
            raise ValidationError("Payment amount must be greater than zero.")

        folio_charge = entry.get("folio_charge")
        if method == OrderPayment.Method.ROOM_CHARGE and folio_charge is None:
            raise ValidationError("Room charge payments require a folio charge.")

        paid_total += amount
        payment_rows.append(
            OrderPayment(
                order=locked_order,
                method=method,
                amount=amount,
                payment=entry.get("payment"),
                folio_charge=folio_charge,
            )
        )

    paid_total = quantize_money(paid_total)
    if paid_total < locked_order.total:
        raise ValidationError("Total payment amount is less than the order total.")

    active_lines = list(
        locked_order.lines.exclude(status=OrderLine.Status.VOID).prefetch_related(
            "menu_item__ingredients__item"
        )
    )
    needed_by_item: dict[Any, Decimal] = defaultdict(lambda: Decimal("0.000"))

    for line in active_lines:
        line_qty = Decimal(str(line.quantity))
        for recipe in line.menu_item.ingredients.all():
            if recipe.item.track_stock:
                needed_by_item[recipe.item] += Decimal(str(recipe.quantity)) * line_qty

    if needed_by_item:
        StockMovement = apps.get_model("inventory", "StockMovement")
        movements = []
        for inv_item, qty_needed in needed_by_item.items():
            if qty_needed <= Decimal("0.000"):
                continue
            if inv_item.stock_on_hand < qty_needed:
                raise ValidationError(f"Insufficient stock for {inv_item.name}.")
            movements.append(
                StockMovement(
                    item=inv_item,
                    movement_type=StockMovement.MovementType.SALE_DEDUCTION,
                    quantity=-qty_needed,
                    unit_cost=inv_item.standard_cost,
                    reference_type="pos_order",
                    reference_id=str(locked_order.pk),
                    occurred_at=paid_at or timezone.now(),
                    created_by=created_by or locked_order.opened_by,
                    notes=f"Order {cleaned_invoice}",
                )
            )
        StockMovement.objects.bulk_create(movements)

    OrderPayment.objects.bulk_create(payment_rows)

    locked_order.status = Order.Status.PAID
    locked_order.invoice_number = cleaned_invoice
    locked_order.paid_at = paid_at or timezone.now()
    locked_order.save(update_fields=["status", "invoice_number", "paid_at"])

    return locked_order



@transaction.atomic
def void_order(
    *,
    order: Order,
    voided_by: Any,
    void_reason: str,
    voided_at: Any = None,
) -> Order:
    """Void an order and emit VOID_RESTORE stock movements if it was already settled."""
    locked_order = Order.objects.select_for_update().get(pk=order.pk)

    if locked_order.status == Order.Status.VOID:
        raise ValidationError("Order is already voided.")

    cleaned_reason = void_reason.strip()
    if not cleaned_reason:
        raise ValidationError("Void reason is required.")

    was_paid = locked_order.status == Order.Status.PAID

    locked_order.lines.exclude(status=OrderLine.Status.VOID).update(
        status=OrderLine.Status.VOID
    )

    if was_paid:
        StockMovement = apps.get_model("inventory", "StockMovement")
        deductions = list(
            StockMovement.objects.select_related("item").filter(
                reference_type="pos_order",
                reference_id=str(locked_order.pk),
                movement_type=StockMovement.MovementType.SALE_DEDUCTION,
            )
        )
        if deductions:
            restorations = [
                StockMovement(
                    item=movement.item,
                    movement_type=StockMovement.MovementType.VOID_RESTORE,
                    quantity=abs(movement.quantity),
                    unit_cost=movement.unit_cost,
                    reference_type="pos_order",
                    reference_id=str(locked_order.pk),
                    occurred_at=voided_at or timezone.now(),
                    created_by=voided_by,
                    notes=f"Void order {locked_order.invoice_number or locked_order.pk}: {cleaned_reason}",
                )
                for movement in deductions
            ]
            StockMovement.objects.bulk_create(restorations)

    locked_order.status = Order.Status.VOID
    locked_order.voided_by = voided_by
    locked_order.void_reason = cleaned_reason
    locked_order.save(update_fields=["status", "voided_by", "void_reason"])

    return locked_order



def _parse_business_date(raw_date: Any) -> date:
    if isinstance(raw_date, date):
        return raw_date
    return date.fromisoformat(str(raw_date))


@transaction.atomic
def sync_batch(
    *,
    batch_id: uuid.UUID | str,
    terminal: Terminal,
    orders_payload: list[dict[str, Any]],
    outlet: Outlet | None = None,
    uploaded_by: Any = None,
) -> SyncBatch:
    """Idempotently ingest offline orders using batch_id and client-generated order UUIDs."""
    parsed_batch_id = uuid.UUID(str(batch_id))
    effective_outlet = outlet or terminal.outlet
    actor = uploaded_by or getattr(terminal, "assigned_user", None) or Order.objects.filter(
        terminal=terminal
    ).values_list("opened_by", flat=True).first()

    batch, created = SyncBatch.objects.get_or_create(
        batch_id=parsed_batch_id,
        defaults={
            "terminal": terminal,
            "order_count": len(orders_payload),
        },
    )
    if not created:
        return batch

    created_count = 0
    duplicate_count = 0
    conflict_count = 0
    rejected_count = 0
    results: list[dict[str, Any]] = []

    for entry in orders_payload:
        raw_id = entry.get("id")
        if not raw_id:
            rejected_count += 1
            results.append({"id": None, "status": "rejected", "error": "Missing order id."})
            continue

        order_uuid = uuid.UUID(str(raw_id))

        try:
            biz_date = _parse_business_date(entry["business_date"])
            order_type = entry.get("order_type", Order.Type.DINE_IN)
            discount_type = entry.get("discount_type", Order.DiscountType.NONE)
            discount_id_number = entry.get("discount_id_number", "").strip()
            client_created_at = entry.get("client_created_at")

            table_obj: DiningTable | None = None
            if entry.get("table_id") is not None:
                table_obj = DiningTable.objects.filter(pk=entry["table_id"]).first()

            normalized_items: list[dict[str, Any]] = []
            line_payloads: list[dict[str, Any]] = []
            for item_entry in entry.get("items", []):
                menu_item = MenuItem.objects.get(pk=item_entry["menu_item_id"])
                qty = int(item_entry.get("quantity", 1))
                if qty < 1:
                    raise ValidationError("Item quantity must be at least 1.")
                unit_price = quantize_money(Decimal(str(menu_item.price)))
                normalized_items.append(
                    {
                        "menu_item": menu_item,
                        "quantity": qty,
                        "notes": item_entry.get("notes", ""),
                    }
                )
                line_payloads.append(
                    {
                        "unit_price": unit_price,
                        "quantity": qty,
                        "is_vatable": menu_item.is_vatable,
                        "discount_eligible": menu_item.senior_pwd_eligible,
                    }
                )

            if not normalized_items:
                raise ValidationError("Order must contain at least one item.")

            if discount_type != Order.DiscountType.NONE and not discount_id_number:
                raise ValidationError("Discount ID number is required when a discount is applied.")

            computed_totals = calculate_order_totals(
                lines=line_payloads,
                discount_type=discount_type,
            )

            existing = Order.objects.select_for_update().filter(pk=order_uuid).first()
            if existing is not None:
                same_business_date = existing.business_date == biz_date
                same_total = quantize_money(existing.total) == computed_totals["total"]
                same_discount = existing.discount_type == discount_type
                if same_business_date and same_total and same_discount:
                    duplicate_count += 1
                    results.append({"id": str(order_uuid), "status": "duplicate"})
                else:
                    conflict_count += 1
                    results.append(
                        {
                            "id": str(order_uuid),
                            "status": "conflict",
                            "error": "Existing order differs from uploaded payload.",
                        }
                    )
                continue

            if actor is None:
                raise ValidationError("No user available to record as opened_by for offline order.")

            order = Order(
                pk=order_uuid,
                outlet=effective_outlet,
                terminal=terminal,
                table=table_obj,
                order_type=order_type,
                status=Order.Status.OPEN,
                discount_type=discount_type,
                discount_id_number=discount_id_number,
                subtotal=computed_totals["subtotal"],
                discount_total=computed_totals["discount_total"],
                service_charge=computed_totals["service_charge"],
                vat_amount=computed_totals["vat_amount"],
                total=computed_totals["total"],
                created_offline=True,
                client_created_at=client_created_at,
                business_date=biz_date,
                opened_by=actor,
            )
            order.save()

            OrderLine.objects.bulk_create(
                [
                    OrderLine(
                        order=order,
                        menu_item=item["menu_item"],
                        name_snapshot=item["menu_item"].name,
                        unit_price_snapshot=quantize_money(Decimal(str(item["menu_item"].price))),
                        quantity=item["quantity"],
                        line_total=quantize_money(
                            quantize_money(Decimal(str(item["menu_item"].price))) * item["quantity"]
                        ),
                        is_vatable=item["menu_item"].is_vatable,
                        discount_eligible=item["menu_item"].senior_pwd_eligible,
                        status=OrderLine.Status.PENDING,
                        notes=item["notes"],
                    )
                    for item in normalized_items
                ]
            )

            if entry.get("payments") and entry.get("invoice_number"):
                settle_order(
                    order=order,
                    payments=entry["payments"],
                    invoice_number=entry["invoice_number"],
                    created_by=actor,
                )

            created_count += 1
            results.append({"id": str(order_uuid), "status": "created"})

        except (ValidationError, MenuItem.DoesNotExist, KeyError, ValueError) as exc:
            rejected_count += 1
            results.append({"id": str(order_uuid), "status": "rejected", "error": str(exc)})

    batch.order_count = len(orders_payload)
    batch.created_count = created_count
    batch.duplicate_count = duplicate_count
    batch.conflict_count = conflict_count
    batch.rejected_count = rejected_count
    batch.results = results
    batch.save(
        update_fields=[
            "order_count",
            "created_count",
            "duplicate_count",
            "conflict_count",
            "rejected_count",
            "results",
        ]
    )

    return batch




@transaction.atomic
def generate_z_reading(
    *,
    terminal: Terminal,
    business_date: date,
    generated_by: Any = None,
) -> ZReading:
    """Aggregate settled orders and payments for a terminal and business date into a ZReading."""
    locked_terminal = Terminal.objects.select_for_update().get(pk=terminal.pk)

    if ZReading.objects.filter(terminal=locked_terminal, business_date=business_date).exists():
        raise ValidationError(
            f"A Z-reading already exists for terminal {locked_terminal} on {business_date}."
        )

    actor = generated_by or Order.objects.filter(terminal=locked_terminal).values_list(
        "opened_by", flat=True
    ).first()
    if actor is None:
        raise ValidationError("A user is required as generated_by to create a Z-reading.")

    paid_orders = list(
        Order.objects.filter(
            terminal=locked_terminal,
            business_date=business_date,
            status=Order.Status.PAID,
        )
        .prefetch_related("payments", "lines")
        .order_by("paid_at", "created_at", "pk")
    )

    gross_sales = Decimal("0.00")
    vatable_sales = Decimal("0.00")
    vat_exempt_sales = Decimal("0.00")
    vat_amount = Decimal("0.00")
    discounts = Decimal("0.00")
    service_charge = Decimal("0.00")
    totals_by_method: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    invoice_numbers: list[str] = []

    for order in paid_orders:
        order_gross = quantize_money(Decimal(str(order.subtotal)))
        order_vat = quantize_money(Decimal(str(order.vat_amount)))
        order_discount = quantize_money(Decimal(str(order.discount_total)))
        order_sc = quantize_money(Decimal(str(order.service_charge)))

        gross_sales += order_gross
        vat_amount += order_vat
        discounts += order_discount
        service_charge += order_sc

        active_lines = [
            {
                "unit_price": line.unit_price_snapshot,
                "quantity": line.quantity,
                "is_vatable": line.is_vatable,
                "discount_eligible": line.discount_eligible,
            }
            for line in order.lines.all()
            if line.status != OrderLine.Status.VOID
        ]
        if active_lines:
            line_totals = calculate_order_totals(
                lines=active_lines,
                discount_type=order.discount_type,
                vat_rate=Decimal("0.12") if order_vat > Decimal("0.00") else Decimal("0.00"),
            )
            vatable_sales += line_totals["vatable_sales"]
            vat_exempt_sales += line_totals["vat_exempt_sales"]
        else:
            vat_exempt_sales += quantize_money(order_gross - order_vat - order_discount)

        if order.invoice_number:
            invoice_numbers.append(order.invoice_number)

        for payment in order.payments.all():
            totals_by_method[payment.method] += quantize_money(Decimal(str(payment.amount)))

    return ZReading.objects.create(
        terminal=locked_terminal,
        business_date=business_date,
        gross_sales=quantize_money(gross_sales),
        vatable_sales=quantize_money(vatable_sales),
        vat_exempt_sales=quantize_money(vat_exempt_sales),
        vat_amount=quantize_money(vat_amount),
        discounts=quantize_money(discounts),
        service_charge=quantize_money(service_charge),
        totals_by_method={
            method: str(quantize_money(amount))
            for method, amount in totals_by_method.items()
        },
        first_invoice_no=invoice_numbers[0] if invoice_numbers else "",
        last_invoice_no=invoice_numbers[-1] if invoice_numbers else "",
        generated_by=actor,
    )