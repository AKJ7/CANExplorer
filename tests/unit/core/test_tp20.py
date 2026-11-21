import asyncio
import uuid

import can
import pytest

from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.tp20 import (
    OP_SETUP_POSITIVE,
    OP_SETUP_REQUEST,
    SETUP_ID,
    ChannelParameters,
    ChannelSetup,
    Tp20Channel,
    Tp20Client,
    Tp20Error,
    decode_channel_id,
    decode_timing,
    encode_channel_id,
    encode_timing,
    split_payload,
)
from tests.unit.waiting import wait_until

# The example exchange of the community TP 2.0 description (https://jazdw.net/tp20)
SETUP_REQUEST = bytes.fromhex('01C00010000301')
SETUP_RESPONSE = bytes.fromhex('00D00003400701')
PARAMETERS_REQUEST = bytes.fromhex('A00F8AFF32FF')
PARAMETERS_RESPONSE = bytes.fromhex('A10F8AFF4AFF')
GROUP_1 = bytes.fromhex('6101010000270000220080 1A324B25027A250000250000250000'.replace(' ', ''))


@pytest.fixture
async def tester_and_ecu():
    channel = f'tp20-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    yield tester, ecu
    tester.stop()
    ecu.stop()


class FakeModule:
    """A module end: answers the channel setup (transmit on 0x300, listen on 0x740, as in the example), then
    answers each request with `handler(payload)` through a `Tp20Channel`."""

    def __init__(self, session, module=0x01, handler=None, refuse=None, parameters=None, answer_setup=True):
        self.session, self.module = session, module
        self.handler = handler or (lambda payload: bytes([payload[0] + 0x40]) + payload[1:])
        self.refuse, self.answer_setup = refuse, answer_setup
        self.parameters = parameters or ChannelParameters(0x0F, 0.1, 0.01)
        self.channel = None
        self.requests = []
        self.sent = []  # Frames this end sent, by (id, data)
        self._task = None
        session.add_message_listener(self._on_setup)

    def _on_setup(self, msg):
        if not msg.is_rx or msg.arbitration_id != SETUP_ID or msg.data[0] != self.module or not self.answer_setup:
            return
        request = ChannelSetup.parse(bytes(msg.data))
        if request is None or request.opcode != OP_SETUP_REQUEST:
            return
        if self.refuse is not None:
            self._send(SETUP_ID + self.module, ChannelSetup(0x00, self.refuse, None, None).encode())
            return
        # The module transmits on the identifier the tester asked for and listens on 0x740
        self.channel = Tp20Channel(self.session, request.tx_id, 0x740, self.parameters)
        self._send(SETUP_ID + self.module, ChannelSetup(0x00, OP_SETUP_POSITIVE, request.tx_id, 0x740).encode())
        self._task = asyncio.get_running_loop().create_task(self._serve(self.channel))

    def _send(self, can_id, data):
        self.sent.append((can_id, bytes(data)))
        self.session.send(can.Message(arbitration_id=can_id, is_extended_id=False, data=data))

    async def _serve(self, channel):
        while channel.open:
            try:
                payload = await channel.receive(10)
            except Tp20Error:
                return
            self.requests.append(payload)
            response = self.handler(payload)
            if response:
                await channel.send(response)

    def close(self):
        self.session.remove_message_listener(self._on_setup)
        if self._task is not None:
            self._task.cancel()
        if self.channel is not None:
            self.channel.close()


def sent_frames(session, can_id):
    return [bytes(frame.message.data) for frame in session.trace if frame.message.arbitration_id == can_id]


# Frame helpers


def test_channel_ids_match_the_example_frames():
    assert decode_channel_id(bytes.fromhex('0003')) == 0x300
    assert decode_channel_id(bytes.fromhex('4007')) == 0x740
    assert decode_channel_id(bytes.fromhex('0010')) is None  # Validity nibble 1: invalid
    assert encode_channel_id(0x300) == bytes.fromhex('0003')
    assert encode_channel_id(0x740) == bytes.fromhex('4007')
    assert encode_channel_id(None) == bytes.fromhex('0010')
    with pytest.raises(ValueError):
        encode_channel_id(0x800)


def test_setup_frames_round_trip():
    request = ChannelSetup.parse(SETUP_REQUEST)
    assert request == ChannelSetup(0x01, OP_SETUP_REQUEST, None, 0x300, 0x01)
    assert request.encode() == SETUP_REQUEST
    response = ChannelSetup.parse(SETUP_RESPONSE)
    assert (response.opcode, response.rx_id, response.tx_id) == (OP_SETUP_POSITIVE, 0x300, 0x740)
    assert response.encode() == SETUP_RESPONSE
    assert ChannelSetup.parse(bytes.fromhex('00D600100010 01'.replace(' ', ''))).negative
    assert ChannelSetup.parse(bytes.fromhex('01C0')) is None
    assert ChannelSetup.parse(bytes.fromhex('0123000000 0000'.replace(' ', ''))) is None  # Broadcast


def test_timing_bytes():
    assert decode_timing(0x8A) == pytest.approx(0.1)  # 10 x 10 ms
    assert decode_timing(0x32) == pytest.approx(0.005)  # 50 x 0.1 ms
    assert decode_timing(0x4A) == pytest.approx(0.010)  # 10 x 1 ms
    assert encode_timing(0.005) == 0x32
    assert encode_timing(0.1) == 0x8A  # The finest unit that reaches it, as in the example's T1
    assert encode_timing(100) == 0xFF  # Clamped to 63 x 100 ms
    for seconds in (0.0003, 0.002, 0.05, 0.3, 5.0):
        assert decode_timing(encode_timing(seconds)) == pytest.approx(seconds)


def test_parameters_frames():
    assert ChannelParameters(0x0F, decode_timing(0x8A), 0.005).encode() == PARAMETERS_REQUEST
    response = ChannelParameters.parse(PARAMETERS_RESPONSE)
    assert response.block_size == 0x0F and response.t3 == pytest.approx(0.010)
    assert ChannelParameters.parse(b'\xa3') is None


def test_split_payload():
    assert split_payload(bytes.fromhex('1089')) == [bytes.fromhex('00021089')]
    chunks = split_payload(GROUP_1)
    assert chunks[0] == bytes.fromhex('001A6101010000')
    assert [len(chunk) for chunk in chunks] == [7, 7, 7, 7]
    with pytest.raises(Tp20Error):
        split_payload(b'')


# The client against a module


async def test_open_request_and_disconnect_match_the_example(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session, handler=lambda payload: bytes([payload[0] + 0x40]) + payload[1:])
    try:
        client = Tp20Client(tester_session, 0x01, parameters=ChannelParameters(0x0F, 0.1, 0.005), keep_alive=0)
        async with client:
            assert (client.tx_id, client.rx_id) == (0x740, 0x300)
            assert client.channel.peer.t3 == pytest.approx(0.01)
            assert await client.request(bytes.fromhex('1089')) == bytes.fromhex('5089')
            assert await client.request(bytes.fromhex('2101')) == bytes.fromhex('6101')
        tester_frames = sent_frames(tester_session, 0x740)
        assert sent_frames(tester_session, SETUP_ID)[0] == SETUP_REQUEST
        assert tester_frames[0] == PARAMETERS_REQUEST
        # Data and ACKs exactly as in the example: first message sequence 0, then 1, ACKs carry the next number
        assert bytes.fromhex('10000210 89'.replace(' ', '')) in tester_frames
        assert bytes.fromhex('1100022101') in tester_frames
        assert tester_frames[-1] == b'\xa8'
        assert b'\xb1' in sent_frames(ecu_session, 0x300)
        assert not client.is_open
    finally:
        module.close()


@pytest.mark.parametrize('length', [1, 5, 6, 12, 26, 200, 1000])
async def test_long_messages_both_directions(tester_and_ecu, length):
    tester_session, ecu_session = tester_and_ecu
    payload = bytes([0x21]) + bytes(i % 256 for i in range(length - 1))
    module = FakeModule(ecu_session, handler=lambda request: request[::-1], parameters=ChannelParameters(3, 0.1, 0))
    try:
        async with Tp20Client(tester_session, 0x01, parameters=ChannelParameters(4, 0.1, 0), keep_alive=0) as client:
            assert await client.request(payload, timeout=5) == payload[::-1]
        assert module.requests == [payload]
        # The smaller block size (3) was honoured: no more than three packets between ACK requests
        run = 0
        for data in sent_frames(tester_session, 0x740):
            if data[0] >> 4 in (0x2,):
                run += 1
                assert run < 3
            elif data[0] >> 4 in (0x0, 0x1):
                run = 0
    finally:
        module.close()


async def test_module_measuring_block_reply_is_reassembled(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session, handler=lambda request: GROUP_1)
    try:
        async with Tp20Client(tester_session, 0x01, keep_alive=0) as client:
            assert await client.request(bytes.fromhex('2101')) == GROUP_1
    finally:
        module.close()


async def test_refused_setup(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session, refuse=0xD6)
    try:
        with pytest.raises(Tp20Error, match='refused'):
            await Tp20Client(tester_session, 0x01, keep_alive=0).open()
    finally:
        module.close()


async def test_no_answer_to_setup(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session, module=0x17)  # Another module
    try:
        with pytest.raises(Tp20Error, match='did not answer'):
            await Tp20Client(tester_session, 0x01, timeout=0.2, keep_alive=0).open()
    finally:
        module.close()


async def test_request_before_open_fails(tester_and_ecu):
    tester_session, _ = tester_and_ecu
    with pytest.raises(Tp20Error, match='not open'):
        await Tp20Client(tester_session, 0x01).send(b'\x10\x89')


async def test_module_disconnect_ends_the_channel(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session, handler=lambda request: None)
    try:
        client = Tp20Client(tester_session, 0x01, keep_alive=0)
        await client.open()
        module.channel.send_frame(b'\xa8')
        assert await wait_until(lambda: not client.is_open)
        assert b'\xa8' in sent_frames(tester_session, 0x740)  # The tester answered with its own disconnect
        with pytest.raises(Tp20Error):
            await client.send(b'\x10\x89')
        client.close()
    finally:
        module.close()


async def test_missing_ack_times_out(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session)
    try:
        client = Tp20Client(tester_session, 0x01, timeout=0.2, keep_alive=0)
        await client.open()
        module.channel.close()  # The module stops answering
        with pytest.raises(Tp20Error, match='ACK'):
            await client.send(b'\x10\x89')
        client.close()
    finally:
        module.close()


async def test_keep_alive_sends_channel_tests(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session)
    try:
        async with Tp20Client(tester_session, 0x01, keep_alive=0.05) as client:
            assert await wait_until(lambda: b'\xa3' in sent_frames(tester_session, 0x740))
            # The module answers a channel test with its parameters
            assert await wait_until(lambda: sum(d[0] == 0xA1 for d in sent_frames(ecu_session, 0x300)) >= 2)
            assert client.is_open
    finally:
        module.close()


async def test_close_sends_disconnect_without_waiting(tester_and_ecu):
    tester_session, ecu_session = tester_and_ecu
    module = FakeModule(ecu_session)
    try:
        client = Tp20Client(tester_session, 0x01, keep_alive=0)
        await client.open()
        client.close()
        assert not client.is_open
        assert await wait_until(lambda: b'\xa8' in sent_frames(tester_session, 0x740))
        client.close()  # Idempotent
    finally:
        module.close()


def test_invalid_module_address():
    with pytest.raises(ValueError):
        Tp20Client(None, 0x100)


async def test_a_late_ack_of_an_abandoned_send_is_ignored(tester_and_ecu):
    """A send cancelled while it waits for its ACK (e.g. a stopped live read) leaves that ACK on its way; when it
    arrives during the next send, it must not be taken for the next send's ACK (or fail it)."""
    tester_session, ecu_session = tester_and_ecu
    seen = []

    def module(msg):
        if not msg.is_rx or msg.arbitration_id != 0x740:
            return
        seen.append(bytes(msg.data))
        if msg.data[0] & 0x0F == 1:  # The second request: the late ACK of the first (B1), then its own (B2)
            for ack in (b'\xb1', b'\xb2'):
                ecu_session.send(can.Message(arbitration_id=0x300, is_extended_id=False, data=ack))

    ecu_session.add_message_listener(module)
    channel = Tp20Channel(tester_session, 0x740, 0x300, timeout=1.0)
    try:
        first = asyncio.ensure_future(channel.send(b'\x21\x01'))
        assert await wait_until(lambda: len(seen) == 1)  # Sent, waiting for an ACK that does not come yet
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await channel.send(b'\x21\x9a')  # Gets the stale B1 first, then its own B2
        assert [data[0] for data in seen] == [0x10, 0x11]
    finally:
        channel.close()
        ecu_session.remove_message_listener(module)
