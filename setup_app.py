"""
setup_app.py — py2app build script for the double-clickable .app bundle.

py2app doesn't support pyproject.toml-only projects, so this stays a
separate classic setup.py-style script. Build with the venv interpreter
that has a real Tk (see README's "Desktop app" section) — NOT plain
`python3`, which on this machine resolves to macOS's system Python and its
ancient, broken Tcl/Tk 8.5:

    .venv/bin/python setup_app.py py2app

The finished app lands in dist/iMessage Export.app.

py2app refuses to run at all if setuptools has populated
`Distribution.install_requires` (it wants dependencies declared only via its
own `packages`/`includes` options below, not via pip's dependency
resolution) — see py2app/build_app.py's "install_requires is no longer
supported" check. Modern setuptools auto-populates that attribute from this
directory's pyproject.toml `[project.dependencies]`, which we need for the
normal `pip install -e .` flow but which trips py2app's guard. So: hide
pyproject.toml from setuptools for the duration of this build only.
"""

import os
import sys
from contextlib import contextmanager

from setuptools import setup


def _check_tk_is_usable() -> None:
    """Refuse to build with an interpreter linked against macOS's ancient
    system Tcl/Tk 8.5 — it silently produces an app that opens as a process
    but never shows a window (no crash, no error). See README."""
    import tkinter

    if tkinter.TkVersion < 8.6:
        sys.exit(
            f"Refusing to build: this interpreter ({sys.executable}) has Tk "
            f"{tkinter.TkVersion}, which is macOS's broken system Tcl/Tk.\n"
            "Use the project's venv instead:\n"
            "    brew install python-tk@3.12\n"
            "    /opt/homebrew/bin/python3.12 -m venv .venv\n"
            "    .venv/bin/python -m pip install pillow fpdf2 py2app \"setuptools<81\"\n"
            "    .venv/bin/python setup_app.py py2app"
        )


_check_tk_is_usable()

APP = ["gui_app.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "packages": ["PIL", "fpdf"],
    "includes": ["messages_library", "ios_backup", "pdf_export", "exporters"],
    "plist": {
        "CFBundleName": "iMessage Export",
        "CFBundleDisplayName": "iMessage Export",
        "CFBundleIdentifier": "com.local.imessage-export",
        "CFBundleShortVersionString": "0.1.0",
        "NSHumanReadableCopyright": "",
    },
}

PYPROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pyproject.toml")


@contextmanager
def _pyproject_hidden():
    if not os.path.isfile(PYPROJECT):
        yield
        return
    hidden = PYPROJECT + ".py2app-hidden"
    os.rename(PYPROJECT, hidden)
    try:
        yield
    finally:
        os.rename(hidden, PYPROJECT)


if __name__ == "__main__":
    with _pyproject_hidden():
        setup(
            app=APP,
            name="iMessage Export",
            data_files=DATA_FILES,
            options={"py2app": OPTIONS},
        )
