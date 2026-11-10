# EDR-WD

Cross-platform EDR GUI automation, desktop recording, and golden-trace replay
for HiSecEndpoint / EDRClient targets on Windows and macOS.

EDR-WD separates the **agent machine** (orchestration, MCP sessions, execution,
traces, reports) from the **target machine** (target-local MCP server doing real
GUI automation inside its own desktop session). Desktop recording captures
real user actions and assertions, compiles them into a golden trace, and
replays that trace back against the live target with semantic, hybrid, or
visual locators — with an explicit honesty gate: only traces compiled
`ready` replay, and ambiguous controls fail loudly instead of being guessed.

## What It Does

| Capability | Where |
|---|---|
| Target lifecycle (deploy, start, repair, tunnel) | `agent/`, `references/agent-workflow.md` |
| Target-local MCP server + GUI backends (pywinauto / macOS AX) | `target/` |
| Semantic window/control automation | `references/element-click.md` |
| Desktop recording → golden trace → replay | `references/recording.md` |
| Multi-window flows: same-titled dialogs/menus, sequential reopen, owner chains | `references/recording.md`, `docs/architecture/09…12` |
| Trajectory self-correction: repair candidates → step plan → agent re-record | `agent/recording/trace_repair.py`, `agent/recording/step_plan.py`, `agent/recording/rerecord_loop.py`, `docs/architecture/12-…` |
| Fixed product verification SOPs | `sops/INDEX.md` |
| Planner and evaluation | `agent/planner/`, `agent/eval/` |

## Supported Profiles

| Profile | Target OS | Backend |
|---|---|---|
| `windows_hisec` | Windows | `windows_pywinauto` |
| `macos_hisec` | macOS | `macos_accessibility` |
| `macos_generic` | macOS | `macos_accessibility` |

The agent OS does not decide the GUI backend — the target OS and configured
profile do.

## Installation

Prerequisites:

- Python ≥ 3.9 (3.13+ recommended) on both agent and target machines.
- Windows targets: Python available on the interactive desktop session;
  pywinauto needs a real desktop, not a service session.
- macOS targets: Accessibility permission for the MCP server process.

Install on the agent machine:

```bash
pip install -e .
```

Deploy the target server (run from the agent machine; the scripts push and
register everything the target needs):

```bash
# Windows target
agent/deploy.ps1

# macOS target
agent/edr-wd.sh
```

## Configure

```bash
edr-wd config --init
# edit config/targets.local.json (never commit this file)
edr-wd config --validate
edr-wd config --list
```

Start from `config/targets.example.json`. Target names follow
`<IP 3rd octet>.<IP 4th octet>-<hostname>-<os-version>`, e.g.
`2.26-edr-win26-win11`. Remote targets use Paramiko with inline password auth
for trusted intranet use.

## Verify

```bash
edr-wd status                       # probe the configured target
edr-wd up                           # ensure the target MCP server runs
edr-wd tools                        # list exposed MCP tools
python3 -m pytest -q                # offline test gate
python test_case/run_tests.py --target <TARGET>   # profile-dispatched live suite
```

## Quick Start: Record And Replay

```bash
# 1. Record a flow on the target (locks the matching window first)
edr-wd --target <TARGET> record start \
  --name policy-flow --process-name EDRClient.exe --window-title '^EDRClient$'

# 2. Interact with the target application; add assertions as needed
edr-wd --target <TARGET> record assert

# 3. Stop and compile — writes recording.json, case.json, golden-trace.json,
#    generated pytest, and compile-report.json under recordings/<name>/
edr-wd --target <TARGET> record stop --output-root recordings

# 4. Check readiness (scroll/drag steps need a bound result verifier)
edr-wd --target <TARGET> replay recordings/policy-flow/golden-trace.json
```

A golden trace replays only when `status == "ready"` and every step is `ready`.
`incomplete` traces fail fast with `golden_trace_incomplete` — this is an
intentional safety gate, not a bug. See `references/recording.md` for the
readiness rules, assertion types, recovery behavior, and redaction guarantees.

For an interactive recorder panel on the target's own desktop (no LLM in the
loop):

```bash
edr-wd --target <TARGET> record ui      # agent-driven panel bridge
python3 scripts/open_recorder.py        # target-local double-click entry
```

## Documentation

- `SKILL.md` — operating manual entry point with a task→reference table.
- `references/testing.md` — test suites, live gates, failure triage.
- `references/recording.md` — recording/replay end-to-end guide.
- `references/agent-workflow.md` — lifecycle and deployment internals.
- `docs/architecture/` — design documents: 03 record→replay, 09 cross-window
  sequence & instance binding, 10 compiler disambiguation, 11 basic-action
  usability & integrity (menu-layout locators, capture completeness C-rules),
  12 failure trajectory self-correction & agent re-record.
- `sops/INDEX.md` — fixed product verification procedures.

## Security Notes

- Never commit `config/targets.local.json`; it holds credentials.
- Recording captures, screenshots, OCR text, and evidence metadata are
  source-redacted on the target before persistence; password controls never
  yield readable assertion content (`recording_secret_plaintext` refusal).
- Replay evidence scrubs window titles and process names pre-persistence.
- See `SECURITY_NOTE.md` and `references/target-config.md` for the full model.

## Development

```bash
pip install -e ".[test]"
python3 -m pytest -q                                  # fast offline gate
python3 -m pytest -q -m "unit or regression"          # full offline gate
python3 -m pytest -q -m integration                   # needs a live target
```

Status markers: `unit` (deterministic offline), `regression` (exhaustive
offline matrices), `integration` (configured live MCP target), `e2e`
(live GUI). Live recording acceptance is opt-in via
`EDR_WD_RECORDING_E2E=1` — passing offline fakes never substitutes for the
live gate.

## License And Scope

Internal automation tooling for authorized HiSecEndpoint / EDRClient test
environments. Use only against targets you are authorized to automate.
