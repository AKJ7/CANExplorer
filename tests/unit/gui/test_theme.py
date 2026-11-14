"""Consistency and accessibility checks of the colour themes in Theme.qml (parsed, no Qt needed)."""

import itertools
import re
from pathlib import Path

import pytest

from can_explorer.gui.app_state import SECRET_THEMES, SYSTEM_THEME, THEMES

THEME_QML = Path(__file__).parents[3] / 'can_explorer' / 'gui' / 'qt' / 'CanExplorer' / 'Theme.qml'
ROLES = ('background', 'surface', 'card', 'cardAlt', 'border', 'text', 'muted')
STATUS = ('accent', 'success', 'warning', 'danger')
PANELS = ('background', 'surface', 'card', 'cardAlt')
BLACK = '#000000'  # The dark text colour of Theme.onColor


def _palettes() -> dict:
    qml = THEME_QML.read_text(encoding='utf-8')
    block = qml[qml.index('readonly property var palettes') : qml.index('readonly property var current')]
    palettes = {}
    for name, body in re.findall(r'"([^"]+)":\s*\{(.*?)\n\s*\}', block, re.S):
        colours = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', body))
        chart = re.search(r'chart:\s*\[(.*?)\]', body, re.S)
        colours['chart'] = re.findall(r'"(#[0-9a-fA-F]{6})"', chart.group(1)) if chart else []
        palettes[name] = colours
    return palettes


PALETTES = _palettes()


def _luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _lab(colour: str) -> tuple:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(v):
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _difference(a: str, b: str) -> float:
    """CIE76 colour difference (Delta E): about 2 is just noticeable, 15+ reads as a different colour at a glance."""
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b), strict=True)) ** 0.5


def _on_colour(background: str) -> str:
    """Mirror of Theme.onColor: white, or black when that contrasts more."""
    return '#ffffff' if _contrast('#ffffff', background) >= _contrast(BLACK, background) else BLACK


def test_palettes_match_the_theme_names_of_app_state():
    assert set(PALETTES) == set(THEMES) | set(SECRET_THEMES)
    assert len(PALETTES) == len(THEMES) + len(SECRET_THEMES)  # No palette defined twice
    # 'System' is resolved to Light or Dark in Theme.qml, never a palette of its own
    assert SYSTEM_THEME not in PALETTES and {'Light', 'Dark'} <= set(PALETTES)


def test_theme_list_is_alphabetical():
    assert list(THEMES) == sorted(THEMES, key=str.casefold)
    assert not set(THEMES) & set(SECRET_THEMES)


@pytest.mark.parametrize('name', sorted(PALETTES))
def test_palette_has_every_role(name):
    palette = PALETTES[name]
    assert set(ROLES + STATUS) <= set(palette)
    # Views index chart[0]..chart[5] directly; the colours of the series must differ
    assert len(palette['chart']) >= 6
    assert len(set(c.lower() for c in palette['chart'])) == len(palette['chart'])


@pytest.mark.parametrize('name', sorted(PALETTES))
def test_palette_meets_wcag_contrast(name):
    palette = PALETTES[name]
    failures = []
    for role, minimum in [('text', 4.5), ('muted', 4.5)] + [(role, 3.0) for role in STATUS]:
        for panel in PANELS:
            ratio = _contrast(palette[role], palette[panel])
            if ratio < minimum:
                failures.append(f'{role} on {panel}: {ratio:.2f} < {minimum}')
    for colour in palette['chart']:
        for panel in ('background', 'card', 'cardAlt'):
            ratio = _contrast(colour, palette[panel])
            if ratio < 3.0:
                failures.append(f'chart {colour} on {panel}: {ratio:.2f} < 3.0')
    assert failures == []


@pytest.mark.parametrize('name', sorted(PALETTES))
def test_chart_and_status_colours_are_distinguishable(name):
    """Overlaid series and OK/warning/error must not look alike; differing in lightness alone is enough, so the
    monochrome Night Vision theme passes when its reds step clearly in brightness."""
    palette = PALETTES[name]
    close = [
        f'chart {a} ~ {b}: {_difference(a, b):.1f}'
        for a, b in itertools.combinations(palette['chart'], 2)
        if _difference(a, b) < 15
    ]
    close += [
        f'{a} ~ {b}: {_difference(palette[a], palette[b]):.1f}'
        for a, b in itertools.combinations(('success', 'warning', 'danger'), 2)
        if _difference(palette[a], palette[b]) < 20
    ]
    assert close == []


@pytest.mark.parametrize('name', sorted(PALETTES))
def test_text_on_accent_and_danger_is_legible(name):
    for role in ('accent', 'danger'):
        background = PALETTES[name][role]
        assert _contrast(_on_colour(background), background) >= 4.5, f'{role} {background}'


def test_contrast_helpers():
    assert _contrast('#ffffff', '#000000') == pytest.approx(21.0)
    assert _contrast('#777777', '#777777') == pytest.approx(1.0)
    assert _on_colour('#000000') == '#ffffff' and _on_colour('#ffffff') == BLACK
    assert _difference('#ff0000', '#ff0000') == 0 and _difference('#000000', '#ffffff') == pytest.approx(100, abs=0.1)
