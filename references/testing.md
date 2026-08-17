# Testing Reference

Use this reference before changing profile dispatch, adding E2E cases, or
interpreting live target failures.

## Smoke Tests

Live MCP smoke:

```bash
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp
python target/tests/smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp --gui
```

## Profile-Dispatched Suites

```bash
python test_case/run_tests.py --target 2.26-edr-win26-win11
python test_case/run_tests.py --target 2.29-edr-mac29-macos14
```

The runner dispatches by `target.app_profile` or platform default:

- `windows_hisec` -> `test_case/run_windows_hisec.py`
- `macos_generic` -> `test_case/run_macos_generic.py`
- `macos_hisec` -> `test_case/run_macos_hisec.py`

If a target/backend mismatch is detected, fail clearly or route through the
existing profile-resolution code. Do not silently fall back to Windows tests.

## Pytest Suites

```bash
# Default fast gate: core deterministic tests, excluding exhaustive matrices.
python3 -m pytest -q

# Full offline gate, including evaluation/report/regression matrices.
python3 -m pytest -q -m "unit or regression"

# Only the exhaustive offline matrices.
python3 -m pytest -q -m regression

# Configured live MCP target, without the full GUI workflow.
python3 -m pytest -q -m integration

# Explicit Windows/macOS HiSec GUI workflow.
python3 -m pytest -q -m e2e

# Everything, useful only when a live target is intentionally available.
python3 -m pytest -q -m "unit or regression or integration or e2e"
```

Every collected test receives one primary marker from its directory:

- `unit`: everything outside the two live directories, including fake-target
  integration and regression contracts;
- `integration`: `test_case/test_integration/`, requiring a configured MCP
  target but not necessarily a complete HiSec workflow;
- `e2e`: `test_case/test_e2e/`, requiring the real Windows/macOS GUI workflow.

The high-volume `test_eval/`, `test_runs/`, and `test_regression/` matrices also
receive `regression`. They remain part of `unit` and therefore run in the PR
gate, but are excluded from the default fast gate.

`test_planner_e2e/` is also an offline `regression` suite. It uses a stub LLM
and fake observation snapshots while crossing the real catalog, structured
parser, target resolver, and replan policy. This validates planner safety
without clicking a live GUI.

The default configured in `pyproject.toml` runs `unit and not regression`.
Integration/E2E cases skip when TCP is open but MCP initialization or the
required backend is unavailable. Inspect skip reasons with:

```bash
python3 -m pytest -q -rs -m "integration or e2e"
```

Do not convert an unreachable live target into a local unit-test failure, but
do report that real GUI behavior remains unverified.

Focused local checks:

```bash
python3 target/tests/test_window_lock_contract.py
python3 scripts/test_profile_resolution.py
python3 scripts/test_target_config_platforms.py
```

## Desktop Recording And Golden Replay

Recording is target-scoped and requires a unique process/window match. Start the
recorder only after the target MCP server is ready:

```bash
python3 -m agent.cli --target TARGET record start \
  --name policy-flow \
  --process-name EDRClient.exe \
  --window-title '^EDRClient$'

python3 -m agent.cli --target TARGET record status
python3 -m agent.cli --target TARGET record pause
python3 -m agent.cli --target TARGET record resume
python3 -m agent.cli --target TARGET record assert
python3 -m agent.cli --target TARGET record stop --output-root recordings
```

`record start` connects, locks, and verifies the owning window before installing
listen-only hooks. `record stop` drains the target queue, retrieves each
source-redacted capture once, validates its digest, and writes `recording.json`,
`case.json`, `golden-trace.json`, generated pytest, `compile-report.json`, and
deduplicated observation assets under `OUTPUT_ROOT/<safe-flow-name>/`.
When the target has `app_profile` in its configuration, `record stop` binds it
into the generated golden trace automatically; `--profile PROFILE` overrides
that value.

An existing recording can be recompiled offline:

```bash
python3 -m agent.cli record compile recordings/policy-flow/recording.json \
  --output-root recordings --profile windows_hisec
```

Replay accepts only a ready golden trace whose catalog and profile binding match
the current runtime:

```bash
python3 -m agent.cli --target TARGET replay \
  recordings/policy-flow/golden-trace.json \
  --profile windows_hisec \
  --trace-root result-report/replay-traces
```

Use `--confirm-action ACTION_ID` for each action that requires out-of-band
confirmation. `semantic_only` is the default replay mode;
`--replay-mode semantic_first` or `visual_only` must be explicit. Recording and
replay cannot be active on the same target at the same time.

The raw recording's required `captureDiagnostics` fields make queue loss and
correlator errors compilation failures instead of silently accepting a partial
flow. Recorded scroll and drag, and unbound window transitions, remain visible
as incomplete steps. The platform adapters do not yet synthesize automatic
window-transition events.

Focused offline checks:

```bash
python3 -m pytest -q test_case/test_recording
```

Live recording acceptance is opt-in and requires an intentionally configured
target and desktop session:

```bash
EDR_WD_RECORDING_E2E=1 python3 -m pytest -q \
  test_case/test_e2e/test_recording_live_e2e.py
```

Passing the offline fake-session double-replay test does not replace the live
gate: Windows and macOS must each record a real flow and replay it twice after
fresh application starts before the feature is declared complete.

## E2E Naming

Use platform/profile naming:

- `test_case/test_e2e/test_windows_hisec_e2e.py`
- `test_case/test_e2e/test_macos_hisec_e2e.py`

Keep macOS and Windows E2E behavior separate when implementation differs, even
when the goal is the same. The shared goal for HiSec E2E is that both entry and
client windows become visible and are verified through window detection.

## Failure Triage

For window failures:

1. `activate_edr(wait=True)`
2. `list_windows()`
3. `is_window_open(process_name=...)`
4. `connect(process_name=...)`
5. `dump_tree(max_depth=...)`
6. screenshot only when permissions allow it

For tool-call failures, confirm the call path first. `tools/list` is a protocol
method, not a tool name for `call_tool()`.

PowerShell tests must not depend on public Internet reachability. Never use
`8.8.8.8`, public DNS, `google.com`, or external `Test-NetConnection` /
`Invoke-WebRequest` probes as a pass condition. Use target-local commands
(`Write-Output`, `$PSVersionTable`, `hostname`, local port checks) or the
configured intranet target endpoint.
