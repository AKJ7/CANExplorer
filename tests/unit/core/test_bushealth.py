import can

from can_explorer.core.bushealth import (
    ERR_BUSOFF,
    ERR_CNT,
    ERR_CRTL,
    ERR_PROT,
    ERR_RESTARTED,
    BusHealth,
    buckets,
    decode_error_frame,
    state_from_counters,
)
from can_explorer.core.decoding import FrameDecoder


def error(classes, data=b'', timestamp=0.0):
    return can.Message(
        arbitration_id=classes, is_error_frame=True, data=bytes(data).ljust(8, b'\x00'), timestamp=timestamp
    )


def test_decode_error_frames():
    info = decode_error_frame(error(ERR_CRTL | ERR_CNT, [0, 0x20, 0, 0, 0, 0, 130, 12]))
    assert (info.texts, info.state, info.tec, info.rec) == (('TX error passive',), 'passive', 130, 12)
    assert info.describe() == 'TX error passive (TEC 130, REC 12)'
    info = decode_error_frame(error(ERR_PROT, [0, 0, 0x04 | 0x80]))
    assert info.texts == ('bit stuffing error', 'error while transmitting') and info.state is None
    assert decode_error_frame(error(ERR_BUSOFF)).state == 'bus-off'
    assert decode_error_frame(error(ERR_RESTARTED)).state == 'active'
    assert decode_error_frame(error(ERR_CNT, [0, 0, 0, 0, 0, 0, 100, 3])).state == 'warning'
    assert [state_from_counters(*counters) for counters in ((0, 0), (96, 0), (10, 200), (256, 0))] == [
        'active',
        'warning',
        'passive',
        'bus-off',
    ]
    assert FrameDecoder().describe(error(ERR_BUSOFF)) == 'bus off'


def test_health_history_and_timeline():
    health = BusHealth()
    health.sample(1000.0, 10, 0, 0.1, None)
    health.on_message(error(ERR_CRTL | ERR_CNT, [0, 0x08, 0, 0, 0, 0, 100, 0], 1000.5))
    health.sample(1001.0, 10, 0, 0.2, None)
    health.on_message(error(ERR_BUSOFF, timestamp=1001.5))
    health.on_message(error(ERR_PROT, [0, 0, 0x01], 1001.6))
    health.sample(1002.0, 0, 0, 0.0, None)
    health.on_message(error(ERR_RESTARTED, timestamp=1002.5))
    health.sample(1003.0, 10, 0, 0.1, None)
    assert [sample.state for sample in health.samples] == ['active', 'warning', 'bus-off', 'active']
    assert [sample.errors for sample in health.samples] == [0, 1, 2, 1]
    assert health.error_frames == 4 and (health.tec, health.rec) == (100, 0)
    assert health.transitions == {'active': 2, 'warning': 1, 'bus-off': 1}
    assert [event.text for event in health.events][:2] == ['TX error warning (TEC 100, REC 0)', 'bus off']
    timeline = health.timeline(4)
    assert [segment['state'] for segment in timeline] == ['active', 'warning', 'bus-off', 'active']
    assert timeline[0]['start'] == 0 and timeline[-1]['end'] == 1.0
    health.sample(1004.0, 0, 0, None, 'error')  # The interface reports bus off itself
    assert health.state == 'bus-off' and health.events[-1].text == 'Interface reports bus-off'
    assert len(health.window(2)) == 2


def test_buckets():
    assert buckets([1, 5, 2, None, 3, 4, 0], 2) == [1, 5, 3, 4]  # Groups aligned to the newest value
    assert buckets([1, 2, 3], 1) == [1, 2, 3] and buckets([], 5) == []
    assert buckets([1, 2, 3, 4], 3, combine=sum) == [1, 9]
