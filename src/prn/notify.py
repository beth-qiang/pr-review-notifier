"""Desktop notifications.

Notifications are sent through the branded notifier inside "PR Review Notifier.app" (a renamed
terminal-notifier built by `prn app install`), so macOS shows "PR Review Notifier" and our icon,
and clicking a notification opens the PR. If the app hasn't been built, plain terminal-notifier
is used, then osascript as a last resort; osascript notifications are labelled "Script Editor"
by macOS, so for those the app name goes in the title instead.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

from .events import Notification
from .models import Mention
from .github import find_binary
from .macapp import APP_NAME, helper_binary

log = logging.getLogger(__name__)

MAX_INDIVIDUAL = 5  # beyond this, send one summary instead of a burst


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notifier_binary(notifier: str = "terminal-notifier") -> Optional[str]:
    """Branded notifier first, then Homebrew's terminal-notifier. None means osascript."""
    if notifier != "terminal-notifier":
        return None
    branded = helper_binary()
    if branded:
        return str(branded)
    return find_binary("terminal-notifier")


def send(title: str, subtitle: str, message: str, url: Optional[str] = None, group: Optional[str] = None,
         notifier: str = "terminal-notifier") -> bool:
    if sys.platform != "darwin":
        log.info("notify (non-macOS, skipped): %s | %s | %s", title, subtitle, message)
        return False

    tn = notifier_binary(notifier)
    if tn:
        cmd = [tn, "-title", title, "-subtitle", subtitle, "-message", message, "-sound", "default"]
        if url:
            cmd += ["-open", url]
        if group:
            cmd += ["-group", group]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True
        log.warning("notifier failed (%s); falling back to osascript", res.stderr.strip())

    script = (f'display notification "{_osascript_escape(message)}" '
              f'with title "{_osascript_escape(f"{APP_NAME}: {title}")}" '
              f'subtitle "{_osascript_escape(subtitle)}" sound name "default"')
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.returncode == 0


def send_test(notifier: str = "terminal-notifier") -> bool:
    return send("Notifications are working", "You're all set",
                "You'll get an alert here when a PR needs you.", group="prn-test", notifier=notifier)


def deliver_mentions(mentions: list[Mention], notifier: str) -> int:
    if not mentions:
        return 0
    if len(mentions) > MAX_INDIVIDUAL:
        send(title=f"{len(mentions)} new mentions", subtitle="Open the menu bar inbox for details",
             message="; ".join(f"{m.short_repo}#{m.number}" for m in mentions[:8]) + ("…" if len(mentions) > 8 else ""),
             group="prn-mentions", notifier=notifier)
        return 1
    for m in mentions:
        send("You were mentioned" if m.reason == "mention" else "Your team was mentioned",
             f"{m.short_repo}#{m.number}", m.title, url=m.url, group=f"mention:{m.id}", notifier=notifier)
    return len(mentions)


def deliver(notifications: list[Notification], notifier: str) -> int:
    if not notifications:
        return 0
    if len(notifications) > MAX_INDIVIDUAL:
        send(
            title=f"{len(notifications)} PR updates",
            subtitle="Open the menu bar inbox for details",
            message="; ".join(f"{n.pr.short_repo}#{n.pr.number}" for n in notifications[:8])
            + ("…" if len(notifications) > 8 else ""),
            group="prn-summary",
            notifier=notifier,
        )
        return 1
    for n in notifications:
        send(n.title, n.subtitle, n.message, url=n.pr.url, group=n.pr.key, notifier=notifier)
    return len(notifications)
