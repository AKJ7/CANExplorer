import socket
import struct

import pytest

from can_explorer.core import ethernet, someip
from can_explorer.core.e2e import Profile4
from can_explorer.core.someip_e2e import (
    SomeIpE2eChecker,
    SomeIpE2eConfig,
    SomeIpE2eError,
    load_configs,
    protected_data,
    save_configs,
)

SERVICE, EVENT = 0x1234, 0x8001
CONFIG = SomeIpE2eConfig(SERVICE, EVENT, profile=4, data_id=0x0A0B0C0D, name='SpeedEvent')
HEADER = {4: 12, 6: 5, 7: 20}


def message_bytes(payload: bytes, client=0, session=1, message_type=0x02) -> bytes:
    """A SOME/IP message (notification by default) with this payload."""
    return struct.pack('>HHIHHBBBB', SERVICE, EVENT, 8 + len(payload), client, session, 1, 1, message_type, 0) + payload


def protected_message(
    counter: int, config: SomeIpE2eConfig = CONFIG, user_data: bytes = b'\x00\x64', **header
) -> bytes:
    """A SOME/IP message whose payload carries an E2E header (right after the SOME/IP header) and user data."""
    payload = bytes(HEADER[config.profile]) + user_data
    message = someip.parse_message(message_bytes(payload, **header))
    protected = config.profile_checker().protect(protected_data(message), counter)
    return message_bytes(protected[8:], **header)


def check(checker: SomeIpE2eChecker, data: bytes):
    return checker.check(someip.parse_message(data))


def test_the_spec_example_with_the_someip_header():
    """PRS_E2EProtocol R23-11 table 6.43: the protected data starts with the upper header (here all zero)."""
    config = SomeIpE2eConfig(SERVICE, EVENT, 4, 0x0A0B0C0D)
    message = someip.parse_message(
        struct.pack('>HHIHHBBBB', SERVICE, EVENT, 8 + 16, 0, 0, 0, 0, 0, 0)
        + bytes.fromhex('0018 0000 0a0b0c0d 69d7502e 00000000'.replace(' ', ''))
    )
    assert protected_data(message)[:8] == bytes(8)
    result = SomeIpE2eChecker([config]).check(message)
    assert result.ok and result.describe() == 'E2E P04 ok, counter 0'


@pytest.mark.parametrize('profile, data_id', [(4, 0xDEADBEEF), (6, 0x1234), (7, 0xCAFEF00D)])
def test_profiles_and_counter_sequence(profile, data_id):
    config = SomeIpE2eConfig(SERVICE, EVENT, profile, data_id, max_delta_counter=3)
    checker = SomeIpE2eChecker([config])
    label = f'E2E P{profile:02d}'
    assert check(checker, protected_message(10, config)).describe() == f'{label} ok, counter 10'
    assert check(checker, protected_message(11, config)).describe() == f'{label} ok, counter 11'
    assert check(checker, protected_message(11, config)).describe() == f'{label} REPEATED, counter 11'
    assert check(checker, protected_message(13, config)).describe() == f'{label} ok, counter 13 (1 lost)'
    wrong = check(checker, protected_message(20, config))
    assert not wrong.ok and wrong.describe() == f'{label} WRONG SEQUENCE, counter 20 (after 13)'
    checker.reset()
    assert check(checker, protected_message(5, config)).ok
    # Different clients are separate sequences
    assert check(checker, protected_message(0, config, client=7)).ok


def test_counter_wraps():
    config = SomeIpE2eConfig(SERVICE, EVENT, 6, 0x1234)
    checker = SomeIpE2eChecker([config])
    assert check(checker, protected_message(0xFF, config)).ok
    assert check(checker, protected_message(0x00, config)).describe() == 'E2E P06 ok, counter 0'


def test_errors():
    checker = SomeIpE2eChecker([CONFIG])
    good = protected_message(1)
    corrupted = good[:-1] + bytes([good[-1] ^ 0x01])
    result = check(checker, corrupted)
    assert not result.ok and result.status == 'CRC ERROR'
    assert result.describe().startswith('E2E P04 CRC ERROR 0x') and result.describe().endswith('counter 1')
    # The protected upper header: a changed session id fails the CRC
    session_changed = good[:10] + b'\x00\x02' + good[12:]
    assert check(checker, session_changed).status == 'CRC ERROR'
    # The SOME/IP Message ID and Length are not protected, the E2E length field is
    longer = message_bytes(good[16:] + b'\x00')
    assert check(checker, longer).describe().startswith('E2E P04 ERROR: length field ')
    short = message_bytes(b'\x00\x01')
    assert check(checker, short).describe() == 'E2E P04 ERROR: shorter than the 12-byte E2E header at byte 8'
    other = someip.parse_message(struct.pack('>HHIHHBBBB', SERVICE, 0x8002, 8, 0, 1, 1, 1, 2, 0))
    assert checker.check(other) is None  # Not configured


def test_configuration_validation_and_json(tmp_path):
    with pytest.raises(SomeIpE2eError, match='Profile 5 is not supported'):
        SomeIpE2eConfig(SERVICE, EVENT, 5, 1)
    with pytest.raises(SomeIpE2eError, match='16-bit Data ID'):
        SomeIpE2eConfig(SERVICE, EVENT, 6, 0x10000)
    with pytest.raises(SomeIpE2eError, match='whole number of bytes'):
        SomeIpE2eConfig(SERVICE, EVENT, 4, 1, offset=60)
    with pytest.raises(SomeIpE2eError, match='Two E2E protections'):
        SomeIpE2eChecker([CONFIG, CONFIG])
    with pytest.raises(SomeIpE2eError, match='needs data_id'):
        SomeIpE2eConfig.from_dict({'service_id': 1, 'method_id': 2, 'profile': 4})
    with pytest.raises(SomeIpE2eError, match='is not a number'):
        SomeIpE2eConfig.from_dict({'service_id': 'x', 'method_id': 2, 'profile': 4, 'data_id': 1})
    path = tmp_path / 'e2e.json'
    other = SomeIpE2eConfig(0x0100, 0x0001, 7, 5, offset=96, max_delta_counter=4)
    save_configs(path, [CONFIG, other])
    assert '"service_id": "0x1234"' in path.read_text()
    assert load_configs(path) == [CONFIG, other]
    path.write_text('[{"service_id": "0x1234", "method_id": "0x8001", "profile": 6, "data_id": 4660}]')
    assert load_configs(path) == [SomeIpE2eConfig(SERVICE, EVENT, 6, 0x1234)]
    path.write_text('{"protections": 3}')
    with pytest.raises(SomeIpE2eError, match='does not hold a list'):
        load_configs(path)
    with pytest.raises(SomeIpE2eError, match='Cannot read'):
        load_configs(tmp_path / 'missing.json')


def test_monitor_checks_messages_as_they_arrive():
    monitor = someip.SomeIpMonitor()
    monitor.e2e = SomeIpE2eChecker([CONFIG])
    for counter in (1, 2, 2):
        someip._ingest(monitor, protected_message(counter))
    assert [received.e2e for received in monitor.messages] == [
        'E2E P04 ok, counter 1',
        'E2E P04 ok, counter 2',
        'E2E P04 REPEATED, counter 2',
    ]
    monitor.clear()
    someip._ingest(monitor, protected_message(2))
    assert monitor.messages[-1].e2e == 'E2E P04 ok, counter 2'  # Clearing starts the sequences over


def udp_frame(payload: bytes) -> bytes:
    datagram = struct.pack('>HHHH', 50000, 30509, 8 + len(payload), 0) + payload
    ip = struct.pack('>BBHHHBBH', 0x45, 0, 20 + len(datagram), 0, 0, 64, 17, 0)
    ip += socket.inet_aton('192.168.0.10') + socket.inet_aton('192.168.0.20')
    return bytes.fromhex('112233445566aabbccddeeff0800') + ip + datagram


def test_ethernet_capture_checks_e2e():
    decoder = ethernet.StreamDecoder(e2e=SomeIpE2eChecker([CONFIG]))
    first = decoder.decode(udp_frame(protected_message(1)))
    assert first.protocol == 'SOME/IP' and first.info.endswith('[E2E P04 ok, counter 1]')
    # Several messages in one datagram are all checked, in order
    both = decoder.decode(udp_frame(protected_message(2) + protected_message(2)))
    assert both.info.split(' | ')[1].endswith('[E2E P04 REPEATED, counter 2]')
    plain = ethernet.StreamDecoder().decode(udp_frame(protected_message(1)))
    assert 'E2E' not in plain.info


def test_profile4_header_bytes_in_a_message():
    data = protected_message(7)
    e2e_header = data[16:28]
    assert e2e_header[:2] == (8 + 12 + 2).to_bytes(2, 'big')  # Length: upper header + E2E header + user data
    assert e2e_header[2:4] == (7).to_bytes(2, 'big') and e2e_header[4:8] == bytes.fromhex('0a0b0c0d')
    assert Profile4(0x0A0B0C0D, 8).check(protected_data(someip.parse_message(data))).ok
