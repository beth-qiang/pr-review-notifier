import json

import httpx
import pytest
import respx

from prn import config as config_mod
from prn import paths, poller, store
from prn.github import (API_URL, GROW_AFTER, MIN_PAGE_SIZE, NOTIFICATIONS_URL, PAGE_SIZE, GatewayError,
                        GitHubClient, GitHubError, nodes_query, search_query)

from .conftest import ME, node, test_config_text
from .test_mentions import config_text, thread

HTML_502 = "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body><center><h1>502 Bad Gateway</h1></center></body>\r\n</html>"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("prn.github.time.sleep", lambda s: None)


def gateway_timeout(max_size):
    """A GitHub that answers 502 to any search page larger than `max_size`, and serves 45 PRs otherwise."""
    pool = [node(number=n) for n in range(1, 46)]
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if "viewer" in body["query"] and "search" not in body["query"]:
            return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
        v = body["variables"]
        calls.append(v["first"])
        if v["first"] > max_size:
            return httpx.Response(502, text=HTML_502)
        start = int(v["after"] or 0)
        page = pool[start:start + v["first"]]
        end = start + len(page)
        return httpx.Response(200, json={"data": {
            "rateLimit": {"remaining": 4999, "resetAt": "x", "cost": 1},
            "search": {"issueCount": len(pool), "pageInfo": {"hasNextPage": end < len(pool), "endCursor": str(end)},
                       "nodes": page}}})

    handler.calls, handler.pool = calls, pool
    return handler


# ---------------------------------------------------------------- errors


@respx.mock
def test_a_gateway_error_is_retried_then_raised_with_a_readable_message():
    route = respx.post(API_URL).mock(return_value=httpx.Response(502, text=HTML_502))
    with pytest.raises(GatewayError) as exc:
        GitHubClient(token="t").graphql("query { viewer { login } }")
    assert route.call_count == 3
    assert "slow or overloaded" in str(exc.value) and "502 Bad Gateway" in str(exc.value) and "<html" not in str(exc.value)


@respx.mock
def test_attempts_1_does_not_repeat_a_heavy_request():
    route = respx.post(API_URL).mock(return_value=httpx.Response(504, text=HTML_502))
    with pytest.raises(GatewayError):
        GitHubClient(token="t").graphql("query { viewer { login } }", attempts=1)
    assert route.call_count == 1


@respx.mock
def test_other_http_errors_never_dump_html_into_the_message():
    respx.post(API_URL).mock(return_value=httpx.Response(500, text="<html><head><title>Server Error</title></head></html>"))
    with pytest.raises(GitHubError) as exc:
        GitHubClient(token="t").graphql("query { viewer { login } }")
    assert not isinstance(exc.value, GatewayError)
    assert str(exc.value) == "GitHub API error 500: Server Error"


@respx.mock
def test_notifications_errors_are_readable_too():
    respx.get(NOTIFICATIONS_URL).mock(return_value=httpx.Response(502, text=HTML_502))
    with pytest.raises(GitHubError) as exc:
        GitHubClient(token="t").notifications()
    assert "<html" not in str(exc.value) and "502 Bad Gateway" in str(exc.value)


# ---------------------------------------------------------------- adaptive search


@respx.mock
def test_search_halves_the_page_size_until_github_answers_and_gets_every_pr():
    fake = gateway_timeout(max_size=20)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    nodes = client.search_prs("q")
    assert [n["number"] for n in nodes] == list(range(1, 46))        # nothing missing, nothing duplicated
    assert fake.calls[:3] == [50, 25, 12] and client.page_size == 12


@respx.mock
def test_the_reduced_page_size_is_remembered_so_the_next_search_does_not_fail_again():
    fake = gateway_timeout(max_size=20)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    client.search_prs("q")
    fake.calls.clear()
    client.search_prs("q")
    assert fake.calls[0] == 12 and PAGE_SIZE not in fake.calls


@respx.mock
def test_gives_up_at_the_minimum_page_size():
    fake = gateway_timeout(max_size=0)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    with pytest.raises(GatewayError):
        client.search_prs("q")
    assert client.page_size == MIN_PAGE_SIZE and fake.calls[:4] == [50, 25, 12, 6]


@respx.mock
def test_the_page_size_grows_back_after_enough_clean_searches(monkeypatch):
    monkeypatch.setattr("prn.github.GROW_AFTER", 3)
    fake = gateway_timeout(max_size=50)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    client.page_size = 12
    for _ in range(2):
        client.search_prs("q")
    assert client.page_size == 12
    client.search_prs("q")
    assert client.page_size == 24
    assert GROW_AFTER == 30                                          # the real default isn't touched


@respx.mock
def test_a_search_that_had_to_shrink_does_not_count_toward_growing_back(monkeypatch):
    monkeypatch.setattr("prn.github.GROW_AFTER", 1)
    fake = gateway_timeout(max_size=20)
    respx.post(API_URL).mock(side_effect=fake)
    client = GitHubClient(token="t")
    client.search_prs("q")                                           # shrinks 50 -> 12, even though GROW_AFTER is 1
    assert client.page_size == 12


@respx.mock
def test_refetching_by_id_splits_a_batch_github_times_out_on():
    ids = [f"PR_{n}" for n in range(1, 9)]
    sizes = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        batch = body["variables"]["ids"]
        sizes.append(len(batch))
        if len(batch) > 2:
            return httpx.Response(504, text=HTML_502)
        return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 1, "resetAt": "x", "cost": 1},
                                                  "nodes": [node(number=int(i.split("_")[1])) for i in batch]}})

    respx.post(API_URL).mock(side_effect=handler)
    got = GitHubClient(token="t").nodes_by_id(ids)
    assert sorted(n["number"] for n in got) == list(range(1, 9)) and sizes[0] == 8 and max(sizes[1:]) <= 4


# ---------------------------------------------------------------- merge fields


def test_merge_fields_are_left_out_unless_asked_for():
    assert "mergeStateStatus" in search_query() and "mergeable" in nodes_query()
    for q in (search_query(merge=False), nodes_query(merge=False)):
        assert "mergeStateStatus" not in q and "mergeable" not in q and "reviewDecision" in q


def test_only_conflicts_and_ready_to_merge_filters_need_merge_state():
    cfg = config_mod.parse(test_config_text())
    assert cfg.needs_merge_state() is False
    for flt in ("conflicts: false", "ready_to_merge: true"):
        text = test_config_text().replace("  - name: My Draft PRs\n", f"  - name: Mergeable\n    filters:\n      {flt}\n  - name: My Draft PRs\n")
        assert config_mod.parse(text).needs_merge_state() is True


@respx.mock
def test_a_poll_requests_merge_state_only_when_a_section_uses_it():
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if "viewer" in body["query"] and "search" not in body["query"]:
            return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
        seen.append("mergeStateStatus" in body["query"])
        return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 1, "resetAt": "x", "cost": 1},
                                                  "search": {"issueCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}})

    respx.post(API_URL).mock(side_effect=handler)
    for flt, expected in ((None, False), ("conflicts: false", True)):
        text = test_config_text()
        if flt:
            text = text.replace("  - name: My Draft PRs\n", f"  - name: Mergeable\n    filters:\n      {flt}\n  - name: My Draft PRs\n")
        paths.config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text(text, encoding="utf-8")
        poller._me_cache.clear()
        seen.clear()
        poller.poll_once(client=GitHubClient(token="t"), send_notifications=False)
        assert seen and set(seen) == {expected}


# ---------------------------------------------------------------- mentions survive a failing PR poll


def mock_failing_pr_search_with_a_mention():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if "viewer" in body["query"] and "search" not in body["query"]:
            return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
        return httpx.Response(502, text=HTML_502)

    respx.post(API_URL).mock(side_effect=handler)
    respx.get(NOTIFICATIONS_URL).mock(return_value=httpx.Response(200, json=[thread("1")]))


@respx.mock
def test_poll_once_still_syncs_mentions_when_the_pr_search_fails_and_records_a_readable_error():
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(config_text(enabled=True), encoding="utf-8")
    mock_failing_pr_search_with_a_mention()
    poller._me_cache.clear()
    with pytest.raises(GatewayError):
        poller.poll_once(client=GitHubClient(token="t"), send_notifications=False)
    assert [m.id for m in store.read_mentions()] == ["1"]
    error = json.loads(paths.inbox_path().read_text())["error"]
    assert "slow or overloaded" in error and "<html" not in error


@respx.mock
def test_the_menu_bar_poller_still_syncs_mentions_when_the_pr_search_fails():
    mock_failing_pr_search_with_a_mention()
    poller._me_cache.clear()
    p = poller.Poller(client_factory=lambda: GitHubClient(token="t"))
    with pytest.raises(GatewayError):
        p.tick(config_mod.parse(config_text(enabled=True)))
    assert [m.id for m in store.read_mentions()] == ["1"]
    assert p.last_check_at is None                                   # a failed poll isn't a successful check
