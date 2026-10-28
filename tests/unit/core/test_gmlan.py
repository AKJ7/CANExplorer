import uuid

import pytest

from can_explorer.core.gmlan import (
    GmlanClient,
    GmlanDecoder,
    GmlanDtc,
    GmlanError,
    GmlanNegativeResponse,
    describe_request,
    describe_response,
    format_dtc,
)
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import GmlanEcuSimulator, GmlanServer, xor_key
from can_explorer.core.sources import BusConfig, LiveSource


@pytest.fixture
async def ecu():
    channel = f'gmlan-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel, bitrate=33_333, single_wire=True)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel, bitrate=33_333, single_wire=True)))
    tester.start()
    ecu_session.start()
    server = GmlanServer()
    simulator = GmlanEcuSimulator(ecu_session, server=server)
    simulator.start()
    transport = IsoTpClient(tester, 0x241, 0x641)
    yield GmlanClient(transport), server
    transport.close()
    simulator.stop()
    tester.stop()
    ecu_session.stop()


def test_formatting_and_descriptions():
    assert format_dtc(0x0123, 0x04) == 'P0123-04'
    assert format_dtc(0xC100, 0x08) == 'U0100-08'
    assert GmlanDtc(0x0123, 0x04, 0x60).text == 'P0123-04'
    assert describe_request(bytes([0x1A, 0x90])) == 'ReadDataByIdentifier 90'
    assert describe_response(bytes([0x5A, 0x90, 0x41])) == 'ReadDataByIdentifier response 90 41'
    assert (
        describe_response(bytes([0x7F, 0x1A, 0x31]))
        == 'Negative response to ReadDataByIdentifier: requestOutOfRange (0x31)'
    )


async def test_read_write_identifiers(ecu):
    client, server = ecu
    assert await client.read_data_by_identifier(0x90) == b'W0L0000000000000'
    await client.write_data_by_identifier(0x90, b'TESTVIN0000000001')
    assert server.dids[0x90] == b'TESTVIN0000000001'
    assert await client.read_data_by_identifier(0x90) == b'TESTVIN0000000001'
    with pytest.raises(GmlanNegativeResponse) as info:
        await client.read_data_by_identifier(0x77)  # Unknown DID
    assert info.value.code == 0x31


async def test_modes_and_tester_present(ecu):
    client, server = ecu
    await client.initiate_diagnostic_operation(0x02)
    await client.disable_normal_communication()
    assert server.normal_communication is False
    await client.tester_present()
    assert server.tester_present_count == 1
    await client.return_to_normal_mode()
    assert server.normal_communication is True
    assert await client.report_programmed_state() == 0x00


async def test_security_access(ecu):
    client, server = ecu
    assert await client.security_access(0x01, xor_key) is True
    assert server.unlocked_level == 0x02
    assert await client.security_access(0x01, xor_key) is False  # Already unlocked -> zero seed
    with pytest.raises(GmlanError):
        await client.security_access(0x02, xor_key)  # Even levels are keys, not seed requests


async def test_dtcs(ecu):
    client, server = ecu
    dtcs = await client.read_dtcs()
    assert [dtc.text for dtc in dtcs] == ['P0123-04', 'U0100-08']
    await client.clear_dtcs()
    assert server.dtcs == []
    assert await client.read_dtcs() == []


async def test_raw_request_and_decoder(ecu):
    client, _ = ecu
    result = await client.request(bytes([0x1A, 0x90]))
    assert result.positive and result.response[:2] == bytes([0x5A, 0x90])
    assert result.description.startswith('ReadDataByIdentifier response')
    result = await client.request(bytes([0x01]))  # Unsupported service
    assert not result.positive and 'serviceNotSupported' in result.description

    decoder = GmlanDecoder(0x241, 0x641)
    import can

    request = can.Message(arbitration_id=0x241, data=bytes([0x02, 0x1A, 0x90, 0, 0, 0, 0, 0]))
    response = can.Message(arbitration_id=0x641, data=bytes([0x03, 0x5A, 0x90, 0x41, 0, 0, 0, 0]))
    assert decoder.describe(request) == 'GMLAN → ReadDataByIdentifier 90'
    assert decoder.describe(response).startswith('GMLAN ← ReadDataByIdentifier response')
    assert decoder.describe(can.Message(arbitration_id=0x123, data=b'\x00')) is None
