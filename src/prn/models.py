"""Normalized PR model, independent of the GitHub response shape."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Review:
    author: str          # lowercase login
    state: str           # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    submitted_at: Optional[str] = None
    is_bot: bool = False
    commit: Optional[str] = None   # oid of the commit the review was submitted against


@dataclass
class PR:
    id: str
    repo: str            # owner/name
    number: int
    title: str
    url: str
    author: str          # lowercase login ('ghost' if deleted)
    is_draft: bool
    created_at: str
    updated_at: str
    review_decision: str                     # approved | changes_requested | review_required | none
    ci: str                                  # success | failure | pending | none
    mergeable: str = "unknown"               # mergeable | conflicting | unknown (GitHub may still be computing)
    head_oid: str = ""                       # latest commit on the PR branch
    labels: list[str] = field(default_factory=list)
    comment_count: int = 0
    recent_comment_authors: list[str] = field(default_factory=list)  # last 20, oldest first
    review_count: int = 0
    reviews: list[Review] = field(default_factory=list)            # full history, oldest first
    latest_reviews: list[Review] = field(default_factory=list)     # latest opinionated per reviewer
    requested_users: list[str] = field(default_factory=list)       # lowercase logins
    requested_teams: list[str] = field(default_factory=list)       # org/slug
    review_requested_from_me: bool = False   # GitHub's `review-requested:@me` (direct OR via my team)

    # ---------------------------------------------------------- derived helpers

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def short_repo(self) -> str:
        return self.repo.split("/", 1)[-1]

    def approvals(self, ignored: frozenset[str] = frozenset()) -> int:
        return sum(1 for r in self.latest_reviews if r.state == "APPROVED" and r.author not in ignored)

    def approved_head_by(self, login: str) -> bool:
        """`login` has an APPROVED review on the PR's latest commit."""
        login = login.lower().removesuffix("[bot]")
        return any(r.author == login and r.state == "APPROVED" and r.commit == self.head_oid
                   for r in self.reviews + self.latest_reviews)

    def my_review_state(self, me: str) -> str:
        """Latest opinionated review (approved/changes_requested/dismissed), else commented, else none."""
        me = me.lower()
        for r in self.latest_reviews:
            if r.author == me and r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                return r.state.lower()
        if any(r.author == me for r in self.reviews):
            return "commented"
        return "none"

    def has_reviewed(self, me: str) -> bool:
        me = me.lower()
        return any(r.author == me for r in self.reviews) or any(r.author == me for r in self.latest_reviews)

    def has_reviews_from_others(self, me: str, ignore_bots: bool = False,
                                ignored: frozenset[str] = frozenset()) -> bool:
        me, author = me.lower(), self.author
        everyone = self.reviews + self.latest_reviews
        return any(r.author not in (me, author) and r.author not in ignored and not (ignore_bots and r.is_bot)
                   for r in everyone)

    def has_reviews(self, me: str, ignore_bots: bool = False, ignored: frozenset[str] = frozenset()) -> bool:
        """Anyone other than the author has submitted a review (including me)."""
        return self.has_reviewed(me) or self.has_reviews_from_others(me, ignore_bots, ignored)

    def awaiting_my_review(self, me: str) -> bool:
        """True until I submit any review; becomes true again if my review is re-requested directly."""
        return not self.has_reviewed(me) or self.requested_me_directly(me)

    def requested_me_directly(self, me: str) -> bool:
        return me.lower() in self.requested_users

    def requested_via_team(self, me: str) -> bool:
        return self.review_requested_from_me and not self.requested_me_directly(me) and bool(self.requested_teams)

    def updated_dt(self) -> datetime:
        return datetime.fromisoformat(self.updated_at.replace("Z", "+00:00")).astimezone(timezone.utc)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PR":
        d = dict(d)
        d["reviews"] = [Review(**r) for r in d.get("reviews", [])]
        d["latest_reviews"] = [Review(**r) for r in d.get("latest_reviews", [])]
        return cls(**d)
