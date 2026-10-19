import json
import re
import time

import httpx
import pytest
import respx

from prn import config as config_mod
from prn import events, menubar, paths, poller, store, web
from prn.github import NOTIFICATIONS_URL, AuthError, GitHubClient, GitHubError, parse_mentions
from prn.models import Mention

from .conftest import ME, node, test_config_text


def thread(id="1", reason="mention", kind="PullRequest", repo="acme/web", number=5, unread=True,
           updated="2026-10-16T12:00:00Z", title="Fix the thing"):
    path = "pulls" if kind == "PullRequest" else "issues"
    return {"id": id, "reason": reason, "unread": unread, "updated_at": updated,
            "repository": {"full_name": repo},
            "subject": {"type": kind, "title": title, "url": f"https://api.github.com/repos/{repo}/{path}/{number}"}}


def mention(id="1", updated="2026-10-16T12:00:00Z", reason="mention", number=5):
    return Mention(id=id, repo="acme/web", number=number, title="Fix the thing",
                   url=f"https://github.com/acme/web/pull/{number}", reason=reason, updated_at=updated)


def config_text(notify_mention=True, notify_team_mention=True, **mentions):
    """The test config with the mentions settings replaced (the default test config has them off)."""
    text = re.sub(r"  mentions:.*?\n(?=  notifications:)", f"  mentions: {json.dumps(mentions or {'enabled': True})}\n",
                  test_config_text(), flags=re.S)
    if not notify_mention:
        text = text.replace("    mention: true", "    mention: false")
    if not notify_team_mention:
        text = text.replace("    team_mention: true", "    team_mention: false")
    return text


def cfg_with(**mentions):
    return config_mod.parse(config_text(**mentions))


# ---------------------------------------------------------------- parsing


def test_only_unread_mentions_on_pull_requests_are_kept():
    threads = [thread("1"), thread("2", reason="team_mention"), thread("3", reason="review_requested"),
               thread("4", kind="Issue"), thread("5", unread=False), thread("6", kind="CheckSuite")]
    got = parse_mentions(threads)
    assert sorted(m.id for m in got) == ["1", "2"]


def test_team_mentions_can_be_turned_off():
    got = parse_mentions([thread("1"), thread("2", reason="team_mention")], include_team=False)
    assert [m.id for m in got] == ["1"]


def test_mention_fields_url_and_ordering():
    got = parse_mentions([thread("1", updated="2026-10-15T00:00:00Z"),
                          thread("2", repo="acme/other", number=9, updated="2026-10-16T00:00:00Z")])
    assert [m.id for m in got] == ["2", "1"]            # newest first
    assert got[0].url == "https://github.com/acme/other/pull/9" and got[0].key == "acme/other#9"
    assert got[0].short_repo == "other" and got[0].title == "Fix the thing"


def test_subjects_without_a_pull_url_are_skipped():
    t = thread("1")
    t["subject"]["url"] = None
    assert parse_mentions([t]) == []


# ---------------------------------------------------------------- the client


@respx.mock
def test_notifications_follows_pages_and_remembers_the_poll_interval():
    def handler(request: httpx.Request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[thread("2")])
        return httpx.Response(200, json=[thread("1")], headers={
            "Link": '<https://api.github.com/notifications?page=2>; rel="next"',
            "Last-Modified": "Fri, 16 Oct 2026 12:00:00 GMT", "X-Poll-Interval": "90"})

    respx.get(NOTIFICATIONS_URL).mock(side_effect=handler)
    client = GitHubClient(token="t")
    assert [t["id"] for t in client.notifications()] == ["1", "2"]
    assert client.notifications_poll_interval == 90


@respx.mock
def test_an_unchanged_feed_is_a_304_and_reuses_the_last_result():
    seen_headers = []

    def handler(request: httpx.Request):
        seen_headers.append(request.headers.get("If-Modified-Since"))
        if request.headers.get("If-Modified-Since"):
            return httpx.Response(304)
        return httpx.Response(200, json=[thread("1")], headers={"Last-Modified": "Fri, 16 Oct 2026 12:00:00 GMT"})

    respx.get(NOTIFICATIONS_URL).mock(side_effect=handler)
    client = GitHubClient(token="t")
    first = client.notifications()
    again = client.notifications()
    assert seen_headers == [None, "Fri, 16 Oct 2026 12:00:00 GMT"]
    assert again == first and [t["id"] for t in again] == ["1"]


@respx.mock
def test_notifications_errors():
    respx.get(NOTIFICATIONS_URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(AuthError):
        GitHubClient(token="t").notifications()
    respx.get(NOTIFICATIONS_URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(GitHubError):
        GitHubClient(token="t").notifications()


# ---------------------------------------------------------------- events and notifications


def test_new_mentions_first_run_is_silent_then_new_and_renewed_ones_count():
    a, b = mention("1"), mention("2")
    assert events.new_mentions(None, [a, b]) == []
    assert [m.id for m in events.new_mentions([a], [a, b])] == ["2"]
    renewed = mention("1", updated="2026-10-16T13:00:00Z")
    assert [m.id for m in events.new_mentions([a], [renewed])] == ["1"]
    assert events.new_mentions([a, b], [a]) == []                   # one went away: nothing to say


# ---------------------------------------------------------------- read / done state


def test_mention_read_done_and_resurfacing():
    m = mention("7")
    assert store.is_mention_unread(store.read_seen(), m)
    store.mark_mentions([m])
    seen = store.read_seen()
    assert not store.is_mention_unread(seen, m) and not store.is_mention_done(seen, m)
    store.mark_mentions([m], done=True)
    assert store.is_mention_done(store.read_seen(), m)
    renewed = mention("7", updated="2026-10-16T13:00:00Z")           # mentioned again
    assert store.is_mention_unread(store.read_seen(), renewed)
    assert not store.is_mention_done(store.read_seen(), renewed)


def test_mention_state_is_not_confused_with_pr_state():
    m = mention("7", number=5)
    store.mark_mentions([m], done=True)
    from prn.github import parse_pr
    pr = parse_pr(node(number=5, repo="acme/web"))
    assert store.is_unread(store.read_seen(), pr, "Teammates Need Review", ME)
    assert not store.is_done(store.read_seen(), pr, "Teammates Need Review", ME)


def test_a_mention_still_unread_on_github_is_never_pruned():
    live, gone = mention("1"), mention("2")
    store.write_mentions([live])                                    # only 1 is still listed
    store.mark_mentions([live, gone], done=True)
    data = json.loads(paths.seen_path().read_text())
    old = store.week_start(time.time()) - 40 * 24 * 3600
    for k in data["prs"]:
        data["prs"][k]["at"] = old
    paths.seen_path().write_text(json.dumps(data))
    store.mark_read([], ME)                                          # any write prunes
    assert set(store.read_seen()) == {store.mention_key("1")}


# ---------------------------------------------------------------- syncing


class FakeClient:
    def __init__(self, threads=(), error=None):
        self.threads, self.error, self.calls = list(threads), error, 0
        self.notifications_poll_interval = 60

    def notifications(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.threads


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr("prn.notify.send", lambda *a, **k: out.append((a, k)) or True)
    return out


def test_first_sync_is_silent_then_new_mentions_notify(sent):
    cfg, client = cfg_with(enabled=True), FakeClient([thread("1")])
    poller.sync_mentions(client, cfg)
    assert sent == [] and [m.id for m in store.read_mentions()] == ["1"]

    client.threads = [thread("1"), thread("2", reason="team_mention", number=8)]
    poller.sync_mentions(client, cfg)
    assert len(sent) == 1
    (title, subtitle, message), kwargs = sent[0][0][:3], sent[0][1]
    assert (title, subtitle, message) == ("Your team was mentioned", "web#8", "Fix the thing")
    assert kwargs["url"] == "https://github.com/acme/web/pull/8"


def test_mention_notifications_can_be_switched_off(sent):
    cfg = config_mod.parse(config_text(notify_mention=False, enabled=True))
    client = FakeClient([thread("1")])
    poller.sync_mentions(client, cfg)
    client.threads.append(thread("2"))
    poller.sync_mentions(client, cfg)
    assert sent == [] and len(store.read_mentions()) == 2


def test_many_new_mentions_become_one_summary(sent):
    cfg, client = cfg_with(enabled=True), FakeClient()
    poller.sync_mentions(client, cfg)
    client.threads = [thread(str(i), number=i) for i in range(1, 8)]
    poller.sync_mentions(client, cfg)
    assert len(sent) == 1 and sent[0][1]["group"] == "prn-mentions"


def test_a_mentions_failure_never_raises_and_keeps_the_last_good_list(sent):
    cfg, client = cfg_with(enabled=True), FakeClient([thread("1")])
    poller.sync_mentions(client, cfg)
    client.error = GitHubError("boom")
    poller.sync_mentions(client, cfg)                                # must not raise
    assert [m.id for m in store.read_mentions()] == ["1"]


def test_disabled_does_nothing(sent):
    client = FakeClient([thread("1")])
    poller.sync_mentions(client, cfg_with(enabled=False))
    assert client.calls == 0 and store.read_mentions() is None


def test_team_mentions_setting_filters_and_rewrites(sent):
    client = FakeClient([thread("1"), thread("2", reason="team_mention")])
    poller.sync_mentions(client, cfg_with(enabled=True, team_mentions=True))
    assert len(store.read_mentions()) == 2
    poller.sync_mentions(client, cfg_with(enabled=True, team_mentions=False))
    assert [m.id for m in store.read_mentions()] == ["1"]


def test_the_menu_bar_poller_fetches_mentions_at_most_once_a_minute(sent):
    now = [1000.0]
    p = poller.Poller(client_factory=lambda: FakeClient([thread("1")]), clock=lambda: now[0])
    cfg = cfg_with(enabled=True)
    p._sync_mentions(cfg, force=False)
    p._sync_mentions(cfg, force=False)
    now[0] += 30
    p._sync_mentions(cfg, force=False)
    assert p.client.calls == 1
    now[0] += 31
    p._sync_mentions(cfg, force=False)
    assert p.client.calls == 2
    p._sync_mentions(cfg, force=True)                                # Refresh now ignores the throttle
    assert p.client.calls == 3


# ---------------------------------------------------------------- dashboard and menu bar


def test_mentions_section_goes_where_the_config_says(cfg):
    inbox = store.Inbox(me=ME, sections=[store.InboxSection("A", True, True), store.InboxSection("B", True, True)])
    ms = [mention("1"), mention("2", reason="team_mention", number=6)]
    out = web.payload(inbox, {}, mentions=ms, mentions_position=2)
    assert [s["name"] for s in out["sections"]] == ["A", "Mentions", "B"]
    sec = out["sections"][1]
    assert sec["kind"] == "mentions" and [r["key"] for r in sec["prs"]] == ["mention:1", "mention:2"]
    assert sec["prs"][0]["label"] == "web#5" and sec["prs"][0]["unread"] and not sec["prs"][0]["done"]
    assert [s["name"] for s in web.payload(inbox, {}, mentions=None)["sections"]] == ["A", "B"]
    assert [s["name"] for s in web.payload(inbox, {}, mentions=ms, mentions_position=99)["sections"]][-1] == "Mentions"


def test_mention_rows_read_done_undo_through_the_server():
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(config_text(enabled=True, position=1), encoding="utf-8")
    store.write_inbox([], ME, next_poll_at=None)
    store.write_mentions([mention("9")])
    d = web.Dashboard(port=0, do_refresh=lambda: None)
    d.start()
    try:
        def row():
            out = httpx.get(d.url + f"data?token={d.token}").json()
            assert out["sections"][0]["name"] == "Mentions"
            return out["sections"][0]["prs"][0]

        def post(path):
            return httpx.post(d.url + f"{path}?token={d.token}", json=["mention:9"])

        assert row()["unread"] and not row()["done"]
        post("read")
        assert not row()["unread"] and not row()["done"]
        post("done")
        assert row()["done"]
        post("read")                                                 # Undo
        assert not row()["done"]
        post("done")
        post("unread")
        assert row()["unread"] and not row()["done"]
    finally:
        d.close()


def test_mentions_off_removes_the_section():
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(test_config_text(), encoding="utf-8")     # mentions disabled
    store.write_inbox([], ME, next_poll_at=None)
    store.write_mentions([mention("9")])
    assert all(s.get("kind") != "mentions" for s in web.current_payload()["sections"])


def test_menu_bar_hides_done_mentions_and_everything_when_off():
    ms = [mention("1"), mention("2")]
    store.write_mentions(ms)
    store.mark_mentions([ms[1]], done=True)
    on, off = cfg_with(enabled=True).settings, config_mod.parse(test_config_text()).settings
    assert [m.id for m in menubar.visible_mentions(on, store.read_seen())] == ["1"]
    assert [m.id for m in menubar.visible_mentions(on, None)] == ["1", "2"]     # read tracking off: nothing is done
    assert menubar.visible_mentions(off, store.read_seen()) == []


# ---------------------------------------------------------------- silent team mentions


def silent_team_cfg():
    return config_mod.parse(config_text(notify_team_mention=False, enabled=True))


def test_team_mentions_stay_listed_but_do_not_notify_when_team_mention_is_off(sent):
    cfg, client = silent_team_cfg(), FakeClient()
    poller.sync_mentions(client, cfg)                                 # first fetch, silent
    client.threads = [thread("1", number=5), thread("2", reason="team_mention", number=8)]
    poller.sync_mentions(client, cfg)
    assert sorted(m.id for m in store.read_mentions()) == ["1", "2"]
    assert len(sent) == 1 and sent[0][0][0] == "You were mentioned"   # only the personal one pops up


def test_only_team_mentions_means_no_notification_at_all(sent):
    cfg, client = silent_team_cfg(), FakeClient()
    poller.sync_mentions(client, cfg)
    client.threads = [thread("2", reason="team_mention", number=8)]
    poller.sync_mentions(client, cfg)
    assert sent == [] and [m.id for m in store.read_mentions()] == ["2"]


def test_silenced_team_mentions_do_not_trigger_the_burst_summary(sent):
    cfg, client = silent_team_cfg(), FakeClient()
    poller.sync_mentions(client, cfg)
    client.threads = [thread("p", number=1)] + [thread(f"t{i}", reason="team_mention", number=10 + i) for i in range(6)]
    poller.sync_mentions(client, cfg)
    assert len(sent) == 1 and sent[0][0][0] == "You were mentioned"   # one real notification, not a "7 new mentions" summary
    assert len(store.read_mentions()) == 7


def test_team_mentions_notify_by_default_and_the_master_switch_still_wins(sent):
    cfg, client = cfg_with(enabled=True), FakeClient()
    poller.sync_mentions(client, cfg)
    client.threads = [thread("2", reason="team_mention", number=8)]
    poller.sync_mentions(client, cfg)
    assert len(sent) == 1 and sent[0][0][0] == "Your team was mentioned"

    sent.clear()
    off = config_mod.parse(config_text(notify_mention=False, notify_team_mention=True, enabled=True))
    client.threads = [thread("3", number=9), thread("4", reason="team_mention", number=10)]
    poller.sync_mentions(client, off)                                 # mention: false silences everything
    assert sent == []


def test_a_silent_team_mention_is_still_unread_and_can_be_marked_done():
    m = mention("9", reason="team_mention")
    assert store.is_mention_unread(store.read_seen(), m)
    store.mark_mentions([m], done=True)
    assert store.is_mention_done(store.read_seen(), m)
