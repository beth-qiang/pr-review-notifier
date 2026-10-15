import asyncio

from typer.testing import CliRunner

from prn import store
from prn.cli import app
from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node, review

runner = CliRunner()
SEC = "Teammates Need Review"


def pr_from(**kw):
    return parse_pr(node(number=500, **kw))


def unread_after(before, after, section_before=SEC, section_after=SEC):
    store.mark_read([(before, section_before)], ME)
    return store.is_unread(store.read_seen(), after, section_after, ME)


def test_never_opened_is_unread():
    assert store.is_unread(store.read_seen(), pr_from(), SEC, ME)


def test_nothing_new_stays_read():
    p = pr_from(reviews=[review("bob", "COMMENTED")], comments=2)
    assert not unread_after(p, p)


def test_others_activity_brings_it_back():
    base = pr_from(comments=1)
    assert unread_after(base, pr_from(comments=2))
    later = review("bob", "APPROVED")
    later["submittedAt"] = "2026-10-15T12:00:00Z"
    assert unread_after(base, pr_from(comments=1, reviews=[later]))
    assert unread_after(base, pr_from(comments=1, head="new-sha"))


def test_my_own_activity_does_not():
    base = pr_from(comments=1, comment_authors=["bob"])
    mine = review(ME, "APPROVED")
    mine["submittedAt"] = "2026-10-15T12:00:00Z"
    assert not unread_after(base, pr_from(comment_authors=["bob", ME], reviews=[mine]))
    my_pr = pr_from(author=ME, ci="PENDING")
    assert not unread_after(my_pr, pr_from(author=ME, head="my-new-push", ci="PENDING"))


def test_ci_finishing_on_my_pr_counts():
    assert unread_after(pr_from(author=ME, ci="PENDING"), pr_from(author=ME, ci="FAILURE"))


def test_re_request_counts_but_request_disappearing_does_not():
    assert unread_after(pr_from(), pr_from(req_users=[ME]))
    assert not unread_after(pr_from(req_users=[ME]), pr_from())


def test_changing_section_counts():
    p = pr_from()
    assert unread_after(p, p, section_after="Direct Review Request")


def test_mark_unread():
    p = pr_from()
    store.mark_read([(p, SEC)], ME)
    store.mark_unread([p.key])
    assert store.is_unread(store.read_seen(), p, SEC, ME)


def test_tui_fades_opened_rows_and_toggles(cfg, monkeypatch):
    from prn.tui import PRNTui

    runner.invoke(app, ["config", "init"])
    prs = [parse_pr(node(author="alice", number=301, title="Teammate thing")),
           parse_pr(node(author="bob", number=303, title="Other teammate thing"))]
    store.write_inbox(evaluate(prs, cfg, ME), ME, next_poll_at=None)
    opened = []
    monkeypatch.setattr("prn.tui.webbrowser.open", opened.append)

    async def go():
        tui = PRNTui()
        async with tui.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            assert len(tui._unread) == 2
            first = tui._current_pr().key
            await pilot.press("o")
            await pilot.pause()
            assert opened and first not in tui._unread and len(tui._unread) == 1
            await pilot.press("m")
            await pilot.pause()
            assert first in tui._unread
            await pilot.press("M")
            await pilot.pause()
            assert not tui._unread
    asyncio.run(go())
