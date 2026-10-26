"""Polling OBD-II values (service 01) while recording, as references for finding the same values in the traffic.

Requests go to the functional address (7DF, or 18DB33F1 with 29 bit identifiers); answers from any ECU (7E8-7EF,
18DAF1xx) are recorded with their timestamps.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import can

from can_explorer.core.reverse.reference import Reference

IdKey = Tuple[int, bool]


@dataclass(frozen=True)
class Pid:
    number: int
    name: str
    unit: str
    decode: Callable[[bytes], float]
    length: int = 1  # Data bytes


PIDS = (
    Pid(0x04, 'Calculated engine load', '%', lambda d: d[0] * 100 / 255),
    Pid(0x05, 'Engine coolant temperature', '°C', lambda d: d[0] - 40),
    Pid(0x0B, 'Intake manifold pressure', 'kPa', lambda d: d[0]),
    Pid(0x0C, 'Engine speed', 'rpm', lambda d: (256 * d[0] + d[1]) / 4, 2),
    Pid(0x0D, 'Vehicle speed', 'km/h', lambda d: d[0]),
    Pid(0x0F, 'Intake air temperature', '°C', lambda d: d[0] - 40),
    Pid(0x10, 'Mass air flow rate', 'g/s', lambda d: (256 * d[0] + d[1]) / 100, 2),
    Pid(0x11, 'Throttle position', '%', lambda d: d[0] * 100 / 255),
    Pid(0x2F, 'Fuel tank level', '%', lambda d: d[0] * 100 / 255),
    Pid(0x42, 'Control module voltage', 'V', lambda d: (256 * d[0] + d[1]) / 1000, 2),
    Pid(0x46, 'Ambient air temperature', '°C', lambda d: d[0] - 40),
    Pid(0x5C, 'Engine oil temperature', '°C', lambda d: d[0] - 40),
)
PIDS_BY_NUMBER = {pid.number: pid for pid in PIDS}

REQUEST_ID = 0x7DF
REQUEST_ID_EXTENDED = 0x18DB33F1


def is_obd_identifier(key: IdKey) -> bool:
    """Diagnostic requests and answers, which are not part of the traffic to analyse."""
    frame_id, extended = key
    if extended:
        return frame_id == REQUEST_ID_EXTENDED or frame_id & 0xFFFFFF00 == 0x18DAF100
    return frame_id == REQUEST_ID or 0x7E0 <= frame_id <= 0x7EF


def request(pid: int, extended: bool = False) -> can.Message:
    return can.Message(
        arbitration_id=REQUEST_ID_EXTENDED if extended else REQUEST_ID,
        is_extended_id=extended,
        data=bytes([0x02, 0x01, pid, 0x55, 0x55, 0x55, 0x55, 0x55]),
    )


def response(pid: int, value_bytes: bytes, ecu: int = 0, extended: bool = False) -> can.Message:
    """An ECU's answer (for simulations and tests)."""
    data = bytes([2 + len(value_bytes), 0x41, pid]) + value_bytes
    return can.Message(
        arbitration_id=(0x18DAF100 | ecu) if extended else 0x7E8 + ecu,
        is_extended_id=extended,
        data=data.ljust(8, b'\x55'),
    )


class ObdPoller:
    """Requests the PIDs one after the other, `interval` seconds apart, on a session, and records the answers.

    `send` sends a frame (e.g. BusSession.send); `on_message` must receive the frames of the bus.
    """

    def __init__(
        self,
        send: Callable[[can.Message], None],
        pids: Sequence[int],
        interval: float = 0.05,
        extended: bool = False,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        unknown = [pid for pid in pids if pid not in PIDS_BY_NUMBER]
        if unknown:
            raise ValueError(f'Unknown PIDs: {", ".join(f"{pid:02X}" for pid in unknown)}')
        if not pids:
            raise ValueError('Choose at least one value')
        self._send = send
        self.pids = list(pids)
        self.interval = max(0.01, interval)
        self.extended = extended
        self._on_error = on_error
        self.series: Dict[int, Tuple[List[float], List[float]]] = {pid: ([], []) for pid in self.pids}
        self.requests = 0
        self.error = ''
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self.error = ''
            self._task = asyncio.get_running_loop().create_task(self._run(), name='OBD-II polling')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            for pid in self.pids:
                try:
                    self._send(request(pid, self.extended))
                except Exception as e:
                    self.error = str(e)
                    if self._on_error is not None:
                        self._on_error(self.error)
                    return
                self.requests += 1
                await asyncio.sleep(self.interval)

    def on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.is_error_frame or msg.is_remote_frame:
            return
        if msg.is_extended_id != self.extended or not is_obd_identifier((msg.arbitration_id, msg.is_extended_id)):
            return
        data = bytes(msg.data)
        if len(data) < 4 or data[1] != 0x41 or not 3 <= data[0] <= 7:
            return
        pid = PIDS_BY_NUMBER.get(data[2])
        if pid is None or pid.number not in self.series or data[0] < 2 + pid.length:
            return
        times, values = self.series[pid.number]
        times.append(msg.timestamp)
        values.append(float(pid.decode(data[3 : 3 + pid.length])))

    def answers(self, pid: int) -> int:
        return len(self.series.get(pid, ((), ()))[0])

    def reference(self, number: int) -> Optional[Reference]:
        """The values recorded for a PID."""
        if number not in self.series:
            return None
        pid = PIDS_BY_NUMBER[number]
        times, values = self.series[number]
        return Reference(f'OBD {pid.name}', pid.unit, list(times), list(values))

    def references(self) -> List[Reference]:
        """The recorded values of each PID that was answered."""
        return [self.reference(number) for number, (times, _) in self.series.items() if times]
