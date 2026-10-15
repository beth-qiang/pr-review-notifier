"""Evaluate config sections against PRs. Sections are ordered; first match wins."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Config, Not, Section
from .models import PR


@dataclass
class SectionResult:
    name: str
    notify: bool
    badge: bool
    prs: list[PR] = field(default_factory=list)
    display: bool = True


def matches(pr: PR, section: Section, cfg: Config, me: str, now: Optional[datetime] = None) -> bool:
    f = section.filters
    me_l = me.lower()

    repos = {r.lower() for r in cfg.section_repos(section)}
    if pr.repo.lower() not in repos:
        return False

    if f.authors is not None and pr.author not in cfg.resolve_people(f.authors, me):
        return False
    if f.exclude_authors and pr.author in cfg.resolve_people(f.exclude_authors, me):
        return False

    if f.draft is not None and pr.is_draft != f.draft:
        return False

    if f.review_requested is not None:
        direct = pr.requested_me_directly(me_l)
        team = pr.requested_via_team(me_l)
        want = f.review_requested
        if want == "me" and not direct:
            return False
        if want == "team" and not team:
            return False
        if want == "any" and not (direct or team or pr.review_requested_from_me):
            return False
        if want == "none" and (direct or team or pr.review_requested_from_me):
            return False

    if f.my_review is not None:
        state = pr.my_review_state(me_l)
        if isinstance(f.my_review, Not):
            if state == f.my_review.not_:
                return False
        elif state != f.my_review:
            return False

    if f.review_decision is not None and pr.review_decision != f.review_decision:
        return False

    ignored = cfg.ignored_reviewer_logins()

    if f.approvals is not None:
        n = pr.approvals(ignored)
        if f.approvals.min is not None and n < f.approvals.min:
            return False
        if f.approvals.max is not None and n > f.approvals.max:
            return False

    if f.has_reviews_from_others is not None:
        has = pr.has_reviews_from_others(me_l, ignore_bots=cfg.settings.ignore_bot_reviews, ignored=ignored)
        if has != f.has_reviews_from_others:
            return False

    if f.has_reviews is not None:
        has = pr.has_reviews(me_l, ignore_bots=cfg.settings.ignore_bot_reviews, ignored=ignored)
        if has != f.has_reviews:
            return False

    if f.awaiting_my_review is not None and pr.awaiting_my_review(me_l) != f.awaiting_my_review:
        return False

    if f.labels:
        wanted = {l.lower() for l in f.labels}
        if not wanted & {l.lower() for l in pr.labels}:
            return False
    if f.exclude_labels:
        banned = {l.lower() for l in f.exclude_labels}
        if banned & {l.lower() for l in pr.labels}:
            return False

    if f.ci is not None and pr.ci != f.ci:
        return False

    if f.updated_within_days is not None:
        now = now or datetime.now(timezone.utc)
        if pr.updated_dt() < now - timedelta(days=f.updated_within_days):
            return False

    return True


def evaluate(prs: list[PR], cfg: Config, me: str, now: Optional[datetime] = None) -> list[SectionResult]:
    results = [SectionResult(s.name, s.notify, s.badge, display=s.display) for s in cfg.sections]
    for pr in prs:
        for sec, res in zip(cfg.sections, results):
            if matches(pr, sec, cfg, me, now):
                res.prs.append(pr)
                break
    return results
