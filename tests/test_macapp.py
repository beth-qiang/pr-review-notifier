"""The app bundle build, using fakes for macOS tools (otool, codesign, sips, ...)."""

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from prn import launchd, macapp, notify


class FakeMacTools:
    """Stands in for subprocess.run and records every command."""

    def __init__(self, fail=None):
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    def __call__(self, cmd, capture_output=True, text=True, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        tool = Path(cmd[0]).name
        rc, out = (1 if tool in self.fail else 0), ""
        if tool == "otool" and cmd[1] == "-L":
            out = (f"{cmd[2]}:\n\t@executable_path/../lib/libpython3.12.dylib (compatibility version 3.12.0)\n"
                   "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n")
        elif tool == "otool" and cmd[1] == "-l":
            out = ("Load command 12\n          cmd LC_RPATH\n      cmdsize 32\n"
                   "         path @executable_path/../lib (offset 12)\n")
        elif tool == "sips" and rc == 0:
            Path(cmd[-1]).write_bytes(b"png")
        elif tool == "iconutil" and rc == 0:
            Path(cmd[-1]).write_bytes(b"icns")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="boom" if rc else "")

    def tools(self):
        return [Path(c[0]).name for c in self.calls]


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setenv("PRN_APP_DIR", str(tmp_path / "AppSupport"))
    # Homebrew-style terminal-notifier.app
    tn = tmp_path / "Cellar" / "terminal-notifier.app" / "Contents"
    (tn / "MacOS").mkdir(parents=True)
    (tn / "Resources").mkdir()
    (tn / "_CodeSignature").mkdir()
    exe = tn / "MacOS" / "terminal-notifier"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tn / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "terminal-notifier", "CFBundleIdentifier": "fr.julienxx.oss.terminal-notifier",
        "CFBundleName": "terminal-notifier"}))
    fake_which = lambda name: str(exe) if name == "terminal-notifier" else None  # noqa: E731
    monkeypatch.setattr("prn.github.find_binary", fake_which)
    monkeypatch.setattr("prn.notify.find_binary", fake_which)
    # uv tool venv + base interpreter
    venv = tmp_path / "venv"
    (venv / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /base/bin\nversion_info = 3.12.4\n")
    base = tmp_path / "base" / "bin" / "python3.12"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"\xcf\xfa\xed\xfe fake mach-o")
    tools = FakeMacTools()
    builder = macapp.AppBuilder(run=tools, python_exe=str(base), venv_prefix=str(venv), prn_exe="/u/.local/bin/prn")
    return builder, tools, tmp_path


def test_build_creates_branded_bundle(mac):
    builder, tools, tmp = mac
    app = builder.build()
    assert app == macapp.app_path() and app.name == "PR Review Notifier.app"

    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "PR Review Notifier"
    assert info["CFBundleIdentifier"] == "com.prn.app" and info["LSUIElement"] is True
    assert info["CFBundleExecutable"] == "PR Review Notifier" and info["CFBundleIconFile"] == "AppIcon"
    assert (app / "Contents" / "Resources" / "AppIcon.icns").exists()

    # bundled interpreter that uses the uv tool venv
    assert os.access(macapp.app_executable(), os.X_OK)
    assert (app / "Contents" / "pyvenv.cfg").read_text().startswith("home = /base/bin")
    assert os.path.realpath(app / "Contents" / "lib") == str((tmp / "venv" / "lib").resolve())

    # helper notifier: renamed, re-identified, icon, re-signed
    helper = macapp.helper_path()
    hinfo = plistlib.loads((helper / "Contents" / "Info.plist").read_bytes())
    assert hinfo["CFBundleName"] == hinfo["CFBundleDisplayName"] == "PR Review Notifier"
    assert hinfo["CFBundleIdentifier"] == "com.prn.notifier" and hinfo["CFBundleIconFile"] == "AppIcon"
    assert not (helper / "Contents" / "_CodeSignature").exists()
    assert macapp.helper_binary() == helper / "Contents" / "MacOS" / "terminal-notifier"
    assert macapp.is_installed()

    # launch script: bundled interpreter first, plain prn as fallback
    sh = macapp.launch_script().read_text()
    assert 'exec "$APP_EXE" -m prn menubar' in sh and 'exec "/u/.local/bin/prn" menubar' in sh
    assert 'export PRN_EXECUTABLE="/u/.local/bin/prn"' in sh
    assert os.access(macapp.launch_script(), os.X_OK)


def test_build_relinks_and_signs_interpreter(mac):
    builder, tools, tmp = mac
    builder.build()
    lib = str(tmp / "base" / "lib" / "libpython3.12.dylib")
    change = [c for c in tools.calls if c[:2] == ["install_name_tool", "-change"]]
    assert change and change[0][2:4] == ["@executable_path/../lib/libpython3.12.dylib", lib]
    rpath = [c for c in tools.calls if c[:2] == ["install_name_tool", "-rpath"]]
    assert rpath and rpath[0][2:4] == ["@executable_path/../lib", str(tmp / "base" / "lib")]
    signs = [c for c in tools.calls if c[0] == "codesign"]
    assert any(c[-1].endswith("python-copy") for c in signs)             # interpreter signed on its own
    assert any("--deep" in c and c[-1].endswith("Helpers/PR Review Notifier.app") for c in signs)
    assert sum(1 for c in tools.calls if Path(c[0]).name == "lsregister" and c[1] == "-f") == 2
    # last step: the bundled interpreter must be able to import prn
    assert tools.calls[-1][0] == str(macapp.app_executable())


def test_icon_failure_is_not_fatal(mac):
    builder, tools, _ = mac
    tools.fail = {"iconutil"}
    app = builder.build()
    assert "CFBundleIconFile" not in plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def test_build_errors_are_clear(mac, tmp_path, monkeypatch):
    builder, tools, tmp = mac
    tools.fail = {"codesign"}
    with pytest.raises(macapp.AppBuildError, match="codesign"):
        builder.build()
    assert not macapp.app_path().exists()          # nothing half-built left behind

    monkeypatch.setattr("prn.github.find_binary", lambda name: None)
    tools.fail = set()
    with pytest.raises(macapp.AppBuildError, match="brew install terminal-notifier"):
        builder.build()

    not_venv = macapp.AppBuilder(run=tools, python_exe=builder.python_exe, venv_prefix=str(tmp_path / "nope"),
                                 prn_exe="prn")
    with pytest.raises(macapp.AppBuildError, match="virtual environment"):
        not_venv.build()


def test_rebuild_replaces_and_uninstall_removes(mac):
    builder, tools, _ = mac
    builder.build()
    builder.build()
    assert macapp.is_installed()
    assert macapp.uninstall(run=tools) is True
    assert not macapp.app_path().exists() and macapp.helper_binary() is None
    assert macapp.uninstall(run=tools) is False


def test_login_item_uses_bundle_when_built(mac):
    builder, _, _ = mac
    plain = launchd.build_plist("/u/.local/bin/prn")
    assert plain["ProgramArguments"] == ["/u/.local/bin/prn", "menubar"]
    assert "AssociatedBundleIdentifiers" not in plain
    builder.build()
    branded = launchd.build_plist("/u/.local/bin/prn")
    assert branded["ProgramArguments"] == [str(macapp.launch_script())]
    assert branded["AssociatedBundleIdentifiers"] == ["com.prn.app"]
    assert branded["Label"] == launchd.LABEL


def test_notifications_go_through_branded_notifier(mac, monkeypatch):
    builder, _, _ = mac
    sent = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kw: sent.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))

    # before the app is built: Homebrew's terminal-notifier
    notify.send("CI failed", "web#1 · @alice", "Fix it", url="https://x/1")
    assert sent[-1][0].endswith("Cellar/terminal-notifier.app/Contents/MacOS/terminal-notifier")

    builder.build()
    notify.send("CI failed", "web#1 · @alice", "Fix it", url="https://x/1", group="acme/web#1")
    cmd = sent[-1]
    assert cmd[0] == str(macapp.helper_binary())
    assert cmd[cmd.index("-title") + 1] == "CI failed" and cmd[cmd.index("-open") + 1] == "https://x/1"


def test_osascript_fallback_spells_out_app_name(monkeypatch):
    sent = []
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    monkeypatch.setattr(notify.subprocess, "run",
                        lambda cmd, **kw: sent.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    notify.send("CI failed", "web#1", "Fix it", notifier="osascript")
    assert sent[-1][0] == "osascript" and 'with title "PR Review Notifier: CI failed"' in sent[-1][2]
    assert "python" not in sent[-1][2].lower()


def test_launch_script_falls_back_to_plain_prn(mac, tmp_path):
    """If the bundled interpreter can't start, launch.sh must still start the menu bar via `prn`."""
    builder, _, _ = mac
    fake_prn = tmp_path / "fake-prn"
    fake_prn.write_text('#!/bin/bash\necho "fallback ran: $* PRN_EXECUTABLE=$PRN_EXECUTABLE"\n')
    fake_prn.chmod(0o755)
    builder.prn_exe = str(fake_prn)
    builder.build()   # the bundled "interpreter" is fake bytes, so it can't run
    res = subprocess.run(["bash", str(macapp.launch_script())], capture_output=True, text=True)
    assert res.returncode == 0
    assert f"fallback ran: menubar PRN_EXECUTABLE={fake_prn}" in res.stdout
    assert "falling back" in res.stderr
