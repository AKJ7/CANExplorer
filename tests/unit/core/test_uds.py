import asyncio
import uuid

import can
import pytest

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer, xor_key
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import (
    Dtc,
    SessionKeepAlive,
    UdsClient,
    UdsDecoder,
    UdsError,
    UdsNegativeResponse,
    describe_payload,
    format_did_value,
    format_dtc,
    load_key_function,
)


@pytest.fixture
async def ecu():
    channel = f'uds-{uuid.uuid4().hex}'
    tester_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester_session.start()
    ecu_session.start()
    server = UdsServer(slow_routines=(0x0F0F,))
    simulator = UdsEcuSimulator(ecu_session, server=server)
    simulator.start()
    transport = IsoTpClient(tester_session, 0x7E0, 0x7E8)
    yield UdsClient(transport), server
    transport.close()
    simulator.stop()
    tester_session.stop()
    ecu_session.stop()


def test_dtc_and_did_formatting():
    assert format_dtc(0x012345) == 'P0123-45'
    assert format_dtc(0xC10000) == 'U0100-00'
    assert format_dtc(0x4A1B02) == 'C0A1B-02'
    assert Dtc(0x012345, 0x09).status_flags == ['testFailed', 'confirmedDTC']
    assert format_did_value(0xF190, b'WDB123\x00\x00') == 'WDB123'
    assert format_did_value(0xF186, b'\x03') == 'extended'
    assert format_did_value(0x1234, b'\x01\x02') == '01 02'
    assert describe_payload(bytes([0x7F, 0x22, 0x31])) == 'Negative response to ReadDataByIdentifier: requestOutOfRange'
    assert describe_payload(bytes([0x22, 0xF1, 0x90])) == 'ReadDataByIdentifier F1 90'
    assert describe_payload(bytes([0x00])) == ''


async def test_sessions_reset_and_identifiers(ecu):
    client, server = ecu
    assert await client.diagnostic_session_control(0x03) == bytes([0x00, 0x32, 0x01, 0xF4])
    assert await client.read_data_by_identifier(0xF186) == b'\x03'
    assert await client.read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'
    with pytest.raises(UdsNegativeResponse) as error:
        await client.read_data_by_identifier(0x1234)
    assert error.value.code == 0x31 and 'requestOutOfRange' in str(error.value)
    await client.ecu_reset(0x01)
    assert server.session == 0x01


async def test_security_access_and_write(ecu, tmp_path):
    client, server = ecu
    with pytest.raises(UdsNegativeResponse, match='securityAccessDenied'):
        await client.write_data_by_identifier(0xF198, b'SHOP42')
    await client.diagnostic_session_control(0x03)
    with pytest.raises(UdsNegativeResponse, match='invalidKey'):
        await client.security_access(0x01, lambda level, seed: b'\x00\x00')
    key_file = tmp_path / 'key.py'
    key_file.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n')
    assert await client.security_access(0x01, load_key_function(key_file)) is True
    assert await client.security_access(0x01, xor_key) is False  # Zero seed: already unlocked
    await client.write_data_by_identifier(0xF198, b'SHOP42')
    assert server.dids[0xF198] == b'SHOP42'
    with pytest.raises(UdsError):
        await client.security_access(0x02, xor_key)


def test_invalid_key_files(tmp_path):
    (tmp_path / 'empty.py').write_text('x = 1\n')
    (tmp_path / 'broken.py').write_text('def compute_key(:\n')
    with pytest.raises(UdsError, match='does not define compute_key'):
        load_key_function(tmp_path / 'empty.py')
    with pytest.raises(UdsError, match='Error in key file'):
        load_key_function(tmp_path / 'broken.py')


async def test_dtcs(ecu):
    client, server = ecu
    availability, dtcs = await client.read_dtcs(0xFF)
    assert availability == 0xFF
    assert [(dtc.text, dtc.status) for dtc in dtcs] == [('P0123-45', 0x09), ('U0100-00', 0x08)]
    _, confirmed_failing = await client.read_dtcs(0x01)
    assert [dtc.text for dtc in confirmed_failing] == ['P0123-45']
    await client.clear_diagnostic_information()
    assert (await client.read_dtcs())[1] == []


async def test_routine_with_response_pending(ecu):
    client, _ = ecu
    assert await client.routine_control(0x01, 0x0F0F) == b'\x00'
    result = await client.request(bytes([0x31, 0x01, 0x0F, 0x0F]))
    assert result.positive


async def test_suppressed_responses_and_tester_present(ecu):
    client, server = ecu
    result = await client.request(bytes([0x3E, 0x80]))
    assert result.response is None and result.description == 'No response expected'
    keeper = SessionKeepAlive(client.transport, interval=0.05)
    keeper.start()
    await asyncio.sleep(0.22)
    keeper.stop()
    assert keeper.sent >= 4 and server.tester_present_count >= 4
    assert not keeper.running


async def test_unknown_service(ecu):
    client, _ = ecu
    result = await client.request(bytes([0x23, 0x00]))
    assert not result.positive and 'serviceNotSupported' in result.description


def test_passive_uds_decoder():
    decoder = UdsDecoder()
    request = can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes([0x03, 0x22, 0xF1, 0x90, 0, 0, 0, 0]))
    assert decoder.describe(request) == 'ISO-TP SF (3 B): 22 F1 90 | UDS ReadDataByIdentifier F1 90'
    negative = can.Message(arbitration_id=0x7E8, is_extended_id=False, data=bytes([0x03, 0x7F, 0x22, 0x31, 0, 0, 0, 0]))
    assert decoder.describe(negative).endswith('UDS Negative response to ReadDataByIdentifier: requestOutOfRange')
    flow_control = can.Message(arbitration_id=0x7E0, is_extended_id=False, data=bytes([0x30, 0, 0]))
    assert decoder.describe(flow_control) == 'ISO-TP FC CTS BS=0 STmin=0x00'
