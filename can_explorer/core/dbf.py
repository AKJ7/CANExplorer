"""Reader and writer for BUSMASTER signal databases (.dbf), on top of cantools' database model.

Format (BUSMASTER database version 1.3), one item per line::

    [START_MSG] <name>,<id>,<length>,<signal count>,<byte order>,<S|X>,<sender>
    [START_SIGNALS] <name>,<length>,<byte>,<bit>,<type>,<max>,<min>,<byte order>,<offset>,<factor>,<unit>,<multiplex>,<receivers>
    [VALUE_DESCRIPTION] "<text>",<raw value>
    [END_MSG]

- <byte> is 1-based, <bit> the 0-based bit within that byte. For Intel signals they address the least
  significant bit, like the DBC start bit. For Motorola signals they also address the least significant
  bit, whereas DBC addresses the most significant one.
- <byte order>: 1 = Intel (little endian), 0 = Motorola (big endian).
- <type>: U unsigned, I signed, B boolean, F 32-bit float, D 64-bit float.
- <max>/<min> are raw values: physical = raw * factor + offset.
- <multiplex>: "M" for the multiplexer, "m<n>" for signals present when the multiplexer equals n.
- For [PROTOCOL] J1939 the message id is the PGN.

The layout follows the BUSMASTER DBC <-> DBF converters (github.com/rbei-etas/busmaster).
"""

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cantools.database import conversion
from cantools.database.can import Database, Message, Node, Signal

HEADER = '//******************************BUSMASTER Messages and signals Database ******************************//'
DATABASE_VERSION = '1.3'
BUSMASTER_VERSION = '3.2.2'
ENCODING = 'cp1252'

INTEL, MOTOROLA = '1', '0'
J1939_DEFAULT_PRIORITY = 6
J1939_NULL_ADDRESS = 0xFE
UNKNOWN_NODE = 'Vector__XXX'

_VALUE_DESCRIPTION = re.compile(r'^"(?P<text>.*)",\s*(?P<value>[-+]?[0-9.eE+-]+)\s*$')
# Sections that are recognised but whose content cannot be represented in the cantools model
_SKIPPED_SECTIONS = {
    '[START_SIG_LIST]': ('[END_SIG_LIST]', 'signals not assigned to a message'),
    '[START_VALUE_TABLE]': ('[END_VALUE_TABLE]', 'global value tables'),
    '[START_PARAM]': ('[END_PARAM]', 'attribute definitions'),
    '[START_PARAM_VAL]': ('[END_PARAM_VAL]', 'attribute values'),
    '[START_NOT_SUPPORTED]': ('[END_NOT_SUPPORTED]', 'unsupported DBC content'),
    '[START_NOT_PROCESSED]': ('[END_NOT_PROCESSED]', 'unprocessed DBC content'),
}


class DbfError(ValueError):
    def __init__(self, message: str, line: Optional[int] = None):
        super().__init__(f'line {line}: {message}' if line is not None else message)
        self.line = line


# Bit positions


def dbf_position_to_start(byte: int, bit: int, length: int, big_endian: bool) -> int:
    """DBF (1-based byte, bit of the least significant bit) to the DBC/cantools start bit."""
    if byte < 1 or not 0 <= bit <= 7:
        raise ValueError(f'Invalid position: byte {byte}, bit {bit}')
    if not big_endian:
        return (byte - 1) * 8 + bit
    # Motorola: count bits in transmission order (MSB of byte 0 first), step back to the MSB
    lsb = (byte - 1) * 8 + (7 - bit)
    msb = lsb - (length - 1)
    if msb < 0:
        raise ValueError(f'Motorola signal of {length} bits does not fit before byte {byte}, bit {bit}')
    return (msb // 8) * 8 + (7 - msb % 8)


def start_to_dbf_position(start: int, length: int, big_endian: bool) -> Tuple[int, int]:
    """DBC/cantools start bit to DBF (1-based byte, bit of the least significant bit)."""
    if not big_endian:
        return start // 8 + 1, start % 8
    msb = (start // 8) * 8 + (7 - start % 8)
    lsb = msb + length - 1
    return lsb // 8 + 1, 7 - lsb % 8


# Raw <-> physical limits


def _raw_range(signal_type: str, length: int) -> Tuple[float, float]:
    if signal_type in 'FD':
        return (-3.4e38, 3.4e38) if signal_type == 'F' else (-1.7e308, 1.7e308)
    if signal_type == 'I':
        return -(1 << (length - 1)), (1 << (length - 1)) - 1
    return 0, (1 << length) - 1


def _physical(raw: float, scale: float, offset: float) -> float:
    # Round away binary noise such as 1023 * 0.1 - 40 = 62.300000000000004
    value = float(f'{raw * scale + offset:.12g}')
    return int(value) if float(value).is_integer() and abs(value) < 2**53 else value


def _format_number(value: float) -> str:
    if isinstance(value, int) or float(value).is_integer() and abs(value) < 2**53:
        return str(int(value))
    return f'{value:.15g}'


@dataclass
class _PendingSignal:
    name: str
    start: int
    length: int
    byte_order: str
    is_signed: bool
    is_float: bool
    scale: float
    offset: float
    minimum: float
    maximum: float
    unit: str
    multiplex: str
    receivers: List[str]
    choices: Dict[int, str] = field(default_factory=OrderedDict)


# Reading


class _Parser:
    def __init__(self, text: str):
        self.lines = text.splitlines()
        self.index = 0
        self.warnings: List[str] = []
        self.protocol = 'CAN'
        self.expected_messages: Optional[int] = None
        self.messages: List[Message] = []
        self.nodes: List[str] = []
        self.node_comments: Dict[str, str] = {}
        self.message_comments: Dict[Tuple[int, bool], str] = {}
        self.signal_comments: Dict[Tuple[int, bool, str], str] = {}

    def _next(self) -> Optional[Tuple[int, str]]:
        while self.index < len(self.lines):
            number, line = self.index + 1, self.lines[self.index].strip()
            self.index += 1
            if line and not line.startswith('//'):
                return number, line
        return None

    def parse(self) -> Database:
        while (item := self._next()) is not None:
            number, line = item
            tag, _, rest = line.partition(' ')
            rest = rest.strip()
            if tag == '[DATABASE_VERSION]':
                if rest not in ('1.1', '1.2', '1.3', '1.4'):
                    self.warnings.append(f'Untested database version {rest!r}')
            elif tag == '[PROTOCOL]':
                self.protocol = rest.upper()
                if self.protocol not in ('CAN', 'J1939'):
                    raise DbfError(f'Unsupported protocol {rest!r}', number)
            elif tag == '[BUSMASTER_VERSION]':
                pass
            elif tag == '[NUMBER_OF_MESSAGES]':
                self.expected_messages = int(rest) if rest.isdigit() else None
            elif tag == '[START_MSG]':
                self.messages.append(self._message(rest, number))
            elif tag == '[NODE]':
                self.nodes = [node.strip() for node in rest.split(',') if node.strip()]
            elif tag == '[START_DESC]':
                self._comments()
            elif tag in _SKIPPED_SECTIONS:
                self._skip_section(tag, number)
            else:
                self.warnings.append(f'Line {number}: ignored {line[:60]!r}')
        if self.expected_messages is not None and self.expected_messages != len(self.messages):
            self.warnings.append(
                f'[NUMBER_OF_MESSAGES] says {self.expected_messages}, the file contains {len(self.messages)}'
            )
        return self._database()

    def _skip_section(self, tag: str, number: int) -> None:
        end, what = _SKIPPED_SECTIONS[tag]
        content = 0
        while (item := self._next()) is not None and item[1] != end:
            content += 1
        if item is None:
            raise DbfError(f'{tag} without {end}', number)
        if content:
            self.warnings.append(f'Skipped {what} ({content} lines)')

    def _frame_id(self, raw_id: int, extended: bool) -> int:
        if self.protocol == 'J1939':
            return (J1939_DEFAULT_PRIORITY << 26) | ((raw_id & 0x3FFFF) << 8) | J1939_NULL_ADDRESS
        return raw_id

    def _message(self, definition: str, number: int) -> Message:
        fields = [part.strip() for part in definition.split(',')]
        if len(fields) < 6:
            raise DbfError(f'Message definition needs at least 6 fields: {definition!r}', number)
        name, raw_id, length, _count, _order, frame_format = fields[:6]
        sender = fields[6] if len(fields) > 6 else ''
        try:
            raw_id, length = int(raw_id), int(length)
        except ValueError:
            raise DbfError(f'Invalid id or length in {definition!r}', number) from None
        if frame_format not in ('S', 'X'):
            raise DbfError(f'Frame format must be S or X, got {frame_format!r}', number)
        extended = frame_format == 'X' or self.protocol == 'J1939'
        pending: List[_PendingSignal] = []
        while True:
            item = self._next()
            if item is None:
                raise DbfError(f'Message {name!r} has no [END_MSG]', number)
            line_number, line = item
            tag, _, rest = line.partition(' ')
            if tag == '[END_MSG]':
                break
            if tag == '[START_SIGNALS]':
                pending.append(self._signal(rest.strip(), line_number))
            elif tag == '[VALUE_DESCRIPTION]':
                if not pending:
                    raise DbfError('[VALUE_DESCRIPTION] before any signal', line_number)
                match = _VALUE_DESCRIPTION.match(rest.strip())
                if match is None:
                    raise DbfError(f'Invalid value description {rest!r}', line_number)
                pending[-1].choices[int(float(match['value']))] = match['text']
            else:
                raise DbfError(f'Unexpected {tag!r} inside message {name!r}', line_number)
        frame_id = self._frame_id(raw_id, extended)
        signals = self._signals(pending, name)
        senders = [sender] if sender and sender != UNKNOWN_NODE else None
        try:
            return Message(
                frame_id=frame_id,
                name=name,
                length=length,
                signals=signals,
                senders=senders,
                is_extended_frame=extended,
                is_fd=length > 8,
                strict=False,
            )
        except Exception as e:
            raise DbfError(f'Invalid message {name!r}: {e}', number) from e

    def _signal(self, definition: str, number: int) -> _PendingSignal:
        fields = [part.strip() for part in definition.split(',')]
        if len(fields) < 10:
            raise DbfError(f'Signal definition needs at least 10 fields: {definition!r}', number)
        name = fields[0]
        try:
            length, byte, bit = int(fields[1]), int(fields[2]), int(fields[3])
            signal_type = fields[4].upper()
            maximum, minimum = float(fields[5]), float(fields[6])
            order, offset, scale = fields[7], float(fields[8]), float(fields[9])
        except ValueError:
            raise DbfError(f'Invalid number in signal {name!r}: {definition!r}', number) from None
        if signal_type not in ('U', 'I', 'B', 'F', 'D', 'X', 'Y'):
            raise DbfError(f'Unknown signal type {signal_type!r} of {name!r}', number)
        if order not in (INTEL, MOTOROLA):
            raise DbfError(f'Byte order must be 0 or 1, got {order!r}', number)
        if length < 1:
            raise DbfError(f'Invalid length {length} of {name!r}', number)
        big_endian = order == MOTOROLA
        try:
            start = dbf_position_to_start(byte, bit, length, big_endian)
        except ValueError as e:
            raise DbfError(f'Signal {name!r}: {e}', number) from None
        if scale == 0:
            self.warnings.append(f'Signal {name!r} has factor 0, using 1')
            scale = 1.0
        low, high = sorted((_physical(minimum, scale, offset), _physical(maximum, scale, offset)))
        unit = fields[10] if len(fields) > 10 else ''
        multiplex = fields[11] if len(fields) > 11 else ''
        receivers = [node for node in fields[12:] if node and node != UNKNOWN_NODE]
        return _PendingSignal(
            name=name,
            start=start,
            length=length,
            byte_order='big_endian' if big_endian else 'little_endian',
            # IEEE floats are inherently signed; DBC marks them '-' as well
            is_signed=signal_type in ('I', 'X', 'F', 'D'),
            is_float=signal_type in ('F', 'D'),
            scale=scale,
            offset=offset,
            minimum=low,
            maximum=high,
            unit=unit,
            multiplex=multiplex,
            receivers=receivers,
        )

    def _signals(self, pending: List[_PendingSignal], message: str) -> List[Signal]:
        multiplexers = [signal.name for signal in pending if signal.multiplex == 'M']
        if len(multiplexers) > 1:
            self.warnings.append(f'Message {message!r} has several multiplexers, using {multiplexers[0]!r}')
        signals = []
        for item in pending:
            multiplexer_ids = multiplexer_signal = None
            if item.multiplex and item.multiplex != 'M':
                match = re.fullmatch(r'm(\d+)', item.multiplex)
                if match is None or not multiplexers:
                    self.warnings.append(f'Ignored multiplex indicator {item.multiplex!r} of {item.name!r}')
                else:
                    multiplexer_ids, multiplexer_signal = [int(match[1])], multiplexers[0]
            signals.append(
                Signal(
                    name=item.name,
                    start=item.start,
                    length=item.length,
                    byte_order=item.byte_order,
                    is_signed=item.is_signed,
                    conversion=conversion.BaseConversion.factory(
                        scale=item.scale,
                        offset=item.offset,
                        choices=OrderedDict(item.choices) if item.choices else None,
                        is_float=item.is_float,
                    ),
                    minimum=item.minimum,
                    maximum=item.maximum,
                    unit=item.unit or None,
                    receivers=item.receivers,
                    is_multiplexer=item.multiplex == 'M' and item.name == (multiplexers or [None])[0],
                    multiplexer_ids=multiplexer_ids,
                    multiplexer_signal=multiplexer_signal,
                )
            )
        return signals

    @staticmethod
    def _comment_text(text: str) -> str:
        text = text.strip()
        if text.endswith(';'):
            text = text[:-1].rstrip()
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1]
        return text.replace('\\"', '"')

    def _read_comment(self, first: str) -> str:
        # Comments may span several lines until the closing '";'
        text = first
        while not text.rstrip().endswith('";') and self.index < len(self.lines):
            text += '\n' + self.lines[self.index].rstrip('\r')
            self.index += 1
        return self._comment_text(text)

    def _comments(self) -> None:
        section = None
        while (item := self._next()) is not None:
            number, line = item
            if line == '[END_DESC]':
                return
            if line.startswith('[START_DESC_'):
                section = line[len('[START_DESC_') : -1]
                continue
            if line.startswith('[END_DESC_'):
                section = None
                continue
            if section == 'NET':
                self.warnings.append('Network comments are not supported and were skipped')
                self._read_comment(line)
            elif section == 'NODE':
                name, _, text = line.partition(' ')
                self.node_comments[name] = self._read_comment(text)
            elif section in ('MSG', 'SIG'):
                parts = line.split(' ', 3 if section == 'SIG' else 2)
                if len(parts) < (4 if section == 'SIG' else 3) or not parts[0].isdigit():
                    raise DbfError(f'Invalid {section.lower()} comment {line!r}', number)
                raw_id, extended = int(parts[0]), parts[1] == 'X' or self.protocol == 'J1939'
                key = (self._frame_id(raw_id, extended), extended)
                if section == 'MSG':
                    self.message_comments[key] = self._read_comment(parts[2])
                else:
                    self.signal_comments[(*key, parts[2])] = self._read_comment(parts[3])
            else:
                raise DbfError(f'Unexpected line in [START_DESC]: {line!r}', number)
        raise DbfError('[START_DESC] without [END_DESC]')

    def _database(self) -> Database:
        for message in self.messages:
            key = (message.frame_id, message.is_extended_frame)
            if key in self.message_comments:
                message.comment = self.message_comments.pop(key)
            for signal in message.signals:
                comment = self.signal_comments.pop((*key, signal.name), None)
                if comment is not None:
                    signal.comment = comment
        dangling = len(self.message_comments) + len(self.signal_comments)
        if dangling:
            self.warnings.append(f'{dangling} comments refer to unknown messages or signals and were skipped')
        nodes = [Node(name, comment=self.node_comments.get(name)) for name in self.nodes]
        database = Database(messages=self.messages, nodes=nodes, strict=False)
        database.refresh()
        return database


def parse_dbf(text: str) -> Tuple[Database, List[str]]:
    """Parse DBF text. Returns the database and warnings about content that could not be represented."""
    parser = _Parser(text)
    return parser.parse(), parser.warnings


def load_dbf(path: str | Path, encoding: str = ENCODING) -> Tuple[Database, List[str]]:
    return parse_dbf(Path(path).read_text(encoding=encoding, errors='replace'))


# Writing


def _j1939_protocol(database: Database) -> bool:
    dbc = database.dbc
    attribute = dbc.attributes.get('ProtocolType') if dbc is not None else None
    return attribute is not None and str(attribute.value).upper() == 'J1939'


def _j1939_pgn(frame_id: int) -> int:
    pgn = (frame_id >> 8) & 0x3FFFF
    return pgn & 0x3FF00 if ((pgn >> 8) & 0xFF) < 240 else pgn


def _escape(text: str) -> str:
    return '"' + str(text).replace('"', '\\"').replace('\r', '').replace('\n', ' ') + '";'


def _signal_type(signal: Signal) -> str:
    if signal.is_float:
        return 'D' if signal.length == 64 else 'F'
    return 'I' if signal.is_signed else 'U'


def _raw_limits(signal: Signal, signal_type: str) -> Tuple[str, str]:
    low, high = _raw_range(signal_type, signal.length)
    scale, offset = signal.scale or 1, signal.offset

    def raw(value: Optional[float], default: float) -> float:
        if value is None:
            return default
        result = (value - offset) / scale
        if signal_type in 'FD':
            return result
        return min(max(round(result), low), high)

    raw_min, raw_max = raw(signal.minimum, low), raw(signal.maximum, high)
    if scale < 0:
        raw_min, raw_max = raw_max, raw_min
    if signal.minimum is None and signal.maximum is None or (raw_min == raw_max == 0):
        raw_min, raw_max = low, high  # DBC [0|0] means "no limits"
    return _format_number(raw_max), _format_number(raw_min)


def dump_dbf(database: Database, protocol: Optional[str] = None) -> Tuple[str, List[str]]:
    """Write a database as DBF text. Returns the text and warnings about content that was dropped.

    protocol: 'CAN' or 'J1939'; by default J1939 when the DBC declares ProtocolType "J1939".
    """
    warnings: List[str] = []
    protocol = (protocol or ('J1939' if _j1939_protocol(database) else 'CAN')).upper()
    if protocol not in ('CAN', 'J1939'):
        raise ValueError(f'Unsupported protocol {protocol!r}')
    lines = [
        HEADER,
        '',
        f'[DATABASE_VERSION] {DATABASE_VERSION}',
        '',
        f'[PROTOCOL] {protocol}',
        '',
        f'[BUSMASTER_VERSION] [{BUSMASTER_VERSION}]',
        f'[NUMBER_OF_MESSAGES] {len(database.messages)}',
        '',
    ]
    message_comments, signal_comments = [], []
    for message in database.messages:
        if message.contained_messages:
            warnings.append(f'Container message {message.name!r} written without its contained messages')
        if message.cycle_time is not None:
            warnings.append(f'Cycle time of {message.name!r} is not stored in DBF')
        frame_id = _j1939_pgn(message.frame_id) if protocol == 'J1939' else message.frame_id
        frame_format = 'X' if message.is_extended_frame else 'S'
        orders = {signal.byte_order for signal in message.signals}
        message_order = MOTOROLA if orders == {'big_endian'} else INTEL
        sender = (message.senders or [UNKNOWN_NODE])[0]
        if message.senders and len(message.senders) > 1:
            warnings.append(f'Message {message.name!r}: only the first of several senders is stored')
        lines.append(
            f'[START_MSG] {message.name},{frame_id},{message.length},{len(message.signals)},'
            f'{message_order},{frame_format},{sender}'
        )
        for signal in message.signals:
            big_endian = signal.byte_order == 'big_endian'
            byte, bit = start_to_dbf_position(signal.start, signal.length, big_endian)
            signal_type = _signal_type(signal)
            raw_max, raw_min = _raw_limits(signal, signal_type)
            multiplex = ''
            if signal.is_multiplexer:
                multiplex = 'M'
            elif signal.multiplexer_ids:
                multiplex = f'm{signal.multiplexer_ids[0]}'
                if len(signal.multiplexer_ids) > 1 or signal.multiplexer_signal is None:
                    warnings.append(f'Signal {signal.name!r}: only multiplexer value {signal.multiplexer_ids[0]} kept')
            unit = (signal.unit or '').replace(',', ' ')
            receivers = ','.join(signal.receivers) if signal.receivers else ''
            lines.append(
                f'[START_SIGNALS] {signal.name},{signal.length},{byte},{bit},{signal_type},{raw_max},{raw_min},'
                f'{MOTOROLA if big_endian else INTEL},{_format_number(signal.offset)},'
                f'{_format_number(signal.scale)},{unit},{multiplex},{receivers}'
            )
            for value, text in (signal.choices or {}).items():
                lines.append(f'[VALUE_DESCRIPTION] "{str(text).replace(chr(34), chr(39))}",{int(value)}')
            if signal.comment:
                signal_comments.append(f'{frame_id} {frame_format} {signal.name} {_escape(signal.comment)}')
        lines += ['[END_MSG]', '']
        if message.comment:
            message_comments.append(f'{frame_id} {frame_format} {_escape(message.comment)}')
    lines += ['[START_VALUE_TABLE]', '[END_VALUE_TABLE]', '']
    lines += ['[NODE] ' + ','.join(node.name for node in database.nodes), '']
    node_comments = [f'{node.name} {_escape(node.comment)}' for node in database.nodes if node.comment]
    lines += ['[START_DESC]', '[START_DESC_NET]', '[END_DESC_NET]', '']
    lines += ['[START_DESC_NODE]', *node_comments, '[END_DESC_NODE]', '']
    lines += ['[START_DESC_MSG]', *message_comments, '[END_DESC_MSG]', '']
    lines += ['[START_DESC_SIG]', *signal_comments, '[END_DESC_SIG]', '[END_DESC]', '']
    return '\n'.join(lines), warnings


def save_dbf(database: Database, path: str | Path, protocol: Optional[str] = None) -> List[str]:
    text, warnings = dump_dbf(database, protocol)
    # BUSMASTER is a Windows application: ANSI encoding and CRLF line endings
    with open(path, 'w', encoding=ENCODING, errors='replace', newline='\r\n') as file:
        file.write(text)
    return warnings
