"""
markdown_escape.py — P2.4.D markdown escape for user-controlled values.

This module provides a conservative escape for user-controlled
strings before they are emitted as part of our markdown structure
(tables, headings, code spans). It is NOT a general-purpose markdown
escape — those are notoriously incomplete and tend to escape too much
or too little depending on the renderer. We escape only what's
needed to prevent the markdown serializer from breaking its own
output for the specific structures we emit.

Design §2.1 S5 (P2.4 R1):

    Apply `escape_markdown()` to every **user-controlled value**
    before markdown serialization, NOT to every renderer boundary.

Renderer-generated text (headings, table syntax, fixed structure,
status enums) is NEVER escaped — it's already controlled.

The escape targets the most common breakers in a markdown table cell:

  1. `|`     → `\\|`   (pipe breaks table rows)
  2. `\n`    → ` `     (newline breaks table rows)
  3. `` ` `` → `` \\` ``   (backtick breaks inline code)
  4. `\\`    → `\\\\`  (backslash — escape sequence consistency)

We deliberately do NOT escape:

  * `*`, `_`  — emphasis; legibility > strict escape for our use
  * `#`       — headings only break at line start
  * `[`, `]`  — link syntax only matters for auto-linkers
  * `>`       — blockquote only matters at line start
  * `!`       — image syntax; only matters with `[`
"""

from __future__ import annotations


def _strip_control_chars(s: str) -> str:
    """Remove ASCII control characters (0x00-0x1F) other than
    whitespace.

    Whitespace control chars (TAB=0x09, LF=0x0A, CR=0x0D) are
    handled separately by the caller (CRLF / LF / CR → space). This
    function strips the dangerous non-whitespace control chars
    (NUL, BEL, ESC, etc.) that have no markdown meaning and could
    confuse terminals/log readers.
    """
    return "".join(
        ch for ch in s
        if ord(ch) > 0x1F or ch in ("\t", "\n", "\r")
    )


def escape_markdown(s: str) -> str:
    """Escape user-controlled value for safe markdown embedding.

    Conservative escape targeting table-cell context. Caller should
    only pass user-controlled strings (case ids, trace ids,
    failure messages, etc.) — never pass renderer-generated text.

    Order matters:
      * Strip non-whitespace control chars first (NUL, BEL, etc.).
      * Backslash second (so we don't double-escape the escapes we
        add for the other characters).
      * Then pipe, backtick.
      * Newline + carriage-return last (replace with space rather
        than escape). We normalize \\r\\n → single space to avoid
        two consecutive replacements turning one line break into
        two spaces.

    Args:
        s: User-controlled string. May be empty.

    Returns:
        Escaped string safe to embed in markdown table cells.

    Examples:
        >>> escape_markdown("plain_text")
        'plain_text'
        >>> escape_markdown("with|pipe")
        'with\\\\|pipe'
        >>> escape_markdown("with\\nnewline")
        'with newline'
        >>> escape_markdown("with`backtick")
        'with\\\\`backtick'
        >>> escape_markdown("with\\\\backslash")
        'with\\\\\\\\backslash'
        >>> escape_markdown("with\\r\\nCRLF")
        'with CRLF'
    """
    return (
        _strip_control_chars(s)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        # CRLF first so we get a single space (not "  ").
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


__all__ = ["escape_markdown"]