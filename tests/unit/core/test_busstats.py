import can
import pytest

from can_explorer.core.busstats import BusStatistics, frame_duration


def test_classic_frame_duration():
    standard = can.Message(arbitration_id=1, is_extended_id=False, data=bytes(8))
    extended = can.Message(arbitration_id=1, is_extended_id=True, data=bytes(8))
    assert frame_duration(standard, 500_000) == pytest.approx(111 / 500_000)
    assert frame_duration(extended, 500_000) == pytest.approx(131 / 500_000)
    remote = can.Message(arbitration_id=1, is_extended_id=False, is_remote_frame=True, dlc=8)
    assert frame_duration(remote, 500_000) == pytest.approx(47 / 500_000)


def test_fd_frame_uses_data_bitrate_with_bitrate_switch():
    msg = can.Message(arbitration_id=1, is_extended_id=False, is_fd=True, bitrate_switch=True, data=bytes(64))
    fast = frame_duration(msg, 500_000, 2_000_000)
    assert fast == pytest.approx(30 / 500_000 + (30 + 512) / 2_000_000)
    msg.bitrate_switch = False
    assert frame_duration(msg, 500_000, 2_000_000) == pytest.approx((30 + 30 + 512) / 500_000)


def test_rates_load_and_error_frames():
    statistics = BusStatistics((500_000, None))
    statistics.sample(0.0)
    for _ in range(100):
        statistics.update(can.Message(arbitration_id=1, is_extended_id=False, data=bytes(8), is_rx=True))
    for _ in range(50):
        statistics.update(can.Message(arbitration_id=2, is_extended_id=False, data=bytes(8), is_rx=False))
    statistics.update(can.Message(is_error_frame=True))
    snapshot = statistics.sample(0.5)
    assert snapshot.rx_rate == 200 and snapshot.tx_rate == 100
    assert snapshot.load == pytest.approx(150 * 111 / 500_000 / 0.5)
    assert snapshot.error_frames == 1 and snapshot.rx_total == 100 and snapshot.tx_total == 50
    assert statistics.sample(1.5).rx_rate == 0


def test_unknown_bitrate_has_no_load():
    statistics = BusStatistics(None)
    statistics.sample(0.0)
    statistics.update(can.Message(arbitration_id=1))
    assert statistics.sample(1.0).load is None
