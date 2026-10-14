"""macOS menu bar app (rumps). Owns the polling loop; re-renders whenever inbox.json changes."""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from importlib import resources
from typing import Optional

from . import config as config_mod
from . import fmt, paths, poller, store
from .launchd import prn_executable

log = logging.getLogger(__name__)

try:
    import rumps  # type: ignore
except ImportError:  # non-macOS
    rumps = None


def _icon_path() -> Optional[str]:
    try:
        return str(resources.files("prn").joinpath("assets/menubar_icon.png"))
    except Exception:
        return None


class PollThread(threading.Thread):
    """Background poller so network calls never block the menu bar UI."""

    def __init__(self):
        super().__init__(daemon=True, name="prn-poller")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.interval = 300

    def trigger(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = config_mod.load()
                self.interval = cfg.settings.poll_interval_seconds
                poller.poll_once(cfg=cfg, lock_wait_seconds=30)
            except store.LockBusy:
                log.info("poll skipped: another poll in progress")
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("poll failed")
                try:
                    store.write_inbox_error(str(exc))
                except Exception:
                    pass
            self._wake.wait(timeout=self.interval)
            self._wake.clear()


def _open_terminal_with_prn() -> None:
    try:
        exe = prn_executable()
    except RuntimeError:
        exe = "prn"
    script = f'tell application "Terminal" to do script "{shlex.quote(exe)}"\ntell application "Terminal" to activate'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _edit_config() -> None:
    subprocess.run(["open", "-t", str(paths.config_path())], capture_output=True)


if rumps is not None:

    class PRNApp(rumps.App):
        def __init__(self):
            icon = _icon_path()
            super().__init__("prn", title=None if icon else "PR", icon=icon, template=True, quit_button=None)
            self._hide_dock_icon()
            self._last_mtime = -1.0
            self._poller = PollThread()
            self._render_timer = rumps.Timer(self._maybe_render, 3)
            self._render(store.read_inbox())

        @staticmethod
        def _hide_dock_icon() -> None:
            try:
                from AppKit import NSApplication  # type: ignore
                NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory: no Dock icon
            except Exception:
                pass

        def start(self) -> None:
            self._poller.start()
            self._render_timer.start()
            self.run()

        # ------------------------------------------------------------ rendering

        def _maybe_render(self, _timer=None) -> None:
            mtime = store.inbox_mtime()
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                self._render(store.read_inbox())
            else:
                self._refresh_status_line()

        def _settings(self):
            try:
                return config_mod.load().settings
            except config_mod.ConfigError:
                return config_mod.Settings()

        def _render(self, inbox: Optional[store.Inbox]) -> None:
            settings = self._settings()
            self.menu.clear()

            if inbox is None:
                self.title = "…"
                self.menu.add(rumps.MenuItem("Waiting for first poll…"))
            else:
                count = inbox.badge_count
                self.title = (" ⚠" if inbox.error else "") + (f" {count}" if count or not self.icon else "")
                if inbox.error:
                    self.menu.add(rumps.MenuItem(f"⚠ {fmt.truncate(inbox.error, 70)}"))
                    self.menu.add(rumps.separator)
                shown = 0
                for sec in inbox.sections:
                    if not sec.prs and settings.hide_empty_sections:
                        continue
                    if shown:
                        self.menu.add(rumps.separator)
                    shown += 1
                    self.menu.add(rumps.MenuItem(f"{sec.name} ({len(sec.prs)})"))
                    for pr in sec.prs:
                        label = (f"   {fmt.CI_GLYPH.get(pr.ci, ' ')} {fmt.pr_label(pr)}  "
                                 f"{fmt.truncate(pr.title, settings.max_title_length)}  · {pr.author}")
                        item = rumps.MenuItem(label, callback=self._open_pr)
                        item._prn_url = pr.url  # type: ignore[attr-defined]
                        self.menu.add(item)
                if not shown:
                    self.menu.add(rumps.MenuItem("Inbox zero 🎉"))

            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Refresh now", callback=self._refresh, key="r"))
            self.menu.add(rumps.MenuItem("Open inbox (Terminal)", callback=lambda _: _open_terminal_with_prn()))
            self.menu.add(rumps.MenuItem("Edit config…", callback=lambda _: _edit_config()))
            self._status_item = rumps.MenuItem("")
            self.menu.add(self._status_item)
            self._inbox = inbox
            self._refresh_status_line()
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Quit prn", callback=lambda _: rumps.quit_application(), key="q"))

        def _refresh_status_line(self) -> None:
            inbox = getattr(self, "_inbox", None)
            if inbox is None or not inbox.generated_at:
                self._status_item.title = "Not updated yet"
                return
            updated = datetime.fromtimestamp(inbox.generated_at).strftime("%-I:%M %p")
            self._status_item.title = f"Last updated {updated} · next in {fmt.until(inbox.next_poll_at)}"

        # ------------------------------------------------------------ actions

        def _open_pr(self, sender) -> None:
            url = getattr(sender, "_prn_url", None)
            if url:
                webbrowser.open(url)

        def _refresh(self, _sender) -> None:
            self._status_item.title = "Refreshing…"
            self._poller.trigger()


def run() -> None:
    if rumps is None:
        raise RuntimeError("The menu bar app requires macOS (rumps is not installed).")
    PRNApp().start()
