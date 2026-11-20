import uuid

import pytest

from can_explorer.core.can_command import CanCommandClient
from can_explorer.core.flashing import FlashImage, Segment
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.core.xcp import RESOURCE_PGM, XcpClient, XcpError
from can_explorer.core.xcp_flash import XcpFlashJob, XcpFlashOptions, erase_ranges
from can_explorer.core.xcp_protocol import (
    DOWNLOAD,
    PROGRAM,
    PROGRAM_CLEAR,
    PROGRAM_FORMAT,
    PROGRAM_MAX,
    PROGRAM_NEXT,
    PROGRAM_PREPARE,
    PROGRAM_RESET,
    PROGRAM_START,
)
from can_explorer.core.xcp_slave import DEFAULT_SECTORS, DEMO_ENCRYPTION, XcpSlaveSimulator, demo_encrypt, demo_key


@pytest.fixture
def make_xcp():
    created = []

    def make(compute_key=None, **options):
        channel = f'xcp-flash-{uuid.uuid4().hex}'
        master = BusSession(LiveSource(BusConfig('virtual', channel)))
        slave = BusSession(LiveSource(BusConfig('virtual', channel)))
        master.start()
        slave.start()
        simulator = XcpSlaveSimulator(slave, 0x7E0, 0x7E1, auto_events=False, **options)
        transport = CanCommandClient(master, 0x7E0, 0x7E1, timeout=0.2)
        created.append((master, slave, simulator, transport))
        return XcpClient(transport, compute_key=compute_key), simulator

    yield make
    for master, slave, simulator, transport in created:
        transport.close()
        simulator.stop()
        master.stop()
        slave.stop()


# The simulator's flash: sectors 0x8000+0x2000, 0xA000+0x2000, 0xC000+0x4000
IMAGE = FlashImage([Segment(0x8100, bytes(range(200))), Segment(0xC010, b'\x5a' * 37)])


def test_erase_ranges_cover_the_sectors_touched():
    sectors = [{'start': 0x8000, 'length': 0x2000}, {'start': 0xA000, 'length': 0x2000}, {'start': 0, 'length': 0}]
    assert erase_ranges([Segment(0x9FF0, bytes(0x20))], sectors) == [(0x8000, 0x2000), (0xA000, 0x2000)]
    assert erase_ranges([Segment(0x8000, b'1'), Segment(0x8100, b'2')], sectors) == [(0x8000, 0x2000)]
    with pytest.raises(XcpError, match='0x0000C000 of the image lies outside'):
        erase_ranges([Segment(0xBFFF, b'12')], sectors)


def test_options_are_checked():
    with pytest.raises(ValueError, match='erase is one of'):
        XcpFlashOptions(erase='all')


async def test_flash_job_programs_verifies_and_resets(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    simulator.flash[0xA000:0xA010] = b'\x11' * 16  # In a sector the image does not touch
    changes = []
    job = XcpFlashJob(IMAGE, on_change=lambda j: changes.append(j.progress))
    await job.run(client)
    flash = simulator.flash
    assert flash[0x8100:0x81C8] == bytes(range(200))
    assert flash[0xC010:0xC035] == b'\x5a' * 37
    assert flash[0x8000:0x8100] == b'\xff' * 0x100  # The rest of the touched sectors is erased
    assert flash[0xA000:0xA010] == b'\x11' * 16  # Untouched sector kept
    assert job.erased == [(0x8000, 0x2000), (0xC000, 0x4000)]
    assert [s['name'] for s in job.sectors] == ['Sector 0', 'Sector 1', 'Sector 2']
    assert all(step.status == 'done' for step in job.steps), [(s.name, s.status, s.detail) for s in job.steps]
    assert all('matches' in step.detail for step in job.steps if step.name.startswith('Verify'))
    assert job.bytes_done == job.total == 237 and changes[-1] == 1.0
    # The reset ends the session and restarts the ECU from the new flash
    assert simulator.resets == 1 and not client.connected
    assert simulator.memory[0x8100:0x81C8] == bytes(range(200))
    commands = [c[0] for c in simulator.commands]
    assert commands.index(PROGRAM_CLEAR) < commands.index(PROGRAM) < commands.index(PROGRAM_RESET)
    assert bytes([PROGRAM, 0]) in [c[:2] for c in simulator.commands]  # The end of the sequence


async def test_programming_without_erasing_fails(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    simulator.flash[0x8100] = 0x00  # Not erased
    job = XcpFlashJob(IMAGE, XcpFlashOptions(erase='none', reset=False))
    with pytest.raises(XcpError, match='generic error'):
        await job.run(client)
    assert [s.status for s in job.steps][:2] == ['done', 'failed']
    assert all(s.status == 'skipped' for s in job.steps[2:])
    assert job.error and client.connected and simulator.resets == 0


async def test_segment_erase_and_master_block_mode(make_xcp):
    client, simulator = make_xcp(master_block_mode=True)
    await client.connect()
    job = XcpFlashJob(IMAGE, XcpFlashOptions(erase='segments', verify=False, reset=False))
    await job.run(client)
    assert job.erased == [(0x8100, 200), (0xC010, 37)]
    assert simulator.flash[0x8100:0x81C8] == bytes(range(200))
    assert any(c[0] == PROGRAM_NEXT for c in simulator.commands)
    assert client.connected and simulator.slave.program_finished


async def test_programming_unlocks_pgm(make_xcp):
    client, simulator = make_xcp(protected=RESOURCE_PGM, compute_key=demo_key)
    await client.connect()
    await XcpFlashJob(IMAGE, XcpFlashOptions(reset=False)).run(client)
    assert simulator.flash[0xC010] == 0x5A and not simulator.slave.locked & RESOURCE_PGM

    client, simulator = make_xcp(protected=RESOURCE_PGM)
    await client.connect()
    with pytest.raises(XcpError, match='PROGRAM_START: access locked'):
        await XcpFlashJob(IMAGE).run(client)


async def test_reset_without_an_answer(make_xcp):
    client, simulator = make_xcp(reset_answers=False)
    await client.connect()
    await XcpFlashJob(FlashImage([Segment(0x8000, b'\x01\x02')])).run(client)
    assert simulator.resets == 1 and not client.connected


async def test_programming_outside_the_flash_is_refused(make_xcp):
    client, _simulator = make_xcp()
    await client.connect()
    with pytest.raises(XcpError, match='outside the flash sectors'):
        await XcpFlashJob(FlashImage([Segment(0x1000, b'\x01')])).run(client)


async def test_a_slave_without_programming(make_xcp):
    client, _simulator = make_xcp(sectors=())
    await client.connect()
    assert not client.resources & RESOURCE_PGM
    with pytest.raises(XcpError, match='command unknown'):
        await XcpFlashJob(IMAGE).run(client)


def _flash_after(image, erased_sectors):
    """The simulator's whole flash (all sectors in order) once `image` is programmed into erased sectors."""
    flash = bytearray(0x10000)
    for start, length in erased_sectors:
        flash[start : start + length] = b'\xff' * length
    for segment in image.segments:
        flash[segment.address : segment.end] = segment.data
    return b''.join(bytes(flash[s : s + n]) for s, n in DEFAULT_SECTORS)


async def test_functional_erase_and_ecu_checks(make_xcp):
    import zlib

    client, simulator = make_xcp()
    await client.connect()
    expected = zlib.crc32(_flash_after(IMAGE, DEFAULT_SECTORS))
    job = XcpFlashJob(IMAGE, XcpFlashOptions(erase='functional', ecu_check='value', check_value=expected))
    await job.run(client)
    assert any(c[:2] == bytes([PROGRAM_CLEAR, 1]) for c in simulator.commands)  # Functional mode
    assert simulator.flash[0xA000:0xA010] == b'\xff' * 16  # All the code areas were cleared
    assert simulator.slave.verifications == [(1, 4, expected)]
    assert job.steps[1].detail == 'functional: code areas'

    client, simulator = make_xcp()
    await client.connect()
    with pytest.raises(XcpError, match='verify failed'):
        await XcpFlashJob(IMAGE, XcpFlashOptions(erase='functional', ecu_check='value', check_value=1)).run(client)

    client, simulator = make_xcp()
    await client.connect()
    await XcpFlashJob(IMAGE, XcpFlashOptions(ecu_check='internal', reset=False)).run(client)
    assert simulator.slave.verifications == [(0, 4, 0)]


async def test_flash_kernel_is_downloaded_first(make_xcp):
    kernel = FlashImage([Segment(0x0400, bytes(range(40)))], start_address=0x0410)
    client, simulator = make_xcp(kernel=0x0400)
    await client.connect()
    with pytest.raises(XcpError, match='PROGRAM_START: generic error'):  # Refused without a kernel
        await XcpFlashJob(IMAGE).run(client)
    await XcpFlashJob(IMAGE, XcpFlashOptions(kernel=kernel, reset=False)).run(client)
    assert simulator.memory[0x0400:0x0428] == bytes(range(40))
    # PROGRAM_PREPARE, the download, then PROGRAM_START with the MTA at the entry point
    codes = [c[0] for c in simulator.commands]
    assert codes.index(PROGRAM_PREPARE) < codes.index(DOWNLOAD) < len(codes) - 1 - codes[::-1].index(PROGRAM_START)


async def test_encrypted_image(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    options = XcpFlashOptions(encryption=DEMO_ENCRYPTION, encode=demo_encrypt, reset=False)
    job = XcpFlashJob(IMAGE, options)
    await job.run(client)
    assert any(c[0] == PROGRAM_FORMAT and c[2] == DEMO_ENCRYPTION for c in simulator.commands)
    assert simulator.flash[0x8100:0x81C8] == bytes(range(200))  # Decrypted by the ECU
    assert all('matches' in s.detail for s in job.steps if s.name.startswith('Verify'))  # Against the plain image
    assert job.steps[2].name == 'Set the data format'

    client, _simulator = make_xcp()
    await client.connect()
    with pytest.raises(XcpError, match='does not support compressed data'):
        await XcpFlashJob(IMAGE, XcpFlashOptions(compression=0x80)).run(client)


async def test_program_max(make_xcp):
    client, simulator = make_xcp()
    await client.connect()
    await XcpFlashJob(IMAGE, XcpFlashOptions(program_max=True, reset=False)).run(client)
    sent = [c for c in simulator.commands if c[0] == PROGRAM_MAX]
    assert len(sent) == 200 // 7 + 37 // 7  # Full 7-byte packets; the rest by PROGRAM
    assert simulator.flash[0x8100:0x81C8] == bytes(range(200)) and simulator.flash[0xC010:0xC035] == b'\x5a' * 37


def test_flash_options_are_checked():
    with pytest.raises(ValueError, match='ecu_check is one of'):
        XcpFlashOptions(ecu_check='maybe')
    with pytest.raises(ValueError, match='encryption method is a byte'):
        XcpFlashOptions(encryption=300)
