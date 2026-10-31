"""Python syntax highlighting for the reverse-engineering console's editor.

A QSyntaxHighlighter attached to the editor's QTextDocument (from QML: PythonHighlighter { document: input.textDocument }).
The per-line tokenising is a pure function (single_line_spans) so it can be tested; the highlighter adds the
multi-line (triple-quoted) string handling and the colours, which come from the theme via QML properties.
"""

import keyword
import re
from typing import List, Optional, Tuple

from PyQt6.QtCore import pyqtProperty
from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtQml import qmlRegisterType
from PyQt6.QtQuick import QQuickTextDocument

from can_explorer.core.reverse.console import CONSOLE_NAMES

KEYWORDS = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist)
NAMES = frozenset(CONSOLE_NAMES)
STRING_PREFIXES = frozenset(['r', 'b', 'f', 'u', 'rb', 'br', 'fr', 'rf'])

_WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_NUMBER = re.compile(r'0[xXoObB][0-9a-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?[jJ]?')

# Span kinds: 'keyword', 'name', 'number', 'string', 'comment', 'definition'
Span = Tuple[int, int, str]  # (start, length, kind)


def single_line_spans(text: str) -> List[Span]:
    """Tokenise one line into coloured spans, without multi-line (triple-quoted) strings. Scanning character by
    character keeps a '#' inside a string from looking like a comment, and a keyword inside a string plain."""
    spans: List[Span] = []
    index, length = 0, len(text)
    previous_word = ''
    while index < length:
        char = text[index]
        if char == '#':
            spans.append((index, length - index, 'comment'))
            break
        # A string, optionally with a prefix already matched as a word just before it
        if char in '\'"':
            end = _string_end(text, index)
            start = index
            if spans and spans[-1][2] == 'word-prefix':  # Fold a prefix (r"", f"", …) into the string
                start = spans.pop()[0]
            spans.append((start, end - start, 'string'))
            index = end
            continue
        if char.isdigit() or (char == '.' and index + 1 < length and text[index + 1].isdigit()):
            match = _NUMBER.match(text, index)
            if match:
                spans.append((index, match.end() - index, 'number'))
                index = match.end()
                continue
        if char.isalpha() or char == '_':
            match = _WORD.match(text, index)
            word, end = match.group(), match.end()
            if word in KEYWORDS:
                spans.append((index, end - index, 'keyword'))
            elif previous_word in ('def', 'class'):
                spans.append((index, end - index, 'definition'))
            elif word in NAMES:
                spans.append((index, end - index, 'name'))
            elif word.lower() in STRING_PREFIXES and end < length and text[end] in '\'"':
                spans.append((index, end - index, 'word-prefix'))  # Resolved when the string follows
            previous_word = word
            index = end
            continue
        if not char.isspace():
            previous_word = ''
        index += 1
    return [span for span in spans if span[2] != 'word-prefix']  # Drop any unresolved prefix marker


def _string_end(text: str, start: int) -> int:
    """The index just past a single-line string beginning at `start` (handling backslash escapes)."""
    quote = text[start]
    index = start + 1
    length = len(text)
    while index < length:
        if text[index] == '\\':
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return length  # Unterminated: to the end of the line


class PythonHighlighter(QSyntaxHighlighter):
    """Highlights Python in the document of a QML TextArea, with theme colours and triple-quoted strings."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._quick_document: Optional[QQuickTextDocument] = None
        self._colors = {
            'keyword': QColor('#c678dd'),
            'name': QColor('#61afef'),
            'number': QColor('#d19a66'),
            'string': QColor('#98c379'),
            'comment': QColor('#7f848e'),
            'definition': QColor('#e5c07b'),
        }
        self._formats = {}
        self._rebuild_formats()

    def _get_quick_document(self) -> Optional[QQuickTextDocument]:
        return self._quick_document

    def _set_quick_document(self, document: Optional[QQuickTextDocument]) -> None:
        if document is self._quick_document:
            return
        self._quick_document = document
        self.setDocument(document.textDocument() if document is not None else None)

    # The QML TextArea's textDocument to highlight
    quickDocument = pyqtProperty(QQuickTextDocument, _get_quick_document, _set_quick_document)

    def _set_color(self, kind: str, color) -> None:
        color = QColor(color)
        if self._colors.get(kind) != color:
            self._colors[kind] = color
            self._rebuild_formats()
            self.rehighlight()

    def _rebuild_formats(self) -> None:
        self._formats = {}
        for kind, color in self._colors.items():
            text_format = QTextCharFormat()
            text_format.setForeground(color)
            if kind in ('keyword', 'definition'):
                text_format.setFontWeight(QFont.Weight.DemiBold)
            self._formats[kind] = text_format

    def highlightBlock(self, text: str) -> None:
        for start, length, kind in single_line_spans(text):
            self.setFormat(start, length, self._formats[kind])
        # Triple-quoted strings spanning blocks (overwriting the single-line result inside them)
        self.setCurrentBlockState(-1)
        if not self._match_triple(text, "'''", 1):
            self._match_triple(text, '"""', 2)

    def _match_triple(self, text: str, delimiter: str, state: int) -> bool:
        if self.previousBlockState() == state:
            start, offset = 0, 0
        else:
            start = text.find(delimiter)
            offset = len(delimiter)
        while start >= 0:
            end = text.find(delimiter, start + offset)
            if end >= 0:
                span = end - start + offset + len(delimiter)
                self.setCurrentBlockState(-1)
            else:
                self.setCurrentBlockState(state)
                span = len(text) - start
            self.setFormat(start, span, self._formats['string'])
            start = text.find(delimiter, start + span)
            offset = len(delimiter)
        return self.currentBlockState() == state

    # Theme colours, set from QML
    keywordColor = pyqtProperty(
        QColor, lambda self: self._colors['keyword'], lambda self, color: self._set_color('keyword', color)
    )
    nameColor = pyqtProperty(
        QColor, lambda self: self._colors['name'], lambda self, color: self._set_color('name', color)
    )
    numberColor = pyqtProperty(
        QColor, lambda self: self._colors['number'], lambda self, color: self._set_color('number', color)
    )
    stringColor = pyqtProperty(
        QColor, lambda self: self._colors['string'], lambda self, color: self._set_color('string', color)
    )
    commentColor = pyqtProperty(
        QColor, lambda self: self._colors['comment'], lambda self, color: self._set_color('comment', color)
    )
    definitionColor = pyqtProperty(
        QColor, lambda self: self._colors['definition'], lambda self, color: self._set_color('definition', color)
    )


_registered = False


def register_python_highlighter() -> None:
    global _registered
    if not _registered:
        qmlRegisterType(PythonHighlighter, 'CanExplorer.Plots', 1, 0, 'PythonHighlighter')
        _registered = True
