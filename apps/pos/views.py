# apps/pos/views.py
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.pos.models import DiningTable, MenuItem, Order, Outlet, Terminal
from apps.pos.serializers import (
    CreateOrderInputSerializer,
    GenerateZReadingInputSerializer,
    OrderSerializer,
    SettleOrderInputSerializer,
    SyncBatchInputSerializer,
    VoidOrderInputSerializer,
    ZReadingSerializer,
)
from apps.pos.services import (
    create_order,
    generate_z_reading,
    settle_order,
    sync_batch,
    void_order,
)


class OrderListCreateView(APIView): 
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = CreateOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        outlet = get_object_or_404(Outlet, pk=data["outlet"])
        terminal = get_object_or_404(Terminal, pk=data["terminal"])

        table = (
            get_object_or_404(DiningTable, pk=data["table"])
            if data.get("table") is not None
            else None
        )

        items = []

        for item in data["items"]:
            menu_item = get_object_or_404(MenuItem, pk=item["menu_item"])
            items.append(
                {
                    "menu_item": menu_item,
                    "quantity": item["quantity"],
                    "notes": item.get("notes", ""),
                }
            )

        try: 
            order = create_order(
                outlet=outlet,
                terminal=terminal,
                table=table,
                opened_by=request.user,
                business_date=data["business_date"],
                order_type=data["order_type"],
                discount_type=data["discount_type"],
                discount_id_number=data["discount_id_number"],
                items=items,
            )
        except DjangoValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED )


class OrderSettleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        serializer = SettleOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try: 
            settled = settle_order(
                order=order,
                payments=serializer.validated_data["payments"],
                invoice_number=serializer.validated_data["invoice_number"],
                created_by=request.user,
            )
        except DjangoValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(settled).data, status=status.HTTP_200_OK)

class OrderVoidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        serializer = VoidOrderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try: 
            voided = void_order(
                order=order,
                voided_by=request.user,
                void_reason=serializer.validated_data["void_reason"]
            )
        except DjangoValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(voided).data, status=status.HTTP_200_OK)

class SyncBatchView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer= SyncBatchInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        terminal = get_object_or_404(Terminal, pk=serializer.validated_data["terminal"])

        try:
            batch = sync_batch(
                batch_id=serializer.validated_data["batch_id"],
                terminal=terminal,
                uploaded_by=request.user,
                orders_payload=serializer.validated_data["orders"],
            )
        except DjangoValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "batch_id": str(batch.batch_id),
                "order_count": batch.order_count,
                "created_count": batch.created_count,
                "duplicate_count": batch.duplicate_count,
                "conflict_count": batch.conflict_count,
                "rejected_count": batch.rejected_count,
                "results": batch.results,
            },
            status=status.HTTP_201_CREATED,
        )

class GenerateZReadingView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = GenerateZReadingInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        terminal = get_object_or_404(Terminal, pk=serializer.validated_data["terminal"])

        try: 
            z_reading = generate_z_reading(
                terminal=terminal,
                business_date=serializer.validated_data["business_date"],
                generated_by=request.user
            )

        except DjangoValidationError as exc:
            return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ZReadingSerializer(z_reading).data, status=status.HTTP_200_OK)
        