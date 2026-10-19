# EDR-WD goal validation — 2026-10-16

This is a current-state validation record, not a completion certificate.
The working tree contains the implementation under review and is not yet
deployed to a live target.

## Offline evidence

Command: `python3 -m pytest -q -m 'unit or regression'`

Latest result: **2489 passed, 28 deselected** (38.01 s). `python3 -m compileall -q
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
