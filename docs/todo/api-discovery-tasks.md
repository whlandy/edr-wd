# API Discovery Module — P-level Task Tracker

> Source design: `docs/architecture/02-web-api-discovery-design.md`
> Module root: `agent/api_discovery/`  ·  Tests: `test_case/test_api_discovery/`
> Status legend: `[ ]` pending · `[x]` done · `[~]` in-progress
> Rule: ONE task per scheduled run. Implement → test → commit (NO push) → mark `[x]`.

Work is scoped to `agent/api_discovery/`, `test_case/test_api_discovery/`, and this
tracker only, so it never disturbs unrelated uncommitted changes.

---

## Phase A — Models, CDP, Network Recorder

### P0.1 — Core models + enums (`models.py`, `errors.py`)
- [ ] `agent/api_discovery/__init__.py`
- [ ] `agent/api_discovery/models.py`: frozen dataclasses + enums per §7
  - Enums: `DiscoverySource`, `InteractionRisk`, `EndpointConfidence`, `ResetStrategy`
  - Models: `DiscoveryConfig`, `UIControl`, `EndpointRecord`, `RequestRecord`, `ResponseRecord`, `DiscoveryReport`, `DiscoverySourceRef`
  - Every model: `to_dict()` + strict `from_dict()` (unknown-field rejection)
- [ ] `agent/api_discovery/errors.py`: typed failures (`DiscoveryError`, `ConfigValidationError`, `BrowserConnectError`, `StepTimeoutError`, ...)
- [ ] Unit tests: `test_case/test_api_discovery/test_models.py`

### P0.2 — Config loading + validation (`config.py`)
- [ ] `agent/api_discovery/config.py`: `DiscoveryConfig` defaults + `validate_discovery_config()`
  - Reject unbounded budget, non-HTTP(S) start URL, output outside record root, `allow_write_requests=True` w/o auth ref
- [ ] Unit tests: `test_case/test_api_discovery/test_config.py`

### P0.3 — CDP browser adapter (`browser.py`, `cdp_browser.py`)
- [ ] `browser.py`: `BrowserSession` protocol + adapter selection (`cdp` first, `playwright` optional)
- [ ] `cdp_browser.py`: raw Chrome DevTools Protocol connection
  - Connect to `--remote-debugging-port` (WS handshake), attach page target, enable `Network`/`Page`/`Runtime`
  - `navigate`, `evaluate`, `close`
- [ ] Unit tests: `test_case/test_api_discovery/test_cdp_browser.py` (mock WS)

### P0.4 — Network recorder (`network.py`, `har.py`)
- [ ] `network.py`: capture request/response, correlate request→response→UI action, normalise into `RequestRecord`/`ResponseRecord` (dedupe, strip secrets)
- [ ] `har.py`: HAR persistence + sanitisation
- [ ] Redaction reuse: `redaction.py` traces `agent/redaction.py` concepts
- [ ] Unit tests: `test_case/test_api_discovery/test_network_correlation.py`, `test_redaction.py`

## Phase B — State Crawler And Evidence

### P1.1 — UI inventory + classifier (`ui_inventory.py`, `ui_classifier.py`)
- [ ] `ui_inventory.py`: enumerate visible/nested controls from DOM (role, tag, text, automation/href)
- [ ] `ui_classifier.py`: control semantics + risk classification per §4.5 (READ_ONLY / LOCAL_ONLY / WRITE_POSSIBLE / WRITE_CONFIRMED / FORBIDDEN), skip write controls in default read-only mode
- [ ] Unit tests: `test_case/test_api_discovery/test_ui_classifier.py`

### P1.2 — State graph (`state_graph.py`)
- [ ] `state_graph.py`: state IDs, state signatures (DOM fingerprint), traversal graph, dedupe loops, bounded by `max_states`/`max_depth`
- [ ] Unit tests: `test_case/test_api_discovery/test_state_graph.py`

### P1.3 — Crawler + actions + reset (`crawler.py`, `actions.py`, `reset.py`)
- [ ] `actions.py`: safe action implementations (click read-only control, toggle accordion, open dropdown/dialog, paginate)
- [ ] `reset.py`: baseline restore strategies (`NONE`/`CLOSE_OVERLAY`/`RELOAD`/`NAVIGATE_BASELINE`/`NEW_CONTEXT`) + dirty-state detection
- [ ] `crawler.py`: bounded UI traversal; per confirmed §4.3 reset each dirty branch before sibling
- [ ] Fixture integration test: `test_case/test_api_discovery/fixtures/policy_general.html` + `test_crawler.py`

### P1.4 — Evidence capture
- [ ] Screenshots + action observations after each explored action (reuse `agent/trace` conventions)
- [ ] Attribute every observed request to its triggering control path
- [ ] Unit tests: `test_case/test_api_discovery/test_evidence.py`

## Phase C — Static Bundle Analysis And Merge

### P2.1 — Bundle collection (`bundle_assets.py`)
- [ ] Collect loaded JS assets (scripts, importmaps, module preloads) within scope
- [ ] Unit tests: `test_case/test_api_discovery/test_bundle_assets.py`

### P2.2 — Bundle parser (`bundle_parser.py`)
- [ ] Extract endpoint definitions + call sites from JS (token-aware + conservative regex; optional AST later per §26.2)
- [ ] Classify static endpoints by confidence (`STATIC_CONFIRMED` / `HEURISTIC`)
- [ ] Unit tests: `test_case/test_api_discovery/test_bundle_parser.py` (+ `fixtures/policy_bundle.min.js`)

### P2.3 — Endpoint merge + schema inference (`endpoint_merge.py`, `schema_inference.py`)
- [ ] `endpoint_merge.py`: canonicalise, dedupe, provenance merge across 3 sources, confidence resolution (§4.1/§4.4 — never blend observed with inferred)
- [ ] `schema_inference.py`: request/response structural schema inference
- [ ] Unit tests: `test_case/test_api_discovery/test_endpoint_merge.py`, `test_schema_inference.py`

## Phase D — CLI And Reports

### P3.1 — Orchestrator (`orchestrator.py`)
- [ ] Top-level discovery lifecycle (only public programmatic entry); wires config→browser→crawler→network→merge→artifacts
- [ ] Partial-run recovery
- [ ] Unit tests: `test_case/test_api_discovery/test_orchestrator.py`

### P3.2 — Artifacts + reports (`artifacts.py`, `report.py`)
- [ ] `artifacts.py`: run directory `~/Desktop/edr-wd-record/api-discovery/<ts>/`, atomic writes
- [ ] `report.py`: Markdown / HTML / JSON (+ HAR) inventory renderers with provenance & confidence
- [ ] Unit tests: `test_case/test_api_discovery/test_report.py`

### P3.3 — CLI wiring (`cli.py`)
- [ ] `add_api_discovery_arguments()` + `run_api_discovery_command()`; register `api-discover` subcommand on `agent/cli.py`
- [ ] `api-discover` completes on fixture page (Definition of Done §25.1)
- [ ] Unit tests + smoke: `test_case/test_api_discovery/test_cli.py`

## Phase E — Optional Adapters (deferred)

- [ ] E1 — Playwright adapter (`playwright_browser.py`)
- [ ] E2 — mitmproxy recorder (`proxy.py`)
- [ ] E3 — optional OpenAPI export

---

## Progress Log (append-only)

- (no runs yet — tracker created 2026-08-08)
