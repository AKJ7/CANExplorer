import sys
import asyncio
from can_explorer.sys.log_handler import get_logger, setup_logging
from can_explorer.main_application import MainApplication

logger = get_logger(__name__)


async def run_app() -> int:
    setup_logging()
    logger.info('Starting application')
    main_application = MainApplication()
    result = main_application.run()
    logger.info('Exiting app. Have a nice day!')
    return result


def main():
    result = asyncio.run(main=run_app())
    sys.exit(result)


# async def main() -> None:
#     loop = asyncio.get_running_loop()
#     transport, protocol = await create_can_connection(
#         loop=loop,
#         protocol_factory=None,
#         url=None,
#         channel="vcan0",
#         interface="socketcan",
#         fd=True,
#     )
#     # protocol.write(bytearray([1, 2, 3]), arbitration_id=232)
#     logger.info(f"{transport=}, {protocol=}")
#     await asyncio.Future()


# def run_app() -> int:
#     app = QApplication(sys.argv)
#     event_loop = QEventLoop(app)
#     asyncio.set_event_loop(event_loop)
#
#     async def show_window():
#         window = MainWindow(app)
#         window.show()
#         result = app.exec()
#         return result
#
#     result = asyncio.run(show_window())
#     return result

if __name__ == '__main__':
    main()
