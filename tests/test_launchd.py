"""Parsing `launchctl print` output for the menu bar LaunchAgent."""

import subprocess

from prn import launchd

# trimmed real output: nested endpoint blocks carry their own "state = active"
LAUNCHCTL_PRINT = (
    "gui/501/com.prn.menubar = {\n"
    "\tactive count = 4\n"
    "\tstate = running\n"
    "\tpid = 51350\n"
    "\tlast exit code = (never exited)\n"
    "\tpid-local endpoints = {\n"
    "\t\t\"com.apple.tsm.portname\" = {\n"
    "\t\t\tstate = active\n"
    "\t\t}\n"
    "\t\tstate = active\n"
    "\t}\n"
    "}\n"
)


def test_status_ignores_nested_state_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(launchd, "plist_path", lambda: tmp_path / "x.plist")
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(
        launchd, "_launchctl",
        lambda *a: subprocess.CompletedProcess(a, 0, stdout=LAUNCHCTL_PRINT, stderr=""),
    )
    st = launchd.status()
    assert (st["running"], st["pid"]) == (True, "51350")
