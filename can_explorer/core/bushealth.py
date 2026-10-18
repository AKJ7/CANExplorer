"""Bus health over time: error frames, error counters, error states and the bus load, sampled once per second.

Error frames are decoded with the SocketCAN layout (linux/can/error.h): the identifier holds the error classes,
data byte 1 the controller state, byte 2 the protocol error type and bytes 6 and 7 the transmit and receive error
counters. Interfaces reporting the bus state directly (python-can's `BusABC.state`) are used as well.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import can

HISTORY_SECONDS = 3600
MAX_EVENTS = 500

# Error classes (identifier bits)
ERR_TX_TIMEOUT, ERR_LOSTARB, ERR_CRTL, ERR_PROT, ERR_TRX, ERR_ACK, ERR_BUSOFF, ERR_BUSERROR, ERR_RESTARTED, ERR_CNT = (
    1 << bit for bit in range(10)
)
ERROR_CLASSES = {
    ERR_TX_TIMEOUT: 'TX timeout',
    ERR_LOSTARB: 'lost arbitration',
    ERR_CRTL: 'controller problem',
    ERR_PROT: 'protocol violation',
    ERR_TRX: 'transceiver status',
    ERR_ACK: 'no acknowledgement',
    ERR_BUSOFF: 'bus off',
    ERR_BUSERROR: 'bus error',
    ERR_RESTARTED: 'controller restarted',
}
# Controller problems (data byte 1)
CONTROLLER_PROBLEMS = {
    0x01: 'RX buffer overflow',
    0x02: 'TX buffer overflow',
    0x04: 'RX error warning',
    0x08: 'TX error warning',
    0x10: 'RX error passive',
    0x20: 'TX error passive',
    0x40: 'back to error active',
}
# Protocol violations (data byte 2)
PROTOCOL_ERRORS = {
    0x01: 'single bit error',
    0x02: 'frame format error',
    0x04: 'bit stuffing error',
    0x08: 'unable to send dominant bit',
    0x10: 'unable to send recessive bit',
    0x20: 'bus overload',
    0x40: 'active error announcement',
    0x80: 'error while transmitting',
}

STATES = ('active', 'warning', 'passive', 'bus-off')
STATE_RANK = {state: rank for rank, state in enumerate(STATES)}
WARNING_LIMIT, PASSIVE_LIMIT, BUS_OFF_LIMIT = 96, 128, 256


@dataclass(frozen=True)
class ErrorInfo:
    texts: Tuple[str, ...]
    state: Optional[str] = None  # State the frame reports, if any
    tec: Optional[int] = None
    rec: Optional[int] = None

    def describe(self) -> str:
        text = ', '.join(self.texts) or 'error frame'
        if self.tec is not None:
            text += f' (TEC {self.tec}, REC {self.rec})'
        return text


def state_from_counters(tec: int, rec: int) -> str:
    worst = max(tec, rec)
    if tec >= BUS_OFF_LIMIT:
        return 'bus-off'
    if worst >= PASSIVE_LIMIT:
        return 'passive'
    if worst >= WARNING_LIMIT:
        return 'warning'
    return 'active'


def decode_error_frame(msg: can.Message) -> ErrorInfo:
    """What an error frame reports, in the SocketCAN layout."""
    classes = msg.arbitration_id
    data = bytes(msg.data).ljust(8, b'\x00')
    texts: List[str] = []
    state = None
    for bit, name in ERROR_CLASSES.items():
        if not classes & bit:
            continue
        if bit == ERR_CRTL:
            problems = [text for flag, text in CONTROLLER_PROBLEMS.items() if data[1] & flag]
            texts += problems or [name]
            if data[1] & 0x30:
                state = 'passive'
            elif data[1] & 0x0C:
                state = 'warning'
            elif data[1] & 0x40:
                state = 'active'
        elif bit == ERR_PROT:
            texts += [text for flag, text in PROTOCOL_ERRORS.items() if data[2] & flag] or [name]
        else:
            texts.append(name)
    if classes & ERR_BUSOFF:
        state = 'bus-off'
    elif classes & ERR_RESTARTED and state is None:
        state = 'active'
    tec = rec = None
    if classes & ERR_CNT:
        tec, rec = data[6], data[7]
        if state is None:
            state = state_from_counters(tec, rec)
    return ErrorInfo(tuple(texts), state, tec, rec)


@dataclass
class HealthSample:
    time: float  # Wall clock (seconds since the epoch)
    rx_rate: float
    tx_rate: float
    load: Optional[float]  # 0..1, None when the bitrate is unknown
    errors: int  # Error frames since the previous sample
    tec: Optional[int]
    rec: Optional[int]
    state: Optional[str]


@dataclass
class HealthEvent:
    time: float
    text: str
    state: Optional[str] = None


@dataclass
class BusHealth:
    samples: Deque[HealthSample] = field(default_factory=lambda: deque(maxlen=HISTORY_SECONDS))
    events: Deque[HealthEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    error_frames: int = 0
    tec: Optional[int] = None
    rec: Optional[int] = None
    state: Optional[str] = None  # Current state; None while unknown
    transitions: Dict[str, int] = field(default_factory=dict)  # Times each state was entered
    _errors_since_sample: int = 0
    _state_from_frames: bool = False  # The state comes from error frames, not from the interface

    def on_message(self, msg: can.Message) -> None:
        if not msg.is_error_frame:
            return
        info = decode_error_frame(msg)
        self.error_frames += 1
        self._errors_since_sample += 1
        if info.tec is not None:
            self.tec, self.rec = info.tec, info.rec
        self.events.append(HealthEvent(msg.timestamp, info.describe(), info.state))
        if info.state is not None:
            self._set_state(info.state)
            self._state_from_frames = True

    def _set_state(self, state: Optional[str]) -> None:
        if state is not None and state != self.state:
            self.transitions[state] = self.transitions.get(state, 0) + 1
        self.state = state

    def sample(
        self, time: float, rx_rate: float, tx_rate: float, load: Optional[float], interface_state: Optional[str]
    ) -> HealthSample:
        """Record one second with the state the interface reports ('active', 'passive', 'error' for bus off).

        python-can reports 'active' for interfaces that do not know their state, so 'active' does not override a
        state reported by error frames; the error frames announce the return to error active themselves.
        """
        if interface_state == 'active' and self._state_from_frames:
            interface_state = None
        if interface_state is not None:
            self._state_from_frames = False
            state = {'error': 'bus-off'}.get(interface_state, interface_state)
            if state != self.state and self.state is not None:
                self.events.append(HealthEvent(time, f'Interface reports {state}', state))
            self._set_state(state)
        elif self.state is None and (self.samples or rx_rate or tx_rate):
            self._set_state('active')  # Frames flow without errors reported
        sample = HealthSample(time, rx_rate, tx_rate, load, self._errors_since_sample, self.tec, self.rec, self.state)
        self._errors_since_sample = 0
        self.samples.append(sample)
        return sample

    def window(self, seconds: int) -> List[HealthSample]:
        if not self.samples:
            return []
        start = self.samples[-1].time - seconds
        return [sample for sample in self.samples if sample.time > start]

    def timeline(self, seconds: int) -> List[Dict[str, object]]:
        """Segments of equal state over the last `seconds`, as fractions of the window (0 = oldest)."""
        samples = self.window(seconds)
        if not samples:
            return []
        end = samples[-1].time
        start = end - seconds
        segments: List[Dict[str, object]] = []
        for sample in samples:
            state = sample.state or 'unknown'
            position = (sample.time - 1 - start) / seconds
            if segments and segments[-1]['state'] == state:
                segments[-1]['end'] = (sample.time - start) / seconds
            else:
                segments.append({'state': state, 'start': max(0.0, position), 'end': (sample.time - start) / seconds})
        return segments

    def reset(self) -> None:
        self.__init__()


def buckets(values: List[Optional[float]], size: int, combine=max) -> List[float]:
    """Values combined in groups of `size`, aligned to the newest value (max keeps error peaks visible).

    Missing values (None) are skipped; groups without any value are left out.
    """
    result: List[float] = []
    size = max(1, size)
    end = len(values)
    while end > 0:
        group = [value for value in values[max(0, end - size) : end] if value is not None]
        if group:
            result.append(combine(group))
        end -= size
    return result[::-1]
