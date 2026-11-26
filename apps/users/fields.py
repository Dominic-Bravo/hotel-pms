"""apps/core/fields.py: shared field helpers so money and rates are defined once."""
from django.db import models


def money(**kwargs):
    """PHP amounts. Decimal only, never floats. Max 9,999,999,999.99."""
    return models.DecimalField(max_digits=12, decimal_places=2, **kwargs)


def rate(**kwargs):
    """Percentages stored as fractions, for example 0.1200 for 12 percent."""
    return models.DecimalField(max_digits=5, decimal_places=4, **kwargs)


def qty_field(**kwargs):
    """Stock quantities, three decimals (for example 0.250 kg)."""
    return models.DecimalField(max_digits=12, decimal_places=3, **kwargs)