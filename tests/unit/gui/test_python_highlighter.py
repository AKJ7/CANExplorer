from can_explorer.core.reverse.console import CONSOLE_NAMES, build_namespace
from can_explorer.gui.python_highlighter import single_line_spans


def kinds(text):
    return {text[start : start + length]: kind for start, length, kind in single_line_spans(text)}


def test_keywords_names_numbers():
    spans = kinds("for pid in ids():  # loop")
    assert spans['for'] == 'keyword' and spans['in'] == 'keyword'
    assert spans['ids'] == 'name'
    assert spans['# loop'] == 'comment'


def test_strings_hide_keywords_and_hashes():
    # The '#' and the keyword 'for' inside the string must not be a comment or a keyword
    spans = single_line_spans("x = 'for # not a comment'")
    string_spans = [s for s in spans if s[2] == 'string']
    assert len(string_spans) == 1
    start, length, _ = string_spans[0]
    assert "'for # not a comment'" == "x = 'for # not a comment'"[start : start + length]
    assert not any(kind in ('keyword', 'comment') for _, _, kind in spans)


def test_numbers_including_hex_and_float():
    spans = kinds("v = 0x1A0 + 3.5e2")
    assert spans['0x1A0'] == 'number' and spans['3.5e2'] == 'number'


def test_string_prefix_is_folded_into_the_string():
    spans = single_line_spans('name = f"id {pid}"')
    strings = [s for s in spans if s[2] == 'string']
    assert len(strings) == 1
    start, length, _ = strings[0]
    assert 'name = f"id {pid}"'[start : start + length] == 'f"id {pid}"'


def test_definition_name_after_def():
    spans = kinds('def decode(x): pass')
    assert spans['def'] == 'keyword' and spans['decode'] == 'definition'


def test_console_names_match_the_namespace():
    # The highlighter's name set must stay in sync with what the console actually exposes
    namespace = build_namespace([])
    assert set(CONSOLE_NAMES) == set(namespace)
