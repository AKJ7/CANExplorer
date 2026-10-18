from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

import can
import cantools

from can_explorer.core.database_io import READ_SUFFIXES, load_database
from can_explorer.core.frames import Frame
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
            return f'{definition.name} [{"; ".join(texts)}]'
        return texts[0]


class FrameDecoder:
    """Combines the selected protocol decoder with an optional signal database.

    The protocol decoder runs for every frame on arrival (it is stateful, e.g. reassembling transfers).
    The database is stateless, so frames are only decoded with it when displayed, and the result is cached.
    """

    def __init__(self, protocol: Optional[ProtocolDecoder] = None, database: Optional[DbcDecoder] = None):
        self.protocol = protocol
        self._database = database
        self._generation = 0

    @property
    def database(self) -> Optional[DbcDecoder]:
        return self._database

    @database.setter
    def database(self, database: Optional[DbcDecoder]) -> None:
        self._database = database
        self._generation += 1  # Invalidates the descriptions cached in the frames

    def describe(self, msg: can.Message) -> str:
        """Protocol description of a frame, called once per frame in arrival order.

        Remote frames are passed on: LIN carries headers as RTR frames, CANopen uses them for node guarding.
        """
        if self.protocol is None or msg.is_error_frame:
            return ''
        try:
            return self.protocol.describe(msg) or ''
        except Exception as e:
            logger.debug(f'Decoder {self.protocol} failed on {msg}: {e}')
            return ''

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


def open_signal_database(path: str | Path):
    """Signal database for decoding: DbcDecoder (DBC, DBF, KCD, SYM, ARXML) or LdfDecoder (LIN LDF)."""
    if Path(path).suffix.lower() == '.ldf':
        from can_explorer.core.ldf import LdfDecoder

        return LdfDecoder(path)
    return DbcDecoder(path)
