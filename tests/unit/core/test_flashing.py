import uuid

import pytest

from can_explorer.core.flashing import (
    FlashImage,
    FlashJob,
    FlashOptions,
    ImageError,
    Segment,
    SequenceError,
    default_sequence,
    intel_hex_text,
    load_encoder,
    load_sequence,
    load_image,
    parse_intel_hex,
    parse_srec,
)
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer, xor_key
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient, UdsNegativeResponse
from can_explorer.core.uds_functional import FunctionalUdsClient, obd_11bit

FIRMWARE = bytes((i * 7 + 3) & 0xFF for i in range(3000))


class ServerTransport:
    """Calls a UdsServer directly, without a bus."""

    def __init__(self, server):
        self.server = server
        self.responses = []

    async def send(self, payload):
        self.responses += self.server.handle(bytes(payload))

    async def receive(self, timeout=None):
        if not self.responses:
            raise TimeoutError('No response')
        return self.responses.pop(0)


def test_intel_hex():
    image = FlashImage([Segment(0x0800FFF0, FIRMWARE[:40]), Segment(0x08020000, FIRMWARE[:5])], 0x08000101)
    text = intel_hex_text(image)
    assert ':020000040800F2' in text and text.endswith(':00000001FF\n')
    parsed = parse_intel_hex(text)
    assert [(segment.address, segment.data) for segment in parsed.segments] == [
        (0x0800FFF0, FIRMWARE[:40]),
        (0x08020000, FIRMWARE[:5]),
    ]
    assert parsed.start_address == 0x08000101 and parsed.size == 45
    assert parsed.describe() == '45 bytes in 2 segments: 0x0800FFF0–0x08010017, 0x08020000–0x08020004'
    assert len(parse_intel_hex(text, fill_gap=0x20000).segments) == 1  # Gap filled with 0xFF
    with pytest.raises(ImageError, match='Line 2: checksum error'):
        parse_intel_hex(text.replace(text.splitlines()[1], text.splitlines()[1][:-2] + '00'))
    with pytest.raises(ImageError, match='Missing end of file'):
        parse_intel_hex(text.replace(':00000001FF', ''))
    segment_addressing = ':020000021000EC\n:0400000001020304F2\n:00000001FF\n'  # Type 02: address 0x10000
    assert parse_intel_hex(segment_addressing).segments[0].address == 0x10000


def srec_line(kind, address, data, address_size):
    body = bytes([address_size + len(data) + 1]) + address.to_bytes(address_size, 'big') + data
    return f'S{kind}' + (body + bytes([~sum(body) & 0xFF])).hex().upper()


def test_srec():
    text = '\n'.join(
        [
            srec_line(0, 0, b'app.s37', 2),
            srec_line(3, 0x00010000, FIRMWARE[:16], 4),
            srec_line(3, 0x00010010, FIRMWARE[16:32], 4),
            srec_line(1, 0x2000, b'\x01\x02', 2),
            srec_line(5, 3, b'', 2),
            srec_line(7, 0x00010000, b'', 4),
        ]
    )
    image = parse_srec(text)
    assert image.header == 'app.s37' and image.start_address == 0x10000
    assert [(segment.address, segment.data) for segment in image.segments] == [
        (0x2000, b'\x01\x02'),
        (0x10000, FIRMWARE[:32]),
    ]
    with pytest.raises(ImageError, match='record count 2'):
        parse_srec(text.replace(srec_line(5, 3, b'', 2), srec_line(5, 2, b'', 2)))
    with pytest.raises(ImageError, match='checksum'):
        parse_srec(text[:-1] + ('0' if text[-1] != '0' else '1'))


def test_load_image(tmp_path):
    (tmp_path / 'app.bin').write_bytes(FIRMWARE)
    assert load_image(tmp_path / 'app.bin', base_address=0x8000).segments[0].address == 0x8000
    (tmp_path / 'app.dat').write_text(intel_hex_text(FlashImage([Segment(0x100, b'\xaa' * 4)])))
    assert load_image(tmp_path / 'app.dat').segments[0].data == b'\xaa' * 4  # Recognised by its content
    (tmp_path / 'empty.bin').write_bytes(b'')
    with pytest.raises(ImageError):
        load_image(tmp_path / 'empty.bin')


async def test_flash_job_against_server():
    server = UdsServer(max_block_length=0x82, download_requires_security=True)
    image = FlashImage([Segment(0x08000000, FIRMWARE), Segment(0x08010000, FIRMWARE[:100])])
    changes = []
    job = FlashJob(
        image,
        FlashOptions(security_level=1, compute_key=xor_key, check_routine=0xFF01),
        on_change=lambda job: changes.append(job.bytes_done),
    )
    assert [step.name for step in job.steps] == [
        'Programming session',
        'Security access level 1',
        'Erase 0x08000000 (3000 bytes)',
        'Download 0x08000000 (3000 bytes)',
        'Erase 0x08010000 (100 bytes)',
        'Download 0x08010000 (100 bytes)',
        'Check routine 0xFF01',
        'ECU reset',
    ]
    await job.run(UdsClient(ServerTransport(server)))
    assert server.memory == {0x08000000: FIRMWARE, 0x08010000: FIRMWARE[:100]}
    assert server.erased == [(0x08000000, 3000), (0x08010000, 100)]
    assert all(step.status == 'done' for step in job.steps) and job.progress == 1.0
    assert job.steps[3].detail == '3000 / 3000 bytes, blocks of 128'
    transfers = [request for request in server.requests if request[0] == 0x36]
    assert len(transfers) == 24 + 1 and [request[1] for request in transfers[:3]] == [1, 2, 3]
    assert changes[-1] == 3100 and server.session == 0x01  # Reset back to the default session

    # The block counter wraps from 0xFF to 0x00
    server = UdsServer(max_block_length=0x0A)
    await FlashJob(FlashImage([Segment(0, FIRMWARE[:2400])]), FlashOptions(erase_routine=None)).run(
        UdsClient(ServerTransport(server))
    )
    counters = [request[1] for request in server.requests if request[0] == 0x36]
    assert counters[254:258] == [0xFF, 0x00, 0x01, 0x02] and server.memory[0] == FIRMWARE[:2400]


async def test_flash_job_failures():
    server = UdsServer(download_requires_security=True)
    job = FlashJob(FlashImage([Segment(0x1000, FIRMWARE[:10])]), FlashOptions())
    with pytest.raises(UdsNegativeResponse, match='Security access denied|securityAccessDenied'):
        await job.run(UdsClient(ServerTransport(server)))
    assert [step.status for step in job.steps] == ['done', 'done', 'failed', 'skipped']
    assert job.steps[2].detail == job.error

    job = FlashJob(FlashImage([Segment(0x1000, FIRMWARE[:10])]), FlashOptions(programming_session=False))
    with pytest.raises(UdsNegativeResponse):
        await job.run(UdsClient(ServerTransport(UdsServer())))  # Download outside the programming session

    with pytest.raises(Exception, match='do not fit'):
        await FlashJob(FlashImage([Segment(0x1_0000_0000, b'\x00')]), FlashOptions(erase_routine=None)).run(
            UdsClient(ServerTransport(UdsServer()))
        )


async def test_flashing_over_isotp():
    channel = f'flash-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu_session.start()
    ecu = UdsEcuSimulator(ecu_session, server=UdsServer(max_block_length=0x802))
    ecu.start()
    transport = IsoTpClient(tester, 0x7E0, 0x7E8)
    try:
        job = FlashJob(FlashImage([Segment(0x4000, FIRMWARE * 3)]), FlashOptions())
        await job.run(UdsClient(transport))
        assert ecu.server.memory == {0x4000: FIRMWARE * 3} and job.rate > 0
        assert job.steps[2].detail == '9000 / 9000 bytes, blocks of 2048'
    finally:
        transport.close()
        ecu.stop()
        tester.stop()
        ecu_session.stop()


async def test_pre_programming_quiets_the_bus_and_restores_it():
    channel = f'flash-{uuid.uuid4().hex}'
    tester = BusSession(LiveSource(BusConfig('virtual', channel)))
    ecu_session = BusSession(LiveSource(BusConfig('virtual', channel)))
    tester.start()
    ecu_session.start()
    target, other = UdsServer(), UdsServer()
    ecus = [
        UdsEcuSimulator(ecu_session, 0x7E0, 0x7E8, server=target),
        UdsEcuSimulator(ecu_session, 0x7E1, 0x7E9, server=other),
    ]
    for ecu in ecus:
        ecu.start()
    transport = IsoTpClient(tester, 0x7E0, 0x7E8)
    functional_transport = obd_11bit(tester)
    states = []
    try:
        job = FlashJob(
            FlashImage([Segment(0x4000, FIRMWARE[:500])]),
            FlashOptions(pre_programming=True),
            on_change=lambda job: states.append((other.session, other.communication['control'], other.dtc_setting_on)),
        )
        assert [step.name for step in job.steps] == [
            'Extended session (all ECUs)',
            'Tester present (all ECUs)',
            'DTC setting off (all ECUs)',
            'Communication control disableRxAndTx (all ECUs)',
            'Programming session',
            'Erase 0x00004000 (500 bytes)',
            'Download 0x00004000 (500 bytes)',
            'ECU reset',
            'Communication control enableRxAndTx (all ECUs)',
            'DTC setting on (all ECUs)',
            'Default session (all ECUs)',
        ]
        await job.run(UdsClient(transport), FunctionalUdsClient(functional_transport, timeout=0.2))
        assert target.memory == {0x4000: FIRMWARE[:500]}
        # While programming, the other ECU was in the extended session with communication and DTC setting off
        assert (0x03, 0x03, False) in states
        assert (other.session, other.communication['control'], other.dtc_setting_on) == (0x01, 0x00, True)
        assert other.tester_present_count >= 1  # The functional keep-alive reached it
    finally:
        transport.close()
        functional_transport.close()
        for ecu in ecus:
            ecu.stop()
        tester.stop()
        ecu_session.stop()


async def test_functional_steps_without_a_functional_client_go_to_the_ecu():
    server = UdsServer()
    job = FlashJob(FlashImage([Segment(0, FIRMWARE[:10])]), FlashOptions(pre_programming=True, restore=False))
    await job.run(UdsClient(ServerTransport(server)))
    assert job.steps[0].detail == 'sent to this ECU only (no functional addressing)'
    assert server.memory == {0: FIRMWARE[:10]}


async def test_sequence_presets(tmp_path):
    preset = tmp_path / 'oem.json'
    preset.write_text(
        '{"name": "OEM X", "steps": ['
        '{"step": "session", "session": "03"},'
        '{"step": "security", "level": 1},'
        '{"step": "write_did", "did": "F199", "data": "26 11 15"},'
        '{"step": "session", "session": 2},'
        '{"step": "program", "erase_routine": null},'
        '{"step": "routine", "routine": "FF01", "name": "Check dependencies"},'
        '{"step": "wait", "seconds": 0},'
        '{"step": "request", "payload": "11 01"}]}'
    )
    name, steps = load_sequence(preset)
    assert name == 'OEM X'
    server = UdsServer()
    job = FlashJob(FlashImage([Segment(0x100, FIRMWARE[:64])]), FlashOptions(compute_key=xor_key), sequence=steps)
    assert [step.name for step in job.steps] == [
        'Extended session',
        'Security access level 1',
        'Write DID 0xF199',
        'Programming session',
        'Download 0x00000100 (64 bytes)',
        'Check dependencies',
        'Wait 0 s',
        'Request 11 01',
    ]
    await job.run(UdsClient(ServerTransport(server)))
    assert server.dids[0xF199] == bytes([0x26, 0x11, 0x15]) and server.memory == {0x100: FIRMWARE[:64]}
    assert server.erased == [] and server.session == 0x01

    for bad, message in (
        ('[]', 'expected an object'),
        ('{"steps": [{"step": "session", "session": 2}]}', 'exactly one "program"'),
        ('{"steps": [{"step": "program"}, {"step": "fly"}]}', 'must be one of'),
        ('{"steps": [{"step": "program"}, {"step": "security", "level": 2}]}', 'odd'),
        ('{"steps": [{"step": "program"}, {"step": "routine", "routine": "xyz"}]}', 'not a hex number'),
        ('{"steps": [{"step": "program"}, {"step": "wait", "seconds": -1}]}', 'seconds'),
    ):
        preset.write_text(bad)
        with pytest.raises(SequenceError, match=message):
            load_sequence(preset)
    assert default_sequence(FlashOptions())[-1] == {'step': 'reset', 'type': 0x01}


async def test_encoder_hook(tmp_path):
    encoder = tmp_path / 'encoder.py'
    encoder.write_text('def encode(data, address):\n    return bytes(b ^ 0x55 for b in data)\n')
    encode = load_encoder(encoder)

    class EncodedServer(UdsServer):
        def _request_download(self, request):
            request = bytes([request[0], 0x00]) + request[2:]  # Accepts the encoded format
            return super()._request_download(request)

    server = EncodedServer()
    options = FlashOptions(data_format=0x11, encode=encode, erase_routine=None)
    job = FlashJob(FlashImage([Segment(0, FIRMWARE[:40])]), options)
    await job.run(UdsClient(ServerTransport(server)))
    assert server.memory[0] == bytes(b ^ 0x55 for b in FIRMWARE[:40]) and job.progress == 1.0
    assert [r for r in server.requests if r[0] == 0x34][0][1] == 0x11
    (tmp_path / 'bad.py').write_text('x = 1\n')
    with pytest.raises(SequenceError, match='does not define encode'):
        load_encoder(tmp_path / 'bad.py')
