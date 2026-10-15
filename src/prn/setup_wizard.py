"""Interactive first-run (and re-run) configuration wizard: `prn setup`."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from . import config as config_mod
from . import paths
from .config import REPO_RE, ConfigEditor, ConfigError
from .github import GitHubClient, GitHubError, find_binary

USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
PLACEHOLDER_REPO = "your-org/"
PLACEHOLDER_USER = "teammate-"

NOTIFICATION_LABELS = {
    "new_in_section": "A PR enters a section that has notify: true",
    "re_review_requested": "Someone re-requests your review",
    "my_pr_status": "Your PR gets a review decision, review, or comment",
    "ci_status": "CI on your PR passes or fails",
}

console = Console()


def _csv(text: str) -> list[str]:
    return [t.strip() for t in text.replace(" ", ",").split(",") if t.strip()]


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _client() -> Optional[GitHubClient]:
    try:
        return GitHubClient()
    except GitHubError as exc:
        console.print(f"[yellow]⚠ Can't reach GitHub ({exc}). Continuing without suggestions/validation.[/yellow]")
        return None


def _summary(cfg: config_mod.Config) -> None:
    t = Table(show_header=False, box=None)
    t.add_row("Repos", ", ".join(cfg.defaults.repos))
    t.add_row("Teammates", ", ".join(cfg.groups.get("teammates", [])) or "(none)")
    t.add_row("Sections", ", ".join(s.name for s in cfg.sections))
    s = cfg.settings
    t.add_row("Poll", f"{s.poll_strategy}: full poll every {s.poll_interval_seconds}s"
              + (f", change check every {s.fast_poll_interval_seconds}s" if s.poll_strategy == "aggressive" else ""))
    console.print(t)


# ---------------------------------------------------------------- steps


def _ask_repos(current: list[str], client: Optional[GitHubClient]) -> list[str]:
    console.rule("[bold]1/4  Repositories")
    console.print("Which repos should prn watch? Use owner/name, comma-separated.")
    default = ", ".join(r for r in current if not r.startswith(PLACEHOLDER_REPO))
    while True:
        raw = Prompt.ask("Repos", default=default or None)
        repos = _csv(raw or "")
        bad = [r for r in repos if not REPO_RE.match(r)]
        if not repos or bad:
            console.print(f"[red]Need at least one repo in owner/name form.[/red] {('Bad: ' + ', '.join(bad)) if bad else ''}")
            continue
        if client:
            missing = [r for r in repos if not client.repo_exists(r)]
            if missing:
                console.print(f"[yellow]⚠ Not found or no access: {', '.join(missing)}[/yellow]")
                if not Confirm.ask("Keep them anyway?", default=False):
                    continue
        return repos


def _ask_teammates(current: list[str], repos: list[str], client: Optional[GitHubClient], me: Optional[str]) -> list[str]:
    console.rule("[bold]2/4  Teammates")
    console.print("The [bold]teammates[/bold] group drives the 'Teammates …' sections.")
    current = [u for u in current if not u.startswith(PLACEHOLDER_USER)]
    suggestions: list[tuple[str, int]] = []
    if client and me:
        with console.status("Finding people whose PRs you've reviewed in the last 90 days…"):
            try:
                suggestions = client.frequent_review_partners(repos, me)
            except GitHubError as exc:
                console.print(f"[yellow]⚠ Couldn't fetch suggestions: {exc}[/yellow]")
    if suggestions:
        t = Table(title="People whose PRs you've reviewed (90 days)", title_justify="left")
        t.add_column("#", justify="right")
        t.add_column("Username")
        t.add_column("PRs reviewed", justify="right")
        t.add_column("")
        for i, (login, n) in enumerate(suggestions, 1):
            t.add_row(str(i), login, str(n), "[green]current[/green]" if login in current else "")
        console.print(t)
        console.print("Enter numbers from the table and/or usernames, comma-separated.")
    else:
        console.print("Enter GitHub usernames, comma-separated.")
    if current:
        console.print(f"[dim]Press Enter to keep the current list: {', '.join(current)}[/dim]")
    while True:
        raw = Prompt.ask("Teammates", default=", ".join(current) if current else "")
        chosen: list[str] = []
        bad: list[str] = []
        for tok in _csv(raw):
            if tok.isdigit() and suggestions:
                idx = int(tok) - 1
                if 0 <= idx < len(suggestions):
                    chosen.append(suggestions[idx][0])
                else:
                    bad.append(tok)
            elif USER_RE.match(tok.lstrip("@")):
                chosen.append(tok.lstrip("@").lower())
            else:
                bad.append(tok)
        if bad:
            console.print(f"[red]Not valid: {', '.join(bad)}[/red]")
            continue
        chosen = list(dict.fromkeys(chosen))  # de-dupe, keep order
        if not chosen and not Confirm.ask("No teammates: the 'Teammates …' sections will stay empty. OK?", default=False):
            continue
        return chosen


def _ask_sections(ed: ConfigEditor) -> None:
    console.rule("[bold]3/4  Sections")
    console.print("Sections are checked top to bottom, and each PR shows in the first one it matches.")
    names = ed.section_names()
    keep = [n for n in names if Confirm.ask(f"Keep [bold]{n}[/bold]?", default=True)]
    if not keep:
        console.print("[yellow]At least one section is required; keeping all.[/yellow]")
        return
    for n in names:
        if n not in keep:
            ed.remove_section(n)


def _ask_polling_and_notifications(ed: ConfigEditor) -> None:
    console.rule("[bold]4/4  Polling & notifications")
    settings = ed.doc["settings"]
    fast = int(settings.get("fast_poll_interval_seconds", 15))
    console.print("[bold]conservative[/bold]: a full poll every N seconds. [bold]aggressive[/bold]: a cheap check for "
                  f"changes every {fast} seconds so the inbox is near real-time, plus a full poll every N seconds.")
    settings["poll_strategy"] = Prompt.ask("Polling strategy", choices=["conservative", "aggressive"],
                                           default=str(settings.get("poll_strategy", "conservative")))
    while True:
        secs = IntPrompt.ask("Full poll every N seconds (min 60)", default=int(settings.get("poll_interval_seconds", 300)))
        if secs >= 60:
            break
        console.print("[red]Minimum is 60 seconds.[/red]")
    settings["poll_interval_seconds"] = secs
    notes = settings["notifications"]
    for key, label in NOTIFICATION_LABELS.items():
        notes[key] = Confirm.ask(f"Notify when: {label}?", default=bool(notes.get(key, True)))
    if find_binary("terminal-notifier"):
        settings["notifier"] = "terminal-notifier"
    else:
        console.print("[yellow]terminal-notifier isn't installed, so prn will use osascript "
                      "(clicking a notification won't open the PR).[/yellow]")
        settings["notifier"] = "osascript"


# ---------------------------------------------------------------- entrypoint


def run(target: Path) -> Path:
    console.print(f"[bold]PR Review Notifier setup[/bold]: config file [cyan]{target}[/cyan]\n")
    base_text = config_mod.example_config_text()

    if target.exists():
        try:
            existing = config_mod.load(target)
            _summary(existing)
        except ConfigError as exc:
            console.print(f"[yellow]Existing config is invalid:[/yellow]\n{exc}")
            existing = None
        choice = Prompt.ask(
            "A config already exists. [k]eep it, [r]econfigure it, or start [f]resh from the example?",
            choices=["k", "r", "f"], default="k" if existing else "r",
        )
        if choice == "k":
            if existing is None:
                raise ConfigError("Can't keep an invalid config. Re-run and choose r or f.")
            paths.set_config_pointer(target)
            console.print("[green]✓[/green] Keeping your existing config.")
            return target
        if choice == "r" and existing is not None:
            base_text = target.read_text(encoding="utf-8")

    ed = ConfigEditor(path=target, text=base_text)
    client = _client()
    me = None
    if client:
        try:
            me = client.viewer_login()
            console.print(f"Signed in to GitHub as [bold]@{me}[/bold]\n")
        except GitHubError as exc:
            console.print(f"[yellow]⚠ GitHub auth problem: {exc}[/yellow]")
            client = None

    try:
        repos = _ask_repos(list(ed.doc["defaults"]["repos"]), client)
        ConfigEditor.replace_seq(ed.doc["defaults"]["repos"], repos)

        groups = ed.doc.setdefault("groups", {})
        teammates = _ask_teammates(list(groups.get("teammates") or []), repos, client, me)
        if "teammates" in groups and hasattr(groups["teammates"], "ca"):
            ConfigEditor.replace_seq(groups["teammates"], teammates)
        else:
            groups["teammates"] = teammates

        _ask_sections(ed)
        _ask_polling_and_notifications(ed)
    finally:
        if client:
            client.close()

    if target.exists():
        console.print(f"[dim]Backed up previous config to {_backup(target)}[/dim]")
    cfg = ed.save()
    paths.set_config_pointer(target)
    console.rule()
    console.print(f"[green]✓[/green] Wrote {target}")
    _summary(cfg)
    console.print("[dim]You can edit it anytime with `prn config edit`.[/dim]")
    return target
