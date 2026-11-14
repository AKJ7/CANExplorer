import math
import os

from PyQt6.QtCore import QUrl
from PyQt6.QtQml import QQmlComponent, QQmlEngine

from can_explorer.util.gui import get_res_path

QML = """
import QtQuick 2.15
import CanExplorer 1.0

DonutChart {
    width: 400
    height: 200
}
"""


def _chart(qapp, segments):
    # The Quick Controls style is fixed for the process when first loaded: the application's, as later tests expect
    os.environ.setdefault('QT_QUICK_CONTROLS_STYLE', 'CanExplorerStyle')
    engine = QQmlEngine()
    engine.addImportPath(str(get_res_path('.')))
    component = QQmlComponent(engine)
    component.setData(QML.encode(), QUrl())
    chart = component.create()
    assert chart is not None, component.errorString()
    chart.setProperty('segments', segments)
    return engine, chart


def _call(engine, chart, name, *args):
    js_chart = engine.newQObject(chart)
    return js_chart.property(name).callWithInstance(js_chart, list(args)).toVariant()


def test_tiny_and_empty_segments_stay_within_their_share(qapp):
    """A segment too small for the gap between segments must not end before it starts: the canvas would draw that
    arc the long way round, over the whole donut."""
    segments = [
        {'label': 'Standard', 'value': 9999},
        {'label': 'Extended', 'value': 1},  # 0.01 %
        {'label': 'Remote', 'value': 0},
        {'label': 'Error', 'value': 2},
    ]
    engine, chart = _chart(qapp, segments)
    arcs = _call(engine, chart, 'arcs')
    assert [arc['index'] for arc in arcs] == [0, 1, 3]  # The empty segment is not drawn
    total = sum(segment['value'] for segment in segments)
    angle = -math.pi / 2
    shares = {}
    for index, segment in enumerate(segments):
        sweep = segment['value'] / total * 2 * math.pi
        shares[index] = (angle, angle + sweep)
        angle += sweep
    for arc in arcs:
        start, end = shares[arc['index']]
        assert start <= arc['start'] < arc['end'] <= end  # Forwards, inside its own share
    assert sum(arc['end'] - arc['start'] for arc in arcs) < 2 * math.pi


def test_no_arcs_without_frames(qapp):
    engine, chart = _chart(qapp, [{'label': 'Standard', 'value': 0}])
    assert _call(engine, chart, 'arcs') == []


def test_legend_shows_small_shares_as_less_than_a_tenth(qapp):
    engine, chart = _chart(qapp, [{'label': 'Standard', 'value': 99999}, {'label': 'Extended', 'value': 1}])
    assert _call(engine, chart, 'percentText', 1) == '<0.1%'
    assert _call(engine, chart, 'percentText', 0) == '0.0%'
    assert _call(engine, chart, 'percentText', 99999) == '100.0%'
