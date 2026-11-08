"""The in-app help controller for QML: serves the documentation pages (see can_explorer.core.help) to a help panel,
tracks the current page with back/forward history, and opens the page for a given tool or view on request."""

from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import help as docs


class HelpController(QObject):
    """Exposes the help navigation, the current page's rendered Markdown, and navigation (including the "?" button's
    jump to the page for the current tool). The panel's visibility lives here so any part of the app can open it."""

    changed = pyqtSignal()  # Current page (path, title, markdown, online url, history)
    visible_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._history: List[str] = [docs.default_path()]
        self._index = 0
        self._visible = False

    @pyqtProperty('QVariantList', constant=True)
    def sections(self) -> List[Dict[str, Any]]:
        """The table of contents: the sections shown in the app (the user guide), each with its pages."""
        return [
            {'title': section.title, 'pages': [{'title': page.title, 'path': page.path} for page in section.pages]}
            for section in docs.nav_sections()
        ]

    @pyqtProperty(bool, notify=visible_changed)
    def visible(self) -> bool:
        return self._visible

    @pyqtSlot(bool)
    def set_visible(self, visible: bool) -> None:
        if visible != self._visible:
            self._visible = visible
            self.visible_changed.emit()

    @pyqtProperty(str, notify=changed)
    def path(self) -> str:
        return self._history[self._index]

    @pyqtProperty(str, notify=changed)
    def title(self) -> str:
        return docs.title_of(self.path)

    @pyqtProperty(str, notify=changed)
    def markdown(self) -> str:
        return docs.load_page(self.path)

    @pyqtProperty('QVariantList', notify=changed)
    def blocks(self) -> List[Dict[str, str]]:
        """The current page as ordered text/image blocks, so the view can scale images to fit (see docs.page_blocks)."""
        return docs.page_blocks(self.path)

    @pyqtProperty(str, notify=changed)
    def online_url(self) -> str:
        return docs.online_url(self.path)

    @pyqtProperty(bool, notify=changed)
    def can_go_back(self) -> bool:
        return self._index > 0

    @pyqtProperty(bool, notify=changed)
    def can_go_forward(self) -> bool:
        return self._index < len(self._history) - 1

    @pyqtSlot(str)
    def navigate(self, path: str) -> None:
        """Show a help page, recording it in the history (forward entries are discarded, as in a browser)."""
        if not path or path == self.path:
            return
        del self._history[self._index + 1 :]
        self._history.append(path)
        self._index = len(self._history) - 1
        self.changed.emit()

    @pyqtSlot()
    @pyqtSlot(str)
    def open(self, path: str = '') -> None:
        """Open the panel, optionally navigating to a page first."""
        if path:
            self.navigate(path)
        self.set_visible(True)

    @pyqtSlot(str)
    def open_tool(self, key: str) -> None:
        """Open the help page for a tool or view key (the "?" button)."""
        self.open(docs.resolve_tool(key))

    @pyqtSlot()
    def close(self) -> None:
        self.set_visible(False)

    @pyqtSlot()
    def toggle(self) -> None:
        self.set_visible(not self._visible)

    @pyqtSlot()
    def back(self) -> None:
        if self.can_go_back:
            self._index -= 1
            self.changed.emit()

    @pyqtSlot()
    def forward(self) -> None:
        if self.can_go_forward:
            self._index += 1
            self.changed.emit()

    @pyqtSlot(str, result='QVariantList')
    def search(self, query: str) -> List[Dict[str, str]]:
        return docs.search(query)
