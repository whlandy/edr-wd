# Target Sync Without Ad Hoc Script Writes

## Status

Implemented for the normal lifecycle paths. Keep this document until the
behavior has been exercised on live Windows and macOS targets and the remaining
optional cleanup items are either completed or intentionally dropped.

## Goal

EDR-WD should have one deployable target payload:

```text
repo target/  ->  configured target root
```

Once that payload is present, the agent should connect to the target MCP server,
initialize MCP, check `status`, and run baseline/E2E/SOP operations without
writing additional helper scripts to the target. Any required target-side helper
must be committed under `target/scripts/`, `target/scripts/macos/`, or
`target/automation/`, then delivered by the normal target sync.

The installation path is still agent-driven. The agent uses Paramiko over
SSH/SFTP/SCP to copy the tracked install payload to the target and invoke the
reviewed lifecycle installer. That explicit deploy/install phase is allowed to
write target files; the normal post-install connect/test/E2E/SOP phases are not.

This keeps the target filesystem predictable, makes test failures easier to
reason about, and prevents AI-agent drift where each troubleshooting session
creates a new one-off `.py`, `.ps1`, `.sh`, `.bat`, or `.json` file on the
target.

## Current Behavior To Fix

The original issue was that the code mostly avoided ad hoc generated scripts,
but several connection paths still performed implicit writes.

Original Windows lifecycle issues:

- `WindowsLifecycle.ensure_server_running()` checks required target files and,
  if they are missing, automatically calls `deploy()` and `install()`.
- `WindowsLifecycle.ensure_server_running()` uploads `start_server.ps1` before
  triggering the scheduled task.
- `WindowsLifecycle.stop_server()` uploads `stop_server.ps1` before executing
  it.

Original macOS lifecycle issues:

- `MacOSLifecycle.ensure_server_running()` uploads `start_server.sh` before
  kickstarting LaunchAgent.
- `MacOSLifecycle.stop_server()` uploads `stop_server.sh` before executing it.
- `MacOSLifecycle.deploy()` currently uses the generic upload path for the
  `target/` directory instead of the tracked-only directory sync used by
  Windows deploy. That can copy untracked cache/log/screenshot files.

Manual debug paths:

- `agent/edr-wd.sh push` and `agent/deploy.ps1 -Action push` can upload
  arbitrary files to the target `incoming/` directory. This is useful for manual
  debugging, but it must not be used by normal connect/test/SOP flows.

Current implemented behavior:

- normal `ensure_server_running(..., repair=False)` does not deploy/install or
  upload lifecycle scripts.
- Windows missing payload returns `target_payload_incomplete`.
- Windows invalid scheduled task returns `scheduled_task_invalid`.
- Windows start/stop use already-present target scripts/task definitions.
- macOS start/stop use already-present target scripts/LaunchAgent definitions.
- macOS deploy uses tracked-only target sync.
- wrapper help now marks `push` as manual/debug-only.

## Desired Lifecycle Contract

Use explicit phases instead of hidden repair.

### Phase 1: Preflight

Read-only checks only:

- validate target config.
- verify Paramiko SSH login when the target is remote.
- verify agent dependencies.
- verify target Python and backend dependencies through inline commands.
- check MCP port/tunnel state.
- never upload probe scripts.

### Phase 2: Deploy

Explicitly requested operation only:

- sync tracked `target/` files to the configured target root.
- transfer install payloads from the agent to the target using Paramiko
  SSH/SFTP/SCP, based on the target config credentials.
- do not sync untracked files, logs, screenshots, caches, local config, or
  generated artifacts.
- for Windows, register the tracked scheduled-task script.
- for macOS, register the tracked LaunchAgent script/template.

### Phase 3: Connect

No file writes:

- if `connect_mode=tunnel`, repair only the local tunnel.
- MCP initialize.
- call `status`.
- use the existing server if the backend is loaded.
- do not deploy or upload scripts just because EDR windows are not open.

### Phase 4: Start/Stop

No upload during start/stop:

- start/stop should invoke scripts already present in the target root.
- if a required script is missing, return a structured error such as
  `target_payload_incomplete`.
- the error should suggest running the explicit deploy/install action, not do it
  implicitly.

### Phase 5: E2E/SOP

No lifecycle writes:

- use MCP tools only.
- `activate_edr(wait=True)` may start EDR/HiSec applications, but it must not
  create helper scripts.
- evidence artifacts are allowed only under the configured artifact directory.

## Proposed Code Changes

1. Add a lifecycle option or mode for repair:

   ```python
   ensure_server_running(target, repair=False)
   TargetSubAgent.ensure_running(repair=False)
   ```

   Default `repair=False` for tests and normal connect flows. Explicit
   `deploy`/`install` remain the normal repair path; a dedicated `repair`
   wrapper can be added later if needed.

2. Change Windows `ensure_server_running()`:

   - keep `_target_integrity()` and `_task_integrity()` as read-only checks.
   - if integrity fails and `repair=False`, return
     `target_payload_incomplete` or `scheduled_task_invalid`.
   - if `repair=True`, allow deploy/install.
   - remove the unconditional `start_server.ps1` upload from the start path.
   - start only through the already-installed scheduled task.

3. Change Windows `stop_server()`:

   - execute the already-present remote `scripts/stop_server.ps1`.
   - if missing, return a read-only error plus explicit repair suggestion.
   - do not upload `stop_server.ps1` inside stop.

4. Change macOS `ensure_server_running()`:

   - do not upload `start_server.sh`.
   - verify that the LaunchAgent and tracked script are already present.
   - if missing, return a structured error with the explicit install/deploy
     command.

5. Change macOS `stop_server()`:

   - execute the already-present `scripts/macos/stop_server.sh`.
   - do not upload the script during stop.

6. Change macOS `deploy()`:

   - replace the generic directory upload with tracked-only `scp_dir_to()`.
   - keep conservative fallback filtering if git metadata is unavailable.

7. Restrict debug push paths:

   - document `push` as manual/debug only.
   - do not call `push` from connect, test, E2E, or SOP code.
   - consider renaming CLI help to `debug-push` in a later cleanup if the
     current name keeps causing misuse.

8. Add regression tests:

   - Windows ensure with missing target file and `repair=False` returns a
     structured error and does not call deploy/install.
   - Windows ensure with invalid task and `repair=False` does not call install.
   - macOS deploy uses tracked-only sync.
   - start/stop paths do not call `scp_to()` for lifecycle scripts.
   - test runner and pytest fixtures use non-repair connect by default.

## Should The Agent Run A Local MCP And Connect MCP-To-MCP?

Short answer: not for this cleanup.

Running a local MCP on the agent side and having it call the target MCP would add
another protocol boundary, another session lifecycle, and another failure mode,
but it would not remove the real cause of script writes. The writes come from
lifecycle code choosing to upload/repair target files during connect/start/stop.
That should be fixed directly in the lifecycle contract.

The better model is:

```text
Agent Python orchestration
  -> Paramiko for lifecycle and explicit deploy/install
  -> MCP client for target GUI tools
  -> one target MCP server in the target desktop session
```

Use a local MCP only if EDR-WD later needs to expose agent-side capabilities to
another external orchestrator. In that case it should be an optional facade over
the existing Python APIs, not an MCP-to-MCP proxy required for normal target
control.

Reasons not to add MCP-to-MCP now:

- it complicates debugging because failures can happen in either MCP session.
- it does not improve target filesystem hygiene.
- it risks hiding lifecycle side effects behind a second tool abstraction.
- it makes tests slower and harder to isolate.
- the existing `TargetSubAgent` already gives the right target-scoped ownership
  model without adding a second server.

## Acceptance Criteria

The cleanup is complete when:

- connecting to an existing target performs no remote file upload.
- running baseline tests performs no remote file upload.
- running HiSec E2E/SOP performs no remote lifecycle file upload.
- explicit deploy/install/repair commands are the only operations that sync
  tracked target files or install lifecycle hooks.
- macOS and Windows deploy both use tracked-only target sync.
- missing target files produce actionable errors instead of hidden repair.
- documentation states that `push` is manual/debug only.

Implemented regression coverage:

- `test_case/test_lifecycle_no_implicit_uploads.py`

Remaining optional cleanup:

- consider adding an explicit `repair` wrapper that runs deploy/install in one
  reviewed command.
- consider renaming `push` to `debug-push` in a breaking CLI cleanup.
- run live Windows and macOS target tests to confirm the no-upload lifecycle
  paths behave as expected outside unit tests.

## Suggested Implementation Order

1. Add `repair=False` plumbing through `TargetSubAgent` and
   `target_manager.ensure_server_running()`.
2. Update Windows lifecycle to return structured errors instead of hidden
   deploy/install by default.
3. Remove start/stop script uploads from Windows lifecycle normal paths.
4. Update macOS lifecycle to match the same no-upload start/stop behavior.
5. Fix macOS deploy to use tracked-only sync.
6. Add regression tests around no-upload connect/start/stop behavior.
7. Update `SKILL.md` and `references/agent-workflow.md` after code behavior and
   tests match this design.
