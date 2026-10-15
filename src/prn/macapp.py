"""Build "PR Review Notifier.app" so macOS shows our name and icon instead of "Python".

macOS labels notifications, Login Items, and Activity Monitor entries with the *app bundle*
that the process runs from. A plain Python process has no bundle, so it shows as "Python".
This module builds, under ~/Library/Application Support/PR Review Notifier/:

  PR Review Notifier.app/                      bundle id com.prn.app (menu bar app)
    Contents/Info.plist                        name, icon, LSUIElement (no Dock icon)
    Contents/MacOS/PR Review Notifier          a copy of prn's Python interpreter
    Contents/pyvenv.cfg + Contents/lib ->      make that copy use prn's uv tool environment
    Contents/Resources/launch.sh               what launchd runs; falls back to `prn menubar`
    Contents/Resources/AppIcon.icns
    Contents/Helpers/PR Review Notifier.app    bundle id com.prn.notifier: a renamed copy of
                                               terminal-notifier that sends every notification

Each step shells out to standard macOS tools (otool, install_name_tool, codesign, sips,
iconutil, lsregister) through `self.run`, so tests can swap in a fake.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Callable, Optional

from . import __version__

APP_NAME = "PR Review Notifier"
APP_ID = "com.prn.app"
HELPER_ID = "com.prn.notifier"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


class AppBuildError(Exception):
    pass


# ---------------------------------------------------------------- locations


def install_root() -> Path:
    return Path(os.environ.get("PRN_APP_DIR", Path.home() / "Library" / "Application Support" / APP_NAME))


def app_path() -> Path:
    return install_root() / f"{APP_NAME}.app"


def app_executable() -> Path:
    return app_path() / "Contents" / "MacOS" / APP_NAME


def launch_script() -> Path:
    return app_path() / "Contents" / "Resources" / "launch.sh"


def helper_path() -> Path:
    return app_path() / "Contents" / "Helpers" / f"{APP_NAME}.app"


def helper_binary() -> Optional[Path]:
    """The branded notifier executable, if the app has been built."""
    plist = helper_path() / "Contents" / "Info.plist"
    try:
        exe = plistlib.loads(plist.read_bytes()).get("CFBundleExecutable", "terminal-notifier")
    except (FileNotFoundError, plistlib.InvalidFileException):
        return None
    p = helper_path() / "Contents" / "MacOS" / exe
    return p if os.access(p, os.X_OK) else None


def is_installed() -> bool:
    return launch_script().exists() and helper_binary() is not None


# ---------------------------------------------------------------- terminal-notifier discovery


def find_terminal_notifier_app(run: Callable = subprocess.run) -> Path:
    """Locate Homebrew's terminal-notifier.app (the bin entry is a wrapper script or a symlink)."""
    from .github import find_binary

    exe = find_binary("terminal-notifier")
    if exe:
        real = Path(os.path.realpath(exe))
        if real.parent.name == "MacOS" and real.parents[2].suffix == ".app":
            return real.parents[2]
        try:
            m = re.search(r'([^"\'\s]+?\.app)/Contents/MacOS/terminal-notifier', real.read_text(errors="ignore"))
            if m and Path(m.group(1)).is_dir():
                return Path(m.group(1))
        except OSError:
            pass
    brew = find_binary("brew")
    if brew:
        res = run([brew, "--prefix", "terminal-notifier"], capture_output=True, text=True)
        cand = Path(res.stdout.strip()) / "terminal-notifier.app"
        if res.returncode == 0 and cand.is_dir():
            return cand
    raise AppBuildError("terminal-notifier.app not found. Install it with `brew install terminal-notifier`.")


# ---------------------------------------------------------------- builder


def _launch_sh(prn_fallback: str) -> str:
    return f"""#!/bin/bash
# Started by launchd. Runs the menu bar app from inside this bundle so macOS shows
# "{APP_NAME}" (not "Python"). If the bundled interpreter can't start (for example
# after a Python upgrade), fall back to the plain `prn` command so the app still runs.
APP_EXE="$(cd "$(dirname "$0")/../MacOS" && pwd)/{APP_NAME}"
export PRN_EXECUTABLE="{prn_fallback}"
if "$APP_EXE" -c 'import prn.menubar' >/dev/null 2>&1; then
  exec "$APP_EXE" -m prn menubar
fi
echo "bundled interpreter failed to start; falling back to {prn_fallback}" >&2
exec "{prn_fallback}" menubar
"""


class AppBuilder:
    def __init__(self, run: Callable = subprocess.run, python_exe: Optional[str] = None,
                 venv_prefix: Optional[str] = None, prn_exe: Optional[str] = None):
        self.run = run
        self.python_exe = Path(os.path.realpath(python_exe or getattr(sys, "_base_executable", sys.executable)))
        prefix = Path(venv_prefix or sys.prefix)
        if (prefix / "lib").is_symlink():
            # Running from the bundled interpreter itself: its "venv" is a symlink to the real one.
            prefix = Path(os.path.realpath(prefix / "lib")).parent
        self.venv_prefix = prefix
        if prn_exe is None:
            from .launchd import prn_executable
            prn_exe = prn_executable()
        self.prn_exe = prn_exe

    def _check(self, cmd: list[str], what: str) -> subprocess.CompletedProcess:
        res = self.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise AppBuildError(f"{what} failed: {(res.stderr or res.stdout).strip()[:300]}")
        return res

    # ---------------------------------------------------------- pieces

    def _icon(self, resources_dir: Path, work: Path) -> Optional[str]:
        """Build AppIcon.icns from the packaged 1024px PNG. The icon is cosmetic, so failures are ignored."""
        try:
            png = Path(str(resources.files("prn").joinpath("assets/app_icon.png")))
            iconset = work / "AppIcon.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    px = size * scale
                    name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                    self._check(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)], "sips")
            self._check(["iconutil", "-c", "icns", str(iconset), "-o", str(resources_dir / "AppIcon.icns")],
                        "iconutil")
            return "AppIcon"
        except (AppBuildError, OSError):
            return None

    def _copy_interpreter(self, dest: Path, work: Path) -> None:
        """Copy the Python executable and point its @executable_path libraries back at the original install."""
        tmp = work / "python-copy"
        shutil.copy2(self.python_exe, tmp)
        orig_dir = self.python_exe.parent
        libs = self._check(["otool", "-L", str(tmp)], "otool -L").stdout
        for line in libs.splitlines()[1:]:
            dep = line.strip().split(" (")[0]
            for token in ("@executable_path/", "@loader_path/"):
                if dep.startswith(token):
                    target = os.path.normpath(orig_dir / dep[len(token):])
                    self._check(["install_name_tool", "-change", dep, target, str(tmp)], "install_name_tool")
        rpaths = re.findall(r"cmd LC_RPATH\n\s+cmdsize \d+\n\s+path (\S+)", self._check(
            ["otool", "-l", str(tmp)], "otool -l").stdout)
        for rp in rpaths:
            for token in ("@executable_path/", "@loader_path/"):
                if rp.startswith(token):
                    target = os.path.normpath(orig_dir / rp[len(token):])
                    self._check(["install_name_tool", "-rpath", rp, target, str(tmp)], "install_name_tool")
        # Sign before moving into the bundle so codesign signs just this file, not the bundle.
        self._check(["codesign", "--force", "--sign", "-", str(tmp)], "codesign (interpreter)")
        shutil.move(str(tmp), dest)
        dest.chmod(0o755)

    def _helper(self, tn_app: Path, dest: Path, icon_src: Optional[Path]) -> None:
        shutil.copytree(tn_app, dest, symlinks=True)
        plist_file = dest / "Contents" / "Info.plist"
        info = plistlib.loads(plist_file.read_bytes())
        info.update({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": HELPER_ID,
            "LSUIElement": True,
        })
        if icon_src and icon_src.exists():
            shutil.copy2(icon_src, dest / "Contents" / "Resources" / "AppIcon.icns")
            info["CFBundleIconFile"] = "AppIcon"
        plist_file.write_bytes(plistlib.dumps(info))
        shutil.rmtree(dest / "Contents" / "_CodeSignature", ignore_errors=True)
        self._check(["codesign", "--force", "--deep", "--sign", "-", str(dest)], "codesign (notifier)")

    # ---------------------------------------------------------- build

    def build(self) -> Path:
        if sys.platform != "darwin" and self.run is subprocess.run:
            raise AppBuildError("The app bundle can only be built on macOS.")
        pyvenv = self.venv_prefix / "pyvenv.cfg"
        if not pyvenv.exists():
            raise AppBuildError(f"prn isn't running from a virtual environment ({self.venv_prefix}). "
                                "Install it with ./setup.sh.")
        tn_app = find_terminal_notifier_app(self.run)

        root = install_root()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root, prefix=".build-") as tmp:
            work = Path(tmp)
            staged = work / f"{APP_NAME}.app"
            contents = staged / "Contents"
            (contents / "MacOS").mkdir(parents=True)
            (contents / "Resources").mkdir()
            (contents / "Helpers").mkdir()

            icon = self._icon(contents / "Resources", work)
            info = {
                "CFBundleName": APP_NAME,
                "CFBundleDisplayName": APP_NAME,
                "CFBundleIdentifier": APP_ID,
                "CFBundleExecutable": APP_NAME,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": __version__,
                "CFBundleVersion": __version__,
                "LSUIElement": True,               # menu bar only, no Dock icon
                "NSHighResolutionCapable": True,
            }
            if icon:
                info["CFBundleIconFile"] = icon
            (contents / "Info.plist").write_bytes(plistlib.dumps(info))

            # Bundled interpreter that behaves like prn's venv: pyvenv.cfg next to MacOS/,
            # and lib/ -> the venv's lib (site-packages, including the editable install).
            self._copy_interpreter(contents / "MacOS" / APP_NAME, work)
            shutil.copy2(pyvenv, contents / "pyvenv.cfg")
            (contents / "lib").symlink_to(self.venv_prefix / "lib")

            script = contents / "Resources" / "launch.sh"
            script.write_text(_launch_sh(self.prn_exe))
            script.chmod(0o755)

            self._helper(tn_app, contents / "Helpers" / f"{APP_NAME}.app",
                         contents / "Resources" / "AppIcon.icns" if icon else None)

            final = app_path()
            if final.exists():
                shutil.rmtree(final)
            shutil.move(str(staged), final)

        for bundle in (app_path(), helper_path()):
            self.run([LSREGISTER, "-f", str(bundle)], capture_output=True, text=True)
        self.verify()
        return app_path()

    def verify(self) -> None:
        res = self.run([str(app_executable()), "-c", "import prn.menubar, sys; print(sys.prefix)"],
                       capture_output=True, text=True)
        if res.returncode != 0:
            raise AppBuildError("The bundled interpreter can't import prn: "
                                f"{(res.stderr or res.stdout).strip()[-300:]}")


def build(run: Callable = subprocess.run) -> Path:
    return AppBuilder(run=run).build()


def uninstall(run: Callable = subprocess.run) -> bool:
    removed = False
    for bundle in (helper_path(), app_path()):
        if bundle.exists():
            run([LSREGISTER, "-u", str(bundle)], capture_output=True, text=True)
    if app_path().exists():
        shutil.rmtree(app_path())
        removed = True
    try:
        install_root().rmdir()
    except OSError:
        pass
    return removed
