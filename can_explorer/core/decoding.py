import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

import can
import cantools

from can_explorer.core.database_io import READ_SUFFIXES, load_database
from can_explorer.core.e2e_check import E2eConfig, e2e_config_of
from can_explorer.core.frames import Frame
from can_explorer.core.secoc_check import SecOcCheck, SecOcConfig, SecOcError, SecOcVerifier, secoc_config_of
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DATABASE_SUFFIXES = READ_SUFFIXES + ('.ldf',)


class ProtocolDecoder(Protocol):
    name: str

    def describe(self, msg: can.Message) -> Optional[str]: ...

    def reset(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SignalInfo:
    message: str
    signal: str
    unit: str

    @property
    def key(self) -> str:
        return f'{self.message}.{self.signal}'


def contained_parts(definition, data: bytes) -> List[Tuple[object, bytes]]:
    """(contained message, its bytes) of a container frame, for the contained PDUs the database knows."""
    try:
        parts = definition.unpack_container(bytes(data), allow_truncated=True)
    except Exception:  # noqa: BLE001 - a malformed container: nothing to check
        return []
    return [(contained, bytes(raw)) for contained, raw in parts if not isinstance(contained, int)]


def _with_contained(messages) -> List[object]:
    """The messages and the PDUs contained in container messages."""
    result = []
    for message in messages:
        result.append(message)
        result.extend(message.contained_messages or [])
    return result


class DbcDecoder:
    """Decodes frames into signal values using a signal database (DBC, BUSMASTER DBF, KCD, SYM or ARXML).

    Databases describing several buses (ARXML clusters, KCD buses) may reuse identifiers on different buses.
    Select the bus of the connection with `bus`; without it, the first definition of an identifier is used.
    Container frames (ARXML container I-PDUs) are decoded into their contained PDUs.
    """

    def __init__(self, path: str | Path, bus: Optional[str] = None):
        self.path = Path(path)
        self.database, self.warnings = load_database(self.path)
        for warning in self.warnings:
            logger.warning(f'{self.path.name}: {warning}')
        self.buses: List[str] = [bus.name for bus in self.database.buses]
        self._bus: Optional[str] = None
        self._index: Dict[Tuple[int, bool], 'cantools.database.Message'] = {}
        self.set_bus(bus)

    @property
    def bus(self) -> Optional[str]:
        return self._bus

    def set_bus(self, bus: Optional[str]) -> None:
        """Restrict decoding to the messages of one bus (None: all buses)."""
        if bus is not None and bus not in self.buses:
            raise ValueError(f'Unknown bus {bus!r}; the database defines {", ".join(self.buses) or "no buses"}')
        self._bus = bus
        self._index = {}
        conflicts = set()
        for message in self.messages():
            key = (message.frame_id, message.is_extended_frame)
            if key in self._index:
                conflicts.add(key)
                continue
            self._index[key] = message
        if conflicts:
            identifiers = ', '.join(f'0x{frame_id:X}' for frame_id, _ in sorted(conflicts))
            logger.warning(
                f'{self.path.name}: identifiers {identifiers} are defined on several buses '
                f'({", ".join(self.buses)}); select the bus of the connection to decode them correctly'
            )

    @property
    def conflicting_identifiers(self) -> bool:
        seen = set()
        for message in self.messages():
            key = (message.frame_id, message.is_extended_frame)
            if key in seen:
                return True
            seen.add(key)
        return False

    def messages(self) -> List['cantools.database.Message']:
        return [message for message in self.database.messages if self._bus is None or message.bus_name == self._bus]

    def message_definition(self, msg: can.Message) -> Optional['cantools.database.Message']:
        return self._index.get((msg.arbitration_id, msg.is_extended_id))

    def signals(self) -> List[SignalInfo]:
        result = []
        for message in sorted(self.messages(), key=lambda message: message.name):
            for part in message.contained_messages or [message]:
                result += [SignalInfo(part.name, signal.name, signal.unit or '') for signal in part.signals]
        return result

    @staticmethod
    def _format_value(value) -> str:
        if isinstance(value, float):
            return f'{value:g}'
        return str(value)

    @staticmethod
    def decode_message(definition, data: bytes, decode_choices: bool = True) -> List[Tuple[str, Dict[str, object]]]:
        """Decode a frame into [(message or contained PDU name, signal values)]."""
        data = bytes(data)
        if definition.is_container:
            parts = definition.decode(data, decode_choices=decode_choices, decode_containers=True, allow_truncated=True)
            result = []
            for contained, values in parts:
                if isinstance(contained, int):  # PDU with an unknown header identifier
                    if contained == 0 and not any(values):
                        continue  # Zero padding after the last contained PDU
                    result.append((f'PDU 0x{contained:06X}', {}))
                else:
                    result.append((contained.name, values))
            return result
        return [(definition.name, definition.decode(data, decode_choices=decode_choices, allow_truncated=True))]

    def decode(self, msg: can.Message, decode_choices: bool = True) -> Optional[Dict[str, object]]:
        definition = self.message_definition(msg)
        if definition is None:
            return None
        values: Dict[str, object] = {}
        for _, part_values in self.decode_message(definition, msg.data, decode_choices):
            values.update(part_values)
        return values

    def _units(self, definition) -> Dict[str, str]:
        units = {}
        for part in definition.contained_messages or [definition]:
            units.update({signal.name: signal.unit for signal in part.signals if signal.unit})
        return units

    def describe(self, msg: can.Message) -> Optional[str]:
        definition = self.message_definition(msg)
        if definition is None:
            return None
        try:
            parts = self.decode_message(definition, msg.data)
        except Exception as e:
            return f'{definition.name}: <cannot decode: {e}>'
        units = self._units(definition)
        texts = []
        for name, values in parts:
            signals = ', '.join(
                f'{signal}={self._format_value(value)}{" " + units[signal] if signal in units else ""}'
                for signal, value in values.items()
            )
            texts.append(f'{name}: {signals}' if signals else name)
        if definition.is_container:
            # AUTOSAR E2E protection of the contained PDUs (a container itself is never protected)
            checks = {}
            for contained, raw in contained_parts(definition, msg.data):
                if contained.name not in checks and (e2e := e2e_config_of(contained)) is not None:
                    checks[contained.name] = e2e.describe(raw)
            texts = [
                f'{text} [{checks[name]}]' if name in checks else text
                for text, (name, _values) in zip(texts, parts, strict=True)
            ]
            return f'{definition.name} [{"; ".join(texts)}]'
        # AUTOSAR E2E protection from an ARXML
        if (e2e := e2e_config_of(definition)) is not None:
            return f'{texts[0]} [{e2e.describe(msg.data)}]'
        return texts[0]

    def e2e_protected(self) -> List[Tuple[str, E2eConfig]]:
        """The messages with an AUTOSAR E2E protection (from an ARXML), with their configuration."""
        return [
            (message.name, config)
            for message in _with_contained(self.messages())
            if (config := e2e_config_of(message)) is not None
        ]

    def secoc_protected(self) -> List[SecOcConfig]:
        """The secured PDUs (AUTOSAR SecOC, from an ARXML) with their configuration."""
        return [
            config for message in _with_contained(self.messages()) if (config := secoc_config_of(message)) is not None
        ]


class FrameDecoder:
    """Combines the selected protocol decoder with an optional signal database.

    The protocol decoder runs for every frame on arrival (it is stateful, e.g. reassembling transfers).
    The database is stateless, so frames are only decoded with it when displayed, and the result is cached.
    """

    def __init__(
        self,
        protocol: Optional[ProtocolDecoder] = None,
        database: Optional[DbcDecoder] = None,
        secoc: Optional[SecOcVerifier] = None,
    ):
        self.protocol = protocol
        self._database = database
        self._generation = 0
        # SecOC checks of secured PDUs, run on arrival (the freshness values follow the frame order)
        self.secoc = secoc
        self._secured: Dict[Tuple[int, bool], SecOcConfig] = self._secured_pdus(database)
        self._secured_containers = self._containers_with_secured_pdus(database)
        # The SecOC check of each frame still referenced elsewhere (e.g. by a test step waiting for it)
        self._secoc_results: 'weakref.WeakKeyDictionary[can.Message, SecOcCheck]' = weakref.WeakKeyDictionary()

    @staticmethod
    def _containers_with_secured_pdus(database) -> Dict[Tuple[int, bool], object]:
        """The container messages carrying secured PDUs, by identifier."""
        if not hasattr(database, 'secoc_protected'):
            return {}
        return {
            (message.frame_id, message.is_extended_frame): message
            for message in database.messages()
            if message.is_container and any(secoc_config_of(part) for part in message.contained_messages or [])
        }

    @staticmethod
    def _secured_pdus(database) -> Dict[Tuple[int, bool], SecOcConfig]:
        if not hasattr(database, 'secoc_protected'):  # SecOC is configured in ARXML (DbcDecoder) only
            return {}
        secured = {}
        for message in database.messages():
            config = secoc_config_of(message)
            if config is not None:
                secured.setdefault((message.frame_id, message.is_extended_frame), config)
        return secured

    @property
    def database(self) -> Optional[DbcDecoder]:
        return self._database

    @property
    def secured_pdus(self) -> Dict[Tuple[int, bool], SecOcConfig]:
        """The SecOC configuration of the secured PDUs of the database (an ARXML), by identifier."""
        return dict(self._secured)

    @database.setter
    def database(self, database: Optional[DbcDecoder]) -> None:
        self._database = database
        self._generation += 1  # Invalidates the descriptions cached in the frames
        self._secured = self._secured_pdus(database)
        self._secured_containers = self._containers_with_secured_pdus(database)
        if self.secoc is not None:
            self.secoc.reset()

    def describe(self, msg: can.Message) -> str:
        """Protocol description of a frame, called once per frame in arrival order.

        Remote frames are passed on: LIN carries headers as RTR frames, CANopen uses them for node guarding.
        """
        if msg.is_error_frame:
            from can_explorer.core.bushealth import decode_error_frame

            return decode_error_frame(msg).describe()
        text = ''
        if self.protocol is not None:
            try:
                text = self.protocol.describe(msg) or ''
            except Exception as e:
                logger.debug(f'Decoder {self.protocol} failed on {msg}: {e}')
        if self.secoc is not None and not msg.is_remote_frame:
            key = (msg.arbitration_id, msg.is_extended_id)
            config = self._secured.get(key)
            if config is not None:
                result = self.secoc.check(config, bytes(msg.data))
                self._secoc_results[msg] = result
                check = f'[{result.describe()}]'
                text = f'{text} {check}' if text else check
            elif key in self._secured_containers:  # Secured PDUs inside a container frame
                checks = []
                for contained, raw in contained_parts(self._secured_containers[key], msg.data):
                    contained_config = secoc_config_of(contained)
                    if contained_config is None:
                        continue
                    result = self.secoc.check(contained_config, raw)
                    if msg not in self._secoc_results or self._secoc_results[msg].ok:
                        self._secoc_results[msg] = result  # The first failure counts for the frame
                    checks.append(f'{contained.name}: {result.describe()}')
                if checks:
                    check = f'[{"; ".join(checks)}]'
                    text = f'{text} {check}' if text else check
        return text

    def secoc_result(self, msg: can.Message) -> Optional[SecOcCheck]:
        """The SecOC check of a received frame (None for frames of no secured PDU, or without a verifier)."""
        return self._secoc_results.get(msg)

    def secured_config(self, arbitration_id: int, is_extended_id: bool) -> Optional[SecOcConfig]:
        """The SecOC configuration of a secured PDU of the database with this identifier, if any."""
        return self._secured.get((arbitration_id, is_extended_id))

    def secure(self, msg: can.Message) -> can.Message:
        """`msg` with its payload secured (new freshness value and MAC) when it is a secured PDU of the database;
        other frames are returned unchanged. Raises SecOcError without a key or a usable configuration."""
        config = self._secured.get((msg.arbitration_id, msg.is_extended_id))
        if config is None or msg.is_remote_frame:
            return msg
        if self.secoc is None:
            raise SecOcError(f'Cannot secure {config.pdu}: no security manager')
        data = self.secoc.secure(config, bytes(msg.data))
        msg.data = bytearray(data)
        msg.dlc = len(data)
        return msg

    def full_description(self, frame: Frame) -> str:
        """Protocol and signal database description of a frame, for display."""
        database = self._database
        if database is None:
            return frame.info
        cached = frame.details
        if cached is not None and cached[0] == self._generation:
            return cached[1]
        msg = frame.message
        signals = None
        if not (msg.is_error_frame or msg.is_remote_frame):
            try:
                signals = database.describe(msg)
            except Exception as e:
                logger.debug(f'Database decoding failed on {msg}: {e}')
        text = ' | '.join(part for part in (frame.info, signals) if part)
        frame.details = (self._generation, text)
        return text

    def reset(self) -> None:
        if self.protocol is not None:
            self.protocol.reset()
        if self.secoc is not None:
            self.secoc.reset()


def open_signal_database(path: str | Path):
    """Signal database for decoding: DbcDecoder (DBC, DBF, KCD, SYM, ARXML) or LdfDecoder (LIN LDF)."""
    if Path(path).suffix.lower() == '.ldf':
        from can_explorer.core.ldf import LdfDecoder

        return LdfDecoder(path)
    return DbcDecoder(path)
