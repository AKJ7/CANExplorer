import pytest
import logging
import can
import asyncio

logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption("--can-channel", default="vcan0")


@pytest.fixture(scope="session")
def can_channel(request):
    return request.config.getoption("--can-channel")


@pytest.fixture(scope="session")
def can_bus(can_channel):
    bus = can.Bus(channel=can_channel, interface="socketcan")
    yield bus
    bus.shutdown()


@pytest.fixture(scope="session")
def loop():
    return asyncio.get_event_loop()
