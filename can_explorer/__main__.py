import sys
import asyncio
import logging
from qasync import QEventLoop
from PyQt6.QtWidgets import QApplication
from can_explorer import LOG_FORMAT, LOG_LEVEL, PROJECT_NAME
from can_explorer.gui.main_window import MainWindow
from can_explorer.transport.can_connection import create_can_connection
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine


logger = logging.getLogger(__name__)


def run_gui() -> int:
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    engine.quit.connect(app.quit)
    engine.load("can_explorer/gui/qt/main_window.qml")
    status = app.exec()
    return status


async def testing():
    return


async def main() -> None:
    loop = asyncio.get_running_loop()
    transport, protocol = await create_can_connection(
        loop=loop,
        protocol_factory=None,
        url=None,
        channel="vcan0",
        interface="socketcan",
        fd=True,
    )
    # protocol.write(bytearray([1, 2, 3]), arbitration_id=232)
    logger.info(f"{transport=}, {protocol=}")
    await asyncio.Future()


def run_app() -> int:
    app = QApplication(sys.argv)
    event_loop = QEventLoop(app)
    asyncio.set_event_loop(event_loop)

    async def show_window():
        window = MainWindow(app)
        window.show()
        result = app.exec()
        return result

    result = asyncio.run(show_window())
    return result


if __name__ == "__main__":
    logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL)
    logger.info(f"Starting: {PROJECT_NAME}")
    status = run_gui()
    logger.info(f"Exiting {PROJECT_NAME}. Have a nice day!")
    sys.exit(status)
    # run_app()
    # asyncio.run(main=main())
    # asyncio.run(main=run_app())
    # run_app()
    # logger.info(f"Exiting: {PROJECT_NAME} ")
    logger.info(f"{LOG_LEVEL=}")
