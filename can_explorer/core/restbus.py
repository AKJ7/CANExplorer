"""Restbus simulation: send the messages of a database the way their nodes would, so the ECU under test sees the
rest of the bus it expects even though those nodes are not on the bench.

**What is sent.** Each database message is sent according to its send mode, taken from the database and changeable
per message:

- ``cyclic`` — every cycle time (the DBC ``GenMsgCycleTime`` / ARXML cycle, else the default cycle);
- ``on_change`` — when one of its values is edited (or on *Send now*), then ``GenMsgNrOfRepetition`` repetitions;
- ``cyclic_on_change`` — both;
- ``if_active`` — every ``GenMsgCycleTimeFast`` (else the cycle time) while the message is *active*, i.e. while a
  signal differs from its ``GenSigInactiveValue`` (from its default value when the database defines none), and not
  at all while inactive.

The DBC ``GenMsgSendType`` values map onto these (``Cyclic``, ``Spontaneous``…, ``CyclicAndSpontaneous``…,
``IfActive``/``CyclicIfActive``…); without one, a message is cyclic (at the default cycle when it has no cycle
time). Repetitions are spaced by ``GenMsgCycleTimeFast`` (else ``GenMsgDelayTime``, else 10 ms), and on-change
sends keep at least ``GenMsgDelayTime`` between frames. Messages with a cycle time are enabled by default.

**Frames are built afresh for every transmission**: counter signals count (wrapping at their maximum), multiplexed
messages rotate through their multiplexer pages, checksum signals are computed over the other bytes, AUTOSAR E2E
protected PDUs of an ARXML get their E2E counter and CRC (profiles 2, 5 and 22), and SecOC secured PDUs a new
freshness value and MAC (through the `secure` function: the connection's `FrameDecoder.secure`, with the security
manager's keys) — in that order, so a secured PDU authenticates the E2E-protected payload.

**Timing.** The cyclic messages start staggered (1 ms apart, within their period, after their
``GenMsgStartDelayTime``), so they do not all fall on the same instant. Frames that never change can be sent with
the interface's hardware timing (`hardware`), the others are timed by the event loop.

Qt-free; it drives a BusSession's periodic transmissions.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import can

from can_explorer.core.busstats import frame_duration
from can_explorer.core.e2e_check import E2eConfig, e2e_config_of
from can_explorer.core.reverse.checksums import crc8
from can_explorer.core.secoc_check import SecOcError, secoc_config_of
from can_explorer.core.session import BusSession, PeriodicTransmit, SessionError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DEFAULT_CYCLE_MS = 100  # Fallback period for messages without a cycle time in the database
STAGGER_MS = 1  # Gap between the first frames of the cyclic messages at start
DEFAULT_REPETITION_GAP_MS = 10  # Between repetitions of an on-change send, without a fast cycle or delay time

IdKey = Tuple[int, bool]  # (frame_id, is_extended_frame)

CYCLIC = 'cyclic'
ON_CHANGE = 'on_change'
CYCLIC_ON_CHANGE = 'cyclic_on_change'
IF_ACTIVE = 'if_active'
MODES = (CYCLIC, ON_CHANGE, CYCLIC_ON_CHANGE, IF_ACTIVE)

# The DBC GenMsgSendType values (lower case, without separators) and the send mode each one maps to; None: the
# message is not sent by the nodes' interaction layer (listed off; enabling it sends it as if it had none).
_SEND_TYPES: Dict[str, Optional[str]] = {
    'cyclic': CYCLIC,
    'spontaneous': ON_CHANGE,
    'spontaneouswithdelay': ON_CHANGE,
    'spontaneouswithrepetition': ON_CHANGE,
    'onchange': ON_CHANGE,
    'onchangewithrepetition': ON_CHANGE,
    'onwrite': ON_CHANGE,
    'onwritewithrepetition': ON_CHANGE,
    'event': ON_CHANGE,
    'cyclicandspontaneous': CYCLIC_ON_CHANGE,
    'cyclicandspontaneouswithdelay': CYCLIC_ON_CHANGE,
    'cyclicandonchange': CYCLIC_ON_CHANGE,
    'cyclicandonwrite': CYCLIC_ON_CHANGE,
    'ifactive': IF_ACTIVE,
    'cyclicifactive': IF_ACTIVE,
    'cyclicifactivefast': IF_ACTIVE,
    'cyclicifactiveandspontaneouswithdelay': IF_ACTIVE,
    'nomsgsendtype': None,
    'notused': None,
}

VALUE = 'value'
COUNTER = 'counter'
CHECKSUM = 'checksum'
ROLES = (VALUE, COUNTER, CHECKSUM)

# Names of signals taken for counters by default
_COUNTER_NAME = re.compile(r'counter|zaehler|(^|_)(cnt|ctr|sqc|alive|bz)($|_)', re.IGNORECASE)


@dataclass(frozen=True)
class ChecksumAlgorithm:
    name: str
    function: Callable[[bytes], int]  # Of the other bytes of the frame, in byte order
    constant_xor: bool = True  # The constant is XORed into the result (CRCs, XOR); else it is added (sums)


def _sum(data: bytes) -> int:
    return sum(data) & 0xFF


# The checksum algorithms, each with its standard parameters (start value, final XOR, reflection)
CHECKSUMS: Dict[str, ChecksumAlgorithm] = {
    'crc8_sae_j1850': ChecksumAlgorithm('CRC-8 SAE J1850 (AUTOSAR)', lambda d: crc8(d, 0x1D, 0xFF, 0xFF)),
    'crc8h2f': ChecksumAlgorithm('CRC-8H2F (AUTOSAR)', lambda d: crc8(d, 0x2F, 0xFF, 0xFF)),
    'crc8_smbus': ChecksumAlgorithm('CRC-8 (SMBus)', lambda d: crc8(d, 0x07)),
    'crc8_maxim': ChecksumAlgorithm('CRC-8 Maxim', lambda d: crc8(d, 0x31, reflected=True)),
    'crc8_cdma2000': ChecksumAlgorithm('CRC-8 CDMA2000', lambda d: crc8(d, 0x9B, 0xFF)),
    'xor': ChecksumAlgorithm('XOR of the bytes', lambda d: _xor(d)),
    'sum': ChecksumAlgorithm('Sum of the bytes', _sum, constant_xor=False),
    'inverted_sum': ChecksumAlgorithm('Inverted sum', lambda d: 0xFF - _sum(d), constant_xor=False),
}


def _xor(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
    return value


def checksum(algorithm: str, other_bytes: bytes, constant: int = 0) -> int:
    """The checksum byte over `other_bytes` with `constant` folded in (XORed for CRCs and XOR; added for sums, and
    subtracted from an inverted sum, so 0 gives the plain algorithm)."""
    spec = CHECKSUMS[algorithm]
    if spec.constant_xor:
        return (spec.function(other_bytes) ^ constant) & 0xFF
    if algorithm == 'inverted_sum':
        return (spec.function(other_bytes) - constant) & 0xFF
    return (spec.function(other_bytes) + constant) & 0xFF


@dataclass(frozen=True)
class SignalRole:
    role: str = VALUE  # VALUE (sent as set), COUNTER (counts per frame) or CHECKSUM (computed per frame)
    algorithm: str = 'crc8_sae_j1850'  # Of a checksum
    constant: int = 0  # Of a checksum

    def to_dict(self) -> Dict[str, Any]:
        if self.role == CHECKSUM:
            return {'role': self.role, 'algorithm': self.algorithm, 'constant': self.constant}
        return {'role': self.role}


def send_mode_of(message) -> Tuple[str, bool]:
    """(send mode, sent by the nodes at all) of a database message, from its send type. Without one (or with
    one the nodes do not send), a message is cyclic: at its cycle time, or the default cycle when enabled."""
    fallback = CYCLIC
    send_type = getattr(message, 'send_type', None)
    if not send_type:
        return fallback, True
    key = re.sub(r'[^a-z]', '', str(send_type).lower())
    if key not in _SEND_TYPES:
        logger.debug('Restbus: unknown send type %r of %s; treated as %s', send_type, message.name, fallback)
        return fallback, True
    mode = _SEND_TYPES[key]
    return (mode, True) if mode is not None else (fallback, False)


def _attribute(owner, name: str, defaults: Dict[str, Any]) -> Any:
    """A DBC attribute of a message or signal, else its definition's default, else None (or for non-DBC
    databases)."""
    dbc = getattr(owner, 'dbc', None)
    attributes = getattr(dbc, 'attributes', None) or {}
    if name in attributes:
        return attributes[name].value
    return defaults.get(name)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms_attribute(owner, name: str, defaults: Dict[str, Any]) -> float:
    """A time attribute in seconds; 0 when absent or not positive."""
    value = _number(_attribute(owner, name, defaults))
    return value / 1000.0 if value and value > 0 else 0.0


def _default_values(message) -> Dict[str, object]:
    """A physical value for every signal: its initial value, else its minimum, else 0."""
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


def _raw(signal, value: object) -> Optional[float]:
    """The raw value of a physical value or choice name; None if it cannot be converted."""
    if isinstance(value, str):
        for number, name in (signal.choices or {}).items():
            if str(name) == value:
                return float(number)
        return None
    try:
        return float(signal.conversion.scaled_to_raw(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _physical(signal, raw: int) -> object:
    return signal.conversion.raw_to_scaled(raw, decode_choices=False)


def _raw_range(signal) -> Tuple[int, int]:
    """The raw values a counter signal runs through: its minimum to its maximum when the database gives a range
    (min == max is taken for no range), else the whole bit range."""
    if signal.is_signed:
        low, high = -(1 << (signal.length - 1)), (1 << (signal.length - 1)) - 1
    else:
        low, high = 0, (1 << signal.length) - 1
    minimum, maximum = signal.minimum, signal.maximum
    if minimum is not None and maximum is not None and minimum != maximum:
        raw_min, raw_max = _raw(signal, minimum), _raw(signal, maximum)
        if raw_min is not None and raw_max is not None:
            raw_min, raw_max = sorted((round(raw_min), round(raw_max)))
            low, high = max(low, raw_min), min(high, raw_max)
    return low, high


def whole_byte(signal) -> Optional[int]:
    """The byte index of a signal that is exactly one whole byte (as a checksum must be), else None."""
    if signal.length != 8:
        return None
    if signal.byte_order == 'little_endian':
        return signal.start // 8 if signal.start % 8 == 0 else None
    return signal.start // 8 if signal.start % 8 == 7 else None  # Motorola: the start bit is the MSB


def _encode(message, values: Dict[str, object]) -> bytes:
    """Encode a message from physical signal values, falling back to zero bytes when it cannot be encoded (for
    example a container message whose layout the given values do not satisfy)."""
    try:
        return bytes(message.encode(values, strict=False, padding=True))
    except Exception as error:  # noqa: BLE001 - cantools raises various errors; a restbus must still send something
        logger.debug('Restbus: encoding %s failed (%s); sending zeros', message.name, error)
        return bytes(message.length)


def _multiplexer(message):
    """The top-level multiplexer signal of a message and its pages (multiplexer values), or (None, [])."""
    for signal in message.signals:
        if signal.is_multiplexer and signal.multiplexer_signal is None:
            pages = sorted(
                {
                    page
                    for other in message.signals
                    if other.multiplexer_signal == signal.name
                    for page in (other.multiplexer_ids or [])
                }
            )
            return signal, pages
    return None, []


class RestbusMessage:
    """One database message prepared for transmission, with the state that changes from frame to frame."""

    def __init__(
        self,
        message,
        period: float,
        enabled: bool,
        defaults: Optional[Dict[str, Any]] = None,
        brs: bool = False,
    ):
        self.message = message  # cantools Message
        defaults = defaults or {}
        self._defaults = defaults
        self.period = period  # Seconds
        self.mode, self.sent_by_nodes = send_mode_of(message)
        self.database_mode = self.mode
        self.fast_period = _ms_attribute(message, 'GenMsgCycleTimeFast', defaults)
        self.start_delay = _ms_attribute(message, 'GenMsgStartDelayTime', defaults)
        self.min_gap = _ms_attribute(message, 'GenMsgDelayTime', defaults)
        repetitions = _number(_attribute(message, 'GenMsgNrOfRepetition', defaults))
        self.repetitions = max(0, int(repetitions)) if repetitions else 0
        self.enabled = enabled
        self.values = _default_values(message)
        self.default_values = dict(self.values)
        self.secured = secoc_config_of(message) is not None  # Secured afresh for every frame
        self.e2e: Optional[E2eConfig] = e2e_config_of(message)  # Protected afresh for every frame
        self.is_fd = bool(getattr(message, 'is_fd', False)) or message.length > 8
        brs_attribute = _number(_attribute(message, 'CANFD_BRS', defaults))
        self.bitrate_switch = self.is_fd and (bool(brs_attribute) if brs_attribute is not None else brs)
        self.multiplexer, self.pages = _multiplexer(message)
        self.selected_pages: List[int] = list(self.pages)
        self.roles: Dict[str, SignalRole] = {}
        for signal in message.signals:
            if self._default_counter(signal):
                self.roles[signal.name] = SignalRole(COUNTER)
        self.default_roles = dict(self.roles)
        self._counters: Dict[str, int] = {}
        self._page_index = 0
        self._e2e_counter = 0
        self._transmit: Optional[PeriodicTransmit] = None
        self._burst: Optional[asyncio.Task] = None
        self._pending: Optional[asyncio.TimerHandle] = None  # A delayed hardware-timed start
        self._last_sent = float('-inf')  # Loop time of the last on-change frame
        self.problem = ''  # Why it could not start or stopped (set by the simulator)
        self.hardware_timed = False

    def _default_counter(self, signal) -> bool:
        return (
            self.e2e is None
            and not signal.is_multiplexer
            and not signal.choices
            and signal.length <= 16
            and signal.name not in self.computed_signals
            and bool(_COUNTER_NAME.search(signal.name))
        )

    @property
    def key(self) -> IdKey:
        return (self.message.frame_id, self.message.is_extended_frame)

    @property
    def name(self) -> str:
        return self.message.name

    @property
    def senders(self) -> List[str]:
        return list(self.message.senders)

    @property
    def has_cycle_time(self) -> bool:
        return bool(self.message.cycle_time)

    @property
    def cyclic(self) -> bool:
        return self.mode in (CYCLIC, CYCLIC_ON_CHANGE)

    @property
    def on_change(self) -> bool:
        return self.mode in (ON_CHANGE, CYCLIC_ON_CHANGE)

    @property
    def active_period(self) -> float:
        """The period while active (if_active), the fast cycle if the database has one."""
        return self.fast_period or self.period

    @property
    def repetition_gap(self) -> float:
        return self.fast_period or self.min_gap or DEFAULT_REPETITION_GAP_MS / 1000.0

    @property
    def running(self) -> bool:
        """Being sent periodically right now (or about to start)."""
        if self._pending is not None:
            return True
        return self._transmit is not None and not self._transmit.finished

    @property
    def computed_signals(self) -> set:
        """Signals whose value is computed for each frame, not edited: SecOC freshness and authenticator."""
        if not self.secured:
            return set()
        return {f'{self.message.name}_Freshness', f'{self.message.name}_Authenticator'}

    @property
    def dynamic(self) -> bool:
        """The frame changes from one transmission to the next (so it cannot be timed by the hardware)."""
        return (
            self.secured
            or self.e2e is not None
            or any(role.role != VALUE for role in self.roles.values())
            or len(self.selected_pages) > 1
        )

    @property
    def active(self) -> bool:
        """Whether an if-active message is to be sent: a value signal differs from its inactive value (the
        GenSigInactiveValue attribute; without one in the database, its default value)."""
        signals = [
            signal
            for signal in self.message.signals
            if signal.name not in self.roles or self.roles[signal.name].role == VALUE
            if not signal.is_multiplexer and signal.name not in self.computed_signals
        ]
        defined = 'GenSigInactiveValue' in self._defaults or any(
            'GenSigInactiveValue' in (getattr(getattr(signal, 'dbc', None), 'attributes', None) or {})
            for signal in signals
        )
        for signal in signals:
            value = self.values.get(signal.name)
            if defined:
                inactive = _number(_attribute(signal, 'GenSigInactiveValue', self._defaults)) or 0.0
                raw = _raw(signal, value)
                if raw is not None and round(raw) != round(inactive):
                    return True
            elif value != self.default_values.get(signal.name):
                return True
        return False

    @property
    def data(self) -> bytes:
        """The payload of the current values (without advancing counters, pages or E2E)."""
        return _encode(self.message, self.values)

    def _can_message(self, data: bytes) -> can.Message:
        return can.Message(
            arbitration_id=self.message.frame_id,
            is_extended_id=self.message.is_extended_frame,
            is_fd=self.is_fd,
            bitrate_switch=self.bitrate_switch,
            data=data,
        )

    def static_frame(self) -> can.Message:
        """A frame of the current values, without advancing the per-frame state."""
        return self._can_message(self.data)

    def next_frame(self) -> can.Message:
        """The next frame to send: counters counted, the next multiplexer page, checksums and E2E computed (the
        caller secures a SecOC PDU)."""
        values = dict(self.values)
        for name, role in self.roles.items():
            if role.role == COUNTER:
                signal = self.message.get_signal_by_name(name)
                low, high = _raw_range(signal)
                raw = self._counters.get(name, low)
                values[name] = _physical(signal, raw)
                self._counters[name] = low if raw >= high else raw + 1
        if self.multiplexer is not None and self.selected_pages:
            self._page_index %= len(self.selected_pages)
            values[self.multiplexer.name] = self.selected_pages[self._page_index]
            self._page_index += 1
        data = bytearray(_encode(self.message, values))
        for name, role in self.roles.items():
            if role.role == CHECKSUM:
                index = whole_byte(self.message.get_signal_by_name(name))
                if index is not None and index < len(data):
                    others = bytes(data[:index] + data[index + 1 :])
                    data[index] = checksum(role.algorithm, others, role.constant)
        frame = bytes(data)
        if self.e2e is not None and not self.e2e.problem:
            try:
                frame = self.e2e.protect(frame, self._e2e_counter)
                self._e2e_counter = (self._e2e_counter + 1) % self.e2e.counter_modulus
            except ValueError as error:
                logger.debug('Restbus: E2E of %s not computed: %s', self.message.name, error)
        return self._can_message(frame)

    @property
    def note(self) -> str:
        """Something to know about the frames that does not stop them, e.g. why E2E is not computed."""
        if self.e2e is not None and self.e2e.problem:
            return f'{self.e2e.label} not computed: {self.e2e.problem}'
        return ''

    def set_value(self, signal: str, value: object) -> None:
        self.values[signal] = value

    def set_role(self, signal: str, role: SignalRole) -> str:
        """Make a signal a plain value, a counter or a checksum. Returns why it cannot be, or ''."""
        try:
            definition = self.message.get_signal_by_name(signal)
        except KeyError:
            return f'{self.message.name} has no signal {signal}'
        if role.role not in ROLES:
            return f'unknown role {role.role!r}'
        if role.role == CHECKSUM:
            if role.algorithm not in CHECKSUMS:
                return f'unknown checksum algorithm {role.algorithm!r}'
            if whole_byte(definition) is None:
                return f'{signal} is not one whole byte; checksums are computed for byte-aligned 8-bit signals'
        if role.role != VALUE and (definition.is_multiplexer or signal in self.computed_signals):
            return f'{signal} is computed already'
        if role.role == VALUE:
            self.roles.pop(signal, None)
        else:
            self.roles[signal] = role
        self._counters.pop(signal, None)
        return ''

    def set_pages(self, pages: Sequence[int]) -> None:
        self.selected_pages = [page for page in self.pages if page in set(pages)]
        self._page_index = 0

    # Configuration

    def to_dict(self) -> Dict[str, Any]:
        """What differs from the defaults (for a workspace)."""
        config: Dict[str, Any] = {}
        if self.enabled != (self.sent_by_nodes and self.has_cycle_time and self.database_mode != ON_CHANGE):
            config['enabled'] = self.enabled
        if self.mode != self.database_mode:
            config['mode'] = self.mode
        values = {name: value for name, value in self.values.items() if value != self.default_values.get(name)}
        if values:
            config['values'] = values
        if self.roles != self.default_roles:
            config['roles'] = {
                signal.name: self.roles.get(signal.name, SignalRole()).to_dict()
                for signal in self.message.signals
                if self.roles.get(signal.name) != self.default_roles.get(signal.name)
            }
        if self.selected_pages != self.pages:
            config['pages'] = list(self.selected_pages)
        return config

    def apply(self, config: Dict[str, Any]) -> List[str]:
        problems: List[str] = []
        if 'enabled' in config:
            self.enabled = bool(config['enabled'])
        if config.get('mode') in MODES:
            self.mode = config['mode']
        names = {signal.name for signal in self.message.signals}
        for signal, value in (config.get('values') or {}).items():
            if signal in names:
                self.values[signal] = value
            else:
                problems.append(f'{self.name}: no signal {signal}')
        for signal, role in (config.get('roles') or {}).items():
            try:
                problem = self.set_role(
                    signal,
                    SignalRole(
                        str(role.get('role', VALUE)),
                        str(role.get('algorithm', 'crc8_sae_j1850')),
                        int(role.get('constant', 0)),
                    ),
                )
            except (AttributeError, TypeError, ValueError) as error:
                problem = f'{signal}: {error}'
            if problem:
                problems.append(f'{self.name}: {problem}')
        if 'pages' in config:
            self.set_pages([int(page) for page in config['pages']])
        return problems


class RestbusSimulator:
    """Drives the transmission of a set of database messages on a session."""

    def __init__(
        self,
        session: BusSession,
        messages,
        default_cycle_ms: int = DEFAULT_CYCLE_MS,
        secure: Optional[Callable[[can.Message], can.Message]] = None,
        attribute_defaults: Optional[Dict[str, Any]] = None,
        bitrate_switch: bool = False,
    ):
        """`attribute_defaults` are the defaults of the DBC attribute definitions (by name); `bitrate_switch` is
        used for CAN FD messages without a CANFD_BRS attribute (e.g. True when the connection has a data
        bitrate)."""
        self.session = session
        self.secure = secure  # Secures the frames of SecOC secured PDUs
        self.default_period = default_cycle_ms / 1000.0
        self.hardware = False  # Time the frames that never change with the interface's periodic transmission
        self._messages: Dict[IdKey, RestbusMessage] = {}
        for message in messages:
            period = message.cycle_time / 1000.0 if message.cycle_time else self.default_period
            entry = RestbusMessage(message, period, False, attribute_defaults, bitrate_switch)
            entry.enabled = entry.sent_by_nodes and entry.has_cycle_time and entry.mode != ON_CHANGE
            self._messages[entry.key] = entry
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def messages(self) -> List[RestbusMessage]:
        return list(self._messages.values())

    def get(self, key: IdKey) -> Optional[RestbusMessage]:
        return self._messages.get(key)

    def by_name(self, name: str) -> Optional[RestbusMessage]:
        return next((entry for entry in self._messages.values() if entry.name == name), None)

    @property
    def problems(self) -> Dict[IdKey, str]:
        """The enabled messages that could not start or stopped, and why."""
        return {key: problem for key, entry in self._messages.items() if (problem := self.problem_of(entry))}

    def problem_of(self, entry: RestbusMessage) -> str:
        if entry.problem:
            return entry.problem
        transmit = entry._transmit
        if transmit is not None and transmit.finished and transmit.error:
            return f'stopped: {transmit.error}'
        return ''

    def state_of(self, entry: RestbusMessage) -> str:
        """'off', 'ready' (enabled, not started), 'sending', 'on change' (waiting for a change), 'inactive'
        (if-active and inactive) or 'stopped' (could not start, or ended by an error)."""
        if not entry.enabled:
            return 'off'
        if not self._running:
            return 'ready'
        if self.problem_of(entry):
            return 'stopped'
        if entry.mode == IF_ACTIVE and not entry.active:
            return 'inactive'
        if entry.mode == ON_CHANGE:
            return 'on change'
        return 'sending' if entry.running else 'stopped'

    @property
    def enabled_count(self) -> int:
        return sum(1 for entry in self._messages.values() if entry.enabled)

    def expected_load(self, bitrate: Optional[int], data_bitrate: Optional[int] = None) -> Optional[float]:
        """The share of the bus the enabled periodic messages take (0..1, without bit stuffing, which adds up to
        ~20%; if-active messages at their active period, on-change ones not at all); None without a bitrate."""
        if not bitrate:
            return None
        load = 0.0
        for entry in self._messages.values():
            if not entry.enabled or entry.mode == ON_CHANGE:
                continue
            period = entry.active_period if entry.mode == IF_ACTIVE else entry.period
            if period > 0:
                load += frame_duration(entry.static_frame(), bitrate, data_bitrate) / period
        return load

    # Choosing what is sent

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

    def set_mode(self, key: IdKey, mode: str) -> None:
        entry = self._messages.get(key)
        if entry is None or mode not in MODES or entry.mode == mode:
            return
        entry.mode = mode
        self._restart(entry)

    def set_default_period(self, seconds: float) -> None:
        """The period used for messages without a cycle time; restarts those already running with the new period."""
        self.default_period = seconds
        for entry in self._messages.values():
            if not entry.has_cycle_time:
                entry.period = seconds
                self._restart(entry)

    def set_hardware(self, hardware: bool) -> None:
        """Time the frames that never change with the interface (where it supports it); restarts them."""
        if hardware == self.hardware:
            return
        self.hardware = hardware
        for entry in self._messages.values():
            if not entry.dynamic:
                self._restart(entry)

    def exclude_sender(self, node: str) -> int:
        """Disable every message sent by `node` (the ECU under test); returns how many were disabled."""
        count = 0
        for entry in self._messages.values():
            if node in entry.senders and entry.enabled:
                self.set_enabled(entry.key, False)
                count += 1
        return count

    # Values

    def set_value(self, key: IdKey, signal: str, value: object) -> None:
        entry = self._messages.get(key)
        if entry is None:
            return
        entry.set_value(signal, value)
        if not (self._running and entry.enabled):
            return
        if entry.mode == IF_ACTIVE:
            if entry.active and not entry.running:
                self._start_entry(entry)
            elif not entry.active and entry.running:
                self._stop_entry(entry)
        if entry._transmit is not None and not entry.dynamic:
            if entry._transmit.hardware:
                self._restart(entry)  # The hardware repeats one fixed frame
            else:
                entry._transmit.message = entry.static_frame()  # The next tick sends the new payload
        if entry.on_change:
            self.send_now(key)

    def set_role(self, key: IdKey, signal: str, role: SignalRole) -> str:
        entry = self._messages.get(key)
        if entry is None:
            return 'unknown message'
        was_dynamic = entry.dynamic
        problem = entry.set_role(signal, role)
        if not problem and entry.dynamic != was_dynamic:
            self._restart(entry)  # Between per-frame and fixed frames (hardware or software timing)
        return problem

    def set_pages(self, key: IdKey, pages: Sequence[int]) -> None:
        entry = self._messages.get(key)
        if entry is not None:
            was_dynamic = entry.dynamic
            entry.set_pages(pages)
            if entry.dynamic != was_dynamic:
                self._restart(entry)

    def send_now(self, key: IdKey) -> str:
        """Send a message at once, with its repetitions (what an on-change message does when a value changes).
        Returns an error, or ''."""
        entry = self._messages.get(key)
        if entry is None:
            return 'unknown message'
        if not self.session.is_open:
            return 'Connect to the bus first'
        if entry._burst is not None and not entry._burst.done():
            entry._burst.cancel()  # A new change restarts the repetitions
        entry._burst = asyncio.get_running_loop().create_task(self._send_burst(entry), name=f'Restbus {entry.name}')
        return ''

    async def _send_burst(self, entry: RestbusMessage) -> None:
        loop = asyncio.get_running_loop()
        count = 1 + entry.repetitions
        try:
            for index in range(count):
                wait = entry._last_sent + entry.min_gap - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.session.send(self._frame(entry))
                entry._last_sent = loop.time()
                if index + 1 < count:
                    await asyncio.sleep(entry.repetition_gap)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a session error, or a frame that cannot be secured
            entry.problem = str(error) or type(error).__name__
            logger.warning('Restbus: could not send %s: %s', entry.name, error)

    def _frame(self, entry: RestbusMessage) -> can.Message:
        frame = entry.next_frame()
        if entry.secured:
            if self.secure is None:
                raise SecOcError('secured PDU (SecOC), but no security manager to secure it')
            frame = self.secure(frame)
        return frame

    # Running

    def start(self) -> List[str]:
        """Begin transmitting every enabled message. Requires the session to be open. Returns why messages could
        not start (e.g. secured PDUs without a key); the others run."""
        if self._running:
            return []
        self._running = True
        offset = 0
        for entry in self._messages.values():
            entry.problem = ''
            if not entry.enabled:
                continue
            period = entry.active_period if entry.mode == IF_ACTIVE else entry.period
            delay = entry.start_delay
            if entry.mode != ON_CHANGE and period > 0:
                delay += (offset * STAGGER_MS / 1000.0) % period
                offset += 1
            self._start_entry(entry, delay)
        return [f'{self._messages[key].name}: {problem}' for key, problem in self.problems.items()]

    def stop(self) -> None:
        if not self._running:
            return
        for entry in self._messages.values():
            self._stop_entry(entry)
        self._running = False

    def _restart(self, entry: RestbusMessage) -> None:
        if self._running and entry.enabled:
            self._stop_entry(entry)
            self._start_entry(entry)

    def _start_entry(self, entry: RestbusMessage, delay: float = 0.0) -> None:
        if entry.running:
            return
        entry.problem = ''
        if entry.secured:
            if self.secure is None:
                entry.problem = 'secured PDU (SecOC), but no security manager to secure it'
                return
            try:
                self.secure(entry.static_frame())  # Fails now, not on every period, without a key
            except SecOcError as error:
                entry.problem = str(error)
                logger.warning('Restbus: not sending %s: %s', entry.name, error)
                return
        if entry.mode == ON_CHANGE or (entry.mode == IF_ACTIVE and not entry.active):
            return  # Sent when a value changes, or once it becomes active
        period = entry.active_period if entry.mode == IF_ACTIVE else entry.period
        if self.hardware and not entry.dynamic:
            if delay > 0:
                entry._pending = asyncio.get_running_loop().call_later(delay, self._start_hardware, entry, period)
                return
            self._start_hardware(entry, period)
            return
        self._start_software(entry, period, delay)

    def _start_software(self, entry: RestbusMessage, period: float, delay: float = 0.0) -> None:
        entry.hardware_timed = False
        prepare = (lambda _msg: self._frame(entry)) if entry.dynamic else None
        try:
            entry._transmit = self.session.start_periodic(entry.static_frame(), period, prepare=prepare, delay=delay)
        except (SessionError, ValueError) as error:
            entry.problem = str(error)
            logger.warning('Restbus: could not start %s: %s', entry.name, error)

    def _start_hardware(self, entry: RestbusMessage, period: float) -> None:
        entry._pending = None
        try:
            entry._transmit = self.session.start_periodic(entry.static_frame(), period, hardware=True)
            entry.hardware_timed = True
        except SessionError as error:  # The interface has no periodic transmission: the event loop times it
            logger.info('Restbus: %s timed in software: %s', entry.name, error)
            self._start_software(entry, period)

    def _stop_entry(self, entry: RestbusMessage) -> None:
        if entry._pending is not None:
            entry._pending.cancel()
            entry._pending = None
        if entry._burst is not None:
            entry._burst.cancel()
            entry._burst = None
        if entry._transmit is not None:
            try:
                entry._transmit.stop()
            except Exception:  # noqa: BLE001 - stopping must not raise
                pass
            entry._transmit = None
        entry.hardware_timed = False

    # Configuration

    def to_dict(self) -> Dict[str, Any]:
        """The configuration that differs from the database's defaults, by message name (for a workspace)."""
        messages = {entry.name: config for entry in self._messages.values() if (config := entry.to_dict())}
        return {'default_cycle_ms': round(self.default_period * 1000), 'hardware': self.hardware, 'messages': messages}

    def apply(self, config: Dict[str, Any]) -> List[str]:
        """Apply a configuration from `to_dict` (while stopped). Returns problems, e.g. messages the database does
        not have."""
        problems: List[str] = []
        if config.get('default_cycle_ms'):
            self.set_default_period(max(1, int(config['default_cycle_ms'])) / 1000.0)
        self.set_hardware(bool(config.get('hardware', False)))
        for name, message_config in (config.get('messages') or {}).items():
            entry = self.by_name(name)
            if entry is None:
                problems.append(f'restbus: the database has no message {name}')
                continue
            problems += [f'restbus: {problem}' for problem in entry.apply(message_config or {})]
        return problems
