import httpx
import pytest

from prn import store, web
from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node, review, write_config


def publish(cfg, *nodes):
    prs = [parse_pr(n) for n in nodes]
    store.write_inbox(evaluate(prs, cfg, ME), ME, next_poll_at=None)
    return prs


@pytest.fixture
def dash():
    write_config()
    calls = []
    d = web.Dashboard(port=0, do_refresh=lambda: calls.append(1))
    d.start()
    d.refreshes = calls
    yield d
    d.close()


def get(d, path, **kw):
    return httpx.get(d.url.rstrip("/") + path, **kw)


def data(d):
    return get(d, f"/data?token={d.token}").json()


def test_payload_without_inbox():
    assert web.payload(None, {})["empty"] is True


def test_payload_rows_and_unread(cfg):
    prs = publish(cfg, node(number=1, ci="FAILURE", decision="APPROVED"))
    out = web.payload(store.read_inbox(), {})
    sec = next(s for s in out["sections"] if s["prs"])
    row = sec["prs"][0]
    assert (row["key"], row["unread"]) == (prs[0].key, True)
    assert row["ci"] == "none" and not row["mine"]   # CI is hidden on other people's PRs

    store.mark_read([(prs[0], sec["name"])], ME)
    row = next(s for s in web.payload(store.read_inbox(), store.read_seen())["sections"] if s["prs"])["prs"][0]
    assert row["unread"] is False


def test_read_tracking_off_means_everything_unread(cfg):
    prs = publish(cfg, node(number=1))
    store.mark_read([(prs[0], "Teammates Need Review")], ME)
    out = web.payload(store.read_inbox(), None)
    assert all(p["unread"] for s in out["sections"] for p in s["prs"])


def test_requires_token_and_host(dash):
    assert get(dash, "/data").status_code == 403
    assert get(dash, "/data?token=nope").status_code == 403
    assert get(dash, f"/data?token={dash.token}", headers={"Host": "evil.example"}).status_code == 403
    assert get(dash, "/", headers={"Host": "evil.example"}).status_code == 403
    assert httpx.post(dash.url + "read", content="[]").status_code == 403


def test_page_embeds_token(dash):
    r = get(dash, "/")
    assert r.status_code == 200 and dash.token in r.text and "__TOKEN__" not in r.text


def test_read_and_unread_round_trip(dash, cfg):
    prs = publish(cfg, node(number=1))

    def unread():
        return next(p for s in data(dash)["sections"] for p in s["prs"])["unread"]

    assert unread()
    r = httpx.post(dash.url + f"read?token={dash.token}", json=[prs[0].key])
    assert r.status_code == 200 and not unread()
    httpx.post(dash.url + f"unread?token={dash.token}", json=[prs[0].key])
    assert unread()


def test_bad_body_is_rejected(dash):
    r = httpx.post(dash.url + f"read?token={dash.token}", content="{not json")
    assert r.status_code == 400
    r = httpx.post(dash.url + f"read?token={dash.token}", content='{"a": 1}')
    assert r.status_code == 400


def test_refresh_calls_poll_and_reports_error(dash):
    r = httpx.post(dash.url + f"refresh?token={dash.token}")
    assert r.json() == {"error": None} and dash.refreshes == [1]


def test_section_menu_mode_round_trips_through_the_inbox(cfg):
    from prn import config as config_mod

    text = config_mod.example_config_text().replace("your-org/repo-one", "acme/web").replace(
        "your-org/repo-two", "acme/api").replace("teammate-one", "alice").replace("teammate-two", "bob")
    text = text.replace("  - name: My Open PRs\n", "  - name: My Open PRs\n    menu: count\n")
    c = config_mod.parse(text)
    assert {s.name: s.menu for s in c.sections}["My Open PRs"] == "count"
    assert {s.name: s.menu for s in c.sections}["Teammates Need Review"] == "rows"

    store.write_inbox(evaluate([parse_pr(node(number=1))], c, ME), ME, next_poll_at=None)
    assert {s.name: s.menu for s in store.read_inbox().sections}["My Open PRs"] == "count"


def test_menu_mode_must_be_known():
    from prn import config as config_mod

    bad = config_mod.example_config_text().replace("  - name: My Open PRs\n", "  - name: My Open PRs\n    menu: nope\n")
    with pytest.raises(config_mod.ConfigError):
        config_mod.parse(bad)


def test_inbox_written_before_menu_existed_reads_as_rows():
    import json
    from prn import paths

    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.inbox_path().write_text(json.dumps(
        {"me": ME, "sections": [{"name": "Old", "notify": True, "badge": True, "prs": []}]}))
    assert store.read_inbox().sections[0].menu == "rows"


def test_ci_is_kept_on_my_prs(cfg):
    publish(cfg, node(number=1, author=ME, ci="FAILURE"))
    row = next(p for s in web.payload(store.read_inbox(), {})["sections"] for p in s["prs"])
    assert row["mine"] and row["ci"] == "failure"


def test_reviewers_humans_first_bots_dimmed_author_and_me_handled():
    pr = parse_pr(node(author="alice", reviews=[
        review("coderabbitai", "COMMENTED", bot=True), review("coderabbitai", "APPROVED", bot=True),
        review("carol", "CHANGES_REQUESTED"), review("dave", "COMMENTED"),
        review(ME, "APPROVED"), review("alice", "COMMENTED")]))
    got = web.reviewers(pr, ME)
    assert [(r["login"], r["state"], r["bot"]) for r in got] == [
        ("you", "approved", False), ("carol", "changes_requested", False), ("dave", "commented", False),
        ("coderabbitai", "approved", True)]


def test_dismissed_review_shows_as_commented():
    pr = parse_pr(node(reviews=[review("bob", "APPROVED"), review("bob", "DISMISSED")],
                       latest=[review("bob", "DISMISSED")]))
    assert [(r["login"], r["state"]) for r in web.reviewers(pr, ME)] == [("bob", "commented")]


def test_status_is_approved_then_draft_then_open():
    assert web.status(parse_pr(node(decision="APPROVED", draft=True))) == "approved"
    assert web.status(parse_pr(node(draft=True))) == "draft"
    assert web.status(parse_pr(node())) == "open"


def test_grouped_sections_become_one_table_with_a_status_per_row(cfg):
    publish(cfg, node(number=1, author=ME, decision="APPROVED"), node(number=2, author=ME),
            node(number=3, author=ME, draft=True), node(number=4, author="alice"))
    groups = {"My Approved PRs": "My PRs", "My Open PRs": "My PRs", "My Draft PRs": "My PRs"}
    out = web.payload(store.read_inbox(), {}, groups)
    names = [s["name"] for s in out["sections"]]
    assert names.count("My PRs") == 1 and "My Open PRs" not in names and "Teammates Need Review" in names
    mine = next(s for s in out["sections"] if s["name"] == "My PRs")
    assert mine["grouped"] is True
    assert [(p["key"].split("#")[1], p["status"]) for p in mine["prs"]] == [("1", "approved"), ("2", "open"), ("3", "draft")]
    assert not next(s for s in out["sections"] if s["name"] == "Teammates Need Review")["grouped"]


def test_group_keeps_the_position_of_its_first_section(cfg):
    publish(cfg, node(number=1, author=ME))
    groups = {"My Open PRs": "My PRs", "My Draft PRs": "My PRs"}
    names = [s["name"] for s in web.payload(store.read_inbox(), {}, groups)["sections"]]
    assert names.index("My PRs") == names.index("My Approved PRs") + 1


def test_config_accepts_a_section_group():
    from prn import config as config_mod

    text = config_mod.example_config_text().replace("  - name: My Open PRs\n", "  - name: My Open PRs\n    group: My PRs\n")
    assert {s.name: s.group for s in config_mod.parse(text).sections}["My Open PRs"] == "My PRs"


# ---------------------------------------------------------------- done

SEC = "Teammates Need Review"


def done_pr(**kw):
    return parse_pr(node(number=700, **kw))


def test_done_hides_until_something_new_happens():
    before = done_pr(comments=1)
    store.mark_read([(before, SEC)], ME, done=True)
    assert store.is_done(store.read_seen(), before, SEC, ME)
    assert not store.is_done(store.read_seen(), done_pr(comments=2), SEC, ME)   # new comment: back
    assert not store.is_done(store.read_seen(), done_pr(comments=1, head="new-sha"), SEC, ME)
    assert not store.is_done(store.read_seen(), before, "Another Section", ME)    # moved sections: back


def test_read_without_done_is_not_done_and_marking_read_clears_done():
    pr = done_pr()
    store.mark_read([(pr, SEC)], ME)
    assert not store.is_done(store.read_seen(), pr, SEC, ME)
    store.mark_read([(pr, SEC)], ME, done=True)
    store.mark_read([(pr, SEC)], ME)             # Undo
    seen = store.read_seen()
    assert not store.is_done(seen, pr, SEC, ME) and not store.is_unread(seen, pr, SEC, ME)


def test_mark_unread_clears_done():
    pr = done_pr()
    store.mark_read([(pr, SEC)], ME, done=True)
    store.mark_unread([pr.key])
    assert not store.is_done(store.read_seen(), pr, SEC, ME)


def test_hide_done_removes_prs_and_badge_counts(cfg):
    prs = publish(cfg, node(number=1), node(number=2))
    inbox = store.read_inbox()
    sec = next(s for s in inbox.sections if s.prs)
    assert inbox.badge_count == 2
    store.mark_read([(prs[0], sec.name)], ME, done=True)
    visible = store.hide_done(inbox, store.read_seen())
    assert visible.badge_count == 1 and visible.total == 1
    assert inbox.total == 2                      # the original is untouched


def test_a_done_pr_still_in_the_inbox_is_never_pruned():
    import json
    import time
    from prn import paths

    old, other = done_pr(), parse_pr(node(number=701))
    store.mark_read([(old, SEC)], ME, done=True)
    data = json.loads(paths.seen_path().read_text())
    data["prs"][old.key]["at"] = time.time() - 60 * 24 * 3600        # marked done 60 days ago
    paths.seen_path().write_text(json.dumps(data))
    store.mark_read([(other, SEC)], ME, live_keys={old.key, other.key})
    assert store.is_done(store.read_seen(), old, SEC, ME)


def test_done_endpoint_and_undo_through_the_server(dash, cfg):
    prs = publish(cfg, node(number=1))

    def row():
        return next(p for s in data(dash)["sections"] for p in s["prs"])

    assert not row()["done"] and data(dash)["tracking"] is True
    httpx.post(dash.url + f"done?token={dash.token}", json=[prs[0].key])
    assert row()["done"] and not row()["unread"]
    httpx.post(dash.url + f"read?token={dash.token}", json=[prs[0].key])   # Undo
    assert not row()["done"] and not row()["unread"]


def test_nothing_is_done_when_read_tracking_is_off(cfg):
    prs = publish(cfg, node(number=1))
    store.mark_read([(prs[0], SEC)], ME, done=True)
    out = web.payload(store.read_inbox(), None)
    assert out["tracking"] is False
    assert not any(p["done"] for s in out["sections"] for p in s["prs"])


def test_week_start_is_monday_midnight_local():
    from datetime import datetime

    wed = datetime(2026, 10, 14, 15, 30).timestamp()          # a Wednesday afternoon
    assert datetime.fromtimestamp(store.week_start(wed)) == datetime(2026, 10, 12, 0, 0)
    sun = datetime(2026, 10, 18, 23, 59).timestamp()          # still the same week
    assert datetime.fromtimestamp(store.week_start(sun)) == datetime(2026, 10, 12, 0, 0)
    mon = datetime(2026, 10, 19, 0, 0).timestamp()            # the next week starts
    assert datetime.fromtimestamp(store.week_start(mon)) == datetime(2026, 10, 19, 0, 0)


def test_entries_from_earlier_weeks_are_cleared_unless_the_pr_is_still_in_the_inbox():
    import json
    import time
    from prn import paths

    last_week, this_week, live = done_pr(), parse_pr(node(number=701)), parse_pr(node(number=702))
    for pr in (last_week, this_week, live):
        store.mark_read([(pr, SEC)], ME, done=True)
    data = json.loads(paths.seen_path().read_text())
    start = store.week_start(time.time())
    data["prs"][last_week.key]["at"] = start - 3600           # an hour before this week began
    data["prs"][this_week.key]["at"] = start + 1              # this week
    data["prs"][live.key]["at"] = start - 40 * 24 * 3600      # ancient, but still open and in the inbox
    paths.seen_path().write_text(json.dumps(data))
    store.mark_read([], ME, live_keys={live.key})             # any write triggers the prune
    assert set(store.read_seen()) == {this_week.key, live.key}


# ---------------------------------------------------------------- collapsed / dependency bumps


def bumps_config():
    from prn import config as config_mod

    text = (config_mod.example_config_text().replace("your-org/repo-one", "acme/web")
            .replace("your-org/repo-two", "acme/api").replace("teammate-one", "alice").replace("teammate-two", "bob"))
    text = text.replace("groups:\n", "groups:\n  bots: [dependabot, renovate]\n", 1)
    text = text.replace("  - name: Team Review Requested\n", """  - name: Dependency Bumps
    notify: false
    badge: false
    menu: hidden
    collapsed: true
    filters:
      authors: [group:bots]
      review_requested: team
  - name: Team Review Requested
""")
    return config_mod.parse(text)


def test_bot_prs_requesting_my_team_land_in_dependency_bumps_not_team_review():
    c = bumps_config()
    prs = [parse_pr(node(number=n, author=a, req_teams=["core"]), requested_from_me=True)
           for n, a in ((1, "dependabot"), (2, "renovate"), (3, "carol"))]
    got = {r.name: [p.number for p in r.prs] for r in evaluate(prs, c, ME)}
    assert got["Dependency Bumps"] == [1, 2] and got["Team Review Requested"] == [3]


def test_dependency_bumps_adds_no_extra_github_searches():
    from prn.github import plan_queries

    base = plan_queries(bumps_config(), ME)
    from prn import config as config_mod
    without = plan_queries(config_mod.parse(config_mod.example_config_text().replace("your-org/repo-one", "acme/web")
                                            .replace("your-org/repo-two", "acme/api")), ME)
    assert not any("author:dependabot" in q or "author:renovate" in q for q in base)
    assert len(base) == len(without)


def test_collapsed_default_comes_from_config_and_group_uses_its_first_section():
    c = bumps_config()
    publish(c, node(number=1, author="dependabot", req_teams=["core"]))
    out = web.payload(store.read_inbox(), {}, collapsed={s.name for s in c.sections if s.collapsed})
    by = {s["name"]: s["collapsed"] for s in out["sections"]}
    assert by["Dependency Bumps"] is True and by["Teammates Need Review"] is False

    groups = {"My Approved PRs": "My PRs", "My Open PRs": "My PRs"}
    out = web.payload(store.read_inbox(), {}, groups, collapsed={"My Approved PRs"})
    assert next(s for s in out["sections"] if s["name"] == "My PRs")["collapsed"] is True
