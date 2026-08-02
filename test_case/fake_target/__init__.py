"""Test-only fixtures for fake targets + fake image providers.

P1.2 ships FakeBackend + FakeObservationProvider (executor
harness); P1.4 adds fake image generation under `fake_image` for
evidence / screenshot tests.
"""

from __future__ import annotations

from .fake_backend import FakeBackend, FakeObservationProvider, _failed_receipt, _ok_receipt
from .fake_image import make_png

__all__ = [
    "FakeBackend",
    "FakeObservationProvider",
    "_failed_receipt",
    "_ok_receipt",
    "make_png",
]
