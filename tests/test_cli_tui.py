import asyncio
import json

from typer.testing import CliRunner

from prn import store
from prn.cli import app
from prn.engine import evaluate
from prn.github import parse_pr

from .conftest import ME, node

runner = CliRunner()


def seed_inbox(cfg):
    prs = [parse_pr(node(author="alice", number=301, title="Teammate thing")),
           parse_pr(node(author=ME, number=302, title="My thing", decision="APPROVED"))]
    store.write_inbox(evaluate(prs, cfg, ME), ME, next_poll_at=None)


def test_config_init_validate_and_sections():
    assert runner.invoke(app, ["config", "init"]).exit_code == 0
    r = runner.invoke(app, ["config", "validate"])
    assert r.exit_code == 0 and "7 sections" in r.output
    r = runner.invoke(app, ["sections", "add", "Infra only", "--repos", "acme/api",
                            "--review-requested", "any", "--my-review", "not:approved", "--max-approvals", "1",
                            "--position", "1", "--badge"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["sections", "list"])
    assert "Infra only" in r.output
    r = runner.invoke(app, ["sections", "move", "Infra only", "--to", "8"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["sections", "remove", "Infra only"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["sections", "add", "Bad", "--review-requested", "nobody"])
    assert r.exit_code == 1 and "review_requested" in r.output


def test_list_text_and_json(cfg):
    runner.invoke(app, ["config", "init"])
    seed_inbox(cfg)
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0
    assert "Teammates Need Review (1)" in r.output and "web#301" in r.output
    r = runner.invoke(app, ["list", "--json", "--section", "my approved prs"])
    data = json.loads(r.output)
    assert data["sections"][0]["prs"][0]["number"] == 302


def test_list_without_data_fails():
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 1 and "no inbox data" in r.output


def test_tui_renders_and_navigates(cfg):
    from textual.widgets import OptionList, Static

    from prn.tui import PRNTui

    runner.invoke(app, ["config", "init"])
    seed_inbox(cfg)

    async def go():
        tui = PRNTui()
        async with tui.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ol = tui.query_one("#list", OptionList)
            assert ol.option_count > 7
            assert tui._current_pr().number == 301
            await pilot.press("down")
            await pilot.pause()
            detail = str(tui.query_one("#detail", Static).render())
            assert "My thing" in detail or "Teammate thing" in detail
            # live reload when inbox changes
            store.write_inbox(evaluate([], cfg, ME), ME, next_poll_at=None)
            tui._reload(force=True)
            await pilot.pause()
            assert tui._current_pr() is None
            await pilot.press("slash")
            await pilot.press("q")  # typed into filter, should not quit
            await pilot.pause()
            assert tui.is_running
            await pilot.press("escape")
    asyncio.run(go())
