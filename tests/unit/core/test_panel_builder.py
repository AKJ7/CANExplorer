import json

import pytest

from can_explorer.core import panel_builder
from can_explorer.core.panel_builder import KINDS, PanelError, PanelLayout, Widget


def full_layout() -> PanelLayout:
    """One widget of every kind, configured."""
    widgets = []
    for index, kind in enumerate(KINDS):
        widget = Widget.new(kind, 10 + index * 20, 10 + index * 30)
        widgets.append(widget)
        if kind in panel_builder.DISPLAY_KINDS:
            widget.props['source'] = 'signal:EngineData.EngineSpeed' if kind != 'lamp' else 'variable:ignition'
        if kind in ('switch', 'slider'):
            widget.props['variable'] = 'ignition' if kind == 'switch' else 'target_speed'
    button = Widget.new('button', 400, 300)
    button.props.update(action='message', message='EngineControl', signals='TargetSpeed=2000, Mode=1')
    raw = Widget.new('button', 400, 350)
    raw.props.update(action='raw', id='7DF', data='02 01 0D')
    log = Widget.new('button', 400, 400)
    log.props.update(action='log', text='Note "quoted" \\ and\nnewline')
    widgets[-1].props.update(action='variable', variable='gear', value='3')
    return PanelLayout('Engine bench', 800, 500, widgets + [button, raw, log])


def test_new_widgets_and_unknown_kinds():
    gauge = Widget.new('gauge', -5, 20)
    assert (gauge.x, gauge.y, gauge.width, gauge.height) == (0, 20, 200, 200)
    assert gauge.props == {'label': '', 'source': '', 'min': 0, 'max': 100, 'warn': 0}
    gauge.props['min'] = 50  # Each widget has its own properties
    assert Widget.new('gauge').props['min'] == 0
    with pytest.raises(PanelError, match='Unknown widget'):
        Widget.new('dial')


def test_generated_panel_round_trip(tmp_path):
    layout = full_layout()
    assert panel_builder.check(layout) == []
    path = panel_builder.save(layout, tmp_path / 'bench')  # .qml added
    assert path.name == 'bench.qml'
    text = path.read_text(encoding='utf-8')
    assert text.startswith(panel_builder.MARKER) and 'property var panel' in text
    assert panel_builder.load(path) == layout
    # An ordinary panel: the panel API, and only the components used
    assert 'root.panel.signal("EngineData.EngineSpeed")' in text
    assert 'root.panel.send("EngineControl", {"TargetSpeed": 2000.0, "Mode": 1.0})' in text
    assert 'root.panel.send_raw("7DF", "02 01 0D")' in text
    assert 'root.panel.set_variable("gear", 3)' in text
    assert 'root.panel.log("Note \\"quoted\\" \\\\ and\\nnewline")' in text  # Escaped as a JS string
    assert text.count('component Builder') == 4
    only_labels = PanelLayout('Text', 200, 100, [Widget.new('label')])
    assert 'component' not in panel_builder.generate_qml(only_labels)


def test_problems_are_reported_per_widget():
    layout = PanelLayout('Checks', 400, 300)
    value = Widget.new('value')
    gauge = Widget.new('gauge')
    gauge.props.update(source='signal:NoDot', min=10, max=5)
    lamp = Widget.new('lamp')
    lamp.props.update(source='variable:on', color='purple')
    slider = Widget.new('slider')
    slider.props.update(step=0, variable='x')
    button = Widget.new('button')
    button.props.update(action='message', message='Msg', signals='Bad')
    raw = Widget.new('button')
    raw.props.update(action='raw', id='XYZ')
    tiny = Widget.new('label')
    tiny.width = 2
    layout.widgets = [value, gauge, lamp, slider, button, raw, tiny]
    assert panel_builder.check(layout) == [
        'Value 1: choose the signal or variable it shows',
        "Gauge 2: A signal is named Message.Signal, not 'NoDot'",
        'Lamp 3: the colour is one of green, red, amber, blue',
        'Slider 4: the step must be above zero',
        "Button 5: signal values are Name=value, separated by commas, not 'Bad'",
        'Button 6: the identifier is hex, e.g. 7DF',
        'Text 7: size 2×32 is out of range',
    ]
    with pytest.raises(PanelError, match='Value 1'):
        panel_builder.generate_qml(layout)


def test_files_not_made_by_the_builder(tmp_path):
    hand = tmp_path / 'hand.qml'
    hand.write_text('import QtQuick 2.15\nItem { property var panel }\n')
    with pytest.raises(PanelError, match='not made with the panel builder'):
        panel_builder.load(hand)
    broken = tmp_path / 'broken.qml'
    broken.write_text(panel_builder.MARKER + '{"widgets": [}\n')
    with pytest.raises(PanelError, match='Not a panel builder layout'):
        panel_builder.load(broken)
    with pytest.raises(PanelError, match='Cannot read'):
        panel_builder.load(tmp_path / 'missing.qml')
    # Properties added later get their defaults when an older layout is opened
    old = {'title': 'Old', 'width': 100, 'height': 100, 'widgets': [{'kind': 'gauge', 'props': {'source': 's:a'}}]}
    layout = PanelLayout.from_json(json.dumps(old))
    assert layout.widgets[0].props['max'] == 100 and layout.widgets[0].props['source'] == 's:a'


def test_find_the_topmost_widget():
    a, b = Widget.new('label', 0, 0), Widget.new('label', 100, 10)
    layout = PanelLayout(widgets=[a, b])
    assert panel_builder.find(layout, 120, 20) == 1
    assert panel_builder.find(layout, 10, 10) == 0
    assert panel_builder.find(layout, 500, 500) is None
