from django.urls import include, path

from apps.users.views import RegisterAPIView

app_name = "users"

urlpatterns = [
    path("register/", RegisterAPIView.as_view(), name="register"),
]