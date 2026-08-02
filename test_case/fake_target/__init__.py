"""Stub MCP-style fake_target for P1.2 integration tests.

This package is the test-only harness used by the integration tests in
`test_case/test_executor_integration/`.  It is not imported by any
production code."""

from .fake_backend import FakeBackend, FakeObservationProvider, _failed_receipt, _ok_receipt

__all__ = ["FakeBackend", "FakeObservationProvider", "_ok_receipt", "_failed_receipt"]