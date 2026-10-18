from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol

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
    """Decodes frames into signal values using a signal database (DBC, BUSMASTER DBF, KCD, SYM or ARXML)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.database, self.warnings = load_database(self.path)
        for warning in self.warnings:
            logger.warning(f'{self.path.name}: {warning}')
        self._lookup: Dict[tuple, Optional[cantools.database.Message]] = {}

    def message_definition(self, msg: can.Message) -> Optional['cantools.database.Message']:
        key = (msg.arbitration_id, msg.is_extended_id)
        try:
            return self._lookup[key]
        except KeyError:
            pass
        try:
            definition = self.database.get_message_by_frame_id(msg.arbitration_id)
        except KeyError:
            definition = None
        if definition is not None and definition.is_extended_frame != msg.is_extended_id:
            definition = None
        self._lookup[key] = definition
        return definition

    def signals(self) -> List[SignalInfo]:
        return [
            SignalInfo(message.name, signal.name, signal.unit or '')
            for message in sorted(self.database.messages, key=lambda message: message.name)
            for signal in message.signals
        ]

    @staticmethod
    def _format_value(value) -> str:
        if isinstance(value, float):
            return f'{value:g}'
        return str(value)

    def decode(self, msg: can.Message, decode_choices: bool = True) -> Optional[Dict[str, object]]:
        definition = self.message_definition(msg)
        if definition is None:
            return None
        return definition.decode(bytes(msg.data), decode_choices=decode_choices, allow_truncated=True)

    def describe(self, msg: can.Message) -> Optional[str]:
        definition = self.message_definition(msg)
        if definition is None:
            return None
        try:
            values = definition.decode(bytes(msg.data), decode_choices=True, allow_truncated=True)
        except Exception as e:
            return f'{definition.name}: <cannot decode: {e}>'
        parts = []
        for name, value in values.items():
            unit = definition.get_signal_by_name(name).unit
            parts.append(f'{name}={self._format_value(value)}{" " + unit if unit else ""}')
        return f'{definition.name}: {", ".join(parts)}'


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
