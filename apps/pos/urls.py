# apps/pos/urls.py
from django.urls import path

from apps.pos.views import (
    GenerateZReadingView,
    OrderListCreateView,
    OrderSettleView,
    OrderVoidView,
    SyncBatchView,
)

app_name = "pos"

urlpatterns = [
    path("orders/", OrderListCreateView.as_view(), name="order-list"),
    path("orders/<uuid:pk>/settle/", OrderSettleView.as_view(), name="order-settle"),
    path("orders/<uuid:pk>/void/", OrderVoidView.as_view(), name="order-void"),
    path("sync/", SyncBatchView.as_view(), name="sync-batch"),
    path("z-readings/generate/", GenerateZReadingView.as_view(), name="z-reading-generate"),
]