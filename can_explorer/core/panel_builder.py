"""The visual panel builder's model: widgets placed on a panel, saved as an ordinary QML panel file.

The file it writes is a panel like a hand-written one (`property var panel`, see the panel API), so it runs in the
Simulation view unchanged and can be edited by hand afterwards. Its first line keeps the layout as JSON, so the
builder can open the file again; a file edited by hand keeps working as a panel, but the builder then opens its
layout as it was saved (the hand edits are in the QML only).

Widgets show a source — a signal ("signal:Message.Signal") or a shared variable ("variable:name") — or set a
variable, or send frames. Positions and sizes are in pixels.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MARKER = '// CANExplorer panel builder layout: '
VERSION = 1

# kind -> (description, default size, default properties)
KINDS: Dict[str, Tuple[str, Tuple[int, int], Dict[str, Any]]] = {
    'label': ('Text', (160, 32), {'text': 'Label', 'size': 14, 'bold': False}),
    'value': ('Value', (200, 56), {'label': 'Value', 'source': '', 'size': 22}),
    'gauge': ('Gauge', (200, 200), {'label': '', 'source': '', 'min': 0, 'max': 100, 'warn': 0}),
    'bar': ('Bar', (240, 48), {'label': '', 'source': '', 'min': 0, 'max': 100}),
    'lamp': ('Lamp', (160, 40), {'label': 'Lamp', 'source': '', 'threshold': 0.5, 'color': 'green'}),
    'trend': ('Trend', (320, 140), {'label': '', 'source': '', 'min': 0, 'max': 100, 'seconds': 10}),
    'switch': ('Switch', (160, 40), {'label': 'Switch', 'variable': ''}),
    'slider': ('Slider', (260, 56), {'label': 'Slider', 'variable': '', 'min': 0, 'max': 100, 'step': 1}),
    'button': (
        'Button',
        (160, 40),
        {
            'label': 'Button',
            'action': 'variable',
            'variable': '',
            'value': '1',
            'message': '',
            'signals': '',
            'id': '',
            'data': '',
            'text': '',
        },
    ),
}
DISPLAY_KINDS = ('value', 'gauge', 'bar', 'lamp', 'trend')
ACTIONS = ('variable', 'message', 'raw', 'log')
LAMP_COLORS = {'green': '#3ccf6e', 'red': '#ff5c57', 'amber': '#f5b942', 'blue': '#5ec8f2'}
MIN_SIZE, MAX_SIZE = 16, 4000


class PanelError(ValueError):
    pass


@dataclass
class Widget:
    kind: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    props: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, kind: str, x: int = 0, y: int = 0) -> 'Widget':
        if kind not in KINDS:
            raise PanelError(f'Unknown widget {kind!r}; one of {", ".join(KINDS)}')
        _, (width, height), props = KINDS[kind]
        return cls(kind, max(0, int(x)), max(0, int(y)), width, height, dict(props))


@dataclass
class PanelLayout:
    title: str = 'Panel'
    width: int = 640
    height: int = 400
    widgets: List[Widget] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({'version': VERSION, **asdict(self)}, ensure_ascii=False, separators=(',', ':'))

    @classmethod
    def from_json(cls, text: str) -> 'PanelLayout':
        try:
            data = json.loads(text)
            widgets = [Widget(**widget) for widget in data.get('widgets', [])]
            layout = cls(str(data.get('title', 'Panel')), int(data['width']), int(data['height']), widgets)
        except (ValueError, KeyError, TypeError) as e:
            raise PanelError(f'Not a panel builder layout: {e}') from e
        for widget in layout.widgets:
            if widget.kind not in KINDS:
                raise PanelError(f'Unknown widget {widget.kind!r} in the layout')
            # Properties added in later versions get their defaults
            widget.props = {**KINDS[widget.kind][2], **widget.props}
        return layout


def _number(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PanelError(f'{what} is not a number: {value!r}') from None
    if number != number or number in (float('inf'), float('-inf')):
        raise PanelError(f'{what} is not a number: {value!r}')
    return number


def _source(value: str) -> Tuple[str, str]:
    """('signal', 'Message.Signal') or ('variable', 'name') of a source."""
    kind, _, name = str(value).partition(':')
    if kind not in ('signal', 'variable') or not name.strip():
        raise PanelError(f'A source is "signal:Message.Signal" or "variable:name", not {value!r}')
    if kind == 'signal' and '.' not in name:
        raise PanelError(f'A signal is named Message.Signal, not {name!r}')
    return kind, name.strip()


def check(layout: PanelLayout) -> List[str]:
    """The problems that keep the panel from working, one text each (empty: it can be saved)."""
    problems = []
    for index, widget in enumerate(layout.widgets, 1):
        name = f'{KINDS[widget.kind][0]} {index}'
        props = widget.props
        try:
            if not (MIN_SIZE <= widget.width <= MAX_SIZE and MIN_SIZE <= widget.height <= MAX_SIZE):
                raise PanelError(f'size {widget.width}×{widget.height} is out of range')
            if widget.kind in DISPLAY_KINDS:
                if not props.get('source'):
                    raise PanelError('choose the signal or variable it shows')
                _source(props['source'])
            if widget.kind in ('gauge', 'bar', 'trend', 'slider'):
                if _number(props['min'], 'minimum') >= _number(props['max'], 'maximum'):
                    raise PanelError('the minimum must be below the maximum')
            if widget.kind == 'lamp':
                _number(props['threshold'], 'threshold')
                if props.get('color') not in LAMP_COLORS:
                    raise PanelError(f'the colour is one of {", ".join(LAMP_COLORS)}')
            if widget.kind == 'trend' and not 1 <= _number(props['seconds'], 'time window') <= 600:
                raise PanelError('the time window is 1 to 600 seconds')
            if widget.kind == 'slider' and _number(props['step'], 'step') <= 0:
                raise PanelError('the step must be above zero')
            if widget.kind in ('switch', 'slider') and not str(props.get('variable', '')).strip():
                raise PanelError('choose the variable it sets')
            if widget.kind == 'button':
                action = props.get('action')
                if action not in ACTIONS:
                    raise PanelError(f'the action is one of {", ".join(ACTIONS)}')
                if action == 'variable' and not str(props.get('variable', '')).strip():
                    raise PanelError('choose the variable it sets')
                if action == 'message':
                    if not str(props.get('message', '')).strip():
                        raise PanelError('choose the message it sends')
                    parse_signal_values(props.get('signals', ''))
                if action == 'raw':
                    if not re.fullmatch(r'(0x)?[0-9A-Fa-f]{1,8}', str(props.get('id', '')).strip()):
                        raise PanelError('the identifier is hex, e.g. 7DF')
                    if not re.fullmatch(r'[0-9A-Fa-f\s]*', str(props.get('data', ''))):
                        raise PanelError('the data is hex bytes, e.g. 02 01 0D')
        except PanelError as e:
            problems.append(f'{name}: {e}')
    return problems


def parse_signal_values(text: str) -> Dict[str, float]:
    """'Pressure=50, Mode=2' -> {'Pressure': 50.0, 'Mode': 2.0}."""
    values = {}
    for part in (part.strip() for part in str(text).split(',')):
        if not part:
            continue
        name, separator, value = part.partition('=')
        if not separator or not name.strip():
            raise PanelError(f'signal values are Name=value, separated by commas, not {part!r}')
        values[name.strip()] = _number(value.strip(), f'the value of {name.strip()}')
    return values


# --- QML ---


def _q(value: Any) -> str:
    """A QML (JavaScript) string literal."""
    return json.dumps(str(value), ensure_ascii=False)


def _watch(source: str) -> str:
    kind, name = _source(source)
    return f'root.panel.{kind}({_q(name)})'


def _geometry(widget: Widget) -> str:
    return f'x: {widget.x}; y: {widget.y}; width: {widget.width}; height: {widget.height}'


def _variable_value(text: str) -> str:
    """A variable's value from the text of a button: a number, true/false, or a string."""
    stripped = str(text).strip()
    if stripped.lower() in ('true', 'false'):
        return stripped.lower()
    try:
        return repr(float(stripped)) if any(c in stripped for c in '.eE') else str(int(stripped, 0))
    except ValueError:
        return _q(stripped)


def _widget_qml(widget: Widget, index: int) -> str:
    p, kind = widget.props, widget.kind
    geometry = _geometry(widget)
    if kind == 'label':
        bold = 'true' if p.get('bold') else 'false'
        return (
            f'Label {{ {geometry}; text: {_q(p["text"])}; font.pixelSize: {int(_number(p["size"], "size"))}; '
            f'font.bold: {bold}; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }}'
        )
    if kind == 'value':
        return (
            f'Column {{ {geometry}\n'
            f'        Label {{ text: {_q(p["label"])}; opacity: 0.7 }}\n'
            f'        Label {{ text: {_watch(p["source"])}.text; font.pixelSize: {int(_number(p["size"], "size"))}; '
            f'font.bold: true }}\n'
            f'    }}'
        )
    if kind == 'gauge':
        return (
            f'BuilderGauge {{ {geometry}; label: {_q(p["label"])}; watch: {_watch(p["source"])}; '
            f'from: {_number(p["min"], "minimum")}; to: {_number(p["max"], "maximum")}; '
            f'warn: {_number(p["warn"], "warning")} }}'
        )
    if kind == 'bar':
        return (
            f'BuilderBar {{ {geometry}; label: {_q(p["label"])}; watch: {_watch(p["source"])}; '
            f'from: {_number(p["min"], "minimum")}; to: {_number(p["max"], "maximum")} }}'
        )
    if kind == 'lamp':
        return (
            f'BuilderLamp {{ {geometry}; label: {_q(p["label"])}; watch: {_watch(p["source"])}; '
            f'threshold: {_number(p["threshold"], "threshold")}; onColor: {_q(LAMP_COLORS[p["color"]])} }}'
        )
    if kind == 'trend':
        return (
            f'BuilderTrend {{ {geometry}; label: {_q(p["label"])}; watch: {_watch(p["source"])}; '
            f'from: {_number(p["min"], "minimum")}; to: {_number(p["max"], "maximum")}; '
            f'seconds: {_number(p["seconds"], "time window")} }}'
        )
    if kind == 'switch':
        variable = _q(str(p['variable']).strip())
        return (
            f'Switch {{ {geometry}; text: {_q(p["label"])}\n'
            f'        checked: root.panel.variable({variable}).value === true\n'
            f'        onToggled: root.panel.set_variable({variable}, checked)\n'
            f'    }}'
        )
    if kind == 'slider':
        variable = _q(str(p['variable']).strip())
        return (
            f'Column {{ {geometry}\n'
            f'        Label {{ text: {_q(p["label"])} + ": " + (+slider{index}.value.toFixed(3)) }}\n'
            f'        Slider {{\n'
            f'            id: slider{index}\n'
            f'            width: parent.width\n'
            f'            from: {_number(p["min"], "minimum")}; to: {_number(p["max"], "maximum")}; '
            f'stepSize: {_number(p["step"], "step")}\n'
            f'            value: root.panel.variable({variable}).number\n'
            f'            onMoved: root.panel.set_variable({variable}, value)\n'
            f'        }}\n'
            f'    }}'
        )
    if kind == 'button':
        action = p['action']
        if action == 'variable':
            call = f'root.panel.set_variable({_q(str(p["variable"]).strip())}, {_variable_value(p["value"])})'
        elif action == 'message':
            values = json.dumps(parse_signal_values(p['signals']), ensure_ascii=False)
            call = f'root.panel.send({_q(str(p["message"]).strip())}, {values})'
        elif action == 'raw':
            call = f'root.panel.send_raw({_q(str(p["id"]).strip())}, {_q(str(p["data"]).strip())})'
        else:
            call = f'root.panel.log({_q(p["text"])})'
        return f'Button {{ {geometry}; text: {_q(p["label"])}; onClicked: {call} }}'
    raise PanelError(f'Unknown widget {kind!r}')


# Components the widgets use, written into the file only when used (so each file stands alone)
_COMPONENTS = {
    'gauge': """    component BuilderGauge: Item {
        id: gauge
        property string label
        property var watch
        property real from: 0
        property real to: 100
        property real warn: 0
        readonly property real value: watch ? watch.number : 0
        onValueChanged: canvas.requestPaint()
        Canvas {
            id: canvas
            anchors.fill: parent
            property color ink: root.palette.text
            onInkChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                const cx = width / 2, cy = height / 2, r = Math.min(width, height) / 2 - 12
                const a0 = Math.PI * 0.75, span = Math.PI * 1.5
                const fraction = Math.max(0, Math.min(1, (gauge.value - gauge.from) / (gauge.to - gauge.from)))
                ctx.lineCap = "round"
                ctx.lineWidth = 10
                ctx.strokeStyle = "#363b47"
                ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span); ctx.stroke()
                ctx.strokeStyle = gauge.warn > gauge.from && gauge.value >= gauge.warn ? "#ff5c57" : "#5ec8f2"
                ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + span * fraction); ctx.stroke()
            }
        }
        Column {
            anchors.centerIn: parent
            Label { text: gauge.watch ? gauge.watch.text : "–"; font.pixelSize: 20; font.bold: true; anchors.horizontalCenter: parent.horizontalCenter }
            Label { text: gauge.label; opacity: 0.7; anchors.horizontalCenter: parent.horizontalCenter }
        }
    }
""",
    'bar': """    component BuilderBar: Column {
        id: bar
        property string label
        property var watch
        property real from: 0
        property real to: 100
        spacing: 4
        Label { text: bar.label + (bar.label ? ": " : "") + (bar.watch ? bar.watch.text : "–") }
        ProgressBar {
            width: parent.width
            from: bar.from
            to: bar.to
            value: bar.watch ? Math.max(bar.from, Math.min(bar.to, bar.watch.number)) : bar.from
        }
    }
""",
    'lamp': """    component BuilderLamp: Row {
        id: lamp
        property string label
        property var watch
        property real threshold: 0.5
        property color onColor: "#3ccf6e"
        readonly property bool on: watch !== undefined && (watch.value === true || watch.number > threshold)
        spacing: 8
        Rectangle {
            width: Math.min(lamp.height, 24); height: width; radius: width / 2
            anchors.verticalCenter: parent.verticalCenter
            color: lamp.on ? lamp.onColor : "transparent"
            border.color: lamp.on ? lamp.onColor : root.palette.mid
            border.width: 2
        }
        Label { text: lamp.label; anchors.verticalCenter: parent.verticalCenter }
    }
""",
    'trend': """    component BuilderTrend: Item {
        id: trend
        property string label
        property var watch
        property real from: 0
        property real to: 100
        property real seconds: 10
        property var points: []
        Timer {
            interval: 100; running: true; repeat: true
            onTriggered: {
                const now = Date.now(), kept = trend.points.filter(p => now - p.t <= trend.seconds * 1000)
                if (trend.watch && trend.watch.value !== undefined)
                    kept.push({ t: now, v: trend.watch.number })
                trend.points = kept
                canvas.requestPaint()
            }
        }
        Label { id: caption; text: trend.label + (trend.label ? ": " : "") + (trend.watch ? trend.watch.text : "–") }
        Canvas {
            id: canvas
            anchors { top: caption.bottom; left: parent.left; right: parent.right; bottom: parent.bottom; topMargin: 4 }
            onPaint: {
                const ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = "#5ec8f2"
                ctx.lineWidth = 2
                ctx.beginPath()
                const now = Date.now()
                trend.points.forEach((p, i) => {
                    const x = width - (now - p.t) / (trend.seconds * 1000) * width
                    const y = height - (Math.max(trend.from, Math.min(trend.to, p.v)) - trend.from) / (trend.to - trend.from) * height
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                })
                ctx.stroke()
            }
        }
    }
""",
}


def generate_qml(layout: PanelLayout) -> str:
    """The panel file of a layout. Raises PanelError when the layout has problems (`check`)."""
    problems = check(layout)
    if problems:
        raise PanelError('; '.join(problems))
    used = {widget.kind for widget in layout.widgets}
    components = ''.join(text for kind, text in _COMPONENTS.items() if kind in used)
    widgets = '\n'.join(f'    {_widget_qml(widget, index)}' for index, widget in enumerate(layout.widgets))
    return (
        f'{MARKER}{layout.to_json()}\n'
        f'// {layout.title}: made with the panel builder of CANExplorer. It is an ordinary panel (property var panel);\n'
        f'// the builder opens it again from the layout above. Changes made here by hand are not seen by the builder.\n'
        f'import QtQuick 2.15\n'
        f'import QtQuick.Controls 2.15\n'
        f'\n'
        f'Pane {{\n'
        f'    id: root\n'
        f'    property var panel\n'
        f'    padding: 0\n'
        f'    implicitWidth: {layout.width}\n'
        f'    implicitHeight: {layout.height}\n'
        f'\n'
        f'{components}'
        f'{widgets}\n'
        f'}}\n'
    )


def read_layout(text: str) -> PanelLayout:
    """The layout kept in a panel file the builder wrote."""
    first = text.split('\n', 1)[0]
    if not first.startswith(MARKER):
        raise PanelError('This panel was not made with the panel builder (open it in a text editor instead)')
    return PanelLayout.from_json(first[len(MARKER) :])


def save(layout: PanelLayout, path: str | Path) -> Path:
    path = Path(path)
    if path.suffix.lower() != '.qml':
        path = path.with_suffix('.qml')
    path.write_text(generate_qml(layout), encoding='utf-8')
    return path


def load(path: str | Path) -> PanelLayout:
    try:
        return read_layout(Path(path).read_text(encoding='utf-8'))
    except OSError as e:
        raise PanelError(f'Cannot read {Path(path).name}: {e}') from e


def find(layout: PanelLayout, x: float, y: float) -> Optional[int]:
    """The topmost widget at a point, or None."""
    for index in range(len(layout.widgets) - 1, -1, -1):
        widget = layout.widgets[index]
        if widget.x <= x < widget.x + widget.width and widget.y <= y < widget.y + widget.height:
            return index
    return None
