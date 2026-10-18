from pathlib import Path
from typing import Dict, Optional, Protocol

import can
import cantools

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

DATABASE_SUFFIXES = ('.dbc', '.kcd', '.sym', '.arxml')


class ProtocolDecoder(Protocol):
    name: str

    def describe(self, msg: can.Message) -> Optional[str]: ...

    def reset(self) -> None: ...


class DbcDecoder:
    """Decodes frames into signal values using a cantools database (DBC, KCD, SYM or ARXML)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.database = cantools.database.load_file(str(self.path), strict=False)
        self._lookup: Dict[tuple, Optional[cantools.database.Message]] = {}

    def _message(self, msg: can.Message) -> Optional['cantools.database.Message']:
        key = (msg.arbitration_id, msg.is_extended_id)
        if key not in self._lookup:
            try:
                definition = self.database.get_message_by_frame_id(msg.arbitration_id)
            except KeyError:
                definition = None
            if definition is not None and definition.is_extended_frame != msg.is_extended_id:
                definition = None
            self._lookup[key] = definition
        return self._lookup[key]

    @staticmethod
    def _format_value(value) -> str:
        if isinstance(value, float):
            return f'{value:g}'
        return str(value)

    def decode(self, msg: can.Message) -> Optional[Dict[str, object]]:
        definition = self._message(msg)
        if definition is None:
            return None
        return definition.decode(bytes(msg.data), decode_choices=True, allow_truncated=True)

    def describe(self, msg: can.Message) -> Optional[str]:
        definition = self._message(msg)
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
    """Combines the selected protocol decoder with an optional signal database."""

    def __init__(self, protocol: Optional[ProtocolDecoder] = None, database: Optional[DbcDecoder] = None):
        self.protocol = protocol
        self.database = database

    def describe(self, msg: can.Message) -> str:
        if msg.is_error_frame or msg.is_remote_frame:
            return ''
        parts = []
        for decoder in (self.protocol, self.database):
            if decoder is None:
                continue
            try:
                description = decoder.describe(msg)
            except Exception as e:
                logger.debug(f'Decoder {decoder} failed on {msg}: {e}')
                description = None
            if description:
                parts.append(description)
        return ' | '.join(parts)

    def reset(self) -> None:
        if self.protocol is not None:
            self.protocol.reset()
