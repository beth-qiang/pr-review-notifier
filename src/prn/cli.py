"""Command-line entrypoint for PR Review Notifier (`prn`, also installed as `pr-review-notifier`)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config as config_mod
from . import fmt, launchd, logs, macapp, notify, paths, poller, store
from .github import AuthError, GitHubClient, GitHubError, find_binary, get_token

app = typer.Typer(
    help="PR Review Notifier (prn): a local GitHub PR inbox with a menu bar app and desktop notifications. "
         "Run with no command to open the interactive inbox. `pr-review-notifier` is the same command.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
)
sections_app = typer.Typer(help="List, add, remove, and reorder inbox sections.", no_args_is_help=True)
config_app = typer.Typer(help="Create, edit, and validate the config file.", no_args_is_help=True)
daemon_app = typer.Typer(help="Run the menu bar app at login via launchd.", no_args_is_help=True)
app_bundle_app = typer.Typer(help="Build the macOS app bundle so notifications and Login Items say "
                                  "\"PR Review Notifier\" instead of Python.", no_args_is_help=True)
app.add_typer(sections_app, name="sections")
app.add_typer(config_app, name="config")
app.add_typer(daemon_app, name="daemon")
app.add_typer(app_bundle_app, name="app")

console = Console()
err = Console(stderr=True)


def _fail(msg: str, code: int = 1) -> None:
    err.print(f"[red]error:[/red] {msg}")
    raise typer.Exit(code)


def _load_cfg() -> config_mod.Config:
    try:
        return config_mod.load()
    except config_mod.ConfigError as exc:
        _fail(str(exc))
        raise  # unreachable


@app.callback()
def main(ctx: typer.Context, verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging to stderr")):
    logs.setup(verbose=verbose)
    if ctx.invoked_subcommand is None:
        if not paths.config_path().exists():
            _fail("no config yet. Run ./setup.sh (or `prn setup`) first.")
        from . import tui
        tui.run()


# ---------------------------------------------------------------- inbox commands


def _do_refresh(quiet: bool = False) -> store.Inbox:
    try:
        res = poller.poll_once(lock_wait_seconds=60)
    except (config_mod.ConfigError, GitHubError, store.LockBusy) as exc:
        _fail(str(exc))
        raise
    if not quiet:
        extra = " (first run: seeded state, no notifications)" if res.seeded else ""
        console.print(f"[green]✓[/green] refreshed in {res.duration_s:.1f}s: {res.inbox.total} PRs, "
                      f"{res.inbox.badge_count} need attention, {len(res.notifications)} notifications{extra}")
    return res.inbox


@app.command()
def refresh():
    """Poll GitHub now (sends notifications for anything new)."""
    _do_refresh()


@app.command("list")
def list_cmd(
    section: Optional[str] = typer.Option(None, "--section", "-s", help="Only this section (case-insensitive)"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    refresh_first: bool = typer.Option(False, "--refresh", "-r", help="Poll before listing"),
    show_empty: bool = typer.Option(False, "--all", "-a", help="Include empty sections"),
):
    """Print the inbox grouped by section."""
    inbox = _do_refresh(quiet=True) if refresh_first else store.read_inbox()
    if inbox is None:
        _fail("no inbox data yet. Run `prn refresh` (or start the menu bar app).")
    secs = inbox.sections
    if section:
        secs = [s for s in secs if s.name.lower() == section.lower()]
        if not secs:
            _fail(f"no section named '{section}'")

    if as_json:
        out = {
            "me": inbox.me,
            "generated_at": inbox.generated_at,
            "badge_count": inbox.badge_count,
            "error": inbox.error,
            "sections": [{"name": s.name, "count": len(s.prs),
                          "prs": [{"repo": p.repo, "number": p.number, "title": p.title, "url": p.url,
                                   "author": p.author, "draft": p.is_draft, "review_decision": p.review_decision,
                                   "ci": p.ci, "approvals": p.approvals(), "updated_at": p.updated_at}
                                  for p in s.prs]} for s in secs],
        }
        typer.echo(json.dumps(out, indent=2))
        return

    if inbox.error:
        err.print(f"[yellow]⚠ last poll failed:[/yellow] {inbox.error}")
    console.print(f"[bold]@{inbox.me}[/bold] · updated {fmt.ago(inbox.generated_at) if inbox.generated_at else '?'}"
                  f" · {inbox.badge_count} need attention")
    for s in secs:
        if not s.prs and not show_empty and not section:
            continue
        t = Table(title=f"{s.name} ({len(s.prs)})", title_justify="left", title_style="bold",
                  show_header=False, box=None, pad_edge=False, expand=False)
        t.add_column(width=1)
        t.add_column(style="cyan", no_wrap=True)
        t.add_column(overflow="ellipsis", max_width=70, no_wrap=True)
        t.add_column(style="dim", no_wrap=True)
        t.add_column(style="dim", no_wrap=True)
        for p in s.prs:
            t.add_row(fmt.CI_GLYPH.get(p.ci, " "), fmt.pr_label(p),
                      ("[dim]" + p.title + "[/dim]") if p.is_draft else p.title, f"@{p.author}", fmt.ago(p.updated_at))
        if not s.prs:
            t.add_row("", "[dim](empty)[/dim]", "", "", "")
        console.print()
        console.print(t)


@app.command()
def menubar():
    """Run the menu bar app in the foreground (what the launchd agent runs)."""
    logs.setup(to_stderr=True)
    from . import menubar as mb
    try:
        mb.run()
    except RuntimeError as exc:
        _fail(str(exc))


@app.command()
def doctor():
    """Check gh auth, token, notifier, config, and daemon."""
    ok = True
    console.print(f"[bold]{macapp.APP_NAME}[/bold] (prn) health check\n")

    def line(good: bool, label: str, detail: str = ""):
        nonlocal ok
        ok = ok and good
        mark = "[green]✓[/green]" if good else "[red]✗[/red]"
        console.print(f"{mark} {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))

    gh = find_binary("gh")
    line(bool(gh), "GitHub CLI installed", gh or "brew install gh")
    try:
        token = get_token()
        client = GitHubClient(token=token)
        login = client.viewer_login()
        line(True, "GitHub auth", f"logged in as @{login}")
        client.close()
    except (AuthError, GitHubError) as exc:
        line(False, "GitHub auth", str(exc))

    tn = find_binary("terminal-notifier")
    line(bool(tn), "terminal-notifier installed", tn or "required: brew install terminal-notifier")

    try:
        cfg = config_mod.load()
        line(True, "Config valid", f"{paths.config_path()} · {len(cfg.sections)} sections")
    except config_mod.ConfigError as exc:
        line(False, "Config valid", str(exc).replace("\n", " "))

    if sys.platform == "darwin":
        try:
            import rumps  # noqa: F401
            line(True, "rumps available")
        except ImportError:
            line(False, "rumps available", "reinstall prn on macOS")
        line(macapp.is_installed(), f"{macapp.APP_NAME}.app built",
             str(macapp.app_path()) if macapp.is_installed() else "run `prn app install` (or ./setup.sh)")
        st = launchd.status()
        line(st["installed"] and st["running"], "Menu bar daemon",
             ("running pid " + str(st["pid"])) if st["running"] else
             ("installed but not running" if st["installed"] else "not installed: `prn daemon install`"))
    else:
        line(False, "macOS", "menu bar + notifications only work on macOS")

    inbox = store.read_inbox()
    if inbox and inbox.generated_at:
        line(not inbox.error, "Last poll", f"{fmt.ago(inbox.generated_at)}" + (f" · error: {inbox.error}" if inbox.error else ""))
    raise typer.Exit(0 if ok else 1)


# ---------------------------------------------------------------- sections


@sections_app.command("list")
def sections_list():
    """Show sections in evaluation order (first match wins)."""
    cfg = _load_cfg()
    t = Table(show_lines=False)
    t.add_column("#", justify="right")
    t.add_column("Section", style="bold")
    t.add_column("Notify")
    t.add_column("Badge")
    t.add_column("Filters")
    for i, s in enumerate(cfg.sections, 1):
        filters = s.filters.model_dump(exclude_defaults=True, by_alias=True)
        t.add_row(str(i), s.name, "✓" if s.notify else "", "✓" if s.badge else "",
                  ", ".join(f"{k}={v}" for k, v in filters.items()) or "[dim](none)[/dim]")
    console.print(t)


def _csv(v: Optional[str]) -> Optional[list[str]]:
    if v is None:
        return None
    items = [x.strip() for x in v.split(",") if x.strip()]
    return items or None


@sections_app.command("add")
def sections_add(
    name: str = typer.Argument(..., help="Section name"),
    authors: Optional[str] = typer.Option(None, help="Comma list: usernames, @me, group:<name>"),
    exclude_authors: Optional[str] = typer.Option(None, help="Comma list"),
    repos: Optional[str] = typer.Option(None, help="Comma list of owner/name (default: defaults.repos)"),
    exclude_repos: Optional[str] = typer.Option(None, help="Comma list"),
    review_requested: Optional[str] = typer.Option(None, help="me | team | any | none"),
    my_review: Optional[str] = typer.Option(None, help="none|commented|changes_requested|approved|dismissed, or not:<state>"),
    review_decision: Optional[str] = typer.Option(None, help="approved | changes_requested | review_required | none"),
    min_approvals: Optional[int] = typer.Option(None),
    max_approvals: Optional[int] = typer.Option(None),
    has_reviews_from_others: Optional[bool] = typer.Option(None, "--has-reviews-from-others/--no-reviews-from-others"),
    has_reviews: Optional[bool] = typer.Option(None, "--has-reviews/--no-reviews", help="Anyone but the author reviewed"),
    awaiting_my_review: Optional[bool] = typer.Option(
        None, "--awaiting-my-review/--not-awaiting-my-review",
        help="Not reviewed by me yet, or my review was re-requested directly"),
    draft: Optional[bool] = typer.Option(None, "--draft/--not-draft"),
    labels: Optional[str] = typer.Option(None, help="Comma list (PR has any)"),
    exclude_labels: Optional[str] = typer.Option(None, help="Comma list"),
    ci: Optional[str] = typer.Option(None, help="success | failure | pending | none"),
    updated_within_days: Optional[int] = typer.Option(None),
    notify: bool = typer.Option(True, "--notify/--no-notify"),
    badge: bool = typer.Option(False, "--badge/--no-badge"),
    position: Optional[int] = typer.Option(None, help="1-based position (default: last)"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Prompt for each filter"),
):
    """Add a section. Pass filters as flags, or use -i to be prompted."""
    if interactive:
        p = lambda label, default="": typer.prompt(label, default=default, show_default=bool(default)) or None  # noqa: E731
        authors = p("Authors (comma list, @me, group:<name>; blank = any)")
        exclude_authors = p("Exclude authors (blank = none)")
        repos = p("Repos (blank = defaults)")
        review_requested = p("Review requested from (me/team/any/none; blank = don't care)")
        my_review = p("My review (none/commented/changes_requested/approved, not:<state>; blank = don't care)")
        review_decision = p("Review decision (approved/changes_requested/review_required/none; blank = don't care)")
        d = p("Draft? (yes/no; blank = don't care)")
        draft = None if d is None else d.lower().startswith("y")
        notify = typer.confirm("Notify when a PR enters this section?", default=True)
        badge = typer.confirm("Count in menu bar badge?", default=False)

    filters: dict = {}
    for key, val in (("authors", _csv(authors)), ("exclude_authors", _csv(exclude_authors)),
                     ("repos", _csv(repos)), ("exclude_repos", _csv(exclude_repos)),
                     ("labels", _csv(labels)), ("exclude_labels", _csv(exclude_labels))):
        if val:
            filters[key] = val
    for key, val in (("review_requested", review_requested), ("review_decision", review_decision),
                     ("ci", ci), ("updated_within_days", updated_within_days),
                     ("has_reviews_from_others", has_reviews_from_others), ("has_reviews", has_reviews),
                     ("awaiting_my_review", awaiting_my_review), ("draft", draft)):
        if val is not None:
            filters[key] = val
    if my_review:
        filters["my_review"] = {"not": my_review[4:]} if my_review.startswith("not:") else my_review
    if min_approvals is not None or max_approvals is not None:
        filters["approvals"] = {k: v for k, v in (("min", min_approvals), ("max", max_approvals)) if v is not None}

    section = {"name": name, "notify": notify, "badge": badge, "filters": filters}
    try:
        ed = config_mod.ConfigEditor()
        ed.add_section(section, position=None if position is None else position - 1)
        ed.save()
    except config_mod.ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] added section '{name}'. Changes apply on the next poll (`prn refresh`).")


@sections_app.command("remove")
def sections_remove(name: str):
    """Remove a section by name."""
    try:
        ed = config_mod.ConfigEditor()
        ed.remove_section(name)
        ed.save()
    except config_mod.ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] removed '{name}'")


@sections_app.command("move")
def sections_move(name: str, to: int = typer.Option(..., "--to", help="New 1-based position")):
    """Reorder a section (order matters: first match wins)."""
    try:
        ed = config_mod.ConfigEditor()
        ed.move_section(name, to - 1)
        ed.save()
    except config_mod.ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] moved '{name}' to position {to}")


# ---------------------------------------------------------------- config


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config"),
    path: Optional[str] = typer.Option(None, "--path", help="Write here and make it the active config"),
):
    """Copy config.example.yaml into place (prefer ./setup.sh for guided setup)."""
    from pathlib import Path
    try:
        p = config_mod.init(force=force, path=Path(path).expanduser() if path else None)
    except config_mod.ConfigError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] wrote {p}")


@config_app.command("path")
def config_path():
    """Print the active config file path."""
    typer.echo(str(paths.config_path()))


@config_app.command("use")
def config_use(path: str):
    """Point prn (CLI + menu bar daemon) at a config file, e.g. <repo>/config.yaml."""
    from pathlib import Path
    p = Path(path).expanduser().resolve()
    if not p.exists():
        _fail(f"{p} does not exist")
    try:
        config_mod.load(p)
    except config_mod.ConfigError as exc:
        _fail(str(exc))
    paths.set_config_pointer(p)
    console.print(f"[green]✓[/green] prn now uses {p}")


@app.command()
def setup(
    config: Optional[str] = typer.Option(None, "--config", help="Config file to create/update (default: active path)"),
):
    """Interactive setup wizard: repos, teammates, sections, polling, notifications."""
    from pathlib import Path

    from . import setup_wizard
    target = Path(config).expanduser().resolve() if config else paths.config_path()
    try:
        setup_wizard.run(target)
    except (KeyboardInterrupt, EOFError):
        err.print("\n[yellow]setup cancelled; nothing written[/yellow]")
        raise typer.Exit(130)
    except config_mod.ConfigError as exc:
        _fail(str(exc))


@config_app.command("validate")
def config_validate():
    """Validate the config file."""
    cfg = _load_cfg()
    console.print(f"[green]✓[/green] valid: {len(cfg.sections)} sections, {len(cfg.defaults.repos)} default repos, "
                  f"{len(cfg.groups)} groups")


@config_app.command("edit")
def config_edit():
    """Open the config in $EDITOR, then validate it."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.call([*editor.split(), str(paths.config_path())])
    config_validate()


# ---------------------------------------------------------------- daemon


@app.command("test-notification")
def test_notification():
    """Send a test desktop notification."""
    settings = config_mod.load().settings if paths.config_path().exists() else config_mod.Settings()
    sent = notify.send_test(settings.notifier)
    via = notify.notifier_binary(settings.notifier) or "osascript"
    console.print(("[green]✓[/green] sent" if sent else "[red]✗[/red] failed to send") + f" [dim](via {via})[/dim]")
    raise typer.Exit(0 if sent else 1)


@app_bundle_app.command("install")
def app_install():
    """Build "PR Review Notifier.app" (menu bar app + branded notifier). Re-run after a Python upgrade."""
    try:
        p = macapp.build()
    except (macapp.AppBuildError, RuntimeError) as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] built {p}")
    if launchd.status()["installed"]:
        launchd.install()  # point the login item at the app
        console.print("[green]✓[/green] menu bar app restarted from the bundle")


@app_bundle_app.command("uninstall")
def app_uninstall():
    """Remove the app bundle. The login item falls back to plain `prn menubar`."""
    removed = macapp.uninstall()
    console.print("[green]✓[/green] removed" if removed else "nothing to remove")
    if launchd.status()["installed"]:
        launchd.install()


@app_bundle_app.command("status")
def app_status():
    """Show whether the app bundle is built."""
    console.print(f"built: {macapp.is_installed()}  path: {macapp.app_path()}  "
                  f"notifier: {macapp.helper_binary() or '-'}")


@daemon_app.command("install")
def daemon_install():
    """Install + start the LaunchAgent (menu bar app at login)."""
    try:
        p = launchd.install()
    except RuntimeError as exc:
        _fail(str(exc))
    console.print(f"[green]✓[/green] installed {p}. The menu bar icon should appear in a few seconds.")


@daemon_app.command("uninstall")
def daemon_uninstall():
    """Stop and remove the LaunchAgent."""
    removed = launchd.uninstall()
    console.print("[green]✓[/green] uninstalled" if removed else "nothing to uninstall")


@daemon_app.command("restart")
def daemon_restart():
    """Restart the menu bar app (e.g. after upgrading prn)."""
    try:
        launchd.restart()
    except RuntimeError as exc:
        _fail(str(exc))
    console.print("[green]✓[/green] restarted")


@daemon_app.command("status")
def daemon_status():
    """Show whether the LaunchAgent is installed and running."""
    st = launchd.status()
    console.print(f"installed: {st['installed']}  running: {st['running']}  pid: {st['pid'] or '-'}")


@daemon_app.command("logs")
def daemon_logs(lines: int = typer.Option(50, "-n"), follow: bool = typer.Option(False, "-f")):
    """Tail the prn log."""
    log = paths.log_path()
    if not log.exists():
        _fail(f"no log yet at {log}")
    cmd = ["tail", f"-n{lines}"] + (["-f"] if follow else []) + [str(log)]
    raise typer.Exit(subprocess.call(cmd))


if __name__ == "__main__":
    app()
