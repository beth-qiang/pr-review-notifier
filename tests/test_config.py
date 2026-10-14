import pytest

from prn import config as config_mod
from prn import paths


def test_default_config_is_valid(cfg):
    assert [s.name for s in cfg.sections] == [
        "Teammates Need Review",
        "Teammates Reviewed Not Approved by Me",
        "Team Review Requested",
        "Direct Review Request",
        "My Approved PRs",
        "My Open PRs",
        "My Draft PRs",
    ]
    assert cfg.defaults.repos == ["acme/web", "acme/api"]
    assert cfg.settings.poll_interval_seconds == 300
    assert [s.name for s in cfg.sections if s.badge] == [
        "Teammates Need Review", "Teammates Reviewed Not Approved by Me",
        "Team Review Requested", "Direct Review Request",
    ]
    assert cfg.groups["teammates"] == ["alice", "bob"]


def test_example_config_is_valid_and_generic():
    text = config_mod.example_config_text()
    ex = config_mod.parse(text)
    assert ex.defaults.repos == ["your-org/repo-one", "your-org/repo-two"]
    assert ex.groups["teammates"] == ["teammate-one", "teammate-two"]


def test_config_pointer(tmp_path, monkeypatch):
    monkeypatch.delenv("PRN_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_cfg = tmp_path / "repo" / "config.yaml"
    config_mod.init(path=repo_cfg)
    assert paths.config_path() == repo_cfg.resolve()
    assert config_mod.load().sections
    monkeypatch.setenv("PRN_CONFIG", str(tmp_path / "explicit.yaml"))
    assert paths.config_path() == tmp_path / "explicit.yaml"


def test_resolve_people(cfg):
    got = cfg.resolve_people(["@me", "group:teammates", "SomeOne"], "me-user")
    assert "me-user" in got and "alice" in got and "someone" in got


@pytest.mark.parametrize("bad,needle", [
    ("defaults: {repos: [nope]}\nsections: [{name: a}]", "owner/name"),
    ("defaults: {repos: [a/b]}\nsections: [{name: a}, {name: A}]", "duplicate section"),
    ("defaults: {repos: [a/b]}\nsections: [{name: a, filters: {authors: [group:x]}}]", "unknown group"),
    ("defaults: {repos: [a/b]}\nsections: [{name: a, filters: {bogus: 1}}]", "bogus"),
    ("defaults: {repos: [a/b]}\nsections: [{name: a, filters: {approvals: {min: 3, max: 1}}}]", "min cannot"),
    ("defaults: {repos: [a/b]}\nsettings: {poll_interval_seconds: 5}\nsections: [{name: a}]", "poll_interval"),
    ("defaults: [unclosed", "parse YAML"),
])
def test_invalid_configs(bad, needle):
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.parse(bad)
    assert needle in str(exc.value)


def test_editor_preserves_comments_and_validates():
    config_mod.init()
    ed = config_mod.ConfigEditor()
    ed.add_section({"name": "Labeled", "notify": False, "badge": False, "filters": {"labels": ["urgent"]}}, position=0)
    ed.move_section("My Draft PRs", 1)
    ed.remove_section("My Open PRs")
    cfg = ed.save()
    text = paths.config_path().read_text()
    assert "# Sections are evaluated top to bottom" in text
    assert [s.name for s in cfg.sections][:2] == ["Labeled", "My Draft PRs"]
    assert "My Open PRs" not in [s.name for s in cfg.sections]


def test_editor_rejects_invalid_and_leaves_file_untouched():
    config_mod.init()
    before = paths.config_path().read_text()
    ed = config_mod.ConfigEditor()
    ed.add_section({"name": "Bad", "filters": {"review_requested": "nobody"}})
    with pytest.raises(config_mod.ConfigError):
        ed.save()
    assert paths.config_path().read_text() == before
