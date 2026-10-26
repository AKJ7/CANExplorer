import uuid

import pytest

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient
from can_explorer.core.uds_scan import (
    ABSENT,
    CONDITIONS,
    NO_RESPONSE,
    POSITIVE,
    SECURITY,
    ScanResult,
    classify,
    describe_probe,
    scan_data_identifiers,
    scan_routines,
    scan_services,
    scan_subfunctions,
    sweep,
)


@pytest.fixture
async def client():
    channel = f'udsscan-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    server = UdsServer(dids={0xF190: b'VIN123', 0xF18C: b'SN-1'}, slow_routines=(0x0F0F,))
    simulator = UdsEcuSimulator(ecu, server=server)
    simulator.start()
    transport = IsoTpClient(tester, 0x7E0, 0x7E8)
    yield UdsClient(transport, timeout=0.3), server
    transport.close()
    simulator.stop()
    tester.stop()
    ecu.stop()


async def collect(generator):
    return [result async for result in generator]


def test_classify_covers_the_outcomes():
    assert classify('dids', b'\x22\xf1\x90', b'\x62\xf1\x90VIN', None) == POSITIVE
    assert classify('dids', b'\x22\x12\x34', b'\x7f\x22\x31', None) == ABSENT  # requestOutOfRange
    assert classify('dids', b'\x22\x12\x34', b'\x7f\x22\x33', None) == SECURITY  # securityAccessDenied
    assert classify('dids', b'\x22\x12\x34', None, 'Timeout') == NO_RESPONSE
    assert classify('services', b'\x27', b'\x7f\x27\x7f', None) == CONDITIONS  # not in this session
    # For a service scan, 'subFunctionNotSupported' still proves the service is handled
    assert classify('services', b'\x10', b'\x7f\x10\x12', None) != ABSENT
    assert classify('subfunctions', b'\x10\x09', b'\x7f\x10\x12', None) == ABSENT


async def test_scan_services_finds_the_handled_ones(client):
    uds, _ = client
    results = await collect(scan_services(uds))
    supported = {result.identifier for result in results if result.interesting}
    # The simulator handles these; none of them report serviceNotSupported
    assert {0x10, 0x11, 0x22, 0x27, 0x31, 0x3E, 0x19, 0x2E} <= supported
    assert 0x85 not in supported  # ControlDTCSetting is not handled: serviceNotSupported
    reset = next(result for result in results if result.identifier == 0x11)
    assert reset.interesting and not reset.response[0] == 0x51  # Rejected (truncated), not executed


async def test_scan_data_identifiers(client):
    uds, _ = client
    results = await collect(scan_data_identifiers(uds, 0xF184, 0xF192))
    found = {result.identifier: result for result in results if result.interesting}
    assert set(found) == {0xF18C, 0xF186, 0xF190}  # F186 (active session) is always answered
    assert found[0xF190].category == POSITIVE
    assert found[0xF190].response[3:] == b'VIN123'
    assert describe_probe(found[0xF190]).startswith('DID 0xF190:')
    assert all(not r.interesting for r in results if r.identifier in (0xF18D, 0xF191))


async def test_scan_subfunctions_of_session_control(client):
    uds, _ = client
    results = await collect(scan_subfunctions(uds, 0x10, 0x00, 0x05))
    positive = {result.identifier for result in results if result.category == POSITIVE}
    assert positive == {0x01, 0x02, 0x03}  # default, programming, extended sessions
    # Probing must not leave the ECU in a non-default session
    await uds.diagnostic_session_control(0x01)


async def test_scan_routines_without_starting_them(client):
    uds, server = client
    results = await collect(scan_routines(uds, 0x0F0E, 0x0F10))
    # control_type 0x03 (request results) is the default: no routine is started
    assert all(request not in server.requests or request[1] == 0x03 for request in [r.request for r in results])
    assert all(r.request[1] == 0x03 for r in results)
    assert not server.erased


async def test_sweep_iterates_one_byte(client):
    uds, _ = client
    results = await collect(sweep(uds, bytes([0x10, 0x00]), position=1, start=0x00, end=0x04))
    assert [r.identifier for r in results] == [0, 1, 2, 3, 4]
    # Session 0 and 4 are rejected (subFunctionNotSupported), 1–3 are the real sessions
    assert [r.category for r in results] == ['rejected', POSITIVE, POSITIVE, POSITIVE, 'rejected']
    with pytest.raises(ValueError, match='outside the template'):
        anext(sweep(uds, bytes([0x10]), position=1))
    with pytest.raises(ValueError, match='width'):
        anext(sweep(uds, bytes([0x22, 0, 0]), position=0, width=3))


async def test_stop_event_ends_the_scan(client):
    import asyncio

    uds, _ = client
    stop = asyncio.Event()
    results = []
    async for result in scan_data_identifiers(uds, 0x0000, 0xFFFF, stop=stop):
        results.append(result)
        if len(results) == 3:
            stop.set()
    assert len(results) == 3


def test_scan_result_nrc_and_summary():
    result = ScanResult('dids', 0x1234, b'\x22\x12\x34', b'\x7f\x22\x31')
    assert result.nrc == 0x31 and not result.interesting
    assert 'requestOutOfRange' in result.summary
    assert ScanResult('services', 0x3E, b'\x3e', None, 'Timeout waiting for a response').summary.startswith('Timeout')
