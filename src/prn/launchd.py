"""Install the menu bar app as a per-user LaunchAgent so it starts at login."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "com.prn.menubar"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def prn_executable() -> str:
    if os.environ.get("PRN_EXECUTABLE"):  # set by the app bundle's launch.sh
        return os.environ["PRN_EXECUTABLE"]
    exe = shutil.which("prn") or shutil.which("prn", path=str(Path.home() / ".local" / "bin"))
    if exe:
        return exe
    argv0 = Path(sys.argv[0]).resolve()
    if argv0.name == "prn" and argv0.exists():
        return str(argv0)
    raise RuntimeError("Can't find the `prn` executable on PATH. Install with `uv tool install .` first.")


def build_plist(exe: str) -> dict:
    from . import macapp

    log = str(paths.state_dir() / "menubar.out.log")
    if macapp.is_installed():
        # Run from inside "PR Review Notifier.app" so macOS shows our name instead of "Python",
        # and tie the login item to the app so Login Items shows its name and icon too.
        program = [str(macapp.launch_script())]
        extra = {"AssociatedBundleIdentifiers": [macapp.APP_ID]}
    else:
        program, extra = [exe, "menubar"], {}
    return {
        "Label": LABEL,
        **extra,
        "ProgramArguments": program,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # restart on crash, not on Quit
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("launchd install is macOS only")
    paths.ensure_dirs()
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    with p.open("wb") as fh:
        plistlib.dump(build_plist(prn_executable()), fh)
    res = _launchctl("bootstrap", _domain(), str(p))
    if res.returncode != 0:
        # Older macOS fallback
        res = _launchctl("load", "-w", str(p))
        if res.returncode != 0:
            raise RuntimeError(f"launchctl failed: {res.stderr.strip()}")
    return p


def uninstall() -> bool:
    p = plist_path()
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if p.exists():
        p.unlink()
        return True
    return False


def restart() -> None:
    res = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if res.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {res.stderr.strip()}")


def status() -> dict:
    info = {"installed": plist_path().exists(), "running": False, "pid": None}
    if sys.platform != "darwin":
        return info
    res = _launchctl("print", f"{_domain()}/{LABEL}")
    if res.returncode == 0:
        for raw in res.stdout.splitlines():
            # nested blocks (endpoints, sockets) have their own "state = active" lines;
            # only the service's top-level fields, indented one tab, describe the process
            if not raw.startswith("\t") or raw.startswith("\t\t"):
                continue
            line = raw.strip()
            if line.startswith("state ="):
                info["running"] = line.endswith("running")
            elif line.startswith("pid ="):
                info["pid"] = line.split("=", 1)[1].strip()
    return info
