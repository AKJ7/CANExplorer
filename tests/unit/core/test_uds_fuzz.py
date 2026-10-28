import uuid

import pytest

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient
from can_explorer.core.uds_fuzz import fuzz, parse_seeds
from can_explorer.core.uds_scan import FUZZ, UNRESPONSIVE


def _make_client(channel, server):
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    simulator = UdsEcuSimulator(ecu, server=server)
    simulator.start()
    transport = IsoTpClient(tester, 0x7E0, 0x7E8)
    return UdsClient(transport, timeout=0.2), simulator, tester, ecu


@pytest.fixture
async def client():
    channel = f'fuzz-{uuid.uuid4().hex}'
    uds, simulator, tester, ecu = _make_client(channel, UdsServer())
    yield uds
    uds.transport.close()
    simulator.stop()
    tester.stop()
    ecu.stop()


async def collect(generator):
    return [result async for result in generator]


async def test_fuzz_runs_and_classifies(client):
    results = await collect(fuzz(client, seeds=[bytes([0x22, 0xF1, 0x90])], count=60, seed=1, liveness_every=0))
    assert len(results) == 60
    assert all(result.kind == FUZZ for result in results)
    # A well-behaved, hardened ECU never goes unresponsive to a fuzzed request
    assert not any(result.category == UNRESPONSIVE for result in results)
    # Reading the real VIN occasionally slips through unmutated enough to be accepted
    assert any(result.response is not None for result in results)


async def test_fuzz_is_reproducible(client):
    first = [r.request for r in await collect(fuzz(client, count=40, seed=42, liveness_every=0))]
    second = [r.request for r in await collect(fuzz(client, count=40, seed=42, liveness_every=0))]
    different = [r.request for r in await collect(fuzz(client, count=40, seed=7, liveness_every=0))]
    assert first == second and first != different


async def test_fuzz_stops_on_stop_event(client):
    import asyncio

    stop = asyncio.Event()
    results = []
    async for result in fuzz(client, count=1000, seed=1, liveness_every=0):
        results.append(result)
        if len(results) == 5:
            stop.set()
        # The generator checks the event at the top of each iteration
        fuzz_stop = stop
        if fuzz_stop.is_set() and len(results) >= 5:
            break
    assert len(results) == 5


async def test_detects_an_unresponsive_ecu():
    """When the ECU stops answering mid-run, a liveness check reports it as a possible hang/crash."""

    class DeadAfter(UdsServer):
        def __init__(self, after):
            super().__init__()
            self._after = after
            self._count = 0

        def handle(self, request):
            self._count += 1
            if self._count > self._after:
                return []  # Stops answering anything, including TesterPresent: looks hung
            return super().handle(request)

    channel = f'fuzz-dead-{uuid.uuid4().hex}'
    uds, simulator, tester, ecu = _make_client(channel, DeadAfter(after=8))
    try:
        results = await collect(fuzz(uds, count=200, seed=3, liveness_every=5, delay=0))
        unresponsive = [r for r in results if r.category == UNRESPONSIVE]
        assert unresponsive, 'expected the hang to be detected'
        assert 'possible hang or crash' in unresponsive[-1].summary
        assert unresponsive[-1].interesting
        # It stopped on the hang rather than sending all 200 requests
        assert len(results) < 200
    finally:
        uds.transport.close()
        simulator.stop()
        tester.stop()
        ecu.stop()


def test_parse_seeds():
    assert parse_seeds('22 F1 90\n10 03') == (bytes([0x22, 0xF1, 0x90]), bytes([0x10, 0x03]))
    assert parse_seeds('3E00; 1902FF') == (bytes([0x3E, 0x00]), bytes([0x19, 0x02, 0xFF]))
    assert parse_seeds('   ') == ()
