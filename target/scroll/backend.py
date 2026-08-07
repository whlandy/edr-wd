"""
backend.py — production wiring: real backend dump_tree -> Observer -> ScrollCoordinator.

(f3 / P0-3 review item: "server.py:452 scroll() 仍直接调 _backend.scroll()，绕过
Detector/Navigator/StateMachine/DispatcherAdapter/Verifier".)

This is the ONLY module that bridges the pure scroll package
(target/scroll) to a live AutomationBackend. It owns two jobs:

  1. `ScrollBackendSource` — turns the backend's `dump_tree(window_title_re,
     max_depth)` control dicts into the `targets_fn` contract that
     `Observer.snapshot()` needs (a list of `build_snapshot`-compatible
     target dicts, with a synthetic window target prepended). It never
     fabricates `ActionReceipt`s and never decides movement — those stay in
     the executor / verifier.

  2. `run_scroll_region(...)` — a thin facade that composes the real chain
     for one bounded step:

         real backend snapshot
             -> PageDetector.detect
             -> ScrollCoordinator.step (controller.plan -> executor.execute_plan
                -> real dispatcher -> verifier.page_changed)
             -> ScrollResult

This is the "main execution chain" the server's MCP `scroll_region` tool
should call, replacing the old `_backend.scroll()` raw-wheel bypass.

Data contract (Target.from_dict in target/observations/models.py):
  * every target dict needs: kind, process_name, pid, native_window_id, title
  * optional: control_type, automation_id, text, rect
The backend control dicts (from dump_tree) carry text/title/name/class_name/
automation_id/control_type; the window metadata (process_name/pid/
native_window_id/title) is supplied by the caller from the connected window.
`build_snapshot` (observations/snapshot.py) computes target_id / fingerprint /
tree_digest, so this module does not.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable, Mapping, Optional

from .detect import PageDetector
from .executor import ScrollExecutor
from .results import ScrollResult
from .verifier import ScrollCoordinator, PageVerifier
from .observer import Observer

# The subset of backend dump_tree / window metadata actually consumed.
DumpTreeFn = Callable[..., Mapping[str, Any]]


def _shape_control(c: Mapping[str, Any]) -> dict:
    """Map one backend dump_tree control dict -> a build_snapshot-compatible
    target dict (kind='control'). Field names mapped from the backend's
    pywinauto-ish keys to the observation contract keys."""
    return {
        "kind": "control",
        "process_name": c.get("process_name") or "",
        "pid": c.get("pid"),
        "native_window_id": str(c.get("native_window_id") or c.get("handle") or c.get("native_handle") or ""),
        "title": c.get("title") or "",
        "control_type": c.get("control_type") or c.get("class_name"),
        "automation_id": str(c.get("automation_id") or "") or None,
        "text": c.get("text") or c.get("name"),
        # rect: backend may give {x,y,width,height} or (left,top,width,height).
        **(_shape_rect(c.get("rect")) if c.get("rect") is not None else {}),
    }


def _shape_rect(rect: Any) -> dict:
    """Normalise a backend rect into a (left, top, right, bottom) 4-tuple."""
    if isinstance(rect, (tuple, list)) and len(rect) == 4:
        return {"rect": tuple(int(v) for v in rect)}
    if isinstance(rect, Mapping):
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("width", 0)), int(rect.get("height", 0))
        return {"rect": (x, y, x + w, y + h)}
    return {}


class ScrollBackendSource:
    """A `targets_fn` factory wired to a real backend's dump_tree.

    `snapshot(targets_fn)` on an `Observer` built with this source returns a
    live `ObservationSnapshot` reflecting the connected window's current
    controls, mirroring exactly the traversal the backend already produces.
    If the backend is unavailable, `dump_tree` returns `{"ok": False, ...}`
    and `targets()` raises `BackendUnavailableError` so the caller can
    respond with a backend-unavailable JSON instead of a fabricated result.
    """

    def __init__(
        self,
        backend: object,
        *,
        window_title_re: Optional[str] = None,
        max_depth: int = 10,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        native_window_id: Optional[str] = None,
        window_title: Optional[str] = None,
        observer_backend: str = "windows_pywinauto",
        host: str = "local",
    ) -> None:
        self._dump_tree = getattr(backend, "dump_tree")
        self._window_title_re = window_title_re
        self._max_depth = max_depth
        self._window_meta = {
            "kind": "window",
            "process_name": process_name or "",
            "pid": pid,
            "native_window_id": native_window_id or "",
            "title": window_title or window_title_re or "",
        }
        self._observer_backend = observer_backend
        self._host = host

    def targets(self) -> list[dict]:
        """Collect target dicts (window prepended) for Observer.snapshot()."""
        tree = self._dump_tree(self._window_title_re, max_depth=self._max_depth)
        if not isinstance(tree, Mapping) or not tree.get("ok"):
            raise BackendUnavailableError(
                tree.get("error") if isinstance(tree, Mapping) else "dump_tree returned non-mapping"
            )
        controls = tree.get("controls", [])
        controls = controls if isinstance(controls, list) else []
        # Derive the window target from the backend's own top-level metadata
        # (title, window_rectangle) rather than blank call-site defaults, so a
        # `scroll_region()` call with no explicit identity still yields a
        # valid, buildable window target (f8 / review issue: an all-empty
        # window meta previously crashed build_snapshot with ProtocolModelError).
        window_meta = dict(self._window_meta)
        tree_title = (tree.get("title") or "").strip()
        if tree_title:
            window_meta["title"] = tree_title
        # native_window_id must be non-empty for Target.from_dict; fall back to
        # the resolved window title when no explicit id/handle was provided.
        if not window_meta.get("native_window_id") and window_meta.get("title"):
            window_meta["native_window_id"] = f"win:{window_meta['title']}"
        # Backend dump_tree doesn't carry the owning process name either —
        # fall back to the window title so process_name stays non-empty and the
        # whole subtree (window + controls) remains buildable.
        if not window_meta.get("process_name") and window_meta.get("title"):
            window_meta["process_name"] = window_meta["title"]
        # Only prepend a window target when it actually carries identity —
        # otherwise the snapshot is anchored by the controls alone.
        if not (window_meta.get("native_window_id") or window_meta.get("title")):
            window_meta = None

        target_dicts = []
        for c in controls:
            d = _shape_control(c)
            # Backend dump_tree nodes (pywinauto/macos) don't carry the owning
            # process identity — stamp it from the resolved window meta so each
            # control target is buildable (Target.from_dict requires non-empty
            # process_name/native_window_id). See f8 / review note.
            if not d.get("process_name"):
                d["process_name"] = window_meta.get("process_name") if window_meta else ""
            if d.get("pid") is None:
                d["pid"] = window_meta.get("pid") if window_meta else None
            if not d.get("native_window_id"):
                d["native_window_id"] = window_meta.get("native_window_id") if window_meta else ""
            if not d.get("title") and window_meta:
                d["title"] = window_meta.get("title") or ""
            target_dicts.append(d)
        if window_meta is not None:
            target_dicts.insert(0, window_meta)
        # Every shaped dict is buildable by Target.from_dict even when a
        # control only carries text/automation_id (no native id/title).
        return target_dicts

    def make_observer(self) -> Observer:
        return Observer(targets_fn=self.targets, backend=self._observer_backend, host=self._host)


class BackendUnavailableError(RuntimeError):
    """Raised when dump_tree reports the backend is not available."""


def run_scroll_region(
    source: ScrollBackendSource,
    detector: Optional[PageDetector] = None,
    verifier: Optional[PageVerifier] = None,
    executor: Optional[ScrollExecutor] = None,
    controller=None,
    threshold: float = 0.0,
) -> dict:
    """Compose the real chain for one bounded step and return a plain dict.

    This is the production entry the server.py `scroll_region` MCP tool
    should call. It executes against the live backend and returns a
    `ScrollResult`-shaped dict (see ScrollResult.to_dict). On backend
    unavailability or a non-matching structure it returns a not_dispatched
    result — never a fabricated success.
    """
    from .controller import NavigationMode, PageController

    controller = controller or PageController()
    detector = detector or PageDetector()
    verifier = verifier or PageVerifier()
    executor = executor or ScrollExecutor()
    observer = source.make_observer()
    coord = ScrollCoordinator(
        detector=detector,
        controller=controller,
        verifier=verifier,
        observer=observer,
        executor=executor,
        threshold=threshold,
    )

    try:
        before = observer.snapshot()
    except BackendUnavailableError as e:
        # backend unavailable — NOT_DISPATCHED, honest terminal outcome.
        return ScrollResult.not_dispatched().to_dict() | {"backend_error": str(e)}
    except ValueError:
        # no controls / no targets — nothing to scroll.
        return ScrollResult.not_dispatched().to_dict()

    result = detector.detect(before)
    coord.set_detection(result)
    try:
        sr = coord.step(NavigationMode.MOVE_NEXT, before, policy=None)
        return sr.to_dict()
    finally:
        coord.clear_detection()


__all__ = [
    "ScrollBackendSource",
    "BackendUnavailableError",
    "run_scroll_region",
    "_shape_control",
    "_shape_rect",
]
