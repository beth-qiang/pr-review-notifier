"""Detect config and code changes so the menu bar app can re-poll or restart itself.

- Config: when config.yaml's mtime changes, validate it. If it's valid, re-poll right away.
  If it's invalid, report the error once per save.
- Code: fingerprint prn's own source files. With an editable install (what setup.sh does),
  that's <repo>/src/prn, so `git pull` or editing a file changes it. A restart only happens
  once the files have been stable for one extra tick (so a half-finished `git pull` isn't
  picked up), and only if the new code imports cleanly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config as config_mod
from . import paths

PKG_DIR = Path(__file__).resolve().parent


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def source_fingerprint(pkg_dir: Path = PKG_DIR) -> tuple:
    """(path, mtime, size) for every source file in the package. Changes when code changes."""
    out = []
    for p in sorted(pkg_dir.rglob("*")):
        if p.suffix in (".py", ".yaml") and "__pycache__" not in p.parts:
            st = p.stat()
            out.append((str(p.relative_to(pkg_dir)), st.st_mtime, st.st_size))
    return tuple(out)


def new_code_imports() -> Optional[str]:
    """Import the updated code in a fresh interpreter. Returns an error message, or None if OK."""
    res = subprocess.run([sys.executable, "-c", "import prn.cli, prn.menubar, prn.poller"],
                         capture_output=True, text=True, timeout=60)
    if res.returncode == 0:
        return None
    lines = [l for l in (res.stderr or res.stdout).strip().splitlines() if l.strip()]
    return lines[-1] if lines else f"exit code {res.returncode}"


@dataclass
class WatchResult:
    repoll: bool = False                 # config changed and is valid
    config_error: Optional[str] = None   # config changed and is invalid (reported once per save)
    restart: bool = False                # code changed, is stable, and imports cleanly
    code_error: Optional[str] = None     # code changed but the new code fails to import (reported once)


class ChangeWatcher:
    def __init__(
        self,
        config_path: Callable[[], Path] = paths.config_path,
        fingerprint: Callable[[], tuple] = source_fingerprint,
        import_check: Callable[[], Optional[str]] = new_code_imports,
    ):
        self._config_path = config_path
        self._fingerprint = fingerprint
        self._import_check = import_check
        self._cfg_mtime = _mtime(config_path())
        self._code_fp = fingerprint()
        self._pending_fp: Optional[tuple] = None
        self._bad_fp: Optional[tuple] = None

    def check(self) -> WatchResult:
        res = WatchResult()

        # ---- config
        path = self._config_path()
        mtime = _mtime(path)
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            try:
                config_mod.load(path)
                res.repoll = True
            except config_mod.ConfigError as exc:
                res.config_error = str(exc)

        # ---- code (debounced: must be unchanged across two consecutive checks)
        fp = self._fingerprint()
        if fp != self._code_fp and fp != self._bad_fp:
            if fp == self._pending_fp:
                err = self._import_check()
                if err is None:
                    res.restart = True
                else:
                    res.code_error = err
                    self._bad_fp = fp  # don't retry or re-notify until the code changes again
                self._pending_fp = None
            else:
                self._pending_fp = fp
        return res


def restart_self() -> None:
    """Replace this process with a fresh `prn menubar` running the new code."""
    os.execv(sys.executable, [sys.executable, "-m", "prn", "menubar"])
