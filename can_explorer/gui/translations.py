"""Translations of the user interface.

The texts of the QML files (`qsTr`) are collected in Qt Linguist files, `resources/translations/canexplorer_<code>.ts`
(`tools/update_translations.py` creates and updates them; translate them with Qt Linguist or a text editor).
`TsTranslator` reads these files directly, so no compiled `.qm` files are needed. Texts without a translation stay
in English.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QCoreApplication, QLocale, QTranslator

from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / 'resources' / 'translations'
PREFIX = 'canexplorer_'
LANGUAGE_NAMES = {'en': 'English', 'de': 'Deutsch', 'fr': 'Français', 'es': 'Español', 'it': 'Italiano'}


def translation_file(code: str, directory: Path = TRANSLATIONS_DIR) -> Path:
    return directory / f'{PREFIX}{code}.ts'


def available_languages(directory: Path = TRANSLATIONS_DIR) -> List[Tuple[str, str]]:
    """(code, name) of English and every language with a translation file."""
    codes = sorted(path.stem.removeprefix(PREFIX) for path in directory.glob(f'{PREFIX}*.ts'))
    return [('en', LANGUAGE_NAMES['en'])] + [(code, LANGUAGE_NAMES.get(code, code)) for code in codes if code != 'en']


def read_ts(path: Path) -> Dict[Tuple[str, str], str]:
    """{(context, source): translation} of the finished and unfinished-but-filled messages of a .ts file."""
    messages = {}
    root = ET.parse(path).getroot()
    for context in root.iter('context'):
        name = context.findtext('name', '')
        for message in context.iter('message'):
            source = message.findtext('source')
            translation = message.find('translation')
            if source is None or translation is None or translation.get('type') in ('obsolete', 'vanished'):
                continue
            if translation.text:
                messages[(name, source)] = translation.text
    return messages


class TsTranslator(QTranslator):
    """Translates with the messages of a Qt Linguist .ts file."""

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path
        self.messages = read_ts(path)

    def translate(self, context, source_text, disambiguation=None, n=-1):  # noqa: D102 (Qt override)
        return self.messages.get((context, source_text))  # None: not translated, the next translator or English

    def isEmpty(self) -> bool:  # noqa: N802 (Qt override)
        return not self.messages


def resolve_language(code: str) -> str:
    """The language to use for a setting: '' follows the system language, when a translation exists."""
    available = {language for language, _ in available_languages()}
    if not code:
        code = QLocale.system().name().split('_')[0]
    return code if code in available else 'en'


class TranslationManager:
    """Installs the translation of the chosen language in the application."""

    def __init__(self, application: QCoreApplication):
        self._application = application
        self._translator: Optional[TsTranslator] = None
        self.language = 'en'

    def set_language(self, code: str) -> str:
        """Use a language ('' for the system's); returns the language in use."""
        language = resolve_language(code)
        if self._translator is not None:
            self._application.removeTranslator(self._translator)
            self._translator = None
        if language != 'en':
            path = translation_file(language)
            try:
                self._translator = TsTranslator(path, parent=self._application)
            except (OSError, ET.ParseError) as e:
                logger.error(f'Could not read the translation {path}: {e}')
                language = 'en'
            else:
                self._application.installTranslator(self._translator)
        self.language = language
        return language
