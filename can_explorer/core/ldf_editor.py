"""Editing LIN description files (LDF): frames, signals, nodes, encodings and schedule tables.

The file is edited as ldfparser's dictionary and written with ldf_writer; saving parses the written text with
ldfparser first, so only files ldfparser accepts are written. Comments of the original file are not kept.
"""

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import ldfparser
from lark.exceptions import LarkError

from can_explorer.core.database_editor import NAME_PATTERN, EditError
from can_explorer.core.ldf_writer import encoding_value_text, ldf_text, parse_ldf_text, schedule_entry_text

MAX_UNCONDITIONAL_ID = 0x3B  # Frame identifiers 0x3C and above are diagnostic or reserved
MAX_ARRAY_BYTES = 8
LIN_SPEEDS = (2400, 9600, 10417, 19200, 20000)

NEW_LDF: Dict[str, Any] = {
    'header': 'lin_description_file',
    'protocol_version': '2.1',
    'language_version': '2.1',
    'speed': 19200,
    'nodes': {'master': {'name': 'Master', 'timebase': 0.005, 'jitter': 0.0001}, 'slaves': []},
    'signals': [],
    'frames': [],
    'schedule_tables': [],
    'signal_encoding_types': [],
    'signal_representations': [],
    'node_attributes': [],
}


def _name(value: str, what: str) -> str:
    value = (value or '').strip()
    if not NAME_PATTERN.match(value):
        raise EditError(f'{what} name {value!r}: use letters, digits and _, not starting with a digit')
    return value


def _integer(value: Any, what: str) -> int:
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise EditError(f'{what}: {value!r} is not an integer') from None


def _milliseconds(value: Any, what: str) -> float:
    try:
        seconds = float(value) / 1000
    except (TypeError, ValueError):
        raise EditError(f'{what}: {value!r} is not a number') from None
    if seconds < 0:
        raise EditError(f'{what} cannot be negative')
    return seconds


def _names(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r'[,\s]+', value)
    return [name.strip() for name in value or [] if name and name.strip()]


class LdfEditor:
    kind = 'lin'

    def __init__(self, path: Optional[str | Path] = None):
        self.path: Optional[Path] = None
        self.warnings: List[str] = []
        self.dirty = False
        if path is None:
            self.data = copy.deepcopy(NEW_LDF)
        else:
            self.path = Path(path)
            self.data = ldfparser.parse_ldf_to_dict(str(self.path))
            comments = ldfparser.parse_ldf_to_dict(str(self.path), capture_comments=True).get('comments')
            if comments:
                self.warnings.append('Comments of the file are not kept when it is saved')
        for key in ('signals', 'frames', 'schedule_tables', 'signal_encoding_types', 'signal_representations'):
            self.data.setdefault(key, [])
        self.data.setdefault('nodes', {}).setdefault('slaves', [])

    # Files

    @property
    def writable(self) -> bool:
        return self.path is not None

    def text(self) -> str:
        return ldf_text(self.data)

    def save(self, path: Optional[str | Path] = None) -> List[str]:
        """Write the file; it is checked with ldfparser first. Returns warnings."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise EditError('Choose a file name')
        if target.suffix.lower() != '.ldf':
            raise EditError('LIN databases are saved as .ldf files')
        text = self.text()
        directory = target.parent if target.parent.is_dir() else Path(tempfile.gettempdir())
        with tempfile.NamedTemporaryFile('w', suffix='.ldf', dir=directory, delete=False, encoding='utf-8') as file:
            file.write(text)
            check = Path(file.name)
        try:
            ldfparser.parse_ldf(str(check))
        except Exception as e:
            check.unlink(missing_ok=True)
            raise EditError(f'The database is not valid and was not saved: {e}') from e
        os.replace(check, target)
        self.path, self.dirty = target, False
        return list(self.warnings)

    def _changed(self) -> None:
        self.dirty = True

    # Header and nodes

    @property
    def master(self) -> Dict[str, Any]:
        return self.data['nodes'].get('master') or {}

    @property
    def slaves(self) -> List[str]:
        return list(self.data['nodes'].get('slaves', []))

    @property
    def node_names(self) -> List[str]:
        return [name for name in [self.master.get('name'), *self.slaves] if name]

    def set_header(self, **fields: Any) -> None:
        """speed (bit/s), protocol_version, language_version, channel_name."""
        if 'speed' in fields:
            speed = _integer(fields['speed'], 'Speed')
            if not 1000 <= speed <= 20000:
                raise EditError('LIN speeds are 1 to 20 kbit/s')
            self.data['speed'] = speed
        for key in ('protocol_version', 'language_version'):
            if key in fields:
                version = str(fields[key]).strip()
                if not re.match(r'^(\d+\.\d+|ISO17987:\d+|J2602_\d+_\d+\.\d+)$', version):
                    raise EditError(f'{version!r} is not a LIN version such as 2.1')
                self.data[key] = version
        if 'channel_name' in fields:
            self.data['channel_name'] = str(fields['channel_name']).strip() or None
        self._changed()

    def set_master(self, name: Optional[str] = None, timebase_ms: Any = None, jitter_ms: Any = None) -> None:
        master = self.data['nodes'].setdefault('master', {'name': 'Master', 'timebase': 0.005, 'jitter': 0.0001})
        if name is not None and name != master['name']:
            self.rename_node(master['name'], name)
        if timebase_ms is not None:
            master['timebase'] = _milliseconds(timebase_ms, 'Time base')
        if jitter_ms is not None:
            master['jitter'] = _milliseconds(jitter_ms, 'Jitter')
        self._changed()

    def add_node(self, name: str) -> None:
        name = _name(name, 'Node')
        if name in self.node_names:
            raise EditError(f'Node {name} exists already')
        self.data['nodes']['slaves'].append(name)
        # LIN 2.x files describe every slave in Node_attributes
        used = {node.get('configured_nad') for node in self.data.setdefault('node_attributes', [])}
        nad = next(value for value in range(1, 0x80) if value not in used)
        self.data['node_attributes'].append(
            {
                'name': name,
                'lin_protocol': self.data.get('protocol_version', '2.1'),
                'configured_nad': nad,
                'initial_nad': nad,
                'product_id': {'supplier_id': 0, 'function_id': 0, 'variant': 0},
            }
        )
        self._changed()

    def remove_node(self, name: str) -> None:
        if name not in self.slaves:
            raise EditError(f'{name} is not a slave node' if name in self.node_names else f'No node {name!r}')
        published = [signal['name'] for signal in self.data['signals'] if signal['publisher'] == name]
        published += [frame['name'] for frame in self.data['frames'] if frame['publisher'] == name]
        if published:
            raise EditError(f'{name} publishes {", ".join(published)}; move or remove them first')
        self.data['nodes']['slaves'].remove(name)
        for signal in self.data['signals']:
            if name in signal.get('subscribers', []):
                signal['subscribers'].remove(name)
        self.data['node_attributes'] = [node for node in self.data.get('node_attributes', []) if node['name'] != name]
        for table in self.data['schedule_tables']:
            table['schedule'] = [entry for entry in table['schedule'] if entry['command'].get('node') != name]
        self._changed()

    def rename_node(self, old: str, new: str) -> None:
        new = _name(new, 'Node')
        if old not in self.node_names:
            raise EditError(f'No node {old!r}')
        if new in self.node_names and new != old:
            raise EditError(f'Node {new} exists already')
        nodes = self.data['nodes']
        if nodes.get('master', {}).get('name') == old:
            nodes['master']['name'] = new
        nodes['slaves'] = [new if slave == old else slave for slave in nodes.get('slaves', [])]
        for signal in self.data['signals']:
            if signal['publisher'] == old:
                signal['publisher'] = new
            signal['subscribers'] = [new if node == old else node for node in signal.get('subscribers', [])]
        for frame in self.data['frames']:
            if frame['publisher'] == old:
                frame['publisher'] = new
        for node in self.data.get('node_attributes', []):
            if node['name'] == old:
                node['name'] = new
        for table in self.data['schedule_tables']:
            for entry in table['schedule']:
                if entry['command'].get('node') == old:
                    entry['command']['node'] = new
        self._changed()

    def node_attributes(self, name: str) -> Dict[str, Any]:
        return next((node for node in self.data.get('node_attributes', []) if node['name'] == name), {})

    def update_node_attributes(self, name: str, **fields: Any) -> None:
        """For a slave: lin_protocol, configured_nad, initial_nad, supplier_id, function_id, variant, P2_min_ms,
        ST_min_ms."""
        if name not in self.slaves:
            raise EditError(f'Node attributes are for slave nodes, not {name!r}')
        attributes = self.data.setdefault('node_attributes', [])
        node = self.node_attributes(name)
        if not node:
            node = {'name': name, 'lin_protocol': self.data.get('protocol_version', '2.1')}
            attributes.append(node)
        for key in ('configured_nad', 'initial_nad'):
            if key in fields:
                value = _integer(fields[key], key.replace('_', ' '))
                if not 1 <= value <= 0x7F:
                    raise EditError('NADs are 1 to 0x7F')
                node[key] = value
        product = node.setdefault('product_id', {'supplier_id': 0, 'function_id': 0, 'variant': 0})
        for key in ('supplier_id', 'function_id', 'variant'):
            if key in fields:
                limit = 0xFF if key == 'variant' else 0xFFFF
                value = _integer(fields[key], key.replace('_', ' '))
                if not 0 <= value <= limit:
                    raise EditError(f'{key.replace("_", " ").capitalize()} is 0 to 0x{limit:X}')
                product[key] = value
        if 'lin_protocol' in fields:
            node['lin_protocol'] = str(fields['lin_protocol']).strip()
        for key in ('P2_min', 'ST_min'):
            if f'{key}_ms' in fields:
                node[key] = _milliseconds(fields[f'{key}_ms'], key)
        self._changed()

    # Frames

    @property
    def frames(self) -> List[Dict[str, Any]]:
        return self.data['frames']

    def frame(self, name: str) -> Dict[str, Any]:
        for frame in self.data['frames']:
            if frame['name'] == name:
                return frame
        raise EditError(f'No frame {name!r}')

    def _all_frame_names(self) -> set:
        names = {frame['name'] for frame in self.data['frames']}
        for key in ('sporadic_frames', 'event_triggered_frames', 'diagnostic_frames'):
            names |= {frame['name'] for frame in self.data.get(key, [])}
        return names

    def _used_ids(self, exclude: Optional[str] = None) -> Dict[int, str]:
        used = {frame['frame_id']: frame['name'] for frame in self.data['frames'] if frame['name'] != exclude}
        used.update({frame['frame_id']: frame['name'] for frame in self.data.get('event_triggered_frames', [])})
        return used

    def add_frame(self, name: Optional[str] = None, frame_id: Optional[int] = None) -> str:
        existing = self._all_frame_names()
        if name:
            name = _name(name, 'Frame')
            if name in existing:
                raise EditError(f'A frame named {name} exists already')
        else:
            index = 1
            while f'Frame{index}' in existing:
                index += 1
            name = f'Frame{index}'
        used = self._used_ids()
        if frame_id is None:
            frame_id = next((value for value in range(MAX_UNCONDITIONAL_ID + 1) if value not in used), None)
            if frame_id is None:
                raise EditError('All frame identifiers 0 to 0x3B are used')
        self.data['frames'].append(
            {'name': name, 'frame_id': frame_id, 'publisher': self.master.get('name', ''), 'length': 8, 'signals': []}
        )
        self._changed()
        return name

    def update_frame(self, frame_name: str, **fields: Any) -> str:
        """name, frame_id, length (1-8 bytes), publisher. Returns the (possibly new) name."""
        frame = self.frame(frame_name)
        new_name = _name(fields['name'], 'Frame') if 'name' in fields else frame['name']
        if new_name != frame['name'] and new_name in self._all_frame_names():
            raise EditError(f'A frame named {new_name} exists already')
        frame_id = _integer(fields['frame_id'], 'Identifier') if 'frame_id' in fields else frame['frame_id']
        if not 0 <= frame_id <= MAX_UNCONDITIONAL_ID:
            raise EditError(f'Frame identifiers are 0 to 0x{MAX_UNCONDITIONAL_ID:X}')
        other = self._used_ids(exclude=frame['name']).get(frame_id)
        if other:
            raise EditError(f'Identifier 0x{frame_id:X} is used by {other}')
        length = _integer(fields['length'], 'Length') if 'length' in fields else (frame.get('length') or 8)
        if not 1 <= length <= 8:
            raise EditError('LIN frames are 1 to 8 bytes long')
        publisher = fields.get('publisher', frame['publisher'])
        if publisher not in self.node_names:
            raise EditError(f'No node {publisher!r}')

        if new_name != frame['name']:
            self._rename_frame_references(frame['name'], new_name)
        frame.update(name=new_name, frame_id=frame_id, length=length, publisher=publisher)
        for signal in self._frame_signal_definitions(frame):  # The publisher of a frame publishes its signals
            signal['publisher'] = publisher
            if publisher in signal.get('subscribers', []):
                signal['subscribers'].remove(publisher)
        self._changed()
        return new_name

    def _rename_frame_references(self, old: str, new: str) -> None:
        for key in ('sporadic_frames', 'event_triggered_frames'):
            for frame in self.data.get(key, []):
                frame['frames'] = [new if name == old else name for name in frame['frames']]
        for node in self.data.get('node_attributes', []):
            frames = node.get('configurable_frames')
            if isinstance(frames, dict) and old in frames:
                node['configurable_frames'] = {new if key == old else key: value for key, value in frames.items()}
            elif isinstance(frames, list):
                node['configurable_frames'] = [new if name == old else name for name in frames]
        for table in self.data['schedule_tables']:
            for entry in table['schedule']:
                if entry['command'].get('frame') == old:
                    entry['command']['frame'] = new

    def remove_frame(self, frame_name: str) -> None:
        """Remove a frame, its signals (when no other frame carries them) and the references to it."""
        frame = self.frame(frame_name)
        for mapping in list(frame['signals']):
            self._unmap_signal(frame, mapping['signal'])
        self.data['frames'].remove(frame)
        for key in ('sporadic_frames', 'event_triggered_frames'):
            for entry in self.data.get(key, []):
                entry['frames'] = [name for name in entry['frames'] if name != frame_name]
            self.data[key] = [entry for entry in self.data.get(key, []) if entry['frames']]
        for node in self.data.get('node_attributes', []):
            frames = node.get('configurable_frames')
            if isinstance(frames, dict):
                frames.pop(frame_name, None)
            elif isinstance(frames, list) and frame_name in frames:
                frames.remove(frame_name)
        for table in self.data['schedule_tables']:
            table['schedule'] = [entry for entry in table['schedule'] if entry['command'].get('frame') != frame_name]
        self._changed()

    # Signals

    def signal(self, name: str) -> Dict[str, Any]:
        for signal in self.data['signals']:
            if signal['name'] == name:
                return signal
        raise EditError(f'No signal {name!r}')

    def _frame_signal_definitions(self, frame: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [self.signal(mapping['signal']) for mapping in frame['signals']]

    def frame_signals(self, frame_name: str) -> List[Tuple[Dict[str, Any], int]]:
        """(signal, offset) of a frame, by offset."""
        frame = self.frame(frame_name)
        return sorted(
            ((self.signal(mapping['signal']), mapping['offset']) for mapping in frame['signals']),
            key=lambda item: item[1],
        )

    def _mapping(self, frame: Dict[str, Any], signal_name: str) -> Dict[str, Any]:
        for mapping in frame['signals']:
            if mapping['signal'] == signal_name:
                return mapping
        raise EditError(f'Frame {frame["name"]} has no signal {signal_name!r}')

    def add_signal(self, frame_name: str, name: Optional[str] = None) -> str:
        """A new 8 bit signal at the first free byte of the frame."""
        frame = self.frame(frame_name)
        existing = {signal['name'] for signal in self.data['signals']} | {
            signal['name'] for signal in self.data.get('diagnostic_signals', [])
        }
        if name:
            name = _name(name, 'Signal')
            if name in existing:
                raise EditError(f'A signal named {name} exists already')
        else:
            index = 1
            while f'Signal{index}' in existing:
                index += 1
            name = f'Signal{index}'
        used = {bit for bits in self.bit_owners(frame_name).values() for bit in bits}
        length = frame.get('length') or 8
        offset = next((byte * 8 for byte in range(length) if not used & set(range(byte * 8, byte * 8 + 8))), 0)
        self.data['signals'].append(
            {'name': name, 'width': 8, 'init_value': 0, 'publisher': frame['publisher'], 'subscribers': []}
        )
        frame['signals'].append({'signal': name, 'offset': offset})
        self._changed()
        return name

    def update_signal(self, frame_name: str, signal_name: str, **fields: Any) -> str:
        """name, start (bit offset in the frame), length (width in bits), initial (number, or bytes '1, 2, 3' for
        byte arrays), subscribers, encoding ('' for none). Returns the (possibly new) name."""
        frame = self.frame(frame_name)
        mapping = self._mapping(frame, signal_name)
        signal = self.signal(signal_name)
        new_name = _name(fields['name'], 'Signal') if 'name' in fields else signal['name']
        if new_name != signal['name'] and any(other['name'] == new_name for other in self.data['signals']):
            raise EditError(f'A signal named {new_name} exists already')
        offset = _integer(fields['start'], 'Start bit') if 'start' in fields else mapping['offset']
        width = _integer(fields['length'], 'Length') if 'length' in fields else signal['width']
        if offset < 0:
            raise EditError('The start bit cannot be negative')
        if not 1 <= width <= 64:
            raise EditError('LIN signals are 1 to 64 bits wide')
        initial = signal['init_value']
        if 'initial' in fields:
            initial = self._initial_value(fields['initial'], width)
        elif isinstance(initial, list) and len(initial) * 8 != width:
            initial = [0] * (width // 8) if width % 8 == 0 else 0
        if isinstance(initial, int) and not 0 <= initial < 2**width:
            raise EditError(f'The initial value does not fit into {width} bits')
        subscribers = _names(fields['subscribers']) if 'subscribers' in fields else signal.get('subscribers', [])
        for subscriber in subscribers:
            if subscriber not in self.node_names:
                raise EditError(f'No node {subscriber!r}')
        encoding = fields.get('encoding')
        if encoding and encoding not in [entry['name'] for entry in self.data['signal_encoding_types']]:
            raise EditError(f'No encoding type {encoding!r}')

        if new_name != signal['name']:
            self._rename_signal_references(signal['name'], new_name)
        signal.update(name=new_name, width=width, init_value=initial, subscribers=subscribers)
        mapping['offset'] = offset
        if encoding is not None:
            self._set_representation(new_name, encoding)
        self._changed()
        return new_name

    @staticmethod
    def _initial_value(value: Any, width: int) -> Any:
        if isinstance(value, (list, tuple)):
            items = list(value)
        elif isinstance(value, str) and (',' in value or '{' in value):
            items = [item for item in re.split(r'[{},\s]+', value) if item]
        else:
            return _integer(value, 'Initial value')
        items = [_integer(item, 'Initial value') for item in items]
        if width % 8 or len(items) * 8 != width or len(items) > MAX_ARRAY_BYTES:
            raise EditError(f'A byte array initial value needs {width // 8 if width % 8 == 0 else "whole"} bytes')
        if any(not 0 <= item <= 0xFF for item in items):
            raise EditError('Byte array values are 0 to 255')
        return items

    def _rename_signal_references(self, old: str, new: str) -> None:
        for frame in self.data['frames']:
            for mapping in frame['signals']:
                if mapping['signal'] == old:
                    mapping['signal'] = new
        for entry in self.data['signal_representations']:
            entry['signals'] = [new if name == old else name for name in entry['signals']]
        for node in self.data.get('node_attributes', []):
            if node.get('response_error') == old:
                node['response_error'] = new
            if old in node.get('fault_state_signals', []):
                node['fault_state_signals'] = [new if name == old else name for name in node['fault_state_signals']]
        for group in self.data.get('signal_groups', []):
            if old in group['signals']:
                group['signals'] = {new if name == old else name: offset for name, offset in group['signals'].items()}

    def _unmap_signal(self, frame: Dict[str, Any], signal_name: str) -> None:
        frame['signals'] = [mapping for mapping in frame['signals'] if mapping['signal'] != signal_name]
        if any(mapping['signal'] == signal_name for other in self.data['frames'] for mapping in other['signals']):
            return  # Still carried by another frame
        self.data['signals'] = [signal for signal in self.data['signals'] if signal['name'] != signal_name]
        for entry in self.data['signal_representations']:
            entry['signals'] = [name for name in entry['signals'] if name != signal_name]
        for node in self.data.get('node_attributes', []):
            if node.get('response_error') == signal_name:
                del node['response_error']
            if signal_name in node.get('fault_state_signals', []):
                node['fault_state_signals'].remove(signal_name)

    def remove_signal(self, frame_name: str, signal_name: str) -> None:
        frame = self.frame(frame_name)
        self._mapping(frame, signal_name)
        self._unmap_signal(frame, signal_name)
        self._changed()

    def signal_encoding(self, signal_name: str) -> str:
        return next(
            (entry['encoding'] for entry in self.data['signal_representations'] if signal_name in entry['signals']), ''
        )

    def _set_representation(self, signal_name: str, encoding: str) -> None:
        for entry in self.data['signal_representations']:
            if signal_name in entry['signals']:
                entry['signals'].remove(signal_name)
        if encoding:
            entry = next(
                (entry for entry in self.data['signal_representations'] if entry['encoding'] == encoding), None
            )
            if entry is None:
                entry = {'encoding': encoding, 'signals': []}
                self.data['signal_representations'].append(entry)
            entry['signals'].append(signal_name)
        self.data['signal_representations'] = [
            entry for entry in self.data['signal_representations'] if entry['signals']
        ]

    def bit_owners(self, frame_name: str) -> Dict[str, List[int]]:
        """Signal name -> bit positions in the frame (LIN signals are sent least significant bit first)."""
        return {
            signal['name']: list(range(offset, offset + signal['width']))
            for signal, offset in self.frame_signals(frame_name)
        }

    def bit_layout(self, frame_name: str) -> List[List[int]]:
        frame = self.frame(frame_name)
        layout: List[List[int]] = [[] for _ in range((frame.get('length') or 8) * 8)]
        for index, bits in enumerate(self.bit_owners(frame_name).values()):
            for bit in bits:
                if 0 <= bit < len(layout):
                    layout[bit].append(index)
        return layout

    # Encodings

    @property
    def encodings(self) -> List[Dict[str, Any]]:
        return self.data['signal_encoding_types']

    def encoding(self, name: str) -> Dict[str, Any]:
        for entry in self.data['signal_encoding_types']:
            if entry['name'] == name:
                return entry
        raise EditError(f'No encoding type {name!r}')

    def encoding_text(self, name: str) -> str:
        return '\n'.join(encoding_value_text(value) for value in self.encoding(name)['values'])

    def encoding_signals(self, name: str) -> List[str]:
        return next(
            (list(entry['signals']) for entry in self.data['signal_representations'] if entry['encoding'] == name), []
        )

    def add_encoding(self, name: str) -> str:
        name = _name(name, 'Encoding type')
        if any(entry['name'] == name for entry in self.data['signal_encoding_types']):
            raise EditError(f'An encoding type named {name} exists already')
        self.data['signal_encoding_types'].append(
            {
                'name': name,
                'values': [{'type': 'physical', 'min': 0, 'max': 255, 'scale': 1.0, 'offset': 0.0, 'unit': None}],
            }
        )
        self._changed()
        return name

    def rename_encoding(self, old: str, new: str) -> None:
        new = _name(new, 'Encoding type')
        entry = self.encoding(old)
        if new != old and any(other['name'] == new for other in self.data['signal_encoding_types']):
            raise EditError(f'An encoding type named {new} exists already')
        entry['name'] = new
        for representation in self.data['signal_representations']:
            if representation['encoding'] == old:
                representation['encoding'] = new
        self._changed()

    def remove_encoding(self, name: str) -> None:
        self.data['signal_encoding_types'].remove(self.encoding(name))
        self.data['signal_representations'] = [
            entry for entry in self.data['signal_representations'] if entry['encoding'] != name
        ]
        self._changed()

    def set_encoding_text(self, name: str, text: str) -> None:
        """Replace the values of an encoding type, written as in an LDF, e.g.
        'logical_value, 0, "off"; physical_value, 1, 254, 0.5, -40, "degC";'."""
        entry = self.encoding(name)
        try:
            parsed = parse_ldf_text(f'Signal_encoding_types {{ {name} {{ {text} }} }}')
        except LarkError as e:
            raise EditError(f'Invalid encoding values: {str(e).splitlines()[0]}') from None
        values = parsed['signal_encoding_types'][0]['values']
        for value in values:
            if value['type'] == 'physical' and value['min'] > value['max']:
                raise EditError('physical_value: the minimum is above the maximum')
        entry['values'] = values
        self._changed()

    # Schedule tables

    @property
    def schedules(self) -> List[Dict[str, Any]]:
        return self.data['schedule_tables']

    def schedule(self, name: str) -> Dict[str, Any]:
        for table in self.data['schedule_tables']:
            if table['name'] == name:
                return table
        raise EditError(f'No schedule table {name!r}')

    def schedule_text(self, name: str) -> str:
        return '\n'.join(schedule_entry_text(entry) for entry in self.schedule(name)['schedule'])

    def add_schedule(self, name: str) -> str:
        name = _name(name, 'Schedule table')
        if any(table['name'] == name for table in self.data['schedule_tables']):
            raise EditError(f'A schedule table named {name} exists already')
        frames = [frame['name'] for frame in self.data['frames']]
        self.data['schedule_tables'].append(
            {
                'name': name,
                'schedule': [{'command': {'type': 'frame', 'frame': frame}, 'delay': 0.01} for frame in frames],
            }
        )
        self._changed()
        return name

    def rename_schedule(self, old: str, new: str) -> None:
        new = _name(new, 'Schedule table')
        table = self.schedule(old)
        if new != old and any(other['name'] == new for other in self.data['schedule_tables']):
            raise EditError(f'A schedule table named {new} exists already')
        table['name'] = new
        for frame in self.data.get('event_triggered_frames', []):
            if frame.get('collision_resolving_schedule_table') == old:
                frame['collision_resolving_schedule_table'] = new
        self._changed()

    def remove_schedule(self, name: str) -> None:
        users = [
            frame['name']
            for frame in self.data.get('event_triggered_frames', [])
            if frame.get('collision_resolving_schedule_table') == name
        ]
        if users:
            raise EditError(f'{name} resolves collisions of {", ".join(users)}')
        self.data['schedule_tables'].remove(self.schedule(name))
        self._changed()

    def set_schedule_text(self, name: str, text: str) -> None:
        """Replace the entries of a schedule table, written as in an LDF, e.g. 'SeatStatus delay 10 ms;'."""
        table = self.schedule(name)
        try:
            parsed = parse_ldf_text(f'Schedule_tables {{ {name} {{ {text} }} }}')
        except LarkError as e:
            raise EditError(f'Invalid schedule entries: {str(e).splitlines()[0]}') from None
        entries = parsed['schedule_tables'][0]['schedule']
        frames = self._all_frame_names() | {'MasterReq', 'SlaveResp'}
        for entry in entries:
            command = entry['command']
            if command.get('frame') and command['frame'] not in frames:
                raise EditError(f'Unknown frame {command["frame"]!r} in the schedule')
            if command.get('node') and command['node'] not in self.node_names:
                raise EditError(f'Unknown node {command["node"]!r} in the schedule')
        table['schedule'] = entries
        self._changed()

    # Checks

    def frame_issues(self, frame: Dict[str, Any]) -> List[str]:
        issues = []
        size = (frame.get('length') or 8) * 8
        owners: Dict[int, str] = {}
        for signal, offset in self.frame_signals(frame['name']):
            bits = range(offset, offset + signal['width'])
            if offset + signal['width'] > size:
                issues.append(f'{frame["name"]}.{signal["name"]} does not fit into {size // 8} bytes')
            for bit in bits:
                if bit in owners and owners[bit] != signal['name']:
                    issues.append(f'{frame["name"]}: {signal["name"]} overlaps {owners[bit]}')
                    break
                owners[bit] = signal['name']
            if signal['publisher'] != frame['publisher']:
                issues.append(
                    f'{signal["name"]} is published by {signal["publisher"]}, its frame by {frame["publisher"]}'
                )
        return issues

    def issues(self) -> List[str]:
        issues = []
        seen: Dict[int, str] = {}
        for frame in self.data['frames']:
            if frame['frame_id'] in seen:
                issues.append(
                    f'{frame["name"]} and {seen[frame["frame_id"]]} have the same identifier 0x{frame["frame_id"]:X}'
                )
            seen.setdefault(frame['frame_id'], frame['name'])
            issues += self.frame_issues(frame)
        carried = {mapping['signal'] for frame in self.data['frames'] for mapping in frame['signals']}
        for signal in self.data['signals']:
            if signal['name'] not in carried:
                issues.append(f'Signal {signal["name"]} is not carried by any frame')
        return issues
