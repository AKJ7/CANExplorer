import asyncio
import sys

import qasync

from can_explorer import settings
from can_explorer.main_application import MainApplication
from can_explorer.sys.environment import sanitize_snap_environment
from can_explorer.sys.log_handler import get_logger, setup_logging

logger = get_logger(__name__)


def run_app() -> int:
    settings.apply_cli_args()
    setup_logging()
    logger.info('Starting application')
    if restored := sanitize_snap_environment():
        logger.info(f'Removed snap runtime from environment: {", ".join(restored)}')
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
