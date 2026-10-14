from __future__ import annotations

import itertools

import pytest

from prn import config as config_mod
from prn import paths

ME = "me-user"
_ids = itertools.count(1)


def test_config_text() -> str:
    """The committed example config with test repos/teammates (alice, bob) substituted in."""
    return (config_mod.example_config_text()
            .replace("your-org/repo-one", "acme/web")
            .replace("your-org/repo-two", "acme/api")
            .replace("teammate-one", "alice")
            .replace("teammate-two", "bob"))


test_config_text.__test__ = False  # not a pytest test


def write_config() -> None:
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(test_config_text(), encoding="utf-8")


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("PRN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("PRN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    yield tmp_path


@pytest.fixture
def cfg() -> config_mod.Config:
    return config_mod.parse(test_config_text())


def actor(login, bot=False):
    return {"__typename": "Bot" if bot else "User", "login": login}


def review(login, state, bot=False, commit=None):
    return {"author": actor(login, bot), "state": state, "submittedAt": "2026-10-13T12:00:00Z",
            "commit": {"oid": commit} if commit else None}


def node(
    author="alice",
    repo="acme/web",
    number=None,
    draft=False,
    decision="REVIEW_REQUIRED",
    reviews=(),
    latest=None,
    req_users=(),
    req_teams=(),
    ci="SUCCESS",
    comments=0,
    comment_authors=None,
    labels=(),
    updated="2026-10-14T09:00:00Z",
    title=None,
    head="head-sha",
):
    """Build a GraphQL-shaped PullRequest node."""
    number = number or next(_ids)
    reviews = list(reviews)
    if latest is None:
        # derive latest opinionated per author
        seen = {}
        for r in reviews:
            if r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                seen[r["author"]["login"]] = r
        latest = list(seen.values())
    rr = [{"requestedReviewer": {"__typename": "User", "login": u}} for u in req_users]
    rr += [{"requestedReviewer": {"__typename": "Team", "slug": t, "organization": {"login": "acme"}}}
           for t in req_teams]
    authors = comment_authors if comment_authors is not None else ["someone"] * comments
    return {
        "id": f"PR_{number}",
        "number": number,
        "title": title or f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": draft,
        "createdAt": "2026-10-10T09:00:00Z",
        "updatedAt": updated,
        "reviewDecision": decision,
        "repository": {"nameWithOwner": repo},
        "author": actor(author),
        "labels": {"nodes": [{"name": l} for l in labels]},
        "comments": {"totalCount": len(authors), "nodes": [{"author": {"login": a}} for a in authors]},
        "reviews": {"totalCount": len(reviews), "nodes": reviews},
        "latestOpinionatedReviews": {"nodes": latest},
        "reviewRequests": {"nodes": rr},
        "commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {"state": ci} if ci else None}}]},
    }
