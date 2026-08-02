"""Render the assistant's Markdown safely.

The order here is the whole point: **escape first, then add markup.** The text
being rendered is not trustworthy. It is written by a model, but it is written
*from* rows read out of the shop's own database — a material named
``<img src=x onerror=...>`` in someone's inventory reaches this function by an
entirely ordinary path. Escaping the whole string before any tag is inserted
means anything that arrived as data stays data; only the handful of patterns
matched afterwards can ever become an element.

No Markdown library, for the same reason the stylesheet ships no CDN: a general
renderer would have to be paired with a sanitiser to be safe here, and that is
two dependencies to keep correct where a whitelist of four constructs will do.
Anything not listed below renders as literal text, which is the right failure
mode — an unrendered pipe character is a cosmetic bug, an unescaped one is not.
"""

import re

from django.utils.html import escape
from django.utils.safestring import mark_safe

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_CODE = re.compile(r"`([^`\n]+?)`")
#: A Markdown table separator row: |---|:--:|---|
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _inline(text: str) -> str:
    """Bold and inline code, on already-escaped text."""
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    return _CODE.sub(r"<code>\1</code>", text)


def _table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="md-table"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def render(text: str) -> str:
    """Markdown subset -> safe HTML. Tables, bold, inline code, paragraphs."""
    if not text:
        return ""

    # Everything downstream operates on escaped text. Do not move this.
    lines = escape(text).split("\n")

    out: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            joined = "<br>".join(_inline(line) for line in paragraph)
            out.append(f"<p>{joined}</p>")
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]

        # A table is a header row, a separator row, then rows until a line
        # that no longer looks like one.
        is_row = "|" in line and line.strip().startswith("|")
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if is_row and _SEPARATOR.match(next_line):
            flush()
            header = _cells(line)
            index += 2
            rows = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_cells(lines[index]))
                index += 1
            out.append(_table(header, rows))
            continue

        if not line.strip():
            flush()
        else:
            paragraph.append(line)
        index += 1

    flush()
    return mark_safe("".join(out))
