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
    Terminal,
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