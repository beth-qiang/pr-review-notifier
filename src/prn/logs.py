"""Logging setup: rotating file log in the state dir, optional stderr."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import paths


def setup(verbose: bool = False, to_stderr: bool = False) -> None:
    paths.ensure_dirs()
    root = logging.getLogger()
    if getattr(root, "_prn_configured", False):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(paths.log_path(), maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if to_stderr or verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._prn_configured = True  # type: ignore[attr-defined]
