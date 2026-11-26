"""apps/core/models.py"""

from decimal import Decimal
from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone

from apps.users.fields import rate


# ---------------------------------------------------------------- base classes
class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteManager(models.Manager):
    """Default manager hides soft-deleted rows. Use `all_objects` to see them."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteModel(TimeStampedModel):
    """Financial and booking records are never hard-deleted."""

    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta(TimeStampedModel.Meta):
        abstract = True

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at", "updated_at"])
        label: str = self._meta.label or self.__class__.__name__
        return (1, {label: 1})


# ------------------------------------------------------------------------ user
class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    MANAGER = "manager", "Manager"
    FRONT_DESK = "front_desk", "Front desk"
    CASHIER = "cashier", "Cashier"
    STOREKEEPER = "storekeeper", "Storekeeper"
    ACCOUNTANT = "accountant", "Accountant"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.ADMIN)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Set AUTH_USER_MODEL = "core.User" BEFORE the first migration."""

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.FRONT_DESK)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.role})"


# ------------------------------------------------------------------- audit log
class AuditLog(models.Model):
    """Append-only. Written by a helper in services.py, never edited."""

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        VOID = "void", "Void"
        REFUND = "refund", "Refund"
        LOGIN = "login", "Login"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )  # null = system action (Celery task, night audit)
    action = models.CharField(max_length=20, choices=Action.choices)
    model_label = models.CharField(max_length=100)  # e.g. "property.FolioCharge"
    object_id = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (models.Index(fields=["model_label", "object_id"]),)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Audit log entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Audit log entries cannot be deleted.")

    def __str__(self):
        return f"{self.action} {self.model_label}#{self.object_id}"


# --------------------------------------------------------------- tax settings
class TaxSetting(TimeStampedModel):
    """Effective-dated so old invoices keep the rates that applied to them."""

    vat_rate = rate(default=Decimal("0.1200"))
    service_charge_rate = rate(default=Decimal("0.1000"))
    senior_pwd_discount_rate = rate(default=Decimal("0.2000"))
    vat_inclusive = models.BooleanField(default=True)
    effective_from = models.DateField()

    class Meta:
        ordering = ("-effective_from",)
        constraints = (
            models.UniqueConstraint(fields=["effective_from"], name="uniq_tax_effective_from"),
        )


# ------------------------------------------------------------ invoice numbers
class InvoiceSequence(TimeStampedModel):
    """Gap-free numbering per series, for example ROOM or POS-T01."""

    series = models.CharField(max_length=30, unique=True)
    prefix = models.CharField(max_length=20, blank=True)
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=8)

    def __str__(self):
        return self.series