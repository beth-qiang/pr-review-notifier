import json

import httpx
import pytest
import respx

from prn import paths, poller, store
from prn import config as config_mod
from prn.github import API_URL, MAX_QUERY_LEN, GitHubClient, plan_queries

from .conftest import ME, node, review, write_config


def test_plan_queries_respect_length_and_cover_sections(cfg):
    qs = plan_queries(cfg, ME)
    assert all(len(q) <= MAX_QUERY_LEN for q in qs), [len(q) for q in qs]
    joined = "\n".join(qs)
    assert f"author:{ME}" in joined
    assert "review-requested:@me" in joined
    for u in cfg.groups["teammates"]:
        assert f"author:{u}" in joined
    assert len(qs) == len(set(qs))


def test_plan_queries_chunk_many_repos():
    c = config_mod.parse(
        "defaults:\n  repos:\n" + "".join(f"    - acme/a-very-long-repository-name-{i}\n" for i in range(12))
        + "sections: [{name: Everything}]"
    )
    qs = [q for q in plan_queries(c, ME) if "repo:" in q]
    assert len(qs) > 1
    assert all(len(q) <= MAX_QUERY_LEN for q in qs)
    covered = {t for q in qs for t in q.split() if t.startswith("repo:")}
    assert len(covered) == 12


class FakeGitHub:
    """respx side-effect that answers viewer + search queries from canned nodes."""

    def __init__(self, nodes, requested_ids=()):
        self.nodes = nodes
        self.requested_ids = set(requested_ids)
        self.queries = []

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        if "viewer" in body["query"] and "search" not in body["query"]:
            return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
        q = body["variables"]["q"]
        self.queries.append(q)
        if "review-requested:@me" in q:
            hits = [n for n in self.nodes if n["id"] in self.requested_ids]
        elif f"author:{ME}" in q and "author:" not in q.replace(f"author:{ME}", ""):
            hits = [n for n in self.nodes if n["author"]["login"] == ME]
        else:
            hits = self.nodes
        return httpx.Response(200, json={"data": {
            "rateLimit": {"remaining": 4999, "resetAt": "x", "cost": 1},
            "search": {"issueCount": len(hits), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": hits},
        }})


@respx.mock
def test_full_poll_cycle_seeds_then_notifies(monkeypatch):
    write_config()
    sent = []
    monkeypatch.setattr("prn.notify.send", lambda *a, **k: sent.append((a, k)) or True)
    poller._me_cache.clear()

    team_pr = node(author="dave", number=201, req_teams=["platform"])
    mine = node(author=ME, number=202, ci="PENDING")
    fake = FakeGitHub([team_pr, mine], requested_ids={"PR_201"})
    respx.post(API_URL).mock(side_effect=fake)

    r1 = poller.poll_once(client=GitHubClient(token="t"))
    assert r1.seeded and sent == []
    names = {s.name: [p.number for p in s.prs] for s in r1.inbox.sections}
    assert names["Team Review Requested"] == [201]
    assert names["My Open PRs"] == [202]
    assert r1.inbox.badge_count == 1

    # Second poll: a teammate opens a PR, my CI fails
    fake.nodes = [team_pr, node(author=ME, number=202, ci="FAILURE"), node(author="alice", number=203)]
    r2 = poller.poll_once(client=GitHubClient(token="t"))
    titles = sorted(n.title for n in r2.notifications)
    assert titles == ["CI failed", "New in Teammates Need Review"]
    assert len(sent) == 2
    assert r2.inbox.badge_count == 2

    # inbox.json round-trips
    again = store.read_inbox()
    assert again.me == ME and again.badge_count == 2

    # Third poll: I review the team PR → it leaves the inbox, no notification
    fake.nodes = [node(author="dave", number=201, reviews=[review(ME, "APPROVED")]),
                  node(author=ME, number=202, ci="FAILURE"), node(author="alice", number=203)]
    fake.requested_ids = set()
    r3 = poller.poll_once(client=GitHubClient(token="t"))
    assert r3.notifications == []
    assert r3.inbox.badge_count == 1


@respx.mock
def test_poll_error_is_recorded_in_inbox():
    write_config()
    poller._me_cache.clear()
    respx.post(API_URL).mock(return_value=httpx.Response(401, json={}))
    from prn.github import AuthError
    with pytest.raises(AuthError):
        poller.poll_once(client=GitHubClient(token="t"))
    assert "gh auth login" in json.loads(paths.inbox_path().read_text())["error"]


def test_lock_prevents_concurrent_polls():
    with store.poll_lock():
        with pytest.raises(store.LockBusy):
            with store.poll_lock(wait_seconds=0):
                pass
