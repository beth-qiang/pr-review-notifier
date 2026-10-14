"""Diff the previous snapshot against the current evaluation and produce notification events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .engine import SectionResult
from .models import PR

NEW_IN_SECTION = "new_in_section"
RE_REVIEW = "re_review_requested"
MY_PR_STATUS = "my_pr_status"
CI_STATUS = "ci_status"

# Higher number = more important; used to pick the notification title when a PR has several events.
PRIORITY = {RE_REVIEW: 4, MY_PR_STATUS: 3, CI_STATUS: 2, NEW_IN_SECTION: 1}


@dataclass
class Event:
    kind: str
    pr: PR
    detail: str
    section: Optional[str] = None


@dataclass
class Notification:
    pr: PR
    title: str
    subtitle: str
    message: str
    kinds: list[str] = field(default_factory=list)


def snapshot_entry(pr: PR, section: Optional[str], me: str) -> dict:
    me_l = me.lower()
    return {
        "section": section,
        "title": pr.title,
        "url": pr.url,
        "is_mine": pr.author == me_l,
        "review_decision": pr.review_decision,
        "ci": pr.ci,
        "review_count": pr.review_count,
        "comment_count": pr.comment_count,
        "requested_me": pr.requested_me_directly(me_l) or pr.requested_via_team(me_l) or pr.review_requested_from_me,
        "has_reviewed": pr.has_reviewed(me_l),
        "my_review_state": pr.my_review_state(me_l),
    }


def build_snapshot(results: list[SectionResult], all_prs: list[PR], me: str) -> dict[str, dict]:
    section_of = {pr.key: r.name for r in results for pr in r.prs}
    return {pr.key: snapshot_entry(pr, section_of.get(pr.key), me) for pr in all_prs}


_DECISION_TEXT = {
    "approved": "Approved",
    "changes_requested": "Changes requested",
    "review_required": "Review required",
}


def diff(prev: dict[str, dict], results: list[SectionResult], all_prs: list[PR], cfg: Config, me: str) -> list[Event]:
    toggles = cfg.settings.notifications
    me_l = me.lower()
    notify_sections = {s.name for s in cfg.sections if s.notify}
    section_of = {pr.key: r.name for r in results for pr in r.prs}
    events: list[Event] = []

    for pr in all_prs:
        old = prev.get(pr.key)
        cur = snapshot_entry(pr, section_of.get(pr.key), me)
        sec = cur["section"]

        # 1. Entered a notifying section (skip moves caused by my own review: I already know)
        moved_by_me = old is not None and (
            (cur["has_reviewed"] and not old.get("has_reviewed"))
            or ("my_review_state" in old and cur["my_review_state"] != old["my_review_state"])
        )
        if (toggles.new_in_section and sec in notify_sections and not moved_by_me
                and (old is None or old.get("section") != sec)):
            events.append(Event(NEW_IN_SECTION, pr, f"New in {sec}", sec))

        if not cur["is_mine"]:
            # 2. Re-review requested: I'd reviewed, and I'm (newly) requested again
            if toggles.re_review_requested and cur["requested_me"] and cur["has_reviewed"]:
                if old is None or not old.get("requested_me"):
                    events.append(Event(RE_REVIEW, pr, f"@{pr.author} re-requested your review", sec))
            continue

        if old is None:
            continue  # brand-new PR of mine: nothing to compare against

        # 3. My PR status changes
        if toggles.my_pr_status:
            if cur["review_decision"] != old.get("review_decision") and cur["review_decision"] in _DECISION_TEXT:
                events.append(Event(MY_PR_STATUS, pr, _DECISION_TEXT[cur["review_decision"]], sec))
            # Only count activity from other people (replying to threads creates reviews by me).
            delta = cur["review_count"] - old.get("review_count", 0)
            new_reviews = [r for r in pr.reviews[-delta:] if r.author != me_l] if delta > 0 else []
            if new_reviews:
                n = len(new_reviews)
                events.append(Event(MY_PR_STATUS, pr,
                                    f"{n} new review{'s' if n > 1 else ''} from @{new_reviews[-1].author}", sec))
            delta = cur["comment_count"] - old.get("comment_count", 0)
            new_comments = [a for a in pr.recent_comment_authors[-delta:] if a != me_l] if delta > 0 else []
            if new_comments:
                n = len(new_comments)
                events.append(Event(MY_PR_STATUS, pr,
                                    f"{n} new comment{'s' if n > 1 else ''} from @{new_comments[-1]}", sec))

        # 4. CI finished
        if toggles.ci_status and cur["ci"] != old.get("ci") and cur["ci"] in ("success", "failure"):
            events.append(Event(CI_STATUS, pr, "CI passed" if cur["ci"] == "success" else "CI failed", sec))

    return events


def to_notifications(events: list[Event]) -> list[Notification]:
    """Collapse multiple events for the same PR into one notification."""
    by_pr: dict[str, list[Event]] = {}
    for e in events:
        by_pr.setdefault(e.pr.key, []).append(e)
    out = []
    for evs in by_pr.values():
        evs.sort(key=lambda e: PRIORITY[e.kind], reverse=True)
        pr = evs[0].pr
        details = []
        for e in evs:
            if e.detail not in details:
                details.append(e.detail)
        out.append(Notification(
            pr=pr,
            title=details[0],
            subtitle=f"{pr.short_repo}#{pr.number} · @{pr.author}",
            message=pr.title + (f"\n{' · '.join(details[1:])}" if len(details) > 1 else ""),
            kinds=[e.kind for e in evs],
        ))
    return out
