# Window-Scoped Scroll Remaining TODO

## Status

Partially implemented.

The generic verified scroll chain now exists through `scroll_region`, but the
older raw `scroll(clicks, x, y)` primitive is still a low-level pointer action.
This document keeps only the remaining window-safety, CLI, schema, and live
integration work.

## Remaining P0: Raw Pointer Window Safety

### P0.1 Window-Scoped Raw Scroll

Extend or wrap raw `scroll` so callers can bind the event to a specific window.

Desired request shape:

```json
{
  "clicks": -5,
  "x": 416,
  "y": 174,
  "coordinate_space": "window",
  "window_title_re": "^日志中心$",
  "expected_process_name": "EDRClient.exe",
  "expected_pid": 6752
}
```

Acceptance:

- Resolve a unique target window from title, process, and optional PID.
- Convert window-relative coordinates to screen coordinates.
- Confirm the point belongs to the intended top-level window before dispatch.
- Return a stable ownership/occlusion error when another window covers the
  point.
- Preserve `scroll(clicks, x, y)` compatibility, but clearly mark it as
  unscoped/low-level in docs and results.

### P0.2 Windows RDP Foreground Fallback

Harden Windows window-lock verification under RDP.

Acceptance:

- Prefer Win32 foreground HWND, PID, title, and rect over pywinauto-only active
  window detection.
- `strict=True` blocks when foreground ownership cannot be verified.
- `strict=False` still checks window existence, coordinate hit, and process
  ownership before dispatch.
- Degraded verification is explicit in the result payload.

### P0.3 Unified Pointer Result Envelope  `[DONE — round-002]`

Keep low-level dispatch success separate from verified UI effect.

Acceptance:

- Raw pointer results expose at least `event_dispatched`.
- Verified composites expose `dispatched`, `moved`, and `reason`.
- Callers must not present raw `ok=true` as user-visible scroll success.
- Failure states have stable machine-readable codes, for example
  `target_occluded`, `target_ambiguous`, `verification_unavailable`,
  `no_effect`, and `not_dispatched`.

Implementation summary (round-002): new pure module
`target/scroll/pointer_result.py` centralises the stable codes
(`STABLE_POINTER_CODES`) and provides `normalize()` to wrap raw backend
pointer payloads in the envelope (`ok` / `event_dispatched` / `code` /
`scope` / `tool`, additive so existing fields survive), `code_for()` /
`event_dispatched_for()` derivation (dry-run is not a real dispatch), and
`is_verified_success()` (only `moved=True` in a composite is success). The
server MCP tools `scroll`, `scroll_window`, `click_at`, and `drag` now emit
the envelope. `ScrollResult.to_dict()` aliases `event_dispatched` =
`dispatched` and adds `code` via `reason_to_code()`.

## Remaining P1: Semantic Planner And Evidence

### P1.1 CLI Entry Points

Add operator-friendly CLI wrappers so normal workflows do not require ad-hoc
temporary Python scripts.

Desired examples:

```bash
edr-wd --target win-dev window list
edr-wd --target win-dev window inspect --title '^日志中心$'
edr-wd --target win-dev scroll --window-title '^日志中心$' --down 5 --verify
edr-wd --target win-dev page-next --window-title '^日志中心$' --verify
```

Acceptance:

- CLI handles MCP result unpacking, timeouts, and error summaries.
- CLI can persist before/action/after evidence when `--verify` is used.
- Normal workflows do not create `/tmp/edr_wd_*.py`; temporary scripts are only
  marked debug-only.

### P1.2 Unified Window Argument Schema

Normalize tool window selection parameters.

Desired shape:

```json
{
  "window": {
    "title_re": "^日志中心$",
    "process_name": "EDRClient.exe",
    "pid": 6752,
    "handle": 66336
  }
}
```

Acceptance:

- New tools use a consistent `window` object where possible.
- Legacy parameters remain compatible.
- Tool docs include executable JSON examples so agents do not need to inspect
  `server.py` for parameter names.

### P1.3 Before/Action/After Reports

Attach evidence to GUI actions by default.

Acceptance:

- Capture before observation/screenshot.
- Record exact action parameters and ownership/coordinate decisions.
- Capture after observation/screenshot.
- Reuse a step's `after` as the next step's `before`.
- Render Markdown/HTML reports with trace and screenshots together.

## Remaining Live Tests

### Windows RDP Occlusion

Scenario:

1. Open target window `日志中心`.
2. Open another window overlapping the target scroll point.
3. Attempt window-scoped scroll.
4. Verify EDR-WD either restores ownership safely or returns an occlusion error.
5. Verify the overlapping window is never scrolled by mistake.

### HiSec Paginated Logs

Scenario: `日志中心 -> 操作日志 -> 查看更早记录`.

Acceptance:

- Planner detects a paginated table.
- The action uses semantic next-page behavior, not blind wheel scrolling.
- Before/after first-row timestamp or page marker changes.
- Report shows before/action/after and verification result.
- No temporary Python script is created for the normal path.

### macOS Scroll Area

Scenario:

1. Resolve and focus a macOS scrollable region.
2. Execute one bounded scroll-region action.
3. Verify content change or return `NO_SCROLL_EFFECT`.

## Non-Goals

- Do not bypass window safety by closing unrelated windows.
- Do not treat screenshots alone as authoritative movement proof.
- Do not hard-code a fixed resolution or fixed coordinate script.
- Do not hard-code HiSec `nextPageButton`; use structural pagination detection.
