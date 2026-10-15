import json

import httpx
import respx
from typer.testing import CliRunner

from prn import config as config_mod
from prn import paths
from prn.cli import app
from prn.github import API_URL

from .conftest import ME

runner = CliRunner()


def fake_github(request: httpx.Request):
    body = json.loads(request.content)
    q = body["query"]
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {"login": ME}}})
    if "repository(" in q:
        exists = body["variables"]["n"] != "missing"
        return httpx.Response(200, json={"data": {"repository": {"nameWithOwner": "x"} if exists else None}})
    # reviewed-by search for teammate suggestions
    nodes = ([{"author": {"__typename": "User", "login": "alice"}}] * 3
             + [{"author": {"__typename": "User", "login": "bob"}}] * 2
             + [{"author": {"__typename": "Bot", "login": "dependabot"}}]
             + [{"author": {"__typename": "User", "login": ME}}])
    return httpx.Response(200, json={"data": {"search": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}})


@respx.mock
def test_first_run_wizard(tmp_path, monkeypatch):
    respx.post(API_URL).mock(side_effect=fake_github)
    target = tmp_path / "repo" / "config.yaml"
    answers = "\n".join([
        "acme/web, acme/missing",   # repos: one missing
        "n",                        # don't keep missing
        "acme/web acme/api",        # repos (space separated also OK)
        "1, carol",                 # teammates: suggestion #1 (alice) + carol
        "y", "y", "y", "y", "y",    # keep first 5 sections
        "n", "n",                   # drop My Open PRs, My Draft PRs
        "aggressive",               # poll strategy
        "120",                      # poll interval
        "y", "n", "y", "y",         # notifications (re_review off)
    ]) + "\n"
    r = runner.invoke(app, ["setup", "--config", str(target)], input=answers)
    assert r.exit_code == 0, r.output
    assert "alice" in r.output and "dependabot" not in r.output

    cfg = config_mod.load(target)
    assert cfg.defaults.repos == ["acme/web", "acme/api"]
    assert cfg.groups["teammates"] == ["alice", "carol"]
    assert [s.name for s in cfg.sections][-1] == "My Approved PRs"
    assert cfg.settings.poll_interval_seconds == 120
    assert cfg.settings.poll_strategy == "aggressive"
    assert cfg.settings.notifications.re_review_requested is False
    text = target.read_text()
    assert "# Sections are evaluated top to bottom" in text   # comments preserved
    assert "your-org" not in text and "teammate-one" not in text


@respx.mock
def test_rerun_keep_and_reconfigure_with_backup(tmp_path):
    respx.post(API_URL).mock(side_effect=fake_github)
    target = tmp_path / "config.yaml"
    from .conftest import test_config_text
    target.write_text(test_config_text())

    r = runner.invoke(app, ["setup", "--config", str(target)], input="k\n")
    assert r.exit_code == 0 and "Keeping your existing config" in r.output
    assert not list(tmp_path.glob("config.yaml.bak-*"))

    # Reconfigure: Enter keeps current repos and teammates as defaults
    answers = "r\n\n\n" + "y\n" * 7 + "\n\n" + "y\n" * 4
    r = runner.invoke(app, ["setup", "--config", str(target)], input=answers)
    assert r.exit_code == 0, r.output
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1
    cfg = config_mod.load(target)
    assert cfg.defaults.repos == ["acme/web", "acme/api"]
    assert cfg.groups["teammates"] == ["alice", "bob"]


def test_wizard_without_github(tmp_path, monkeypatch):
    # Simulate a machine with no gh / terminal-notifier, even when run on a Mac that has them.
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("prn.github.find_binary", lambda name: None)
    monkeypatch.setattr("prn.setup_wizard.find_binary", lambda name: None)
    target = tmp_path / "config.yaml"
    answers = "acme/web\nalice\n" + "y\n" * 7 + "\n300\n" + "y\n" * 4
    r = runner.invoke(app, ["setup", "--config", str(target)], input=answers)
    assert r.exit_code == 0, r.output
    assert "Can't reach GitHub" in r.output
    assert config_mod.load(target).settings.notifier == "osascript"


def test_wizard_cancel_writes_nothing(tmp_path):
    target = tmp_path / "config.yaml"
    r = runner.invoke(app, ["setup", "--config", str(target)], input="")
    assert r.exit_code != 0
    assert not target.exists()
