


from rest_framework import serializers

from apps.pos.models import Order, OrderLine, OrderPayment, ZReading


class OrderLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderLine
        fields = (
            "id",
            "menu_item",
            "name_snapshot",
            "unit_price_snapshot",
            "quantity",
            "line_total",
            "status",
            "notes",
        )
        read_only_fields = fields

class OrderSerializer(serializers.ModelSerializer):
    lines = OrderLineSerializer(many=True, read_only=True)
    class Meta:
        model = Order
        fields = (
            "id",
            "outlet",
            "terminal",
            "table",
            "order_type",
            "status",
            "invoice_number",
            "discount_type",
            "discount_id_number",
            "subtotal",
            "discount_total",
            "service_charge",
            "vat_amount",
            "total",
            "business_date",
            "paid_at",
            "void_reason",
            "lines",
        )
        read_only_fields = (
            "id",
            "status",
            "invoice_number",
            "subtotal",
            "discount_total",
            "service_charge",
            "vat_amount",
            "total",
            "paid_at",
            "void_reason",
            "lines",
        )

class CreateOrderItemInputSerializer(serializers.Serializer):
    menu_item = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CreateOrderInputSerializer(serializers.Serializer):
    outlet = serializers.IntegerField()
    terminal = serializers.IntegerField()
    table = serializers.IntegerField(required=False, allow_null=True)
    order_type = serializers.ChoiceField(choices=Order.Type.choices)
    business_date = serializers.DateField()
    discount_type = serializers.ChoiceField(
        choices=Order.DiscountType.choices, required=False, default=Order.DiscountType.NONE
    )
    discount_id_number = serializers.CharField(required=False, allow_blank=True, default="")
    items = CreateOrderItemInputSerializer(many=True)

class PaymentInputSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=OrderPayment.Method.choices)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)

class SettleOrderInputSerializer(serializers.Serializer):
    invoice_number = serializers.CharField()
    payments = PaymentInputSerializer(many=True)

class VoidOrderInputSerializer(serializers.Serializer):
    void_reason = serializers.CharField(allow_blank=True)

class SyncBatchInputSerializer(serializers.Serializer):
    batch_id = serializers.UUIDField()
    terminal = serializers.IntegerField()
    orders = serializers.ListField(child=serializers.DictField())

class GenerateZReadingInputSerializer(serializers.Serializer):
    terminal = serializers.IntegerField()
    business_date = serializers.DateField()

class ZReadingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ZReading
        fields =  (
            "id",
            "terminal",
            "business_date",
            "gross_sales",
            "vatable_sales",
            "vat_exempt_sales",
            "vat_amount",
            "discounts",
            "service_charge",
            "totals_by_method",
            "first_invoice_no",
            "last_invoice_no",
            "generated_by",
        )
        read_only_fields = fields

