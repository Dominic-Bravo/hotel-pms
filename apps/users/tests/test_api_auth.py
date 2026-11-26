from typing import cast

import pytest
from rest_framework.response import Response
from rest_framework.test import APIClient

from apps.users.models import User


@pytest.mark.django_db
def test_signup_creates_user_with_default_role():
    client = APIClient()
    payload = {
        "email": "staff001@gmail.com",
        "full_name": "Dom Bravo",
        "password": "StrongPassword123!",
        "password_confirm": "StrongPassword123!",
        "role": "admin",
        "is_active": True,
        "is_staff": True,
    }

    response = cast(
        Response,
        client.post("/api/v1/users/register/", payload, format="json"),
    )

    assert response.status_code == 201
    user = User.objects.get(email="staff001@gmail.com")
    assert user.full_name == "Dom Bravo"
    assert user.role != "admin" 
    assert user.check_password("StrongPassword123!")

@pytest.mark.django_db
def test_login_user_with_http_only_cokkies():
    User.objects.create_user(
        email="desk@gmail.com", 
        full_name="Sam Cruz",
        password="StrongPassword123!",
    )

    payload = {
        "email" : "desk@gmail.com",
        "password" : "StrongPassword123!",
    }

    client = APIClient()

    response = cast(
        Response,
        client.post("/api/v1/users/login/", payload, format="json"),
    )

    # Access token is returned in the JSON response body
    assert response.data is not None
    assert "access" in response.data  # (or "access_token" depending on your JSON key)

    # Only refresh_token is stored in an HttpOnly cookie
    assert "refresh_token" in response.cookies
    assert response.cookies["refresh_token"]["httponly"] is True
    assert "refresh_token" not in response.data
