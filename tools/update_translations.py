"""Create or update the translation files (Qt Linguist .ts) with the texts of the QML files.

Usage: uv run python tools/update_translations.py [LANGUAGE ...]

Without languages, the existing files in can_explorer/resources/translations are updated; with them (e.g. `fr`),
files for these languages are created as well. Existing translations are kept; texts no longer in the QML files are
dropped and new ones are added untranslated. The context of a text is the name of its QML file, as for qsTr.
"""

import ast
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from can_explorer.gui.translations import PREFIX, TRANSLATIONS_DIR, translation_file  # noqa: E402

QML_DIRECTORY = ROOT / 'can_explorer' / 'gui' / 'qt'
STRING = r'"(?:[^"\\]|\\.)*"'
QSTR = re.compile(rf'qsTr\(\s*({STRING}(?:\s*\+\s*{STRING})*)\s*[,)]')

Message = Tuple[str, str, str, int]  # context, source, file (relative to the .ts file), line


def extract(directory: Path = QML_DIRECTORY) -> List[Message]:
    messages: List[Message] = []
    seen = set()
    for path in sorted(directory.rglob('*.qml')):
        text = path.read_text(encoding='utf-8')
        for match in QSTR.finditer(text):
            source = ''.join(ast.literal_eval(part) for part in re.findall(STRING, match.group(1)))
            key = (path.stem, source)
            if key in seen:
                continue
            seen.add(key)
            line = text.count('\n', 0, match.start()) + 1
            try:  # Relative to the .ts files in can_explorer/resources/translations
                relative = Path('..', '..', path.relative_to(ROOT / 'can_explorer')).as_posix()
            except ValueError:
                relative = path.relative_to(directory).as_posix()
            messages.append((path.stem, source, relative, line))
    return messages


def existing_translations(path: Path) -> Dict[Tuple[str, str], Tuple[str, bool]]:
    """{(context, source): (translation, finished)}."""
    if not path.exists():
        return {}
    result = {}
    for context in ET.parse(path).getroot().iter('context'):
        name = context.findtext('name', '')
        for message in context.iter('message'):
            translation = message.find('translation')
            if translation is None:
                continue
            finished = translation.get('type') not in ('unfinished', 'obsolete', 'vanished')
            result[(name, message.findtext('source', ''))] = (translation.text or '', finished)
    return result


def write(path: Path, language: str, messages: List[Message]) -> Tuple[int, int]:
    """Write the .ts file; returns (messages, translated)."""
    old = existing_translations(path)
    root = ET.Element('TS', version='2.1', language=language)
    contexts: Dict[str, ET.Element] = {}
    translated = 0
    for context, source, filename, line in messages:
        if context not in contexts:
            contexts[context] = ET.SubElement(root, 'context')
            ET.SubElement(contexts[context], 'name').text = context
        message = ET.SubElement(contexts[context], 'message')
        ET.SubElement(message, 'location', filename=filename, line=str(line))
        ET.SubElement(message, 'source').text = source
        text, finished = old.get((context, source), ('', False))
        element = ET.SubElement(message, 'translation')
        element.text = text
        if not (finished and text):
            element.set('type', 'unfinished')
        else:
            translated += 1
    ET.indent(root, space='    ')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n' + ET.tostring(root, encoding='unicode') + '\n',
        encoding='utf-8',
    )
    return len(messages), translated


def main(languages: List[str]) -> None:
    messages = extract()
    codes = sorted({path.stem.removeprefix(PREFIX) for path in TRANSLATIONS_DIR.glob(f'{PREFIX}*.ts')} | set(languages))
    if not codes:
        print('No translation files yet; name the languages to create, e.g. de')
    for code in codes:
        total, translated = write(translation_file(code), code, messages)
        print(f'{translation_file(code).relative_to(ROOT)}: {translated} of {total} texts translated')


if __name__ == '__main__':
    main(sys.argv[1:])
