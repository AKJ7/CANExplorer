"""In-app help: the user guide and reference, served from the very same Markdown as the online documentation.

The documentation lives in `docs/` (built into the website with MkDocs). This module locates those files at
runtime — bundled under `can_explorer/resources/help/docs` in a packaged build, or the repository's `docs/` when
running from source — and turns one page into Markdown that Qt's `Text.MarkdownText` renders well: MkDocs/Material
constructs Qt does not understand (admonitions, content tabs, snippets, Mermaid diagrams) are converted or given a
graceful fallback, and links and images are rewritten so navigation and pictures work inside the app.

The navigation tree (`HELP_NAV`) mirrors the `nav:` of `mkdocs.yml`; a unit test parses the yaml and fails if the
two drift, so the in-app table of contents and the website never diverge. Qt-free, so it is tested directly.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

SITE_URL = 'https://akj7.github.io/CANExplorer/'

_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # can_explorer/


class Page(NamedTuple):
    title: str
    path: str  # Relative to the docs root, e.g. 'user-guide/ethernet.md'


class Section(NamedTuple):
    title: str
    pages: Tuple[Page, ...]


# Mirrors the nav of mkdocs.yml (kept in sync by tests/unit/core/test_help.py).
HELP_NAV: Tuple[Section, ...] = (
    Section('', (Page('Home', 'index.md'),)),
    Section(
        'Getting started',
        (
            Page('Getting started', 'getting-started/index.md'),
            Page('Installation', 'getting-started/installation.md'),
            Page('Quick start', 'getting-started/quick-start.md'),
        ),
    ),
    Section(
        'User guide',
        (
            Page('User guide', 'user-guide/index.md'),
            Page('Connections', 'user-guide/connections.md'),
            Page('Trace and overview', 'user-guide/trace-and-overview.md'),
            Page('Filters', 'user-guide/filters.md'),
            Page('Bus health', 'user-guide/bus-health.md'),
            Page('Reverse engineering', 'user-guide/reverse-engineering.md'),
            Page('Recording, replay and export', 'user-guide/recording-and-replay.md'),
            Page('Comparing recordings', 'user-guide/compare-recordings.md'),
            Page('Video and GPS map', 'user-guide/video-and-map.md'),
            Page('Several buses', 'user-guide/multi-bus.md'),
            Page('Bus gateway', 'user-guide/bus-gateway.md'),
            Page('Restbus simulation', 'user-guide/restbus.md'),
            Page('Remote buses', 'user-guide/remote-buses.md'),
            Page('MCP interface', 'user-guide/mcp.md'),
            Page('MQTT', 'user-guide/mqtt.md'),
            Page('Signal databases', 'user-guide/signal-databases.md'),
            Page('Database editor', 'user-guide/database-editor.md'),
            Page('Signal plots', 'user-guide/signal-plots.md'),
            Page('Transmitting', 'user-guide/transmitting.md'),
            Page('Protocol decoding', 'user-guide/protocols.md'),
            Page('Diagnostics (UDS)', 'user-guide/diagnostics.md'),
            Page('OBD-II', 'user-guide/obd.md'),
            Page('Flashing', 'user-guide/flashing.md'),
            Page('UDS discovery', 'user-guide/uds-discovery.md'),
            Page('XCP/CCP', 'user-guide/xcp.md'),
            Page('GMLAN', 'user-guide/gmlan.md'),
            Page('J1850 / Class 2', 'user-guide/j1850.md'),
            Page('DoIP', 'user-guide/doip.md'),
            Page('SOME/IP', 'user-guide/someip.md'),
            Page('Ethernet capture', 'user-guide/ethernet.md'),
            Page('Security manager', 'user-guide/security-manager.md'),
            Page('J1939', 'user-guide/j1939.md'),
            Page('CANopen', 'user-guide/canopen.md'),
            Page('EtherCAT (CoE)', 'user-guide/ethercat.md'),
            Page('Physical I/O', 'user-guide/physical-io.md'),
            Page('LIN', 'user-guide/lin.md'),
            Page('Simulation and scripting', 'user-guide/simulation.md'),
            Page('Test automation', 'user-guide/test-automation.md'),
            Page('Workspaces', 'user-guide/workspaces.md'),
            Page('Preferences and settings', 'user-guide/preferences.md'),
            Page('Updates', 'user-guide/updates.md'),
        ),
    ),
    Section(
        'Reference',
        (
            Page('Reference', 'reference/index.md'),
            Page('Scripting API', 'reference/scripting-api.md'),
            Page('Test API', 'reference/test-api.md'),
            Page('QML panel API', 'reference/panel-api.md'),
            Page('Supported interfaces', 'reference/interfaces.md'),
            Page('Command line', 'reference/command-line.md'),
            Page('File formats', 'reference/file-formats.md'),
            Page('Keyboard shortcuts', 'reference/keyboard-shortcuts.md'),
            Page('Links', 'reference/links.md'),
        ),
    ),
    Section(
        'Development',
        (
            Page('Development', 'development/index.md'),
            Page('Architecture', 'development/architecture.md'),
            Page('Building and testing', 'development/building.md'),
            Page('Packaging', 'development/packaging.md'),
            Page('Translations', 'development/translations.md'),
            Page('Contributing', 'development/contributing.md'),
        ),
    ),
    Section('', (Page('Changelog', 'changelog.md'),)),
)

# The help page shown for each tool and connection view (its `tool` string or `page` key); opened by the "?" button.
TOOL_HELP: Dict[str, str] = {
    '': 'user-guide/connections.md',
    'database-editor': 'user-guide/database-editor.md',
    'multi-bus': 'user-guide/multi-bus.md',
    'compare': 'user-guide/compare-recordings.md',
    'ethercat': 'user-guide/ethercat.md',
    'physical-io': 'user-guide/physical-io.md',
    'doip': 'user-guide/doip.md',
    'someip': 'user-guide/someip.md',
    'ethernet': 'user-guide/ethernet.md',
    'gateway': 'user-guide/bus-gateway.md',
    'restbus': 'user-guide/restbus.md',
    'trace': 'user-guide/trace-and-overview.md',
    'overview': 'user-guide/trace-and-overview.md',
    'plots': 'user-guide/signal-plots.md',
    'health': 'user-guide/bus-health.md',
    'security': 'user-guide/security-manager.md',
    'reverse': 'user-guide/reverse-engineering.md',
    'transmit': 'user-guide/transmitting.md',
    'diagnostics': 'user-guide/diagnostics.md',
    'obd': 'user-guide/obd.md',
    'j1939': 'user-guide/j1939.md',
    'canopen': 'user-guide/canopen.md',
    'lin': 'user-guide/lin.md',
    'simulation': 'user-guide/simulation.md',
    'media': 'user-guide/video-and-map.md',
}

DEFAULT_PAGE = 'user-guide/index.md'

# The sections shown in the in-app table of contents. HELP_NAV mirrors the whole website (and the getting-started,
# reference and development pages stay reachable through in-page links and the search box), but the help panel lists
# only the user guide — the part people browse while using the app.
IN_APP_SECTIONS: Tuple[str, ...] = ('User guide',)

_PAGES: Dict[str, Page] = {page.path: page for section in HELP_NAV for page in section.pages}


def nav_sections() -> Tuple[Section, ...]:
    """The sections to list in the in-app table of contents (the user guide only); see IN_APP_SECTIONS."""
    return tuple(section for section in HELP_NAV if section.title in IN_APP_SECTIONS)


def docs_dir() -> Path:
    """The documentation root: the bundled copy in a packaged build, else the repository's docs/ when from source."""
    bundled = _PACKAGE_DIR / 'resources' / 'help' / 'docs'
    return bundled if bundled.is_dir() else _PACKAGE_DIR.parent / 'docs'


def pages() -> List[Page]:
    return list(_PAGES.values())


def title_of(path: str) -> str:
    page = _PAGES.get(path)
    return page.title if page else path


def online_url(path: str) -> str:
    """The address of a page on the documentation website (where diagrams, search and the full styling live)."""
    slug = path[: -len('index.md')] if path.endswith('index.md') else path[: -len('.md')] + '/'
    return SITE_URL + slug


def exists(path: str) -> bool:
    return (docs_dir() / path).is_file() if path in _PAGES else False


def load_page(path: str) -> str:
    """The rendered Markdown of a help page (preprocessed for Qt), or a short not-found note."""
    if path not in _PAGES:
        return f'# Not found\n\nThe help page `{path}` does not exist.'
    file = docs_dir() / path
    try:
        text = file.read_text(encoding='utf-8')
    except OSError as error:
        return f'# {title_of(path)}\n\nCould not read this page ({error}). [Open it online]({online_url(path)}).'
    return _preprocess(text, path)


# A standalone image: an image that is the whole line (no indentation, so images inside lists stay in their text).
_IMAGE_LINE = re.compile(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$')


def page_blocks(path: str) -> List[Dict[str, str]]:
    """A help page split into renderable blocks, in order: runs of Markdown text and standalone images.

    Qt's Markdown renderer draws images at their native pixel size, so large screenshots would overflow the help
    panel. Returning the standalone images as their own blocks lets the UI render them with an Image that scales to
    fit the panel. Inline images inside a paragraph or list stay in the surrounding text block.
    """
    blocks: List[Dict[str, str]] = []
    buffer: List[str] = []

    def flush() -> None:
        text = '\n'.join(buffer).strip()
        if text:
            blocks.append({'type': 'text', 'text': text})
        buffer.clear()

    for line in load_page(path).splitlines():
        image = _IMAGE_LINE.match(line)
        if image:
            flush()
            blocks.append({'type': 'image', 'source': image.group(2), 'alt': image.group(1)})
        else:
            buffer.append(line)
    flush()
    return blocks


# Markdown preprocessing -------------------------------------------------------------------------------------------

_ADMONITION = re.compile(r'^(?:!!!|\?\?\?\+?|\?\?\?)\s+(\w+)(?:\s+"([^"]*)")?\s*$')
_TAB = re.compile(r'^(\s*)===\s+"([^"]*)"\s*$')
_SNIPPET = re.compile(r'^\s*(?:--8<--|-{2}8<-{2})')
_IMAGE = re.compile(r'(!\[[^\]]*\]\()([^)]+)(\))')
_LINK = re.compile(r'(?<!!)(\[[^\]]+\]\()([^)]+)(\))')
_FENCE = re.compile(r'^(\s*)(```+|~~~+)(.*)$')


def _preprocess(text: str, path: str) -> str:
    lines = text.splitlines()
    out: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence and fence.group(3).strip().lower() == 'mermaid':
            index = _skip_fence(lines, index, fence.group(2))
            out.append(f'> **Diagram** — [view it in the online docs]({online_url(path)})')
            out.append('')
            continue
        if fence:  # A normal code fence: copy it through untouched so its contents are not transformed
            end = _skip_fence(lines, index, fence.group(2))
            out.extend(lines[index:end])
            index = end
            continue
        admonition = _ADMONITION.match(line)
        if admonition:
            index = _emit_admonition(lines, index, admonition, out, path)
            continue
        tab = _TAB.match(line)
        if tab:
            index = _emit_tab(lines, index, tab, out, path)
            continue
        if _SNIPPET.match(line):  # MkDocs file include: not resolvable here, drop the marker
            index += 1
            continue
        out.append(_rewrite_inline(line, path))
        index += 1
    return '\n'.join(out)


def _skip_fence(lines: List[str], index: int, marker: str) -> int:
    """The index just past a code fence that opens at `index` with `marker` (``` or ~~~)."""
    close = marker[0]
    index += 1
    while index < len(lines) and not lines[index].strip().startswith(close * 3):
        index += 1
    return min(index + 1, len(lines))


def _dedent_block(lines: List[str], start: int) -> Tuple[List[str], int]:
    """The indented body of an admonition/tab starting at `start`: its lines de-indented, and the index after it."""
    body: List[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.strip() == '':
            body.append('')
            index += 1
        elif line.startswith('    '):
            body.append(line[4:])
            index += 1
        else:
            break
    while body and body[-1] == '':  # Trailing blanks belong after the block, not inside it
        body.pop()
    return body, index


def _emit_admonition(lines: List[str], index: int, match: re.Match, out: List[str], path: str) -> int:
    kind, custom = match.group(1), match.group(2)
    title = custom if custom is not None else kind.capitalize()
    body, after = _dedent_block(lines, index + 1)
    out.append(f'> **{title}**')
    if body:
        out.append('>')
    for line in body:
        out.append('> ' + _rewrite_inline(line, path) if line else '>')
    out.append('')
    return after


def _emit_tab(lines: List[str], index: int, match: re.Match, out: List[str], path: str) -> int:
    out.append(f'**{match.group(2)}**')
    out.append('')
    body, after = _dedent_block(lines, index + 1)
    out.extend(_rewrite_inline(line, path) for line in body)
    out.append('')
    return after


def _rewrite_inline(line: str, path: str) -> str:
    base = str(Path(path).parent)
    line = _IMAGE.sub(lambda m: m.group(1) + _resolve_image(m.group(2), base) + m.group(3), line)
    line = _LINK.sub(lambda m: m.group(1) + _resolve_link(m.group(2), base) + m.group(3), line)
    return line


def _resolve_image(target: str, base: str) -> str:
    if target.startswith(('http://', 'https://', 'file://', 'data:')):
        return target
    absolute = (docs_dir() / base / target).resolve()
    return absolute.as_uri()


def _resolve_link(target: str, base: str) -> str:
    if target.startswith(('http://', 'https://', 'mailto:', 'help:', '#')):
        return target
    anchor = ''
    if '#' in target:
        target, anchor = target.split('#', 1)
    if not target:  # A bare in-page anchor
        return '#' + anchor
    normalised = str(Path(base) / target).replace('\\', '/')
    parts: List[str] = []
    for part in normalised.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
        else:
            parts.append(part)
    resolved = '/'.join(parts)
    return 'help:' + resolved if resolved in _PAGES else online_url(resolved) if resolved.endswith('.md') else target


@lru_cache(maxsize=1)
def _search_corpus() -> Tuple[Tuple[str, str, str], ...]:
    """(path, title, lowercased plain text) for every page, read once."""
    corpus = []
    for page in _PAGES.values():
        try:
            text = (docs_dir() / page.path).read_text(encoding='utf-8')
        except OSError:
            text = ''
        corpus.append((page.path, page.title, text.lower()))
    return tuple(corpus)


def search(query: str, limit: int = 30) -> List[Dict[str, str]]:
    """Pages whose title or body contains `query`, title matches first, each with a short context snippet."""
    needle = query.strip().lower()
    if not needle:
        return []
    results = []
    for path, title, text in _search_corpus():
        in_title = needle in title.lower()
        position = text.find(needle)
        if not in_title and position < 0:
            continue
        results.append(
            {
                'path': path,
                'title': title,
                'snippet': _snippet(text, position) if position >= 0 else '',
                'rank': 0 if in_title else 1,
            }
        )
    results.sort(key=lambda item: (item['rank'], item['title']))
    for item in results:
        del item['rank']
    return results[:limit]


def _snippet(text: str, position: int, width: int = 60) -> str:
    start = max(0, position - width // 2)
    end = min(len(text), position + width)
    fragment = text[start:end].replace('\n', ' ').strip()
    return ('…' if start > 0 else '') + fragment + ('…' if end < len(text) else '')


def reset_cache() -> None:
    """Forget the cached search corpus (after the docs location changes, e.g. in tests)."""
    _search_corpus.cache_clear()


def resolve_tool(key: str) -> str:
    """The help page for a tool/view key, falling back to the user guide index."""
    return TOOL_HELP.get(key) or (key if key in _PAGES else 'user-guide/index.md')


def default_path() -> str:
    return DEFAULT_PAGE
