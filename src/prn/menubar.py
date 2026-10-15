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
from . import fmt, menu_style, notify, paths, poller, store, watch
from .launchd import prn_executable
from .macapp import APP_NAME

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
        self._force_full = False
        self.poller = poller.Poller()
        self.wait = 60.0

    def trigger(self) -> None:
        """Run a full poll now (Refresh now, config saved)."""
        self._force_full = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            force, self._force_full = self._force_full, False
            try:
                cfg = config_mod.load()
                _, self.wait = self.poller.tick(cfg, force_full=force)
            except store.LockBusy:
                log.info("poll skipped: another poll in progress")
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("poll failed")
                try:
                    store.write_inbox_error(str(exc))
                except Exception:
                    pass
            self._wake.wait(timeout=self.wait)
            self._wake.clear()


def _open_terminal_with_prn() -> None:
    """Open the interactive inbox in Terminal.

    Uses a .command file opened with `open` rather than AppleScript, so macOS never shows a
    "Python wants to control Terminal" permission prompt.
    """
    try:
        exe = prn_executable()
    except RuntimeError:
        exe = "prn"
    paths.ensure_dirs()
    cmd_file = paths.state_dir() / "Open PR Review Notifier inbox.command"
    cmd_file.write_text(f"#!/bin/bash\nclear\nexec {shlex.quote(exe)}\n")
    cmd_file.chmod(0o755)
    subprocess.run(["open", "-a", "Terminal", str(cmd_file)], capture_output=True)


def _edit_config() -> None:
    subprocess.run(["open", "-t", str(paths.config_path())], capture_output=True)


if rumps is not None:

    class PRNApp(rumps.App):
        def __init__(self):
            icon = _icon_path()
            super().__init__(APP_NAME, title=None if icon else "PR", icon=icon, template=True, quit_button=None)
            self._hide_dock_icon()
            self._last_mtime: tuple[float, float] = (-1.0, -1.0)
            self._poller = PollThread()
            self._watcher = watch.ChangeWatcher()
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

        def _check_changes(self) -> None:
            """Re-poll when config.yaml is saved; restart when the app's code changes."""
            try:
                res = self._watcher.check()
            except Exception:
                log.exception("change watcher failed")
                return
            if res.repoll:
                log.info("config changed: re-polling")
                self._status_item.title = "Config changed, refreshing…"
                self._poller.trigger()
            if res.config_error:
                log.warning("config changed but is invalid: %s", res.config_error)
                store.write_inbox_error(fmt.truncate(" ".join(res.config_error.split()), 150)
                                        + " (see `prn config validate`)")
                notify.send("Config error", "Keeping your last good inbox",
                            fmt.truncate(" ".join(res.config_error.split()), 200),
                            group="prn-config", notifier=self._settings().notifier)
            if res.code_error:
                log.error("updated code failed to import, still running the old code: %s", res.code_error)
                notify.send("Update failed to load", "Still running the previous version",
                            fmt.truncate(res.code_error, 200) + " (if dependencies changed, re-run ./setup.sh)",
                            group="prn-code", notifier=self._settings().notifier)
            if res.restart:
                log.info("code changed: restarting menu bar app")
                watch.restart_self()

        def _maybe_render(self, _timer=None) -> None:
            self._check_changes()
            mtime = (store.inbox_mtime(), store.seen_mtime())
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
            self.menu.add(rumps.MenuItem(APP_NAME))  # header (disabled: no callback)
            self.menu.add(rumps.separator)

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
                seen = store.read_seen() if settings.read_tracking else None
                for sec in inbox.sections:
                    if not sec.prs and settings.hide_empty_sections:
                        continue
                    if shown:
                        self.menu.add(rumps.separator)
                    shown += 1
                    self.menu.add(rumps.MenuItem(f"{sec.name} ({len(sec.prs)})"))
                    for pr in sec.prs:
                        read = seen is not None and not store.is_unread(seen, pr, sec.name, inbox.me)
                        segments = menu_style.pr_segments(pr, settings.max_title_length, read=read)
                        item = rumps.MenuItem(menu_style.plain(segments), callback=self._open_pr)
                        item._prn_url = pr.url  # type: ignore[attr-defined]
                        item._prn_seen = (pr, sec.name)  # type: ignore[attr-defined]
                        self.menu.add(item)
                        menu_style.apply(item, segments)  # native colors; plain text if unavailable
                if not shown:
                    self.menu.add(rumps.MenuItem("Inbox zero 🎉"))
                else:
                    self.menu.add(rumps.separator)
                    self.menu.add(rumps.MenuItem(menu_style.LEGEND))

            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Refresh now", callback=self._refresh, key="r"))
            self.menu.add(rumps.MenuItem("Open inbox (Terminal)", callback=lambda _: _open_terminal_with_prn()))
            self.menu.add(rumps.MenuItem("Edit config…", callback=lambda _: _edit_config()))
            self._status_item = rumps.MenuItem("")
            self.menu.add(self._status_item)
            self._inbox = inbox
            self._refresh_status_line()
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem(f"Quit {APP_NAME}", callback=lambda _: rumps.quit_application(), key="q"))

        def _refresh_status_line(self) -> None:
            inbox = getattr(self, "_inbox", None)
            if inbox is None or not inbox.generated_at:
                self._status_item.title = "Not updated yet"
                return
            updated = datetime.fromtimestamp(inbox.generated_at).strftime("%-I:%M %p")
            checked = self._poller.poller.last_check_at
            if inbox.strategy == "aggressive" and checked:
                self._status_item.title = f"Updated {updated} · live, checked {int(time.time() - checked)}s ago"
            else:
                self._status_item.title = f"Last updated {updated} · next in {fmt.until(inbox.next_poll_at)}"

        # ------------------------------------------------------------ actions

        def _open_pr(self, sender) -> None:
            url = getattr(sender, "_prn_url", None)
            if url:
                webbrowser.open(url)
            inbox = getattr(self, "_inbox", None)
            seen = getattr(sender, "_prn_seen", None)
            if inbox and seen and self._settings().read_tracking:
                try:
                    store.mark_read([seen], inbox.me, live_keys={p.key for s in inbox.sections for p in s.prs})
                except Exception:
                    log.exception("couldn't mark %s read", seen[0].key)

        def _refresh(self, _sender) -> None:
            self._status_item.title = "Refreshing…"
            self._poller.trigger()


def run() -> None:
    if rumps is None:
        raise RuntimeError("The menu bar app requires macOS (rumps is not installed).")
    PRNApp().start()
