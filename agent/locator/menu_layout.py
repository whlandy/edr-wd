"""Locale-independent menu positions, not absolute-screen coordinate clicks.

The explicit selector records the entire sibling menu layout. Replay must see
the same ancestry and layout, with one real current target in the recorded
slot. Labels are descriptive only; no label aliases or fabricated IDs.
"""
from __future__ import annotations

import math
import re
from typing import Mapping

KIND = "menu-layout/v1"
TOLERANCE = 0.02


def layout(rects):
    """Return sorted indices and scale/translation-invariant rectangles."""
    if not 2 <= len(rects) <= 64:
        return None
    if any(not isinstance(r, (list, tuple)) or len(r) != 4 or
           any(isinstance(v, bool) or not isinstance(v, (int, float)) or
               not math.isfinite(v) for v in r) or
           r[2] <= r[0] or r[3] <= r[1] for r in rects):
        return None
    # Overlapping candidates cannot establish distinct menu slots.
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if min(a[2], b[2]) > max(a[0], b[0]) and \
                    min(a[3], b[3]) > max(a[1], b[1]):
                return None
    left = min(r[0] for r in rects)
    top = min(r[1] for r in rects)
    width = max(r[2] for r in rects) - left
    height = max(r[3] for r in rects) - top
    order = sorted(range(len(rects)), key=lambda i: (
        rects[i][1], rects[i][0], rects[i][3], rects[i][2]))
    normalized = [[round((rects[i][j] - (left if j % 2 == 0 else top)) /
                         (width if j % 2 == 0 else height), 6)
                   for j in range(4)] for i in order]
    return order, normalized


def valid_contract(value):
    if not isinstance(value, Mapping) or set(value) != {"kind", "index", "layout"}:
        return False
    rows = value.get("layout")
    index = value.get("index")
    if value.get("kind") != KIND or not isinstance(rows, (list, tuple)) or \
            isinstance(index, bool) or not isinstance(index, int) or \
            not 0 <= index < len(rows):
        return False
    result = layout(rows)
    return bool(result and result[0] == list(range(len(rows))) and
                all(0 <= v <= 1 for r in rows for v in r) and
                all(abs(a - b) < 0.00001 for r, s in zip(rows, result[1])
                    for a, b in zip(r, s)))


def matches_ancestry(actual, expected):
    if not expected or not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
        return False
    for a, e in zip(actual, expected):
        if not isinstance(a, Mapping) or not isinstance(e, Mapping) or \
                not any(v not in (None, "") for v in e.values()):
            return False
        for key, value in e.items():
            actual_value = a.get(key, a.get(re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()))
            if value not in (None, "") and actual_value != value:
                return False
    return True


def from_recording(control, target_rect, nodes):
    if control.get("controlType") != "MenuItem" or \
            control.get("automationId") or control.get("identifier") or \
            not control.get("ancestry"):
        return None
    peers = [n for n in nodes if isinstance(n, Mapping) and
             n.get("controlType") == "MenuItem" and
             matches_ancestry(n.get("ancestry"), control["ancestry"])]
    rects = [n.get("rect") for n in peers]
    result = layout(rects)
    if result is None or not isinstance(target_rect, (list, tuple)):
        return None
    hits = [i for i, r in enumerate(rects) if list(r) == list(target_rect)]
    if len(hits) != 1:
        return None
    return {"kind": KIND, "index": result[0].index(hits[0]),
            "layout": result[1]}


def resolve_index(contract, rects):
    if not valid_contract(contract):
        return None
    result = layout(rects)
    if result is None or len(result[1]) != len(contract["layout"]):
        return None
    if any(abs(a - b) > TOLERANCE
           for r, s in zip(result[1], contract["layout"])
           for a, b in zip(r, s)):
        return None
    return result[0][contract["index"]]


def current_click_args(target, controls):
    """Materialize the fresh hit into backend arguments, not the old label.

    Geometry finds a current UIA object; injection still uses its unique
    current semantic identity, guarded by the observation fingerprint.
    """
    def field(item, *keys):
        return next((item[k] for k in keys if item.get(k) is not None), None)
    peers = [c for c in controls if field(c, "control_type", "controlType", "role") == "MenuItem"]
    text = field(target, "text", "name", "title")
    if isinstance(text, str) and text and sum(
            field(c, "text", "name", "title") == text for c in peers) == 1:
        # Text only: pairing control_type here makes the backend's
        # child_window(title_re=…) + control_type strict path miss deep Qt
        # MenuItems (text alone resolves them; replay r3 evidence).
        return {"text": text, "parent_fallback": False}
    control_id = field(target, "control_id", "controlId")
    if isinstance(control_id, int) and not isinstance(control_id, bool) and control_id > 0 and sum(
            field(c, "control_id", "controlId") == control_id for c in peers) == 1:
        return {"control_id": control_id, "parent_fallback": False}
    raise ValueError("current menu target has no unique injectable identity")
