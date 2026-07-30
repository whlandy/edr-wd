"""Phase 3: macOS deploy must use scp_dir_to(tracked_only=True), not
the generic scp_to() directory upload that existed before.

Background:
  macOS deploy used `scp_to(LOCAL_TARGET, macos_root)` which uploads
  the entire target/ tree — including untracked caches, logs, .pyc,
  local config, etc. Windows already uses scp_dir_to(tracked_only=True).
  Phase 3 brings macOS in line (design doc step 5).

These tests verify:
  - macos.deploy() calls scp_dir_to (not scp_to for directory upload)
  - the tracked_only=True argument is forwarded
  - non-tracked files (logs/, .tmp, untracked .py) are NOT uploaded
  - failure in scp_dir_to is propagated as deploy_failed
"""

from __future__ import annotations


def test_macos_deploy_uses_scp_dir_to_with_tracked_only(monkeypatch, tmp_path):
    """macOS deploy must call scp_dir_to(..., tracked_only=True)."""
    from agent import lifecycle
    from agent.lifecycle.macos import MacOSLifecycle

    captured = []

    def fake_scp_dir_to(ssh_cfg, local_dir, remote_dir, *, timeout=60, tracked_only=True):
        captured.append({
            "ssh_cfg": ssh_cfg,
            "local_dir": local_dir,
            "remote_dir": remote_dir,
            "timeout": timeout,
            "tracked_only": tracked_only,
        })
        return (0, "ok")

    # run_ssh verifies the uploaded files exist on the target; mock to
    # skip real SSH (we are testing the deploy wrapper, not the verify).
    monkeypatch.setattr(
        lifecycle.macos, "run_ssh",
        lambda *_a, **_k: (0, "found"),
    )

    monkeypatch.setattr(lifecycle.macos, "scp_dir_to", fake_scp_dir_to)
    monkeypatch.setattr(lifecycle.macos, "scp_to", lambda *a, **k: captured.append(("scp_to_called", a, k)) or (0, ""))

    lifecycle_obj = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    result = lifecycle_obj.deploy(cfg)

    assert result["ok"] is True
    assert len(captured) == 1
    call = captured[0]
    assert isinstance(call, dict)  # not the scp_to_called marker
    assert call["tracked_only"] is True, (
        f"tracked_only must be True to prevent untracked file upload. "
        f"Got: {call}"
    )
    assert str(call["remote_dir"]) == "/Users/admin/edr-wd/target"


def test_macos_deploy_does_not_call_scp_to_for_directory(monkeypatch):
    """The legacy `scp_to(LOCAL_TARGET, macos_root)` directory upload
    must not be used — that path leaks untracked files."""
    from agent import lifecycle
    from agent.lifecycle.macos import MacOSLifecycle

    scp_to_calls = []

    def fake_scp_to(*args, **kwargs):
        scp_to_calls.append((args, kwargs))
        return (0, "")

    monkeypatch.setattr(lifecycle.macos, "scp_to", fake_scp_to)
    monkeypatch.setattr(
        lifecycle.macos, "scp_dir_to",
        lambda *a, **k: (0, "ok"),
    )
    monkeypatch.setattr(
        lifecycle.macos, "run_ssh",
        lambda *_a, **_k: (0, "found"),
    )

    lifecycle_obj = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    result = lifecycle_obj.deploy(cfg)

    assert result["ok"] is True
    assert scp_to_calls == [], (
        f"macOS deploy must not use scp_to for directory upload "
        f"(leaks untracked files). Got calls: {scp_to_calls}"
    )


def test_macos_deploy_propagates_scp_dir_to_failure(monkeypatch):
    """When scp_dir_to returns rc != 0, deploy must return deploy_failed."""
    from agent import lifecycle
    from agent.lifecycle.macos import MacOSLifecycle

    monkeypatch.setattr(
        lifecycle.macos, "scp_dir_to",
        lambda *a, **k: (1, "Permission denied (publickey)"),
    )

    lifecycle_obj = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    result = lifecycle_obj.deploy(cfg)

    assert result["ok"] is False
    assert result["code"] == "deploy_failed"
    assert "Permission denied" in result["error"]


def test_macos_deploy_excludes_untracked_files(monkeypatch, tmp_path):
    """End-to-end-ish: build a LOCAL_TARGET tree with a mix of tracked
    and untracked files, mock git ls-files to return only the tracked
    ones, and verify scp_dir_to only sees those tracked rel paths."""
    import subprocess
    from agent import lifecycle
    from agent.lifecycle.macos import MacOSLifecycle

    # Build a fake local target tree
    target_root = tmp_path / "target"
    target_root.mkdir()
    (target_root / "server.py").write_text("# tracked\n")
    (target_root / "scripts").mkdir()
    (target_root / "scripts" / "start.sh").write_text("# tracked\n")
    # Untracked junk that must NOT be uploaded
    (target_root / "logs").mkdir()
    (target_root / "logs" / "big.log").write_text("debug noise")
    (target_root / ".tmp").write_text("scratch")
    (target_root / "local_config.json").write_text("{}")

    tracked_rel_paths = ["server.py", "scripts/start.sh"]

    # Mock subprocess.run so git ls-files returns the tracked-only list.
    # scp_dir_to reads cp.stdout as bytes and decodes utf-8, so the
    # mocked stdout MUST be bytes (CompletedProcess returns whatever
    # you give it). scp_dir_to runs git with cwd=local_dir.parent and
    # expects paths like "target/server.py" — the leading "target/"
    # gets stripped before upload.
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "ls-files"]:
            joined = "\n".join(
                f"target/{p}" for p in tracked_rel_paths
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=joined.encode("utf-8"),
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    uploaded_rel_paths = []

    def fake_paramiko_scp_to(ssh_cfg, local_path, remote_path, timeout=30):
        # Record which local paths scp_dir_to chose to upload.
        from pathlib import Path
        uploaded_rel_paths.append(Path(local_path).relative_to(target_root).as_posix())
        return (0, "")

    # _paramiko_scp_to is module-level in agent.ssh_runner; scp_dir_to
    # references it directly so monkeypatching the source module works.
    from agent import ssh_runner
    monkeypatch.setattr(ssh_runner, "_paramiko_scp_to", fake_paramiko_scp_to)

    # Skip the post-deploy run_ssh verification.
    monkeypatch.setattr(
        lifecycle.macos, "run_ssh",
        lambda *_a, **_k: (0, "found"),
    )

    lifecycle_obj = MacOSLifecycle()
    cfg = {
        "ssh": {"host": "127.0.0.1"},
        "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
    }

    # Patch LOCAL_TARGET to point at our tmp_path tree
    monkeypatch.setattr(lifecycle.macos, "LOCAL_TARGET", target_root)

    result = lifecycle_obj.deploy(cfg)

    assert result["ok"] is True, f"deploy failed: {result}"
    # Only tracked files uploaded
    assert set(uploaded_rel_paths) == set(tracked_rel_paths), (
        f"Tracked-only contract violated. Uploaded: {uploaded_rel_paths}. "
        f"Expected: {tracked_rel_paths}"
    )