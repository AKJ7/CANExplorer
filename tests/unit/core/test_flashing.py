import uuid

import pytest

from can_explorer.core.flashing import (
    FlashImage,
    FlashJob,
    FlashOptions,
    ImageError,
    Segment,
    intel_hex_text,
    load_image,
    parse_intel_hex,
    parse_srec,
)
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession
from can_explorer.core.simulation import UdsEcuSimulator, UdsServer, xor_key
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.uds import UdsClient, UdsNegativeResponse

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
