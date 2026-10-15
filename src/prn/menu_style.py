"""Colored menu item titles for the menu bar dropdown.

Each PR row is built as (text, role) segments. On macOS the roles become native system
colors through NSAttributedString, so they adapt to light and dark mode. Elsewhere, or if
AppKit isn't available, the plain text is used unchanged.

Row layout:   ✓ ◆ web#123  Title of the PR…  · author
              │ └ review: ◆ green = approved, ◆ red = changes requested
              └ CI: ✓ green = passed, ✗ red = failed, ● orange = running
Draft PRs are shown dimmed. A row I've already opened keeps its colors at half opacity until something new happens on it.
"""

from __future__ import annotations

from typing import Iterable

from . import fmt
from .models import PR

Segment = tuple[str, str]  # (text, role)

CI_ROLE = {"success": "green", "failure": "red", "pending": "orange"}
REVIEW_MARK = {"approved": ("◆", "green"), "changes_requested": ("◆", "red")}
LEGEND = "✓ ✗ ● CI   ◆ approved / changes requested"
MUTED = "muted"
MUTED_ALPHA = 0.5


def pr_segments(pr: PR, max_title: int, read: bool = False) -> list[Segment]:
    """`read`: I've opened it and nothing new has happened since, so every segment is muted (same hue, faded)."""
    title_role = "dim" if pr.is_draft else "normal"
    mark, mark_role = REVIEW_MARK.get(pr.review_decision, (" ", "normal"))
    segments = [
        ("   ", "normal"),
        (fmt.CI_GLYPH.get(pr.ci, " "), CI_ROLE.get(pr.ci, "normal")),
        (" ", "normal"),
        (mark, mark_role),
        (f" {fmt.pr_label(pr)}  ", title_role),
        (fmt.truncate(pr.title, max_title) + ("  (draft)" if pr.is_draft else ""), title_role),
        (f"  · {pr.author}", "dim"),
    ]
    return [(text, f"{role} {MUTED}") for text, role in segments] if read else segments


def plain(segments: Iterable[Segment]) -> str:
    return "".join(text for text, _ in segments)


def _color(role: str):
    from AppKit import NSColor  # type: ignore
    base, _, modifier = role.partition(" ")
    color = {
        "green": NSColor.systemGreenColor,
        "red": NSColor.systemRedColor,
        "orange": NSColor.systemOrangeColor,
        "dim": NSColor.secondaryLabelColor,
    }.get(base, NSColor.labelColor)()
    return color.colorWithAlphaComponent_(color.alphaComponent() * MUTED_ALPHA) if modifier == MUTED else color


def apply(menu_item, segments: list[Segment]) -> bool:
    """Set a colored attributed title on a rumps.MenuItem. Returns False if colors aren't available."""
    try:
        from AppKit import NSFont, NSFontAttributeName, NSForegroundColorAttributeName  # type: ignore
        from Foundation import NSAttributedString, NSMutableAttributedString  # type: ignore
    except Exception:
        return False
    try:
        font = NSFont.menuFontOfSize_(0)  # system default menu font size
        out = NSMutableAttributedString.alloc().init()
        for text, role in segments:
            attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: _color(role)}
            out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
        menu_item._menuitem.setAttributedTitle_(out)
        return True
    except Exception:
        return False
