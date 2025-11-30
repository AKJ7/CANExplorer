import pytest
import logging
import can
from qasync import QEventLoop
from pytestqt.qt_compat import qt_api
import asyncio
import signal

# from tests.QTestApplication import QTestApplication

# qt_api.set_qt_api(None)


logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption("--can-channel", default="vcan0")


@pytest.fixture(scope="session")
def can_channel(request):
    return request.config.getoption("--can-channel")


@pytest.fixture(scope="session")
def can_bus():
    bus = can.Bus(channel='test', interface='virtual', preserve_timestamp=True, receive_own_messages=True)
    yield bus
    bus.shutdown()


@pytest.fixture(scope='session')
def qapp_cls():
    class QTestApplication(qt_api.QtWidgets.QApplication):
        def __init__(self, *args):
            self._register_signals()
            super().__init__(*args)

        def _register_signals(self):
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    return QTestApplication


@pytest.fixture(scope='session')
def qloop(qapp):
    loop = QEventLoop(qapp)
    with loop:
        asyncio.set_event_loop(loop)
        logger.info(f'Running: {loop=}')
        current_loop = asyncio.get_running_loop()
        logger.info(f'Actual: {current_loop=}')
        yield loop
        # loop.close()

