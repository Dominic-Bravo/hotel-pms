# ADMIN = "admin", "Admin"
# MANAGER = "manager", "Manager"
# FRONT_DESK = "front_desk", "Front desk"
# CASHIER = "cashier", "Cashier"
# STOREKEEPER = "storekeeper", "Storekeeper"
# ACCOUNTANT = "accountant", "Accountant"

# your_app/permissions.py
from rest_framework import permissions


class HasRole(permissions.BasePermission):
    """
    Base permission class that checks if the authenticated user has one of the allowed roles.
    """
    allowed_roles = []

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) in self.allowed_roles
        )


# --- Individual Role Permissions ---

class IsAdmin(HasRole):
    allowed_roles = ["admin"]

class IsManager(HasRole):
    allowed_roles = ["manager"]

class IsFrontDesk(HasRole):
    allowed_roles = ["front_desk"]

class IsCashier(HasRole):
    allowed_roles = ["cashier"]

class IsStorekeeper(HasRole):
    allowed_roles = ["storekeeper"]

class IsAccountant(HasRole):
    allowed_roles = ["accountant"]



class IsAdminOrManager(HasRole):
    allowed_roles = ["admin", "manager"]

class IsFinanceStaff(HasRole):
    allowed_roles = ["admin", "accountant", "cashier"]
