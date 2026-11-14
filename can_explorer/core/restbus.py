"""Restbus simulation: send the messages of a database periodically, so the ECU under test sees the rest of the
bus it expects even though those nodes are not on the bench.

Each database message becomes a periodic transmission at its cycle time (the DBC ``GenMsgCycleTime`` / ARXML
cycle); messages with a cycle time are enabled by default, event-only messages are listed but off. The payload is
encoded from the signals' initial values (editable at runtime), and messages sent by a chosen node — the ECU you
are testing — can be excluded, which is the usual restbus set.

Qt-free; it drives a BusSession's periodic transmissions. It does **not** compute message counters or E2E/CRC
checksums: those stay at whatever value you set, so a receiver that checks them will see them constant. AUTOSAR
SecOC secured PDUs are secured afresh for every frame (freshness value and MAC) when a `secure` function is given
(the connection's `FrameDecoder.secure`, with the security manager's keys).
"""

from typing import Callable, Dict, List, Optional, Tuple

import can

from can_explorer.core.secoc_check import SecOcError, secoc_config_of
from can_explorer.core.session import BusSession, PeriodicTransmit, SessionError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DEFAULT_CYCLE_MS = 100  # Fallback period for messages without a cycle time in the database

IdKey = Tuple[int, bool]  # (frame_id, is_extended_frame)


def _default_values(message) -> Dict[str, object]:
    """A physical value for every signal: its initial value, else its minimum, else 0 (or the first choice)."""
    values: Dict[str, object] = {}
    for signal in message.signals:
        if signal.initial is not None:
            value = signal.initial
        elif getattr(signal, 'minimum', None) is not None:
            value = signal.minimum
        else:
            value = 0
        values[signal.name] = value
    return values


def _encode(message, values: Dict[str, object]) -> bytes:
    """Encode a message from physical signal values, falling back to zero bytes when it cannot be encoded (for
    example a multiplexed or container message whose layout the given values do not satisfy)."""
    try:
        return bytes(message.encode(values, strict=False, padding=True))
    except Exception as error:  # noqa: BLE001 - cantools raises various errors; a restbus must still send something
        logger.debug('Restbus: encoding %s failed (%s); sending zeros', message.name, error)
        return bytes(message.length)


class RestbusMessage:
    """One database message prepared for periodic transmission."""

    def __init__(self, message, period: float, enabled: bool):
        self.message = message  # cantools Message
        self.period = period  # Seconds
        self.enabled = enabled
        self.values = _default_values(message)
        self.data = _encode(message, self.values)
        self.secured = secoc_config_of(message) is not None  # Secured afresh for every frame
        self._transmit: Optional[PeriodicTransmit] = None

    @property
    def key(self) -> IdKey:
        return (self.message.frame_id, self.message.is_extended_frame)

    @property
    def senders(self) -> List[str]:
        return list(self.message.senders)

    @property
    def has_cycle_time(self) -> bool:
        return bool(self.message.cycle_time)

    @property
    def running(self) -> bool:
        return self._transmit is not None

    def _can_message(self) -> can.Message:
        return can.Message(
            arbitration_id=self.message.frame_id,
            is_extended_id=self.message.is_extended_frame,
            is_fd=len(self.data) > 8,
            data=self.data,
        )

    def set_value(self, signal: str, value: object) -> None:
        self.values[signal] = value
        self.data = _encode(self.message, self.values)
        if self._transmit is not None:
            self._transmit.message = self._can_message()  # The next tick sends the new payload


class RestbusSimulator:
    """Drives the periodic transmission of a set of database messages on a session."""

    def __init__(
        self,
        session: BusSession,
        messages,
        default_cycle_ms: int = DEFAULT_CYCLE_MS,
        secure: Optional[Callable[[can.Message], can.Message]] = None,
    ):
        self.session = session
        self.secure = secure  # Secures the frames of SecOC secured PDUs
        self.problems: Dict[IdKey, str] = {}  # Enabled messages that could not start, and why
        self.default_period = default_cycle_ms / 1000.0
        self._messages: Dict[IdKey, RestbusMessage] = {}
        for message in messages:
            period = message.cycle_time / 1000.0 if message.cycle_time else self.default_period
            entry = RestbusMessage(message, period, enabled=bool(message.cycle_time))
            self._messages[entry.key] = entry
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def messages(self) -> List[RestbusMessage]:
        return list(self._messages.values())

    def get(self, key: IdKey) -> Optional[RestbusMessage]:
        return self._messages.get(key)

    @property
    def enabled_count(self) -> int:
        return sum(1 for entry in self._messages.values() if entry.enabled)

    def set_enabled(self, key: IdKey, enabled: bool) -> None:
        entry = self._messages.get(key)
        if entry is None or entry.enabled == enabled:
            return
        entry.enabled = enabled
        if self._running:
            self._start_entry(entry) if enabled else self._stop_entry(entry)

    def set_all(self, enabled: bool) -> None:
        for entry in self._messages.values():
            self.set_enabled(entry.key, enabled)

    def enable_cyclic_only(self) -> None:
        """Enable only the messages that have a cycle time in the database (the usual restbus set)."""
        for entry in self._messages.values():
            self.set_enabled(entry.key, entry.has_cycle_time)

    def set_default_period(self, seconds: float) -> None:
        """The period used for messages without a cycle time; restarts those already running with the new period."""
        self.default_period = seconds
        for entry in self._messages.values():
            if not entry.has_cycle_time:
                entry.period = seconds
                if entry.running:
                    self._stop_entry(entry)
                    if self._running and entry.enabled:
                        self._start_entry(entry)

    def exclude_sender(self, node: str) -> int:
        """Disable every message sent by `node` (the ECU under test); returns how many were disabled."""
        count = 0
        for entry in self._messages.values():
            if node in entry.senders and entry.enabled:
                self.set_enabled(entry.key, False)
                count += 1
        return count

    def set_value(self, key: IdKey, signal: str, value: object) -> None:
        entry = self._messages.get(key)
        if entry is not None:
            entry.set_value(signal, value)

    def start(self) -> List[str]:
        """Begin transmitting every enabled message at its period. Requires the session to be open. Returns why
        messages could not start (e.g. secured PDUs without a key); the others run."""
        if self._running:
            return []
        self._running = True
        self.problems = {}
        for entry in self._messages.values():
            if entry.enabled:
                self._start_entry(entry)
        return [f'{self._messages[key].message.name}: {problem}' for key, problem in self.problems.items()]

    def stop(self) -> None:
        if not self._running:
            return
        for entry in self._messages.values():
            self._stop_entry(entry)
        self._running = False

    def _start_entry(self, entry: RestbusMessage) -> None:
        if entry._transmit is not None:
            return
        prepare = None
        if entry.secured:
            if self.secure is None:
                self.problems[entry.key] = 'secured PDU (SecOC), but no security manager to secure it'
                return
            try:
                self.secure(entry._can_message())  # Fails now, not on every period, without a key
            except SecOcError as error:
                self.problems[entry.key] = str(error)
                logger.warning('Restbus: not sending %s: %s', entry.message.name, error)
                return
            prepare = self.secure
        try:
            entry._transmit = self.session.start_periodic(entry._can_message(), entry.period, prepare=prepare)
        except (SessionError, ValueError) as error:
            self.problems[entry.key] = str(error)
            logger.warning('Restbus: could not start %s: %s', entry.message.name, error)

    def _stop_entry(self, entry: RestbusMessage) -> None:
        if entry._transmit is not None:
            try:
                entry._transmit.stop()
            except Exception:  # noqa: BLE001 - stopping must not raise
                pass
            entry._transmit = None
