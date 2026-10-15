"""Persisted state: the rendered inbox (read by TUI/menu bar) and the per-PR snapshot (for diffing)."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from . import paths
from .engine import SectionResult
from .models import PR, has_new_activity


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- inbox


@dataclass
class InboxSection:
    name: str
    notify: bool
    badge: bool
    prs: list[PR] = field(default_factory=list)


@dataclass
class Inbox:
    me: str = ""
    generated_at: Optional[float] = None
    next_poll_at: Optional[float] = None
    error: Optional[str] = None
    sections: list[InboxSection] = field(default_factory=list)

    @property
    def badge_count(self) -> int:
        return sum(len(s.prs) for s in self.sections if s.badge)

    @property
    def total(self) -> int:
        return sum(len(s.prs) for s in self.sections)


def write_inbox(results: list[SectionResult], me: str, next_poll_at: Optional[float], error: Optional[str] = None) -> Inbox:
    inbox = Inbox(
        me=me,
        generated_at=time.time(),
        next_poll_at=next_poll_at,
        error=error,
        sections=[InboxSection(r.name, r.notify, r.badge, r.prs) for r in results if r.display],
    )
    _atomic_write_json(paths.inbox_path(), {
        "me": inbox.me,
        "generated_at": inbox.generated_at,
        "next_poll_at": inbox.next_poll_at,
        "error": inbox.error,
        "badge_count": inbox.badge_count,
        "sections": [
            {"name": s.name, "notify": s.notify, "badge": s.badge, "prs": [p.to_dict() for p in s.prs]}
            for s in inbox.sections
        ],
    })
    return inbox


def write_inbox_error(error: str) -> None:
    """Keep last good sections, but record the error so UIs can surface it."""
    data = _read_json(paths.inbox_path()) or {"sections": []}
    data["error"] = error
    data["error_at"] = time.time()
    _atomic_write_json(paths.inbox_path(), data)


def read_inbox() -> Optional[Inbox]:
    data = _read_json(paths.inbox_path())
    if data is None:
        return None
    return Inbox(
        me=data.get("me", ""),
        generated_at=data.get("generated_at"),
        next_poll_at=data.get("next_poll_at"),
        error=data.get("error"),
        sections=[
            InboxSection(s["name"], s.get("notify", True), s.get("badge", False),
                         [PR.from_dict(p) for p in s.get("prs", [])])
            for s in data.get("sections", [])
        ],
    )


def inbox_mtime() -> float:
    try:
        return paths.inbox_path().stat().st_mtime
    except FileNotFoundError:
        return 0.0


# ---------------------------------------------------------------- snapshot


def read_snapshot() -> Optional[dict[str, dict]]:
    data = _read_json(paths.snapshot_path())
    return None if data is None else data.get("prs", {})


def write_snapshot(prs: dict[str, dict]) -> None:
    _atomic_write_json(paths.snapshot_path(), {"written_at": time.time(), "prs": prs})


# ---------------------------------------------------------------- read state

SEEN_TTL_SECONDS = 30 * 24 * 3600


def seen_mtime() -> float:
    try:
        return paths.seen_path().stat().st_mtime
    except FileNotFoundError:
        return 0.0


def read_seen() -> dict[str, dict]:
    data = _read_json(paths.seen_path())
    return (data or {}).get("prs", {})


def _seen_entry(pr: PR, section: str, me: str) -> dict:
    return {**pr.activity(me), "section": section, "at": time.time()}


def is_unread(seen: dict[str, dict], pr: PR, section: str, me: str) -> bool:
    entry = seen.get(pr.key)
    return entry is None or has_new_activity(entry, {**pr.activity(me), "section": section})


def mark_read(items: list[tuple[PR, str]], me: str, live_keys: Optional[set[str]] = None) -> None:
    """Record each (PR, section) as I see it now. Entries older than 30 days are dropped
    unless the PR is still in the inbox (`live_keys`)."""
    seen = read_seen()
    for pr, section in items:
        seen[pr.key] = _seen_entry(pr, section, me)
    cutoff = time.time() - SEEN_TTL_SECONDS
    live = live_keys or set()
    seen = {k: v for k, v in seen.items() if k in live or v.get("at", 0) >= cutoff}
    _atomic_write_json(paths.seen_path(), {"prs": seen})


def mark_unread(keys: list[str]) -> None:
    seen = read_seen()
    for k in keys:
        seen.pop(k, None)
    _atomic_write_json(paths.seen_path(), {"prs": seen})


# ---------------------------------------------------------------- lock


class LockBusy(Exception):
    pass


@contextmanager
def poll_lock(wait_seconds: float = 0) -> Iterator[None]:
    """Cross-process lock so the daemon and `prn refresh` never poll simultaneously."""
    paths.ensure_dirs()
    fh = open(paths.lock_path(), "w")
    deadline = time.time() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    raise LockBusy("another poll is already running")
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
