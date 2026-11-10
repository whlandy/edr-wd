"""Server-tools transport for the controlled v3 replay adapter.

`execute_v3_window_stage_real` drives the real target.server tool chain
through an injected `server_tools` object.  In-process tests pass
`target.server` itself; MCP-driven replays (live 11.26 target, recovery
loops) need the same chain across HTTP.  Before this module every caller
hand-rolled its own shim — `tmp/v3_replay_driver.py` and its `-r3` copy —
so the parameter order, the None-filter whitelist, and the facade shim
drifted per copy.  One transport, two carriers:

- `InProcessServerTools` — the imported `target.server` module (tests,
  same-machine replays).  Exercised by the whole existing v3 test set.
- `MCPServerTools` — JSON-RPC `tools/call` over the target-local MCP HTTP
  endpoint.  Tool JSON text comes back byte-compatible with the
  in-process return shape, which is what the adapter's `json.loads`
  contract expects.

`derive_role_map` lives here too: the golden's window identities and the
installed product's role patterns determine the W→role authorization map
the adapter demands, so callers no longer hand-write
`{"W1": "main", "W6": "log_center"}` per script (the copy I shipped in
the r3 driver was exactly that kind of drift).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

__all__ = [
    "MCPServerTools",
    "InProcessServerTools",
    "derive_role_map",
    "RoleDerivationError",
]

#: Positional order per tool, for callers that pass positional args.
_TOOL_POSITIONAL: dict[str, list[str]] = {
    "observe_product_windows": ["run_token"],
    "begin_product_run": ["profile_id", "digest", "lease_seconds"],
    "end_product_run": ["run_token"],
    "bind_entry_window": ["window_id", "identity", "run_token",
                          "control_selector"],
    "register_window_declaration": ["declarations_json", "run_token"],
    "focus_bound_window": ["window_id", "binding_token", "run_token",
                           "action_id", "action_args", "request_id",
                           "target_ref", "observe"],
}

#: Keys the adapter passes as None on purpose; the MCP carrier must still
#: forward them (FastMCP validates required/optional per tool).
_NONE_PASSTHROUGH = frozenset({
    "control_selector", "observe", "action_id", "action_args",
    "request_id", "target_ref",
})


class _BindingFacadeShim:
    """Read-only binding-facade stand-in: `step_context` resolves the
    authoritative revision through MCP `reverify_bound_window` (run
    consistency is guaranteed by the MCP run_token)."""

    def __init__(self, tools: "MCPServerTools"):
        self._tools = tools

    def step_context(self, *, run_id: str, step_id: str, attempt_id: str,
                     window_id: str, binding_token: str) -> dict:
        raw = self._tools.call(
            "reverify_bound_window",
            {"window_id": window_id, "binding_token": binding_token,
             "run_token": self._tools.run_token})
        doc = json.loads(raw)
        if not doc.get("binding_token"):
            raise ValueError(f"{window_id}: reverify refused: {raw[:200]}")
        return {"bindingRevision": doc.get("revision")}


class MCPServerTools:
    """Attribute access forwards to MCP `tools/call`; returns the tool's
    JSON text unchanged — the adapter treats it exactly like the
    in-process tool's return value.

    `run_token` is captured from `begin_product_run`'s response, matching
    the adapter's begin-then-use flow.
    """

    def __init__(self, transport: Callable[[str, dict], str]):
        self._transport = transport
        self.run_token: str | None = None
        self._binding_facade_holder = {"facade": _BindingFacadeShim(self)}

    def call(self, name: str, arguments: dict) -> str:
        raw = self._transport(name, arguments)
        if name == "begin_product_run":
            try:
                doc = json.loads(raw)
                self.run_token = doc.get("run_token")
            except (TypeError, ValueError):
                pass
        return raw

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*args: Any, **kwargs: Any) -> str:
            params = dict(kwargs)
            for value, key in zip(args, _TOOL_POSITIONAL.get(name, [])):
                params.setdefault(key, value)
            params = {k: v for k, v in params.items()
                      if v is not None or k in _NONE_PASSTHROUGH}
            return self.call(name, params)

        return _call


class InProcessServerTools:
    """Wraps the imported `target.server` module so the adapter can accept
    either carrier transparently.  Plain attribute forwarding — the
    in-process tools already return the JSON text the adapter expects."""

    def __init__(self, server_module: Any):
        self._server = server_module

    def __getattr__(self, name: str):
        return getattr(self._server, name)


class RoleDerivationError(ValueError):
    """The golden's window identities cannot be mapped to the installed
    product's roles unambiguously.  Fail closed — a hand-written map may
    only override, never paper over, this refusal."""


def _literal_core(title_regex: str) -> str:
    """Strip the compiler's anchoring and literal escapes (`\\X` → `X`),
    mirroring `_role_compatible_identity`'s reading of a recorded title."""
    core = title_regex
    if core.startswith("^") and core.endswith("$") and len(core) >= 2:
        core = core[1:-1]
    return re.sub(r"\\(.)", r"\1", core)


def derive_role_map(golden: Mapping[str, Any], capability: Any) -> dict[str, str]:
    """Derive the W→role authorization map from the golden's declared
    window identities and the installed product's role patterns.

    Matching rules, in order:
    1. `rootAutomationId` in the identity must appear in the role's
       `root_control_ids` when the role declares any — that is exact
       evidence and wins outright.
    2. Otherwise the identity's literal title core must fullmatch the
       role's `title_pattern`, and the role's `process_names` must
       include the identity's `processName`.
    3. Several roles matching one window is a derivation failure unless
       one match is strictly more specific; the installed hisec profile's
       `entry`/`main` split shares `logo1`, so the window is genuinely
       ambiguous to a title matcher — refuse and let the caller supply an
       explicit map (the same fail-closed contract
       `_role_compatible_identity` enforces downstream).

    Windows the golden declares but no role matches also refuse: the
    adapter validates every W against its role, so an unmapped W would
    only fail later, after begin/register side effects.
    """
    sequence = (golden.get("windowSequence") or {}) if isinstance(golden, Mapping) else {}
    windows = sequence.get("windows") or []
    if not windows:
        raise RoleDerivationError(
            "golden has no windowSequence windows; derive_role_map needs "
            "v3 identity evidence")
    roles = {r.role: r for r in capability.window_roles}
    role_map: dict[str, str] = {}
    for window in windows:
        wid = window.get("id")
        identity = window.get("identity") or {}
        process_name = str(identity.get("processName") or "")
        root = identity.get("rootAutomationId")
        title_core = _literal_core(str(identity.get("titleRegex") or "")) \
            if identity.get("titleRegex") else None
        matches: list[str] = []
        for name, role in roles.items():
            declared_roots = tuple(role.root_control_ids or ())
            if declared_roots:
                if root and root in declared_roots:
                    matches.append(name)
                continue
            if role.process_names and process_name not in role.process_names:
                continue
            if not role.title_pattern or not title_core:
                continue
            if re.fullmatch(role.title_pattern, title_core):
                matches.append(name)
        if len(matches) == 1:
            role_map[wid] = matches[0]
        elif not matches:
            raise RoleDerivationError(
                f"{wid}: no installed product role matches identity "
                f"{json.dumps(identity, ensure_ascii=False)}")
        else:
            raise RoleDerivationError(
                f"{wid}: identity {json.dumps(identity, ensure_ascii=False)} "
                f"matches several roles {sorted(matches)}; supply an "
                "explicit window_role_map override")
    return role_map
