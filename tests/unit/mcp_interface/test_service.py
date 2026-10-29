import asyncio
import uuid

import can
import pytest

from can_explorer.core.reverse.obd import response as obd_response
from can_explorer.core.reverse.synthetic import Behaviour, generate
from can_explorer.core.decoding import open_signal_database
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.mcp.service import CanService, SendNotAllowed, _parse_id


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def channel():
    return f'mcp-{uuid.uuid4().hex}'


def service(channel, **options):
    source = LiveSource(BusConfig('virtual', channel, bitrate=500_000))
    svc = CanService(source, **options)
    svc.start()
    return svc


def service_unstarted(channel, **options):
    """A service whose trace is filled by dispatching frames directly (no bus), keeping their timestamps."""
    return CanService(LiveSource(BusConfig('virtual', channel, bitrate=500_000)), **options)


def test_parse_id():
    assert _parse_id('1A0') == 0x1A0 and _parse_id('0x1a0') == 0x1A0 and _parse_id(42) == 42
    with pytest.raises(ValueError):
        _parse_id('ZZ')


async def test_status_and_identifiers(channel, data_dir):
    svc = service(channel, database=open_signal_database(data_dir / 'example.dbc'))
    try:
        peer = can.Bus(interface='virtual', channel=channel)
        for i in range(10):
            peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([i, 0x27, 0, 0, 0, 0, 0, 0])))
        peer.send(can.Message(arbitration_id=0x200, is_extended_id=False, data=bytes(3)))
        await wait_until(lambda: svc.session.trace.total == 11)
        await asyncio.sleep(0.05)

        status = svc.status()
        assert status['open'] and status['frames'] == 11 and status['identifiers'] == 2
        assert status['protocol'] == 'Raw' and status['database'].endswith('example.dbc')
        assert status['sending_allowed'] is False and status['error_frames'] == 0

        ids = svc.identifiers()
        assert [row['id'] for row in ids] == ['100', '200']
        engine = ids[0]
        assert engine['name'] == 'EngineData' and engine['count'] == 10 and engine['direction'] == 'rx'
        assert engine['cycle_ms'] is not None
        assert svc.identifiers(limit=1) == ids[:1]
        peer.shutdown()
    finally:
        svc.stop()


async def test_frames_and_decode(channel, data_dir):
    svc = service(channel, database=open_signal_database(data_dir / 'example.dbc'))
    try:
        peer = can.Bus(interface='virtual', channel=channel)
        peer.send(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes([0xA0, 0x0F, 90, 3, 0, 0, 0, 0])))
        await wait_until(lambda: svc.session.trace.total == 1)
        frames = svc.frames(limit=10)
        assert len(frames) == 1
        frame = frames[0]
        assert frame['id'] == '100' and frame['dlc'] == 8 and frame['rx'] is True
        assert frame['signals']['EngineSpeed'] == 1000.0  # 0x0FA0 * 0.25

        # decode_frame does not need traffic and does not transmit
        decoded = svc.decode_frame('100', 'A0 0F 5A 03')
        assert decoded['name'] == 'EngineData' and decoded['signals']['CoolantTemp'] == 50
        # Filter by identifier
        peer.send(can.Message(arbitration_id=0x200, is_extended_id=False, data=bytes(3)))
        await wait_until(lambda: svc.session.trace.total == 2)
        assert [f['id'] for f in svc.frames(identifier='200')] == ['200']
        peer.shutdown()
    finally:
        svc.stop()


async def test_decode_without_database(channel):
    svc = service(channel)
    try:
        assert svc.decode_frame('100', '00')['error'].startswith('No database')
        assert svc.load_database  # present
    finally:
        svc.stop()


async def test_load_database(channel, data_dir):
    svc = service(channel)
    try:
        result = svc.load_database(str(data_dir / 'example.dbc'))
        assert result['database'].endswith('example.dbc') and result['messages'] >= 2 and result['signals'] >= 2
        assert svc.status()['database'].endswith('example.dbc')
        with pytest.raises((FileNotFoundError, OSError, ValueError)):
            svc.load_database(str(data_dir / 'does_not_exist.dbc'))
    finally:
        svc.stop()


async def test_analyze(channel, data_dir):
    svc = service(channel)
    try:
        traffic = generate(
            open_signal_database(data_dir / 'reverse.dbc'),
            duration=30,
            seed=0,
            behaviours={
                'Powertrain.Checksum': Behaviour('checksum'),
                'Powertrain.Counter': Behaviour('counter'),
                'Powertrain.EngineSpeed': Behaviour('sine', period=20, amplitude=0.95),
            },
        )
        peer = can.Bus(interface='virtual', channel=channel)
        engine_frames = [m for m in traffic.messages if m.arbitration_id == 0x123]
        for message in engine_frames:
            peer.send(
                can.Message(arbitration_id=message.arbitration_id, is_extended_id=False, data=bytes(message.data))
            )
        await wait_until(lambda: svc.session.trace.total >= len(engine_frames))
        result = svc.analyze('123')
        assert result['frames'] >= 100 and result['note'] is None
        kinds = {field['field']: field['kind'] for field in result['fields']}
        assert kinds.get('123[0|8@1+]') == 'checksum'
        assert kinds.get('123[8|4@1+]') == 'counter'
        assert any(field['kind'] == 'physical' for field in result['fields'])
        # No constant fields unless asked
        assert all(field['kind'] != 'constant' for field in result['fields'])
        assert any(f['kind'] == 'constant' for f in svc.analyze('123', include_constant=True)['fields'])
        assert svc.analyze('7FF')['note'] == 'No frames for this identifier'
        peer.shutdown()
    finally:
        svc.stop()


async def _play(channel, svc, data_dir, seed=0):
    """Feed synthetic traffic of reverse.dbc (checksum, counter, Intel/Motorola values, multiplexer) onto the bus."""
    traffic = generate(
        open_signal_database(data_dir / 'reverse.dbc'),
        duration=40,
        seed=seed,
        behaviours={
            'Powertrain.Checksum': Behaviour('checksum'),
            'Powertrain.Counter': Behaviour('counter'),
            'Powertrain.EngineSpeed': Behaviour('sine', period=20, amplitude=0.95),
            'Powertrain.Torque': Behaviour('sine', period=13, amplitude=0.95),
            'Body.VehicleSpeed': Behaviour('sine', period=27, amplitude=0.95),
        },
    )
    peer = can.Bus(interface='virtual', channel=channel)
    for message in traffic.messages:
        peer.send(
            can.Message(
                arbitration_id=message.arbitration_id, is_extended_id=message.is_extended_id, data=bytes(message.data)
            )
        )
    await wait_until(lambda: svc.session.trace.total >= len(traffic.messages), timeout=10)
    return traffic, peer


def _dispatched_service(channel, data_dir, *, database=None, extra=None, seed=0):
    """A service whose trace is filled with synthetic traffic of reverse.dbc, with the synthetic timestamps kept
    (frames are dispatched directly, so a reference's times line up with the frames). `extra(message)` may yield
    additional frames to inject, e.g. an echo of a signal in another message."""
    svc = service_unstarted(channel, database=database)
    traffic = generate(
        open_signal_database(data_dir / 'reverse.dbc'),
        duration=40,
        seed=seed,
        behaviours={
            'Powertrain.Checksum': Behaviour('checksum'),
            'Powertrain.Counter': Behaviour('counter'),
            'Powertrain.EngineSpeed': Behaviour('sine', period=20, amplitude=0.95),
            'Powertrain.Torque': Behaviour('sine', period=13, amplitude=0.95),
            'Body.VehicleSpeed': Behaviour('sine', period=27, amplitude=0.95),
        },
    )
    for message in traffic.messages:
        svc.session._dispatch(message)
        if extra is not None:
            for echo in extra(message):
                svc.session._dispatch(echo)
    return svc, traffic


def test_correlate_with_a_database_signal(channel, data_dir):
    """The engine speed, echoed into another message (0x321), is found from the Powertrain.EngineSpeed signal; its
    own message 0x123 is excluded from the candidates."""

    def echo(message):
        if message.arbitration_id == 0x123:
            yield can.Message(
                timestamp=message.timestamp + 1e-4,
                arbitration_id=0x321,
                is_extended_id=False,
                data=bytes(message.data[2:4]) + bytes(6),
            )

    svc, _ = _dispatched_service(channel, data_dir, database=open_signal_database(data_dir / 'reverse.dbc'), extra=echo)
    result = svc.correlate(signal='Powertrain.EngineSpeed', max_lag=0.2)
    assert result['reference'] == 'Powertrain.EngineSpeed' and result['unit'] == 'rpm' and result['samples'] > 50
    best = result['matches'][0]
    assert best['id'] == '321' and best['field'] == '321[0|16@1+]' and best['correlation'] > 0.99
    assert all(match['id'] != '123' for match in result['matches'])  # The reference's own message is excluded
    assert svc.correlate(signal='Nope.Nope')['error'].startswith('No such signal')
    assert svc.correlate()['error'].startswith('Provide a reference')


def test_correlate_with_inline_series(channel, data_dir):
    svc, traffic = _dispatched_service(channel, data_dir)
    times = [t for t, _ in traffic.raw['Body.VehicleSpeed']]
    values = [raw * 0.01 for _, raw in traffic.raw['Body.VehicleSpeed']]
    result = svc.correlate(times=times, values=values, name='gps', unit='km/h', max_lag=0.2)
    best = result['matches'][0]
    assert best['field'] == '500[7|16@0+]' and best['correlation'] > 0.99 and abs(best['scale'] - 0.01) < 1e-4
    assert result['matches'][0]['lag_ms'] == 0.0
    assert svc.correlate(times=[1, 2], values=[1])['error']


def test_correlate_finds_a_non_byte_aligned_field(channel, data_dir):
    """Powertrain.Torque is a 12-bit signed Motorola field (39|12@0-); it is found only because correlate feeds
    the analysis's candidates, not just byte-aligned 8/16-bit ones."""
    svc, traffic = _dispatched_service(channel, data_dir)
    times = [t for t, _ in traffic.raw['Powertrain.Torque']]
    values = [raw * 0.5 for _, raw in traffic.raw['Powertrain.Torque']]
    result = svc.correlate(times=times, values=values, name='torque', unit='Nm', identifier='123', max_lag=0.2, top=12)
    match = next(m for m in result['matches'] if m['field'] == '123[39|12@0-]')  # The exact field, from the analysis
    assert match['correlation'] > 0.99 and abs(match['scale'] - 0.5) < 1e-3


async def test_promote_to_dbc(channel, data_dir, tmp_path):
    svc = service(channel)
    try:
        _, peer = await _play(channel, svc, data_dir)
        out = tmp_path / 'discovered.dbc'
        result = svc.promote_to_dbc(str(out), identifier='123')
        assert result['signals'] >= 3 and result['messages'] == 1 and out.is_file()

        # The written DBC decodes: the checksum, counter and engine-speed fields are there
        from can_explorer.core.decoding import open_signal_database as load

        database = load(out)
        message = next(m for m in database.messages() if m.frame_id == 0x123)
        names = {signal.name for signal in message.signals}
        assert any(name.startswith('Checksum_') for name in names)
        assert any(name.startswith('Counter_') for name in names)
        assert any(name.startswith('Value_') for name in names)

        # Promoting again extends the same file (all identifiers now)
        after = svc.promote_to_dbc(str(out))
        assert after['signals'] >= result['signals']
        assert svc.promote_to_dbc(str(tmp_path / 'x.arxml'))['error'].startswith('Cannot write')
        peer.shutdown()
    finally:
        svc.stop()


async def test_sending_is_gated(channel):
    svc = service(channel, allow_send=False)
    try:
        with pytest.raises(SendNotAllowed):
            svc.send_frame('100', '01 02 03')
        with pytest.raises(SendNotAllowed):
            await svc.query_obd(0x0D)
    finally:
        svc.stop()


async def test_send_frame(channel):
    svc = service(channel, allow_send=True)
    peer = can.Bus(interface='virtual', channel=channel)
    try:
        result = svc.send_frame('1A0', '01 02 03 04', extended=False)
        assert result == {'sent': True, 'id': '1A0', 'data': '01 02 03 04'}
        received = peer.recv(1.0)
        assert received is not None and received.arbitration_id == 0x1A0 and bytes(received.data) == bytes([1, 2, 3, 4])
    finally:
        peer.shutdown()
        svc.stop()


async def test_query_obd(channel):
    """An ECU answering the vehicle speed PID; the service sends the request and returns the decoded value."""
    svc = service(channel, allow_send=True)
    peer = can.Bus(interface='virtual', channel=channel)
    try:

        async def answer():
            while True:
                message = await asyncio.get_running_loop().run_in_executor(None, peer.recv, 0.2)
                if message is not None and message.arbitration_id == 0x7DF and message.data[2] == 0x0D:
                    peer.send(obd_response(0x0D, bytes([120])))  # 120 km/h
                    return

        answering = asyncio.ensure_future(answer())
        result = await svc.query_obd(0x0D, timeout=2.0)
        await answering
        assert result == {'pid': 0x0D, 'name': 'Vehicle speed', 'value': 120.0, 'unit': 'km/h', 'answered': True}

        # No answer -> not answered, not an exception
        missing = await svc.query_obd(0x05, timeout=0.2)
        assert missing['answered'] is False and missing['pid'] == 0x05
        with pytest.raises(ValueError):
            await svc.query_obd(0x99)
    finally:
        peer.shutdown()
        svc.stop()


def test_identifiers_cycle_ms_is_zero_not_none_for_equal_timestamps(channel):
    """A coarse clock (e.g. on Windows) can give frames identical timestamps; the cycle time is then 0.0, which
    must still be reported, not dropped as if it were unknown."""
    svc = service_unstarted(channel)
    for i in range(3):
        svc.session._dispatch(can.Message(timestamp=5.0, arbitration_id=0x100, is_extended_id=False, data=bytes([i])))
    row = svc.identifiers()[0]
    assert row['cycle_ms'] == 0.0
