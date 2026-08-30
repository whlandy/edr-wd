# -*- coding: utf-8 -*-
r"""AppleScript string literals need AppleScript escaping, not JSON escaping.

`json.dumps` escapes `"` and `\` identically to AppleScript, which is why the
two were confused. It also escapes every non-ASCII character as `\uXXXX`, and
AppleScript has no such escape: it reads the literal characters. On a
Chinese-language product that breaks every application and window name.
"""

import sys
import types

import pytest

# The module imports pyautogui at import time, which the dev venv lacks.
if "pyautogui" not in sys.modules:
    _stub = types.ModuleType("pyautogui")
    for name in ("moveTo", "scroll", "doubleClick", "rightClick", "middleClick", "dragTo"):
        setattr(_stub, name, lambda *a, **k: None)
    sys.modules["pyautogui"] = _stub

from target.automation.macos_accessibility import _applescript_string

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["安全防护中心", "日志中心", "HiSec 终端", "Café"])
def test_non_ascii_names_survive_verbatim(name):
    """This is the case a JSON-escaped literal silently destroys."""
    quoted = _applescript_string(name)

    assert quoted == f'"{name}"'
    assert "\\u" not in quoted


def test_a_double_quote_is_escaped_so_the_literal_cannot_be_closed_early():
    assert _applescript_string('Foo"Bar') == '"Foo\\"Bar"'


def test_a_backslash_is_escaped_before_anything_else():
    assert _applescript_string("a\\b") == '"a\\\\b"'


def test_a_backslash_before_a_quote_does_not_escape_the_quote():
    """Ordering matters: escaping the quote first would leave `\\\\"` open."""
    assert _applescript_string('a\\"b') == '"a\\\\\\"b"'


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("a\nb", '"a\\nb"'), ("a\rb", '"a\\rb"'), ("a\tb", '"a\\tb"')],
)
def test_control_characters_use_applescript_escapes(raw, expected):
    assert _applescript_string(raw) == expected


def test_an_injection_attempt_stays_inside_the_literal():
    hostile = '" & (do shell script "id") & "'

    quoted = _applescript_string(hostile)

    # Every quote that could end the literal is escaped, so the payload is data.
    assert quoted.startswith('"') and quoted.endswith('"')
    assert quoted.count('"') - quoted.count('\\"') == 2


def test_no_applescript_is_built_with_json_escaping_any_more():
    """json.dumps must not reach an AppleScript literal again."""
    import inspect

    from target.automation import macos_accessibility as mac

    source = inspect.getsource(mac)
    for line in source.splitlines():
        if "json.dumps" in line and "applescript" not in line.lower():
            assert "tell application" not in line, line.strip()
