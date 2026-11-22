import asyncio
import uuid

import can
import pytest

from can_explorer.core.kwp2000_client import (
    KLineTransport,
    KwpClient,
    KwpError,
    KwpNegativeResponse,
    is_response_to,
)
from can_explorer.core.measuring_blocks import read_measuring_block
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.tp20 import Tp20Client
from tests.unit.core.test_tp20 import GROUP_1, FakeModule
from tests.unit.waiting import wait_until


class ScriptedTransport:
    """Answers each request with the next list of responses (several for 'response pending' or stale traffic)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []
        self._pending = []

    async def send(self, payload):
        self.sent.append(bytes(payload))
        if self.answers:
            self._pending.extend(bytes.fromhex(answer) for answer in self.answers.pop(0))

    async def receive(self, timeout=None):
        if not self._pending:
            raise KwpError('Timeout waiting for a response')
        return self._pending.pop(0)


def test_is_response_to():
    assert is_response_to(b'\x10\x89', b'\x50\x89')
    assert not is_response_to(b'\x10\x89', b'\x50\x81')  # Another session's answer
    assert is_response_to(b'\x21\x01', b'\x61\x01\x01\x00\x00')
    assert not is_response_to(b'\x21\x01', b'\x61\x02')
    assert is_response_to(b'\x21\x01', b'\x7f\x21\x31')
    assert not is_response_to(b'\x21\x01', b'\x7f\x22\x31')
    assert is_response_to(b'\x3e', b'\x7e')
    assert not is_response_to(b'', b'\x7e')


async def test_session_0x89_waits_for_its_answer():
    # Bit 7 of 0x89 would be UDS's suppress-positive-response bit; KWP2000 must still wait for 50 89
    transport = ScriptedTransport(['50 89'])
    assert await KwpClient(transport).start_diagnostic_session(0x89) == b''
    assert transport.sent == [b'\x10\x89']


async def test_response_pending_and_stale_traffic_are_skipped():
    seen = []
    transport = ScriptedTransport(['7F 21 78', '61 02 AA', '61 01 01 C8 18'])
    client = KwpClient(transport)
    client.unsolicited.append(seen.append)
    assert await client.read_local_identifier(0x01) == bytes.fromhex('01C818')
    assert seen == [bytes.fromhex('6102AA')]


async def test_negative_response_raises():
    client = KwpClient(ScriptedTransport(['7F 21 31']))
    with pytest.raises(KwpNegativeResponse, match='ReadDataByLocalIdentifier: requestOutOfRange') as info:
        await client.read_local_identifier(0x99)
    assert (info.value.service, info.value.code) == (0x21, 0x31)


async def test_transport_error_is_reported_not_raised_by_request():
    result = await KwpClient(ScriptedTransport()).request(b'\x1a\x9b')
    assert result.response is None and 'Timeout' in result.error
    assert result.description == result.error
    with pytest.raises(KwpError, match='Timeout'):
        await KwpClient(ScriptedTransport()).call(b'\x1a\x9b')


async def test_services():
    transport = ScriptedTransport(
        ['5A 9B 30 33 4C'],
        ['7B 10'],
        ['71 B8 01'],
        ['72 B8'],
        ['73 B8 00'],
        ['51 01'],
        ['7E'],
        ['60'],
    )
    client = KwpClient(transport)
    assert await client.read_ecu_identification(0x9B) == b'03L'
    await client.write_local_identifier(0x10, b'\x01\x02')
    assert await client.start_routine(0xB8, b'\x05') == b'\x01'
    assert await client.stop_routine(0xB8) == b''
    assert await client.routine_results(0xB8) == b'\x00'
    await client.ecu_reset()
    await client.tester_present()
    await client.stop_diagnostic_session()
    assert transport.sent == [
        bytes.fromhex('1A9B'),
        bytes.fromhex('3B100102'),
        bytes.fromhex('31B805'),
        bytes.fromhex('32B8'),
        bytes.fromhex('33B8'),
        bytes.fromhex('1101'),
        bytes.fromhex('3E'),
        bytes.fromhex('20'),
    ]


async def test_session_with_baud_rate_byte():
    transport = ScriptedTransport(['50 89'])
    await KwpClient(transport).start_diagnostic_session(0x89, baud_rate=0x14)
    assert transport.sent == [bytes.fromhex('108914')]


async def test_security_access():
    keys = []

    def compute_key(level, seed):
        keys.append((level, seed))
        return bytes(b ^ 0xFF for b in seed)

    transport = ScriptedTransport(['67 01 12 34'], ['67 02'])
    assert await KwpClient(transport).security_access(0x01, compute_key)
    assert keys == [(0x01, b'\x12\x34')]
    assert transport.sent == [b'\x27\x01', b'\x27\x02\xed\xcb']
    assert not await KwpClient(ScriptedTransport(['67 01 00 00'])).security_access(0x01, compute_key)
    with pytest.raises(KwpError, match='odd'):
        await KwpClient(ScriptedTransport()).security_access(0x02, compute_key)
    with pytest.raises(KwpNegativeResponse, match='invalidKey'):
        await KwpClient(ScriptedTransport(['67 01 12 34'], ['7F 27 35'])).security_access(0x01, compute_key)


async def test_negative_session_response_raises():
    client = KwpClient(ScriptedTransport(['7F 10 12']))
    with pytest.raises(KwpNegativeResponse):
        await client.start_diagnostic_session(0x89)


# Over real transports


@pytest.fixture
async def sessions():
    channel = f'kwp-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    yield tester, ecu
    tester.stop()
    ecu.stop()


async def test_measuring_block_over_tp20(sessions):
    tester_session, ecu_session = sessions

    def handler(request):
        return {b'\x10\x89': b'\x50\x89', b'\x21\x01': GROUP_1}.get(bytes(request), b'\x7f' + request[:1] + b'\x31')

    module = FakeModule(ecu_session, handler=handler)
    try:
        async with Tp20Client(tester_session, 0x01, keep_alive=0) as transport:
            client = KwpClient(transport)
            await client.start_diagnostic_session(0x89)
            block = await read_measuring_block(client, 1)
            assert [(v.formula, v.a, v.b) for v in block.values][:4] == [
                (0x01, 0x00, 0x00),
                (0x27, 0x00, 0x00),
                (0x22, 0x00, 0x80),
                (0x1A, 0x32, 0x4B),
            ]
            with pytest.raises(KwpNegativeResponse):
                await read_measuring_block(client, 2)
    finally:
        module.close()


async def test_kline_transport_header(sessions):
    tester_session, ecu_session = sessions
    received = []

    def ecu(msg):
        if msg.is_rx and msg.is_extended_id:
            received.append(msg)
            # Answer from 0x01 to the tester 0xF1 (format: physical, 2 bytes)
            ecu_session.send(can.Message(arbitration_id=0x82F101, is_extended_id=True, data=b'\x50\x89'))

    ecu_session.add_message_listener(ecu)
    # Traffic between other nodes is not an answer
    with KLineTransport(tester_session, target=0x01) as transport:
        ecu_session.send(can.Message(arbitration_id=0x82F133, is_extended_id=True, data=b'\x50\x81'))
        await asyncio.sleep(0.05)
        assert await KwpClient(transport).start_diagnostic_session(0x89) == b''
    assert await wait_until(lambda: received)
    assert received[0].arbitration_id == 0x8201F1  # Format 0x80 | length 2, target 0x01, source 0xF1
    assert bytes(received[0].data) == b'\x10\x89'


async def test_kline_transport_limits(sessions):
    tester_session, _ = sessions
    with KLineTransport(tester_session, target=0x33, functional=True) as transport:
        with pytest.raises(KwpError, match='1-63'):
            await transport.send(bytes(64))
        await transport.send(b'\x3e')
        with pytest.raises(KwpError, match='Timeout'):
            await transport.receive(0.05)
    sent = [frame.message for frame in tester_session.trace if not frame.message.is_rx]
    assert sent[-1].arbitration_id == 0xC133F1  # Functional addressing


class SlowTransport:
    """Answers every request positively after `delay`, recording overlapping requests."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.sent = []
        self.in_flight = 0
        self.overlapped = False
        self._answers = asyncio.Queue()

    async def send(self, payload):
        self.in_flight += 1
        self.overlapped |= self.in_flight > 1
        self.sent.append(bytes(payload))
        await asyncio.sleep(self.delay)
        self._answers.put_nowait(bytes([payload[0] + 0x40]) + bytes(payload[1:2]))

    async def receive(self, timeout=None):
        try:
            return await asyncio.wait_for(self._answers.get(), timeout)
        finally:
            self.in_flight -= 1


async def test_requests_run_one_at_a_time():
    transport = SlowTransport(delay=0.02)
    client = KwpClient(transport)
    await asyncio.gather(*(client.read_local_identifier(group) for group in range(1, 6)))
    assert not transport.overlapped and len(transport.sent) == 5


async def test_keep_alive_sends_tester_present_when_idle():
    from can_explorer.core.kwp2000_client import KwpKeepAlive

    transport = SlowTransport()
    client = KwpClient(transport)
    keep_alive = KwpKeepAlive(client, interval=0.05)
    keep_alive.start()
    keep_alive.start()  # Idempotent
    try:
        assert await wait_until(lambda: keep_alive.sent >= 2)
        assert all(payload == b'\x3e' for payload in transport.sent)
        assert keep_alive.running
    finally:
        keep_alive.stop()
    assert not keep_alive.running


async def test_keep_alive_waits_while_requests_keep_the_session_busy():
    from can_explorer.core.kwp2000_client import KwpKeepAlive

    transport = SlowTransport()
    client = KwpClient(transport)
    keep_alive = KwpKeepAlive(client, interval=0.2)
    keep_alive.start()
    try:
        for _ in range(8):  # A request every 50 ms: never idle for 200 ms
            await client.read_local_identifier(1)
            await asyncio.sleep(0.05)
        assert b'\x3e' not in transport.sent
    finally:
        keep_alive.stop()
