"""An offline target must return an operator-readable status, not a traceback."""

from agent.target_manager import _catch


def test_probe_timeout_is_structured():
    def timeout():
        raise TimeoutError("connection details must not leak")

    result = _catch("win-target", "probe", timeout)
    assert result == {
        "ok": False, "target": "win-target", "stage": "probe",
        "error": "SSH connection timed out",
    }
