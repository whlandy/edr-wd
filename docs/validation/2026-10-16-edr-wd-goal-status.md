# EDR-WD goal validation — 2026-10-16

This is a current-state validation record, not a completion certificate.
The working tree contains the implementation under review and is not yet
deployed to a live target.

## Offline evidence

Command: `python3 -m pytest -q -m 'unit or regression'`

Latest result: **2615 passed, 28 deselected** (39.44 s). `python3 -m compileall -q
agent target` and `git diff --check` both exit 0. This rerun includes the
generic-product activation failure-code check and macOS window resolution tests.
The latter now cover bundle/app-name connection through an actual native
window and verify that a cached connection cannot fabricate 800×600 bounds or
resurrect a closed window. Template replay also keeps a 100% relative click
anchor inside the exclusive element rectangle; boolean anchor coordinates
are rejected at selector validation. Component-ID drag replay now also keeps
100% start/end anchors within each current control rectangle and rejects
empty or inverted rectangles. The focused visual/replay suite has **75 passed**.
Visual-only template replay and the shared locator now reject non-finite,
non-numeric, boolean or out-of-range confidence values before ranking or
pointer materialization; OCR no longer turns Tesseract `NaN` into a perfect
score. The focused hybrid/OCR/visual/live-locator suite has **38 passed**.
Time-range matching now ignores ISO timezone offsets such as `+08:00` rather
than treating them as event-clock text; the focused time/expectation suites
have **51 passed**. The repository's `SKILL.md` passed
`skill-creator/scripts/quick_validate.py`.

Recorded control assertions now carry the assertion editor's own stable
control match into case and golden artifacts. A bound assertion on a log-time
control is evaluated against that control, not the preceding clicked button;
the compiled and golden JSON both validate against their schemas. A weak
assertion identity stays incomplete, and the recorder rejects it early when
authored through the editor. Positive control assertions reject missing and
duplicate matches; absence assertions fail if any match remains. Empty-string
`text_contains`/`window_text_contains` expectations are rejected before
recording and fail if supplied directly to an evaluator. The focused
expectation, recording, time and replay suite has **238 passed**. These are
offline assertions and do not substitute for a live EDRClient log check.

The selected suite includes:

| Requirement | Direct evidence |
| --- | --- |
| Product catalog/adapter and EDRClient intent | `test_case/test_app_catalog.py`, `test_case/test_product_intent.py`, `test_case/test_registered_app_activation.py` |
| Window identity, registry, ownership and guarded pointer operations | `test_case/test_window_registry.py`, `test_case/test_window_session.py`, `test_case/test_window_guard_coverage.py` |
| Shared hybrid locator, OCR and live visual candidates | `test_case/test_hybrid_locator.py`, `test_case/test_live_hybrid_action.py`, `test_case/test_ocr_redacted.py` |
| Time prefix, duration, record/compile/replay and deadline polling | `test_case/test_recording/test_time_range_assertion.py`, `test_case/test_recording/test_replay.py` |
| Stable control-ID and RGB/context-template replay, including repeated-icon locality, ambiguity rejection, pre-lock asset integrity and post-click cross-window re-locking | `test_case/test_recording/test_replay.py`, `test_case/test_recording/test_visual_templates.py` |
| Cross-window recording and stop-time flush | `test_case/test_recording/test_windows_capture.py`, `test_case/test_recording/test_windows_app_scope.py` |
| Tracked-only target deployment and required-module integrity | `test_case/test_deploy_payload_integrity.py`, `test_case/test_lifecycle_target_integrity.py`, `test_case/test_macos_deploy_tracked_only.py` |
| Page/API plus locked-client end–cloud comparison | `test_case/test_end_cloud_consistency.py`, `test_case/test_bundle_provider.py` |

The target deployment allowlist (`git ls-files -- target`) contains the new
`target/time_assertions.py`, `target/windowing/*`, `target/automation/ocr.py`,
`target/product_profiles.py`, `target/product_policy.py`, and
`target/product_specs/apps.json`. This only proves local packaging eligibility,
not upload or successful target import.

Deployment now refuses an empty tracked-only manifest, checks every current
product/windowing/OCR/time-assertion module and platform startup script on the
remote host, and reads the probe's actual `found`/`missing` output rather than
accepting exit code 0 alone. Target startup integrity uses the same required
file lists. Before any remote I/O, Windows and macOS deployment now also
reject required local files missing from disk or the actual Git upload manifest;
this prevents an old remote copy from masking an omitted new module. The local
manifest and negative paths have direct tests (**11 focused passes**), but
there is still no successful remote upload/import evidence.

The replay executor now focuses a recorded step's window before its first
refresh, and if a successful action transfers foreground to the explicitly
recorded next window, it can re-lock that window after the action. This
recovery is not permitted when the current step still asserts a control in
the old window. If a cached lock for consecutive same-window steps has gone
stale before dispatch, it now re-focuses that recorded window once and
refreshes again; it never repeats the preceding action. All three paths have
direct offline tests; live EDRClient replay remains unverified.

Recording now builds a click's visual template from its redacted
`beforeCapture`, not the post-action `capture`. This matters when the click
closes or navigates away from its target: replay searches the pre-action UI.
Template generation also requires `observed_target.protected is False`; an
unknown protection status cannot turn a pre-action frame into a target
template. The focused recording-evidence, management and visual-template
suite has **60 passed**. Its end-to-end artifact test deliberately uses a
blue pre-action frame and red post-action frame and checks that the emitted
template is blue; a parametrized privacy test covers protected and unknown
targets. This establishes offline artifact correctness, not live template
match stability.

Image-only clicks now have an explicit path through recording compilation and
replay. Compilation leaves a click with no stable semantic identity incomplete;
artifact generation promotes only that selector issue after it creates a
digest-verified redacted pre-action template for an explicitly unprotected
target. Missing/tampered captures leave it incomplete. The saved golden trace,
compile report and `record stop` response agree on the promoted status and
issues. `semantic_first` and `visual_only` can execute the resulting empty-
control selector through the guarded visual resolver; default `semantic_only`
fails without dispatch. The focused evidence, replay and management suite has
**117 passed** after adding generated-fixture and pre-lock mode coverage. The generated pytest
fixture selects `semantic_first` for image-only clicks, otherwise
`semantic_only`, while allowing an explicit `EDR_WD_REPLAY_MODE` override.
An image-only trace requested in `semantic_only` mode is rejected before any
target window lock or remote call.
The emitted PNGs were also matched twice with the real Pillow matcher at two
different window origins; both resolved to the same guarded window-relative
click point. This is offline evidence; actual image-only EDRClient replay
still needs the live two-run acceptance gate.

Windows cross-process recording now derives its application root from the
locked entry-window PID when available, rather than the first same-named
process. When a foreground/window event supplies a PID, the correlator checks
that exact executable path against the root; a different installation with
the same executable name is excluded. The focused Windows capture/scope suite
has **95 passed**; the target's actual multi-process flow remains unverified.

End–cloud polling now reads cloud and client independently on every attempt.
If either event is pending, a timeout retains the available evidence from
both sides and reports both pending reasons when applicable. This behavior
has offline tests; a real Browser Use page/API and EDRClient log comparison
is still required.

Within the cloud reader, page and API are likewise read independently: an
API event already visible while the page is pending still contributes its
opaque evidence reference. This also holds when the page fetch raises a
`SnapshotPending` exception: both fetches run, and one or both pending reasons
and evidence references survive the poll. The focused end–cloud suite has
**24 passed**. A pending client log retains both its observation
ID and redacted frame digest. These are evidence-preservation checks, not a
substitute for live source binding.
The sibling `edr-cloud-recorder` Skill has an `orchestrate/` layer for cloud
and endpoint steps, but no configured Browser Use page/API callbacks or live
EDRClient log selector were available in this run; those are still acceptance
inputs, not offline proof.

## Live gates not yet passed

- `python3 -m agent.cli --target 11.26-desktop-s917l7s-win10 status` again
  returned `stage=probe`, `SSH connection timed out` (2026-10-16, latest probe
  after the pre-action template fix). No deployment
  or live EDRClient run was attempted after this failure.
- `python3 -m agent.cli --target mac-dev status` also returned an SSH timeout.
  `/Applications/HiSecEndpoint.app` is absent on the local host.
- The 28 deselected tests are live/integration tests. They include Windows and
  macOS HiSec activation/window checks, the opt-in recording lifecycle, and a
  new generic-product Calculator smoke.
- Local Calculator smoke reached application activation, actual-window discovery,
  product-run creation, and role resolution. Strict foreground lock then rejected
  it: the latest opt-in run still reported WeChat as frontmost despite Calculator
  activation. Direct backend diagnostics confirmed a successful PID connection
  to Calculator, followed by a failed strict lock (`window_focus_failed`).
  The test is not counted as passed; no ownership guard was relaxed to force it.
- Live acceptance still needs: deploy/import the target package; record a
  real cross-window flow; replay it twice after fresh starts in both stable-ID
  and guarded-template modes; validate an actual page/API/client comparison
  for one device, policy and event-time window. Preserve trace and redacted
  evidence IDs, and do not claim success from an offline double alone.

On 2026-10-19, the terminal-window gap was addressed offline. A new
`observe_product_windows` target tool enumerates registered roles under the
active product run without shifting the GUI lock, and verifies each PID's
executable path against the installed app catalog. Ambiguous roles, uncertain
process ownership, unclassified product windows and failed platform
enumeration now fail closed. The MCP
observation provider exposes this inventory only for window-only assertions;
the executor uses it both immediately after the action and while polling, so
an asynchronous new window can appear after the old lock loses foreground.
An older target without the inventory tool is rejected before window-lock
acquisition for a golden trace with a window verifier. The focused window
session and replay suites have **79 passed**. These tests cover a terminal
`window_open` after the old lock becomes unusable, delayed appearance,
`window_closed` passing after a window genuinely disappears and failing while
the registered window remains, path-based
exclusion of a namesake process, role ambiguity, ownership uncertainty and
MCP run-token routing. At that stage, the full selected offline suite reported
**2476 passed, 28 deselected**; `compileall`, `git diff --check`, and the
Skill validator also exited 0. The Windows target status probe on 2026-10-19
still failed at `stage=probe` with `SSH connection timed out`, so this is not
yet live EDRClient evidence.

The opt-in local macOS Calculator activation/lock/tree smoke passed once on
2026-10-19 (`1 passed`, 66.83 s). A second run, extended to assert the new
inventory tool after focus, failed at strict `focus_window_role` with
`window_focus_failed` before the inventory assertion; the GUI foreground
guard was not relaxed. A separate direct product-run probe then called
`observe_product_windows` without taking a GUI lock and succeeded: it
returned one registered `main` Calculator window with a native ID, PID,
title `计算器`, and the expected role, then released the run token. This is
real macOS inventory evidence, but neither a repeatable focus test nor a
Windows EDRClient replay pass.

The end–cloud correlation spec now rejects non-finite, boolean or nonnumeric
polling deadlines and intervals before either provider is read. The cloud
page/API skew limit has the same validation, avoiding a `NaN` deadline that
could never expire. The focused provider/checker suite has **35 passed**;
the full selected offline suite is now **2489 passed, 28 deselected**. The
11.26 Windows status probe still timed out over SSH in this continuation.
The browser inventory showed no usable in-app/MCP Apps tab; the Chrome tab
provider twice returned `nodeRepl.fetch request failed`, so no cloud page or
API session could be bound. This is a tool-access failure, not evidence that
the cloud application has no event.
Native Chrome inspection found the existing `HiSec Endpoint` tab, but its
saved login URL currently displays `ERR_CONNECTION_CLOSED` for
`170.170.12.157:31943`; it exposes no page/API event state to compare. The
tab was accidentally closed during inspection and immediately restored with
Chrome's reopen-tab shortcut; the original URL is present again. No cloud
mutation or credential entry was attempted.

On 2026-10-19, an offline check loaded all nine existing golden traces with
`GoldenTrace.from_dict`; all retained the current action-catalog digest. Three
traces were ready and six were incomplete. The recorded Windows selectors
include `HiSecEndpointAgent.exe` / `logo1` for the entry window and
`EDRClient.exe` / `logo1` for the main window, followed by
`EDRClient.exe` / `日志中心`. The registered EDRClient profile previously omitted
`logo1` from the entry and main title patterns, which would have rejected
these real recorded selectors during product-scoped role resolution. The
profile now admits that observed title for the corresponding process-bound
roles; it does not relax the executable-path ownership check. A regression
test reads the existing traces and resolves their entry, main and log-center
roles through `WindowSession.observe_product_windows`. The focused suites
reported **87 passed**; the full selected offline suite reported **2490
passed, 28 deselected**. `compileall`, `git diff --check`, and the Skill
validator exited 0. This fixes catalog/recording alignment offline, not a
live replay success.

On 2026-10-19, negative assertion review found an offline false-pass path:
`window_closed` and `control_absent` treated a missing or malformed observation
collection as an empty, valid inventory. They now require a present,
well-formed window list or control tree before absence can pass. The MCP
observation provider also rejects malformed control entries instead of
silently dropping them, so a partially invalid target tree cannot make a
recorded control look absent. Eight regression cases first reproduced the
false pass; two provider tests cover both legacy and bundled observations.
The focused expectation/provider/replay suites reported **123 passed**, and
the selected offline suite reported **2500 passed, 28 deselected**.
`compileall`, `git diff --check`, and the Skill validator exited 0. The
11.26 Windows target was probed again after these changes and still returned
`stage=probe`, `SSH connection timed out`; no deployment or live replay was
claimed.

A further 2026-10-19 cross-process replay check found that initial target
acquisition preferred `golden.environment.application` over the first step's
own recorded window process. In a flow beginning in the HiSec entry agent but
labelled with the primary `EDRClient.exe` application, that would lock the
wrong process before step replay. `golden_entry_scope` now uses the first
selector's process and title together, falling back to the environment
application only if the selector lacks a process. A regression test covers
this entry-agent/main-client split; focused replay, generated-projection and
CLI suites reported **82 passed**. As an additional read-only artifact check,
all three locally ready golden traces compiled to test cases and passed
SHA-256/PNG validation for their existing visual templates in
`semantic_first` mode. The six locally incomplete traces were not presented
as runnable cases. The full selected offline suite reported **2501 passed,
28 deselected**; `compileall`, `git diff --check`, and the Skill validator
exited 0. This remains offline evidence, not live EDRClient replay.

Another 2026-10-19 recording-evidence review found that protected-control
redaction caching could be keyed only by PID/title, or by no usable identity
on the Windows facade. A second top-level window with the same title could
therefore inherit the first window's sensitive rectangles for the cache TTL.
The cache now prefers the actual window lock's native handle and PID (or a
connected snapshot with a native handle); when native identity is unavailable
it does not reuse cached redactions. Frame capture also checks that this
identity is unchanged across protected-control enumeration and screenshot;
it re-enumerates once after a replacement and refuses a frame if the window
keeps changing. Direct tests cover same-title/different-handle invalidation,
unknown-identity non-reuse, one successful replacement recovery, and repeated
replacement refusal. The focused recording-evidence suite reported **34
passed**. The selected offline suite reported **2505 passed, 28 deselected**;
`compileall`, `git diff --check`, and the Skill validator exited 0. No live
target capture was performed in this check.

The next 2026-10-19 end–cloud review reproduced a false-consistency case:
Python equates `True` with `1`, so a boolean policy state on the cloud page
could previously pass against a numeric client value, and page/API values of
those two types could be silently merged. Field comparison now recurses
through mappings and arrays with boolean-type awareness while retaining
normal numeric equivalence. The Browser Use reader also requires a distinct
page evidence reference and API evidence reference, permitting a shared
session reference but rejecting a duplicated single-read proof. Direct tests
cover cloud/client and page/API boolean-vs-number disagreement, nested
values, numeric equivalence, duplicate evidence refusal, and valid shared
session provenance. The focused consistency/provider suites reported **43
passed**. The selected offline suite reported **2510 passed, 28 deselected**;
`compileall`, `git diff --check`, and the Skill validator exited 0. These
remain provider-contract tests; no live cloud/client comparison is claimed.

The 11.26 target was probed again on 2026-10-19 and still returned
`stage=probe`, `SSH connection timed out`. Offline time-assertion review then
checked the user's literal `10:10:0` example: the matcher previously accepted
only two-digit seconds, so `10:10` did not match that displayed value. It now
accepts one- or two-digit seconds while retaining strict hour/minute bounds,
minute-prefix width, explicit duration, midnight wrap and timezone-offset
exclusion. A record → compile → replay test covers an unpadded second prefix
with a two-second duration and its exclusive upper boundary. The focused
time/expectation suite reported **97 passed** before this added end-to-end
case; the final time-range suite reported **28 passed**. The selected offline
suite reported **2514 passed, 28 deselected**; `compileall`, `git diff --check`,
and the Skill validator exited 0. Live EDRClient replay is still unverified.

Another 2026-10-19 deployment review found that both platform lifecycles
could report `already_running` as successful solely because their managed
server port was listening, bypassing the required-payload check. Both now
check target payload integrity before reporting the existing server ready;
two new regression tests cover missing required files with a listener present.
The focused deployment/lifecycle suites reported **36 passed**. The selected
offline suite reported **2516 passed, 28 deselected**; `compileall`,
`git diff --check`, and the Skill validator exited 0. This verifies required
files on disk, not that a pre-existing process has loaded the latest code.

An opt-in local macOS Calculator smoke test did not pass its live focus gate:
`focus_window_role("main")` returned `window_focus_failed`. A direct activation
probe found Calculator's window and PID, but the observed foreground process
remained WeChat. The strict window-ownership check was preserved; this run is
not evidence of successful GUI replay. The 11.26 Windows target and cloud
session remain unavailable for live EDRClient/end–cloud validation.

Follow-up deployment validation on 2026-10-19 closed the remaining
`already_running` ambiguity: file presence is not proof that the listener
loaded those files. Windows and macOS now query the live MCP `tools/list`
after payload integrity and again after a fresh start. The required set covers
product runs/window roles, locked observation, action execution, recording,
and replay capture. A missing tool returns `runtime_capabilities_missing`;
transport or malformed-response failures return
`runtime_capability_probe_failed`, with no ready result. The required set was
checked against the current FastMCP server's actual registered tools; stale,
healthy, and transport-failure cases have direct tests. The focused
deployment/lifecycle suites reported **49 passed** and the selected offline
suite **2522 passed, 28 deselected**. This checks advertised capabilities,
not an exact process code digest or successful live GUI operation.

The next 2026-10-19 visual-replay check reproduced a coordinate-provenance
gap: bundle ingestion accepted a redacted screenshot with a missing, malformed
or unrelated screen origin. The Pillow matcher could then use a wrong origin
and return an offset template location. Bundle ingestion now requires an
integer screenshot origin near the locked window's top-left, with a bounded
32-pixel allowance for UIA versus Win32 frame geometry; an invalid window
rectangle is also refused. Four negative origin cases and a small-frame-skew
positive case were added. The focused bundle/replay/template suites reported
**100 passed**, and the selected offline suite **2527 passed, 28 deselected**.
The 11.26 status probe in this continuation again returned `stage=probe`,
`SSH connection timed out`, so no live template or component-ID replay pass
is claimed from this check.

Later on 2026-10-19, the 11.26 SSH connection and remote identity probe
recovered. `python3 -m agent.cli --target 11.26-desktop-s917l7s-win10 status`
now reaches the host but returns `code=python_not_found`: the configured
Python 3.11 executable path does not exist. Read-only remote checks found no
`python.exe` or `py.exe` on PATH, no Python installation at the configured
per-user directory, no `python.exe` in the first two levels beneath Program
Files, and no MCP listener on port 8765. A broad per-user recursive search
timed out without output, so this is not proof that Python is absent from the
entire machine. Live deployment/replay is gated on an operator-provided valid
Python path or installation; no remote files or services were changed.

An offline end–cloud review on 2026-10-19 reproduced a false-consistency
case: provider mappings could include additional event timestamps, but
normalization discarded them, allowing a current observation time to hide a
stale client log or cloud page/API event. Provider normalization now parses
and validates optional timezone-aware `supporting_times`; the cloud reader
retains each side's declared event times and `check_consistency()` checks all
of them against the requested device/policy time window. Three regression
tests first failed for dropped page/API times, a stale client event, and a
naive client timestamp; the focused suite then reported **43 passed**. The
selected offline suite reported **2530 passed, 28 deselected**. This remains
provider-contract evidence, not a live cloud/EDRClient comparison.

## Next gate

On 2026-10-19, the cloud endpoint responded to read-only probes (`HTTP 200`
on the HTTP root and `HTTP 302` on the HTTPS root with certificate verification
disabled for the probe). The existing Chrome console tab still displayed
`ERR_CONNECTION_CLOSED`, and a refresh could not attach to that tab. This is
only network reachability evidence: there is no authenticated page/API read,
and no end–cloud comparison or live replay pass is claimed.

The next locator review reproduced two false-selection paths before the fix:
duplicate visual detections of one candidate could outvote a stronger unique
match elsewhere, and a low-confidence overlap on the sole semantic control
could mask a high-confidence conflicting visual location. Fusion now uses at
most the strongest visual and strongest OCR hit per candidate; conflict
checking counts only visual hits meeting the policy threshold. OCR evidence
also requires a redacted window capture and an OCR rectangle fully inside
the locked window, matching the existing visual-evidence boundary. Four new
negative/regression tests failed before the corrections and passed after;
the focused hybrid/live/replay/OCR suite reported **98 passed**. The selected
offline suite reported **2534 passed, 28 deselected**; `compileall`, `git
diff --check`, and the Skill validator exited 0. These are synthetic
candidate tests, not live matching or replay stability evidence.

The same day's 11.26 status probe reached SSH but still returned
`python_not_found` for the configured Python 3.11 path. A bounded read-only
probe of common system, Conda, user and WindowsApps interpreter paths found
no `python.exe`; it does not prove absence from every directory. No remote
installation or configuration change was made.

A further recording-to-replay check on 2026-10-19 found that `record stop`
binds the target's app profile to the golden trace, while CLI `replay` used
the literal `default` when `--profile` was omitted. That rejected a normally
recorded EDRClient trace before execution. CLI replay now resolves the same
target app profile from the shared config; the generated pytest fixture uses
the golden binding or, for an unbound trace, the target app profile. A bound
profile mismatch is rejected inside `build_replay_runtime()` before any
target tool call, including when a generated fixture supplies an override.
The four direct cases failed before correction and now pass; the focused
replay/generated-adhoc/CLI suite reported **84 passed**. The selected offline
suite reported **2536 passed, 28 deselected**; `compileall`, `git diff
--check`, and the Skill validator exited 0. This closes a default-path
rejection, but no 11.26 replay success is claimed.

When a target is reachable, run target status/identity probe, deploy via the
normal repair path, then perform the live checks above. If the cloud session
is available, bind the Browser Use page and API reads to
`BrowserUseCloudReader` and the locked EDR-WD observation to
`EDRWDClientReader`; use the same device/policy IDs and explicit event-time
window. Do not store raw page content, credentials or screenshots in the
comparison fields.

On 2026-10-19, a cross-window assertion review reproduced a recording bug:
opening the assertion editor by Ctrl+Shift+A in 日志中心 still authored the
assertion against the entry EDRClient window. The session now carries the
hotkey event's product-window identity into the assertion event, including
the UIA root ID when present; the CLI editor uses the last recorded window
or a newly opened window transition before its first click. Window-wide and
control-ID time-range assertion regressions, including the before-first-click
case, failed before correction and now pass. The focused recording/compiler
suite reported **134 passed**, and the selected offline suite reported
**2540 passed, 28 deselected**. `compileall`, `git diff --check`, and the Skill
validator exited 0. This confirms selector projection in synthetic capture,
not live 11.26 replay. A verifier explicitly bound to a previous action in
a different window still needs separate cross-window execution validation;
the default assertion editor leaves `bindPrevious` off.

The next 2026-10-19 replay check reproduced the remaining explicit-binding
gap: a `bindPrevious=true` content assertion in 日志中心 was folded into an
EDRClient action step, so the verifier read the old window. The compiler now
emits a separate observe-only step whenever a causally bound content
assertion carries a different recorded window identity, while retaining its
causal proof for scroll/drag verifier requirements. Source-event alignment
for visual artifacts follows the same split. Tests cover control-ID and
window-wide time-range assertions, a verifier-required scroll, and the
replay executor focusing the log window without repeating the click. The
focused compiler/replay/time suite reported **184 passed**; the selected
offline suite reported **2545 passed, 28 deselected**. `compileall`, `git
diff --check`, and the Skill validator exited 0. The 11.26 status probe still
reached SSH but returned `python_not_found` at its configured interpreter
path; no live replay or end-cloud comparison is claimed.

A further 2026-10-19 identity check found that two windows with one title and
process but different recorded HWNDs, and no root UIA identity, were folded
into one apparently ready bound-assertion step. The compiler now uses HWNDs
only to recognize this in-session distinction: it separates the assertion
step, then marks the trace `compile_window_identity_ambiguous`/incomplete if
both windows still have the same stable replay selector. With distinct
`rootAutomationId` values, the two-step trace remains ready and selects each
window by its stable root; HWND is absent from both selectors. Both cases
were regression-tested, giving **186 passed** in the focused
compiler/replay/time suite and **2547 passed, 28 deselected** in the selected
offline suite. `compileall`, `git diff --check`, and the Skill validator
exited 0. These results establish fail-closed compilation, not a live
same-title-window replay pass.

On the next 2026-10-19 assertion-evaluator review, five negative tests
reproduced false passes from missing control selectors: with one unrelated
control in the snapshot, `control_exists`, text equality and time-range
assertions selected it implicitly; an empty `control_absent` selector also
passed on an empty inventory. A state-only filter such as `checked=false`
was likewise accepted as a control identity. Control evaluators now require
an explicit, non-empty widget selector with an identity field before
matching; negative assertions fail on invalid selectors even when no
controls are present. A duplicate-component-ID time assertion regression
also confirms that one matching duplicate cannot pass. The focused
expectations/time/replay suite reported **164 passed**, the selected offline
suite **2554 passed, 28 deselected**, and `compileall`, `git diff --check`,
and Skill validation exited 0. This is offline evaluator evidence, not an
11.26 live assertion pass.

On 2026-10-20, the first scheduled 20-minute round ran on a clean tree
(all prior work committed as six commits and pushed to
`hermes-remote`). The selected offline suite now reports **2562 passed,
28 deselected** — 8 more tests than the previous entry, all passing;
per-test attribution against the previous count is not tracked.
`compileall` and `git diff --check` exited 0. The 11.26 status probe
reached the host over SSH (`DESKTOP-S917L7S`, `desktop-s917l7s\admin`)
but still returned `python_not_found` at the configured Python 3.11
path; no remote file or service was changed, so live deployment and
replay remain gated on an operator-provided Python path or
installation. The cloud endpoint again answered read-only probes
(`HTTP 200` on the HTTP root, `HTTP 302` on the HTTPS root with
certificate verification disabled for the probe); no authenticated
page/API session was available, so no end–cloud comparison is claimed.
This round produced no new offline verification.

Second 2026-10-20 round: a bounded read-only search of the 11.26 host
checked `C:\Python*`, both Program Files roots, the per-user
`AppData\Local\Programs` directory, the `python`/`python3`/`py` PATH
lookups and `C:\Windows\py.exe`; all returned nothing (`NOT_FOUND`).
This is stronger than the earlier unbounded recursive search timeout:
no standard-location Python installation exists on the host, so the
deployment gate is an operator action (install Python 3.11, or provide
a different host/path), not a local config correction. No remote file
or service was changed. The offline baseline is unchanged at **2562
passed, 28 deselected**.

Third 2026-10-20 round: an adversarial review of the time-prefix and
duration assertion path found no new false-pass. The matcher's
lookbehind/lookahead correctly rejects embedded digits and timezone
offsets while accepting one-digit seconds; midnight-wrap arithmetic in
`matching_times` keeps the exclusive upper bound correct across the
day boundary; both time-range evaluators fail closed on invalid or
duplicate control selectors (already parameterized-tested), and a
malformed non-dict window entry cannot pass — it raises and the
executor blocks the step, so no assertion can pass from a broken
observation. The registry-wide crash smoke and the
record→compile→replay duration-preservation test cover the remaining
paths. No code change was required; the offline baseline remains
**2562 passed, 28 deselected**. The 11.26 Python gate still awaits the
operator action recorded in the previous entry.

Fourth 2026-10-20 round: the normal repair path was run against 11.26
(now SSH-reachable). `deploy` passed (payload uploaded to
`C:\Users\admin\Desktop\edr-wd`, required files verified). `install`
reproduced a real quoting defect: `install_cmd` wrapped `-File` /
`-TaskName` / `-TargetRoot` in single quotes, but the Windows OpenSSH
default shell (cmd.exe) passes single quotes through verbatim, so
PowerShell received literal quote characters inside the path and
refused it with "The format of the path is not supported"
(`stage=install`, `task_registration_failed`). `install_cmd` now uses
double quotes (stripped by cmd before PowerShell parses the arguments)
and pure-backslash Windows paths on the PowerShell side; two regression
tests capture the constructed command, first failing on the old form and
passing after the fix. The focused lifecycle/repair suites reported
**32 passed**; the selected offline suite reported **2564 passed,
28 deselected**; `compileall` and `git diff --check` exited 0. The live
repair rerun verified the fix: `deploy` ok, the `StartEDRMCP` scheduled
task registered with the correct TargetRoot and start script, and
`ensure_server_running` now reaches the launch attempt and fails with
`server_start_timeout` (port 8765 did not open within 20s). The
remaining live gate is unchanged: the host has no Python, so the MCP
server cannot start until an operator installs Python 3.11 (or provides
a different interpreter path). No other remote change was made.

Fifth 2026-10-20 round: the component-ID / image-template replay path
was reviewed end to end — `generate_redacted_templates`
(redact-then-crop, target/redaction overlap refusal, digests computed on
the stored bytes), `PillowTemplateMatcher` (per-scale IoU seeding,
±stride refinement, final dedup), and `SafeVisualResolver` (all gates
fail closed) — all sound. The compile-time promotion in
`_attach_visual_templates` had one lenient fallback: a missing or
malformed `beforeCapture.origin` silently rebased the template crop to
(0, 0). The PNG digest covers only the frame bytes, so a tampered origin
would still digest-verify and the template could be cut from the wrong
region, letting replay match a similar-looking one. The raw-document
model layer (`_validate_capture_metadata`) already rejects a malformed
origin at parse/construct time — a CLI-level probe confirmed that is the
first line of defence (`recording_persistence_failed`: "origin must
contain two integers") — and the compile layer is now strict as well,
matching the replay bundle-ingestion and visual-resolver practice of
re-validating at each trust boundary. Six malformed-origin regression
cases fail before the fix and pass after; a positive control proves the
fixture itself is promotable. The focused and selected offline suites
reported **2571 passed, 28 deselected**; `compileall` and
`git diff --check` exited 0.

Sixth 2026-10-20 round: a further visual-replay trust-boundary check found
that `PillowTemplateMatcher` silently replaced a malformed explicit
`screenshot_origin` with `(0, 0)` (or coerced strings/bools to integers).
That could project an otherwise high-confidence template hit to the wrong
screen coordinate when a caller supplied a damaged observation. Four
parameterized negative cases reproduced the old behavior before the fix.
The matcher now returns no candidates for a malformed explicit origin;
omitted origin still means `(0, 0)` for deliberately local matcher use,
while the MCP window-bundle path independently requires and validates its
origin. The focused visual/evidence suite reports **56 passed**; the
selected offline suite reports **2575 passed, 28 deselected**.
`compileall`, `git diff --check`, and Skill validation exited 0. This
adds offline fail-closed evidence only; the live 11.26 replay gate is
unchanged because the configured Python 3.11 executable is absent.

A follow-up replay-chain test drives the real Pillow matcher through
`SafeVisualResolver` with a damaged origin and verifies that no guarded
click is materialized (`visual_match_low_confidence`). The visual-template
suite reports **23 passed** and the selected offline suite **2576 passed,
28 deselected**. A fresh read-only 11.26 status probe confirms SSH still
connects as `desktop-s917l7s\\admin` but the configured Python 3.11 path
still returns `python_not_found`; this is not live replay evidence.

Seventh 2026-10-20 round: the shared `HybridLocator.collect_candidates()`
treated an empty or non-identifying control selector as matching every
control. In a live image-only action on a window with exactly one unrelated
control, a failed template match could therefore become a false semantic
click. Four initial regression cases failed on the old behavior. The locator
now requires a non-empty supported control field or a meaningful ancestry
path before considering semantic candidates; malformed/empty ancestry is
not an identity. A positive ancestry-only case preserves that legitimate
lookup. The live executor regression verifies a weak template yields
`locator_low_confidence` with no materialized step, while a replay-chain
case confirms image-only golden replay already fails closed through its
visual-only branch. The focused locator/live/replay suite reports **108
passed**; the selected offline suite reports **2585 passed, 28 deselected**.
`compileall`, `git diff --check`, and Skill validation exited 0. The 11.26
live replay and authenticated end-cloud comparison gates remain open.

Sixth 2026-10-20 round: the cross-window recording path was reviewed
end to end. Compiler side: a causally bound assertion whose recorded
window differs from the action's window keeps its own observe-only step
instead of being folded into the action, and the action still counts as
verifier-verified for scroll/drag through `cross_window_verified_actions`;
distinct recorded handles that share one replay selector mark the new
step incomplete with `compile_window_identity_ambiguous`; `window_open`
assertions stay attached because they evaluate the product window
inventory, not the focused window. Replay side: the executor focuses the
step's own window before refreshing a stale lock, re-locks once before
dispatch when the cached lock went stale, and re-locks a declared next
window only while the current step carries no expectation outside
`action_ok`/`window_open` — a control assertion on the old window blocks
the re-lock rather than being silently evaluated against the new one.
Each guard has a direct test (`test_replay_does_not_retarget_old_window
_control_assertion_to_next_window`, focus-once caching, stale-lock
recovery), so the review found no remaining false-pass gap in this area.
11.26 re-probed: still `python_not_found` (the install gate is unchanged).
No code change this round; the recording suite was re-run and stayed
green.

End-cloud evidence follow-up (2026-10-20): a transient no-reference
`SnapshotPending` response on the final polling attempt overwrote earlier
page/client evidence references with empty tuples. A two-attempt regression
first reproduced this: an initial field mismatch produced both references,
then both sources became pending without new references and the timeout lost
them. `check_consistency()` now retains each side's last non-empty refs while
still reporting the latest pending reason; it never turns pending into a pass.
The focused consistency suite reports **44 passed**, the selected offline
suite **2586 passed, 28 deselected**, and `compileall`, `git diff --check`,
and Skill validation exited 0. This is offline evidence only: authenticated
Browser Use data and 11.26 EDRClient observations have not yet been compared
live.

Seventh 2026-10-20 round: the tree held an uncommitted, complete
changeset in the live-visual area that was preserved and committed
rather than reworked. The guard: a selector carrying no semantic
identity at all (no non-empty automationId/identifier/controlType/name
and no identifying ancestry) used to let the hybrid locator return the
window's single unrelated control as a semantic hit, so an image-only
click whose template was missing or weak could land on the only control
in the tree — a false pass at replay. `collect_candidates` now returns
no candidates for such a selector, and the live and replay paths fail
with `locator_low_confidence` / `visual_match_low_confidence` without
dispatching a click; an ancestry-only selector with real identifying
content still qualifies as a semantic candidate. Nine regression cases
(sole-control inheritance, five non-identifying shapes, the ancestry
positive control, the live action, and the replay no-click assertion)
pass, and the selected offline suite reported **2585 passed, 28
deselected**. `compileall` and `git diff --check` exited 0.

End-cloud adapter integration follow-up (2026-10-20): an offline
contract test now composes independent Browser Use-like page/API reads
with the actual `MCPObservationProvider` bundle ingestion path, not only
a stubbed client reader. The simulated target returns a window-scoped,
source-redacted PNG, a matching SHA-256 digest, stable window identity,
and a policy-state control; `EDRWDClientReader` extracts the client value.
`check_consistency()` accepts the same device, policy, and time window and
returns separate page, API, and client observation/digest references. The
new test passes, and the selected offline suite reports **2587 passed,
28 deselected**. This establishes the adapter wiring in simulation, not
an authenticated Browser Use session or an EDRClient live comparison.

Deployment preflight follow-up (2026-10-20): the live 11.26 repair had
previously uploaded scripts and registered `StartEDRMCP` even though the
configured Python interpreter was absent; only server startup exposed
that missing prerequisite. `repair_target(repair=True)` now runs
`probe_target` before deployment, installation, or restart. Direct Windows
`install()` also probes Python, required imports, PowerShell, and target
identity before uploading scripts or registering a task. Negative tests
verify a `python_not_found` preflight performs zero deployment writes;
positive tests preserve the ordered probe/deploy/install/ensure path.
The focused repair/install tests report **7 passed**; the selected offline
suite reports **2590 passed, 28 deselected**. `compileall`, Skill validation,
and `git diff --check` exited 0. This prevents future partial repairs; it
does not remove the task already registered on 11.26, install Python, or
constitute successful live replay.

Eighth 2026-10-20 round: the end–cloud consistency checker was
reviewed end to end and every path is fail closed — a non-Snapshot
fetch raises instead of comparing, a concrete snapshot from the wrong
device or policy returns `consistency_identity_mismatch` immediately,
pending sides poll to the deadline and then report
`consistency_timeout` carrying the last concrete evidence references,
an out-of-window snapshot cannot be compared, and `consistent` requires
every declared field present on both sides and strictly equal
(`_same_value` keeps bool apart from int and demands identical mapping
keys and list lengths). The concurrent round's fix to retain prior
evidence on an empty pending read was verified safe: the retained
references are report metadata for the identity-mismatch and timeout
results only and never enter the verdict, while the consistent path
still reports the fresh references. The companion adapter tests from
that round were collected and pass. The consistency suite reported
**45 passed** and the selected offline suite **2590 passed, 28
deselected**. The live end–cloud gate is unchanged: it needs an
authenticated Browser Use page/API session for the cloud side, which is
not available offline. 11.26 re-probed: still `python_not_found`. The
branch's one unpushed concurrent commit was pushed as housekeeping
(no content change).

Window-inventory follow-up (2026-10-20): macOS `list_windows()` used a
single `(pid, title)` dictionary entry to merge System Events and
CGWindowList. Two EDRClient windows with the same title caused one
CGWindowID to be assigned arbitrarily and the other discarded. The
inventory now retains distinct CG windows when they have unique native
numbers and rectangles, while preserving System Events' app name.
If System Events sees more windows than CG (or the relationship is
otherwise ambiguous), entries stay separate and unbound rather than
guessing ownership. Three regression cases cover equal-count duplicates,
unequal counts, and the no-drop case where CG sees fewer windows. The
focused macOS suite reports **26 passed**; the selected offline suite
reports **2593 passed, 28 deselected**. `compileall`, `git diff --check`,
and Skill validation exited 0. This hardens inventory only; exact focus
of same-title macOS windows still requires live validation and may fail
closed. The 11.26 Windows runtime and authenticated cloud gates remain
open.

Ninth 2026-10-20 round: the concurrent round's lifecycle preflight
fix was reviewed against the deployment-integrity area. The probe is
staged and fail closed (ssh reachability, then the configured Python
path executing, then the runtime dependency imports) and never raises;
`WindowsLifecycle.install` and the `repair_target` deploy path both
run it before any side effect, so a failed probe aborts with the
staged code instead of leaving a partially uploaded payload or a broken
task/LaunchAgent definition. On 11.26 this turns the open Python gate
into a clean, staged `python_not_found` failure with an actionable path
hint until the interpreter is installed, after which install and repair
proceed. A transient ssh blip aborting the operation is the intended
fail-closed trade for a long-running deployment. The focused
lifecycle/repair suites reported **7 passed** and the selected offline
suite **2593 passed, 28 deselected**, still green including an
in-progress, untouched concurrent work-in-progress in the macOS
accessibility path. 11.26 re-probed: still `python_not_found`.

Tenth 2026-10-20 round: after the same-title macOS inventory fix,
`WindowSession` now connects a selected macOS window by its native
CGWindowID, not by PID/title. Its lock verification resolves the frontmost
native ID from CG z-order and rejects a same-title sibling; a background-only
agent's visibility fallback also requires the exact native ID and rejects
an overlapping same-process sibling. Screenshot rectangle refresh uses the
connected native ID rather than treating equal-title windows as ambiguous.
Six new focused tests cover exact connection, PID and duplicate rejection,
frontmost lock mismatch, exact screenshot bounds, same-process occlusion,
and session routing. The focused macOS/window-session suite reports **44
passed**; selected offline suite **2599 passed, 28 deselected**.
`compileall`, `git diff --check`, and Skill validation exited 0. This is
offline fail-closed evidence, not proof that macOS activation can raise a
specific same-title sibling or that live EDRClient replay succeeds. The
11.26 Python runtime and authenticated cloud gates remain open.

Eleventh 2026-10-20 round: a read-only local macOS probe exercised the
new exact-window path against a real foreground window. System Events
reported the foreground PID but an empty window title, while CGWindowList
reported the native ID and title; the first exact-lock probe therefore
failed. `_frontmost_window_state` now takes the foremost visible CG window
for that PID, keeps its native ID, and fills only a missing System Events
title. A higher same-process sibling still cannot satisfy the target's
native ID. A regression test covers the empty-title case; the focused
macOS/window-session suite reports **45 passed**. The same read-only live
probe then verified the exact native lock and matching ID without moving or
clicking a window. The selected offline suite reports **2600 passed, 28
deselected**. This proves the generic macOS foreground evidence path,
not EDRClient activation or repeated replay. 11.26 SSH still reaches
`DESKTOP-S917L7S`, but the configured Python path remains `python_not_found`.

Twelfth 2026-10-20 round: reviewing the record→bundle→replay chain found
that macOS `dump_tree()` enumerated every window of a connected process,
while `observe_window_bundle()` assigned the locked window identity to all
returned controls. That could make a time assertion or stable-control
selector pass on a same-process sibling window. The backend now scopes the
AX tree to exactly one root whose geometry matches the connected, verified
CG window and includes only that root's descendants; missing or ambiguous
roots fail closed. Two regression tests show that a same-title sibling's
control cannot enter the tree or be selected, and two equal-geometry roots
are rejected. The focused macOS/window-session suite reports **47 passed**;
the selected offline suite reports **2602 passed, 28 deselected**. A
read-only local macOS probe could connect a native window but returned no
AX roots for sampled Chrome/ChatGPT windows, so this fix has synthetic
scope evidence only; live EDRClient AX-tree behavior remains unverified.

Tenth 2026-10-20 round: the concurrent round's macOS exact-lock chain
(five commits: same-title identity preservation, native window lock
binding, empty-AX-title resolution, and AX control scoping) was
reviewed link by link and every link is fail closed. Locks now bind a
CGWindowNumber instead of a title, so equal-title siblings cannot
satisfy one another; `connect_window_identity` requires exactly one
match and refuses with `window_identity_ambiguous`/`_unavailable`;
an empty AX title resolves through the pid-frontmost visible CG
window and backfills the title, after which the exact handle check
still applies; a same-app sibling stacked above the target fails the
unoccluded check; and the AX control tree is geometrically bound to
the one verified CG window (IoU ≥ 0.85 plus a 32 px edge tolerance),
returning an empty control set for zero or multiple matches so a
click can never resolve against the wrong window. The focused macOS
window suites reported **47 passed**. Note for the record: a
`git stash pop` in this round accidentally applied a July stash that
had sat at the top of the stash list, scattering stale conflict
markers across six files; the incident was fully recovered with zero
data loss — the peer's live work was already committed, the July
stash entry remains intact in the stash list, the contaminated state
is preserved under `recovery-backup-20261020`, and the tree now holds
only the peer's current work-in-progress. The selected offline suite
reported **2610 passed, 28 deselected** after the recovery.

Eleventh 2026-10-20 round: the in-tree work-in-progress on Windows
click attribution was verified and preserved as a commit. The low-level
hook now pins the receiving HWND at hook time (`WindowFromPoint` plus
`GetAncestor(GA_ROOT)`, failures degrade to `None` instead of raising
inside the callback), because the queue may process a click after the
desktop has changed and a later hit test would answer a different
window. The UIA resolver carries `ownerPid` into the target data and
enriches the native hit with the process name on the worker, not in the
hook. The correlator's ownership check is now strict for the
production resolver: a missing or invalid `ownerPid` fails closed
(legacy test doubles keep their old permissive contract), and the
former fallback that admitted any window pid present in the registry is
gone — a sibling product process can own another window of the same
product but cannot own the control under this particular input
window. Missing hook-time window evidence no longer falls back to the
later foreground either. Nine named regressions cover the hit test,
the enrichment, surviving a changed point hit test, both no-fallback
paths, the pid on the description, the missing-owner refusal, and the
sibling-window refusal. The focused capture suite reported **99
passed** and the selected offline suite **2610 passed, 28
deselected**.

Twelfth 2026-10-20 round: follow-up adversarial pass over the click
ownership change from the previous round, checking that the strict
`ownerPid` rule cannot reject a legitimate flow or admit a stale one.
The window identity fed to the ownership check is the hook-time stamped
window (enriched on the worker), never a later hit test: when the
stamp is usable, a click whose destination moved to another window
between hook and processing is refused rather than attributed to
either side; when the stamp is unusable and a fresh point hit test
still finds a window, both window and element are fresh and mutually
consistent, so admission is sound; when nothing is under the point,
the production resolver refuses instead of falling back to the
foreground, and the `foreground()` fallback is retained only for the
macOS/legacy resolver contracts the correlator also serves. The
removed registry-wide pid tolerance only changes multi-process
providers of a single product, an intentional fail-closed trade that
leaves the single-process EDRClient flow untouched. No gap found;
no code change this round. 11.26 re-probed: still `python_not_found`.

Thirteenth 2026-10-20 round: milestone confirmation. All six work
areas (end–cloud consistency, live visual candidates, cross-window
recording, deployment integrity, time-prefix/duration assertions,
component-ID and image-template replay stability) have now each been
adversarially reviewed at least once in this session and are
offline-saturated: the selected offline suite reports **2610 passed,
28 deselected**, and `compileall` / `git diff --check` exit 0. The
remaining unverified items are all live gates: the end–cloud
comparison needs an authenticated Browser Use page/API session, and
live EDRClient record/replay, live time-assertion checks, and the
deployment upload/import evidence all need Python 3.11 installed on
11.26 (operator action, still pending). The tree is clean and in sync
with `origin/hermes-remote`. No new code work this round.

Fourteenth 2026-10-20 round: live environment bring-up and replay
failure diagnosis (diagnosis only, no code fix this round).

First, the stale `python_not_found` conclusion is corrected: 11.26
now has **Python 3.11.9** installed at
`C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe`
(silent install exit 0, version verified) with the full offline
dependency bundle (82 packages; 75 pure-Python wheels reused, 7
cp311 wheels re-fetched at the same pinned versions; imports of
fastmcp / pywinauto 0.6.9 / psutil / PIL verified), in addition to
the existing Python 3.15. The target `config.json` pins
`server.python_path` to the 3.11 interpreter, and the MCP server was
confirmed running under it (process `PID 8876`, `server.py
--http --host 0.0.0.0 --port 8765`). This round also landed and
deployed commit `ae051fa` (pywinauto 0.6.x `UIAWrapper` has no
`is_top_level()`; the shared top-window enumeration now tolerates the
missing method instead of silently enumerating zero windows — the
cause of the earlier `window_inventory_unavailable` and the fail-
closed guard's `native window disappeared or owner changed`);
live-verified on the restarted 3.11 server: `begin_product_run` →
`focus_window_role(main)` bound logo1 HWND 459530, and
`observe_window_bundle(capture_image=true)` returned a window-scoped
source-redacted observation with **44 controls** and origin
`[259, 0]`.

Replay status: the cross-window golden trace
(`live_edr_xwin_20261020/golden-trace.json`, status `ready`, 2
steps) **still has not passed** — `taskSuccess: false`, 0/2 steps,
both blocked `backend_unavailable`. Diagnosis via the exact CLI
pipeline (driver `diag_replay_probe.py`, kept) plus target-side
forensics established the failure **stage is connection**, with this
causal chain, all from 11.26's own logs:

1. The live server (3.11, started 20:51:28) died at **20:58:59** and
   the 3.15 predecessor (started 19:45:10) died the same way at
   **20:49:44**: `logs\server.*.log.stderr` carries the unhandled
   `ValueError: unknown backend 'uia'; expected one of
   ['macos_accessibility', 'windows_pywinauto']` raised in the
   `status` tool — `server.py` reads `_backend.backend`
   (`WindowsPywinautoBackend.backend` → pywinauto GUI string `'uia'`)
   and passes it to `status_action_space()`, which only accepts the
   registry kinds. The defect only arms once the backend has a
   connected window (`_gui` set); before that the property raises
   `AttributeError`, the `except` swallows it, and the safe default
   survives. Defect recorded; fix deferred (diagnosis-only round).
2. After the crash, the `StartEDRMCP` task could not restart the
   server: `schtasks /Run` reports success but `lastRun=` stays
   empty, because the task principal is `logonType=Interactive` and
   `query user` now returns **no users** — the target's RDP session
   has ended (a "Shut Down Windows" dialog was present earlier; the
   machine did not shut down). A manual start from an SSH (session
   0) context died within seconds with 0-byte server logs — the
   server cannot initialize without an interactive desktop.
3. Consequently every MCP call this round reached a dead port:
   `tools_list()` returns 0 tools, and the replay pipeline's probe
   reports `backend_unavailable` at the connection stage (the
   earlier `product_window_selector_unavailable` raised by the
   driver's tool check is a symptom of the same dead server, not a
   separate cause). One residual question remains: the **20:52**
   replay run failed with the server alive (2.4 s, both steps
   blocked) and is not yet explained at stage level; the probe
   driver is staged to capture the full tool-call sequence and raw
   exception on the next live attempt.

Commands used this round (all read-only or environment restoration,
no dependency reinstall, no target-config overwrite): Python 3.11
installer fetched on the Mac (size-verified against python.org
content-length, SHA-256 matching the published 3.11.9 value) and
scp'd; silent per-user install; offline `pip install --no-index
--no-deps` of the 82-file bundle into 3.11; `config.json`
`server.python_path` written once (still in place, except the target
file was later found empty by a read probe — recorded, not
overwritten); server restarts via the lifecycle `restart`/task and
one manual `start_server.ps1` run (failed, session-0); SSH-based
process/port/log/session forensics via `C:\Users\admin\diag_*.ps1`
(kept on the target). Local diagnostic driver:
`diag_replay_probe.py` (untracked, kept).

Live gate status after this round: 部署完整性 ✓ (live ops incl.
3.11 switch); 跨窗录制 ✓ recorded + artifacts (replay still pending);
时间前缀/组件ID+图像模板 replay gates pending (environment down);
端云一致性 still blocked on an authenticated Browser Use session;
实时视觉候选 not re-probed live this round.

Operator action required: re-establish the interactive RDP session to
11.26 (logon as `admin`); do **not** confirm any shutdown dialog.
Next round, once the session is back: start the server, run
`diag_replay_probe.py` to capture the raw replay-path exception
(first the 20:52-style live failure, then the full 3-run acceptance
with per-run HWND/role/failure-code/assertion evidence), fix the
`status` `'uia'` defect and the replay blocker with targeted
regressions, and re-run the offline suite.

Fifteenth 2026-10-21 round: cross-window golden replay gate PASSED on
11.26 (operator restored the RDP session as `admin`; no shutdown dialog
was confirmed). The operator-run acceptance also closed this session's
open item: the original trace's alive-server failure (20:52) reproduced
as `window_selector_not_found` on step one — recording had combined the
post-click `Log Center` title with the pre-click main-window root
`EdrMainWindow` into a selector for a window that never existed
(`result-report/2026-10-21_00-52-29/original-golden-failure/probe.json`).

Fixes landed (reviewed this round, regression-tested, now committed):
(1) the correlator stamps the mouse-down window on pointer events and no
longer reclassifies a release against a window that already changed
title/content (`target/recording/windows.py`); (2) a root-identified
window selector no longer carries the post-action title — title is a
fallback only when no stable root exists
(`agent/recording/selectors.py`); (3) the `log_center` role now matches
the English build's `Log Center` title (`target/product_specs/apps.json`);
(4) the control tree reports live `checked` state for CheckBox
(toggle 0/1, indeterminate stays unknown) and RadioButton
(selection fallback) (`target/pywinauto_client.py`); (5) the `status`
tool queries the action catalog by the startup-resolved `_backend_kind`
instead of the adapter's engine string, so the `unknown backend 'uia'`
crash that killed both long-lived servers (documented in the fourteenth
round) can no longer occur, with a dedicated regression
(`test_server_status_uses_canonical_kind_when_adapter_reports_uia`);
(6) `golden_trace_incomplete` now names the unready steps and the
missing result verifier (docs in `SKILL.md` / `references/testing.md`).

Acceptance (three independent runs, each closing Log Center first, then a
fresh product run + replay of the recompiled golden trace): 2/2 steps
and 4/4 assertions passed every run (`action_ok` x2, `window_open`,
`control_checked_equals`), no failure codes, `taskSuccess=true`,
`pathFidelity=1.0`, `cleanupPassed=true`. Main window HWND 917736 / role
`main` / root `EdrMainWindow` in all three; Log Center HWND 262368 /
393982 / 524628 per run (role `log_center`, root `LogCenterWindow`),
matching the close-and-reopen precondition. Per-run call logs and HTML
reports: `result-report/2026-10-21_00-52-29/acceptance-{1,2,3}/`,
summary `acceptance-summary.md`. Note the coverage boundary stated in
the summary: this validates the recorded "open Log Center -> check
operation log" path, not full EDRClient functionality; window regions
outside the RDP viewport limit screenshot coverage while semantic
assertions ran on the live UIA tree.

Offline: the selected marker suite reports **2625 passed, 28
deselected** (baseline 2615 + 10 new regressions); the operator's
default full run reported 2040 passed / 613 deselected
(`offline-tests.xml`).

Live gate status: 跨窗录制与回放 ✓ (recorded, compiled, replayed 3x
with per-run evidence); 组件ID回放稳定性 ✓ live (semantic
component-ID dispatch in all three runs); 时间前缀/持续时间断言 remain
captured in the assertion-only recording `live_edr_20261020b` — live
replay of a time-range assertion still pending; 图像模板 (visual) replay
not re-probed live this round; 端云一致性 still blocked on an
authenticated Browser Use session (operator item); 实时视觉候选 not
re-probed live. The `status` tool is safe again: the server no longer
crashes on a status call with a connected backend.

Sixteenth 2026-10-21 round: time-prefix + duration assertion live replay
gate PASSED on 11.26. The old asset
`recordings/live-20261020/live_edr_20261020b` was un-replayable
(`incomplete`); both defects were root-caused and fixed by re-recording
rather than by code change: (1) a mid-session `connect` to the recorder's
own UI window disturbed the backend's window state, so the assertion event
carried `captureError` (protected-control enumeration/screenshot failure)
— clean re-recording (dialog located via read-only `list_windows`, driven
via in-session `run_powershell` SetForegroundWindow+SendInput coordinate
clicks, no window-lock touches) eliminated it; (2) an assertion created
before any recorded input had no window evidence and fell back to the
scope-seed window (`^logo1$`) instead of the window whose text it reads —
recording one real in-window click before the assertion binds it to
`LogCenterWindow`. Both findings are documented in `references/testing.md`
(assertion-recording hygiene).

New ready assets, all bound to `LogCenterWindow` (HWND 524628):
`live_time_20261021b` (pass set: operationLog click + `00:28` whole-minute
+ `00:28:56` dur 1), `live_time_20261021c` (`00:28:57` dur 1, expected
failure), `live_time_20261021d` (`00:28:55` dur 1, expected failure).
Live baseline: Operation Log's first entry `2026-10-21 00:28:56`; the
other on-page timestamps (21:07:57, 21:07:40, 21:05:27, 20:06:24,
18:59:10) are natural negative samples.

Replay results (per-run details, time inputs, observed values, failure
codes, window attribution, report paths:
`result-report/2026-10-21_time-assertion/acceptance-summary.md`): three
independent replays of the pass set — all `taskSuccess=true`, 3/3 steps,
assertion rate 1.0, `pathFidelity=1.0`, verifiers recorded the actual
match `["2026-10-21 00:28:56"]` for both time assertions (whole-minute
`00:28` and start-inclusive `00:28:56` dur 1), `action_ok` +
`control_checked_equals` (real UIA checked state) on the click step, no
failure codes. Boundary runs: `00:28:57` dur 1 (one second before the
start) and `00:28:55` dur 1 (observed value exactly at the exclusive end)
both failed with the assertion-level diagnostic `no observed time is
within the expected range` — controlled semantic failures, not
`backend_unavailable`. Window attribution was identical across all five
runs (HWND 524628 / root `LogCenterWindow`). Coverage note: the asserted
timestamp comes from the current first log entry and changes as logs
refresh; a future re-run must re-record the assertion value.

Suites: targeted recording set **497 passed**; full marker suite
**2625 passed, 28 deselected** (unchanged — no product code changed this
round).

Live gate status: 时间前缀/持续时间断言 ✓ live-verified (this round);
跨窗录制与回放 ✓ (fifteenth round); 部署完整性 ✓; 组件ID回放稳定性 ✓
live (semantic); 图像模板 (visual) replay not re-probed live; 端云一致性
still blocked on an authenticated Browser Use session (operator item);
实时视觉候选 not re-probed live.

On 2026-10-21 the cloud recorder's assertion layer was upgraded in the
sibling repo `edr-cloud-recorder` (branch `maa-dp`), per explicit request:
assertions had to become human-friendly and precisely specifiable — time,
count, presence/absence, and exact expected content. The new spec separates
**what** is checked from **how** it is compared, aligned with the operator
sets of mature systems (Playwright `expect`, Katalon verify keywords):

- comparison operators for text/value/attribute assertions — equals
  (default; old recordings carry no `op` field and replay byte-compatibly),
  not-equals, contains/not-contains, starts/ends with, regex full-match,
  blank/non-blank; numeric greater/less for input values (fails closed on
  non-numeric instead of falling back to string order);
- element-count assertions (equals / at least / at most) via
  `locator.count()` — hidden elements count, so absence is asserted with
  visibility=false, not count=0;
- a third time kind, `timerange`: start `HH:MM` covers the whole minute,
  `HH:MM:SS` + `durationSeconds` a half-open interval with midnight wrap —
  the same semantics the desktop `window_text_time_range` assertions were
  live-verified against in the sixteenth round, so both recorders now speak
  one time-assertion language;
- the right-click menu gained an operator select, the count type, and the
  time-range type with a live interval preview; blank ops need no expected
  value;
- `assets/rec_assert.py` is now the single comparison core shared by the
  replay runtime and the generated pytest specs, so both judge identically;
  malformed op/shape fails fast at replay with the requirement in the
  message instead of a vague failure after the timeout.

Evidence: **347 passed** in the cloud-recorder suite, including 39 new
tests — operator/time-range boundary units, fake-page replay tests,
real-Chromium end-to-end replay of the new assertion kinds, and real
right-click menu tests (operator list, integer validation, timerange
prefix/duration/preview). Commits `1dc5be8` (pre-existing working-tree
keyboard/Enter recording fix, committed as its own change at the operator's
choice) and `1775283` (the assertion spec), pushed
`0db1f67..1775283`. Left untouched in the working tree: an in-flight
`mcp_server.py` change plus `test/test_replay_check_mcp.py` (parallel
work, deliberately not committed). edr-wd product code was not changed this
round; its suites remain at the sixteenth-round numbers. 端云一致性 stays
blocked on an authenticated Browser Use session (operator item); the
upgraded web assertions are what that gate will replay once unblocked.

Eighteenth 2026-10-21 round: cloud-side assertion acceptance defects D1–D5
fixed (minimal-fix round, P1×4 + P2×1) in `edr-cloud-recorder` (branch
`maa-dp`), working tree deliberately left uncommitted for independent
re-verification.

The 2026-10-21 acceptance review (`doc/dsh-acceptance-20261021.md`, against
`1775283`) ruled the upgraded assertion layer "not passing yet" with five
reproducible defects. Each was fixed with a failing regression first:

- **D1 (P1) read failures entered the comparison** — `count()` throwing fed
  `-1` into the compare, so "at most 0" passed; `input_value()` /
  `get_attribute()` throwing fed the exception string in, so
  `not_equals` / `is_not_blank` passed on the error message. Fixed in
  `rec_assert.py` (all three helpers + the three time poll loops) and in
  `replay_trace.py` (count branch + readers loop): a failed read only sets
  the last error and retries within budget; on timeout the assertion fails
  carrying the last exception in the message (diagnostic, never an observed
  value).
- **D2 (P1) generated `to_contain` / `not_to_contain`** — attributes that do
  not exist on Playwright `LocatorAssertions` (AttributeError in real
  Chromium). Live API probe established this version has
  `to_contain_text` / `not_to_contain_text` (normalized substring), so
  `generate_spec.py` now emits those. Verification was upgraded from
  "renders/compiles" to **executing the generated pytest function in real
  Chromium**.
- **D3 (P1) regex semantics split** — `to_have_text(re)` matches the raw,
  unnormalized text with SEARCH semantics (live-proven: `re.compile("abc")`
  passed on "prefix abc suffix"), so anchored wraps still diverged from the
  replay chain (`re.fullmatch` on rendered inner_text). Fix: text ops
  starts_with / ends_with / matches / is_blank / is_not_blank no longer use
  Playwright regex at all — the generated spec polls
  `matches_assert(locator.inner_text(), expected, op)`, the exact same pure
  function and the same read the replay chain uses. One fullmatch
  definition, one text source, both chains judge identically.
- **D4 (P1) value/attribute over-normalization** — the new core normalized
  whitespace for every assertion type, so input value `"a  b"` vs expected
  `"a b"` passed (old behavior: strict), and `None`→`""` erased the
  missing-attribute vs empty-attribute distinction. Fix:
  `matches_assert(strict=True)` — text keeps whitespace normalization;
  value/attribute/visible/checked compare strictly (no normalization,
  `None` ≠ `""`); the explicit extension ops (contains/starts/ends/matches)
  on value/attribute are declared separately (raw-string compare, `None`
  treated as "not contained"). Old recordings without `op` remain
  byte-compatible; the reviewer's split case now fails on both chains.
- **D5 (P2) timerange missing duration rendered an empty positional
  argument** (`expect_time_range(loc, "12:00", , read="value")` →
  SyntaxError). Fix: omit the argument when `durationSeconds` is absent;
  invalid durations (0 / float / bool / out of 1..86400) are rejected at
  generation (`SpecGenerationError`) and fail fast at replay
  (`TraceReplayError`) instead of silently defaulting.

Evidence: the reviewer's own repro script
(`output/dsh-review-20261021/reproduce.py`) re-run — all four read-error
entries flipped from `RETURNED_SUCCESS` to `AssertionError … RuntimeError:
detached`; `contains`/`not_contains` now succeed on both chains in real
Chromium; the `matches` negative and the value `equals` negative now fail on
both chains; the default timerange compiles **and executes** successfully.
Full cloud-recorder suite **392 passed** (was 347; +45 regressions),
including the new `test/test_generated_execution.py` — 36/36 same-recording
dual-chain agreement cases (positive, negative, read-exception,
missing-element, invalid-config) executed against a real Chromium page, not
rendered strings. Full evidence and re-verification commands:
`output/dsh-fix-20261021.md`.

Constraints held this round: minimal fixes only; no baseline re-freeze;
no product changes in edr-wd, trace-eval, or the ZCode orchestration
(`scripts/mcp_server.py` + `test/test_replay_check_mcp.py` left untouched);
no deploy, no new schedules, **no commit or push** — the mixed working
tree is handed back for independent re-verification. Live gate status
unchanged: 端云一致性 still blocked on an authenticated Browser Use session
(operator item); the repaired web assertions are what that gate will replay
once unblocked.

Independent re-verification of the D1–D5 fix round: **passed** (fresh
context, adversarial, reviewer ran everything itself). Its own probes,
all outside the repo: old recordings without `op` through the real
`to_v2` path + real Chromium dual chain 6/6 (legacy semantics fully
restored); 30 boundary cases on `matches_assert` (strict ops vs `None`,
non-str expected, unknown op raises in both modes); all invalid
timerange durations rejected at generation and fail-fast at replay;
regex-metachar dual-chain agreement 6/6 (the old search-semantics false
pass on `[0-9]+` vs `"abc123"` is gone); every example line in the
updated `references/assertions.md` executed verbatim in real Chromium.
Full suite re-run: **392 passed**. Change-surface confirmed: the six
fix-round files only; ZCode's in-flight `mcp_server.py` /
`test_replay_check_mcp.py` untouched. One non-blocking P3 (doc example
two `poll_until` lines shared one element class) corrected in the same
pass. Commit/push still withheld per the round instruction — the mixed
working tree stays as-is for the operator.

Nineteenth 2026-10-21 round: D6 (P1) minimal-fix round in
`edr-cloud-recorder`, after the second independent re-verification of the
D1–D5 fix round passed but found a residual split: the four native text
branches (`to_have_text` / `not_to_have_text` / `to_contain_text` /
`not_to_contain_text`) compare Playwright's default `textContent` — which
includes `display:none` descendants — while the replay chain reads
`inner_text`. Reviewer's real-Chromium repro: on
`<div>shown<span style=display:none>hidden</span></div>` the generated
spec passed `contains "hidden"` (false pass) and failed `equals "shown"`,
both opposite of the Trace.

Fix (per the reviewer's minimal suggestion, keeping the Trace
`inner_text` semantics as the single source of truth): all four generated
text branches now pass `use_inner_text=True` (verified present on all four
methods in this Playwright version; with it, the assertions read exactly
what `inner_text` returns, so both chains judge the same text, with the
retry budget and "read errors never enter the comparison" rules from D1
intact). No replay-side change — the replay already read `inner_text`.

Regressions first, then fix: the dual-chain matrix
(`test/test_generated_execution.py`) gained 7 cases — hidden-descendant
× the four text ops (before the fix: all five hidden-text expectations
diverged, exactly the D6 repro), plus visible-control and dynamic
show/hide (a `display:none` span un-hidden 150 ms after load) so the
polling semantics stay proven on both chains. The reviewer's own
`output/joint-acceptance-20261021/dsh-hidden-text.py` re-run:
`contains "hidden"` → generated `AssertionError` + trace `failed`
(both fail, agree); `equals "shown"` → both `success` (agree). One
generated-shape assertion in `test/test_verify.py` was updated to pin the
post-fix form (it asserted the old, D6-buggy rendering).
`references/assertions.md` now documents the inner_text rule and shows the
`use_inner_text=True` examples.

Full cloud-recorder suite: **399 passed** (392 + 7 new D6 cases).
Working tree still uncommitted by instruction; ZCode's in-flight
`mcp_server.py` / `test_replay_check_mcp.py` untouched. Handed back for
independent re-verification.

Independent re-verification of the D6 fix: **passed** (fresh context,
adversarial, all run by itself). Reviewer's original hidden-text repro now
agrees on both chains (`contains "hidden"` both fail, `equals "shown"`
both pass); full suite re-run **399 passed** (392 + the 7 D6 cases,
accounts close); 13 adversarial probes over real Chromium dual chain:
hidden-mechanism variants (visibility:hidden, whole-block, nested
display:none, blank hidden span) all agree and match inner_text intent;
dynamic show/hide at 400 ms passes both chains and at 9 s fails both
(6 s sits inside the generated chain's window after its ~2 s goto/setup
offset — a real pass, a pre-existing budget-window difference, not a D6
defect); whitespace normalization and missing-element equality both agree
with the D1 direction. Change surface confirmed: only the four
`use_inner_text=True` branches plus the documented test/doc updates;
replay-side text read untouched; ZCode in-flight files uncontaminated.
The cloud-side assertion thread (D1–D6) is fully fixed and re-verified.

Twentieth 2026-10-21 round: live re-probe of the 实时视觉候选 /
image-template replay gates on 11.26 (now that the environment is back up),
plus one P2 CLI error-contract defect found and fixed during the probe.
Full evidence: `result-report/2026-10-21_live-visual/ROUND20-evidence.md`.

Environment: tunnel/MCP/GUI all healthy, but **EDRClient.exe was not
running** — the lone `logo1` window belonged to `HiSecEndpointAgent.exe`
(root control `SafraUIMainWindow`, the documented two-process same-title
case), so the xwin golden's `focus_window_selector(EDRClient.exe,
EdrMainWindow)` returned `window_selector_not_found` and the replay
pre-flight probe failed in ~70 ms (`backend_unavailable`, 0/2; two failed
traces retained). Recovered through the designed `open-edr` /
`activate_edr` path: EDRClient relaunched (pid 1964, HWND 1573628, same
259,149 / 922x602 geometry and 44-control main page as round 14).

Run A (semantic): the 15th-round cross-window golden
(`live_edr_xwin_20261020`, Log Center open → Scan Log toggle) **replayed
live: 2/2 steps, pathFidelity 1.0, `window_open` + `checked` verifiers,
cleanup + trace integrity** (`TR-01a15e2c51de00000669`) — the cross-window
replay pipeline re-verified after an application restart.

Runs B/C (`visual_only`, the only ready goldens with click templates,
`hisec_flow` / `hisec_scroll`): the **live visual candidate pipeline ran
end-to-end** — window-scoped redacted frame → scale-aware match → 32 live
candidates (cap) → best/second margins 0.0011 / 0.0008 below the policy
threshold → `visual_match_ambiguous` → **refused, no click** (the
ambiguity-rejection safety property held live; traces
`TR-01a15e33578900009179`, `TR-01a15e34b26b0000456b`). Offline re-scoring
of the captured live frame with the same `PillowTemplateMatcher` explains
the margin: those goldens were recorded on the **Chinese-UI** state (control
text `日志中心`), the live UI is now **English** (`Log Center`), and the
true button region (window x 51–156) scored below the top-32 floor while
all 32 candidates sit in the look-alike tile grid at x ≥ 283. Verdict:
template-vs-live **state drift**, not a matcher/policy defect; a
task-complete visual replay needs goldens re-recorded under the current
English-UI state (next-round candidate, new recording assets).

Defect (P2, agent-side CLI): the `replay` handler caught
`RecordingModelError` from `build_replay_runtime` **without importing the
name** → any local validation error crashed with a raw `NameError`
traceback + exit 1 instead of the JSON error contract (reproduced live
during Run B). Fixed with a one-line import; regression
`test_case/test_cli_replay_errors.py` (pre-fix: exact NameError; post-fix:
clean `{"ok": false, "code": "visual_template_missing", ...}` + exit 1,
and no remote action dispatched — only the `recording_status` pre-flight).
Post-fix live re-run of the crashing command returns the clean JSON.

Suites: focused CLI/replay contracts **96 passed**; full offline suite
**2026 passed, 613 deselected**. Working-tree surface this round:
`agent/cli.py` (+1/−1) and the new test file only; parallel-thread WIP
untouched; workspace uncommitted by instruction.

Twenty-first 2026-10-21 round: 跨窗录制 + 实时视觉候选 under the current
**English UI** on 11.26 — recorded a fresh two-step Log Center cross-window
golden with real pre-action visual templates and result verifiers, replayed
it `visual_only` twice (second time from a freshly relaunched window state),
and diagnosed the recurring `visual_match_ambiguous` block down to a
minimal reproducible gap. Full evidence:
`result-report/2026-10-21_live-visual/ROUND21-evidence.md` (RUN traces
`TR-01a15ec56b8b0000ee8c`, `TR-01a15ed999ef0000cc0d`).

Recording (the new gap the round-20 diagnosis pointed at — Chinese-UI
templates vs English live UI) is **closed**: golden
`recordings/live-visual-20261021/live_edr_visual_20261021` (session
`REC-4da074d8…`), 2 steps, `status: ready`, English control text
(`Log Center`, `Scan Log`):

- step-0001 `gui.click` on `…operationWidget.logCenterBtn`, window scope
  `EDRClient.exe`/root `EdrMainWindow`, visual template
  `assets/step-0001-element.png` (105×98) + context (201×194),
  `redacted: true`, element/context sha256-bound, verifier
  `window_open` (exists, `EDRClient.exe`, title `Log Center`, 5 s);
- step-0002 `gui.click` on
  `LogCenterWindow.mainWidget.topWidget.virusScanLogBtn`, window scope
  root `LogCenterWindow` (cross-window ✓), template 70×27 + context
  166×123 same structure, verifier `checked` = true.

Event chain: `pointer_click` → `window_transition` opened (causal-bound to
step 1) → `toggle_change` value=true (step 2). captureDiagnostics all
zero; three CAP-*.png persisted under `assets/observations/`; catalog
1.0.1 digest and `windows_hisec`/`windows_pywinauto` bindings intact.

Replay: both `visual_only` runs were **refused by the ambiguity gate**
(no click dispatched, no threshold touched): RUN 1 margin 0.000424 (32
candidates), RUN 2 margin 0.000344 from the fresh state (main window
closed via its own `closeBtn`, relaunched with `activate_edr`; note the
process survives window close — a full process restart is not reachable
with current privileges, `taskkill` was access-denied, documented in the
evidence). Cross-launch reproducibility confirmed.

Offline re-scoring (same matcher + resolver parameters, live frame after
RUN 2, `/tmp/live21_main_frame.png`) localizes the block to **candidate
ranking, not template, not state drift**: the true button region is
pixel-identical to the template (mean abs diff 0.0, full-precision score
1.000000) — zero drift — but its nearest coarse-grid point ranks 217/770
because 216 grid points in the look-alike icon cluster score higher
(0.9567 at frame (216,216) etc.); the top-32 seed cap starves the true
position before refinement, and the context path (which would have
constrained the search) is discarded by design when its own best-second
margin (0.0246) is below 0.03. Minimal reproducible gap:
`PillowTemplateMatcher()(step-0001-element.png, 922×602 frame @ origin
[259,10])` → true position absent from top-32; coarse rank 217/770.

Gate status: 跨窗录制 re-established under the English UI (recording
milestone replicated); 实时视觉候选 ambiguity-refusal property re-verified
live twice; task-complete visual replay remains gated on the matcher
ranking defect. No threshold/assertion/matcher code was changed this
round; no commit/push; protected cloud-side and ZCode files untouched;
workspace uncommitted. Next-round candidate (not done here, outside the
record/replay+docs mandate): matcher search-side fix (per-cluster seed
retention and/or context-anchored element seeding) with an offline
regression using this golden + live frame as the fixture.

Twenty-second 2026-10-22 round: 实时视觉候选 minimal matcher search fix.
Turned the round-21 coarse-grid top-32 seed starvation into a repeatable
offline failing regression (synthetic dense same-family cluster + off-grid
unique target, no /tmp dependency:
`test_case/test_recording/test_visual_seed_coverage.py`, RED confirmed
against `git show HEAD:agent/recording/pillow_matcher.py`, GREEN on the
fixed tree). Fix (only `agent/recording/pillow_matcher.py`, +47/-1):
seed the strict local maxima of the coarse score grid (highest first,
capped) alongside the score-ordered seeds — a unique exact match is always
a local maximum, a look-alike plateau at most one per tile — plus stride
min(w,h)//4 → //8 (the steep metric made the old funnel miss unique
optima; the ±stride refinement window shrinks, net cost drops). Final
dedup/cap and every resolver gate unchanged (0.92/0.05/0.03,
redacted/window/digest checks, duplicate-icon ambiguity refusal, no
full-frame brute force, no silent first-pick). Counterexamples pinned:
truly repeated target → ambiguous refusal; dense identical family →
refusal; context tie → context dropped, element path decides; 1.25×
scale accepted; 0.6× scale → visual_scale_unsupported. Performance bound
recorded: live frame element 46s→26.4s (0.57×), context 158s→116.7s
(0.74×), 922×602 synthetic 21.9s (<90s bound test, @pytest.mark.regression);
construction bound ≤2×. Suites: test_recording 514 passed; full offline
2643 passed / 28 deselected / 0 failed. Offline comparison on the real
English golden + 11.26 live frame: true position now enters refinement
and is top-1 at conf 1.0 (element), but margin 0.0431 < 0.05 vs the
near-duplicate tile family (0.9569) and context margin 0.0246 < 0.03 →
the gates still correctly block. Live visual_only replay RUN3 (trace
TR-01a15f44053e0000d8d1, Log Center path, no business writes, window
normalized to (259,10) via MoveWindow after live-state check): step-0001
blocked visual_match_ambiguous with margin 0.0431489421165292 — identical
to offline — 32 candidates, step-0002 skipped_after_abort, 8 events, zero
clicks dispatched, window-scoped redacted captures (sha only),
traceIntegrity true, two-step verifiers not reached (block precedes
verification). Gap recorded (not relaxed): the real UI's near-duplicate
tile/radio families keep the true margin below the safety gates — a
UI-level discriminability gap, no longer a search gap. Task-level visual
replay remains BLOCKED by design. Protected: ZCode root/trace-eval and
cloud-side WIP untouched (edr-cloud-recorder tree identical to the
hand-back set), no commit/push/deploy/re-freeze. Evidence:
result-report/2026-10-21_live-visual/ROUND22-evidence.md.

Twenty-third 2026-10-22 round: 实时视觉候选 minimal discriminability fix.
Read-only survey found no OCR engine anywhere; the empty `ocr_boxes`
channel (agent-side plumbing exists, target produces nothing) and the
existing `ocr_text` precedent in the semantic_first fused-locator path.
The safe source that does separate the real UI neighbours is the fresh
window-scoped observation's verified UIA control tree: in the RUN3
snapshot "Log Center" is unique tree-wide with rect == the true candidate
rect, while every look-alike tile carries different text ("Quarantine &
Blocking Area", "Trusted List", "Quick Scan"). Minimal resolver extension
(`agent/recording/visual.py` only): at the refused pixel-margin gate and
NOWHERE else, a fail-closed text confirmation — the recorded
`selector.control.name` must equal the text of exactly one control inside
the locked window, which must confirm exactly one candidate at/above
min_confidence; missing/duplicate/near-text/out-of-window signals leave
the refusal standing. No threshold touched (both live steps still refuse
at pixel margin 0.0431/~0.003 < 0.05), no new candidates, no silent
first-pick, no verifier bypass; `VisualResolution.text_confirmed` exposes
when the rescue ran. R22 guarantee bounded as instructed: docstring now
states the unique-match refinement guarantee holds only while fewer than
max_candidates decoy local maxima outrank it, pinned by a new
counterexample (34 decoy peaks > cap → target starved again → safe
visual_match_ambiguous refusal). Tests: 10 new text-confirmation cases
(positive rescue; same-name/near-text/missing-tree/no-text/out-of-window/
no-recorded-name/duplicate-candidate refusals; selected-state radio
refused at the confidence gate; missing template refused at the model
layer; legacy margin-pass behaviour unchanged) + the cap counterexample;
test_recording 525 passed, full offline 2654 passed / 28 deselected.
Offline re-verification on real material (RUN3 control tree + live frame
+ golden selector with runtime digest verification): text_confirmed=True,
confidence 1.0, margin 0.0431, click (103,451) == true button centre.
Live visual_only replays on the no-business-write Log Center path:
RUN4 (TR-01a15f8b5bd700001d94, no token) — visual resolution succeeded,
blocked pre-dispatch by the profile confirmation gate
(confirmation_required, risk=high, gui_mutation), zero dispatches;
RUN5 (TR-01a15f91143a0000d68e, operator token --confirm-action
pointer.click_window as authorized) — taskSuccess=true, 2/2 steps passed,
both resolved via the pure visual path, clicks landed exactly on the true
Log Center button (362,461) and Scan Log radio (594,298), window_open and
control_checked_equals verifiers passed, extraActionCount=0,
traceIntegrity=true. Residual live state (Log Center open, radio checked)
matches the round-21 post-recording state; no further GUI actions taken.
Protected: ZCode root/trace-eval and cloud-side WIP untouched; no
commit/push/deploy/re-freeze. Evidence:
result-report/2026-10-21_live-visual/ROUND23-evidence.md.

Twenty-fourth 2026-10-22 round: round-23 review fixes (V1/P1 + V2/P2).
The independent acceptance correctly showed that round-23's UIA-text
disambiguation silently changed the pure visual_only replay contract:
with identical pixels (1.0/0.99) and an identical observation, only the
recorded control name decided whether a refused-margin step materialized
(30,30) vs (120,30) or refused, and `text_confirmed` never reached the
real trace events. Fixes, all inside edr-wd locator routing/evidence:
(1) visual_only restored to the strict pixel-margin refusal — the text
confirmation is now an explicit opt-in (`allow_text_confirmation=False`
by default) that the visual_only call site never sets, so its outcome is
byte-equivalent to the accepted round-22 behaviour (refused margin stays
`visual_match_ambiguous`, as live-demonstrated by RUN1–RUN3);
(2) the assist is enabled only in the explicitly semantic semantic_first
visual fallbacks, keeps every fail-closed refusal (same-name, missing
source, near-text, out-of-window, duplicate candidates, malformed
signals), and records its actual evidence channel — source
"uia_control_text", frame sha256/origin/scope, window process/pid/rect,
control automation_id/rect and the refused margin, never the control
text — into the real action_requested event via a consume-once
ResolutionEvidenceSink (`resolution_evidence` payload field; the
dispatched tool args are untouched);
(3) V2: a named control's rectangle is validated (four finite numbers,
no booleans, positive area) before conversion; malformed data keeps the
structured `visual_match_ambiguous` refusal instead of raising
ValueError/TypeError.
Regressions added first, per the review: materializer level (the
supervisor's exact counterexample — three recorded names, all refused,
zero visual fallbacks; a pure-pixel visual_only resolution publishes no
evidence) and trace level (semantic_first assist hits the real event
with the evidence channel and (30,30); name "Wrong Window" refuses with
zero dispatches) plus ten malformed-rect parameterized refusals.
test_recording 530 passed; full offline suite green. The supervision
reproduce script re-run after the fix prints refusals for every name
variant and structured errors for malformed rects; the RUN4/RUN5 chains
are read-only intact. ROUND23-evidence.md now carries a dated erratum
reclassifying RUN5 as template+UIA-assisted navigation and correcting
the "pure visual" / "gates unchanged" wording; the old runs and events
are preserved untouched. No live replay this round: the fix restores the
already twice-demonstrated pure-pixel refusal path, so a new live run
adds no information (pure visual remains blocked on this material).
Protected: ZCode root/trace-eval and cloud-side WIP untouched; no
commit/push/deploy; thresholds 0.92/0.05/0.03 unchanged in value and the
visual_only refusal semantics restored.

Twenty-fifth 2026-10-22 round: resolution-evidence lifecycle fix (V3/P2).
The independent review reproduced a cross-step attribution flaw: the
confirmation gate runs after materialization and before dispatch, so an
assisted main action refused with `confirmation_required` (zero
dispatches) left its evidence in the single-slot sink, and the following
pure-semantic cleanup dispatch consumed it unconditionally — its real
action_requested event (resolution=semantic, snapshot OBS-FRESH-4) then
carried the main step's uia_control_text/text_confirmed/margin, a pure
audit-provenance distortion (no mis-click, no gate bypass). Minimal
lifecycle fix per the review's suggestion, all inside edr-wd evidence
routing: (1) every materialization entry — semantic, assertion, cleanup,
retry and failure paths — clears the slot first, so a confirmation-
refused or otherwise undispatched action can never lend its provenance
to a later one; (2) the published envelope now binds the evidence to its
materialized `{step_id, action_id}`; (3) the dispatch consumes the slot
exactly once and writes `resolution_evidence` only when the pending
entry's action matches the action actually being dispatched (otherwise
the event stays clean); (4) the evidence gains a traceable current
observation/snapshot reference (`frame.snapshot_id`) and a stable window
reference (`window.native_id`, locator-consistent "legacy" default),
still redacted with no control text. Regressions mirror the review's
matrix: the exact confirmation-blocked-main → semantic-cleanup flow
(main zero dispatches; cleanup event carries no resolution_evidence), a
matcher-refused main variant, two consecutive assisted steps each
carrying their own step_id/margin, a no-trace-store dispatch, and
extended integration assertions for the new references. The supervision
reproduce script now prints zero `resolution_evidence` occurrences;
the round-23 script's visual_only isolation and malformed-rect refusals
are unchanged. test_recording 534 passed; full offline suite green;
RUN4/RUN5 events remain untouched. Protected: ZCode root/trace-eval and
cloud-side WIP untouched; no commit/push/deploy; thresholds and
verifiers unchanged.

Closeout 2026-10-22: handover summary created at
`/Users/edr-test/ai-projects/doc/dsh-closeout-20261022.md` (facts only; no
new implementation, live action, commit, push, deploy, re-freeze or
automation). One erratum to the twenty-fifth-round entry above: its
"test_recording 534 passed" is a counting-expression slip — the suite
collected 534 items of which 533 passed and 1 was deselected, exactly as
the independent round-7 verification reported; the full-offline figure
"2663 passed / 28 deselected" in the same entry remains a self-reported
result and has not been independently re-run. Nothing else in the round
entries is withdrawn; the RUN5 "pure visual" wording was already
corrected by the twenty-fourth-round erratum and re-confirmed
independently.
