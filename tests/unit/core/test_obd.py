import uuid

import pytest

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.obd import ECU_RESPONSE, FUNCTIONAL_REQUEST, ObdClient, ObdServer, decode_dtc
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient


def test_decode_dtc():
    assert decode_dtc(0x01, 0x33) == 'P0133'
    assert decode_dtc(0x04, 0x20) == 'P0420'
    assert decode_dtc(0xC1, 0x00) == 'U0100'
    assert decode_dtc(0x81, 0x23) == 'B0123'


@pytest.fixture
async def obd():
    channel = f'obd-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu.start()
    server = ObdServer()
    # The ECU listens on the functional request id and answers on the ECU id
    simulator = UdsEcuSimulator(ecu, request_id=FUNCTIONAL_REQUEST, response_id=ECU_RESPONSE, server=server)
    simulator.start()
    transport = IsoTpClient(tester, FUNCTIONAL_REQUEST, ECU_RESPONSE)
    yield ObdClient(UdsClient(transport)), server
    transport.close()
    simulator.stop()
    tester.stop()
    ecu.stop()


async def test_supported_pids_and_live_values(obd):
    client, _ = obd
    supported = await client.supported_pids()
    assert {0x04, 0x05, 0x0C, 0x0D, 0x11, 0x2F, 0x42}.issubset(supported)

    speed = await client.read_value(0x0D)
    assert speed.name == 'Vehicle speed' and speed.unit == 'km/h' and speed.value == 60
    rpm = await client.read_value(0x0C)
    assert rpm.value == 2500


async def test_status_and_dtcs_and_clear(obd):
    client, _ = obd
    status = await client.status()
    assert status.mil_on and status.dtc_count == 2

    assert [dtc.code for dtc in await client.read_dtcs()] == ['P0133', 'P0420']
    assert [dtc.code for dtc in await client.read_dtcs(pending=True)] == ['P0171']

    await client.clear_dtcs()
    assert await client.read_dtcs() == []
    assert not (await client.status()).mil_on


async def test_vin(obd):
    client, _ = obd
    assert await client.read_vin() == '1HGCM82633A004352'
