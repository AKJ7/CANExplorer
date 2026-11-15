import uuid

import can
import pytest

from can_explorer.core import uds_dtc, uds_events, uds_file_transfer, uds_functional, uds_periodic
from can_explorer.core.isotp_client import IsoTpClient, IsoTpError
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import (
    TIMING_MARGIN,
    SessionTiming,
    UdsClient,
    UdsError,
    UdsNegativeResponse,
    describe_payload,
    describe_request,
    parse_scaling_data,
)
from tests.unit.waiting import wait_until


class ServerTransport:
    """Calls a UdsServer directly, without a bus; the server's event-triggered responses follow its answers."""

    def __init__(self, server):
        self.server = server
        self.responses = []

    async def send(self, payload):
        self.responses += self.server.handle(bytes(payload))
        self.responses += self.server.outbox
        self.server.outbox.clear()

    async def receive(self, timeout=None):
        if not self.responses:
            raise TimeoutError('No response')
        return self.responses.pop(0)


def client_of(server=None):
    server = server or UdsServer()
    return UdsClient(ServerTransport(server)), server


# Timing


async def test_session_timing_is_adopted_but_never_shortened():
    assert SessionTiming.parse(bytes([0x00, 0x32, 0x01, 0xF4])) == SessionTiming(0.05, 5.0)
    assert SessionTiming.parse(b'\x00') is None
    client, server = client_of()
    await client.diagnostic_session_control(0x03)
    # P2 50 ms + margin is shorter than the base 1 s: kept; P2* 5 s + margin is longer: adopted
    assert client.timeout == 1.0 and client.extended_timeout == 5.0 + TIMING_MARGIN
    assert client.server_timing.describe() == 'P2 50 ms, P2* 5000 ms'

    slow = UdsServer()
    slow._session_control = lambda request: [bytes([0x50, request[1], 0x0B, 0xB8, 0x0F, 0xA0])]  # 3 s, 40 s
    client, _ = client_of(slow)
    await client.diagnostic_session_control(0x02)
    assert client.timeout == 3.0 + TIMING_MARGIN and client.extended_timeout == 40.0 + TIMING_MARGIN

    fixed = UdsClient(ServerTransport(UdsServer()), adopt_timing=False)
    await fixed.diagnostic_session_control(0x03)
    assert fixed.extended_timeout == 5.0 and fixed.server_timing is not None
    assert describe_payload(bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])) == (
        'Positive response to DiagnosticSessionControl: extended, P2 50 ms, P2* 5000 ms'
    )


# Scaling, communication control, DTC setting, link control


async def test_scaling_communication_dtc_setting_and_link_control():
    scaling = parse_scaling_data(bytes([0x64, 0x01, 0x00, 0x02, 0xA1, 0x20]))
    assert [(item.type_name, item.length, item.extension) for item in scaling.scaling] == [
        ('unsigned numeric', 2, b''),
        ('unit/format', 1, b'\x20'),
    ]
    # Bit-mapped types: the extension layout is not decoded, the rest is kept raw
    bitmapped = parse_scaling_data(bytes([0x64, 0x01, 0x00, 0x31, 0xF0, 0x01]))
    assert bitmapped.undecoded == b'\xf0\x01' and 'not decoded' in bitmapped.describe()

    client, server = client_of()
    assert (await client.read_scaling_data_by_identifier(0x0100)).scaling[0].length == 2
    with pytest.raises(UdsNegativeResponse, match='serviceNotSupportedInActiveSession'):
        await client.communication_control(0x03, 0x01)
    await client.diagnostic_session_control(0x03)
    await client.communication_control(0x03, 0x01)
    await client.control_dtc_setting(False)
    assert server.communication == {'control': 0x03, 'type': 0x01} and not server.dtc_setting_on
    with pytest.raises(UdsError, match='node identification number'):
        await client.communication_control(0x04)
    await client.communication_control(0x05, 0x01, node=0x1234)
    await client.diagnostic_session_control(0x01)  # The default session ends both
    assert server.communication['control'] == 0x00 and server.dtc_setting_on

    await client.link_control(0x01, mode=0x12)
    await client.link_control(0x03, suppress=True)
    assert server.link_mode == 0x12 and server.link_transitions == 1
    await client.link_control(0x02, baudrate=666_666)
    assert server.link_mode == 666_666
    with pytest.raises(UdsError, match='baud rate'):
        await client.link_control(0x02)
    assert describe_request(bytes([0x28, 0x83, 0x01])) == 'CommunicationControl 83 01 (disableRxAndTx)'
    assert describe_request(bytes([0x85, 0x02])) == 'ControlDTCSetting 02 (off)'
    assert describe_request(bytes([0x84, 0x00])).endswith('(secured: content not decoded)')


# ReadDTCInformation


def test_dtc_report_parsing():
    count = uds_dtc.parse_count(bytes([0x59, 0x01, 0xFF, 0x01, 0x00, 0x02]))
    assert (count.count, count.format_name) == (2, 'ISO 14229-1 DTC format')
    listed = uds_dtc.parse_list(bytes([0x59, 0x0A, 0xFF, 0x01, 0x23, 0x45, 0x09]), 0x0A)
    assert listed.dtcs[0].text == 'P0123-45' and listed.dtcs[0].status_flags == ['testFailed', 'confirmedDTC']
    with pytest.raises(UdsError, match='not a multiple of 4'):
        uds_dtc.parse_list(bytes([0x59, 0x02, 0xFF, 0x01, 0x23]), 0x02)
    counters = uds_dtc.parse_list(bytes([0x59, 0x14, 0x01, 0x23, 0x45, 0xEC]), 0x14)
    assert counters.dtcs[0].fault_detection_counter == -20
    # WWH-OBD by mask: severity, DTC, status records (5 bytes); permanent: DTC and status records (4 bytes)
    wwh = uds_dtc.parse_list(bytes([0x59, 0x42, 0x33, 0xFF, 0xE0, 0x04, 0x40, 0x01, 0x23, 0x45, 0x08]), 0x42)
    assert (wwh.functional_group, wwh.dtcs[0].severity, wwh.dtcs[0].status, wwh.format) == (0x33, 0x40, 0x08, 4)
    permanent = uds_dtc.parse_list(bytes([0x59, 0x55, 0x33, 0xFF, 0x04, 0x01, 0x23, 0x45, 0x08]), 0x55)
    assert permanent.dtcs[0].code == 0x012345

    snapshot = bytes([0x59, 0x04, 0x01, 0x23, 0x45, 0x09, 0x01, 0x02, 0x01, 0x00, 0x0B, 0xB8, 0x01, 0x01, 0x5A])
    # Without DID lengths the record stays undecoded instead of being split at a guess
    unknown = uds_dtc.parse_snapshots(snapshot)
    assert unknown.snapshots[0].values == () and unknown.snapshots[0].undecoded == snapshot[8:]
    known = uds_dtc.parse_snapshots(snapshot, did_length={0x0100: 2, 0x0101: 1}.get)
    assert [(v.did, v.data) for v in known.snapshots[0].values] == [(0x0100, b'\x0b\xb8'), (0x0101, b'\x5a')]
    one = uds_dtc.parse_snapshots(
        bytes([0x59, 0x04, 0x01, 0x23, 0x45, 0x09, 0x02, 0x01, 0x01, 0x00, 0xAA]), single_record=True
    )
    assert one.snapshots[0].values[0].data == b'\xaa'

    extended = bytes([0x59, 0x06, 0x01, 0x23, 0x45, 0x09, 0x01, 0x03, 0x02, 0x10, 0x20])
    assert uds_dtc.parse_extended_data(extended).undecoded == extended[6:]
    split = uds_dtc.parse_extended_data(extended, record_lengths={0x01: 1, 0x02: 2})
    assert [(r.number, r.data) for r in split.extended_data] == [(0x01, b'\x03'), (0x02, b'\x10\x20')]
    with pytest.raises(UdsError, match='needs a DTC'):
        uds_dtc.request(uds_dtc.REPORT_SNAPSHOT_BY_DTC)
    assert describe_request(bytes([0x19, 0x04, 0x01, 0x23, 0x45, 0xFF])).endswith(
        '(reportDTCSnapshotRecordByDTCNumber)'
    )


class Lengths:
    """A stand-in for a DiagnosticDescription knowing two DIDs."""

    def byte_length(self, did):
        return {0x0100: 2, 0x0101: 1}.get(did)

    def describe(self, did, data):
        return f'did {did:04X}={data.hex()}'


async def test_dtc_reports_against_the_simulated_ecu():
    client, server = client_of()
    assert (await client.read_dtc_information(0x01, status_mask=0x08)).count == 2
    assert (await client.read_dtc_information(0x07, severity_mask=0x40, status_mask=0xFF)).count == 1
    supported = await client.read_dtc_information(0x0A)
    assert [dtc.text for dtc in supported.dtcs] == ['P0123-45', 'U0100-00']
    identification = await client.read_dtc_information(0x03)
    assert [(d.code, d.record_number) for d in identification.dtcs] == [(0x012345, 0x01), (0x012345, 0x02)]
    details = await client.read_dtc_information(0x04, dtc=0x012345, description=Lengths())
    assert [len(s.values) for s in details.snapshots] == [2, 1]
    assert details.snapshots[0].values[0].describe() == 'did 0100=0bb8'
    extended = await client.read_dtc_information(0x06, dtc=0x012345, record_number=0x02)
    assert extended.extended_data[0].data == b'\x10\x20'
    counters = await client.read_dtc_information(0x14)
    assert {d.code: d.fault_detection_counter for d in counters.dtcs} == {0x012345: 127, 0xC10000: -20}
    memory = await client.read_dtc_information(0x17, status_mask=0xFF, memory_selection=0x01)
    assert memory.memory_selection == 0x01 and memory.dtcs[0].code == 0x9A0011
    wwh = await client.read_dtc_information(0x42, status_mask=0xFF, severity_mask=0xFF)
    assert len(wwh.dtcs) == 2 and wwh.functional_group == 0x33
    assert (await client.read_dtc_information(0x55)).dtcs[0].code == 0x012345
    with pytest.raises(UdsNegativeResponse, match='requestOutOfRange'):
        await client.read_dtc_information(0x04, dtc=0x111111)


# Dynamically defined and periodic data


async def test_dynamically_defined_and_periodic_data_over_isotp():
    channel = f'periodic-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu_session.start()
    server = UdsServer(dids={0xF190: b'WDB1234567890ABCD', 0x0100: b'\x0b\xb8\x5a'})
    server.memory[0x1000] = b'\x01\x02\x03\x04'
    ecu = UdsEcuSimulator(ecu_session, server=server, periodic_id=0x5E8)
    ecu.start()
    transport = IsoTpClient(tester, 0x7E0, 0x7E8)
    client = UdsClient(transport)
    receiver = uds_periodic.PeriodicDataReceiver(lengths={0x01: 3})
    receiver.attach(tester, 0x5E8)
    try:
        await client.diagnostic_session_control(0x03)
        await client.dynamically_define_by_identifier(0xF201, [(0x0100, 1, 2), (0xF190, 1, 1)])
        await client.dynamically_define_by_memory_address(0xF202, [(0x1002, 2)], address_bytes=2, size_bytes=1)
        assert await client.read_data_by_identifier(0xF201) == b'\x0b\xb8W'
        assert await client.read_data_by_identifier(0xF202) == b'\x03\x04'
        await client.read_data_by_periodic_identifier(uds_periodic.SEND_AT_FAST_RATE, [0xF201])
        await wait_until(lambda: len(receiver.received) >= 3)
        data = receiver.latest[0x01]
        # The UUDT frame carries padding-free data here; the length trims anyway
        assert (data.did, data.data, data.source) == (0xF201, b'\x0b\xb8W', 'uudt')
        await client.read_data_by_periodic_identifier(uds_periodic.STOP_SENDING)
        assert server.periodic == {}
        await client.clear_dynamically_defined(0xF201)
        assert 0xF201 not in server.dynamic_dids
        with pytest.raises(UdsError, match='needs at least one'):
            await client.read_data_by_periodic_identifier(uds_periodic.SEND_AT_SLOW_RATE)
        with pytest.raises(UdsError, match='not a periodic'):
            uds_periodic.periodic_id(0x1234)
    finally:
        receiver.close()
        transport.close()
        ecu.stop()
        tester.stop()
        ecu_session.stop()


async def test_periodic_data_on_the_diagnostic_channel():
    server = UdsServer(dids={0xF203: b'\x42'})
    client, _ = client_of(server)
    receiver = uds_periodic.PeriodicDataReceiver()
    receiver.listen_to(client)
    await client.diagnostic_session_control(0x03)
    await client.read_data_by_periodic_identifier(uds_periodic.SEND_AT_SLOW_RATE, [0x03])
    client.transport.responses.append(bytes([0x6A, 0x03, 0x42]))  # As an older ECU sends it
    await client.read_data_by_identifier(0xF203)  # The periodic message arrives first and goes to the receiver
    assert receiver.latest[0x03].data == b'\x42' and receiver.latest[0x03].source == 'usdt'
    receiver.close()


# File transfer


async def test_file_transfer():
    server = UdsServer(max_block_length=0x12)
    client, _ = client_of(server)
    content = bytes(range(100))
    progress = []
    await client.write_file('/data/map.bin', content, progress=lambda done, total: progress.append(done))
    assert server.files['/data/map.bin'] == content and progress[-1] == 100
    with pytest.raises(UdsNegativeResponse, match='conditionsNotCorrect'):
        await client.write_file('/data/map.bin', b'x')  # addFile of an existing file
    await client.write_file('/data/map.bin', b'new', replace=True)
    assert await client.read_file('/data/map.bin') == b'new'
    assert await client.read_file('/logs/trip.txt') == b'trip 1\n'
    assert await client.read_directory('/data') == b'/data/map.bin'
    await client.delete_file('/data/map.bin')
    assert '/data/map.bin' not in server.files
    request = uds_file_transfer.file_transfer_request(uds_file_transfer.ADD_FILE, '/a', size=0x1234)
    assert request == bytes([0x38, 0x01, 0x00, 0x02]) + b'/a' + bytes([0x00, 0x02, 0x12, 0x34, 0x12, 0x34])
    parsed = uds_file_transfer.parse_file_transfer_response(
        bytes([0x78, 0x04, 0x02, 0x04, 0x02, 0x00, 0x00, 0x02, 0x00, 0x10, 0x00, 0x08])
    )
    assert (parsed.max_block_length, parsed.file_size, parsed.compressed_size) == (0x402, 16, 8)
    with pytest.raises(UdsError, match='ASCII'):
        uds_file_transfer.file_transfer_request(uds_file_transfer.READ_FILE, '/ä')


# ResponseOnEvent


async def test_response_on_event():
    client, server = client_of()
    events = uds_events.EventCollector(client)
    setup = await client.response_on_event(
        uds_events.ON_CHANGE_OF_DATA_IDENTIFIER,
        event_record=uds_events.data_identifier_record(0xF198),
        service_to_respond=bytes([0x22, 0xF1, 0x98]),
    )
    assert setup.window_time == uds_events.INFINITE and setup.rest == bytes([0xF1, 0x98, 0x22, 0xF1, 0x98])
    await client.response_on_event(
        uds_events.ON_DTC_STATUS_CHANGE,
        event_record=uds_events.dtc_status_record(0x08),
        service_to_respond=bytes([0x19, 0x02, 0x08]),
    )
    started = await client.response_on_event(uds_events.START)
    assert started.identified_events == 2
    await client.diagnostic_session_control(0x03)
    await client.security_access(0x01, lambda level, seed: bytes(b ^ 0xFF for b in seed))
    await client.write_data_by_identifier(0xF198, b'SHOP42')  # The event response follows the write's answer
    server.set_dtc_status(0x777777, 0x08)
    client.transport.responses += server.outbox
    server.outbox.clear()
    await client.listen(0.01)
    payloads = [event.payload for event in events.events]
    assert payloads[0] == bytes([0x62, 0xF1, 0x98]) + b'SHOP42'
    assert payloads[1][:2] == bytes([0x59, 0x02]) and bytes([0x77, 0x77, 0x77, 0x08]) in payloads[1]
    activated = await client.response_on_event(uds_events.REPORT_ACTIVATED_EVENTS)
    assert activated.identified_events == 2 and activated.window_time is None
    await client.response_on_event(uds_events.CLEAR)
    assert server.events == []
    events.close()
    record = uds_events.comparison_record(0x0100, '>', 3000, hysteresis=10, length=16)
    assert record == bytes([0x01, 0x00, 0x02, 0x00, 0x00, 0x0B, 0xB8, 0x0A, 0x40, 0x00])
    with pytest.raises(UdsError, match='service to respond to'):
        uds_events.roe_request(uds_events.ON_DTC_STATUS_CHANGE, event_record=b'\x08')


# Functional addressing


@pytest.fixture
async def two_ecus():
    channel = f'functional-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu_session.start()
    engine = UdsServer(dids={0xF190: b'ENGINE-VIN-123456', 0xF18C: b'E1'})
    brakes = UdsServer(dids={0xF18C: b'B2'}, dtcs=[(0xC12300, 0x09)])
    ecus = [
        UdsEcuSimulator(ecu_session, 0x7E0, 0x7E8, server=engine),
        UdsEcuSimulator(ecu_session, 0x7E1, 0x7E9, server=brakes),
    ]
    for ecu in ecus:
        ecu.start()
    transport = uds_functional.obd_11bit(tester)
    yield uds_functional.FunctionalUdsClient(transport, timeout=0.3), engine, brakes, tester
    transport.close()
    for ecu in ecus:
        ecu.stop()
    tester.stop()
    ecu_session.stop()


async def test_functional_requests_collect_every_ecu(two_ecus):
    functional, engine, brakes, _ = two_ecus
    assert await functional.discover() == [0x7E8, 0x7E9]
    assert await functional.read_data_by_identifier(0xF18C) == {0x7E8: b'E1', 0x7E9: b'B2'}
    # A segmented response (the VIN) is reassembled with flow control to that ECU's physical id
    assert await functional.read_data_by_identifier(0xF190) == {0x7E8: b'ENGINE-VIN-123456'}
    dtcs = await functional.read_dtcs()
    assert [d.text for d in dtcs[0x7E8]] == ['P0123-45', 'U0100-00'] and dtcs[0x7E9][0].code == 0xC12300
    # Suppressed: no positive responses; serviceNotSupported is not sent to functional requests either
    results = await functional.diagnostic_session_control(0x03)
    assert results.results == {} and engine.session == brakes.session == 0x03
    await functional.control_dtc_setting(False)
    await functional.communication_control(0x03, 0x01)
    assert not engine.dtc_setting_on and brakes.communication['control'] == 0x03
    assert (await functional.request(bytes([0x2F, 0x01, 0x00, 0x00]))).results == {}
    with pytest.raises(IsoTpError, match='must fit one frame'):
        await functional.request(bytes(8))


def test_functional_address_conventions():
    session = type('Session', (), {'add_message_listener': lambda self, f: None})()
    transport = uds_functional.obd_29bit(session)
    assert transport.functional_id == 0x18DB33F1
    assert transport._response_to_request(0x18DAF110) == 0x18DA10F1
    assert transport._response_to_request(0x18DA10F1) is None
    mapped = uds_functional.mapped(session, 0x700, {0x781: 0x701})
    assert mapped._response_to_request(0x781) == 0x701 and mapped._response_to_request(0x782) is None


async def test_unsolicited_responses_are_not_lost():
    client, server = client_of()
    seen = []
    client.add_unsolicited_listener(seen.append)
    client.transport.responses.append(bytes([0x62, 0x12, 0x34, 0x00]))  # Stale answer to an earlier request
    await client.read_data_by_identifier(0xF190)
    assert seen == [bytes([0x62, 0x12, 0x34, 0x00])]
    client.remove_unsolicited_listener(seen.append)


def test_isotp_feed_and_payload_callback():
    session = type('Session', (), {'add_message_listener': lambda self, f: pytest.fail('must not listen')})()
    payloads = []
    client = IsoTpClient(session, 0x7E0, 0x7E8, listen=False, on_payload=payloads.append)
    client.feed(can.Message(arbitration_id=0x7E8, is_extended_id=False, data=[0x02, 0x50, 0x03], is_rx=True))
    assert payloads == [b'\x50\x03']


# Shared ISO-TP endpoint and periodic helpers


async def test_shared_isotp_routes_to_the_newest_receiving_user():
    channel = f'shared-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu_session.start()
    server = UdsServer()
    ecu = UdsEcuSimulator(ecu_session, server=server)
    ecu.start()
    idle = []
    from can_explorer.core.uds_shared import SharedIsoTp

    shared = SharedIsoTp(tester, 0x7E0, 0x7E8, on_idle=idle.append)
    try:
        keep_alive = shared.transport(receiving=False)
        with shared.transport() as transport:
            assert shared.busy
            client = UdsClient(transport)
            assert await client.read_data_by_identifier(0xF190) == b'WDB1234567890ABCD'  # Segmented
        assert not shared.busy
        await keep_alive.send(bytes([0x3E, 0x00]))  # Answered while nobody receives: goes to on_idle
        await wait_until(lambda: idle)
        assert idle == [bytes([0x7E, 0x00])]
        keep_alive.close()
        assert shared.matches(tester, 0x7E0, 0x7E8, False, False) and not shared.matches(
            tester, 0x7E1, 0x7E8, False, False
        )
    finally:
        shared.close()
        ecu.stop()
        tester.stop()
        ecu_session.stop()


def test_periodic_helpers():
    assert uds_periodic.parse_sources('0100:1:2, F190:1:4') == (
        'did',
        [uds_periodic.SourceElement(0x0100, 1, 2), uds_periodic.SourceElement(0xF190, 1, 4)],
    )
    assert uds_periodic.parse_sources('@20001000:4 @10:2') == ('memory', [(0x20001000, 4), (0x10, 2)])
    for bad, message in (('', 'Give the sources'), ('0100:1', 'expected'), ('0100:1:2 @10:2', 'not both')):
        with pytest.raises(UdsError, match=message):
            uds_periodic.parse_sources(bad)
    receiver = uds_periodic.PeriodicDataReceiver()
    for timestamp in (10.0, 10.1, 10.2):
        receiver.feed_frame(can.Message(timestamp=timestamp, arbitration_id=0x5E8, data=[0x01, 0xAA]))
    assert receiver.counts[0x01] == 3 and abs(receiver.rate(0xF201) - 10.0) < 0.01
    assert receiver.rate(0x02) == 0.0
    receiver.clear()
    assert receiver.latest == {} and receiver.counts == {}
    # The positive response to 0x2A is 0x6A alone; 0x6A with data is a periodic message, not the answer
    from can_explorer.core.uds import _is_response_to

    assert _is_response_to(bytes([0x2A, 0x03, 0x01]), b'\x6a')
    assert not _is_response_to(bytes([0x2A, 0x03, 0x01]), b'\x6a\x01\xaa')
