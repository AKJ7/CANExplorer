from dataclasses import dataclass
from typing import Optional, Tuple

import can

from can_explorer.core.lin import frame_duration as lin_frame_duration

# Bits of a classic frame excluding the data field and bit stuffing (SOF up to the interframe space)
_CLASSIC_OVERHEAD = {False: 47, True: 67}
# CAN FD: bits sent at the nominal rate (arbitration phase, CRC delimiter, ACK, EOF, IFS) ...
_FD_NOMINAL = {False: 30, True: 49}
# ... and bits of the data phase besides the data: ESI, DLC, stuff count and CRC (17 or 21 bits)
_FD_DATA_OVERHEAD_SHORT = 26
_FD_DATA_OVERHEAD_LONG = 30


def frame_duration(msg: can.Message, bitrate: int, data_bitrate: Optional[int] = None) -> float:
    """Time a frame occupies the bus in seconds, excluding bit stuffing (adds up to ~20% on real buses)."""
    payload_bits = 0 if msg.is_remote_frame else 8 * len(msg.data)
    if not msg.is_fd:
        return (_CLASSIC_OVERHEAD[msg.is_extended_id] + payload_bits) / bitrate
    data_overhead = _FD_DATA_OVERHEAD_SHORT if len(msg.data) <= 16 else _FD_DATA_OVERHEAD_LONG
    data_rate = data_bitrate if (msg.bitrate_switch and data_bitrate) else bitrate
    return _FD_NOMINAL[msg.is_extended_id] / bitrate + (data_overhead + payload_bits) / data_rate


@dataclass(frozen=True, slots=True)
class BusSnapshot:
    rx_rate: float
    tx_rate: float
    load: Optional[float]
    error_frames: int
    rx_total: int
    tx_total: int


class BusStatistics:
    """Frame rates, estimated bus load and error frame count, sampled periodically."""

    def __init__(self, bitrates: Optional[Tuple[int, Optional[int]]] = None, lin_baudrate: Optional[int] = None):
        self._bitrates = bitrates
        self._lin_baudrate = lin_baudrate
        self.rx_total = 0
        self.tx_total = 0
        self.error_frames = 0
        self._rx = self._tx = 0
        self._busy_time = 0.0
        self._last_sample: Optional[float] = None
        self.last = BusSnapshot(0.0, 0.0, None if self._timing_unknown else 0.0, 0, 0, 0)

    @property
    def _timing_unknown(self) -> bool:
        return self._bitrates is None and self._lin_baudrate is None

    def update(self, msg: can.Message) -> None:
        if msg.is_error_frame:
            self.error_frames += 1
            return
        if msg.is_rx:
            self._rx += 1
        else:
            self._tx += 1
        if self._lin_baudrate is not None:
            self._busy_time += lin_frame_duration(msg, self._lin_baudrate)
        elif self._bitrates is not None:
            self._busy_time += frame_duration(msg, *self._bitrates)

    def sample(self, now: float) -> BusSnapshot:
        """Rates over the time since the previous sample. `now` is a monotonic time in seconds."""
        self.rx_total += self._rx
        self.tx_total += self._tx
        elapsed = None if self._last_sample is None else now - self._last_sample
        if elapsed and elapsed > 0:
            load = None if self._timing_unknown else min(1.0, self._busy_time / elapsed)
            self.last = BusSnapshot(
                self._rx / elapsed, self._tx / elapsed, load, self.error_frames, self.rx_total, self.tx_total
            )
        self._rx = self._tx = 0
        self._busy_time = 0.0
        self._last_sample = now
        return self.last

    def reset(self) -> None:
        self.__init__(self._bitrates, self._lin_baudrate)
