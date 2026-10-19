"""Versioned product descriptions and adapters."""

from .models import ProductAdapter, ProductProfile, WindowRole
from .registry import (backend_for_product, get_product_adapter, get_product_profile,
                       register_product_adapter)

__all__ = ["ProductAdapter", "ProductProfile", "WindowRole", "get_product_adapter",
           "get_product_profile", "register_product_adapter", "backend_for_product"]
