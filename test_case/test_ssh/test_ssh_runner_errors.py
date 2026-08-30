# -*- coding: utf-8 -*-
"""A failed SSH command must say which kind of failure it was.

Collapsing authentication, timeout and protocol errors into one opaque string
makes an operator debug the wrong thing: a live timeout and a wrong password
were indistinguishable from the caller's side.
"""

import socket

import pytest

from agent import ssh_runner

pytestmark = pytest.mark.unit


class _Channel:
    def recv_exit_status(self):
        return 0


class _Stream:
    def __init__(self, payload=b"", error=None):
        self._payload = payload
        self._error = error
        self.channel = _Channel()

    def read(self):
        if self._error is not None:
            raise self._error
        return self._payload


class _Client:
    def __init__(self, stdout=None, stderr=None):
        self._stdout = stdout if stdout is not None else _Stream(b"ok")
        self._stderr = stderr if stderr is not None else _Stream(b"")
        self.closed = False

    def exec_command(self, command, timeout=None):
        return None, self._stdout, self._stderr

    def close(self):
        self.closed = True


@pytest.fixture
def connect(monkeypatch):
    holder = {}

    def _install(client=None, error=None):
        def _connect(ssh_config, timeout=30):
            if error is not None:
                raise error
            holder["client"] = client or _Client()
            return holder["client"]

        monkeypatch.setattr(ssh_runner, "_paramiko_connect", _connect)
        return holder

    return _install


def test_a_successful_command_returns_its_output(connect):
    holder = connect(_Client(stdout=_Stream(b"hello"), stderr=_Stream(b"")))

    rc, out = ssh_runner._paramiko_run_ssh({}, "echo hello")

    assert (rc, out) == (0, "hello")
    assert holder["client"].closed is True


def test_a_timeout_says_it_timed_out_and_for_how_long(connect):
    connect(_Client(stdout=_Stream(error=socket.timeout())))

    rc, out = ssh_runner._paramiko_run_ssh({}, "sleep 60", timeout=6)

    assert rc == -1
    assert "no output for 6s" in out


def test_another_execution_failure_names_its_type(connect):
    connect(_Client(stdout=_Stream(error=EOFError("channel closed"))))

    rc, out = ssh_runner._paramiko_run_ssh({}, "whoami")

    assert rc == -1
    assert "EOFError" in out
    # The message may carry host or user detail; the type does not.
    assert "channel closed" not in out


def test_an_authentication_failure_propagates_rather_than_becoming_output(connect):
    """Auth failure is the caller's to handle, not a command that returned -1."""
    connect(error=ssh_runner.SSHAuthError("no usable credential"))

    with pytest.raises(ssh_runner.SSHAuthError):
        ssh_runner._paramiko_run_ssh({}, "whoami")


def test_the_client_is_closed_even_when_the_command_fails(connect):
    holder = connect(_Client(stdout=_Stream(error=socket.timeout())))

    ssh_runner._paramiko_run_ssh({}, "sleep 60")

    assert holder["client"].closed is True
