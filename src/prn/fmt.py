"""Shared formatting helpers for CLI, TUI, and menu bar."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from .models import PR

CI_GLYPH = {"success": "✓", "failure": "✗", "pending": "●", "none": " "}
DECISION_TEXT = {
    "approved": "approved",
    "changes_requested": "changes requested",
    "review_required": "review required",
    "none": "no review rule",
}


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(n - 1, 1)] + "…"


def ago(ts_iso_or_epoch, now: Optional[float] = None) -> str:
    now = now or time.time()
    if isinstance(ts_iso_or_epoch, str):
        ts = datetime.fromisoformat(ts_iso_or_epoch.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    else:
        ts = float(ts_iso_or_epoch)
    secs = max(0, int(now - ts))
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def until(epoch: Optional[float], now: Optional[float] = None) -> str:
    if not epoch:
        return "?"
    secs = int(epoch - (now or time.time()))
    if secs <= 0:
        return "now"
    return f"{secs // 60}m" if secs >= 60 else f"{secs}s"


def pr_label(pr: PR) -> str:
    return f"{pr.short_repo}#{pr.number}"
