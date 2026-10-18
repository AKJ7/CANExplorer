"""Editing CAN signal databases (DBC, DBF, KCD, SYM; ARXML is read and saved in one of the others).

The database is a cantools Database; the editor changes its messages, signals and nodes in place, checks the
result and saves it in the format of the file extension.
"""

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cantools.database.can import Database, Message, Node, Signal

from can_explorer.core.database_io import WRITE_SUFFIXES, DatabaseFormatError, load_database, save_database

MAX_STANDARD_ID = 0x7FF
MAX_EXTENDED_ID = 0x1FFFFFFF
CAN_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8)
CAN_FD_LENGTHS = CAN_LENGTHS + (12, 16, 20, 24, 32, 48, 64)
BYTE_ORDERS = ('little_endian', 'big_endian')  # Intel, Motorola
NAME_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')  # DBC identifiers


class EditError(ValueError):
    """An edit that cannot be applied; the database is unchanged."""


def signal_bits(signal: Signal) -> List[int]:
    """Bit positions (byte * 8 + bit, bit 0 = least significant) of a signal, from the least significant bit on
    for Intel, from the most significant one for Motorola (whose DBC start bit is its most significant bit)."""
    if signal.byte_order == 'little_endian':
        return list(range(signal.start, signal.start + signal.length))
    bits, bit = [], signal.start
    for _ in range(signal.length):
        bits.append(bit)
        bit = bit + 15 if bit % 8 == 0 else bit - 1  # Next less significant bit: wraps into the following byte
    return bits


def parse_choices(text: str) -> Optional[Dict[int, str]]:
    """Value descriptions from text such as '0=Off; 1=On; 0x0F=Error' (empty: none)."""
    choices: Dict[int, str] = OrderedDict()
    for part in re.split(r'[;\n]', text):
        if not part.strip():
            continue
        value, separator, label = part.partition('=')
        if not separator or not label.strip():
            raise EditError(f'Value descriptions are written as value=text, not {part.strip()!r}')
        try:
            choices[int(value.strip(), 0)] = label.strip()
        except ValueError:
            raise EditError(f'{value.strip()!r} is not an integer value') from None
    return choices or None


def format_choices(choices: Optional[Dict[Any, Any]]) -> str:
    return '; '.join(f'{value}={label}' for value, label in (choices or {}).items())


def _number(value: Any, name: str, integer: bool = False, allow_none: bool = False) -> Any:
    if value in (None, '') and allow_none:
        return None
    try:
        if isinstance(value, str):
            value = int(value.strip(), 0) if integer else float(value.strip())
        return int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise EditError(f'{name}: {value!r} is not a{"n integer" if integer else " number"}') from None


def _names(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r'[,\s]+', value)
    return [name.strip() for name in value or [] if name and name.strip()]


class DatabaseEditor:
    def __init__(self, path: Optional[str | Path] = None):
        self.path: Optional[Path] = None
        self.warnings: List[str] = []
        self.dirty = False
        if path is None:
            self.database = Database()
        else:
            self.path = Path(path)
            self.database, self.warnings = load_database(self.path)

    # Files

    @property
    def writable(self) -> bool:
        """Whether Save writes back to the opened file (ARXML can only be saved in another format)."""
        return self.path is not None and self.path.suffix.lower() in WRITE_SUFFIXES

    def save(self, path: Optional[str | Path] = None) -> List[str]:
        """Save to `path` (default: the opened file). Returns warnings about content the format cannot hold."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise DatabaseFormatError('Choose a file name')
        if target.suffix.lower() not in WRITE_SUFFIXES:
            raise DatabaseFormatError(
                f'Cannot write {target.suffix or "files without extension"}; use {", ".join(WRITE_SUFFIXES)}'
            )
        warnings = save_database(self.database, target)
        self.path, self.dirty = target, False
        return warnings

    # Lookup

    @property
    def messages(self) -> List[Message]:
        return list(self.database.messages)

    def message(self, name: str) -> Message:
        for message in self.database.messages:
            if message.name == name:
                return message
        raise EditError(f'No message {name!r}')

    def signal(self, message_name: str, signal_name: str) -> Signal:
        for signal in self.message(message_name).signals:
            if signal.name == signal_name:
                return signal
        raise EditError(f'Message {message_name} has no signal {signal_name!r}')

    @property
    def node_names(self) -> List[str]:
        return [node.name for node in self.database.nodes]

    # Changes

    def _changed(self, message: Optional[Message] = None) -> None:
        if message is not None:
            message.refresh(strict=False)
        self.database.refresh()
        self.dirty = True

    @staticmethod
    def _check_name(name: str, what: str) -> str:
        name = (name or '').strip()
        if not NAME_PATTERN.match(name):
            raise EditError(f'{what} name {name!r}: use letters, digits and _, not starting with a digit')
        return name

    def _unique_message_name(self, base: str) -> str:
        names = {message.name for message in self.database.messages}
        index = 1
        while f'{base}{index}' in names:
            index += 1
        return f'{base}{index}'

    def _free_frame_id(self, extended: bool) -> int:
        used = {message.frame_id for message in self.database.messages if message.is_extended_frame == extended}
        frame_id = 0x100
        while frame_id in used:
            frame_id += 1
        return frame_id

    def add_message(self, name: Optional[str] = None, frame_id: Optional[int] = None, extended: bool = False) -> str:
        name = self._check_name(name, 'Message') if name else self._unique_message_name('Message')
        if any(message.name == name for message in self.database.messages):
            raise EditError(f'A message named {name} exists already')
        frame_id = self._free_frame_id(extended) if frame_id is None else frame_id
        self._check_frame_id(frame_id, extended)
        message = Message(frame_id, name, 8, [], is_extended_frame=extended, strict=False)
        self.database.messages.append(message)
        self._changed(message)
        return name

    @staticmethod
    def _check_frame_id(frame_id: int, extended: bool) -> None:
        maximum = MAX_EXTENDED_ID if extended else MAX_STANDARD_ID
        if not 0 <= frame_id <= maximum:
            raise EditError(f'Identifier 0x{frame_id:X} is out of range (0 to 0x{maximum:X})')

    def update_message(self, message_name: str, **fields: Any) -> str:
        """Change message properties: name, frame_id, is_extended_frame, is_fd, length, cycle_time, senders,
        comment. Returns the (possibly new) name."""
        message = self.message(message_name)
        new_name = self._check_name(fields['name'], 'Message') if 'name' in fields else message.name
        if new_name != message.name and any(other.name == new_name for other in self.database.messages):
            raise EditError(f'A message named {new_name} exists already')
        extended = bool(fields.get('is_extended_frame', message.is_extended_frame))
        frame_id = _number(fields['frame_id'], 'Identifier', integer=True) if 'frame_id' in fields else message.frame_id
        self._check_frame_id(frame_id, extended)
        is_fd = bool(fields.get('is_fd', message.is_fd))
        length = _number(fields['length'], 'Length', integer=True) if 'length' in fields else message.length
        if length not in (CAN_FD_LENGTHS if is_fd else CAN_LENGTHS):
            allowed = ', '.join(map(str, CAN_FD_LENGTHS if is_fd else CAN_LENGTHS))
            raise EditError(f'Length {length} is not possible for {"CAN FD" if is_fd else "CAN"} frames ({allowed})')
        cycle_time = message.cycle_time
        if 'cycle_time' in fields:
            cycle_time = _number(fields['cycle_time'], 'Cycle time', integer=True, allow_none=True) or None
        senders = _names(fields['senders']) if 'senders' in fields else message.senders
        for sender in senders:
            self._check_name(sender, 'Node')

        message.name, message.frame_id, message.is_extended_frame = new_name, frame_id, extended
        message.is_fd, message.length, message.cycle_time = is_fd, length, cycle_time
        message.senders[:] = senders
        if 'comment' in fields:
            message.comment = fields['comment'] or None
        self._add_nodes(senders)
        self._changed(message)
        return new_name

    def remove_message(self, name: str) -> None:
        self.database.messages.remove(self.message(name))
        self._changed()

    def add_signal(self, message_name: str, name: Optional[str] = None) -> str:
        """Add an 8 bit signal at the first free byte (or bit 0 when the message is full)."""
        message = self.message(message_name)
        existing = {signal.name for signal in message.signals}
        if name:
            name = self._check_name(name, 'Signal')
            if name in existing:
                raise EditError(f'Message {message.name} has a signal named {name} already')
        else:
            index = 1
            while f'Signal{index}' in existing:
                index += 1
            name = f'Signal{index}'
        used = {bit for signal in message.signals for bit in signal_bits(signal)}
        start = next((byte * 8 for byte in range(message.length) if not used & set(range(byte * 8, byte * 8 + 8))), 0)
        message.signals.append(Signal(name, start, 8))
        self._changed(message)
        return name

    def update_signal(self, message_name: str, signal_name: str, **fields: Any) -> str:
        """Change signal properties: name, start, length, byte_order, is_signed, scale, offset, minimum, maximum,
        unit, initial, receivers, comment, choices (dict or text). Returns the (possibly new) name."""
        message = self.message(message_name)
        signal = self.signal(message_name, signal_name)
        new_name = self._check_name(fields['name'], 'Signal') if 'name' in fields else signal.name
        if new_name != signal.name and any(other.name == new_name for other in message.signals):
            raise EditError(f'Message {message.name} has a signal named {new_name} already')
        start = _number(fields['start'], 'Start bit', integer=True) if 'start' in fields else signal.start
        length = _number(fields['length'], 'Length', integer=True) if 'length' in fields else signal.length
        byte_order = fields.get('byte_order', signal.byte_order)
        if byte_order not in BYTE_ORDERS:
            raise EditError(f'Byte order must be one of {", ".join(BYTE_ORDERS)}')
        if not 1 <= length <= 64:
            raise EditError('Signals are 1 to 64 bits long')
        if start < 0:
            raise EditError('The start bit cannot be negative')
        scale = _number(fields['scale'], 'Factor') if 'scale' in fields else signal.scale
        if scale == 0:
            raise EditError('The factor cannot be 0')
        offset = _number(fields['offset'], 'Offset') if 'offset' in fields else signal.offset
        minimum = _number(fields['minimum'], 'Minimum', allow_none=True) if 'minimum' in fields else signal.minimum
        maximum = _number(fields['maximum'], 'Maximum', allow_none=True) if 'maximum' in fields else signal.maximum
        if minimum is not None and maximum is not None and minimum > maximum:
            raise EditError(f'The minimum ({minimum:g}) is above the maximum ({maximum:g})')
        choices = signal.choices
        if 'choices' in fields:
            value = fields['choices']
            choices = parse_choices(value) if isinstance(value, str) else (OrderedDict(value) if value else None)
        receivers = _names(fields['receivers']) if 'receivers' in fields else signal.receivers
        for receiver in receivers:
            self._check_name(receiver, 'Node')
        initial = signal.raw_initial
        if 'initial' in fields:
            initial = _number(fields['initial'], 'Initial value', allow_none=True)

        signal.name, signal.start, signal.length, signal.byte_order = new_name, start, length, byte_order
        signal.is_signed = bool(fields.get('is_signed', signal.is_signed))
        signal.scale, signal.offset, signal.minimum, signal.maximum = scale, offset, minimum, maximum
        signal.choices = choices
        signal.raw_initial = initial
        if 'unit' in fields:
            signal.unit = fields['unit'] or None
        if 'comment' in fields:
            signal.comment = fields['comment'] or None
        signal.receivers[:] = receivers
        self._add_nodes(receivers)
        message.signals.sort(key=lambda candidate: candidate.start)
        self._changed(message)
        return new_name

    def remove_signal(self, message_name: str, signal_name: str) -> None:
        message = self.message(message_name)
        message.signals.remove(self.signal(message_name, signal_name))
        self._changed(message)

    def _add_nodes(self, names: Iterable[str]) -> None:
        known = set(self.node_names)
        for name in names:
            if name not in known and name != 'Vector__XXX':
                self.database.nodes.append(Node(name))
                known.add(name)

    def add_node(self, name: str) -> None:
        name = self._check_name(name, 'Node')
        if name in self.node_names:
            raise EditError(f'Node {name} exists already')
        self.database.nodes.append(Node(name))
        self._changed()

    def remove_node(self, name: str) -> None:
        """Remove a node and its sender/receiver references."""
        node = next((node for node in self.database.nodes if node.name == name), None)
        if node is None:
            raise EditError(f'No node {name!r}')
        self.database.nodes.remove(node)
        for message in self.database.messages:
            if name in message.senders:
                message.senders.remove(name)
            for signal in message.signals:
                if name in signal.receivers:
                    signal.receivers.remove(name)
        self._changed()

    # Checks

    def bit_layout(self, message_name: str) -> List[List[int]]:
        """Per bit of the message (byte * 8 + bit): indices of the signals occupying it."""
        message = self.message(message_name)
        layout: List[List[int]] = [[] for _ in range(message.length * 8)]
        for index, signal in enumerate(message.signals):
            for bit in signal_bits(signal):
                if 0 <= bit < len(layout):
                    layout[bit].append(index)
        return layout

    def message_issues(self, message: Message) -> List[str]:
        issues = []
        size = message.length * 8
        owners: Dict[int, str] = {}
        multiplexed = any(signal.multiplexer_ids for signal in message.signals)
        for signal in message.signals:
            bits = signal_bits(signal)
            if any(not 0 <= bit < size for bit in bits):
                issues.append(f'{message.name}.{signal.name} does not fit into {message.length} bytes')
            if multiplexed:
                continue  # Multiplexed signals share bits on purpose
            for bit in bits:
                if bit in owners and owners[bit] != signal.name:
                    issues.append(f'{message.name}: {signal.name} overlaps {owners[bit]}')
                    break
                owners[bit] = signal.name
            if signal.minimum is not None and signal.maximum is not None and signal.minimum > signal.maximum:
                issues.append(f'{message.name}.{signal.name}: minimum above maximum')
        return issues

    def issues(self) -> List[str]:
        """Problems that make frames undecodable or ambiguous; saving is still possible."""
        issues: List[str] = []
        seen: Dict[Tuple[int, bool, Optional[str]], str] = {}
        for message in self.database.messages:
            key = (message.frame_id, message.is_extended_frame, message.bus_name)
            if key in seen:
                issues.append(f'{message.name} and {seen[key]} have the same identifier 0x{message.frame_id:X}')
            seen.setdefault(key, message.name)
            issues += self.message_issues(message)
        return issues
