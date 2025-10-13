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
def can_bus():
    bus = can.Bus(channel='test', interface='virtual', preserve_timestamp=True, receive_own_messages=True)
    yield bus
    bus.shutdown()
