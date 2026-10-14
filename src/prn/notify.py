"""Desktop notifications via terminal-notifier (clickable) with an osascript fallback."""

from __future__ import annotations

import logging
import subprocess
import sys

from .events import Notification
from .github import find_binary

log = logging.getLogger(__name__)

MAX_INDIVIDUAL = 5  # beyond this, send one summary instead of a burst


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, subtitle: str, message: str, url: str | None = None, group: str | None = None,
         notifier: str = "terminal-notifier") -> bool:
    if sys.platform != "darwin":
        log.info("notify (non-macOS, skipped): %s | %s | %s", title, subtitle, message)
        return False

    tn = find_binary("terminal-notifier") if notifier == "terminal-notifier" else None
    if tn:
        cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", message, "-sound", "default"]
        if url:
            cmd += ["-open", url]
        if group:
            cmd += ["-group", group]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True
        log.warning("terminal-notifier failed (%s); falling back to osascript", res.stderr.strip())

    script = (f'display notification "{_osascript_escape(message)}" '
              f'with title "{_osascript_escape(title)}" subtitle "{_osascript_escape(subtitle)}" '
              f'sound name "default"')
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.returncode == 0


def deliver(notifications: list[Notification], notifier: str) -> int:
    if not notifications:
        return 0
    if len(notifications) > MAX_INDIVIDUAL:
        send(
            title=f"{len(notifications)} PR updates",
            subtitle="prn inbox",
            message="; ".join(f"{n.pr.short_repo}#{n.pr.number}" for n in notifications[:8])
            + ("…" if len(notifications) > 8 else ""),
            group="prn-summary",
            notifier=notifier,
        )
        return 1
    for n in notifications:
        send(n.title, n.subtitle, n.message, url=n.pr.url, group=n.pr.key, notifier=notifier)
    return len(notifications)
