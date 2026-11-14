"""The system report: what a bug report needs to know about this installation, as text to paste into an issue.

It holds the same header as a crash report (crash_log.py: versions, operating system, Qt platform and theme), what
each python-can backend reported in the last channel detection (core/channel_detection.py: found channels, or why
none), and whether the previous run left a crash report.
"""

import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import can

from can_explorer.build_info import build_info
from can_explorer.core.channel_detection import BackendProbe
from can_explorer.sys.crash_log import PREVIOUS_CRASH_LOG, crash_log_dir, report_header


def application_header() -> List[str]:
    """The lines describing this run, shared by the crash report and the system report."""
    from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, qVersion

    return report_header(
        build_info().describe(),
        [f'PyQt {PYQT_VERSION_STR}, Qt {qVersion()} (built with {QT_VERSION_STR})', f'python-can {can.__version__}'],
    )


def system_report(
    header: Iterable[str],
    probes: Iterable[BackendProbe],
    detected_at: Optional[datetime.datetime] = None,
    crash_dir: Optional[Path] = None,
) -> str:
    """The report as Markdown-friendly text (a fenced block pastes cleanly into a GitHub issue)."""
    lines = ['CANExplorer system report', '', '```', *header, '```', '']
    probes = list(probes)
    if probes:
        when = f' ({detected_at.isoformat(timespec="seconds")})' if detected_at is not None else ''
        lines += [f'Channel detection{when}:', '```', *(probe.summary() for probe in probes), '```']
    else:
        lines.append('Channel detection: not run')
    previous = (crash_log_dir() if crash_dir is None else crash_dir) / PREVIOUS_CRASH_LOG
    lines.append('')
    if previous.exists():
        lines.append(f'The last crash left a crash report: {previous} (please attach it too)')
    else:
        lines.append('No crash report')
    return '\n'.join(lines) + '\n'
