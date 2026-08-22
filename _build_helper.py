"""Wrapper around ``python -m PyInstaller`` used by :file:`build.bat`.

Why a separate Python file instead of a huge ``python -c`` one-liner inside
``build.bat``?  Long argument lists with quoting / line-continuations are the
#1 source of "it works when I paste it but fails when the script runs" bugs
on Windows CMD.  Moving the logic into a real module gives us predictable
parsing, real stack traces on error, and a single place to tweak the
PyInstaller options without fighting the batch-script escape rules.

Inputs (all optional; build.bat sets them from the user-facing variables):

  ``APP_NAME``          env   Name of the produced binary / directory
                              (default: ``WxReadAssistant``).
  ``ENTRY``             env   Entry-point Python script relative to CWD
                              (default: ``main.py``).
  ``PYI_LOG``           env   File that receives *all* PyInstaller output
                              (stdout + stderr combined).
  ``RC_FILE``           env   File that receives the single integer return
                              code from PyInstaller so build.bat can read it
                              back without relying on ``%ERRORLEVEL%`` edge
                              cases.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


APP_NAME = os.environ.get("APP_NAME", "WxReadAssistant")
ENTRY = os.environ.get("ENTRY", "main.py")
PYI_LOG = os.environ.get("PYI_LOG", "pyinstaller.log")
RC_FILE = os.environ.get("RC_FILE", "build_rc.txt")


QT_MODULES_CORE = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtPrintSupport",
]


def build_args() -> list[str]:
    """Return the ``python -m PyInstaller ...`` argument list."""
    args: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        APP_NAME,
        "--windowed",
        "--onedir",
    ]

    for mod in QT_MODULES_CORE:
        args.extend(["--collect-submodules", mod])
        args.extend(["--collect-data", mod])
        args.extend(["--collect-binaries", mod])
        args.extend(["--hidden-import", mod])

    # Entry point must live next to this helper; we trust CWD set by build.bat.
    entry_path = Path(ENTRY)
    if not entry_path.exists():
        raise FileNotFoundError(f"ENTRY not found: {entry_path.resolve()}")
    args.append(str(entry_path))
    return args


def main() -> int:
    args = build_args()

    log_path = Path(PYI_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path = Path(RC_FILE)

    # Show a one-line summary to stdout so the wrapper log stays useful.
    print(f"[PYI_HELPER] argv count = {len(args) - 1}")
    print(f"[PYI_HELPER] log file   = {log_path.resolve()}")
    sys.stdout.flush()

    with log_path.open("w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.run(
            args,
            stdout=fh,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )

    rc = int(proc.returncode)
    rc_path.write_text(str(rc), encoding="utf-8")
    print(f"[PYI_HELPER] PyInstaller returned {rc}")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - we want a hard failure visible in BAT
        # Make the failure impossible to miss: ensure RC_FILE reports a
        # nonzero value even if we died before PyInstaller ran.
        try:
            Path(RC_FILE).write_text("9001", encoding="utf-8")
        except Exception:  # pragma: no cover - disk is totally broken
            pass
        print(f"[PYI_HELPER][FATAL] {exc}", file=sys.stderr)
        sys.exit(9001)
