from types import SimpleNamespace

import can
import pytest

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.e2e import Profile2, Profile5, Profile22
from can_explorer.core.e2e_check import E2eConfig, e2e_config_of, profile_of

DATA_IDS_16 = tuple(range(0x10, 0x20))


@pytest.mark.parametrize(
    'category, profile',
    [
        ('PROFILE_05', 5),
        ('PROFILE_02', 2),
        ('Profile22', 22),
        ('E2E_PROFILE_01', 1),
        ('profile 11', 11),
        ('', None),
        (None, None),
        ('CRC_ONLY', None),
    ],
)
def test_profile_of(category, profile):
    assert profile_of(category) == profile


@pytest.mark.parametrize(
    'config, protect',
    [
        (E2eConfig('PROFILE_05', (0x1234,)), lambda data, counter: Profile5(0x1234).protect(data, counter)),
        (E2eConfig('PROFILE_02', DATA_IDS_16), lambda data, counter: Profile2(DATA_IDS_16).protect(data, counter)),
        (E2eConfig('PROFILE_22', DATA_IDS_16), lambda data, counter: Profile22(DATA_IDS_16).protect(data, counter)),
    ],
)
def test_supported_profiles_check_frames(config, protect):
    frame = protect(bytes([0, 0, 0, 0x11, 0x22, 0x33, 0x44, 0x55]), 9)
    assert config.problem == ''
    good = config.check(frame)
    assert good.checked and good.ok and good.counter == 9
    corrupted = config.check(frame[:-1] + bytes([frame[-1] ^ 0x01]))
    assert corrupted.checked and not corrupted.ok and corrupted.crc != corrupted.expected_crc


def test_descriptions():
    config = E2eConfig('PROFILE_05', (0x1234,))
    frame = Profile5(0x1234).protect(bytes(8), 7)
    assert config.describe(frame) == 'E2E P05 ok, counter 7'
    check = config.check(bytes([frame[0] ^ 1]) + frame[1:])
    assert config.describe(bytes([frame[0] ^ 1]) + frame[1:]) == (
        f'E2E P05 CRC ERROR 0x{check.crc:04X}, expected 0x{check.expected_crc:04X}, counter 7'
    )
    p02 = E2eConfig('PROFILE_02', DATA_IDS_16)
    frame = Profile2(DATA_IDS_16).protect(bytes(8), 3)
    assert p02.describe(bytes([frame[0] ^ 1]) + frame[1:]).startswith('E2E P02 CRC ERROR 0x')  # 8-bit CRC
    assert len(p02.describe(bytes([frame[0] ^ 1]) + frame[1:]).split()[4]) == len('0x00,')


@pytest.mark.parametrize(
    'config, reason',
    [
        (E2eConfig('PROFILE_01', (0x12,)), 'profile 1 is not implemented'),
        (E2eConfig('PROFILE_11', (0x12,)), 'profile 11 is not implemented'),
        (E2eConfig('CRC_ONLY', (0x12,)), "profile not recognised from category 'CRC_ONLY'"),
        (E2eConfig('', (0x12,)), 'no profile category'),
        (E2eConfig('PROFILE_05', (1, 2)), 'profile 5 needs one Data ID, the ARXML gives 2'),
        (E2eConfig('PROFILE_02', (1,)), 'the ARXML gives 1'),
    ],
)
def test_configurations_that_are_not_checked(config, reason):
    """Never guessed: an unknown profile, or Data IDs that do not fit the profile, are shown as not checked."""
    assert reason in config.problem
    result = config.check(bytes(8))
    assert not result.checked and reason in result.reason
    assert config.describe(bytes(8)).endswith(f'not checked ({result.reason})')


def test_short_frames_and_the_protected_length():
    config = E2eConfig('PROFILE_05', (0x1234,), length=4)
    assert not config.check(bytes(2)).checked
    protected = Profile5(0x1234).protect(bytes([0, 0, 0, 0xAB]), 1)
    # Bytes after the protected length (e.g. a SecOC MAC) do not enter the CRC
    assert config.check(protected + bytes([0xDE, 0xAD, 0xBE, 0xEF])).ok


def test_config_of_a_cantools_message():
    message = SimpleNamespace(
        autosar=SimpleNamespace(e2e=SimpleNamespace(category='PROFILE_05', data_ids=[4660], payload_length=8))
    )
    assert e2e_config_of(message) == E2eConfig('PROFILE_05', (4660,), 8)
    assert e2e_config_of(SimpleNamespace(autosar=None)) is None
    assert e2e_config_of(SimpleNamespace(autosar=SimpleNamespace(e2e=None))) is None
    assert e2e_config_of(SimpleNamespace()) is None


def test_arxml_protections_are_checked_in_the_trace(e2e_arxml):
    decoder = DbcDecoder(e2e_arxml, bus='Powertrain')
    config = E2eConfig('PROFILE_05', (4660,), 8)
    # The frame, and the same PDU contained in the Bundle container frame
    assert decoder.e2e_protected() == [('EngineData', config), ('EnginePdu', config)]
    frame = Profile5(4660).protect(bytes(8), 7)
    message = can.Message(arbitration_id=0x100, is_extended_id=False, data=frame)
    assert decoder.describe(message).endswith('[E2E P05 ok, counter 7]')
    message.data = bytearray([frame[0] ^ 1]) + frame[1:]
    assert '[E2E P05 CRC ERROR 0x' in decoder.describe(message)


def test_databases_without_protections_are_unchanged(data_dir):
    decoder = DbcDecoder(data_dir / 'example.arxml', bus='Powertrain')
    assert decoder.e2e_protected() == []
    assert 'E2E' not in decoder.describe(can.Message(arbitration_id=0x100, is_extended_id=False, data=bytes(8)))


def test_protected_pdus_inside_a_container_are_checked(e2e_arxml):
    decoder = DbcDecoder(e2e_arxml, bus='Powertrain')
    bundle = decoder.database.get_message_by_name('Bundle')
    engine = Profile5(4660).protect(bytes(8), 3)
    data = bundle.encode([(bundle.get_contained_message_by_name('EnginePdu'), engine)])
    message = can.Message(arbitration_id=0x200, is_extended_id=False, is_fd=True, data=data)
    assert '[E2E P05 ok, counter 3]' in decoder.describe(message)
    corrupted = bundle.encode(
        [(bundle.get_contained_message_by_name('EnginePdu'), bytes([engine[0] ^ 1]) + engine[1:])]
    )
    message.data = bytearray(corrupted)
    assert 'EnginePdu: ' in decoder.describe(message) and 'E2E P05 CRC ERROR' in decoder.describe(message)
