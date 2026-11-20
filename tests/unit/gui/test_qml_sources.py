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


def test_menu_icons_exist_and_are_not_icon_theme_names():
    """The application menu draws bundled line icons (Actions.menuIcon): an icon theme name shows nothing on
    Windows and macOS, and a misspelt icon file only shows as a blank space, without a QML warning."""
    actions = (QML_ROOT / 'CanExplorer' / 'Actions.qml').read_text(encoding='utf-8')
    assert 'icon.name' not in actions
    used = set()
    for path in QML_FILES:
        used.update(re.findall(r'menuIcon\("([\w-]+)"\)', path.read_text(encoding='utf-8')))
    assert len(used) > 15
    missing = sorted(
        name for name in used if not (QML_ROOT / 'CanExplorer' / 'images' / 'menu' / f'{name}.svg').is_file()
    )
    assert missing == []
