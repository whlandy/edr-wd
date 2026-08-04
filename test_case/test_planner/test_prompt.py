"""
test_prompt.py — P3.1 Commit G (prompt.py) unit tests.

Covers D15 (per-transport prompt sanitisation contract)
and FR-P3.1-04 (planner guidance prefers semantic actions
over coordinate fallbacks).

Test classes:

  TestPromptTransport          — enum boundaries
  TestEscapePlainText          — D15 plain-text escaper
  TestEscapeForTransport       — D15 boundary dispatch
  TestRenderSystemPrompt       — system message assembly
  TestRenderUserPrompt         — user message sanitisation
  TestRenderPrompt             — full pipeline
  TestHashPrompt               — D16 persistence contract
  TestAuditPrompt              — D15 secret-leakage audit
  TestSanitisationBoundaries   — D15 "trust boundaries not merged"
  TestLayerBoundary            — pure policy, no I/O, no LLM dispatch
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.planner.prompt import (
    PLANNER_PROMPT_TEMPLATE_VERSION,
    PromptTransport,
    RenderedPrompt,
    SYSTEM_GUIDANCE,
    USER_GUIDANCE,
    _escape_plain_text,
    audit_prompt,
    hash_prompt,
    render_prompt,
    render_system_prompt,
    render_user_prompt,
)

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def actions() -> list[dict]:
    return [
        {
            "action_id": "gui.click",
            "description": "Click on a target element",
            "input_schema": {"type": "object"},
            "requires": ("target_ref",),
            "side_effect": "navigation",
            "risk": "low",
            "rollback_class": "none",
            "preferred_over": ("pointer.click",),
        },
        {
            "action_id": "pointer.click",
            "description": "Click at absolute coordinates",
            "input_schema": {"type": "object"},
            "requires": ("coordinates",),
            "side_effect": "navigation",
            "risk": "medium",
            "rollback_class": "none",
            "preferred_over": (),
        },
    ]


# ---------------------------------------------------------------------------
# TestPromptTransport
# ---------------------------------------------------------------------------


class TestPromptTransport:
    def test_four_transports(self) -> None:
        assert {t.value for t in PromptTransport} == {
            "markdown",
            "json",
            "structured",
            "plain",
        }

    def test_enum_is_str_compatible(self) -> None:
        # Used as dict keys and persisted to events.
        assert PromptTransport.MARKDOWN.value == "markdown"


# ---------------------------------------------------------------------------
# TestEscapePlainText
# ---------------------------------------------------------------------------


class TestEscapePlainText:
    def test_plain_text_unchanged(self) -> None:
        assert _escape_plain_text("hello world") == "hello world"

    def test_newline_replaced_with_space(self) -> None:
        assert _escape_plain_text("line1\nline2") == "line1 line2"

    def test_carriage_return_replaced_with_space(self) -> None:
        assert _escape_plain_text("line1\rline2") == "line1 line2"

    def test_crlf_replaced_with_single_space(self) -> None:
        # Per the rule: a single newline replacement, not double.
        assert _escape_plain_text("line1\r\nline2") == "line1 line2"

    def test_tab_replaced_with_space(self) -> None:
        assert _escape_plain_text("col1\tcol2") == "col1 col2"

    def test_nul_stripped(self) -> None:
        assert _escape_plain_text("hello\x00world") == "helloworld"

    def test_bel_stripped(self) -> None:
        assert _escape_plain_text("hello\x07world") == "helloworld"

    def test_esc_stripped(self) -> None:
        assert _escape_plain_text("hello\x1bworld") == "helloworld"

    def test_no_backslash_escaping(self) -> None:
        # Plain-text transport must NOT add backslashes —
        # the LLM does not parse escapes.
        assert _escape_plain_text("a|b") == "a|b"
        assert _escape_plain_text("a`b") == "a`b"
        assert _escape_plain_text("a\\b") == "a\\b"

    def test_unicode_preserved(self) -> None:
        # Non-ASCII codepoints (e.g. Chinese text) preserved
        # verbatim in plain-text transport.
        assert _escape_plain_text("确认") == "确认"


# ---------------------------------------------------------------------------
# TestEscapeForTransport
# ---------------------------------------------------------------------------


class TestEscapeForTransport:
    """D15 boundary dispatch.

    Per D15: each transport has its own encoder. The
    dispatch MUST route correctly and reject non-enum
    transport values.
    """

    def test_markdown_uses_escape_markdown(self) -> None:
        from agent.planner.prompt import _escape_for_transport
        # Pipe must be escaped.
        assert (
            _escape_for_transport("a|b", PromptTransport.MARKDOWN)
            == "a\\|b"
        )

    def test_json_uses_json_dumps_inner(self) -> None:
        from agent.planner.prompt import _escape_for_transport
        # JSON transport: surrounding quotes are stripped, so
        # json.dumps("a\"b") = '"a\"b"' → "a\"b".
        out = _escape_for_transport('a"b', PromptTransport.JSON)
        assert out == 'a\\"b'

    def test_structured_returns_verbatim(self) -> None:
        from agent.planner.prompt import _escape_for_transport
        # Structured-output-schema transport: no escape.
        assert (
            _escape_for_transport("a|b\"c", PromptTransport.STRUCTURED)
            == "a|b\"c"
        )

    def test_plain_strips_control_chars(self) -> None:
        from agent.planner.prompt import _escape_for_transport
        assert (
            _escape_for_transport("a\nb", PromptTransport.PLAIN)
            == "a b"
        )

    def test_non_enum_transport_rejected(self) -> None:
        from agent.planner.prompt import _escape_for_transport
        with pytest.raises(TypeError, match="PromptTransport"):
            _escape_for_transport("x", "markdown")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestRenderSystemPrompt
# ---------------------------------------------------------------------------


class TestRenderSystemPrompt:
    def test_includes_guidance(self, actions: list[dict]) -> None:
        prompt = render_system_prompt(actions)
        assert SYSTEM_GUIDANCE.strip() in prompt

    def test_includes_action_ids(self, actions: list[dict]) -> None:
        prompt = render_system_prompt(actions)
        assert "gui.click" in prompt
        assert "pointer.click" in prompt

    def test_includes_descriptions(self, actions: list[dict]) -> None:
        prompt = render_system_prompt(actions)
        assert "Click on a target element" in prompt
        assert "Click at absolute coordinates" in prompt

    def test_includes_schema_ref(self, actions: list[dict]) -> None:
        prompt = render_system_prompt(
            actions, schema_ref="test_case/schema/plan.schema.json"
        )
        assert "test_case/schema/plan.schema.json" in prompt

    def test_includes_required_prefer_semantic_guidance(
        self, actions: list[dict]
    ) -> None:
        # FR-P3.1-04: prefer semantic actions.
        prompt = render_system_prompt(actions)
        assert "PREFER semantic actions" in prompt
        assert "pointer.* fallbacks" in prompt
        assert "NEVER choose a coordinate" in prompt

    def test_includes_requires_contract_guidance(
        self, actions: list[dict]
    ) -> None:
        prompt = render_system_prompt(actions)
        assert "NEVER bypass the requires contract" in prompt

    def test_includes_transition_expected_guidance(
        self, actions: list[dict]
    ) -> None:
        prompt = render_system_prompt(actions)
        assert "transition.expected" in prompt
        assert "on_error" in prompt

    def test_descriptions_escaped_for_markdown(
        self, actions: list[dict]
    ) -> None:
        # Mutate a description to include a pipe — must be escaped.
        actions[0]["description"] = "Click|a|target"
        prompt = render_system_prompt(actions)
        assert "Click\\|a\\|target" in prompt

    def test_renderer_structure_not_escaped(
        self, actions: list[dict]
    ) -> None:
        # Renderer-controlled field names (action_id, requires, etc.)
        # are NOT escaped. The fixed structure survives.
        prompt = render_system_prompt(actions)
        # `- action_id: gui.click` literal structure.
        assert "- action_id: gui.click" in prompt
        # `requires: [...]` literal structure.
        assert "requires: ['target_ref']" in prompt

    def test_empty_actions(self) -> None:
        prompt = render_system_prompt([])
        assert "# Available Actions" in prompt


# ---------------------------------------------------------------------------
# TestRenderUserPrompt
# ---------------------------------------------------------------------------


class TestRenderUserPrompt:
    def test_basic_render(self) -> None:
        out = render_user_prompt(
            snapshot_id="snap-1", target="case_x", profile="windows_hisec"
        )
        assert "snap-1" in out
        assert "case_x" in out
        assert "windows_hisec" in out

    def test_with_hints(self) -> None:
        out = render_user_prompt(
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
            hints={"reason": "open dialog", "step": "1"},
        )
        assert "# Hints" in out
        assert "reason: open dialog" in out
        assert "step: 1" in out

    def test_user_controlled_target_sanitised_markdown(self) -> None:
        # Target comes from case files — user-controlled.
        out = render_user_prompt(
            snapshot_id="snap-1",
            target="evil|tgt",
            profile="windows_hisec",
        )
        # Pipe MUST be escaped in markdown transport.
        assert "evil\\|tgt" in out
        assert "evil|tgt" not in out

    def test_user_controlled_hints_sanitised_markdown(self) -> None:
        out = render_user_prompt(
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
            hints={"reason": "open|dialog"},
        )
        assert "open\\|dialog" in out

    def test_newline_in_hint_replaced(self) -> None:
        out = render_user_prompt(
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
            hints={"reason": "multi\nline"},
        )
        assert "multi line" in out
        assert "multi\nline" not in out

    def test_transport_plain(self) -> None:
        out = render_user_prompt(
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
            hints={"reason": "multi\nline"},
            transport=PromptTransport.PLAIN,
        )
        # Plain-text transport: newline → space, no backslash.
        assert "multi line" in out


# ---------------------------------------------------------------------------
# TestRenderPrompt
# ---------------------------------------------------------------------------


class TestRenderPrompt:
    def test_returns_rendered_prompt(
        self, actions: list[dict]
    ) -> None:
        rp = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
        )
        assert isinstance(rp, RenderedPrompt)
        assert rp.system
        assert rp.user
        assert rp.prompt_hash.startswith("sha256:")
        assert rp.template_version == PLANNER_PROMPT_TEMPLATE_VERSION

    def test_prompt_hash_stable(self, actions: list[dict]) -> None:
        rp1 = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
        )
        rp2 = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="case_x",
            profile="windows_hisec",
        )
        assert rp1.prompt_hash == rp2.prompt_hash

    def test_prompt_hash_changes_with_target(self, actions: list[dict]) -> None:
        rp1 = render_prompt(
            actions, snapshot_id="snap-1", target="case_x", profile="p"
        )
        rp2 = render_prompt(
            actions, snapshot_id="snap-1", target="case_y", profile="p"
        )
        assert rp1.prompt_hash != rp2.prompt_hash

    def test_prompt_hash_changes_with_transport(
        self, actions: list[dict]
    ) -> None:
        rp_md = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="case_x|pipe",
            profile="p",
            transport=PromptTransport.MARKDOWN,
        )
        rp_struct = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="case_x|pipe",
            profile="p",
            transport=PromptTransport.STRUCTURED,
        )
        # Markdown escapes the pipe; structured does not.
        # Hashes MUST differ.
        assert rp_md.prompt_hash != rp_struct.prompt_hash


# ---------------------------------------------------------------------------
# TestHashPrompt
# ---------------------------------------------------------------------------


class TestHashPrompt:
    def test_format_sha256(self) -> None:
        h = hash_prompt("hello")
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64  # SHA-256 hex

    def test_stable(self) -> None:
        assert hash_prompt("hello") == hash_prompt("hello")

    def test_different_inputs_differ(self) -> None:
        assert hash_prompt("hello") != hash_prompt("world")

    def test_unicode_unicode(self) -> None:
        # Hash is computed over utf-8 bytes.
        h1 = hash_prompt("确认")
        h2 = hash_prompt("确认")
        assert h1 == h2


# ---------------------------------------------------------------------------
# TestAuditPrompt
# ---------------------------------------------------------------------------


class TestAuditPrompt:
    def test_no_registry_returns_empty(self) -> None:
        assert audit_prompt("hello world", registry=None) == []

    def test_clean_prompt_no_matches(self) -> None:
        from agent.redaction import default_registry
        registry = default_registry()
        result = audit_prompt("just plain text", registry=registry)
        assert result == []

    def test_secret_in_prompt_detected(self) -> None:
        # Build a registry that flags AWS access keys.
        from agent.redaction import RedactionRegistry
        from agent.trace.redaction import TextRule
        registry = RedactionRegistry(
            rules=(
                TextRule(
                    rule_id="aws-access-key",
                    pattern=r"AKIA[0-9A-Z]{16}",
                ),
            ),
        )
        # A "prompt" containing a fake AWS key.
        bad = "Hello AKIAIOSFODNN7EXAMPLE world"
        fired = audit_prompt(bad, registry=registry)
        assert "aws-access-key" in fired

    def test_registry_without_apply_text_rejected(self) -> None:
        class Bad:
            pass
        with pytest.raises(TypeError, match="apply_text"):
            audit_prompt("x", registry=Bad())


# ---------------------------------------------------------------------------
# TestSanitisationBoundaries (D15 "trust boundaries not merged")
# ---------------------------------------------------------------------------


class TestSanitisationBoundaries:
    """D15: prompt sanitisation ≠ markdown escape ≠ trace escape.

    A single helper that "does all escaping" is a reject.
    Each transport has its own entry point; the
    escapers may share low-level primitives but the
    entry points MUST be separate.
    """

    def test_markdown_transport_escapes_pipe(self) -> None:
        # Markdown transport escapes | and `.
        prompt = render_system_prompt(
            [{"action_id": "x", "description": "a|b"}]
        )
        assert "a\\|b" in prompt

    def test_structured_transport_does_not_escape_pipe(self) -> None:
        # Structured transport does not escape — the LLM JSON parser
        # handles it.
        prompt = render_system_prompt(
            [{"action_id": "x", "description": "a|b"}],
            transport=PromptTransport.STRUCTURED,
        )
        assert "a|b" in prompt
        assert "a\\|b" not in prompt

    def test_plain_transport_does_not_add_backslashes(self) -> None:
        prompt = render_system_prompt(
            [{"action_id": "x", "description": "a|b"}],
            transport=PromptTransport.PLAIN,
        )
        assert "a|b" in prompt
        assert "a\\|b" not in prompt

    def test_distinct_encoders_per_transport(self) -> None:
        # The four transports use DIFFERENT escape functions.
        # Verify by passing the same input through each and
        # checking outputs are distinct (for inputs containing
        # escape-significant characters).
        from agent.planner.prompt import _escape_for_transport
        s = "a|b`c\"d\ne"

        md = _escape_for_transport(s, PromptTransport.MARKDOWN)
        js = _escape_for_transport(s, PromptTransport.JSON)
        st = _escape_for_transport(s, PromptTransport.STRUCTURED)
        pl = _escape_for_transport(s, PromptTransport.PLAIN)

        # Markdown: pipe and backtick escaped, newline replaced.
        assert md != s
        # JSON: quotes escaped.
        assert js != s
        # Structured: verbatim.
        assert st == s
        # Plain: newline replaced.
        assert pl != s

        # Markdown, JSON, and Plain must all differ from each other
        # for this input (a/b/c are escape-significant in at least
        # one of them).
        assert len({md, js, st, pl}) >= 3


# ---------------------------------------------------------------------------
# TestLayerBoundary (P3.1 architecture §7.2)
# ---------------------------------------------------------------------------


class TestLayerBoundary:
    """P3.1.G layer boundary:

      * Pure policy (no I/O, no LLM dispatch, no mutation).
      * No executor / catalog-write / persistence imports.
      * Reuses ``escape_markdown`` from ``agent.trace``
        (a low-level primitive) but does NOT call into the
        trace rendering pipeline.
    """

    def test_no_llm_dispatch(self) -> None:
        import agent.planner.prompt as mod
        src = open(mod.__file__).read()
        # No network or LLM SDK imports.
        for forbidden in (
            "openai", "anthropic", "requests", "urllib", "socket",
            "anthropic.", "openai.", "litellm", "ollama",
        ):
            assert forbidden not in src, (
                f"prompt.py must not import {forbidden!r}"
            )

    def test_no_persistence_writes(self) -> None:
        import agent.planner.prompt as mod
        src = open(mod.__file__).read()
        # No filesystem writes, no persistence calls.
        for forbidden in ("pathlib", "open(", "write_text", ".write("):
            assert forbidden not in src, (
                f"prompt.py must not do I/O ({forbidden!r})"
            )

    def test_no_executor_import(self) -> None:
        import agent.planner.prompt as mod
        src = open(mod.__file__).read()
        # The planner prompt must NOT depend on the executor;
        # they are separate layers.
        for forbidden in (
            "agent.execution.executor",
            "agent.execution.atomic",
            "agent.execution.confirmation",
            "target.action_dispatcher",
        ):
            assert forbidden not in src, (
                f"prompt.py must not import {forbidden!r}"
            )

    def test_does_not_mutate_inputs(self) -> None:
        # Render twice with the same actions; hashes match.
        actions: list[dict] = [
            {"action_id": "x", "description": "click|a|target"}
        ]
        rp1 = render_prompt(
            actions, snapshot_id="snap-1", target="t", profile="p"
        )
        rp2 = render_prompt(
            actions, snapshot_id="snap-1", target="t", profile="p"
        )
        assert rp1.prompt_hash == rp2.prompt_hash
        # Input dict not mutated.
        assert actions[0]["description"] == "click|a|target"

    def test_render_prompt_does_not_dispatch(self) -> None:
        # render_prompt returns RenderedPrompt; it never
        # calls anything that returns an LLM response.
        rp = render_prompt(
            [{"action_id": "x", "description": "y"}],
            snapshot_id="snap-1",
            target="t",
            profile="p",
        )
        # No "response" / "completion" / "messages" attributes.
        for forbidden in ("response", "completion", "messages"):
            assert not hasattr(rp, forbidden), (
                f"RenderedPrompt should not carry {forbidden!r}"
            )


# ---------------------------------------------------------------------------
# TestTemplateVersion (D16 contract)
# ---------------------------------------------------------------------------


class TestTemplateVersion:
    def test_template_version_constant(self) -> None:
        assert PLANNER_PROMPT_TEMPLATE_VERSION == "planner-prompt-v1"

    def test_rendered_prompt_carries_version(
        self, actions: list[dict]
    ) -> None:
        rp = render_prompt(
            actions,
            snapshot_id="snap-1",
            target="t",
            profile="p",
        )
        assert rp.template_version == "planner-prompt-v1"