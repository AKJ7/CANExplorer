"""The Reverse engineering page of a connection: bit sniffer with notch, byte statistics and bit fields, and (see
reverse_tools.py) the automatic analysis, reference signals and event markers."""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import can
from PyQt6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.reverse.bitstats import BitStatistics, IdBitStatistics, IdKey
from can_explorer.core.reverse.fields import BYTE_ORDERS, BitField
from can_explorer.gui.reverse_tools import (
    AnalysisController,
    MarkerController,
    ReferenceController,
    RelatedController,
)

REFRESH_INTERVAL_MS = 250
SNAPSHOT_FRAMES = 1_000_000  # The newest frames of the trace are used by the analysis tools
HIGHLIGHT_SECONDS = 1.0  # Bits that changed this recently are highlighted, fading out
HISTOGRAM_BARS = 64  # Values 0-255 per byte, in buckets of 4


def _id_text(key: IdKey) -> str:
    frame_id, extended = key
    return f'{frame_id:08X}' if extended else f'{frame_id:03X}'


def _parse_id(text: str) -> Optional[IdKey]:
    """'1A0' (standard) or '18FEF1FE' (extended, always 8 digits)."""
    try:
        return int(text, 16), len(text) > 3
    except ValueError:
        return None


_ID_FIELDS = ('id', 'name', 'count', 'length', 'cycle', 'changing', 'hits', 'new')


class IdentifierModel(QAbstractListModel):
    """The identifier list as a proper model, so live updates change rows in place instead of resetting the whole
    list. A plain `QVariantList` model was rebuilt on every refresh (every 250 ms under traffic), destroying and
    recreating all delegates — a click landing during a rebuild was lost and could crash."""

    _ROLES = {Qt.ItemDataRole.UserRole + index: field.encode() for index, field in enumerate(_ID_FIELDS)}

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._rows: List[Dict[str, Any]] = []

    def roleNames(self):
        return dict(self._ROLES)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        field = self._ROLES.get(role)
        return self._rows[index.row()].get(field.decode()) if field is not None else None

    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def update(self, rows: List[Dict[str, Any]]) -> None:
        """Bring the model to `rows`, changing only what differs: existing rows update in place (dataChanged),
        new identifiers are inserted and gone ones removed, so delegates are not destroyed under the cursor."""
        new_ids = [row['id'] for row in rows]
        if new_ids == [row['id'] for row in self._rows]:
            for position, (old, new) in enumerate(zip(self._rows, rows, strict=True)):
                if old != new:
                    self._rows[position] = new
                    self.dataChanged.emit(self.index(position), self.index(position))
            return
        by_id = {row['id']: row for row in rows}
        position = 0  # Remove identifiers no longer present
        while position < len(self._rows):
            if self._rows[position]['id'] not in by_id:
                self.beginRemoveRows(QModelIndex(), position, position)
                del self._rows[position]
                self.endRemoveRows()
            else:
                position += 1
        for target, row in enumerate(rows):  # Insert new ones in order, update any that moved into place
            if target < len(self._rows) and self._rows[target]['id'] == row['id']:
                if self._rows[target] != row:
                    self._rows[target] = row
                    self.dataChanged.emit(self.index(target), self.index(target))
            else:
                self.beginInsertRows(QModelIndex(), target, target)
                self._rows.insert(target, row)
                self.endInsertRows()


class ReverseController(QObject):
    """Statistics are collected once the page is first shown (from the frames in the trace on), so connections
    that never use the page do not pay for them."""

    changed = pyqtSignal()  # Statistics of the selected identifier (bits, bytes, field value)
    # The list of identifiers. Kept separate from `changed`: selecting an identifier must not re-notify the id
    # list, or Qt would reset that list's model while its own delegate's onClicked (which called select) is still
    # running, destroying the delegate mid-handler and crashing.
    identifiers_changed = pyqtSignal()
    selection_changed = pyqtSignal()
    field_changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._connection = connection
        self.statistics = BitStatistics()
        self._active = False
        self._visible = False
        self._selected: Optional[IdKey] = None
        self._seen = (0, None)  # Frames and identifiers at the last refresh
        self._field: Dict[str, Any] = {
            'start': 0,
            'length': 8,
            'byte_order': 'little_endian',
            'signed': False,
            'scale': 1.0,
            'offset': 0.0,
            'unit': '',
            'name': '',
        }
        self._field_error = ''
        self._multiplexer: Optional[Tuple[BitField, int]] = None  # The field is only in frames with this value
        # The QML bit grid reads `bits` once per byte row (N+1 times per pass); computing it is O(bytes) and the
        # per-bit work scans a 256-value histogram, so for a 64-byte CAN FD frame the naive recompute took ~350 ms
        # a pass and, fired every refresh, froze the GUI. Compute it once per change instead.
        self._bits_cache: Optional[List[Dict[str, Any]]] = None
        self.changed.connect(self._invalidate_bits)
        self.field_changed.connect(self._invalidate_bits)
        self._id_model = IdentifierModel(self)
        self._id_search = ''
        self._hits_only = False
        self.analyses = []  # Of the last automatic analysis, also used as candidates for references
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        connection.database_changed.connect(self.changed)
        self._analysis = AnalysisController(self, parent=self)
        self._reference = ReferenceController(self, parent=self)
        self._markers = MarkerController(self, parent=self)
        self._related = RelatedController(self, parent=self)

    @property
    def connection(self):
        return self._connection

    @pyqtProperty(QObject, constant=True)
    def analysis(self):
        return self._analysis

    @pyqtProperty(QObject, constant=True)
    def reference(self):
        return self._reference

    @pyqtProperty(QObject, constant=True)
    def markers(self):
        return self._markers

    @pyqtProperty(QObject, constant=True)
    def related(self):
        return self._related

    async def snapshot(self) -> List[can.Message]:
        """The newest SNAPSHOT_FRAMES frames of the trace, read in chunks so the window stays responsive."""
        trace = self.session.trace
        end = trace.total
        start = max(trace.first, end - SNAPSHOT_FRAMES)
        messages: List[can.Message] = []
        for frame in trace.iter_from(start):
            if len(messages) >= end - start:
                break
            messages.append(frame.message)
            if len(messages) % 50_000 == 0:
                await asyncio.sleep(0)
        return messages

    def time_text(self, timestamp: float) -> str:
        """A timestamp relative to the first frame, as the trace shows it."""
        start = self.session.start_time
        return f'{timestamp - (start or 0.0):.3f} s'

    @property
    def session(self):
        return self._connection.session

    # Collection

    @pyqtProperty(bool, notify=changed)
    def active(self) -> bool:
        return self._active

    @pyqtSlot(bool)
    def set_visible(self, visible: bool):
        """Called by the page: start collecting when it is first shown, refresh only while it is visible."""
        self._visible = visible
        if visible and not self._active:
            self.statistics.update_all(frame.message for frame in self.session.trace)
            self.session.add_message_listener(self.statistics.on_message)
            self._active = True
            if self._selected is None and self.statistics.ids:
                self._selected = min(self.statistics.ids, key=lambda key: (key[1], key[0]))
            self.changed.emit()
            self._update_id_model()
            self.selection_changed.emit()
        if visible:
            self._timer.start()
        else:
            self._timer.stop()

    def _refresh(self) -> None:
        seen = (sum(item.count for item in self.statistics.ids.values()), self.statistics.notch_time)
        if seen != self._seen:
            self._seen = seen
            self.changed.emit()
            self._update_id_model()

    @pyqtSlot()
    def reset(self):
        """Forget the statistics (and the notch), e.g. when the trace is cleared."""
        self.statistics.reset()
        self.changed.emit()
        self._update_id_model()

    def clear(self) -> None:
        """The trace was cleared: forget the statistics, results and markers, which refer to its frames."""
        self.reset()
        self._analysis.clear()
        self._reference.clear()
        self._markers.clear()
        self._related.clear()

    def shutdown(self) -> None:
        self._reference.stop_obd()

    # Notch

    @pyqtProperty(bool, notify=changed)
    def notched(self) -> bool:
        return self.statistics.notched

    @pyqtSlot()
    def notch(self):
        """Hide the bits that changed so far; bits changing afterwards stand out."""
        self.statistics.notch()
        self.changed.emit()
        self._update_id_model()

    @pyqtSlot()
    def clear_notch(self):
        self.statistics.clear_notch()
        self.changed.emit()
        self._update_id_model()

    # Identifiers

    def _name(self, key: IdKey) -> str:
        database = self.session.decoder.database
        if database is None or not hasattr(database, 'message_definition'):
            return ''
        try:
            definition = database.message_definition(can.Message(arbitration_id=key[0], is_extended_id=key[1]))
        except Exception:
            return ''
        return definition.name if definition is not None else ''

    @pyqtProperty(QObject, constant=True)
    def identifier_model(self) -> IdentifierModel:
        """The identifier list for the view, updated in place (see IdentifierModel)."""
        return self._id_model

    @pyqtProperty('QVariantList', notify=identifiers_changed)
    def identifiers(self) -> List[Dict[str, Any]]:
        """Each identifier with its frames, cycle time, changing bits and, after a notch, the bits that changed
        since (`hits`) and whether it is `new`."""
        return self._identifier_rows()

    def _identifier_rows(self) -> List[Dict[str, Any]]:
        statistics = self.statistics
        result = []
        for key in sorted(statistics.ids, key=lambda key: (key[1], key[0])):
            item = statistics.ids[key]
            cycle = (item.last_time - item.first_time) / (item.count - 1) if item.count > 1 else None
            result.append(
                {
                    'id': _id_text(key),
                    'name': self._name(key),
                    'count': item.count,
                    'length': item.length,
                    'cycle': '' if cycle is None else f'{cycle * 1000:.1f} ms',
                    'changing': bin(item.changed).count('1'),
                    'hits': bin(item.unmasked_changes).count('1') if statistics.notched else 0,
                    'new': statistics.is_new(key),
                }
            )
        return result

    def _filtered_rows(self) -> List[Dict[str, Any]]:
        search = self._id_search.lower()
        notched = self.statistics.notched
        rows = []
        for row in self._identifier_rows():
            if search and search not in row['id'].lower() and search not in row['name'].lower():
                continue
            if self._hits_only and notched and not (row['hits'] > 0 or row['new']):
                continue
            rows.append(row)
        return rows

    def _update_id_model(self) -> None:
        self._id_model.update(self._filtered_rows())
        self.identifiers_changed.emit()  # The "no identifiers" label and scripts read `identifiers`

    @pyqtSlot(str)
    def set_id_search(self, text: str):
        self._id_search = text or ''
        self._update_id_model()

    @pyqtSlot(bool)
    def set_hits_only(self, enabled: bool):
        self._hits_only = bool(enabled)
        self._update_id_model()

    @pyqtProperty(str, notify=selection_changed)
    def selected(self) -> str:
        return _id_text(self._selected) if self._selected is not None else ''

    @pyqtProperty(str, notify=selection_changed)
    def selected_name(self) -> str:
        return self._name(self._selected) if self._selected is not None else ''

    @pyqtSlot(str)
    def select(self, id_text: str):
        key = _parse_id(id_text)
        if key is not None and key != self._selected:
            self._selected = key
            self._multiplexer = None
            self.selection_changed.emit()
            self.changed.emit()
            self.field_changed.emit()

    def _current(self) -> Optional[IdBitStatistics]:
        return self.statistics.get(self._selected) if self._selected is not None else None

    def _invalidate_bits(self) -> None:
        self._bits_cache = None

    @pyqtProperty('QVariantList', notify=changed)
    def bits(self) -> List[Dict[str, Any]]:
        """The bits of the selected identifier in display order: byte 0 bit 7 to 0, byte 1 bit 7 to 0, …

        Computed once per change and cached, because the QML grid reads it once per byte row (see __init__).

        age: seconds since the bit last changed (relative to the newest frame), -1 if it never changed;
        recent: 1 just after a change, fading to 0 over HIGHLIGHT_SECONDS.
        """
        if self._bits_cache is None:
            self._bits_cache = self._compute_bits()
        return self._bits_cache

    def _compute_bits(self) -> List[Dict[str, Any]]:
        item = self._current()
        if item is None:
            return []
        now = self.statistics.last_time or 0.0
        field = self._field_object()
        in_field = set(field.bits) if field is not None else set()
        result = []
        for byte in range(item.length):
            for bit_in_byte in range(7, -1, -1):
                info = item.bit(byte * 8 + bit_in_byte)
                age = now - info.changed_at if info.changed_at is not None else -1.0
                result.append(
                    {
                        'bit': info.bit,
                        'byte': byte,
                        'value': info.value,
                        'ones': info.ones,
                        'flips': info.flips,
                        'age': age,
                        'recent': max(0.0, 1.0 - age / HIGHLIGHT_SECONDS) if age >= 0 else 0.0,
                        'masked': info.masked,
                        'hit': self.statistics.notched and not info.masked and info.changed_at is not None,
                        'field': info.bit in in_field,
                    }
                )
        return result

    @pyqtProperty('QVariantList', notify=changed)
    def bytes(self) -> List[Dict[str, Any]]:
        """The bytes of the selected identifier: value, distinct values, range, entropy and a histogram."""
        item = self._current()
        if item is None:
            return []
        result = []
        bucket = 256 // HISTOGRAM_BARS
        for info in item.bytes():
            bars = [sum(info.histogram[start : start + bucket]) for start in range(0, 256, bucket)]
            peak = max(bars) or 1
            result.append(
                {
                    'index': info.index,
                    'value': '' if info.value is None else f'{info.value:02X}',
                    'distinct': info.distinct,
                    'minimum': f'{info.minimum:02X}',
                    'maximum': f'{info.maximum:02X}',
                    'entropy': round(info.entropy, 2),
                    'histogram': [count / peak for count in bars],
                }
            )
        return result

    @pyqtProperty(str, notify=changed)
    def last_data(self) -> str:
        item = self._current()
        return item.last.hex(' ').upper() if item is not None else ''

    # Bit field

    @pyqtProperty('QStringList', constant=True)
    def byte_orders(self) -> List[str]:
        return list(BYTE_ORDERS)

    @pyqtProperty('QVariantMap', notify=field_changed)
    def field(self) -> Dict[str, Any]:
        return dict(self._field)

    @pyqtProperty(str, notify=field_changed)
    def field_error(self) -> str:
        return self._field_error

    def _field_object(self) -> Optional[BitField]:
        if self._selected is None:
            return None
        try:
            return BitField(
                self._selected[0],
                self._selected[1],
                int(self._field['start']),
                int(self._field['length']),
                self._field['byte_order'],
                bool(self._field['signed']),
                float(self._field['scale']),
                float(self._field['offset']),
            )
        except (TypeError, ValueError):
            return None

    @pyqtSlot('QVariantMap')
    def set_field(self, fields: Dict[str, Any]):
        """Change any of start, length, byte_order, signed, scale, offset, unit, name."""
        candidate = {**self._field, **{key: value for key, value in fields.items() if key in self._field}}
        try:
            for key in ('start', 'length'):
                candidate[key] = int(candidate[key])
            for key in ('scale', 'offset'):
                candidate[key] = float(candidate[key])
            candidate['signed'] = bool(candidate['signed'])
            candidate['unit'] = str(candidate['unit'] or '')
            candidate['name'] = str(candidate['name'] or '')
            BitField(
                0, False, candidate['start'], candidate['length'], candidate['byte_order'], scale=candidate['scale']
            )
        except (TypeError, ValueError) as e:
            self._field_error = str(e)
        else:
            self._field, self._field_error = candidate, ''
        self.field_changed.emit()
        self.changed.emit()

    @pyqtSlot(int, int)
    def select_bits(self, first: int, last: int):
        """Make the field span bits `first` to `last` (clicked in the bit grid) in the current byte order."""
        field = BitField.spanning(0, False, first, last, self._field['byte_order'])
        self._multiplexer = None
        self.set_field({'start': field.start, 'length': field.length})

    def use_field(
        self,
        field: BitField,
        unit: str = '',
        name: str = '',
        multiplexer: Optional[BitField] = None,
        mux_value: Optional[int] = None,
    ) -> None:
        """Select a field found by the tools: its identifier, bits, conversion and multiplexer condition."""
        self.select(_id_text((field.frame_id, field.extended)))
        self._field = {
            'start': field.start,
            'length': field.length,
            'byte_order': field.byte_order,
            'signed': field.signed,
            'scale': field.scale,
            'offset': field.offset,
            'unit': unit,
            'name': name,
        }
        self._field_error = ''
        self._multiplexer = (multiplexer, mux_value) if multiplexer is not None else None
        self.field_changed.emit()
        self.changed.emit()

    @pyqtProperty(str, notify=field_changed)
    def field_multiplexer(self) -> str:
        """E.g. '600[0|2@1+] = 1' when the field is only in the frames with that multiplexer value."""
        if self._multiplexer is None:
            return ''
        multiplexer, value = self._multiplexer
        return f'{multiplexer.key} = {value}'

    @pyqtSlot()
    def clear_multiplexer(self):
        self._multiplexer = None
        self.field_changed.emit()

    @pyqtProperty(str, notify=field_changed)
    def field_key(self) -> str:
        field = self._field_object()
        return field.key if field is not None else ''

    @pyqtProperty(str, notify=changed)
    def field_value(self) -> str:
        """The field's value in the last frame of the selected identifier."""
        field, item = self._field_object(), self._current()
        if field is None or item is None:
            return ''
        raw = field.raw(item.last)
        if raw is None:
            return 'outside the frame'
        value = raw * field.scale + field.offset
        unit = f' {self._field["unit"]}' if self._field['unit'] else ''
        return f'{value:.10g}{unit} (raw {raw}, 0x{raw & ((1 << field.length) - 1):X})'

    @pyqtSlot(result=str)
    def plot_field(self) -> str:
        """Plot the field on the Signals page. Returns an error, or ''."""
        field = self._field_object()
        if field is None:
            return self._field_error or 'Select an identifier first'
        multiplexer, value = self._multiplexer or (None, None)
        self._connection.track_field(field, self._field['unit'], multiplexer, value)
        return ''

    @pyqtProperty('QVariantMap', notify=field_changed)
    def signal_definition(self) -> Dict[str, Any]:
        """The field as a database signal, for the database editor (see DatabaseEditorController.add_found_signal)."""
        field = self._field_object()
        if field is None:
            return {}
        item = self._current()
        return {
            'frame_id': field.frame_id,
            'extended': field.extended,
            'frame_length': max(item.length if item is not None else 0, field.min_length),
            'name': self._field['name'] or f'Signal_{field.start}',
            'start': field.start,
            'length': field.length,
            'byte_order': field.byte_order,
            'is_signed': field.signed,
            'scale': field.scale,
            'offset': field.offset,
            'unit': self._field['unit'],
            'comment': f'Multiplexed: only in frames with {self.field_multiplexer}' if self._multiplexer else '',
        }
