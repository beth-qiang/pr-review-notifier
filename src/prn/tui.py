"""Interactive inbox (Textual). Live-reloads whenever the daemon (or `prn refresh`) rewrites inbox.json."""

from __future__ import annotations

import subprocess
import webbrowser
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import fmt, poller, store
from .models import PR

CI_STYLE = {"success": "green", "failure": "red", "pending": "yellow", "none": "dim"}


class PRNTui(App):
    TITLE = "prn"
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #main { height: 1fr; }
    #list { width: 2fr; border: none; }
    #detail { width: 1fr; padding: 0 1; border-left: solid $primary-darken-2; }
    #filter { display: none; }
    #filter.visible { display: block; }
    """
    BINDINGS = [
        Binding("enter", "open", "Open", show=True, priority=False),
        Binding("o", "open", "Open", show=False),
        Binding("y", "copy", "Copy URL"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self._inbox: Optional[store.Inbox] = None
        self._mtime = -1.0
        self._prs: dict[str, PR] = {}
        self._filter = ""
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Input(placeholder="filter by title, repo, author…  (Esc to clear)", id="filter")
        with Horizontal(id="main"):
            yield OptionList(id="list")
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self._reload(force=True)
        self.set_interval(2, self._reload)
        self.set_interval(15, self._update_status)
        if self._inbox is None:
            self.action_refresh()

    # ---------------------------------------------------------------- data

    def _reload(self, force: bool = False) -> None:
        mtime = store.inbox_mtime()
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        self._inbox = store.read_inbox()
        self._render_list()
        self._update_status()

    def _matches_filter(self, pr: PR) -> bool:
        if not self._filter:
            return True
        hay = f"{pr.title} {pr.repo} #{pr.number} {pr.author} {' '.join(pr.labels)}".lower()
        return all(tok in hay for tok in self._filter.lower().split())

    def _render_list(self) -> None:
        ol = self.query_one("#list", OptionList)
        highlighted_id = None
        if ol.highlighted is not None:
            try:
                highlighted_id = ol.get_option_at_index(ol.highlighted).id
            except Exception:
                highlighted_id = None
        ol.clear_options()
        self._prs.clear()
        if self._inbox is None:
            ol.add_option(Option(Text("No data yet. Press r to refresh.", style="dim"), disabled=True))
            return
        first_pr_index = None
        for sec in self._inbox.sections:
            visible = [p for p in sec.prs if self._matches_filter(p)]
            header = Text.assemble((f"▾ {sec.name} ", "bold"), (f"({len(visible)})", "dim"))
            if sec.badge:
                header.append("  ●", style="magenta")
            ol.add_option(Option(header, disabled=True))
            for pr in visible:
                self._prs[pr.key] = pr
                line = Text.assemble(
                    "   ",
                    (fmt.CI_GLYPH.get(pr.ci, " "), CI_STYLE.get(pr.ci, "")),
                    " ",
                    {"approved": ("◆", "green"), "changes_requested": ("◆", "red")}.get(pr.review_decision, " "),
                    " ",
                    (f"{fmt.pr_label(pr):<28}", "cyan"),
                    (fmt.truncate(pr.title, 70), "bold" if not pr.is_draft else "dim"),
                    (f"  @{pr.author}", "dim"),
                    (f"  {fmt.ago(pr.updated_at)}", "dim"),
                )
                ol.add_option(Option(line, id=pr.key))
                if first_pr_index is None:
                    first_pr_index = ol.option_count - 1
            if not visible:
                ol.add_option(Option(Text("     (empty)", style="dim"), disabled=True))
        # restore highlight
        target = None
        if highlighted_id:
            try:
                target = ol.get_option_index(highlighted_id)
            except Exception:
                target = None
        if target is None:
            target = first_pr_index
        if target is not None:
            ol.highlighted = target
        self._show_detail(self._current_pr())

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        inbox = self._inbox
        if self._refreshing:
            status.update(Text("Refreshing…", style="yellow"))
            return
        if inbox is None:
            status.update(Text("No inbox yet. Press r to fetch from GitHub.", style="yellow"))
            return
        parts = [
            (f"@{inbox.me}", "bold"),
            f"  ·  {inbox.badge_count} need attention  ·  {inbox.total} total",
            f"  ·  updated {fmt.ago(inbox.generated_at) if inbox.generated_at else '?'}",
            f"  ·  next poll in {fmt.until(inbox.next_poll_at)}",
        ]
        t = Text.assemble(*parts)
        if inbox.error:
            t.append(f"   ⚠ {fmt.truncate(inbox.error, 80)}", style="red")
        status.update(t)

    def _current_pr(self) -> Optional[PR]:
        ol = self.query_one("#list", OptionList)
        if ol.highlighted is None:
            return None
        try:
            opt = ol.get_option_at_index(ol.highlighted)
        except Exception:
            return None
        return self._prs.get(opt.id) if opt.id else None

    def _show_detail(self, pr: Optional[PR]) -> None:
        detail = self.query_one("#detail", Static)
        if pr is None:
            detail.update("")
            return
        me = self._inbox.me if self._inbox else ""
        t = Text()
        t.append(f"{pr.title}\n\n", style="bold")
        t.append(f"{pr.repo}#{pr.number}\n", style="cyan")
        t.append(f"by @{pr.author}" + ("  (draft)" if pr.is_draft else "") + "\n\n")
        t.append("Review:     ", style="dim"); t.append(fmt.DECISION_TEXT.get(pr.review_decision, pr.review_decision) + "\n")
        t.append("Approvals:  ", style="dim"); t.append(f"{pr.approvals()}\n")
        t.append("CI:         ", style="dim"); t.append(f"{pr.ci}\n", style=CI_STYLE.get(pr.ci, ""))
        if me:
            t.append("You:        ", style="dim"); t.append(f"{pr.my_review_state(me).replace('_', ' ')}\n")
        if pr.latest_reviews:
            t.append("\nReviews\n", style="bold")
            for r in pr.latest_reviews:
                t.append(f"  {r.state.lower().replace('_', ' '):<18} @{r.author}\n")
        if pr.requested_users or pr.requested_teams:
            t.append("\nRequested\n", style="bold")
            for u in pr.requested_users:
                t.append(f"  @{u}\n")
            for team in pr.requested_teams:
                t.append(f"  team {team}\n")
        if pr.labels:
            t.append("\nLabels  ", style="bold"); t.append(", ".join(pr.labels) + "\n")
        t.append(f"\nUpdated {fmt.ago(pr.updated_at)} · {pr.comment_count} comments\n", style="dim")
        t.append(pr.url, style="underline dim")
        detail.update(t)

    # ---------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._show_detail(self._prs.get(event.option.id) if event.option.id else None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        pr = self._prs.get(event.option.id) if event.option.id else None
        if pr:
            webbrowser.open(pr.url)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._filter = event.value
        self._render_list()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.query_one("#list", OptionList).focus()

    # ---------------------------------------------------------------- actions

    def action_open(self) -> None:
        pr = self._current_pr()
        if pr:
            webbrowser.open(pr.url)

    def action_copy(self) -> None:
        pr = self._current_pr()
        if not pr:
            return
        try:
            subprocess.run(["pbcopy"], input=pr.url, text=True, check=True)
            self.notify(f"Copied {pr.url}")
        except Exception:
            self.copy_to_clipboard(pr.url)
            self.notify("Copied URL")

    def action_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.add_class("visible")
        inp.focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.value = ""
        inp.remove_class("visible")
        self._filter = ""
        self._render_list()
        self.query_one("#list", OptionList).focus()

    def action_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self._update_status()
        self._do_refresh()

    @work(thread=True, exclusive=True)
    def _do_refresh(self) -> None:
        err = None
        try:
            poller.poll_once(lock_wait_seconds=60)
        except Exception as exc:
            err = str(exc)
        self.call_from_thread(self._after_refresh, err)

    def _after_refresh(self, err: Optional[str]) -> None:
        self._refreshing = False
        self._reload(force=True)
        if err:
            self.notify(err, severity="error", timeout=8)


def run() -> None:
    PRNTui().run()
