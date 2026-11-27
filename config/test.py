from typing import cast

import pytest
from django.urls import path
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from apps.users.models import User
from config.permissions import IsAdminOrManager  # adjust import path if needed


# 1. Define a mock view inside the test file
class MockProtectedView(APIView):
    permission_classes = [IsAdminOrManager]

    def get(self, request: Request) -> Response:
        return Response({"status": "access granted"})


# 2. Expose urlpatterns in this module so @pytest.mark.urls(__name__) can load it
urlpatterns = [
    path("mock-protected/", MockProtectedView.as_view(), name="mock-protected"),
]


# 3. Run APIClient tests against the mock endpoint
@pytest.mark.django_db
@pytest.mark.urls(__name__)
@pytest.mark.parametrize(
    "role, expected_status",
    [
        ("admin", 200),
        ("manager", 200),
        ("front_desk", 403),
        ("cashier", 403),
        ("storekeeper", 403),
        ("accountant", 403),
    ],
)
def test_mock_endpoint_permissions(role: str, expected_status: int):
    client = APIClient()

    user = User.objects.create_user(
        email=f"{role}@gmail.com",
        full_name=f"Test {role.title()}",
        password="StrongPassword123!",
        role=role,
        is_active=True,
    )

    client.force_authenticate(user=user)

    response = cast(
        Response,
        client.get("/mock-protected/"),
    )

    assert response.status_code == expected_status


@pytest.mark.django_db
@pytest.mark.urls(__name__)
def test_mock_endpoint_unauthenticated():
    client = APIClient()

    response = cast(
        Response,
        client.get("/mock-protected/"),
    )

    assert response.status_code in (401, 403)
