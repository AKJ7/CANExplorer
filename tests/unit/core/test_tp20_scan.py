import uuid

import pytest

from can_explorer.core.diagnostic_labels import LabelFile
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.tp20_scan import ModuleScanResult, parse_addresses, scan_modules
from tests.unit.core.test_tp20 import FakeModule
from tests.unit.waiting import wait_until


@pytest.fixture
async def sessions():
    channel = f'tp20-scan-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    yield tester, ecu
    tester.stop()
    ecu.stop()


def test_parse_addresses():
    assert parse_addresses('01-03') == [1, 2, 3]
    assert parse_addresses('01, 17; 0x56') == [0x01, 0x17, 0x56]
    assert parse_addresses('01-02, 02, 17') == [1, 2, 0x17]
    assert len(parse_addresses('01-FF')) == 255
    for text in ('', 'zz', '10-01', '01-100'):
        with pytest.raises(ValueError):
            parse_addresses(text)


def answers(identification: bytes):
    table = {b'\x10\x89': b'\x50\x89', b'\x1a\x9b': b'\x5a\x9b' + identification}
    return lambda request: table.get(bytes(request), b'\x7f' + request[:1] + b'\x11')


async def test_scan_finds_modules(sessions):
    tester, ecu = sessions
    modules = [
        FakeModule(ecu, 0x01, handler=answers(b'03L906018 ')),
        FakeModule(ecu, 0x17, handler=answers(b'1K0920')),
        FakeModule(ecu, 0x25, refuse=0xD6),
    ]
    labels = LabelFile.from_dict({'modules': {'01': {'name': 'Engine'}}})
    try:
        results = [
            r
            async for r in scan_modules(
                tester,
                [0x01, 0x02, 0x17, 0x25],
                identification=0x9B,
                diagnostic_session=0x89,
                labels=labels,
            )
        ]
    finally:
        for module in modules:
            module.close()
    assert [r.address for r in results] == [0x01, 0x02, 0x17, 0x25]
    engine, empty, cluster, refused = results
    assert engine.found and engine.name == 'Engine' and engine.identification_text == '03L906018'
    assert (engine.tx_id, engine.rx_id) == (0x740, 0x300) and not engine.error
    assert not empty.found and empty.as_dict() == {'address': '02'}
    assert cluster.found and cluster.identification == b'1K0920'
    assert refused.found and 'refused' in refused.error and refused.tx_id is None
    assert engine.as_dict() == {
        'address': '01',
        'name': 'Engine',
        'tester_transmits_on': '740',
        'module_transmits_on': '300',
        'identification': '30 33 4C 39 30 36 30 31 38 20',
        'identification_text': '03L906018',
    }
    # Every channel was closed again
    assert all(module.channel is None or not module.channel.open for module in modules)


async def test_scan_without_identification_and_with_a_failing_one(sessions):
    tester, ecu = sessions
    module = FakeModule(ecu, 0x01, handler=lambda request: b'\x7f' + request[:1] + b'\x31')
    try:
        plain = [r async for r in scan_modules(tester, [0x01])]
        failing = [r async for r in scan_modules(tester, [0x01], identification=0x9B)]
    finally:
        module.close()
    assert plain[0].found and plain[0].identification is None and not plain[0].error
    assert failing[0].found and 'requestOutOfRange' in failing[0].error


def test_identification_text_of_nothing():
    assert ModuleScanResult(1, True).identification_text == ''


async def test_cancelling_a_scan_in_flight_unwinds_promptly(sessions):
    """The scan generator closes each client in a finally; that finally must be synchronous, so cancelling a scan
    while it waits inside a module (here an identification read the module never answers) unwinds at once instead
    of hanging in an awaited teardown."""
    import asyncio
    import time

    tester, ecu = sessions
    # Answers the session but never ReadECUIdentification (0x1A), so the scan blocks there until cancelled
    handler = lambda request: b'\x50\x89' if bytes(request) == b'\x10\x89' else None  # noqa: E731
    module = FakeModule(ecu, 0x01, handler=handler)
    try:
        agen = scan_modules(tester, [0x01], identification=0x9B, diagnostic_session=0x89, timeout=5.0)
        task = asyncio.ensure_future(agen.__anext__())
        assert await wait_until(lambda: len(module.requests) >= 2)  # Session sent, waiting on the identification read
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await agen.aclose()
        assert time.monotonic() - started < 2.0  # Unwound at once, not after the 5 s read timeout or a hang
        assert await wait_until(lambda: not module.channel.open)  # The disconnect it sent reached the module
    finally:
        module.close()
