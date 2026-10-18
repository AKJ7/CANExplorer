import importlib.util
from pathlib import Path

from PyQt6.QtCore import QCoreApplication

from can_explorer.gui.translations import (
    TranslationManager,
    TsTranslator,
    available_languages,
    read_ts,
    resolve_language,
    translation_file,
)

ROOT = Path(__file__).resolve().parents[3]


def load_tool():
    spec = importlib.util.spec_from_file_location('update_translations', ROOT / 'tools' / 'update_translations.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_languages_and_files():
    assert ('en', 'English') in available_languages() and ('de', 'Deutsch') in available_languages()
    assert resolve_language('de') == 'de' and resolve_language('xx') == 'en'
    messages = read_ts(translation_file('de'))
    assert messages[('Sidebar', 'CONNECTIONS')] == 'VERBINDUNGEN'
    assert messages[('main_window', 'Disconnect %1')] == '%1 trennen'


def test_translation_file_is_up_to_date():
    """Every qsTr text of the QML files is in the translation files (run tools/update_translations.py)."""
    tool = load_tool()
    sources = {(context, source) for context, source, _, _ in tool.extract()}
    for code, _ in available_languages():
        if code == 'en':
            continue
        in_file = set(tool.existing_translations(translation_file(code)))
        missing = sorted(sources - in_file)[:5]
        assert not missing, f'Run tools/update_translations.py: {missing} are missing in {translation_file(code).name}'


def test_extraction_handles_concatenation(tmp_path):
    tool = load_tool()
    qml = tmp_path / 'qt' / 'Sample.qml'
    qml.parent.mkdir()
    qml.write_text('Label { text: qsTr("First " +\n  "second \\"quoted\\"").arg(1) }\nLabel { text: qsTr("Plain") }\n')
    messages = tool.extract(qml.parent)
    assert [(context, source) for context, source, _, _ in messages] == [
        ('Sample', 'First second "quoted"'),
        ('Sample', 'Plain'),
    ]


def test_translator(qapp, tmp_path):
    translator = TsTranslator(translation_file('de'))
    QCoreApplication.installTranslator(translator)
    try:
        assert QCoreApplication.translate('TopBar', 'Disconnect') == 'Trennen'
        assert QCoreApplication.translate('TopBar', 'Not in the file') == 'Not in the file'
    finally:
        QCoreApplication.removeTranslator(translator)
    manager = TranslationManager(qapp)
    assert manager.set_language('de') == 'de' and QCoreApplication.translate('Sidebar', 'TOOLS') == 'WERKZEUGE'
    assert manager.set_language('en') == 'en' and QCoreApplication.translate('Sidebar', 'TOOLS') == 'TOOLS'
