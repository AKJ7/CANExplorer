import json

import can

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.e2e import Profile5
from can_explorer.core.secoc_check import SecOcConfig, SecOcVerifier, StaticSecOcKeys
from can_explorer.core.security_events import SecurityMonitor

KEYS = StaticSecOcKeys({'*': (bytes(range(16)), '')})
SECURED = SecOcConfig('SecuredStatus', 18, 2, 28, 32, 4, 'CMAC_AES128')


def frame(identifier, data, timestamp, extended=False, is_rx=True):
    return can.Message(arbitration_id=identifier, is_extended_id=extended, data=data, timestamp=timestamp, is_rx=is_rx)


def feed(decoder, monitor, msg):
    decoder.describe(msg)  # As the session does: the decoder checks first, then the listeners see the frame
    monitor.on_message(msg)


def test_secoc_failures_unknown_ids_and_bursts(secoc_arxml):
    decoder = FrameDecoder(database=DbcDecoder(secoc_arxml), secoc=SecOcVerifier(KEYS))
    monitor = SecurityMonitor(decoder, window=1.0, burst=3)
    sender = SecOcVerifier(KEYS)
    good = sender.secure(SECURED, b'\x05\x00', 1)
    feed(decoder, monitor, frame(0x300, good, 10.0))
    assert monitor.rows() == []  # Verified frames are not events
    forged = bytes([good[0] ^ 1]) + good[1:]
    for index in range(3):
        feed(decoder, monitor, frame(0x300, forged, 10.1 + index * 0.1))
    feed(decoder, monitor, frame(0x7AB, b'\x01', 10.5))
    feed(decoder, monitor, frame(0x7AB, b'\x01', 10.6, is_rx=False))  # Own frames are not counted
    rows = {row['kind']: row for row in monitor.rows()}
    secoc = rows['secoc']
    assert secoc['identifier'] == '300' and secoc['count'] == 3 and secoc['severity'] == 'high'
    assert secoc['detail'].startswith('SecOC MAC ERROR') and secoc['first'] == 0.1
    assert rows['unknown'] == {
        'kind': 'unknown',
        'identifier': '7AB',
        'detail': 'not in the signal database',
        'severity': 'medium',
        'first': 0.5,
        'last': 0.5,
        'count': 1,
        'title': 'Unknown identifier',
    }
    assert monitor.rows()[0]['kind'] == 'unknown'  # Most recent first


def test_e2e_errors_and_frames_too_fast(e2e_arxml):
    database = DbcDecoder(e2e_arxml)
    engine = database.database.get_message_by_name('EngineData')
    engine.cycle_time = 100
    decoder = FrameDecoder(database=database)
    monitor = SecurityMonitor(decoder)
    protected = Profile5(4660).protect(bytes(8), 1)
    feed(decoder, monitor, frame(0x100, protected, 1.0))
    feed(decoder, monitor, frame(0x100, Profile5(4660).protect(bytes(8), 2), 1.1))  # On time
    assert monitor.rows() == []
    feed(decoder, monitor, frame(0x100, Profile5(4660).protect(bytes(8), 3), 1.12))  # 20 ms after: injected?
    feed(decoder, monitor, frame(0x100, bytes([protected[0] ^ 1]) + protected[1:], 1.3))
    rows = {row['kind']: row for row in monitor.rows()}
    assert rows['too-fast']['detail'] == '20.0 ms after the previous frame, cycle time 100 ms (EngineData)'
    assert rows['e2e']['detail'].startswith('E2E P05 CRC ERROR') and rows['e2e']['severity'] == 'high'


def test_refused_security_access_and_authentication(tmp_path):
    monitor = SecurityMonitor(FrameDecoder())  # No database: no unknown-identifier events
    for index in range(3):
        feed(monitor.decoder, monitor, frame(0x7E8, bytes([0x03, 0x7F, 0x27, 0x35, 0, 0, 0, 0]), 2.0 + index * 0.1))
    feed(monitor.decoder, monitor, frame(0x7E8, bytes([0x03, 0x7F, 0x29, 0x58, 0, 0, 0, 0]), 3.0))
    feed(monitor.decoder, monitor, frame(0x7E8, bytes([0x03, 0x7F, 0x22, 0x31, 0, 0, 0, 0]), 3.1))  # Not security
    rows = monitor.rows()
    assert [(row['detail'], row['count'], row['severity']) for row in rows] == [
        ('Authentication refused: ownershipVerificationFailed (NRC 0x58)', 1, 'medium'),
        ('SecurityAccess refused: invalidKey (NRC 0x35)', 3, 'high'),
    ]
    csv_path, json_path = tmp_path / 'events.csv', tmp_path / 'events.json'
    assert monitor.export(csv_path) == 2 and monitor.export(json_path) == 2
    assert csv_path.read_text().splitlines()[0] == 'severity,title,kind,identifier,count,first,last,detail'
    assert json.loads(json_path.read_text())['events'][1]['count'] == 3
    monitor.enabled['refused'] = False
    monitor.clear()
    feed(monitor.decoder, monitor, frame(0x7E8, bytes([0x03, 0x7F, 0x27, 0x35, 0, 0, 0, 0]), 4.0))
    assert monitor.rows() == []
