# Web API Discovery Architecture And Function-Level Design

## Status

- State: proposed implementation design
- Scope: agent-side browser API discovery
- Intended implementer: EDR-WD maintainers or a coding agent
- Initial schema version: `1.0.0`
- Related architecture:
  - [`00-overview.md`](00-overview.md)
  - [`01-action-trace-test-report-design.md`](01-action-trace-test-report-design.md)
- Motivating page: HiSec Endpoint `策略配置 -> 通用策略`

This document is the implementation contract for adding an automated website
API discovery mode to EDR-WD. It is deliberately detailed to function and
dependency level so implementation can proceed without rediscovering module
ownership or inventing parallel trace/report models.

## 1. Problem Statement

Recording requests while a person clicks a page only discovers APIs exercised
by that exact path. Static JavaScript scanning finds paths that may be dead,
shared, or unrelated to the current page. A crawler that clicks every visible
control can accidentally submit forms, cross into sibling pages, preserve dirty
form state, or attribute a shared request to the wrong nested component.

EDR-WD needs one controlled discovery workflow that:

1. records browser HTTP, HTTPS, Fetch, XHR, and WebSocket activity;
2. models pages as nested UI states instead of a flat list of controls;
3. explores safe controls automatically and resets state between branches;
4. associates every observed request with the exact UI action that triggered it;
5. statically extracts unexecuted endpoint definitions from loaded JavaScript;
6. distinguishes observed, statically inferred, and write-blocked endpoints;
7. creates screenshots and trace evidence using existing agent-side conventions;
8. renders a stable Markdown/HTML/JSON report suitable for automation work;
9. never sends a write request unless an explicit write policy authorizes it.

## 2. Goals

1. Discover all endpoints reachable from an explicitly scoped page or tab.
2. Traverse tabs, toggles, accordions, dialogs, drawers, dropdowns, trees,
   searches, and pagination controls using bounded deterministic rules.
3. Re-run each mutation-like UI branch from a clean baseline.
4. Record request and response schemas without retaining secrets.
5. Detect APIs that are preloaded before a nested control is opened.
6. Extract endpoint definitions and call sites from loaded JavaScript bundles.
7. Produce an endpoint inventory with confidence and provenance.
8. Reuse existing EDR-WD trace, evidence, redaction, and report directories.
9. Support a CLI entry point that can later be exposed through MCP.

## 3. Non-Goals For V1

- Automatically exploiting or fuzzing endpoints.
- Bypassing authentication, CAPTCHA, certificate policy, or authorization.
- Clicking submit, save, delete, apply, force-apply, upload, or download controls.
- Guaranteeing discovery of server-only or unreachable endpoints.
- Generating a perfect OpenAPI specification when runtime samples are absent.
- Replacing Burp Suite, OWASP ZAP, or mitmproxy as a security scanner.
- Installing browser/proxy dependencies on target machines.
- Driving the Windows EDRClient desktop UI; this design is for browser pages.

## 4. Design Principles

### 4.1 Three Sources, One Inventory

The final inventory merges:

- `runtime_browser`: requests captured from browser instrumentation;
- `runtime_proxy`: requests captured by an optional interception proxy;
- `static_bundle`: endpoint definitions extracted from JavaScript assets.

No source is treated as complete by itself.

### 4.2 UI State Is A Graph

A page is modeled as a graph of stable states and actions:

```text
state
  -> safe action
  -> request delta
  -> screenshot
  -> next state
```

The crawler does not recursively click the live DOM without a state budget.

### 4.3 Reset Between Dirty Branches

Any action that changes a toggle, selection, editable field, or local table may
set a dirty flag even before a server request occurs. Each such branch must be
explored in isolation:

```text
restore baseline -> navigate path -> one branch -> observe -> restore baseline
```

This prevents confirmation dialogs and retained React state from corrupting
later attribution.

### 4.4 Observed And Inferred Are Never Blended

Every endpoint carries `provenance` and `confidence`. A static call site must
not be presented as a captured request. A captured request must preserve its UI
trigger path.

### 4.5 Writes Are Classified Before Interaction

Visible control text, ARIA role, form ancestry, event semantics, and endpoint
method all contribute to write-risk classification. V1 defaults to read-only.

## 5. Agent/Target Ownership

The complete feature lives on the agent host.

```text
CLI / calling agent
  -> agent/api_discovery/orchestrator.py
       -> browser adapter (CDP or Playwright)
       -> optional mitmproxy adapter
       -> UI state crawler
       -> network recorder
       -> JavaScript bundle analyzer
       -> endpoint merger
       -> existing trace/evidence/report facilities
```

The target-side MCP server is not involved. Browser profiles, certificates,
HAR files, JS bundles, screenshots, and reports remain agent-local.

## 6. Proposed Module Layout

```text
agent/api_discovery/
  __init__.py
  models.py                 # dependency-light immutable models and enums
  config.py                 # configuration loading and validation
  orchestrator.py           # top-level discovery lifecycle
  browser.py                # BrowserSession protocol and adapter selection
  cdp_browser.py            # raw CDP adapter
  playwright_browser.py     # optional Playwright adapter
  network.py                # request/response capture and correlation
  har.py                    # HAR persistence and sanitisation
  ui_inventory.py           # visible-control discovery
  ui_classifier.py          # control semantics and risk classification
  state_graph.py            # state IDs, state signatures, traversal graph
  crawler.py                # bounded UI traversal
  reset.py                  # baseline restore strategies
  actions.py                # safe browser action implementations
  bundle_assets.py          # loaded JS asset collection
  bundle_parser.py          # endpoint and call-site extraction
  endpoint_merge.py         # canonicalisation, deduplication, confidence
  schema_inference.py       # request/response structural schemas
  redaction.py              # API-specific secret and payload redaction
  artifacts.py              # run directory and atomic artifact writes
  report.py                 # Markdown, HTML, JSON inventory renderers
  errors.py                 # typed discovery failures

test_case/test_api_discovery/
  __init__.py
  test_models.py
  test_ui_classifier.py
  test_state_graph.py
  test_network_correlation.py
  test_bundle_parser.py
  test_endpoint_merge.py
  test_schema_inference.py
  test_redaction.py
  test_crawler.py
  test_report.py
  fixtures/
    policy_general.html
    policy_general.har
    policy_bundle.min.js
    discovery_run.json
```

Do not add these classes to `agent/e2e_report.py`; that module remains the
compatibility report helper for live target E2E. API discovery may reuse its
atomic-write and image metadata conventions through `agent/trace` utilities.

## 7. Core Data Model

Models should use frozen dataclasses unless the repository standard moves to a
single supported Pydantic version. Every model must implement `to_dict()` and
strict `from_dict()` with unknown-field rejection.

### 7.1 Enums

```python
class DiscoverySource(str, Enum):
    RUNTIME_BROWSER = "runtime_browser"
    RUNTIME_PROXY = "runtime_proxy"
    STATIC_BUNDLE = "static_bundle"


class InteractionRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_ONLY = "local_only"
    WRITE_POSSIBLE = "write_possible"
    WRITE_CONFIRMED = "write_confirmed"
    FORBIDDEN = "forbidden"


class EndpointConfidence(str, Enum):
    OBSERVED = "observed"
    OBSERVED_VARIANT = "observed_variant"
    STATIC_CONFIRMED = "static_confirmed"
    HEURISTIC = "heuristic"


class ResetStrategy(str, Enum):
    NONE = "none"
    CLOSE_OVERLAY = "close_overlay"
    RELOAD = "reload"
    NAVIGATE_BASELINE = "navigate_baseline"
    NEW_CONTEXT = "new_context"
```

### 7.2 DiscoveryConfig

```python
@dataclass(frozen=True)
class DiscoveryConfig:
    start_url: str
    scope_url_prefix: str
    scope_tab_text: str | None
    output_root: Path
    browser_backend: str = "cdp"
    cdp_endpoint: str | None = None
    headless: bool = False
    use_proxy: bool = False
    proxy_url: str | None = None
    max_states: int = 500
    max_depth: int = 8
    max_actions_per_state: int = 100
    action_timeout_seconds: float = 10.0
    settle_time_seconds: float = 0.8
    include_websocket_frames: bool = True
    collect_response_bodies: bool = True
    collect_static_bundles: bool = True
    allow_local_form_mutation: bool = True
    allow_write_requests: bool = False
    deny_control_patterns: tuple[str, ...] = ()
    allow_control_patterns: tuple[str, ...] = ()
```

Validation function:

```python
def validate_discovery_config(config: DiscoveryConfig) -> None:
```

Dependencies: standard library only. It rejects an unbounded state/action
budget, non-HTTP(S) start URL, output paths outside the configured record root,
and `allow_write_requests=True` without an explicit authorization reference.

### 7.3 UIControl

```python
@dataclass(frozen=True)
class UIControl:
    control_id: str
    state_id: str
    role: str
    tag_name: str
    text: str
    aria_label: str | None
    placeholder: str | None
    input_type: str | None
    disabled: bool
    checked: bool | None
    selected: bool | None
    visible_rect: tuple[float, float, float, float]
    ancestor_text: tuple[str, ...]
    stable_attributes: Mapping[str, str]
    fingerprint: str
```

`control_id` is state-local. It is derived from canonical visible semantics,
not DOM array index alone.

### 7.4 UIActionCandidate

```python
@dataclass(frozen=True)
class UIActionCandidate:
    action_id: str
    state_id: str
    control_id: str
    kind: str
    value: str | None
    semantic_path: tuple[str, ...]
    risk: InteractionRisk
    risk_reasons: tuple[str, ...]
    reset_strategy: ResetStrategy
    expected_transition: str
```

Supported V1 `kind` values:

```text
click_tab
toggle
open_dropdown
select_option
open_dialog
close_overlay
expand_tree
expand_accordion
search_safe_value
paginate_next
paginate_previous
scroll_region
```

Typing into password, token, file, rich-text, or unknown-purpose inputs is not
generated automatically.

### 7.5 UIState

```python
@dataclass(frozen=True)
class UIState:
    state_id: str
    url: str
    route: str
    title: str
    semantic_path: tuple[str, ...]
    visible_text_digest: str
    control_digest: str
    overlay_digest: str
    screenshot_evidence_id: str
    dirty: bool
    captured_at: str
```

State identity is computed by:

```python
def compute_state_signature(snapshot: BrowserSnapshot) -> StateSignature:
```

The signature excludes volatile timestamps, random element IDs, animation
classes, and request counters.

### 7.6 NetworkExchange

```python
@dataclass(frozen=True)
class NetworkExchange:
    exchange_id: str
    request_id: str
    source: DiscoverySource
    started_at: str
    completed_at: str | None
    method: str
    url: str
    canonical_path: str
    query: Mapping[str, tuple[str, ...]]
    request_headers: Mapping[str, str]
    request_body: object | None
    response_status: int | None
    response_headers: Mapping[str, str]
    response_body: object | str | None
    resource_type: str | None
    initiator: Mapping[str, object]
    websocket_frames: tuple[Mapping[str, object], ...]
    redaction_rule_ids: tuple[str, ...]
```

Authentication values are removed before model construction. Raw unsanitised
headers must never be written to disk.

### 7.7 ActionObservation

```python
@dataclass(frozen=True)
class ActionObservation:
    action_id: str
    parent_state_id: str
    child_state_id: str | None
    started_at: str
    completed_at: str
    outcome: str
    before_evidence_id: str
    after_evidence_id: str
    exchange_ids: tuple[str, ...]
    console_error_ids: tuple[str, ...]
    reset_performed: ResetStrategy
```

This is the join model between UI traversal and endpoint discovery.

### 7.8 EndpointRecord

```python
@dataclass(frozen=True)
class EndpointRecord:
    endpoint_id: str
    method: str
    canonical_path: str
    host_scope: str
    query_schema: Mapping[str, object]
    request_schema: Mapping[str, object] | None
    response_schemas: Mapping[str, object]
    observed_statuses: tuple[int, ...]
    semantic_paths: tuple[tuple[str, ...], ...]
    action_ids: tuple[str, ...]
    sources: tuple[DiscoverySource, ...]
    confidence: EndpointConfidence
    side_effect: str
    static_call_sites: tuple[StaticCallSite, ...]
    exchange_ids: tuple[str, ...]
```

Canonical endpoint identity:

```python
def endpoint_identity(method: str, url: str) -> str:
```

Dynamic numeric, UUID, ULID, and selected tenant/asset path segments may be
templated only when at least two observed variants or one static template call
site support the substitution.

## 8. Browser Adapter Contract

`agent/api_discovery/browser.py` defines a protocol; orchestration must not
import Playwright directly.

```python
class BrowserSession(Protocol):
    def start(self, config: DiscoveryConfig) -> BrowserInfo: ...
    def stop(self) -> None: ...
    def navigate(self, url: str) -> None: ...
    def reload(self) -> None: ...
    def current_url(self) -> str: ...
    def snapshot(self) -> BrowserSnapshot: ...
    def screenshot(self) -> bytes: ...
    def list_loaded_assets(self) -> tuple[PageAsset, ...]: ...
    def response_body(self, request_id: str) -> bytes | None: ...
    def perform(self, action: BrowserAction) -> BrowserActionResult: ...
    def drain_events(self) -> tuple[BrowserEvent, ...]: ...
```

Factory:

```python
def create_browser_session(config: DiscoveryConfig) -> BrowserSession:
```

Dependencies:

- `create_browser_session()` -> `CDPBrowserSession` when backend is `cdp`.
- `create_browser_session()` -> `PlaywrightBrowserSession` when backend is
  `playwright` and the optional dependency is available.
- no caller imports an implementation class directly.

### 8.1 CDPBrowserSession

`agent/api_discovery/cdp_browser.py` uses Chrome DevTools Protocol domains:

```text
Page
Runtime
DOMSnapshot or Accessibility
Network
Log
Target
```

Required functions:

```python
def discover_cdp_target(endpoint: str, url_prefix: str) -> CDPTarget: ...
def connect_cdp_websocket(target: CDPTarget) -> CDPConnection: ...
def enable_cdp_domains(conn: CDPConnection) -> None: ...
def evaluate_json(conn: CDPConnection, expression: str) -> object: ...
def capture_cdp_screenshot(conn: CDPConnection, *, full_page: bool) -> bytes: ...
def collect_cdp_snapshot(conn: CDPConnection) -> BrowserSnapshot: ...
def execute_cdp_action(conn: CDPConnection, action: BrowserAction) -> BrowserActionResult: ...
```

`CDPConnection` owns one reader loop and correlates responses by command ID.
Callers must not call `recv()` directly.

```python
class CDPConnection:
    def send(self, method: str, params: Mapping[str, object] | None = None) -> dict: ...
    def events_since(self, cursor: int) -> tuple[int, tuple[dict, ...]]: ...
    def close(self) -> None: ...
```

### 8.2 PlaywrightBrowserSession

Optional dependency: `playwright`. It provides context-level request events,
WebSocket events, screenshots, locators, and HAR output.

Required functions:

```python
def launch_playwright_context(config: DiscoveryConfig) -> PlaywrightContextHandle: ...
def attach_playwright_cdp(config: DiscoveryConfig) -> PlaywrightContextHandle: ...
def record_playwright_har(handle: PlaywrightContextHandle, path: Path) -> None: ...
def stop_playwright_har(handle: PlaywrightContextHandle) -> None: ...
def perform_playwright_action(handle: PlaywrightContextHandle, action: BrowserAction) -> BrowserActionResult: ...
```

V1 implementation order should be CDP first because existing EDR-WD browser
work already relies on CDP sessions. Playwright follows as an adapter, not a
rewrite.

## 9. Network Capture And Correlation

`agent/api_discovery/network.py` owns event normalisation.

```python
class NetworkRecorder:
    def start(self, browser: BrowserSession) -> NetworkCursor: ...
    def checkpoint(self) -> NetworkCursor: ...
    def settle(self, *, idle_ms: int, timeout_seconds: float) -> None: ...
    def exchanges_between(self, start: NetworkCursor, end: NetworkCursor) -> tuple[NetworkExchange, ...]: ...
    def stop(self) -> tuple[NetworkExchange, ...]: ...
```

Event handlers:

```python
def on_request_will_be_sent(event: BrowserEvent, store: PendingExchangeStore) -> None: ...
def on_response_received(event: BrowserEvent, store: PendingExchangeStore) -> None: ...
def on_loading_finished(event: BrowserEvent, store: PendingExchangeStore, browser: BrowserSession) -> None: ...
def on_loading_failed(event: BrowserEvent, store: PendingExchangeStore) -> None: ...
def on_websocket_frame(event: BrowserEvent, store: PendingExchangeStore) -> None: ...
```

Correlation algorithm for one UI action:

1. checkpoint immediately before action;
2. perform one action;
3. wait for DOM stability and network idle;
4. checkpoint after settle;
5. select exchanges whose request start lies within the cursor interval;
6. mark earlier requests used by newly opened controls as `preloaded` when the
   control consumes data without a new request;
7. attach exchanges to `ActionObservation`.

`classify_exchange_role()` returns:

```text
triggered
preloaded
background
shared_container
navigation
```

```python
def classify_exchange_role(
    exchange: NetworkExchange,
    action: UIActionCandidate,
    before: UIState,
    after: UIState,
) -> str:
```

Heartbeats, telemetry, asset overview, branding, and message statistics are not
discarded; they are tagged `background` and excluded from the page-core table.

## 10. UI Inventory And Risk Classification

### 10.1 Control Inventory

`agent/api_discovery/ui_inventory.py`:

```python
def inventory_visible_controls(snapshot: BrowserSnapshot) -> tuple[UIControl, ...]: ...
def build_semantic_path(control: UIControl, snapshot: BrowserSnapshot) -> tuple[str, ...]: ...
def fingerprint_control(control: UIControl) -> str: ...
def deduplicate_controls(controls: Iterable[UIControl]) -> tuple[UIControl, ...]: ...
```

Inventory rules:

- visible and non-zero rectangle;
- actionable semantic roles and known interactive classes;
- include custom div-based toggles and tabs;
- include overlay controls separately from background controls;
- ignore hidden duplicate React component trees;
- prefer closest labelled ancestor over global text matches.

### 10.2 Action Generation

`agent/api_discovery/ui_classifier.py`:

```python
def classify_control(control: UIControl, context: ClassificationContext) -> ControlSemantics: ...
def classify_interaction_risk(control: UIControl, semantics: ControlSemantics) -> RiskDecision: ...
def propose_actions(state: UIState, controls: Sequence[UIControl], policy: CrawlPolicy) -> tuple[UIActionCandidate, ...]: ...
def is_scope_navigation(action: UIActionCandidate, config: DiscoveryConfig) -> bool: ...
```

Hard-denied control text patterns by default:

```text
保存
提交
确定（inside an edit/create dialog）
删除
应用
强制应用
更新
上传
下载
导入
导出
重置密码
注销
```

Context matters: `确定` in a read-only selector dialog may be local-only;
`确定` in an add/edit dialog is write-possible and forbidden.

### 10.3 Safe Search Values

```python
def choose_safe_search_value(control: UIControl, context: ClassificationContext) -> str | None:
```

Values come from explicit configuration or already visible non-sensitive text.
The crawler never invents credentials, personal identifiers, malware hashes,
or production mutation values.

## 11. State Graph And Traversal

`agent/api_discovery/state_graph.py`:

```python
class DiscoveryGraph:
    def add_state(self, state: UIState) -> bool: ...
    def add_action(self, observation: ActionObservation) -> None: ...
    def has_state_signature(self, signature: str) -> bool: ...
    def next_unvisited_action(self) -> UIActionCandidate | None: ...
    def to_dict(self) -> dict[str, object]: ...
```

`agent/api_discovery/crawler.py`:

```python
class UIStateCrawler:
    def crawl(self, baseline: BaselineState) -> CrawlResult: ...
    def explore_state(self, state: UIState, depth: int) -> None: ...
    def execute_candidate(self, candidate: UIActionCandidate) -> ActionObservation: ...
    def restore_for_candidate(self, candidate: UIActionCandidate) -> UIState: ...
    def should_stop(self) -> StopDecision: ...
```

Constructor dependencies:

```python
class UIStateCrawler:
    def __init__(
        self,
        browser: BrowserSession,
        recorder: NetworkRecorder,
        graph: DiscoveryGraph,
        artifacts: DiscoveryArtifactStore,
        classifier: UIClassifier,
        resetter: BaselineResetter,
        config: DiscoveryConfig,
    ) -> None: ...
```

Traversal is breadth-first by default. Sibling actions execute from the same
baseline path, not from one another's mutated state.

### 11.1 Nested Toggle Algorithm

```python
def explore_toggle_branch(candidate: UIActionCandidate) -> ActionObservation:
    baseline = resetter.restore(candidate.semantic_path[:-1])
    before = capture_state(baseline)
    start = recorder.checkpoint()
    result = browser.perform(to_browser_action(candidate))
    recorder.settle(idle_ms=500, timeout_seconds=10)
    after = capture_state()
    end = recorder.checkpoint()
    exchanges = recorder.exchanges_between(start, end)
    persist_action_evidence(before, after, candidate, exchanges)
    return observation
```

The next toggle is explored after another restore. This is mandatory even if
the prior toggle produced no network request.

### 11.2 Overlay Algorithm

Dialogs and drawers are explored as child states. The crawler inventories their
controls but blocks confirm/save/delete actions. It closes the overlay using an
explicit close control or Escape, then verifies the baseline state signature.

### 11.3 Scope Guard

```python
def enforce_scope(before_url: str, after_url: str, config: DiscoveryConfig) -> ScopeDecision:
```

If an action navigates outside `scope_url_prefix` or leaves the required tab,
the action is recorded as `scope_escape`, no children are explored, the page is
restored, and the screenshot is excluded from the scoped report gallery.

This specifically prevents confusing the nested “单网通策略” option with the
top-level sibling tab of the same name.

## 12. Baseline Restore

`agent/api_discovery/reset.py`:

```python
@dataclass(frozen=True)
class BaselineState:
    url: str
    semantic_path: tuple[str, ...]
    state_signature: str
    screenshot_evidence_id: str


class BaselineResetter:
    def capture(self) -> BaselineState: ...
    def restore(self, semantic_path: Sequence[str]) -> UIState: ...
    def close_transient_overlays(self) -> tuple[str, ...]: ...
    def verify_clean(self, expected: BaselineState, actual: UIState) -> bool: ...
```

Restore order:

1. close known transient overlays without changing persistent preferences;
2. reload current URL;
3. wait for DOM and network idle;
4. re-enter the explicitly scoped top-level tab;
5. replay only safe navigation actions in `semantic_path`;
6. verify state signature and absence of dirty-confirm dialogs;
7. use a fresh browser context only after bounded reload failures.

## 13. Static JavaScript Analysis

### 13.1 Asset Collection

`agent/api_discovery/bundle_assets.py`:

```python
def collect_javascript_assets(browser: BrowserSession) -> tuple[JavaScriptAsset, ...]: ...
def persist_javascript_asset(asset: JavaScriptAsset, store: DiscoveryArtifactStore) -> ArtifactRef: ...
def should_analyze_asset(asset: JavaScriptAsset, scope: AnalysisScope) -> bool: ...
```

Loaded bundle bodies are obtained from browser response bodies or HAR assets.
Source maps are collected only when already exposed by the application.

### 13.2 Parser

`agent/api_discovery/bundle_parser.py`:

```python
def extract_endpoint_literals(source: str, asset: JavaScriptAsset) -> tuple[StaticEndpoint, ...]: ...
def extract_http_call_sites(source: str, asset: JavaScriptAsset) -> tuple[StaticCallSite, ...]: ...
def extract_template_parameters(expression: str) -> tuple[StaticParameter, ...]: ...
def classify_call_site_scope(call_site: StaticCallSite, runtime_hints: RuntimeHints) -> str: ...
def parse_bundle(asset: JavaScriptAsset, runtime_hints: RuntimeHints) -> BundleAnalysis: ...
```

V1 recognises:

```text
fetch(url, options)
axios.get/post/put/delete/patch
http.get/post/put/delete/patch
basePath + literal
template literals
URLSearchParams
GraphQL endpoint + operationName
WebSocket constructor URLs
```

Regex may discover candidates, but call-site classification must use a token or
AST representation when the optional JavaScript parser is available. A regex-
only result is `heuristic`, never `static_confirmed`.

## 14. Endpoint Merge And Schema Inference

`agent/api_discovery/endpoint_merge.py`:

```python
def canonicalize_url(url: str, rules: CanonicalizationRules) -> CanonicalURL: ...
def merge_runtime_exchanges(exchanges: Sequence[NetworkExchange]) -> tuple[EndpointRecord, ...]: ...
def merge_static_endpoints(runtime: Sequence[EndpointRecord], static: Sequence[StaticEndpoint]) -> tuple[EndpointRecord, ...]: ...
def compute_endpoint_confidence(record: EndpointRecord) -> EndpointConfidence: ...
def classify_side_effect(record: EndpointRecord, call_sites: Sequence[StaticCallSite]) -> str: ...
```

`agent/api_discovery/schema_inference.py`:

```python
def infer_value_schema(value: object, *, depth: int = 0) -> dict[str, object]: ...
def merge_schemas(left: Mapping[str, object], right: Mapping[str, object]) -> dict[str, object]: ...
def infer_query_schema(exchanges: Sequence[NetworkExchange]) -> dict[str, object]: ...
def infer_request_schema(exchanges: Sequence[NetworkExchange]) -> dict[str, object] | None: ...
def infer_response_schemas(exchanges: Sequence[NetworkExchange]) -> dict[str, object]: ...
```

Schema inference records types, nullability, array item shape, observed enum
values, required-in-all-samples, and sample count. It does not retain full
production values in the final report.

## 15. Redaction

`agent/api_discovery/redaction.py` must reuse concepts from `agent/redaction.py`
and `agent/trace/redaction.py` without introducing a third inconsistent policy.

```python
def redact_request_headers(headers: Mapping[str, str]) -> RedactionResult: ...
def redact_response_headers(headers: Mapping[str, str]) -> RedactionResult: ...
def redact_url(url: str) -> RedactionResult: ...
def redact_payload(payload: object, rules: Sequence[RedactionRule]) -> RedactionResult: ...
def redact_websocket_frame(frame: object) -> RedactionResult: ...
```

Always redact:

```text
Authorization
Cookie / Set-Cookie
Proxy-Authorization
X-Auth-Token and equivalent token headers
password / passwd / secret / token / apiKey / session fields
certificate private material
```

The redaction audit stores rule IDs and digests, not original values.

## 16. Artifact And Report Layout

Default output:

```text
~/Desktop/edr-wd-record/api-discovery/<timestamp>/
  manifest.json
  config.redacted.json
  discovery.json
  endpoints.json
  state-graph.json
  network.har
  report.md
  report.html
  screenshots/
    000-init.png
    010-periodic-before.png
    011-periodic-after.png
  bundles/
    <sha256>.js
  schemas/
    <endpoint-id>.json
```

For the user's existing local convention, CLI accepts:

```text
--output-root ~/Desktop/edr-cloud-api
```

`agent/api_discovery/artifacts.py`:

```python
class DiscoveryArtifactStore:
    def create_run(self, config: DiscoveryConfig) -> DiscoveryRunPaths: ...
    def persist_screenshot(self, png: bytes, role: str, sequence: int) -> EvidenceRef: ...
    def persist_json(self, relative_path: str, value: object) -> ArtifactRef: ...
    def persist_bytes(self, relative_path: str, value: bytes) -> ArtifactRef: ...
    def finalize_manifest(self, manifest: DiscoveryManifest) -> None: ...
```

All writes are atomic: temporary file in the destination directory, `fsync`,
and replace.

`agent/api_discovery/report.py`:

```python
def build_report_model(result: DiscoveryResult) -> DiscoveryReport: ...
def render_markdown(report: DiscoveryReport) -> str: ...
def render_html(report: DiscoveryReport) -> str: ...
def render_endpoint_json(report: DiscoveryReport) -> dict[str, object]: ...
def write_discovery_reports(report: DiscoveryReport, store: DiscoveryArtifactStore) -> ReportArtifacts: ...
```

The report must show:

- page scope and browser information;
- coverage and stop reason;
- state hierarchy;
- endpoint table;
- request/response schemas;
- trigger path and screenshot pair;
- runtime vs static provenance;
- side-effect classification;
- blocked controls and why they were not clicked;
- background/shared requests separated from page-core requests;
- unresolved static candidates.

## 17. Orchestrator

`agent/api_discovery/orchestrator.py` is the only public programmatic entry.

```python
class APIDiscoveryOrchestrator:
    def __init__(
        self,
        browser_factory: BrowserFactory,
        proxy_factory: ProxyFactory | None,
        clock: Clock,
    ) -> None: ...

    def run(self, config: DiscoveryConfig) -> DiscoveryResult: ...
```

Internal functions and dependencies:

```python
def prepare_run(config, artifacts) -> PreparedRun:
    # validate_discovery_config
    # DiscoveryArtifactStore.create_run

def open_browser(config, browser_factory) -> BrowserSession:
    # create_browser_session
    # BrowserSession.start
    # BrowserSession.navigate

def capture_baseline(browser, recorder, resetter, artifacts) -> BaselineState:
    # NetworkRecorder.start
    # BaselineResetter.capture
    # persist screenshot

def discover_runtime(prepared, baseline) -> RuntimeDiscovery:
    # UIStateCrawler.crawl
    # DiscoveryGraph
    # ActionObservation persistence

def discover_static(browser, runtime, artifacts) -> StaticDiscovery:
    # collect_javascript_assets
    # parse_bundle
    # persist bundle digests

def reconcile(runtime, static) -> tuple[EndpointRecord, ...]:
    # merge_runtime_exchanges
    # merge_static_endpoints
    # schema inference

def finalize(result, artifacts) -> DiscoveryResult:
    # build_report_model
    # render/write Markdown, HTML, JSON
    # finalize manifest
```

`run()` cleanup contract:

1. stop HAR/proxy recording;
2. restore or reload the browser to discard local form changes;
3. close only browser contexts created by discovery;
4. preserve user-owned browser windows;
5. finalize partial reports on bounded failure;
6. never delete an already persisted run directory.

## 18. CLI Contract

Extend `agent/cli.py`:

```text
edr-wd api-discover URL
  --scope-url-prefix PREFIX
  --scope-tab TEXT
  --browser-backend cdp|playwright
  --cdp-endpoint http://127.0.0.1:9222
  --output-root PATH
  --max-states N
  --max-depth N
  --include-static / --no-static
  --proxy / --no-proxy
  --allow-local-form-mutation / --read-only-ui
```

Parser function:

```python
def add_api_discovery_arguments(parser: argparse.ArgumentParser) -> None:
```

Execution function:

```python
def run_api_discovery_command(args: argparse.Namespace) -> int:
```

`main()` must handle `api-discover` before resolving an EDR target because this
workflow is browser/agent-local and does not require `--target`.

Example:

```bash
python -m agent.cli api-discover \
  'https://170.170.12.157:31943/public/dist/iam/platform-web/index.html#/edr/policyconfig' \
  --scope-tab '通用策略' \
  --cdp-endpoint http://127.0.0.1:9222 \
  --output-root ~/Desktop/edr-cloud-api
```

## 19. Optional mitmproxy Integration

The proxy is a secondary recorder, not the controller.

`agent/api_discovery/proxy.py` may be added after core CDP support:

```python
class ProxyRecorder(Protocol):
    def start(self, config: DiscoveryConfig, run_dir: Path) -> ProxyInfo: ...
    def stop(self) -> ProxyCapture: ...


class MitmproxyRecorder:
    def start(self, config: DiscoveryConfig, run_dir: Path) -> ProxyInfo: ...
    def stop(self) -> ProxyCapture: ...
```

The browser must explicitly use the proxy and trust its CA through an approved
test profile. EDR-WD must not install a CA into the user's normal browser or
system trust store automatically.

Proxy exchanges are merged with browser exchanges by method, canonical URL,
timestamp window, and request-body digest.

```python
def correlate_proxy_exchange(
    proxy_exchange: NetworkExchange,
    browser_exchanges: Sequence[NetworkExchange],
) -> str | None:
```

## 20. Dependency Policy

Core modules use the standard library. Optional dependencies:

| Dependency | Scope | Required for V1 core |
|---|---|---|
| WebSocket client implementation | raw CDP transport | yes, unless implemented internally |
| `playwright` | Playwright adapter and native HAR | no |
| `mitmproxy` | proxy recorder | no |
| JavaScript AST parser | stronger static call-site analysis | no |

Dependencies must be declared under an optional project extra, for example:

```toml
[project.optional-dependencies]
api-discovery = [
  "websocket-client>=1.8,<2",
]
api-discovery-playwright = [
  "playwright>=1.60,<2",
]
api-discovery-proxy = [
  "mitmproxy>=12,<13",
]
```

Exact versions must be validated against the repository's supported Python
version during implementation. Import failures must produce actionable CLI
messages and never affect normal EDR-WD commands.

## 21. Function Dependency Graph

```text
agent.cli.main
  -> run_api_discovery_command
     -> DiscoveryConfig
     -> APIDiscoveryOrchestrator.run
        -> prepare_run
           -> validate_discovery_config
           -> DiscoveryArtifactStore.create_run
        -> open_browser
           -> create_browser_session
              -> CDPBrowserSession | PlaywrightBrowserSession
           -> BrowserSession.start
           -> BrowserSession.navigate
        -> NetworkRecorder.start
        -> BaselineResetter.capture
           -> BrowserSession.snapshot
           -> BrowserSession.screenshot
           -> DiscoveryArtifactStore.persist_screenshot
        -> UIStateCrawler.crawl
           -> inventory_visible_controls
           -> classify_control
           -> classify_interaction_risk
           -> propose_actions
           -> BaselineResetter.restore
           -> NetworkRecorder.checkpoint
           -> BrowserSession.perform
           -> NetworkRecorder.settle
           -> compute_state_signature
           -> NetworkRecorder.exchanges_between
           -> DiscoveryGraph.add_state/add_action
           -> DiscoveryArtifactStore.persist_screenshot
        -> collect_javascript_assets
           -> BrowserSession.list_loaded_assets
           -> BrowserSession.response_body
        -> parse_bundle
           -> extract_endpoint_literals
           -> extract_http_call_sites
           -> extract_template_parameters
        -> reconcile
           -> merge_runtime_exchanges
           -> merge_static_endpoints
           -> infer_query_schema
           -> infer_request_schema
           -> infer_response_schemas
           -> classify_side_effect
        -> build_report_model
        -> write_discovery_reports
           -> render_markdown
           -> render_html
           -> render_endpoint_json
           -> DiscoveryArtifactStore.finalize_manifest
```

## 22. Error Model

`agent/api_discovery/errors.py`:

```python
class DiscoveryError(Exception): ...
class BrowserUnavailableError(DiscoveryError): ...
class AuthenticationRequiredError(DiscoveryError): ...
class ScopeEscapeError(DiscoveryError): ...
class StateRestoreError(DiscoveryError): ...
class ActionBlockedError(DiscoveryError): ...
class NetworkCaptureError(DiscoveryError): ...
class BundleAnalysisError(DiscoveryError): ...
class DiscoveryBudgetExceeded(DiscoveryError): ...
```

Failures become structured stop reasons. A partial run still produces a report
and manifest with `status=partial` unless no baseline could be captured.

## 23. Test Plan

### 23.1 Unit Tests

`test_models.py`

- strict unknown-field rejection;
- enum round trip;
- deterministic IDs and canonical JSON.

`test_ui_classifier.py`

- safe tab/toggle/dropdown classification;
- submit/delete/apply/force-apply blocking;
- duplicate text disambiguation by visible ancestry;
- top-level vs nested same-text control.

`test_state_graph.py`

- stable state signatures;
- hidden duplicate component exclusion;
- cycle and budget termination;
- sibling isolation.

`test_network_correlation.py`

- request/response correlation;
- preloaded candidate classification;
- background heartbeat classification;
- WebSocket frame attachment;
- failed/incomplete response handling.

`test_bundle_parser.py`

- literal paths;
- base path concatenation;
- template literals;
- Axios/fetch/custom HTTP client calls;
- minified code;
- unrelated bundle candidates marked out-of-scope.

`test_endpoint_merge.py`

- runtime/static deduplication;
- method-sensitive identity;
- asset/assetGroup variants;
- path templating only with evidence;
- confidence calculation.

`test_redaction.py`

- cookies and authorization never persisted;
- token-like query and JSON fields;
- audit rule IDs;
- HTML/Markdown rendering escapes.

`test_crawler.py`

- toggle branch reload before sibling;
- overlay close and baseline verification;
- no click on write controls;
- scope escape recovery;
- deterministic traversal order.

### 23.2 Integration Fixture

Create a local fixture page with:

- two top-level tabs, one in scope;
- two hidden duplicate React-like trees;
- toggle revealing a paginated list;
- dropdown whose options are preloaded;
- dialog with cancel and submit;
- same text used for nested option and out-of-scope tab;
- GET, POST, WebSocket, and background heartbeat requests;
- a minified JS bundle containing one untriggered endpoint.

Assertions:

1. all safe GET requests are captured;
2. forbidden POST is statically discovered but not sent;
3. preloaded dropdown API is linked to the dropdown path;
4. hidden duplicate controls are not clicked;
5. state reset prevents dirty confirmation dialogs;
6. report screenshot links exist;
7. secrets are absent from every persisted artifact.

### 23.3 Live Acceptance Test

Against an explicitly authorized HiSec test environment:

```text
策略配置 -> 通用策略
```

Acceptance criteria:

1. all 12 Windows policy sections are inventoried;
2. nested toggle branches are explored independently;
3. asset tree expand/search/select variants are captured;
4. compliance, network, whitelist, tamper, peripheral, and protection dialogs
   appear in the state graph;
5. no application POST/DELETE request is observed;
6. `list-whitelist` read request is captured after opening its local toggle;
7. `escape` and `upgrade` endpoints are included;
8. runtime and static endpoint counts are separately reported;
9. every explored action has an after screenshot;
10. the final page is reloaded and contains no unsaved changes.

## 24. Delivery Phases

### Phase A — Models, CDP, Network Recorder

- `models.py`, `config.py`, `errors.py`
- CDP connection and event loop
- request/response capture
- redaction
- unit tests

### Phase B — State Crawler And Evidence

- UI inventory and classifier
- state graph
- baseline reset
- screenshots and action observations
- fixture integration test

### Phase C — Static Bundle Analysis And Merge

- asset collection
- endpoint/call-site parser
- runtime/static reconciliation
- schema inference

### Phase D — CLI And Reports

- `api-discover` command
- Markdown, HTML, JSON, HAR
- partial-run recovery
- documentation and examples

### Phase E — Optional Adapters

- Playwright adapter
- mitmproxy recorder
- optional OpenAPI export

## 25. Definition Of Done

The design is implemented when:

1. `python -m agent.cli api-discover ...` completes on the fixture page;
2. the crawler explores every safe nested state within configured budgets;
3. state-changing branches restore before sibling exploration;
4. no write endpoint is called in default mode;
5. runtime, proxy, and static sources merge without losing provenance;
6. reports include trigger paths, schemas, screenshots, and confidence;
7. all persisted artifacts pass redaction tests;
8. normal target CLI, lifecycle, MCP, and E2E tests remain unaffected;
9. live HiSec acceptance criteria pass on an authorized test environment.

## 26. Open Decisions For Implementation Review

1. Use `websocket-client` or implement a minimal internal CDP WebSocket
   transport. Recommendation: optional `websocket-client` dependency.
2. Use a Python or Node JavaScript AST parser. Recommendation: begin with a
   token-aware Python parser plus conservative regex candidates; add AST as an
   optional precision enhancement.
3. Export OpenAPI 3.1 immediately or retain the richer internal schema first.
   Recommendation: internal schema first, lossy OpenAPI export in Phase E.
4. Keep mitmproxy as a subprocess or library. Recommendation: subprocess with
   an EDR-WD addon and explicit lifecycle ownership.
5. Attach to the user's browser or launch an isolated profile by default.
   Recommendation: isolated profile by default; explicit `--attach` for a
   user-authenticated CDP session.
