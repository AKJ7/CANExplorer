import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import can
from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core.compare import (
    ALIGN_ID,
    ALIGN_SEQUENCE,
    ALIGN_START,
    Comparison,
    Recording,
    compare,
    key_text,
    read_recording,
    summary_csv,
)
from can_explorer.core.decoding import open_signal_database
from can_explorer.core.frames import format_hex
from can_explorer.core.log_formats import file_filters, is_log_file, unsupported_message
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

ALIGNMENTS = (
    (ALIGN_START, 'By start time'),
    (ALIGN_ID, 'By first frame of an identifier'),
    (ALIGN_SEQUENCE, 'By frame sequence'),
)


def _ms(seconds: Optional[float]) -> str:
    return '' if seconds is None else f'{seconds * 1000:.1f}'


def _value(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float):
        return f'{value:.6g}'
    return str(value)


def _path(url: QUrl) -> str:
    return url.toLocalFile() if url.isLocalFile() else url.toString()


class CompareController(QObject):
    """Comparing two recordings: identifiers, cycle times, payloads and, with a database, signal values."""

    changed = pyqtSignal()
    result_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._recordings: Dict[str, Optional[Recording]] = {'a': None, 'b': None}
        self._loading: Dict[str, bool] = {'a': False, 'b': False}
        self._errors: Dict[str, str] = {'a': '', 'b': ''}
        self._align = ALIGN_START
        self._align_key = None
        self._database = None
        self._comparison: Optional[Comparison] = None
        self._comparing = False
        self._generation = 0

    # Recordings

    @pyqtProperty('QStringList', constant=True)
    def file_filters(self) -> List[str]:
        return file_filters() + ['All files (*)']

    @pyqtSlot(str, QUrl, result=str)
    def open_file(self, side: str, url: QUrl) -> str:
        """Read recording 'a' or 'b' in the background. Returns an error or ''."""
        path = _path(url)
        if side not in self._recordings:
            return f'Unknown side {side!r}'
        if not is_log_file(path):
            return unsupported_message(path)
        self._loading[side] = True
        self._errors[side] = ''
        self.changed.emit()
        asyncio.ensure_future(self._read(side, path))
        return ''

    async def _read(self, side: str, path: str):
        try:
            recording = await asyncio.to_thread(read_recording, path)
        except Exception as e:
            logger.warning(f'Could not read {path}: {e}')
            self._errors[side] = f'Could not read {Path(path).name}: {e}'
            recording = None
        self._recordings[side] = recording
        self._loading[side] = False
        self.changed.emit()
        await self._compare()

    def set_recording(self, side: str, recording: Optional[Recording]):
        """For tests and scripts: use an already read recording."""
        self._recordings[side] = recording
        self.changed.emit()

    def _info(self, side: str) -> Dict[str, Any]:
        recording = self._recordings[side]
        info = {'loading': self._loading[side], 'error': self._errors[side], 'file': '', 'name': '', 'text': ''}
        if recording is not None:
            info.update(
                file=str(recording.path),
                name=recording.path.name,
                text=f'{recording.frames} frames · {len(recording.traces)} identifiers · {recording.duration:.1f} s',
            )
        return info

    @pyqtProperty('QVariantMap', notify=changed)
    def recording_a(self) -> Dict[str, Any]:
        return self._info('a')

    @pyqtProperty('QVariantMap', notify=changed)
    def recording_b(self) -> Dict[str, Any]:
        return self._info('b')

    @pyqtSlot()
    def swap(self):
        self._recordings['a'], self._recordings['b'] = self._recordings['b'], self._recordings['a']
        self._errors['a'], self._errors['b'] = self._errors['b'], self._errors['a']
        self.changed.emit()
        asyncio.ensure_future(self._compare())

    # Options

    @pyqtProperty('QVariantList', constant=True)
    def alignments(self) -> List[Dict[str, str]]:
        return [{'value': value, 'name': name} for value, name in ALIGNMENTS]

    @pyqtProperty(str, notify=changed)
    def align(self) -> str:
        return self._align

    @pyqtSlot(str, str, result=str)
    def set_alignment(self, mode: str, identifier: str) -> str:
        """mode: 'start', 'id' (with the identifier in hex, e.g. 100 or 18FEF100) or 'sequence'."""
        if mode not in (value for value, _ in ALIGNMENTS):
            return f'Unknown alignment {mode!r}'
        key = None
        if mode == ALIGN_ID:
            text = identifier.strip()
            try:
                value = int(text, 16)
            except ValueError:
                return f'Invalid hex identifier: {text!r}'
            if not 0 <= value <= 0x1FFFFFFF:
                return f'Identifier out of range: {text}'
            key = (value, value > 0x7FF or len(text) > 3)
        self._align, self._align_key = mode, key
        self.changed.emit()
        asyncio.ensure_future(self._compare())
        return ''

    @pyqtProperty(str, notify=changed)
    def database_file(self) -> str:
        return str(self._database.path) if self._database is not None else ''

    @pyqtSlot(QUrl, result=str)
    def load_database(self, url: QUrl) -> str:
        path = _path(url)
        try:
            self._database = open_signal_database(path)
        except Exception as e:
            return f'Could not load {Path(path).name}: {e}'
        self.changed.emit()
        asyncio.ensure_future(self._compare())
        return ''

    @pyqtSlot()
    def unload_database(self):
        self._database = None
        self.changed.emit()
        asyncio.ensure_future(self._compare())

    # Results

    async def _compare(self):
        a, b = self._recordings['a'], self._recordings['b']
        self._generation += 1
        generation = self._generation
        if a is None or b is None:
            self._comparison = None
            self.result_changed.emit()
            return
        self._comparing = True
        self.changed.emit()
        try:
            comparison = await asyncio.to_thread(compare, a, b, self._align, self._align_key, self._database)
        except Exception as e:
            logger.warning(f'Comparing failed: {e}')
            comparison = None
        if generation == self._generation:  # Not overtaken by a newer comparison
            self._comparison = comparison
            self._comparing = False
            self.changed.emit()
            self.result_changed.emit()

    async def refresh(self):
        """Compare again and wait for the result (for tests and scripts)."""
        await self._compare()

    @pyqtProperty(bool, notify=changed)
    def busy(self) -> bool:
        return self._comparing or any(self._loading.values())

    @pyqtProperty(bool, notify=result_changed)
    def has_result(self) -> bool:
        return self._comparison is not None

    @pyqtProperty(str, notify=result_changed)
    def align_error(self) -> str:
        return self._comparison.align_error if self._comparison is not None else ''

    @pyqtProperty('QVariantMap', notify=result_changed)
    def counts(self) -> Dict[str, int]:
        return self._comparison.counts() if self._comparison is not None else {}

    def _message_name(self, key) -> str:
        if self._database is None:
            return ''
        definition = self._database.message_definition(can.Message(arbitration_id=key[0], is_extended_id=key[1]))
        return definition.name if definition is not None else ''

    @pyqtProperty('QVariantList', notify=result_changed)
    def identifiers(self) -> List[Dict[str, Any]]:
        if self._comparison is None:
            return []
        order = {'changed': 0, 'only_a': 1, 'only_b': 2, 'same': 3}
        result = []
        for difference in sorted(self._comparison.ids, key=lambda difference: order[difference.status]):
            first = difference.first_difference
            change = difference.cycle_change
            result.append(
                {
                    'id': key_text(difference.key),
                    'name': self._message_name(difference.key),
                    'status': difference.status,
                    'count_a': difference.count_a,
                    'count_b': difference.count_b,
                    'cycle_a': _ms(difference.cycle_a),
                    'cycle_b': _ms(difference.cycle_b),
                    'cycle_change': '' if change is None or abs(change) < 0.01 else f'{change:+.0%}',
                    'paired': difference.paired,
                    'differing': difference.differing,
                    'first_time': f'{first[0]:.3f}' if first else '',
                    'data_a': format_hex(first[1]) if first else '',
                    'data_b': format_hex(first[2]) if first else '',
                    'notes': ', '.join(difference.reasons),
                }
            )
        return result

    @pyqtProperty('QVariantList', notify=result_changed)
    def signal_differences(self) -> List[Dict[str, Any]]:
        if self._comparison is None:
            return []
        result = []
        for difference in self._comparison.signals:
            first = difference.first_difference
            result.append(
                {
                    'key': difference.key,
                    'unit': difference.unit,
                    'changed': difference.changed,
                    'range_a': (
                        f'{_value(difference.min_a)} … {_value(difference.max_a)}'
                        if difference.min_a is not None
                        else ''
                    ),
                    'range_b': (
                        f'{_value(difference.min_b)} … {_value(difference.max_b)}'
                        if difference.min_b is not None
                        else ''
                    ),
                    'mean_a': _value(difference.mean_a),
                    'mean_b': _value(difference.mean_b),
                    'paired': difference.paired,
                    'differing': difference.differing,
                    'max_difference': _value(difference.max_difference) if difference.differing else '',
                    'first': f'{first[0]:.3f} s: {_value(first[1])} → {_value(first[2])}' if first else '',
                }
            )
        return result

    @pyqtSlot(QUrl, result=str)
    def export_csv(self, url: QUrl) -> str:
        """Save the identifier comparison; returns a message for the user."""
        if self._comparison is None:
            return 'Open two recordings first'
        path = Path(_path(url))
        try:
            path.write_text(summary_csv(self._comparison), encoding='utf-8')
        except OSError as e:
            return f'Could not write {path.name}: {e.strerror or e}'
        return f'Saved {path.name}'
