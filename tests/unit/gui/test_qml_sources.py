import re
from pathlib import Path

from can_explorer.util.gui import get_res_path

QML_ROOT = Path(get_res_path('.'))
QML_FILES = sorted(QML_ROOT.rglob('*.qml'))
# The QtQuick.Dialogs import or its non-native dialog types, outside comments
DIALOGS = re.compile(r'^\s*import\s+QtQuick\.Dialogs\b|\b(?:FileDialog|FolderDialog)\s*\{', re.MULTILINE)


def test_qml_files_are_found():
    assert len(QML_FILES) > 50 and QML_ROOT / 'main_window.qml' in QML_FILES


def test_no_qml_uses_qt_quick_dialogs():
    """Qt Quick's FileDialog/FolderDialog fall back to a QML implementation that cannot load from the PyQt6 wheels
    on desktops without a native dialog, and Qt 6.9 then crashes (issue #17). CI runs offscreen, where that fallback
    is never taken, so only this test catches a new use: open files with FileChooser (gui/file_picker.py)."""
    offenders = []
    for path in QML_FILES:
        text = re.sub(r'//[^\n]*', '', path.read_text(encoding='utf-8'))  # Comments may name them
        for match in DIALOGS.finditer(text):
            line = text.count('\n', 0, match.start()) + 1
            offenders.append(f'{path.relative_to(QML_ROOT)}:{line}: {match.group(0).strip()}')
    assert offenders == [], 'Use FileChooser instead of QtQuick.Dialogs:\n' + '\n'.join(offenders)
