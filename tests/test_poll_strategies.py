import json
import threading
import time

import httpx
import pytest
import respx

from prn import config as config_mod
from prn import paths, poller, store
from prn.github import API_URL, VIEWER_QUERY, GitHubClient

from .conftest import ME, node, test_config_text


def make_cfg(strategy="aggressive", **settings):
    text = test_config_text().replace("poll_strategy: conservative", f"poll_strategy: {strategy}")
    cfg = config_mod.parse(text)
    for k, v in settings.items():
        setattr(cfg.settings, k, v)
    return cfg


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeGitHub:
    """respx side effect answering viewer, full searches, combined change checks, and nodes(ids)."""

    def __init__(self, nodes, requested_ids=(), delay=0.0):
        self.nodes = list(nodes)
        self.requested_ids = set(requested_ids)
        self.delay = delay
        self.remaining = 4999
        self.overflow = False
        self.calls = {"search": 0, "check": 0, "nodes": 0}
        self.refetched: list[list[str]] = []
        self._lock = threading.Lock()
        self.active = self.max_active = 0

    def hits(self, q):
        if "review-requested:@me" in q:
            return [n for n in self.nodes if n["id"] in self.requested_ids]
        if f"author:{ME}" in q and "author:" not in q.replace(f"author:{ME}", ""):
            return [n for n in self.nodes if n["author"]["login"] == ME]
        return self.nodes

    @staticmethod
    def check_node(n):
        head = n["commits"]["nodes"][0]["commit"]
        return {"id": n["id"], "updatedAt": n["updatedAt"], "headRefOid": head["oid"],
                "repository": n["repository"], "headRef": {"target": {"statusCheckRollup": head["statusCheckRollup"]}}}

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        query, v = body["query"], body["variables"]
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            rl = {"remaining": self.remaining, "resetAt": "x", "cost": 1}
            if query == VIEWER_QUERY:
                return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
            if "nodes(ids" in query:
                self.calls["nodes"] += 1
                self.refetched.append(list(v["ids"]))
                by_id = {n["id"]: n for n in self.nodes}
                return httpx.Response(200, json={"data": {"rateLimit": rl, "nodes": [by_id.get(i) for i in v["ids"]]}})
            if "q0" in v:
                self.calls["check"] += 1
                data = {"rateLimit": rl}
                i = 0
                while f"q{i}" in v:
                    data[f"s{i}"] = {"pageInfo": {"hasNextPage": self.overflow},
                                     "nodes": [self.check_node(n) for n in self.hits(v[f"q{i}"])]}
                    i += 1
                return httpx.Response(200, json={"data": data})
            self.calls["search"] += 1
            hits = self.hits(v["q"])
            return httpx.Response(200, json={"data": {"rateLimit": rl, "search": {
                "issueCount": len(hits), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": hits}}})
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("prn.notify.send", lambda *a, **k: out.append(a) or True)
    poller._me_cache.clear()
    return out


def make_poller(clock=None):
    return poller.Poller(client_factory=lambda: GitHubClient(token="t"), clock=clock or Clock())


def reset_calls(fake):
    fake.calls = {k: 0 for k in fake.calls}
    fake.refetched = []


@respx.mock
def test_full_poll_runs_searches_in_parallel(sent):
    fake = FakeGitHub([node(author=ME, number=1), node(author="alice", number=2)], delay=0.2)
    respx.post(API_URL).mock(side_effect=fake)
    start = time.time()
    poller.poll_once(cfg=make_cfg("conservative"), client=GitHubClient(token="t"))
    assert fake.calls["search"] >= 3
    assert fake.max_active >= 3
    assert time.time() - start < 0.2 * fake.calls["search"]


@respx.mock
def test_conservative_tick_is_a_full_poll_on_the_normal_schedule(sent):
    fake = FakeGitHub([node(author=ME, number=1)])
    respx.post(API_URL).mock(side_effect=fake)
    p = make_poller()
    cfg = make_cfg("conservative")
    for _ in range(2):
        res, wait = p.tick(cfg)
        assert res.kind == "full" and wait == cfg.settings.poll_interval_seconds
    assert fake.calls["check"] == 0 and fake.calls["nodes"] == 0


@respx.mock
def test_aggressive_checks_cheaply_until_something_changes(sent):
    mine = node(author=ME, number=10, ci="PENDING", updated="2026-10-14T09:00:00Z")
    teammate = node(author="alice", number=11)
    fake = FakeGitHub([mine, teammate])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()

    res, wait = p.tick(cfg)
    assert res.kind == "full" and res.seeded and wait == 15
    assert fake.calls["check"] == 1     # fingerprints are seeded alongside the full poll

    reset_calls(fake)
    clock.t += 15
    res, wait = p.tick(cfg)
    assert res.kind == "check" and res.inbox is None and wait == 15
    assert fake.calls == {"search": 0, "check": 1, "nodes": 0}
    assert res.points == 1

    # My CI fails: only that PR is refetched, and the notification goes out
    fake.nodes = [node(author=ME, number=10, ci="FAILURE", updated="2026-10-14T09:05:00Z"), teammate]
    reset_calls(fake)
    clock.t += 15
    res, _ = p.tick(cfg)
    assert res.kind == "check" and res.changed == 1
    assert fake.calls == {"search": 0, "check": 1, "nodes": 1}
    assert fake.refetched == [["PR_10"]]
    assert [n.title for n in res.notifications] == ["CI failed"]
    assert len(sent) == 1


@respx.mock
def test_ci_change_is_detected_without_an_updated_at_bump(sent):
    fake = FakeGitHub([node(author=ME, number=20, ci="PENDING")])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    p.tick(cfg)
    fake.nodes = [node(author=ME, number=20, ci="SUCCESS")]   # same updatedAt
    clock.t += 15
    res, _ = p.tick(cfg)
    assert res.changed == 1 and [n.title for n in res.notifications] == ["CI passed"]


@respx.mock
def test_new_review_request_is_detected_from_search_membership(sent):
    pr = node(author="dave", number=30, req_users=[ME])
    fake = FakeGitHub([pr])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    p.tick(cfg)
    assert fake.refetched == []

    fake.requested_ids = {"PR_30"}   # fingerprint unchanged; only review-requested:@me membership moved
    clock.t += 15
    res, _ = p.tick(cfg)
    assert fake.refetched == [["PR_30"]]
    names = {s.name: [x.number for x in s.prs] for s in res.inbox.sections}
    assert names["Direct Review Request"] == [30]


@respx.mock
def test_closed_pr_is_dropped_without_a_refetch(sent):
    fake = FakeGitHub([node(author=ME, number=40), node(author=ME, number=41)])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    p.tick(cfg)
    fake.nodes = fake.nodes[:1]
    reset_calls(fake)
    clock.t += 15
    res, _ = p.tick(cfg)
    assert res.changed == 1 and fake.calls["nodes"] == 0
    assert [x.number for s in res.inbox.sections for x in s.prs] == [40]


@respx.mock
def test_full_poll_runs_on_schedule_and_on_demand(sent):
    fake = FakeGitHub([node(author=ME, number=50)])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg(poll_interval_seconds=60)
    p.tick(cfg)

    clock.t += 50
    res, wait = p.tick(cfg)
    assert res.kind == "check" and wait == 10      # don't sleep past the next full poll
    clock.t += 10
    assert p.tick(cfg)[0].kind == "full"
    assert p.tick(cfg, force_full=True)[0].kind == "full"


@respx.mock
def test_oversized_search_falls_back_to_full_polls(sent):
    fake = FakeGitHub([node(author=ME, number=60)])
    fake.overflow = True
    respx.post(API_URL).mock(side_effect=fake)
    p = make_poller()
    cfg = make_cfg()
    res, wait = p.tick(cfg)
    assert res.kind == "full" and wait == cfg.settings.poll_interval_seconds


@respx.mock
def test_low_rate_limit_pauses_change_checks(sent):
    fake = FakeGitHub([node(author=ME, number=70)])
    fake.remaining = 100
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    res, wait = p.tick(cfg)   # the remaining budget is only known after the first request
    assert res.kind == "full" and wait == cfg.settings.poll_interval_seconds
    reset_calls(fake)
    clock.t += wait
    res, wait = p.tick(cfg)
    assert res.kind == "full" and wait == cfg.settings.poll_interval_seconds
    assert fake.calls["check"] == 0


@respx.mock
def test_check_skips_while_another_poll_holds_the_lock(sent):
    fake = FakeGitHub([node(author=ME, number=80)])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    p.tick(cfg)
    clock.t += 15
    with store.poll_lock():
        with pytest.raises(store.LockBusy):
            p.tick(cfg)


@respx.mock
def test_quiet_check_clears_a_stale_error(sent):
    fake = FakeGitHub([node(author=ME, number=90)])
    respx.post(API_URL).mock(side_effect=fake)
    clock = Clock()
    p = make_poller(clock)
    cfg = make_cfg()
    p.tick(cfg)
    store.write_inbox_error("network blip")
    clock.t += 15
    p.tick(cfg)
    assert json.loads(paths.inbox_path().read_text())["error"] is None


@pytest.mark.parametrize("settings, bad", [
    ("poll_strategy: sometimes", "poll_strategy"),
    ("fast_poll_interval_seconds: 5", "fast_poll_interval_seconds"),
])
def test_strategy_settings_are_validated(settings, bad):
    text = f"defaults: {{repos: [a/b]}}\nsettings: {{{settings}}}\nsections: [{{name: a}}]"
    with pytest.raises(config_mod.ConfigError, match=bad):
        config_mod.parse(text)
