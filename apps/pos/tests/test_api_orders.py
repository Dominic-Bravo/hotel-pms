import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.pos.models import Order, OrderPayment, ZReading


@pytest.mark.django_db
def test_create_order_endpoint_returns_201_and_persists_order(pos_context):
    anon_client = APIClient()
    url = reverse("pos:order-list")
    payload = {
        "outlet": pos_context["outlet"].pk,
        "terminal": pos_context["terminal"].pk,
        "table": pos_context["table"].pk,
        "order_type": Order.Type.DINE_IN,
        "business_date": "2026-11-27",
        "items": [
            {"menu_item": pos_context["steak"].pk, "quantity": 1, "notes": "Medium rare"}
        ],
    }

    unauth_res = anon_client.post(url, payload, format="json")
    assert unauth_res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

    auth_client = APIClient()
    auth_client.force_authenticate(user=pos_context["staff"])
    res = auth_client.post(url, payload, format="json")

    assert res.status_code == status.HTTP_201_CREATED
    assert res.data["status"] == Order.Status.OPEN
    assert Decimal(str(res.data["total"])) > Decimal("0.00")
    assert Order.objects.filter(pk=res.data["id"]).exists()


@pytest.mark.django_db
def test_settle_order_endpoint_maps_underpayment_validation_error_to_400(api_auth_client, pos_context):
    create_url = reverse("pos:order-list")
    created = api_auth_client.post(
        create_url,
        {
            "outlet": pos_context["outlet"].pk,
            "terminal": pos_context["terminal"].pk,
            "order_type": Order.Type.DINE_IN,
            "business_date": "2026-11-27",
            "items": [{"menu_item": pos_context["steak"].pk, "quantity": 1}],
        },
        format="json",
    )
    assert created.status_code == status.HTTP_201_CREATED

    settle_url = reverse("pos:order-settle", kwargs={"pk": created.data["id"]})
    bad_res = api_auth_client.post(
        settle_url,
        {
            "invoice_number": "INV-2026-0201",
            "payments": [{"method": OrderPayment.Method.CASH, "amount": "1.00"}],
        },
        format="json",
    )

    assert bad_res.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_void_order_endpoint_requires_reason_and_voids_order(api_auth_client, pos_context):
    created = api_auth_client.post(
        reverse("pos:order-list"),
        {
            "outlet": pos_context["outlet"].pk,
            "terminal": pos_context["terminal"].pk,
            "order_type": Order.Type.DINE_IN,
            "business_date": "2026-11-27",
            "items": [{"menu_item": pos_context["soda"].pk, "quantity": 1}],
        },
        format="json",
    )
    order_id = created.data["id"]
    void_url = reverse("pos:order-void", kwargs={"pk": order_id})

    missing_reason = api_auth_client.post(void_url, {"void_reason": ""}, format="json")
    assert missing_reason.status_code == status.HTTP_400_BAD_REQUEST

    ok_res = api_auth_client.post(void_url, {"void_reason": "Guest changed mind"}, format="json")
    assert ok_res.status_code == status.HTTP_200_OK
    assert ok_res.data["status"] == Order.Status.VOID


@pytest.mark.django_db
def test_sync_batch_and_z_reading_endpoints(api_auth_client, pos_context):
    batch_id = str(uuid.uuid4())
    order_id = str(uuid.uuid4())
    sync_url = reverse("pos:sync-batch")

    sync_res = api_auth_client.post(
        sync_url,
        {
            "batch_id": batch_id,
            "terminal": pos_context["terminal"].pk,
            "orders": [
                {
                    "id": order_id,
                    "business_date": "2026-11-27",
                    "order_type": Order.Type.DINE_IN,
                    "table_id": pos_context["table"].pk,
                    "items": [{"menu_item_id": pos_context["soda"].pk, "quantity": 1}],
                }
            ],
        },
        format="json",
    )
    assert sync_res.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED)
    assert sync_res.data["created_count"] == 1

    z_url = reverse("pos:z-reading-generate")
    z_res = api_auth_client.post(
        z_url,
        {
            "terminal": pos_context["terminal"].pk,
            "business_date": date(2026, 11, 27).isoformat(),
        },
        format="json",
    )
    assert z_res.status_code == status.HTTP_200_OK  
    assert ZReading.objects.filter(
        terminal=pos_context["terminal"], business_date=date(2026, 11, 27)
    ).exists()