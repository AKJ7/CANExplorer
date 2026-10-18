import asyncio
import uuid

import can
import pytest

from can_explorer.core.j1939_tools import (
    PGN_ADDRESS_CLAIMED,
    PGN_DM1,
    Dtc,
    J1939Name,
    J1939Network,
    J1939Sender,
    build_can_id,
    decode_dm,
    encode_dm,
    parse_pgn,
)
from can_explorer.core.protocols.j1939 import J1939Decoder, J1939Id
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource

ENGINE_NAME = J1939Name(
    (1 << 63) | (0 << 60) | (0 << 56) | (0 << 49) | (0 << 40) | (0 << 35) | (0 << 32) | (123 << 21) | 4567
)


def msg(can_id, data, timestamp=0.0):
    return can.Message(arbitration_id=can_id, is_extended_id=True, data=bytes(data), timestamp=timestamp)


def test_identifiers_and_pgns():
    assert build_can_id(0xF004, 0x00, priority=3) == 0x0CF00400  # PDU2: broadcast
    assert build_can_id(0xEA00, 0xF9, 0x00) == 0x18EA00F9  # PDU1: with destination
    assert J1939Id.from_can_id(build_can_id(0xEF00, 0x11, 0x22)).destination_address == 0x22
    assert parse_pgn('CCVS1') == 0xFEF1 and parse_pgn('0xFECA DM1') == 0xFECA and parse_pgn('65226') == 0xFECA
    assert parse_pgn('EngineData', {'EngineData': 0xF004}) == 0xF004
    with pytest.raises(ValueError):
        parse_pgn('Nonsense')
    with pytest.raises(ValueError):
        build_can_id(0xF004, 0, priority=8)


def test_name():
    name = J1939Name.from_bytes(ENGINE_NAME.to_bytes())
    assert (name.identity_number, name.manufacturer_code, name.function, name.industry_group) == (4567, 123, 0, 0)
    assert name.arbitrary_address_capable and name.function_name == 'Engine'
    assert name.describe().startswith('Engine, manufacturer 123, identity 4567')
    assert J1939Name(129 << 40).function_name == 'Off-board diagnostic service tool'


def test_diagnostic_messages():
    payload = encode_dm([Dtc(110, 0, 3), Dtc(0x7FFFE, 31, 127, 1)], {'MIL': 'on', 'AWL': 'on'})
    message = decode_dm(payload)
    assert message.lamps == {'MIL': 'on', 'RSL': 'off', 'AWL': 'on', 'PL': 'off'}
    assert message.dtcs == (Dtc(110, 0, 3), Dtc(0x7FFFE, 31, 127, 1))
    assert message.dtcs[0].spn_name == 'Engine coolant temperature'
    assert 'MIL on' in message.describe() and 'SPN 110 Engine coolant temperature, FMI 0' in message.describe()
    assert decode_dm(encode_dm([])).dtcs == ()  # "No active DTC" placeholder
    assert decode_dm(b'\x00') is None


def test_decoder_describes_claims_and_dm1():
    decoder = J1939Decoder()
    claim = decoder.describe(msg(build_can_id(PGN_ADDRESS_CLAIMED, 0x00, priority=6), ENGINE_NAME.to_bytes()))
    assert 'Address Claimed' in claim and 'NAME Engine, manufacturer 123' in claim
    dm1 = decoder.describe(msg(build_can_id(PGN_DM1, 0x00), encode_dm([Dtc(190, 2, 1)], {'AWL': 'on'})))
    assert dm1.endswith('AWL on: SPN 190 Engine speed, FMI 2 (Data erratic, intermittent or incorrect), 1×')


def test_network_tracks_claims_traffic_and_transported_dm1():
    network = J1939Network()
    network.on_message(msg(build_can_id(PGN_ADDRESS_CLAIMED, 0x00), ENGINE_NAME.to_bytes(), timestamp=1.0))
    network.on_message(msg(0x0CF00400, bytes(8), timestamp=1.1))
    payload = encode_dm([Dtc(100, 1, 2), Dtc(110, 0, 1), Dtc(190, 2, 1)], {'RSL': 'on'})[:14]
    network.on_message(msg(build_can_id(0xEC00, 0x00, priority=7), [32, len(payload), 0, 2, 0xFF, 0xCA, 0xFE, 0x00]))
    network.on_message(msg(build_can_id(0xEB00, 0x00, priority=7), [1, *payload[:7]]))
    network.on_message(msg(build_can_id(0xEB00, 0x00, priority=7), [2, *payload[7:14]]))
    network.on_message(msg(build_can_id(PGN_ADDRESS_CLAIMED, 0xFE), J1939Name(5).to_bytes()))  # Cannot claim
    node = network.nodes[0x00]
    assert node.name == ENGINE_NAME and node.claimed == 1.0 and node.frames == 5
    assert node.pgns[0xF004] == 1 and node.pgns[PGN_DM1] == 1
    assert [dtc.spn for dtc in node.dm1.dtcs] == [100, 110, 190] and node.dm1.lamps['RSL'] == 'on'
    assert network.cannot_claim == [J1939Name(5)] and 0xFE not in network.nodes


async def test_sender_single_frame_bam_and_rts_cts():
    channel = f'j1939-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    log = []
    sender = J1939Sender(session, log.append)
    peer = can.Bus(interface='virtual', channel=channel)

    def received():
        frames = []
        while (frame := peer.recv(timeout=0)) is not None:
            frames.append(frame)
        return frames

    sender.request(PGN_ADDRESS_CLAIMED)
    await sender.send(0xFEF1, bytes(range(8)), source=0x17)
    frames = received()
    assert frames[0].arbitration_id == 0x18EAFFF9 and bytes(frames[0].data) == b'\x00\xee\x00'
    assert frames[1].arbitration_id == build_can_id(0xFEF1, 0x17) and bytes(frames[1].data) == bytes(range(8))

    await sender.send(0xFEEC, b'VIN1234567890ABCDEF', source=0x00)  # 19 bytes: BAM in 3 packets
    frames = received()
    assert bytes(frames[0].data[:4]) == bytes([32, 19, 0, 3]) and [frame.data[0] for frame in frames[1:]] == [1, 2, 3]
    network = J1939Network()
    for frame in frames:
        network.on_message(frame)
    assert network.nodes[0x00].pgns[0xFEEC] == 1

    # RTS/CTS to node 0x42, answered by a simulated receiver
    async def receiver():
        while True:
            frame = peer.recv(timeout=0)
            if frame is None:
                await asyncio.sleep(0.005)
                continue
            ident = J1939Id.from_can_id(frame.arbitration_id)
            if ident.pgn == 0xEC00 and frame.data[0] == 16:
                peer.send(msg(build_can_id(0xEC00, 0x42, 0xF9, 7), [17, 2, 1, 0xFF, 0xFF, *frame.data[5:8]]))
            elif ident.pgn == 0xEB00 and frame.data[0] == 2:
                peer.send(msg(build_can_id(0xEC00, 0x42, 0xF9, 7), [17, 1, 3, 0xFF, 0xFF, 0x00, 0xEF, 0x00]))
            elif ident.pgn == 0xEB00 and frame.data[0] == 3:
                peer.send(msg(build_can_id(0xEC00, 0x42, 0xF9, 7), [19, 20, 0, 3, 0xFF, 0x00, 0xEF, 0x00]))
                return

    task = asyncio.ensure_future(receiver())
    await asyncio.wait_for(sender.send(0xEF00, bytes(range(20)), destination=0x42), 3)
    await task
    assert log[-1] == 'Sent 0x0EF00 (20 B) to 0x42 with RTS/CTS'

    with pytest.raises(TimeoutError):
        await sender.send(0xEF00, bytes(20), destination=0x55)  # Nobody answers
    peer.shutdown()
    session.stop()
