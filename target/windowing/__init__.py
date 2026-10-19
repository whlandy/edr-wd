"""Product-neutral runtime window identity and safety primitives."""

from .models import WindowIdentity, WindowIdentityError
from .registry import WindowRegistry

__all__ = ["WindowIdentity", "WindowIdentityError", "WindowRegistry"]
