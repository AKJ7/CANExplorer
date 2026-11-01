import pytest

from can_explorer.core.j1850 import (
    Class2EcuSimulator,
    Class2Server,
    J1850Bus,
    J1850Client,
    J1850Decoder,
    J1850Error,
    J1850Message,
    J1850NegativeResponse,
    describe_payload,
    format_obd_dtc,
    j1850_crc,
)


@pytest.fixture
async def ecu():
    bus = J1850Bus()
    server = Class2Server()
    simulator = Class2EcuSimulator(bus, server=server)
    simulator.start()
    client = J1850Client(bus.attach())
    yield client, server
    simulator.stop()


def test_crc_and_frame_round_trip():
    # Known SAE J1850 CRC-8 value (polynomial 0x1D, init 0xFF, final XOR 0xFF).
    assert j1850_crc(b'\x00') == 0x3B
    message = J1850Message(0x68, 0x6A, 0xF1, bytes([0x01, 0x0C]))
    frame = message.encode()
    assert frame[:3] == bytes([0x68, 0x6A, 0xF1]) and frame[-1] == j1850_crc(frame[:-1])
    assert J1850Message.decode(frame) == message


def test_decode_rejects_bad_crc_and_short_frames():
    with pytest.raises(J1850Error):
        J1850Message.decode(b'\x68\x6a')  # Too short
    good = J1850Message(0x68, 0x10, 0xF1, b'\x01\x0d').encode()
    with pytest.raises(J1850Error):
        J1850Message.decode(good[:-1] + bytes([good[-1] ^ 0xFF]))  # Corrupted CRC


def test_formatting_and_descriptions():
    assert format_obd_dtc(0x0143) == 'P0143'
    assert format_obd_dtc(0x0171) == 'P0171'
    assert format_obd_dtc(0xC100) == 'U0100'
    assert describe_payload(bytes([0x01, 0x0C])) == 'ShowCurrentData 0C'
    # A mode 0x01 response for PID 0x0C (engine speed) is decoded through the shared OBD-II PID table.
    assert describe_payload(bytes([0x41, 0x0C, 0x0C, 0x6B])).startswith('Engine speed:')
    assert (
        describe_payload(bytes([0x7F, 0x01, 0x31])) == 'Negative response to ShowCurrentData: requestOutOfRange (0x31)'
    )


async def test_current_data(ecu):
    client, _ = ecu
    assert await client.current_data(0x05) == 50.0  # 0x5A - 40 °C
    assert await client.current_data(0x0C) == (256 * 0x0C + 0x6B) / 4  # Engine speed
    with pytest.raises(J1850NegativeResponse) as info:
        await client.request_pid(0x1F)  # Unsupported PID
    assert info.value.code == 0x31


async def test_dtcs(ecu):
    client, server = ecu
    assert await client.read_dtcs() == ['P0143', 'P0171']
    await client.clear_dtcs()
    assert server.dtcs == []
    assert await client.read_dtcs() == []


async def test_vehicle_info_and_unsupported_mode(ecu):
    client, _ = ecu
    assert await client.vehicle_info(0x02) == b'1GCEK14V5XZ100001'
    result = await client.request(bytes([0x02, 0x00]))  # ShowFreezeFrameData: not handled by the simulator
    assert not result.positive and 'serviceNotSupported' in result.description


async def test_raw_request_and_decoder(ecu):
    client, _ = ecu
    result = await client.request(bytes([0x01, 0x0D]))
    assert result.positive and result.response[:2] == bytes([0x41, 0x0D])

    decoder = J1850Decoder()
    request = J1850Message(0x68, 0x6A, 0xF1, bytes([0x01, 0x0C]))
    response = J1850Message(0x68, 0xF1, 0x10, bytes([0x41, 0x0C, 0x0C, 0x6B]))
    assert decoder.describe(request) == 'J1850 → ShowCurrentData 0C'
    assert decoder.describe(response).startswith('J1850 ← Engine speed:')
