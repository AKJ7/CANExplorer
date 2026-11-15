"""The visual panel builder of a connection's simulation (core/panel_builder.py does the work)."""

import dataclasses
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import panel_builder
from can_explorer.core.panel_builder import KINDS, PanelError, PanelLayout, Widget

GRID = 8  # Positions and sizes snap to this


def _snap(value: float) -> int:
    return max(0, int(round(value / GRID)) * GRID)


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


class PanelBuilderController(QObject):
    changed = pyqtSignal()

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self.layout = PanelLayout()
        self._selected = -1
        self._path: Optional[Path] = None
        self._dirty = False
        # Removed with the controller (by Python's finalizer: a slot on Qt's destroyed signal would run while the
        # object is being torn down, which crashes)
        self._preview_tmp = tempfile.TemporaryDirectory(prefix='canexplorer-panel-preview-')
        self._preview_dir = Path(self._preview_tmp.name)
        self._previews = 0

    def _changed(self, dirty: bool = True) -> None:
        self._dirty = self._dirty or dirty
        self.changed.emit()

    # What QML shows

    @pyqtProperty('QVariantList', constant=True)
    def kinds(self) -> List[Dict[str, str]]:
        return [{'kind': kind, 'name': description} for kind, (description, _, _) in KINDS.items()]

    @pyqtProperty('QVariantList', notify=changed)
    def widgets(self) -> List[Dict[str, Any]]:
        return [
            {**dataclasses.asdict(widget), 'name': KINDS[widget.kind][0], 'index': index}
            for index, widget in enumerate(self.layout.widgets)
        ]

    @pyqtProperty(int, notify=changed)
    def selected(self) -> int:
        return self._selected

    @pyqtProperty('QVariantMap', notify=changed)
    def selected_widget(self) -> Dict[str, Any]:
        if 0 <= self._selected < len(self.layout.widgets):
            return self.widgets[self._selected]
        return {}

    @pyqtProperty(str, notify=changed)
    def title(self) -> str:
        return self.layout.title

    @pyqtProperty(int, notify=changed)
    def panel_width(self) -> int:
        return self.layout.width

    @pyqtProperty(int, notify=changed)
    def panel_height(self) -> int:
        return self.layout.height

    @pyqtProperty(str, notify=changed)
    def file_name(self) -> str:
        return self._path.name if self._path else ''

    @pyqtProperty(bool, notify=changed)
    def dirty(self) -> bool:
        return self._dirty

    @pyqtProperty('QStringList', notify=changed)
    def problems(self) -> List[str]:
        return panel_builder.check(self.layout)

    @pyqtProperty(str, notify=changed)
    def qml(self) -> str:
        """The panel file as it would be saved ('' while it has problems): the preview loads it."""
        try:
            return panel_builder.generate_qml(self.layout)
        except PanelError:
            return ''

    @pyqtSlot(result=str)
    def preview_url(self) -> str:
        """The panel as it would be saved, written to a temporary file the preview loads like the Simulation view
        loads panels ('' while it has problems). A new name each time: QML caches components by URL."""
        text = self.qml
        if not text:
            return ''
        for old in self._preview_dir.glob('preview-*.qml'):
            old.unlink(missing_ok=True)
        self._previews += 1
        path = self._preview_dir / f'preview-{self._previews}.qml'
        path.write_text(text, encoding='utf-8')
        return QUrl.fromLocalFile(str(path)).toString()

    @pyqtProperty('QStringList', notify=changed)
    def sources(self) -> List[str]:
        """What display widgets can show: the signals of the connection's database and the shared variables."""
        database = self.connection.session.decoder.database
        signals = [f'signal:{info.key}' for info in database.signals()] if database is not None else []
        variables = [f'variable:{name}' for name, _ in sorted(self.connection.simulation.runtime.variables.items())]
        return variables + signals

    @pyqtProperty('QStringList', constant=True)
    def actions(self) -> List[str]:
        return list(panel_builder.ACTIONS)

    @pyqtProperty('QStringList', constant=True)
    def lamp_colors(self) -> List[str]:
        return list(panel_builder.LAMP_COLORS)

    @pyqtProperty(int, constant=True)
    def grid(self) -> int:
        return GRID

    # Editing

    @pyqtSlot()
    def new_panel(self):
        self.layout, self._selected, self._path, self._dirty = PanelLayout(), -1, None, False
        self.changed.emit()

    @pyqtSlot(str, float, float, result=int)
    def add(self, kind: str, x: float, y: float) -> int:
        """Add a widget at a point of the panel (snapped to the grid); returns its index, selected."""
        widget = Widget.new(kind, _snap(x), _snap(y))
        widget.x = min(widget.x, max(0, self.layout.width - widget.width))
        widget.y = min(widget.y, max(0, self.layout.height - widget.height))
        self.layout.widgets.append(widget)
        self._selected = len(self.layout.widgets) - 1
        self._changed()
        return self._selected

    @pyqtSlot(int)
    def select(self, index: int):
        self._selected = index if 0 <= index < len(self.layout.widgets) else -1
        self._changed(dirty=False)

    @pyqtSlot(float, float, result=int)
    def select_at(self, x: float, y: float) -> int:
        found = panel_builder.find(self.layout, x, y)
        self.select(-1 if found is None else found)
        return self._selected

    @pyqtSlot(int, float, float)
    def move(self, index: int, x: float, y: float):
        if 0 <= index < len(self.layout.widgets):
            widget = self.layout.widgets[index]
            widget.x = min(_snap(x), max(0, self.layout.width - widget.width))
            widget.y = min(_snap(y), max(0, self.layout.height - widget.height))
            self._changed()

    @pyqtSlot(int, float, float)
    def resize(self, index: int, width: float, height: float):
        if 0 <= index < len(self.layout.widgets):
            widget = self.layout.widgets[index]
            widget.width = max(panel_builder.MIN_SIZE, min(_snap(width), self.layout.width - widget.x))
            widget.height = max(panel_builder.MIN_SIZE, min(_snap(height), self.layout.height - widget.y))
            self._changed()

    @pyqtSlot(int, 'QVariantMap')
    def update(self, index: int, props: Dict[str, Any]):
        """Change properties of a widget (only those of its kind)."""
        if 0 <= index < len(self.layout.widgets):
            widget = self.layout.widgets[index]
            widget.props.update({key: value for key, value in props.items() if key in widget.props})
            self._changed()

    @pyqtSlot(int)
    def remove(self, index: int):
        if 0 <= index < len(self.layout.widgets):
            del self.layout.widgets[index]
            self._selected = -1
            self._changed()

    @pyqtSlot(int, result=int)
    def duplicate(self, index: int) -> int:
        if not 0 <= index < len(self.layout.widgets):
            return -1
        copy = dataclasses.replace(self.layout.widgets[index])
        copy.props = dict(copy.props)
        copy.x, copy.y = copy.x + GRID * 2, copy.y + GRID * 2
        self.layout.widgets.append(copy)
        self._selected = len(self.layout.widgets) - 1
        self._changed()
        return self._selected

    @pyqtSlot(int, int)
    def raise_widget(self, index: int, step: int):
        """Move a widget up (step > 0) or down the stacking order."""
        target = index + (1 if step > 0 else -1)
        widgets = self.layout.widgets
        if 0 <= index < len(widgets) and 0 <= target < len(widgets):
            widgets[index], widgets[target] = widgets[target], widgets[index]
            self._selected = target
            self._changed()

    @pyqtSlot(str, int, int)
    def set_panel(self, title: str, width: int, height: int):
        self.layout.title = title.strip() or 'Panel'
        self.layout.width = max(160, min(int(width), panel_builder.MAX_SIZE))
        self.layout.height = max(120, min(int(height), panel_builder.MAX_SIZE))
        self._changed()

    # Files

    @pyqtSlot(QUrl, result=str)
    def open(self, url: QUrl) -> str:
        """Open a panel the builder made. Returns an error, or ''."""
        path = _local_path(url)
        try:
            self.layout = panel_builder.load(path)
        except PanelError as e:
            return str(e)
        self._path, self._selected, self._dirty = path, -1, False
        self.changed.emit()
        return ''

    @pyqtSlot(QUrl, result=str)
    def save(self, url: QUrl) -> str:
        """Save as a panel file (an empty URL: the file it was opened from or last saved to). Returns an error."""
        path = _local_path(url) if not url.isEmpty() else self._path
        if path is None:
            return 'Choose a file name'
        try:
            self._path = panel_builder.save(self.layout, path)
        except (PanelError, OSError) as e:
            return str(e)
        self._dirty = False
        self.changed.emit()
        return ''

    @pyqtSlot(result=str)
    def use_in_simulation(self) -> str:
        """Add the saved panel to the Simulation view (saving it first). Returns an error, or ''."""
        if self._path is None:
            return 'Save the panel first'
        error = self.save(QUrl())
        return error or self.connection.simulation.add_panel(QUrl.fromLocalFile(str(self._path)))
