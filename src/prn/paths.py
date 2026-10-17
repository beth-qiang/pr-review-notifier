"""Filesystem locations.

Config file resolution (first match wins):
  1. $PRN_CONFIG                       explicit file path
  2. $PRN_CONFIG_DIR/config.yaml       (used by tests)
  3. path stored in ~/.config/prn/config-path   (written by `prn setup` / `prn config use`;
                                                 normally <repo>/config.yaml)
  4. ~/.config/prn/config.yaml
State lives in ~/.local/state/prn (override with $PRN_STATE_DIR).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

POINTER_NAME = "config-path"


def config_dir() -> Path:
    return Path(os.environ.get("PRN_CONFIG_DIR", Path.home() / ".config" / "prn"))


def state_dir() -> Path:
    return Path(os.environ.get("PRN_STATE_DIR", Path.home() / ".local" / "state" / "prn"))


def pointer_path() -> Path:
    return config_dir() / POINTER_NAME


def _pointer_target() -> Optional[Path]:
    if "PRN_CONFIG_DIR" in os.environ:
        return None
    try:
        text = pointer_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return Path(text).expanduser() if text else None


def config_path() -> Path:
    if os.environ.get("PRN_CONFIG"):
        return Path(os.environ["PRN_CONFIG"]).expanduser()
    if "PRN_CONFIG_DIR" in os.environ:
        return config_dir() / "config.yaml"
    return _pointer_target() or config_dir() / "config.yaml"


def set_config_pointer(path: Path) -> Path:
    """Remember where the config lives so the installed CLI and launchd daemon find it."""
    config_dir().mkdir(parents=True, exist_ok=True)
    p = pointer_path()
    p.write_text(str(path.expanduser().resolve()) + "\n", encoding="utf-8")
    return p


def inbox_path() -> Path:
    """Rendered inbox (sections -> PRs). Read by the TUI and menu bar."""
    return state_dir() / "inbox.json"


def snapshot_path() -> Path:
    """Last-seen per-PR state used to diff for notification events."""
    return state_dir() / "snapshot.json"


def seen_path() -> Path:
    """What each PR looked like when I last opened it. Rows with nothing new since then are faded."""
    return state_dir() / "seen.json"


def mentions_path() -> Path:
    """Unread @mentions on pull requests, from GitHub's notifications feed."""
    return state_dir() / "mentions.json"


def lock_path() -> Path:
    return state_dir() / "poll.lock"


def log_path() -> Path:
    return state_dir() / "prn.log"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)
