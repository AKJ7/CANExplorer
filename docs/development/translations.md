# Translations

The texts of the user interface are written in English in the QML files, wrapped in `qsTr("…")`. Translations live
in Qt Linguist files, one per language, in `can_explorer/resources/translations/canexplorer_<code>.ts` (e.g.
`canexplorer_de.ts` for German). The application reads these files directly; no compiled `.qm` files are needed.

## Updating the translation files

After adding or changing texts in QML files, update the translation files:

```bash
uv run python tools/update_translations.py
```

New texts are added untranslated, texts that no longer exist are removed, and existing translations are kept. The
test suite checks that the files contain every text (`tests/unit/gui/test_translations.py`).

## Adding a language

```bash
uv run python tools/update_translations.py es
```

creates `canexplorer_es.ts`. Translate it with Qt Linguist (`linguist canexplorer_es.ts`), or in a text editor:
fill in each `<translation>` and remove its `type="unfinished"` attribute. Keep placeholders such as `%1` and the
`&` of menu shortcuts. The language appears in **Preferences → Language** and in the command palette once the
file exists; its name comes from `LANGUAGE_NAMES` in `can_explorer/gui/translations.py`.

## How it works

- The context of a text is the name of its QML file, as with Qt's `qsTr`; the same English text can be translated
  differently in two files.
- `TsTranslator` (in `can_explorer/gui/translations.py`) is a `QTranslator` reading the `.ts` file. Untranslated
  texts fall back to English.
- Changing the language calls `QQmlEngine.retranslate()`, which evaluates every `qsTr` binding again, so the window
  switches language at once.
- Messages created in Python, such as error messages of the controllers, are not translated yet.
