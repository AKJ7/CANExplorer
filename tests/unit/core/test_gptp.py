import struct

from can_explorer.core import gptp

GM = b'\xaa\xbb\xcc\xff\xfe\x11\x22\x33'
CLOCK_A = b'\x00\x11\x22\xff\xfe\x33\x44\x55'


def ptp(message_type, *, domain=0, seq=1, clock=CLOCK_A, port=1, log_interval=-3, body=b''):
    header = (
        bytes([message_type & 0x0F, 0x02])
        + struct.pack('>H', gptp.HEADER_SIZE + len(body))
        + bytes([domain, 0])
        + b'\x00\x00'  # flags
        + b'\x00' * 8  # correctionField
        + b'\x00' * 4  # reserved
        + clock
        + struct.pack('>H', port)
        + struct.pack('>H', seq)
        + b'\x00'  # controlField
        + struct.pack('>b', log_interval)
    )
    return header + body


def announce(*, grandmaster=GM, priority1=128, clock_class=6, priority2=128, steps=0, time_source=0x20, **header):
    body = (
        bytes(10)  # originTimestamp
        + struct.pack('>h', 37)  # currentUtcOffset
        + b'\x00'  # reserved
        + bytes([priority1])
        + bytes([clock_class, 0x21])  # clockClass, clockAccuracy
        + struct.pack('>H', 0x4000)  # offsetScaledLogVariance
        + bytes([priority2])
        + grandmaster
        + struct.pack('>H', steps)
        + bytes([time_source])
    )
    return ptp(0xB, body=body, **header)


def test_parse_header_and_announce():
    message = gptp.parse(announce(domain=0, seq=7, log_interval=0))
    assert message.type == 'Announce' and message.domain == 0 and message.sequence == 7
    assert message.source_clock == '00:11:22:ff:fe:33:44:55' and message.source_port == 1
    assert message.grandmaster == 'aa:bb:cc:ff:fe:11:22:33'
    assert message.gm_priority1 == 128 and message.gm_clock_class == 6 and message.steps_removed == 0
    assert message.interval_ms == 1000.0  # log_interval 0 -> 1 s


def test_parse_sync_and_too_short():
    sync = gptp.parse(ptp(0x0, seq=3, log_interval=-3))
    assert sync.type == 'Sync' and sync.sequence == 3 and abs(sync.interval_ms - 125.0) < 0.01
    assert gptp.parse(b'\x00' * 10) is None  # Too short


def test_describe():
    assert 'Sync seq 3' == gptp.describe(gptp.parse(ptp(0x0, seq=3)))
    description = gptp.describe(gptp.parse(announce(seq=1)))
    assert 'Announce' in description and 'class 6' in description and 'prio 128' in description


def test_summarize_picks_best_grandmaster_and_sync_rate():
    messages = []
    # Two grandmasters announced; the lower priority1 wins
    messages.append(_with_time(gptp.parse(announce(grandmaster=GM, priority1=128)), 0.0))
    messages.append(_with_time(gptp.parse(announce(grandmaster=CLOCK_A, clock=CLOCK_A, priority1=100)), 0.1))
    # Four Sync messages 125 ms apart
    for i in range(4):
        messages.append(_with_time(gptp.parse(ptp(0x0, clock=GM, log_interval=-3)), i * 0.125))

    summary = gptp.summarize(messages)
    # The lower priority1 (100, announced by CLOCK_A) wins over 128
    assert summary.grandmaster == '00:11:22:ff:fe:33:44:55' and summary.gm_priority1 == 100
    assert summary.gm_clock_class == 6 and summary.steps_removed == 0
    assert abs(summary.sync_interval_ms - 125.0) < 0.01
    assert abs(summary.sync_rate_hz - 8.0) < 0.1  # 3 gaps over 0.375 s
    assert summary.counts['Sync'] == 4 and summary.counts['Announce'] == 2
    assert len(summary.clocks) == 2 and summary.domains == [0]


def _with_time(message, timestamp):
    message.timestamp = timestamp
    return message
