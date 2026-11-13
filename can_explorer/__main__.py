import asyncio
import sys

import qasync

from can_explorer import settings
from can_explorer.__version__ import __version__
from can_explorer.main_application import MainApplication
from can_explorer.sys.crash_log import CrashLog, crash_log_dir, report_header
from can_explorer.sys.environment import sanitize_snap_environment
from can_explorer.sys.log_handler import get_logger, setup_logging

logger = get_logger(__name__)


def start_crash_log() -> CrashLog:
    """Write a crash report if the application crashes (see `can_explorer/sys/crash_log.py`)."""
    from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, qVersion

    crash_log = CrashLog(crash_log_dir())
    header = report_header(__version__, [f'PyQt {PYQT_VERSION_STR}, Qt {qVersion()} (built with {QT_VERSION_STR})'])
    if previous := crash_log.start(header):
        logger.warning(f'The previous run crashed, its crash report is {previous}: please attach it to a bug report')
    if crash_log.enabled:
        logger.debug(f'Crash report enabled: {crash_log.path}')
    return crash_log


def run_app() -> int:
    settings.apply_cli_args()
    setup_logging()
    logger.info('Starting application')
    if restored := sanitize_snap_environment():
        logger.info(f'Removed snap runtime from environment: {", ".join(restored)}')
    crash_log = start_crash_log()  # After the environment is final, so the report shows the one Qt uses
    try:
        return _run()
    finally:
        crash_log.stop()


def _run() -> int:
    app = MainApplication()
    # Qt and asyncio share a single event loop, so coroutines and Qt signals run on the GUI thread
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    app_closed = asyncio.Event()
    app.aboutToQuit.connect(app_closed.set)
    with loop:
        loop.run_until_complete(app_closed.wait())
    logger.info('Exiting app. Have a nice day!')
    return 0


DESKTOP_ENTRY_OPTIONS = ('--install-desktop-entry', '--uninstall-desktop-entry')


def main():
    if len(sys.argv) == 2 and sys.argv[1] in DESKTOP_ENTRY_OPTIONS:
        from can_explorer import desktop

        sys.exit(desktop.run(sys.argv[1]))
    sys.exit(run_app())


if __name__ == '__main__':
    main()
