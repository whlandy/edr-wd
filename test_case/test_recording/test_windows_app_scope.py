"""Cross-process window events remain inside the exact installed app root."""

import sys
from types import SimpleNamespace

from target.recording.models import CaptureScope
from target.recording.windows import (
    WindowsUIACorrelator, WindowsUIAResolver, _executable_in_root,
)


def test_sibling_edr_processes_share_the_recording_scope():
    root = r"C:\Program Files\HiSec-Endpoint"
    assert _executable_in_root(r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe", root)
    assert _executable_in_root(r"C:\Program Files\HiSec-Endpoint\core\safra\HisecEndpointAgent.exe", root)


def test_similar_prefix_from_another_app_is_not_admitted():
    root = r"C:\Program Files\HiSec-Endpoint"
    assert not _executable_in_root(r"C:\Program Files\HiSec-Endpoint-Evil\other.exe", root)


def test_recording_scope_checks_the_observed_pid_not_just_a_shared_process_name():
    root = r"C:\Program Files\HiSec-Endpoint"

    class Resolver:
        def process_pid_belongs_to_root(self, pid, expected_root):
            assert expected_root == root
            return pid in {10, 11}

    correlator = WindowsUIACorrelator(Resolver())
    correlator._scope_root = root
    scope = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^Main$")

    assert correlator._process_in_scope("EDRClient.exe", scope, 10)
    assert correlator._process_in_scope("HisecEndpointAgent.exe", scope, 11)
    assert not correlator._process_in_scope("EDRClient.exe", scope, 99)
    assert not correlator._in_scope({"processName": "EDRClient.exe", "pid": 99,
                                     "windowTitle": "Main"}, scope)


def test_application_root_comes_from_the_locked_owner_when_names_collide(monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    paths = {
        10: r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe",
        99: r"C:\Other\EDRClient.exe",
    }

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "EDRClient.exe"

        def exe(self):
            return paths[self.pid]

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=Process,
        process_iter=lambda _fields: (SimpleNamespace(info={"name": "EDRClient.exe", "exe": path})
                                      for path in paths.values()),
    ))

    class Backend:
        def get_window_lock(self):
            return {"lock": {"pid": 10}}

    assert WindowsUIAResolver(Backend()).application_root("EDRClient.exe") == \
        r"C:\Program Files\HiSec-Endpoint"
    assert WindowsUIAResolver().application_root("EDRClient.exe") is None
    assert WindowsUIAResolver.process_pid_belongs_to_root(
        10, r"C:\Program Files\HiSec-Endpoint")
    assert not WindowsUIAResolver.process_pid_belongs_to_root(
        99, r"C:\Program Files\HiSec-Endpoint")
