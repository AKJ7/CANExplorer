"""Signal decoding with LIN description files (LDF), exposing the same interface as DbcDecoder."""

from pathlib import Path
from typing import Dict, List, Optional

import can
import ldfparser

from can_explorer.core.lin import MAX_FRAME_ID, is_lin_frame
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


class LdfFrameDefinition:
    """Adapts an ldfparser frame to the subset of cantools' Message used by the signal tracker."""

    def __init__(self, frame, ldf):
        self._frame = frame
        self._ldf = ldf
        self.name = frame.name
        self.frame_id = frame.frame_id
        self.length = frame.length

    @property
    def signal_names(self) -> List[str]:
        return [signal.name for _, signal in self._frame.signal_map]

    def decode(self, data: bytes, decode_choices: bool = True, allow_truncated: bool = True) -> Dict[str, object]:
        data = bytearray(data[: self.length]).ljust(self.length, b'\x00') if allow_truncated else bytearray(data)
        if decode_choices:
            return self._frame.decode(data, keep_unit=False)
        # Numeric values for plotting: physical values where defined, raw values for logical encodings
        values = self._frame.decode(data, keep_unit=False)
        raw = self._frame.decode_raw(data)
        return {name: value if isinstance(value, (int, float)) else raw[name] for name, value in values.items()}

    def encode(self, values: Dict[str, object]) -> bytes:
        return bytes(self._frame.encode(values))


class LdfDecoder:
    """LDF based signal decoder for LIN connections."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.ldf = ldfparser.parse_ldf(str(self.path))
        self.warnings: List[str] = []
        self._definitions: Dict[int, LdfFrameDefinition] = {}
        for frame in self.ldf.get_unconditional_frames():
            self._definitions[frame.frame_id] = LdfFrameDefinition(frame, self.ldf)

    @property
    def baudrate(self) -> int:
        return self.ldf.get_baudrate()

    @property
    def master(self) -> str:
        return self.ldf.get_master().name

    def frame(self, name: str) -> Optional[LdfFrameDefinition]:
        return next((definition for definition in self._definitions.values() if definition.name == name), None)

    def message_definition(self, msg: can.Message) -> Optional[LdfFrameDefinition]:
        if not is_lin_frame(msg) or msg.is_remote_frame or msg.arbitration_id > MAX_FRAME_ID:
            return None
        return self._definitions.get(msg.arbitration_id)

    def signals(self):
        from can_explorer.core.decoding import SignalInfo

        result = []
        for definition in sorted(self._definitions.values(), key=lambda definition: definition.name):
            for name in definition.signal_names:
                result.append(SignalInfo(definition.name, name, self.unit(name)))
        return result

    def unit(self, signal: str) -> str:
        encoding = self.ldf.get_signal(signal).encoding_type
        if encoding is None:
            return ''
        for converter in encoding.get_converters():
            if getattr(converter, 'unit', None):
                return converter.unit
        return ''

    def master_signals(self) -> List[Dict[str, object]]:
        """Signals the master publishes, with their initial values, for the schedule editor."""
        result = []
        for definition in sorted(self._definitions.values(), key=lambda definition: definition.name):
            if definition._frame.publisher.name != self.master:
                continue
            for name in definition.signal_names:
                signal = self.ldf.get_signal(name)
                result.append(
                    {'frame': definition.name, 'signal': name, 'value': signal.init_value, 'unit': self.unit(name)}
                )
        return result

    def decode(self, msg: can.Message, decode_choices: bool = True) -> Optional[Dict[str, object]]:
        definition = self.message_definition(msg)
        return None if definition is None else definition.decode(bytes(msg.data), decode_choices=decode_choices)

    def describe(self, msg: can.Message) -> Optional[str]:
        definition = self.message_definition(msg)
        if definition is None:
            return None
        try:
            values = definition._frame.decode(bytearray(msg.data).ljust(definition.length, b'\x00'), keep_unit=True)
        except Exception as e:
            return f'{definition.name}: <cannot decode: {e}>'
        return f'{definition.name}: ' + ', '.join(f'{name}={value}' for name, value in values.items())
