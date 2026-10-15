from prn import menu_style
from prn.github import parse_pr

from .conftest import node


def roles(segments):
    return {text.strip(): role for text, role in segments if text.strip()}


def test_ci_and_review_colors():
    pr = parse_pr(node(number=7, ci="FAILURE", decision="APPROVED", title="Fix it"))
    r = roles(menu_style.pr_segments(pr, 50))
    assert r["✗"] == "red" and r["◆"] == "green"
    assert r["Fix it"] == "normal"


def test_changes_requested_and_pending():
    pr = parse_pr(node(ci="PENDING", decision="CHANGES_REQUESTED"))
    r = roles(menu_style.pr_segments(pr, 50))
    assert r["●"] == "orange" and r["◆"] == "red"


def test_no_review_marker_when_undecided():
    pr = parse_pr(node(ci="SUCCESS", decision="REVIEW_REQUIRED"))
    segs = menu_style.pr_segments(pr, 50)
    assert "◆" not in menu_style.plain(segs) and roles(segs)["✓"] == "green"


def test_drafts_are_dimmed_and_labeled():
    pr = parse_pr(node(draft=True, title="WIP thing"))
    segs = menu_style.pr_segments(pr, 50)
    assert all(role == "dim" for text, role in segs if "WIP thing" in text or "#" in text)
    assert "(draft)" in menu_style.plain(segs)


def test_read_rows_are_dimmed_throughout():
    pr = parse_pr(node(ci="FAILURE", decision="APPROVED", title="Seen it"))
    segs = menu_style.pr_segments(pr, 50, read=True)
    assert all(role == "dim" for _, role in segs)
    assert menu_style.plain(segs) == menu_style.plain(menu_style.pr_segments(pr, 50))


def test_plain_text_and_apply_fallback():
    pr = parse_pr(node(number=42, author="alice", title="Hello"))
    segs = menu_style.pr_segments(pr, 50)
    assert menu_style.plain(segs) == "   ✓   web#42  Hello  · alice"
    assert menu_style.apply(object(), segs) is False  # no AppKit here: keeps the plain title
