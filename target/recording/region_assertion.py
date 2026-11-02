"""Region / multi-component assertion drafts and shared condition semantics (v2).

This module is the single authority for the region assertion draft schema
(docs/architecture/06-region-multicomponent-assertion-design.md §3–§5):

- ``build_region_draft`` freezes a wizard editing session into a typed v2
  draft dict;
- ``validate_region_draft`` is the type/shape validator the target session
  and the agent-side golden verifier both reuse, so a draft can never be
  saved with an expectation the replay evaluator would not understand;
- ``render_region_summary`` renders the plain-Chinese summary (06 §4);
- ``evaluate_region_conditions`` is the one row-level conjunction comparator
  shared by the replay evaluator (``agent/execution/expectations.py``) and
  the wizard's read-only preview — a second comparator with different
  semantics is exactly what 05 §3.3 forbids.

Time semantics are the user-confirmed ones (06 §4): content time is anchored
at each check's ``now``, the accepted window is ``[C−recentSeconds, C]``
recomputed per sample, future timestamps never match, and a value that cannot
be parsed as a timezone-aware wall clock is *undetermined* — never ``False``,
never silently widened by an implicit clock tolerance.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

try:  # target-local deployment keeps modules importable standalone
    from .models import RecordingModelError
except ImportError:  # pragma: no cover - flat deployment layout
    from models import RecordingModelError  # type: ignore[no-redef]

SCHEMA_VERSION = 2
KIND = "region_assertion"

TARGET_MODES = ("container_rows", "fixed_components")
QUANTIFIERS = ("any", "all")
CONDITION_OPS = ("all", "any")
LEAF_OPERATORS = (
    "equals", "contains", "not_contains", "regex", "bool_equals",
    "exists", "not_exists", "count", "time_recent",
)
TEXT_FIELDS = ("name", "text", "value")
BOOL_FIELDS = ("checked", "enabled")
LEAF_FIELDS = TEXT_FIELDS + BOOL_FIELDS + ("time",)
COUNT_OPS = ("equals", "at_least", "at_most")
OBSERVATION_MODES = ("first_match", "absent_full_period")
OBSERVATION_STARTS = ("after_bound_action", "immediate")
READ_STATUSES = ("ok", "unknown", "error")

_FIELD_LABELS = {
    "name": "名称", "text": "文字", "value": "值",
    "checked": "勾选状态", "enabled": "可用状态", "time": "时间",
}
_COUNT_LABELS = {"equals": "等于", "at_least": "至少", "at_most": "至多"}

_MAX_RECENT_SECONDS = 3600.0
_MAX_DEADLINE_SECONDS = 300.0

# Timezone-aware wall-clock parsing.  A timestamp without an explicit offset
# has an unknown timezone: per 06 §4 that is *undetermined*, not a silent
# local-time assumption, and a bare time-of-day is missing its date.
_TIME_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S.%f%z",
    "%Y/%m/%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M%z",
    "%Y-%m-%d %H:%M%z",
)


class RegionDraftError(RecordingModelError):
    """A region draft failed validation (stable code ``region_draft_invalid``)."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__("region_draft_invalid", message, path=path)


def _require_str(value: Any, path: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise RegionDraftError(f"{path} must be a string", path=path)
    if not empty and not value.strip():
        raise RegionDraftError(f"{path} must be a non-empty string", path=path)
    return value


def _require_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RegionDraftError(f"{path} must be an integer", path=path)
    return value


def _require_positive_number(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise RegionDraftError(f"{path} must be a positive number", path=path)
    return float(value)


def _identity_part(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegionDraftError(f"{path} must be a string or null", path=path)
    return value or None


def validate_component_identity(identity: Any, path: str) -> dict:
    if not isinstance(identity, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(identity) - {"controlType", "automationId", "identifier", "name"}
    if unknown:
        raise RegionDraftError(
            f"{path} has unknown fields: {sorted(unknown)}", path=path)
    out = {
        "controlType": _identity_part(identity.get("controlType"), f"{path}.controlType"),
        "automationId": _identity_part(identity.get("automationId"), f"{path}.automationId"),
        "identifier": _identity_part(identity.get("identifier"), f"{path}.identifier"),
        "name": _identity_part(identity.get("name"), f"{path}.name"),
    }
    if not (out["automationId"] or out["identifier"]
            or (out["controlType"] and out["name"])):
        raise RegionDraftError(
            f"{path} requires a stable identity (automationId/identifier or controlType+name)",
            path=path,
        )
    return out


def validate_container(container: Any, path: str, *, required: bool) -> dict | None:
    if container is None:
        if required:
            raise RegionDraftError(
                f"{path} is required for container_rows mode", path=path)
        return None
    return validate_component_identity(container, path)


# ---------------------------------------------------------------------------
# Condition tree
# ---------------------------------------------------------------------------


def is_subtree(node: Any) -> bool:
    return isinstance(node, Mapping) and ("op" in node or "children" in node)


def validate_condition_tree(
    conditions: Any, path: str, *, root: bool = False,
    content_time_enabled: bool = True,
) -> dict:
    """Validate one condition subtree; leaves are normalized in place.

    A count leaf is only meaningful against the row collection, so it is
    accepted solely as a direct child of the root tree and at most once.
    """
    if not isinstance(conditions, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    op = conditions.get("op")
    if op not in CONDITION_OPS:
        raise RegionDraftError(
            f"{path}.op must be one of {list(CONDITION_OPS)}", path=f"{path}.op")
    children = conditions.get("children")
    if not isinstance(children, (list, tuple)) or not children:
        raise RegionDraftError(f"{path}.children must be a non-empty array", path=f"{path}.children")
    unknown = set(conditions) - {"op", "children"}
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    normalized_children: list[dict] = []
    count_leaves = 0
    for index, child in enumerate(children):
        child_path = f"{path}.children[{index}]"
        if is_subtree(child):
            if root:
                raise RegionDraftError(
                    "condition subtrees are only supported one level deep in v2",
                    path=child_path,
                )
            normalized_children.append(
                validate_condition_tree(child, child_path, root=False,
                                        content_time_enabled=content_time_enabled))
            continue
        leaf = validate_condition_leaf(
            child, child_path, content_time_enabled=content_time_enabled)
        if leaf["operator"] == "count":
            count_leaves += 1
            if not root:
                raise RegionDraftError(
                    "count leaves are only valid as direct children of the root conditions",
                    path=child_path,
                )
        normalized_children.append(leaf)
    if root and count_leaves > 1:
        raise RegionDraftError("at most one count leaf is allowed", path=path)
    return {"op": op, "children": normalized_children}


def validate_condition_leaf(
    leaf: Any, path: str, *, content_time_enabled: bool = True,
) -> dict:
    if not isinstance(leaf, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(leaf) - {
        "field", "operator", "expected", "count",
        "componentKey",  # P2e：叶锚定组件稳定键（可选；旧草稿兼容）
    }
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    operator = leaf.get("operator")
    if operator not in LEAF_OPERATORS:
        raise RegionDraftError(
            f"{path}.operator must be one of {list(LEAF_OPERATORS)}",
            path=f"{path}.operator",
        )
    out: dict[str, Any] = {"operator": operator}
    # P2f：componentKey 保留并校验（非空字符串；旧草稿无 key=可选）。
    if "componentKey" in leaf:
        _ck = leaf["componentKey"]
        if not isinstance(_ck, str) or not _ck.strip():
            raise RegionDraftError(
                "componentKey must be a non-empty string",
                path=f"{path}.componentKey",
            )
        out["componentKey"] = _ck
    if operator == "count":
        # 数量条件基于满足条件的行数；field 不指向某个单元格。
        field = leaf.get("field")
        if field is not None and field != "rows":
            raise RegionDraftError(
                'count leaves take no cell field (use field "rows")',
                path=f"{path}.field",
            )
        out["field"] = "rows"
        spec = leaf.get("count")
        if not isinstance(spec, Mapping):
            raise RegionDraftError(f"{path}.count must be an object", path=f"{path}.count")
        spec_unknown = set(spec) - {"op", "value"}
        if spec_unknown:
            raise RegionDraftError(
                f"{path}.count has unknown fields: {sorted(spec_unknown)}",
                path=f"{path}.count",
            )
        if spec.get("op") not in COUNT_OPS:
            raise RegionDraftError(
                f"{path}.count.op must be one of {list(COUNT_OPS)}",
                path=f"{path}.count.op",
            )
        value = _require_int(spec.get("value"), f"{path}.count.value")
        if value < 0:
            raise RegionDraftError(
                f"{path}.count.value must be >= 0", path=f"{path}.count.value")
        out["count"] = {"op": spec["op"], "value": value}
        if "expected" in leaf:
            raise RegionDraftError(
                "count leaves carry their expectation in count.value, not expected",
                path=f"{path}.expected",
            )
        return out
    field = leaf.get("field")
    if field not in LEAF_FIELDS:
        raise RegionDraftError(
            f"{path}.field must be one of {list(LEAF_FIELDS)}", path=f"{path}.field")
    out["field"] = field
    if operator in ("exists", "not_exists"):
        if "expected" in leaf:
            raise RegionDraftError(
                f"{path}.expected is not valid for {operator}", path=f"{path}.expected")
        return out
    if operator == "time_recent":
        if field != "time":
            raise RegionDraftError(
                "time_recent requires the time field", path=f"{path}.field")
        if not content_time_enabled:
            raise RegionDraftError(
                "time_recent requires contentTime.enabled", path=path)
        if "expected" in leaf:
            raise RegionDraftError(
                "time_recent takes its threshold from contentTime.recentSeconds",
                path=f"{path}.expected",
            )
        return out
    if "expected" not in leaf:
        raise RegionDraftError(f"{path}.expected is required", path=f"{path}.expected")
    expected = leaf["expected"]
    if operator == "bool_equals":
        if field not in BOOL_FIELDS:
            raise RegionDraftError(
                f"bool_equals requires a boolean field, got {field!r}",
                path=f"{path}.field",
            )
        if not isinstance(expected, bool):
            raise RegionDraftError(
                f"{path}.expected must be a real boolean", path=f"{path}.expected")
    else:
        if field in BOOL_FIELDS:
            raise RegionDraftError(
                f"boolean field {field!r} only supports bool_equals/exists/not_exists",
                path=f"{path}.operator",
            )
        if operator == "regex":
            if not isinstance(expected, str) or not expected:
                raise RegionDraftError(
                    f"{path}.expected must be a non-empty pattern string",
                    path=f"{path}.expected",
                )
            try:
                re.compile(expected)
            except re.error as exc:
                raise RegionDraftError(
                    f"{path}.expected is not a valid regex: {exc}",
                    path=f"{path}.expected",
                ) from exc
        elif not isinstance(expected, str):
            # 文本期望必须字符串："123"/"true" 保持字符串，绝不静默转型。
            raise RegionDraftError(
                f"{path}.expected must be a string", path=f"{path}.expected")
        if operator == "contains" and not expected:
            raise RegionDraftError(
                f"{path}.expected must be non-empty for contains",
                path=f"{path}.expected",
            )
    out["expected"] = expected
    return out


def validate_content_time(content_time: Any, path: str) -> dict:
    if not isinstance(content_time, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(content_time) - {"anchor", "recentSeconds", "enabled"}
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    anchor = content_time.get("anchor", "each_check_now")
    if anchor != "each_check_now":
        raise RegionDraftError(
            f"{path}.anchor must be 'each_check_now' in v2", path=f"{path}.anchor")
    enabled = content_time.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegionDraftError(f"{path}.enabled must be boolean", path=f"{path}.enabled")
    recent = content_time.get("recentSeconds", 5.0)
    recent = _require_positive_number(recent, f"{path}.recentSeconds")
    if recent > _MAX_RECENT_SECONDS:
        raise RegionDraftError(
            f"{path}.recentSeconds must be <= {_MAX_RECENT_SECONDS:g}",
            path=f"{path}.recentSeconds",
        )
    return {"anchor": anchor, "recentSeconds": recent, "enabled": enabled}


def validate_observation(observation: Any, path: str, *, has_binding: bool) -> dict:
    if not isinstance(observation, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(observation) - {"mode", "deadlineSeconds", "start"}
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    mode = observation.get("mode", "first_match")
    if mode not in OBSERVATION_MODES:
        raise RegionDraftError(
            f"{path}.mode must be one of {list(OBSERVATION_MODES)}",
            path=f"{path}.mode",
        )
    deadline = observation.get("deadlineSeconds", 60.0)
    deadline = _require_positive_number(deadline, f"{path}.deadlineSeconds")
    if deadline > _MAX_DEADLINE_SECONDS:
        raise RegionDraftError(
            f"{path}.deadlineSeconds must be <= {_MAX_DEADLINE_SECONDS:g}",
            path=f"{path}.deadlineSeconds",
        )
    start = observation.get("start", "after_bound_action" if has_binding else "immediate")
    if start not in OBSERVATION_STARTS:
        raise RegionDraftError(
            f"{path}.start must be one of {list(OBSERVATION_STARTS)}",
            path=f"{path}.start",
        )
    if start == "after_bound_action" and not has_binding:
        raise RegionDraftError(
            f"{path}.start='after_bound_action' requires an explicit action binding",
            path=f"{path}.start",
        )
    return {"mode": mode, "deadlineSeconds": deadline, "start": start}


def validate_binding(binding: Any, path: str) -> dict | None:
    if binding is None:
        return None
    if isinstance(binding, (list, tuple)):
        if len(binding) != 2:
            raise RegionDraftError(f"{path} must be [sequence, causalId]", path=path)
        binding = {"sequence": binding[0], "causalId": binding[1]}
    if not isinstance(binding, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(binding) - {"sequence", "causalId"}
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    sequence = _require_int(binding.get("sequence"), f"{path}.sequence")
    if sequence < 1:
        raise RegionDraftError(f"{path}.sequence must be >= 1", path=f"{path}.sequence")
    causal_id = _require_str(binding.get("causalId"), f"{path}.causalId")
    return {"sequence": sequence, "causalId": causal_id}


def validate_window(window: Any, path: str) -> dict:
    if not isinstance(window, Mapping):
        raise RegionDraftError(f"{path} must be an object", path=path)
    unknown = set(window) - {"processName", "title", "handle", "pid"}
    if unknown:
        raise RegionDraftError(f"{path} has unknown fields: {sorted(unknown)}", path=path)
    process = _require_str(window.get("processName"), f"{path}.processName")
    title = _require_str(window.get("title"), f"{path}.title")
    out: dict[str, Any] = {"processName": process, "title": title}
    handle = window.get("handle")
    if handle is not None:
        handle = _require_int(handle, f"{path}.handle")
        out["handle"] = handle
    pid = window.get("pid")
    if pid is not None:
        pid = _require_int(pid, f"{path}.pid")
        out["pid"] = pid
    return out


# ---------------------------------------------------------------------------
# Draft build / validate / render
# ---------------------------------------------------------------------------


def build_region_draft(
    *,
    window: Mapping[str, Any],
    target_mode: str,
    container: Mapping[str, Any] | None = None,
    components: Iterable[Mapping[str, Any]] = (),
    quantifier: str = "any",
    conditions: Mapping[str, Any],
    content_time: Mapping[str, Any] | None = None,
    observation: Mapping[str, Any] | None = None,
    binding: Mapping[str, Any] | tuple[int, str] | None = None,
    draft_id: str | None = None,
    request_id: str | None = None,
    observed_at: str | None = None,
    read_status: str = "unknown",
    read_error: str | None = None,
) -> dict:
    """Freeze one wizard editing session into a v2 draft dict.

    Pure logic: no session, no backend.  Validation is delegated to
    ``validate_region_draft`` so build and save cannot disagree about the
    schema; the render summary is computed once here and re-rendered
    authoritatively on validate.
    """
    draft = {
        "draftId": draft_id or uuid.uuid4().hex,
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "requestId": request_id or draft_id or uuid.uuid4().hex,
        "window": dict(window),
        "target": {
            "mode": target_mode,
            "container": dict(container) if container else None,
            "components": [dict(item) for item in components],
        },
        "quantifier": quantifier,
        "conditions": conditions,
        "contentTime": dict(content_time or {}),
        "observation": dict(observation or {}),
        "binding": binding,
        "summaryZh": "",
        "observedAt": observed_at,
        "readStatus": read_status,
        "readError": read_error,
    }
    return validate_region_draft(draft)


def validate_region_draft(
    draft: Mapping[str, Any], *, require_window: bool = True,
) -> dict:
    """Validate one v2 draft and return the normalized dict.

    ``require_window=False`` re-validates a persisted (stripped) draft — the
    wire form carries no window identity (it lives in ``evidence.window``);
    editor submissions must always carry it.  Raises :class:`RegionDraftError`
    (a ``RecordingModelError``) with a stable path so the wizard can keep the
    form open and show the offending field.  Pure-UI observation fields
    (``sampleValue``/``observedAt``/``readStatus``/``readError``/``summaryZh``)
    are validated but not allowed to influence replay semantics (see
    ``draft_for_persistence``).
    """
    if not isinstance(draft, Mapping):
        raise RegionDraftError("draft must be an object", path="draft")
    unknown = set(draft) - {
        "draftId", "schemaVersion", "kind", "requestId", "window", "target",
        "quantifier", "conditions", "contentTime", "observation", "binding",
        "summaryZh", "observedAt", "readStatus", "readError",
    }
    if unknown:
        raise RegionDraftError(f"draft has unknown fields: {sorted(unknown)}", path="draft")
    draft_id = _require_str(draft.get("draftId"), "draft.draftId")
    request_id = draft.get("requestId")
    request_id = _require_str(request_id, "draft.requestId") if request_id is not None else draft_id
    if draft.get("schemaVersion") != SCHEMA_VERSION:
        raise RegionDraftError(
            f"draft.schemaVersion must be {SCHEMA_VERSION}", path="draft.schemaVersion")
    if draft.get("kind") != KIND:
        raise RegionDraftError(f"draft.kind must be {KIND!r}", path="draft.kind")
    raw_window = draft.get("window")
    if raw_window is None and not require_window:
        window = None
    else:
        window = validate_window(raw_window, "draft.window")
    read_status = draft.get("readStatus", "unknown")
    if read_status not in READ_STATUSES:
        raise RegionDraftError(
            f"draft.readStatus must be one of {list(READ_STATUSES)}", path="draft.readStatus")
    read_error = draft.get("readError")
    if read_error is not None and not isinstance(read_error, str):
        raise RegionDraftError("draft.readError must be a string or null", path="draft.readError")

    target = draft.get("target")
    if not isinstance(target, Mapping):
        raise RegionDraftError("draft.target must be an object", path="draft.target")
    target_unknown = set(target) - {"mode", "container", "components"}
    if target_unknown:
        raise RegionDraftError(
            f"draft.target has unknown fields: {sorted(target_unknown)}",
            path="draft.target",
        )
    mode = target.get("mode")
    if mode not in TARGET_MODES:
        raise RegionDraftError(
            f"draft.target.mode must be one of {list(TARGET_MODES)}",
            path="draft.target.mode",
        )
    container = validate_container(
        target.get("container"), "draft.target.container",
        required=(mode == "container_rows"),
    )
    raw_components = target.get("components") or []
    if not isinstance(raw_components, (list, tuple)):
        raise RegionDraftError("draft.target.components must be an array",
                               path="draft.target.components")
    components: list[dict] = []
    for index, item in enumerate(raw_components):
        component_path = f"draft.target.components[{index}]"
        if not isinstance(item, Mapping):
            raise RegionDraftError(f"{component_path} must be an object", path=component_path)
        item_unknown = set(item) - {
            "field", "identity", "sampleValue",
            "key",  # P2e：稳定组件键（可选；旧无 key 草稿兼容）
        }
        if item_unknown:
            raise RegionDraftError(
                f"{component_path} has unknown fields: {sorted(item_unknown)}",
                path=component_path,
            )
        field = item.get("field")
        if field not in LEAF_FIELDS:
            raise RegionDraftError(
                f"{component_path}.field must be one of {list(LEAF_FIELDS)}",
                path=f"{component_path}.field",
            )
        components.append({
            "field": field,
            # P2e：稳定组件键透传（旧无 key 草稿兼容=key 可选）。
            **({"key": str(item["key"])} if item.get("key") else {}),
            "identity": validate_component_identity(
                item.get("identity"), f"{component_path}.identity"),
            # sampleValue 是向导展示用的「当前观测」，不是回放语义字段。
            **({"sampleValue": item["sampleValue"]} if "sampleValue" in item else {}),
        })
    if mode == "fixed_components":
        if not components:
            raise RegionDraftError(
                "fixed_components mode requires at least one component",
                path="draft.target.components",
            )
        # P2d（审查复验）：同 field 不同身份=合法——组件唯一性由 identity
        # 校验保证；多组件可共享同一读取属性并分别评估（各自 condition
        # 叶子）。旧草稿（字段互异）仍兼容=本改动为宽松超集。
        # P2g：key 唯一性+叶绑定校验（重复键=覆盖歧义=显式拒绝；
        # 叶 componentKey 必须绑定存在的组件且 field 一致）。
        keys = [c.get("key") for c in components if c.get("key")]
        if len(keys) != len(set(keys)):
            raise RegionDraftError(
                "fixed_components component keys must be unique",
                path="draft.target.components",
            )
        keyed = {c["key"]: c["field"] for c in components if c.get("key")}

    quantifier = draft.get("quantifier")
    if quantifier not in QUANTIFIERS:
        raise RegionDraftError(
            f"draft.quantifier must be one of {list(QUANTIFIERS)}",
            path="draft.quantifier",
        )
    content_time = validate_content_time(
        draft.get("contentTime") or {}, "draft.contentTime")
    binding = validate_binding(draft.get("binding"), "draft.binding")
    observation = validate_observation(
        draft.get("observation") or {}, "draft.observation",
        has_binding=binding is not None,
    )
    conditions = validate_condition_tree(
        draft.get("conditions"), "draft.conditions", root=True,
        content_time_enabled=content_time["enabled"],
    )
    root_leaves = [child for child in conditions["children"] if "operator" in child]
    count_leaves = [leaf for leaf in root_leaves if leaf["operator"] == "count"]
    if count_leaves and quantifier != "any":
        raise RegionDraftError(
            "count conditions pair with quantifier 'any'", path="draft.quantifier")
    if mode == "fixed_components":
        leaf_fields = {leaf.get("field") for leaf in root_leaves if leaf.get("field")}
        # P2g：叶 componentKey 绑定校验（存在+field 一致）。
        import itertools as _it
        def _iter_leaves(node):
            if is_subtree(node):
                for _c in (node.get("children") or ()):
                    yield from _iter_leaves(_c)
            else:
                yield node
        for _leaf in _it.chain(*(_iter_leaves(_n) for _n in root_leaves)):
            _ck = _leaf.get("componentKey")
            if _ck is not None:
                if _ck not in keyed:
                    raise RegionDraftError(
                        f"conditions reference unknown componentKey: {_ck}",
                        path="draft.conditions",
                    )
                if _leaf.get("field") != keyed[_ck]:
                    raise RegionDraftError(
                        f"componentKey {_ck} field mismatch",
                        path="draft.conditions",
                    )
        component_fields = {item["field"] for item in components}
        unmapped = leaf_fields - component_fields - {"rows"}
        if unmapped:
            raise RegionDraftError(
                f"conditions reference fields no component reads: {sorted(unmapped)}",
                path="draft.conditions",
            )

    normalized: dict[str, Any] = {
        "draftId": draft_id,
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "requestId": request_id,
        "window": window,
        "target": {
            "mode": mode,
            "container": container,
            "components": components,
        },
        "quantifier": quantifier,
        "conditions": conditions,
        "contentTime": content_time,
        "observation": observation,
        "binding": binding,
        "summaryZh": render_region_summary({
            "target": {"mode": mode}, "quantifier": quantifier,
            "conditions": conditions, "contentTime": content_time,
            "observation": observation, "binding": binding,
        }),
        "observedAt": draft.get("observedAt"),
        "readStatus": read_status,
        "readError": read_error,
    }
    observed_at = draft.get("observedAt")
    if observed_at is not None and not isinstance(observed_at, str):
        raise RegionDraftError("draft.observedAt must be a string or null",
                               path="draft.observedAt")
    return normalized


def draft_for_persistence(draft: Mapping[str, Any]) -> dict:
    """Strip pure-UI fields and the ephemeral window before persisting.

    ``window`` only ever reaches ``evidence.window`` and the session's window
    check (agent golden verifiers forbid handle/pid), and sample values /
    read status are editor observation, not replay semantics.
    """
    validated = draft if draft.get("summaryZh") else validate_region_draft(draft)
    persisted = {
        key: value for key, value in validated.items()
        if key not in {"window", "summaryZh", "observedAt", "readStatus", "readError"}
    }
    target = dict(persisted.get("target") or {})
    components = []
    for item in target.get("components") or ():
        components.append({k: v for k, v in item.items() if k != "sampleValue"})
    target["components"] = components
    persisted["target"] = target
    return persisted


# ---------------------------------------------------------------------------
# Chinese summary (06 §4 example wording)
# ---------------------------------------------------------------------------


def _render_leaf(leaf: Mapping[str, Any], recent_seconds: float) -> str:
    operator = leaf.get("operator")
    field = leaf.get("field")
    label = _FIELD_LABELS.get(str(field), str(field))
    if operator == "count":
        spec = leaf.get("count") or {}
        return f"满足条件的行数{_COUNT_LABELS[spec['op']]}{spec['value']}行"
    if operator == "time_recent":
        seconds = f"{recent_seconds:g}秒"
        return f"该行{label}位于本次检查当下最近{seconds}"
    if operator == "exists":
        return f"{label}可读取"
    if operator == "not_exists":
        return f"{label}不存在（当前无法确认时判定不明）"
    expected = leaf.get("expected")
    if operator == "bool_equals":
        if field == "checked":
            return f"勾选状态为「{'已勾选' if expected else '未勾选'}」"
        return f"可用状态为「{'可用' if expected else '不可用'}」"
    verb = {
        "equals": "等于", "contains": "包含", "not_contains": "不包含",
        "regex": "匹配",
    }[str(operator)]
    return f"{label}{verb}「{expected}」"


def _render_conditions(
    conditions: Mapping[str, Any], recent_seconds: float,
) -> str:
    parts = [
        _render_leaf(child, recent_seconds) if not is_subtree(child)
        else _render_conditions(child, recent_seconds)
        for child in conditions.get("children") or ()
    ]
    joiner = "且" if conditions.get("op") == "all" else "或"
    return joiner.join(parts)


def render_region_summary(draft: Mapping[str, Any]) -> str:
    """One plain-Chinese sentence: what is observed, for how long, and what passes.

    Mirrors the confirmed wording in 06 §4; deadline and recent-seconds are
    rendered from the draft, never hard-coded.
    """
    target = draft.get("target") or {}
    mode = target.get("mode") or draft.get("targetMode") or "container_rows"
    quantifier = draft.get("quantifier") or "any"
    conditions = draft.get("conditions") or {}
    content_time = draft.get("contentTime") or {}
    observation = draft.get("observation") or {}
    binding = draft.get("binding")
    recent = float(content_time.get("recentSeconds") or 5.0)
    deadline = float(observation.get("deadlineSeconds") or 60.0)
    scope = "列表" if mode == "container_rows" else "所选组件"
    if mode == "container_rows":
        subject = "任意一行" if quantifier == "any" else "所有行"
    else:
        subject = "任一组件" if quantifier == "any" else "所有组件"
    prefix = (
        f"第 {binding['sequence']} 个动作完成后"
        if isinstance(binding, Mapping) and binding.get("sequence") is not None
        else "开始观察后"
    )
    body = _render_conditions(conditions, recent) if conditions else ""
    if observation.get("mode") == "absent_full_period":
        tail = f"整个观察期（最多{deadline:g}秒）内都不出现才通过"
    else:
        tail = f"最多观察{deadline:g}秒，首次出现即通过"
    time_note = ""
    if content_time.get("enabled"):
        time_note = f"内容时间以每次检查当下为锚点，接受最近{recent:g}秒。"
    return (
        f"{prefix}，{tail}；{scope}中{subject}同时满足{body or '（未配置条件）'}。"
        f"{time_note}"
    )


# ---------------------------------------------------------------------------
# Shared row-level evaluation (replay evaluator + wizard preview)
# ---------------------------------------------------------------------------


def parse_content_time(
    text: Any, now: datetime,
) -> tuple[str, datetime | None]:
    """Parse one observed timestamp against an aware ``now``.

    Returns ``("ok", aware_dt)`` when the text carries an explicit timezone;
    ``("undetermined", None)`` for unparseable / date-missing / timezone-
    unknown values (06 §4: those are undetermined, never ``False``).
    """
    if not isinstance(text, str) or not text.strip():
        return "undetermined", None
    raw = text.strip()
    candidates = [raw]
    if raw.endswith(("Z", "z")):  # UTC designator → +00:00 for fromisoformat
        candidates.insert(0, raw[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return "undetermined", None  # 时区不明：不可判定
        return "ok", parsed.astimezone(timezone.utc)
    for pattern in _TIME_PATTERNS:
        try:
            parsed = datetime.strptime(raw, pattern)
        except ValueError:
            continue
        return "ok", parsed.astimezone(timezone.utc)
    return "undetermined", None


def time_recent_matches(
    text: Any, now: datetime, recent_seconds: float,
) -> tuple[str, dict]:
    """Compare one observed time against ``[now − recentSeconds, now]``.

    The window is recomputed from the caller's ``now`` on every call — the
    anchor is each check's present, never the recording moment.  Future
    timestamps are rejected (no implicit clock tolerance); only an explicit
    configuration may widen the window, which v2 does not carry.
    """
    status, parsed = parse_content_time(text, now)
    if status != "ok":
        return "undetermined", {"raw": text, "reason": "time_unparseable_or_tz_unknown"}
    lower = now - timedelta(seconds=float(recent_seconds))
    if parsed > now:
        return "fail", {"raw": text, "reason": "future_time_rejected",
                        "windowStart": lower.isoformat(), "now": now.isoformat()}
    matched = lower <= parsed <= now
    detail = {
        "raw": text, "parsed": parsed.isoformat(),
        "windowStart": lower.isoformat(), "now": now.isoformat(),
    }
    return ("pass" if matched else "fail"), detail


def evaluate_leaf(
    leaf: Mapping[str, Any], value: Any, *, now: datetime,
    recent_seconds: float,
) -> tuple[str, dict]:
    """Evaluate one leaf against one observed value.

    ``None``/missing observations are undetermined — unreadable is not
    ``False`` (05 §3.3: ``False``/empty/0 are real values compared by
    equality, never truthiness).
    """
    operator = leaf.get("operator")
    detail: dict[str, Any] = {"operator": operator, "field": leaf.get("field")}
    if operator in ("exists", "not_exists"):
        # P2h（审查实锤）：None/空=不可读≠明确不存在——缺失键/读取失败/
        # 保护/重名一律 undetermined（缺失身份不能误过 not_exists）。
        # not_exists 通过需权威确认不存在（readStatus 通道=后续批次）。
        readable = value is not None and value != ""
        detail["actual"] = value
        if operator == "exists":
            return ("pass" if readable else "undetermined"), detail
        if not readable:
            return "undetermined", detail
        return "fail", detail
    if value is None or value == "":
        detail["actual"] = value
        detail["reason"] = "value_unreadable"
        return "undetermined", detail
    if operator == "time_recent":
        status, time_detail = time_recent_matches(value, now, recent_seconds)
        detail.update(time_detail)
        return status, detail
    if operator == "bool_equals":
        detail["expected"] = leaf.get("expected")
        detail["actual"] = value
        if not isinstance(value, bool):
            detail["reason"] = "value_not_boolean"
            return "undetermined", detail
        return ("pass" if value is leaf["expected"] else "fail"), detail
    if not isinstance(value, str):
        detail["actual"] = value
        detail["reason"] = "value_not_text"
        return "undetermined", detail
    detail["expected"] = leaf.get("expected")
    detail["actual"] = value
    expected = leaf.get("expected") or ""
    if operator == "equals":
        return ("pass" if value == expected else "fail"), detail
    if operator == "contains":
        return ("pass" if expected in value else "fail"), detail
    if operator == "not_contains":
        return ("pass" if expected not in value else "fail"), detail
    if operator == "regex":
        try:
            return ("pass" if re.search(expected, value) else "fail"), detail
        except re.error as exc:
            detail["reason"] = f"invalid_regex: {exc}"
            return "undetermined", detail
    detail["reason"] = f"unsupported_operator: {operator}"
    return "undetermined", detail


def _row_value(row: Mapping[str, Any], field: str,
               component_key: str | None = None) -> Any:
    # P2e：叶带 componentKey → 按组件稳定键取样本（componentValues，
    # 不同身份同 field 分别评估）；无 key 旧草稿回退 values[field]。
    if component_key:
        # 叶带 key=身份锚定：样本缺失=None（不回退 values[field]——
        # 否则旧通道覆盖值会把缺失键判成别的组件样本）。
        if isinstance(row, Mapping) and isinstance(
                row.get("componentValues"), Mapping):
            return row["componentValues"].get(component_key)
        return None
    if isinstance(row, Mapping) and isinstance(row.get("values"), Mapping):
        return row["values"].get(field)
    return row.get(field) if isinstance(row, Mapping) else None


def _row_index(row: Mapping[str, Any], fallback: int) -> int:
    for key in ("rowIndex", "index"):
        if isinstance(row, Mapping) and isinstance(row.get(key), int):
            return int(row[key])
    return fallback


def _evaluate_row(
    predicate_children: Iterable[Mapping[str, Any]],
    op: str,
    row: Mapping[str, Any],
    row_index: int,
    *,
    now: datetime,
    recent_seconds: float,
) -> dict:
    results = []
    for leaf in predicate_children:
        if is_subtree(leaf):
            sub = _evaluate_row(
                leaf.get("children") or (), leaf.get("op", "all"),
                row, row_index, now=now, recent_seconds=recent_seconds,
            )
            results.append({
                "subtree": True, "op": leaf.get("op"),
                "result": sub["result"], "children": sub["conditions"],
            })
            continue
        status, detail = evaluate_leaf(
            leaf, _row_value(row, str(leaf.get("field")),
                             leaf.get("componentKey")),
            now=now, recent_seconds=recent_seconds,
        )
        results.append({**detail, "result": status})
    statuses = [item["result"] for item in results]
    if op == "all":
        matched = all(status == "pass" for status in statuses)
        undetermined = (not matched) and any(status == "undetermined" for status in statuses)
    else:
        matched = any(status == "pass" for status in statuses)
        undetermined = (not matched) and all(status == "undetermined" for status in statuses)
    return {
        "rowIndex": _row_index(row, row_index),
        "rowSnapshot": dict(row) if isinstance(row, Mapping) else {"raw": str(row)},
        "conditions": results,
        "matched": matched,
        "undetermined": undetermined,
        "result": "pass" if matched else ("undetermined" if undetermined else "fail"),
    }


def evaluate_region_conditions(
    conditions: Mapping[str, Any],
    rows: Any,
    now_wall: datetime,
    recent_seconds: float,
    *,
    quantifier: str = "any",
) -> dict:
    """Evaluate the condition tree against one sample's rows.

    同帧行级合取 (06 §3): every condition of one row must come from the same
    snapshot — the caller hands over exactly one sample's rows and this
    function never merges values across rows or across samples.  Returns a
    structured result whose per-row evidence (row snapshot, per-condition
    outcome, raw reads) is what the replay report persists for traceability.

    ``rows`` may be ``{"ok": False, "error": ...}`` (unreadable sample →
    undetermined), a list of row mappings, or a mapping carrying ``rows``.
    """
    if not isinstance(now_wall, datetime) or now_wall.tzinfo is None:
        raise RegionDraftError("now_wall must be a timezone-aware datetime",
                               path="now_wall")
    unreadable: str | None = None
    if isinstance(rows, Mapping) and not isinstance(rows.get("rows"), (list, tuple)):
        unreadable = str(rows.get("error") or "container_unreadable")
        row_items: list[Any] = []
    elif isinstance(rows, Mapping):
        row_items = list(rows.get("rows") or [])
    elif isinstance(rows, (list, tuple)):
        row_items = list(rows)
    else:
        unreadable = "rows_payload_invalid"
        row_items = []

    root_op = conditions.get("op", "all") if isinstance(conditions, Mapping) else "all"
    children = list(conditions.get("children") or ()) if isinstance(conditions, Mapping) else []
    count_leaf = next(
        (child for child in children
         if isinstance(child, Mapping) and child.get("operator") == "count"),
        None,
    )
    predicate = [child for child in children if child is not count_leaf]
    predicate_op = root_op

    row_evidences = [
        _evaluate_row(predicate, predicate_op, row, index,
                      now=now_wall, recent_seconds=recent_seconds)
        for index, row in enumerate(row_items)
    ]
    match_count = sum(1 for item in row_evidences if item["matched"])
    any_undetermined_row = any(item["undetermined"] for item in row_evidences)

    result: str
    if unreadable is not None:
        result = "undetermined"
    elif count_leaf is not None:
        spec = count_leaf.get("count") or {}
        value = int(spec.get("value") or 0)
        op = spec.get("op", "at_least")
        # Unknown rows may or may not match.  Only decide when every possible
        # count in [known matches, known matches + unknown rows] agrees.
        maximum = match_count + sum(item["undetermined"] for item in row_evidences)
        if op == "equals":
            result = ("fail" if value < match_count or value > maximum else
                      "pass" if match_count == maximum == value else "undetermined")
        elif op == "at_least":
            result = ("pass" if match_count >= value else
                      "fail" if maximum < value else "undetermined")
        else:  # at_most
            result = ("fail" if match_count > value else
                      "pass" if maximum <= value else "undetermined")
    elif quantifier == "any":
        result = "pass" if match_count >= 1 else (
            "undetermined" if any_undetermined_row else "fail")
    else:  # quantifier == "all"
        if not row_evidences:
            result = "undetermined"  # 空集合无法证明“所有行满足”
        elif any_undetermined_row:
            result = "undetermined"
        else:
            result = "pass" if match_count == len(row_evidences) else "fail"

    return {
        "result": result,
        "quantifier": quantifier,
        "rowCount": len(row_evidences),
        "matchCount": match_count,
        "countCondition": count_leaf.get("count") if isinstance(count_leaf, Mapping) else None,
        "matchedRows": [item for item in row_evidences if item["matched"]],
        "rowEvidences": row_evidences,
        "unreadable": unreadable,
        "nowWall": now_wall.isoformat(),
        "recentSeconds": float(recent_seconds),
    }


__all__ = [
    "SCHEMA_VERSION", "KIND", "TARGET_MODES", "QUANTIFIERS", "LEAF_FIELDS",
    "RegionDraftError",
    "build_region_draft", "validate_region_draft", "draft_for_persistence",
    "render_region_summary",
    "validate_condition_tree", "validate_condition_leaf",
    "validate_content_time", "validate_observation", "validate_binding",
    "validate_component_identity",
    "parse_content_time", "time_recent_matches", "evaluate_leaf",
    "evaluate_region_conditions",
]
