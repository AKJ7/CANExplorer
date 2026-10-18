import asyncio
import uuid
from pathlib import Path

import can
import pytest

from can_explorer.core.canopen_tools import (
    NMT_START,
    STATE_OPERATIONAL,
    CanOpenNetwork,
    CanOpenNodeSimulator,
    ObjectDictionary,
    SdoAbort,
    SdoClient,
    decode_value,
    emcy_text,
    encode_value,
    format_value,
    nmt_message,
    parse_eds_int,
    pdo_mappings,
)
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource

EDS = Path(__file__).parents[3] / 'examples' / 'canopen' / 'io_module.eds'


def test_values():
    assert parse_eds_int('$NODEID+0x180', 5) == 0x185 and parse_eds_int('0x80+$NODEID', 1) == 0x81
    assert parse_eds_int('010') == 10 and parse_eds_int('-5') == -5 and parse_eds_int('') is None
    assert encode_value(0x03, '-350') == (-350).to_bytes(2, 'little', signed=True)
    assert decode_value(0x03, encode_value(0x03, -350)) == -350
    assert decode_value(0x08, encode_value(0x08, '36.5')) == 36.5
    assert decode_value(0x09, b'IO\x00') == 'IO' and encode_value(0x0A, '01 02') == b'\x01\x02'
    assert format_value(0x07, 0x401) == '1025 (0x401)' and format_value(0x05, 5) == '5'
    with pytest.raises(ValueError):
        encode_value(0x05, 256)
    with pytest.raises(ValueError):
        encode_value(0x06, 'twelve')
    assert (
        emcy_text(0x8130) == 'Life guard or heartbeat error'
        and emcy_text(0x4210) == 'Device temperature'
        and emcy_text(0x4000) == 'Temperature'
    )


def test_object_dictionary_and_pdos():
    od = ObjectDictionary.from_file(EDS)
    assert od.device_info['ProductName'] == 'IO module 8DI 8DO 2AI'
    assert od.entry(0x1018, 1).full_name == 'Identity object.Vendor-ID'
    assert od.find('Identity object.Serial number').subindex == 4
    assert od.find('producer heartbeat time').index == 0x1017
    compact = od.objects[0x6200]  # CompactSubObj
    assert [entry.name for entry in compact.entries.values()] == ['Number of entries', 'Entry 1']
    assert compact.entries[1].writable and not od.entry(0x1000).writable
    tpdo1, tpdo2 = [pdo for pdo in pdo_mappings(od, 5) if pdo.kind == 'TPDO']
    rpdo1 = [pdo for pdo in pdo_mappings(od, 5) if pdo.kind == 'RPDO'][0]
    assert (tpdo1.cob_id, tpdo1.valid, tpdo1.transmission_text, tpdo1.length) == (
        0x185,
        True,
        'event driven, 100 ms',
        5,
    )
    assert [variable.name for variable in tpdo1.variables] == [
        'Read input 8-bit.Digital inputs 1-8',
        'Read analogue input 16-bit.Analogue input 1',
        'Read analogue input 16-bit.Analogue input 2',
    ]
    assert tpdo1.decode(bytes([0x05, 0xB0, 0x04, 0xA2, 0xFE])) == [
        ('Read input 8-bit.Digital inputs 1-8', '5'),
        ('Read analogue input 16-bit.Analogue input 1', '1200'),
        ('Read analogue input 16-bit.Analogue input 2', '-350'),
    ]
    assert tpdo2.transmission_text == 'every SYNC' and tpdo2.variables[0].data_type == 0x08
    assert rpdo1.cob_id == 0x205 and rpdo1.variables[0].name == 'Write output 8-bit.Entry 1'
    od.overrides[(0x1800, 1)] = 0x80000185  # Disabled, as read from the node
    assert not pdo_mappings(od, 5)[1].valid


def test_bad_files(tmp_path):
    path = tmp_path / 'empty.eds'
    path.write_text('[FileInfo]\nFileName=empty.eds\n')
    with pytest.raises(ValueError, match='has no objects'):
        ObjectDictionary.from_file(path)


def test_network_tracking():
    network = CanOpenNetwork()
    for can_id, data in ((0x705, [0]), (0x705, [0x7F]), (0x085, [0x30, 0x81, 0x11, 0, 0, 0, 0, 0]), (0x185, [1, 2])):
        network.on_message(can.Message(arbitration_id=can_id, is_extended_id=False, data=data, timestamp=1.0))
    node = network.nodes[5]
    assert (node.state_name, node.boot_ups, node.frames) == ('Pre-Operational', 1, 1)
    assert node.emcy[0][1:3] == (0x8130, 0x11) and network.pdo_data[0x185][1] == b'\x01\x02'
    assert bytes(nmt_message(NMT_START, 5).data) == b'\x01\x05'
    with pytest.raises(ValueError):
        nmt_message(0x33)


async def test_sdo_nmt_and_pdos_against_simulated_node():
    channel = f'canopen-{uuid.uuid4().hex}'
    master = BusSession(LiveSource(BusConfig('virtual', channel)))
    slave = BusSession(LiveSource(BusConfig('virtual', channel)))
    master.start()
    slave.start()
    od = ObjectDictionary.from_file(EDS)
    node = CanOpenNodeSimulator(slave, 5, od)
    node.start()
    network = CanOpenNetwork()
    master.add_message_listener(network.on_message)
    client = SdoClient(master, 5)
    try:
        assert await client.upload(0x1018, 1) == (0xA55).to_bytes(4, 'little')  # Expedited
        assert (await client.upload(0x1008, 0)).decode() == 'IO module 8DI 8DO 2AI'  # Segmented
        await client.download(0x2000, 0, b'A longer note for the segmented download')
        assert node.values[(0x2000, 0)] == b'A longer note for the segmented download'
        assert await client.upload(0x2000, 0) == b'A longer note for the segmented download'
        await client.download(0x1017, 0, (100).to_bytes(2, 'little'))
        with pytest.raises(SdoAbort, match='write a read only object'):
            await client.download(0x1000, 0, bytes(4))
        with pytest.raises(SdoAbort, match='does not exist'):
            await client.upload(0x3000, 0)
        with pytest.raises(SdoAbort, match='Sub-index does not exist'):
            await client.upload(0x1018, 9)
        master.send(nmt_message(NMT_START, 5))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if network.nodes.get(5) and network.nodes[5].state == STATE_OPERATIONAL and 0x185 in network.pdo_data:
                break
        assert network.nodes[5].state == STATE_OPERATIONAL and node.state == STATE_OPERATIONAL
        assert network.pdo_data[0x185][1] == bytes([0x05, 0xB0, 0x04, 0xA2, 0xFE])
        assert 0x285 not in network.pdo_data  # TPDO2 is synchronous
        master.send(can.Message(arbitration_id=0x080, is_extended_id=False, data=b''))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if 0x285 in network.pdo_data:
                break
        assert network.pdo_data[0x285][1] == encode_value(0x08, 36.5)
        with pytest.raises(TimeoutError):
            await SdoClient(master, 6, timeout=0.1).upload(0x1000, 0)
    finally:
        node.stop()
        master.stop()
        slave.stop()
