"""Crash reports: Python tracebacks of native crashes (segmentation faults, aborts) written to a file.

A crash inside Qt kills the process without a Python traceback, and an installed build may have no console to
print one to. While the application runs, `faulthandler` writes the traceback of every thread to `crash.log` in a
per-user directory, after a header describing the run (versions, Qt platform and theme). A clean exit deletes the
file, so a report left at the next start means that run crashed: it is kept as `crash.previous.log`.
"""

import datetime
import faulthandler
import os
import platform
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import IO, Iterable, List, Mapping, Optional, Type

APP_DIR = 'CANExplorer'
CRASH_LOG = 'crash.log'
PREVIOUS_CRASH_LOG = 'crash.previous.log'
# Ends the header: anything written below it is a crash report
REPORT_MARKER = '--- Crash report (written by faulthandler when the application crashes) ---'
# What decides how Qt draws windows and dialogs (e.g. which file dialog is used)
ENVIRONMENT_VARIABLES = (
    'QT_QPA_PLATFORM',
    'QT_QPA_PLATFORMTHEME',
    'QT_QUICK_CONTROLS_STYLE',
    'QT_QUICK_BACKEND',
    'XDG_SESSION_TYPE',
    'XDG_CURRENT_DESKTOP',
    'WAYLAND_DISPLAY',
    'DISPLAY',
)


def crash_log_dir(
    environ: Mapping[str, str] = os.environ, platform_name: str = sys.platform, home: Optional[Path] = None
) -> Path:
    """Per-user directory of the crash reports: the platform's place for logs or local state."""
    home = Path.home() if home is None else home
    if platform_name == 'win32':
        return Path(environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / APP_DIR
    if platform_name == 'darwin':
        return home / 'Library' / 'Logs' / APP_DIR
    return Path(environ.get('XDG_STATE_HOME') or home / '.local' / 'state') / APP_DIR


def report_header(
    version: str,
    extra: Iterable[str] = (),
    environ: Mapping[str, str] = os.environ,
    now: Optional[datetime.datetime] = None,
) -> List[str]:
    """Lines describing the run, written above the report."""
    now = datetime.datetime.now().astimezone() if now is None else now
    lines = [
        f'CANExplorer {version}, started {now.isoformat(timespec="seconds")}',
        f'Python {platform.python_version()} ({sys.executable})',
        f'Platform: {platform.platform()}',
        *extra,
    ]
    lines += [f'{name}={environ[name]}' for name in ENVIRONMENT_VARIABLES if name in environ]
    return lines


class CrashLog:
    """faulthandler (and uncaught exceptions) writing to `directory/crash.log` between start() and stop()."""

    def __init__(self, directory: Path):
        self.path = directory / CRASH_LOG
        self.previous_path = directory / PREVIOUS_CRASH_LOG
        self._file: Optional[IO[str]] = None
        self._previous_hook = None

    @property
    def enabled(self) -> bool:
        return self._file is not None

    def start(self, header: Iterable[str]) -> Optional[Path]:
        """Enable the crash report; returns the report of the previous run when that run crashed."""
        previous = self._keep_previous_report()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, 'w', encoding='utf-8')
            self._file.write(''.join(f'{line}\n' for line in [*header, REPORT_MARKER]))
            self._file.flush()  # faulthandler writes to the file descriptor, past Python's buffer
        except OSError:
            self._close()
            return previous  # E.g. a read-only home directory: run without a crash report
        faulthandler.enable(self._file, all_threads=True)
        self._previous_hook, sys.excepthook = sys.excepthook, self._excepthook
        return previous

    def stop(self) -> None:
        """Disable the crash report after a clean exit, deleting the file (there is nothing to report)."""
        if self._file is None:
            return
        faulthandler.disable()
        if sys.excepthook == self._excepthook:
            sys.excepthook = self._previous_hook
        self._close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def _excepthook(self, kind: Type[BaseException], value: BaseException, tb: Optional[TracebackType]) -> None:
        # PyQt6 aborts on an exception left uncaught in a slot: record its cause above faulthandler's traceback
        if self._file is not None:
            try:
                self._file.write(''.join(traceback.format_exception(kind, value, tb)))
                self._file.flush()
            except (OSError, ValueError):
                pass
        (self._previous_hook or sys.__excepthook__)(kind, value, tb)

    def _keep_previous_report(self) -> Optional[Path]:
        try:
            text = self.path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None  # No previous run, or it exited cleanly
        _, _, report = text.partition(REPORT_MARKER)
        try:
            if report.strip():
                os.replace(self.path, self.previous_path)
                return self.previous_path
            self.path.unlink()  # Killed without crashing (e.g. SIGKILL): only the header was written
        except OSError:
            pass
        return None

    def _close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None
