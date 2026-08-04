"""prompt.py — Planner prompt template and sanitisation (P3.1 Commit G).

Implements P3.1 design gate **D15** (per-transport prompt
sanitisation contract) and **FR-P3.1-04** (planner
guidance prefers semantic actions over coordinate
fallbacks).

This module is responsible for **building and rendering**
the planner prompt that is sent to the LLM provider. The
transport-specific encoding rules from D15 are enforced
here:

  * Markdown-table-cell transport — reuses
    ``agent.trace.markdown_escape.escape_markdown`` (P2.4.D).
  * JSON-string transport — uses ``json.dumps`` (built-in
    JSON escape; the LLM's JSON parser handles strings).
  * Structured-output-schema transport — no extra escape;
    the LLM provider's JSON parser handles strings and the
    schema validator handles structure.
  * Plain-text transport — uses the transport-specific
    escaper introduced here (``_escape_plain_text``).

**Trust boundary** (D15):

  prompt sanitisation (planner surface, this module) is
  **distinct from** markdown escape (``trace.md`` rendering)
  and trace render escape (``agent.trace`` internal use).
  A single helper that "does all escaping" is a reject
  per D15; per-transport encoders are required.

**Layer boundary** (architecture §7.2):

  * This module does **NOT** call the LLM provider. It
    only builds and renders the prompt.
  * This module does **NOT** mutate inputs (snapshot,
    request, hints). All sanitisation produces new
    strings.
  * This module does **NOT** persist the prompt. It
    exposes a ``hash_prompt()`` helper for the caller
    to record the digest per D16 (audit).

**Audit contract** (D15 / FR-P3.1-09):

  The rendered prompt MUST NOT contain any string that
  matches a registered redaction rule. ``audit_prompt``
  surfaces the fired rule_ids so the caller can fail
  fast in CI.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.trace.markdown_escape import escape_markdown


# ---------------------------------------------------------------------------
# Public enums
# ---------------------------------------------------------------------------


class PromptTransport(enum.Enum):
    """Prompt transport identifier (D15).

    The encoding rules in D15 are tied to the transport.
    Each transport has its own encoder; passing a
    non-matching encoder for a given transport is a
    contract violation.
    """

    MARKDOWN = "markdown"
    JSON = "json"
    STRUCTURED = "structured"
    PLAIN = "plain"


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


PLANNER_PROMPT_TEMPLATE_VERSION: str = "planner-prompt-v1"

# The renderer-generated system guidance. **NEVER escaped** —
# it is renderer-controlled text. Per D15 boundary rule.
SYSTEM_GUIDANCE: str = (
    "You are a planner that emits a structured action sequence.\n"
    "\n"
    "Rules:\n"
    "  - Use ONLY the actions listed under # Available Actions.\n"
    "  - PREFER semantic actions (gui.click, gui.type_text, gui.select) "
    "over pointer.* fallbacks. NEVER choose a coordinate action when a "
    "unique semantic target exists in the latest snapshot.\n"
    "  - Each step MUST declare transition.expected and on_error.\n"
    "  - NEVER bypass the requires contract "
    "(connected_window, window_lock, target_ref).\n"
    "  - Output MUST conform to the JSON Schema under # Schema.\n"
)

USER_GUIDANCE: str = (
    "Latest snapshot_id: {snapshot_id}\n"
    "Target: {target}\n"
    "Profile: {profile}\n"
)


# ---------------------------------------------------------------------------
# Prompt hash + audit (D16 contract)
# ---------------------------------------------------------------------------


def hash_prompt(prompt_text: str) -> str:
    """Return the SHA-256 digest of the rendered prompt.

    Per D16, the planner MUST persist a **hash** of the
    rendered prompt (not the prompt itself) for
    reproducibility audit. The hash is a stable,
    deterministic handle; collisions are negligible for
    SHA-256 across realistic prompt sizes.
    """
    return "sha256:" + hashlib.sha256(
        prompt_text.encode("utf-8")
    ).hexdigest()


def audit_prompt(
    prompt_text: str,
    registry: Any | None = None,
) -> list[str]:
    """Audit a rendered prompt for secret leakage (D15).

    Returns the list of rule_ids whose patterns matched
    anywhere in the rendered prompt. Empty list == clean.

    ``registry`` is expected to be an instance of
    ``agent.redaction.RedactionRegistry`` with
    ``.apply_text``. For test isolation we accept ``None``
    and return an empty list (no rules available).
    """
    if registry is None:
        return []
    if not hasattr(registry, "apply_text"):
        raise TypeError(
            "registry must provide apply_text(); got "
            f"{type(registry).__name__}"
        )
    _redacted, fired = registry.apply_text(prompt_text)
    return list(fired)


# ---------------------------------------------------------------------------
# Transport-specific encoders (D15)
# ---------------------------------------------------------------------------


def _escape_plain_text(value: str) -> str:
    """Escape a user-controlled value for plain-text prompts.

    Plain-text prompts are sent as raw strings to the LLM
    provider without a structured schema or markdown
    formatting. The only safety boundary is:

      1. Strip ASCII control characters (NUL, BEL, ESC,
         etc.) that some terminals interpret as command
         sequences.
      2. Strip CR/LF that would break prompt structure
         (the LLM provider concatenates system + user
         messages by separator). CRLF is collapsed to a
         single space so ``"a\\r\\nb"`` becomes ``"a b"``
         rather than ``"a  b"``.
      3. Backslash escapes are **not** added — the LLM
         does not parse escapes; adding ``\\`` would
         introduce literal backslashes in the rendered
         prompt.

    This is intentionally a thin escaper. The D15
    contract is "transport-specific"; a plain-text prompt
    is the weakest transport (no schema validation) and
    the escape surface is correspondingly small.
    """
    out_chars: list[str] = []
    pending_cr = False
    for ch in value:
        cp = ord(ch)
        if ch == "\r":
            pending_cr = True
            continue
        if ch == "\n":
            if pending_cr:
                # CRLF collapses to single space.
                out_chars.append(" ")
                pending_cr = False
            else:
                out_chars.append(" ")
            continue
        if pending_cr:
            out_chars.append(" ")
            pending_cr = False
        if ch == "\t":
            out_chars.append(" ")
            continue
        if cp <= 0x1F:
            # NUL, BEL, ESC, etc. — strip
            continue
        out_chars.append(ch)
    if pending_cr:
        out_chars.append(" ")
    return "".join(out_chars)


def _escape_for_transport(
    value: str,
    transport: PromptTransport,
) -> str:
    """Dispatch to the per-transport encoder.

    Per D15 boundary rule: each transport has its own
    entry point. Callers MUST NOT pre-encode and pass
    "already safe" strings — the renderer does NOT trust
    the caller, because boundary violations are how
    secret leakage happens in practice.
    """
    if not isinstance(transport, PromptTransport):
        raise TypeError(
            f"transport must be PromptTransport, "
            f"got {type(transport).__name__}"
        )
    if transport is PromptTransport.MARKDOWN:
        return escape_markdown(value)
    if transport is PromptTransport.JSON:
        # json.dumps produces a JSON-encoded string literal,
        # which is exactly the escape a JSON-string prompt
        # transport requires. The surrounding quotes are
        # NOT included — callers concatenate into their own
        # template.
        return json.dumps(value, ensure_ascii=False)[1:-1]
    if transport is PromptTransport.STRUCTURED:
        # Structured-output-schema transport: the LLM provider
        # validates the JSON before reading any string field.
        # No extra escape needed — return the value verbatim.
        return value
    if transport is PromptTransport.PLAIN:
        return _escape_plain_text(value)
    # Unreachable, but mypy doesn't know.
    raise ValueError(f"unknown transport: {transport!r}")


# ---------------------------------------------------------------------------
# Prompt rendering (system + user)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedPrompt:
    """Result of rendering a planner prompt.

    Fields:

      * ``system`` — the rendered system message
        (renderer-controlled structure + sanitised action
        descriptions).
      * ``user`` — the rendered user message (sanitised
        snapshot-derived hints).
      * ``prompt_hash`` — SHA-256 of ``system + user``,
        suitable for persistence (D16 audit).
      * ``template_version`` — locked at
        :data:`PLANNER_PROMPT_TEMPLATE_VERSION`. Bumping
        this version is a contract change: callers
        comparing hashes across versions MUST compare
        with the version qualifier.
    """

    system: str
    user: str
    prompt_hash: str
    template_version: str


def _render_actions_block(
    actions: Sequence[Mapping[str, Any]],
    transport: PromptTransport,
) -> str:
    """Render the "Available Actions" block of the system prompt.

    Each action's ``description`` is user-controlled (it
    comes from the catalog) and MUST be sanitised per
    D15. Renderer-generated field names (``action_id``,
    ``requires``, etc.) are NEVER escaped.
    """
    lines: list[str] = ["# Available Actions", ""]
    for entry in actions:
        action_id = str(entry.get("action_id", ""))
        description = str(entry.get("description", ""))
        requires = entry.get("requires", ())
        side_effect = str(entry.get("side_effect", ""))
        risk = str(entry.get("risk", ""))
        rollback_class = str(entry.get("rollback_class", ""))
        preferred_over = entry.get("preferred_over", ())

        # Renderer-generated structure — no escape.
        lines.append(f"- action_id: {action_id}")
        # User-controlled — MUST be sanitised.
        lines.append(
            f"  description: "
            f"{_escape_for_transport(description, transport)}"
        )
        # Renderer-generated enum-like lists — no escape.
        if requires:
            lines.append(f"  requires: {list(requires)}")
        if side_effect:
            lines.append(f"  side_effect: {side_effect}")
        if risk:
            lines.append(f"  risk: {risk}")
        if rollback_class:
            lines.append(f"  rollback_class: {rollback_class}")
        if preferred_over:
            lines.append(f"  preferred_over: {list(preferred_over)}")
        lines.append("")
    return "\n".join(lines)


def _render_schema_block(
    schema_ref: str,
    transport: PromptTransport,
) -> str:
    """Render the schema reference block.

    The ``schema_ref`` value is renderer-controlled (it's
    a path to a JSON Schema file). It is NOT escaped.
    """
    return (
        f"# Schema\n"
        f"\n"
        f"Output MUST conform to JSON Schema at {schema_ref}.\n"
    )


def render_system_prompt(
    actions: Sequence[Mapping[str, Any]],
    *,
    schema_ref: str = "test_case/schema/plan.schema.json",
    transport: PromptTransport = PromptTransport.MARKDOWN,
) -> str:
    """Render the system message for a planner call.

    The system message is renderer-controlled except for
    the action ``description`` fields, which come from
    the catalog and are sanitised per D15.

    Args:
        actions: planner tool list (from
            :func:`planner_tool_list`).
        schema_ref: renderer-controlled path to the JSON
            Schema the LLM must conform to. NOT escaped.
        transport: the prompt transport. Defaults to
            :data:`PromptTransport.MARKDOWN`.

    Returns:
        The rendered system message as a single string.
    """
    parts: list[str] = [
        SYSTEM_GUIDANCE,
        _render_actions_block(actions, transport),
        _render_schema_block(schema_ref, transport),
    ]
    return "\n".join(parts)


def render_user_prompt(
    *,
    snapshot_id: str,
    target: str,
    profile: str,
    hints: Mapping[str, str] | None = None,
    transport: PromptTransport = PromptTransport.MARKDOWN,
) -> str:
    """Render the user message for a planner call.

    The user message contains snapshot-derived context
    (snapshot_id, target, profile) and free-form hints.
    All string values flowing in are treated as
    untrusted (D15) and sanitised per transport.

    Args:
        snapshot_id: the freshest snapshot id (renderer-
            controlled today, but treated as untrusted
            per D15 "all values untrusted" rule).
        target: case target identifier (case id,
            user-controlled).
        profile: live profile name (renderer-controlled
            today, treated as untrusted).
        hints: optional free-form hints (e.g. case
            description); every value is sanitised.
        transport: prompt transport.

    Returns:
        The rendered user message.
    """
    safe_snapshot = _escape_for_transport(snapshot_id, transport)
    safe_target = _escape_for_transport(target, transport)
    safe_profile = _escape_for_transport(profile, transport)

    body = USER_GUIDANCE.format(
        snapshot_id=safe_snapshot,
        target=safe_target,
        profile=safe_profile,
    )

    if hints:
        body += "\n# Hints\n\n"
        for k, v in hints.items():
            safe_k = _escape_for_transport(str(k), transport)
            safe_v = _escape_for_transport(str(v), transport)
            body += f"- {safe_k}: {safe_v}\n"

    return body


def render_prompt(
    actions: Sequence[Mapping[str, Any]],
    *,
    snapshot_id: str,
    target: str,
    profile: str,
    hints: Mapping[str, str] | None = None,
    schema_ref: str = "test_case/schema/plan.schema.json",
    transport: PromptTransport = PromptTransport.MARKDOWN,
) -> RenderedPrompt:
    """Build the full rendered planner prompt (system + user).

    This is the entry point used by the planner when it
    needs to send a request to the LLM. It does NOT
    dispatch to the provider — the planner / executor
    does that.

    Returns:
        :class:`RenderedPrompt` with ``system``, ``user``,
        ``prompt_hash``, and ``template_version``.
    """
    system = render_system_prompt(
        actions, schema_ref=schema_ref, transport=transport
    )
    user = render_user_prompt(
        snapshot_id=snapshot_id,
        target=target,
        profile=profile,
        hints=hints,
        transport=transport,
    )
    combined = system + "\n" + user
    return RenderedPrompt(
        system=system,
        user=user,
        prompt_hash=hash_prompt(combined),
        template_version=PLANNER_PROMPT_TEMPLATE_VERSION,
    )


__all__ = [
    # enums
    "PromptTransport",
    # constants
    "PLANNER_PROMPT_TEMPLATE_VERSION",
    "SYSTEM_GUIDANCE",
    "USER_GUIDANCE",
    # dataclass
    "RenderedPrompt",
    # builders
    "render_system_prompt",
    "render_user_prompt",
    "render_prompt",
    # hashing + audit
    "hash_prompt",
    "audit_prompt",
    # transport-specific escaper (exposed for unit tests)
    "_escape_plain_text",
]