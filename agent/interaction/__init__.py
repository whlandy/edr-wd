"""Live and business-intent entry points over the atomic executor."""

from .runtime import execute_live_action, execute_intent

__all__ = ["execute_live_action", "execute_intent"]
