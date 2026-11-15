"""FlexRay cluster descriptions — FIBEX and AUTOSAR ARXML — for naming FlexRay frames and decoding their signals.

A frame of the cluster is sent in a slot, on channel A, B or both, in the cycles `base_cycle + n × repetition`
(e.g. base 1, repetition 4: cycles 1, 5, 9, …). A received frame is matched to the definition of its slot, channel
and cycle. A frame carries one or more PDUs, each at a byte offset of the payload and optionally with an update bit
(the sender sets it when it refreshed the PDU in this frame; a PDU whose bit is clear is not decoded, its values are
stale). The PDUs' signals are decoded with cantools, which handles byte order, scaling, value texts and multiplexing.

ARXML (AUTOSAR System Template R23-11, Fibex4Flexray, free from autosar.org): FLEXRAY-CLUSTER ›
FLEXRAY-CLUSTER-VARIANTS › FLEXRAY-CLUSTER-CONDITIONAL with the cluster parameters (Table 3.29: CYCLE and
MACROTICK-DURATION in seconds, MACRO-PER-CYCLE, NUMBER-OF-STATIC-SLOTS, STATIC-SLOT-DURATION in macroticks,
PAYLOAD-LENGTH-STATIC in 16-bit words, NUMBER-OF-MINISLOTS, MINISLOT-DURATION, NETWORK-IDLE-TIME, BAUDRATE, …) and
FLEXRAY-PHYSICAL-CHANNELs (CHANNEL-NAME CHANNEL-A or CHANNEL-B) › FLEXRAY-FRAME-TRIGGERING with
FLEXRAY-ABSOLUTELY-SCHEDULED-TIMINGs (SLOT-ID, COMMUNICATION-CYCLE as CYCLE-COUNTER or CYCLE-REPETITION with
BASE-CYCLE). A FLEXRAY-FRAME's PDU-TO-FRAME-MAPPINGs place its PDUs: START-POSITION (a bit position, byte aligned:
the PDU starts at byte START-POSITION // 8 for either PACKING-BYTE-ORDER) and UPDATE-INDICATION-BIT-POSITION (the
update bit, a bit of the frame counted from bit 0 = least significant bit of byte 0). The PDUs and their signals
are read by cantools' ARXML loader, as for CAN, so multiplexed, secured and container PDUs work the same.
ECU-INSTANCEs' FLEXRAY-COMMUNICATION-CONTROLLERs (Table 3.30: KEY-SLOT-ID, MICRO-PER-CYCLE, …) are kept by ECU,
for configuring a node that joins the cluster.

FIBEX (ASAM MCD-2 NET; its schema is for ASAM members only, so the names below are those of public FIBEX readers
and files — canmatrix, FibexConverter, NI-XNET's FIBEX files): CLUSTER (PROTOCOL FlexRay) › CHANNEL-REFS, and
flexray:MACRO-PER-CYCLE, MACROTICK and CYCLE (read in microseconds, as NI-XNET's files use them), STATIC-SLOT and
MINISLOT (macroticks), NUMBER-OF-STATIC-SLOTS, NUMBER-OF-MINISLOTS, PAYLOAD-LENGTH-STATIC (words), N-I-T, and
fx:SPEED (bit/s). CHANNEL › FRAME-TRIGGERINGS › FRAME-TRIGGERING with TIMINGS › ABSOLUTELY-SCHEDULED-TIMING
(SLOT-ID, and CYCLE-COUNTER or BASE-CYCLE with CYCLE-REPETITION) and FRAME-REF; FRAME with BYTE-LENGTH and
SIGNAL-INSTANCES, or PDU-INSTANCES (PDU-REF, BIT-POSITION, PDU-UPDATE-BIT-POSITION); PDU with SIGNAL-INSTANCES or a
MULTIPLEXER (SWITCH with BIT-POSITION, IS-HIGH-LOW-BYTE-ORDER, BIT-LENGTH; DYNAMIC-PART › SWITCHED-PDU-INSTANCES ›
SWITCHED-PDU-INSTANCE with PDU-REF and SWITCH-CODE; STATIC-PART › STATIC-PDU-INSTANCE). A switched PDU's signal
positions are read relative to the multiplexed PDU, as canmatrix writes them. SIGNAL › CODING-REF; CODING with
CODED-TYPE (BIT-LENGTH, BASE-DATA-TYPE) and LINEAR or TEXTTABLE COMPU-METHODs. A signal instance's BIT-POSITION is
read as canmatrix reads FIBEX: for IS-HIGH-LOW-BYTE-ORDER (big endian) signals the most significant bit in LSB0
numbering, which is cantools' (and DBC's) start bit; check big-endian signals against a known value. The channel
letter comes from FLEXRAY-CHANNEL-NAME, else the channel's name, else its order.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from xml.etree import ElementTree

from can_explorer.core.flexray import FlexRayError, FlexRayFrame

CYCLE_REPETITIONS = (1, 2, 4, 5, 8, 10, 16, 20, 32, 40, 50, 64)


@dataclass
class FlexRayPdu:
    """A PDU in a frame: `offset` bytes into the payload, `length` bytes; `update_bit` is a bit of the frame
    (byte × 8 + bit, bit 0 the least significant) or None when the PDU has no update bit."""

    name: str
    offset: int
    length: int
    message: Any = None  # A cantools Message for the PDU's bytes (None: no signals)
    update_bit: Optional[int] = None

    def updated(self, payload: bytes) -> bool:
        if self.update_bit is None:
            return True
        byte, bit = divmod(self.update_bit, 8)
        return byte < len(payload) and bool(payload[byte] >> bit & 1)

    @property
    def signal_names(self) -> List[str]:
        if self.message is None:
            return []
        names = [signal.name for signal in self.message.signals]
        for contained in getattr(self.message, 'contained_messages', None) or []:
            names += [f'{contained.name}.{signal.name}' for signal in contained.signals]
        return names

    def decode(self, payload: bytes) -> Dict[str, Any]:
        if self.message is None:
            return {}
        data = bytes(payload[self.offset : self.offset + self.length]).ljust(self.message.length, b'\0')
        if getattr(self.message, 'contained_messages', None):
            values: Dict[str, Any] = {}
            try:
                decoded = self.message.decode(data, decode_choices=True, scaling=True, decode_containers=True)
            except Exception:  # noqa: BLE001 - a container that does not fit: shown undecoded
                return {}
            for contained, contained_values in decoded:
                if isinstance(contained_values, dict):
                    name = getattr(contained, 'name', str(contained))
                    values.update({f'{name}.{key}': value for key, value in contained_values.items()})
            return values
        return dict(self.message.decode(data, decode_choices=True, scaling=True, allow_truncated=True))


@dataclass
class FlexRayFrameDefinition:
    name: str
    slot: int
    base_cycle: int
    repetition: int
    channels: FrozenSet[str]
    length: int
    pdus: List[FlexRayPdu] = field(default_factory=list)
    senders: List[str] = field(default_factory=list)

    def matches(self, frame: FlexRayFrame) -> bool:
        return frame.slot == self.slot and frame.channel in self.channels and self.in_cycle(frame.cycle)

    def in_cycle(self, cycle: int) -> bool:
        return cycle % self.repetition == self.base_cycle % self.repetition

    @property
    def cycles(self) -> str:
        return 'every cycle' if self.repetition == 1 else f'from {self.base_cycle}, every {self.repetition}'

    @property
    def message(self) -> Any:
        """The cantools Message of a frame with one PDU at its start (None otherwise)."""
        if len(self.pdus) == 1 and self.pdus[0].offset == 0:
            return self.pdus[0].message
        return None

    @property
    def signal_names(self) -> List[str]:
        names: List[str] = []
        for pdu in self.pdus:
            names += pdu.signal_names
        return names


@dataclass
class FlexRayClusterParameters:
    """The cluster-wide timing of a FlexRay cluster, as far as the description gives it (None: not given)."""

    baudrate: Optional[int] = None  # bit/s
    cycle: Optional[float] = None  # seconds
    macrotick: Optional[float] = None  # seconds
    macro_per_cycle: Optional[int] = None
    static_slots: Optional[int] = None
    static_slot_duration: Optional[int] = None  # macroticks
    payload_length_static: Optional[int] = None  # 16-bit words
    minislots: Optional[int] = None
    minislot_duration: Optional[int] = None  # macroticks
    network_idle_time: Optional[int] = None  # macroticks
    symbol_window: Optional[int] = None  # macroticks
    values: Dict[str, str] = field(default_factory=dict)  # Every cluster parameter, as written

    @property
    def cycle_time(self) -> Optional[float]:
        if self.cycle:
            return self.cycle
        if self.macro_per_cycle and self.macrotick:
            return self.macro_per_cycle * self.macrotick
        return None

    @property
    def known(self) -> bool:
        return any(value is not None for value in (self.cycle_time, self.static_slots, self.static_slot_duration))

    def is_dynamic(self, slot: int) -> Optional[bool]:
        """Whether a slot is in the dynamic segment (None: unknown)."""
        return None if self.static_slots is None else slot > self.static_slots

    def static_slot_offset(self, slot: int) -> Optional[float]:
        """When a static slot starts, in seconds from the start of the cycle (None: unknown or not static)."""
        if self.is_dynamic(slot) or not self.static_slot_duration or not self.macrotick:
            return None
        return (slot - 1) * self.static_slot_duration * self.macrotick

    @property
    def static_slot_time(self) -> Optional[float]:
        if not self.static_slot_duration or not self.macrotick:
            return None
        return self.static_slot_duration * self.macrotick

    def rows(self) -> List[Tuple[str, str]]:
        """(label, value) pairs for showing the parameters."""

        def us(seconds):
            return f'{seconds * 1e6:g} µs'

        rows = []
        for label, value in (
            ('Bit rate', f'{self.baudrate / 1e6:g} Mbit/s' if self.baudrate else None),
            ('Cycle', us(self.cycle_time) if self.cycle_time else None),
            ('Macrotick', us(self.macrotick) if self.macrotick else None),
            ('Macroticks per cycle', self.macro_per_cycle),
            ('Static slots', self.static_slots),
            ('Static slot', f'{self.static_slot_duration} MT' if self.static_slot_duration is not None else None),
            ('Static payload', f'{self.payload_length_static * 2} bytes' if self.payload_length_static else None),
            ('Minislots', self.minislots),
            ('Minislot', f'{self.minislot_duration} MT' if self.minislot_duration is not None else None),
            ('Network idle time', f'{self.network_idle_time} MT' if self.network_idle_time is not None else None),
        ):
            if value is not None:
                rows.append((label, str(value)))
        return rows


class FlexRayDatabase:
    def __init__(
        self,
        path: Path,
        definitions: List[FlexRayFrameDefinition],
        cluster: str = '',
        parameters: Optional[FlexRayClusterParameters] = None,
        clusters: Optional[List[str]] = None,
        controllers: Optional[Dict[str, Dict[str, str]]] = None,
    ):
        self.path = path
        self.cluster = cluster
        self.clusters = clusters or ([cluster] if cluster else [])  # Every FlexRay cluster of the file
        self.parameters = parameters or FlexRayClusterParameters()
        self.controllers = controllers or {}  # ECU name → its FlexRay controller's parameters
        self.definitions = definitions
        self._by_slot: Dict[int, List[FlexRayFrameDefinition]] = {}
        for definition in definitions:
            self._by_slot.setdefault(definition.slot, []).append(definition)

    def definitions_for_slot(self, slot: int) -> List[FlexRayFrameDefinition]:
        return list(self._by_slot.get(slot, ()))

    def definition(self, frame: FlexRayFrame) -> Optional[FlexRayFrameDefinition]:
        for definition in self._by_slot.get(frame.slot, ()):
            if definition.matches(frame):
                return definition
        return None

    def decode_pdus(
        self, frame: FlexRayFrame, numeric: bool = False
    ) -> Tuple[Optional[FlexRayFrameDefinition], List[Dict[str, Any]]]:
        """The frame's definition and, per PDU: {'pdu', 'updated', 'values'} (values {} when not updated). Value
        texts are given as text, or by their number when `numeric`."""
        definition = self.definition(frame)
        if definition is None or frame.null_frame:
            return definition, []
        result = []
        for pdu in definition.pdus:
            updated = pdu.updated(frame.payload)
            values: Dict[str, Any] = {}
            if updated:
                try:
                    values = {name: _value(value, numeric) for name, value in pdu.decode(frame.payload).items()}
                except Exception:  # noqa: BLE001 - a payload that does not fit the PDU: shown undecoded
                    values = {}
            result.append({'pdu': pdu, 'updated': updated, 'values': values})
        return definition, result

    def decode(
        self, frame: FlexRayFrame, numeric: bool = False
    ) -> Tuple[Optional[FlexRayFrameDefinition], Dict[str, Any]]:
        """The frame's definition and the values of its updated PDUs' signals ({} for null frames). A name two
        PDUs share is given as "PDU.signal"."""
        definition, pdus = self.decode_pdus(frame, numeric)
        values: Dict[str, Any] = {}
        for entry in pdus:
            for name, value in entry['values'].items():
                values[f'{entry["pdu"].name}.{name}' if name in values else name] = value
        return definition, values

    def describe(self, frame: FlexRayFrame) -> str:
        definition, pdus = self.decode_pdus(frame)
        if definition is None:
            return ''
        if frame.null_frame:
            return f'{definition.name} (null frame)'
        units = {}
        for pdu in definition.pdus:
            if pdu.message is not None:
                units.update({signal.name: signal.unit for signal in pdu.message.signals})
        parts = []
        for entry in pdus:
            if not entry['updated']:
                parts.append(f'{entry["pdu"].name} not updated')
                continue
            parts += [
                f'{name}={_format(value)}{" " + units[name] if units.get(name) else ""}'
                for name, value in entry['values'].items()
            ]
        return f'{definition.name}: {", ".join(parts)}' if parts else definition.name


def _value(value: Any, numeric: bool) -> Any:
    if isinstance(value, (int, float)):
        return value
    if numeric and isinstance(getattr(value, 'value', None), (int, float)):
        return value.value  # A cantools NamedSignalValue
    return str(value)


def _format(value: Any) -> str:
    if isinstance(value, float):
        return f'{value:.6g}'
    return str(value)


def _repetition(text: str) -> int:
    match = re.search(r'(\d+)\s*$', text or '')
    value = int(match.group(1)) if match else 1
    if value not in CYCLE_REPETITIONS:
        raise FlexRayError(f'Not a FlexRay cycle repetition: {text!r}')
    return value


def _number(text: Optional[str]) -> Optional[float]:
    try:
        return float(text) if text not in (None, '') else None
    except ValueError:
        return None


def _integer(text: Optional[str]) -> Optional[int]:
    value = _number(text)
    return int(value) if value is not None else None


def _parse(path: Path):
    try:
        return ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as e:
        raise FlexRayError(f'Cannot read {path.name}: {e}') from e


def cluster_names(path: str | Path) -> List[str]:
    """The FlexRay clusters a FIBEX or ARXML file describes."""
    path = Path(path)
    root = _parse(path)
    if root.tag.endswith('}AUTOSAR'):
        return [
            next((child.text or '' for child in element if _local(child) == 'SHORT-NAME'), '')
            for element in root.iter()
            if _local(element) == 'FLEXRAY-CLUSTER'
        ]
    return [
        _child_text(cluster, 'SHORT-NAME')
        for cluster in _descendants(root, 'CLUSTER')
        if 'flexray' in _child_text(cluster, 'PROTOCOL').lower()
    ]


def load_database(path: str | Path, cluster: Optional[str] = None) -> FlexRayDatabase:
    """The FlexRay cluster `cluster` of a FIBEX or ARXML file (by default its first FlexRay cluster)."""
    path = Path(path)
    root = _parse(path)
    try:
        if root.tag.endswith('}AUTOSAR'):
            return _load_arxml(path, root, cluster)
        if root.tag.endswith('FIBEX'):
            return _load_fibex(path, root, cluster)
    except FlexRayError:
        raise
    except (ValueError, KeyError, AttributeError, TypeError, AssertionError) as e:
        raise FlexRayError(f'Cannot read the FlexRay cluster of {path.name}: {e}') from e
    raise FlexRayError(f'{path.name} is neither FIBEX nor AUTOSAR ARXML')


# --- FIBEX ---


def _local(element) -> str:
    return element.tag.rsplit('}', 1)[-1]


def _children(element, name: str):
    return [child for child in element if _local(child) == name]


def _child(element, name: str):
    return next(iter(_children(element, name)), None)


def _descendants(element, name: str):
    return [child for child in element.iter() if _local(child) == name]


def _child_text(element, name: str, default: str = '') -> str:
    for child in element.iter():
        if _local(child) == name and child is not element:
            return (child.text or '').strip()
    return default


def _reference(element, name: str) -> str:
    for child in element.iter():
        if _local(child) == name and child is not element:
            return child.get('ID-REF', '')
    return ''


def _fibex_coding(coding) -> Dict[str, Any]:
    from cantools.database.conversion import BaseConversion

    coded = next(iter(_descendants(coding, 'CODED-TYPE')), None)
    base_type = ''
    if coded is not None:
        base_type = next((value for key, value in coded.attrib.items() if key.endswith('BASE-DATA-TYPE')), '')
    length = int(_child_text(coding, 'BIT-LENGTH', '0') or 0)
    scale, offset, choices = 1.0, 0.0, None
    unit = ''
    for method in _descendants(coding, 'COMPU-METHOD'):
        category = _child_text(method, 'CATEGORY')
        if category == 'LINEAR':
            numerator = [float(v.text) for v in _descendants(next(iter(_descendants(method, 'COMPU-NUMERATOR'))), 'V')]
            denominators = _descendants(method, 'COMPU-DENOMINATOR')
            divisor = float(_descendants(denominators[0], 'V')[0].text) if denominators else 1.0
            if len(numerator) >= 2:
                offset, scale = numerator[0] / divisor, numerator[1] / divisor
        elif category == 'TEXTTABLE':
            choices = {}
            for scale_element in _descendants(method, 'COMPU-SCALE'):
                lower = _child_text(scale_element, 'LOWER-LIMIT')
                text = _child_text(scale_element, 'VT')
                if lower:
                    choices[int(float(lower))] = text
        unit = unit or _child_text(method, 'DISPLAY-NAME')
    is_float = base_type.startswith('A_FLOAT')
    conversion = BaseConversion.factory(scale=scale, offset=offset, choices=choices or None, is_float=is_float)
    signed = base_type.startswith('A_INT')
    return {'length': length, 'signed': signed, 'conversion': conversion, 'unit': unit or None}


class _Fibex:
    """The elements of a FIBEX file a FlexRay cluster needs, by ID."""

    def __init__(self, root):
        self.ids = {element.get('ID'): element for element in root.iter() if element.get('ID')}
        self.codings = {
            coding.get('ID'): _fibex_coding(coding) for coding in _descendants(root, 'CODING') if coding.get('ID')
        }
        self.signals = {
            signal.get('ID'): (_child_text(signal, 'SHORT-NAME'), _reference(signal, 'CODING-REF'))
            for signal in _descendants(root, 'SIGNAL')
            if signal.get('ID')
        }

    def signal_instances(self, owner, **extra) -> list:
        """The signals of the SIGNAL-INSTANCES directly under `owner` (a FRAME or a PDU)."""
        from cantools.database.can import Signal

        result = []
        for instances in _children(owner, 'SIGNAL-INSTANCES'):
            for instance in _children(instances, 'SIGNAL-INSTANCE'):
                signal = self.signals.get(_reference(instance, 'SIGNAL-REF'))
                if signal is None:
                    continue
                name, coding_id = signal
                coding = self.codings.get(coding_id)
                if coding is None or not coding['length']:
                    continue
                big_endian = _child_text(instance, 'IS-HIGH-LOW-BYTE-ORDER', 'false').lower() == 'true'
                result.append(
                    Signal(
                        name=name,
                        start=int(_child_text(instance, 'BIT-POSITION', '0'), 0),
                        length=coding['length'],
                        byte_order='big_endian' if big_endian else 'little_endian',
                        is_signed=coding['signed'],
                        conversion=coding['conversion'],
                        unit=coding['unit'],
                        **extra,
                    )
                )
        return result

    def pdu_signals(self, pdu) -> list:
        """A PDU's signals; a multiplexed PDU's switch, static part and switched PDUs as cantools multiplexing."""
        from cantools.database.can import Signal

        multiplexer = _child(pdu, 'MULTIPLEXER')
        if multiplexer is None:
            return self.signal_instances(pdu)
        switch = _child(multiplexer, 'SWITCH')
        if switch is None:
            return self.signal_instances(pdu)
        switch_name = _child_text(switch, 'SHORT-NAME') or f'{_child_text(pdu, "SHORT-NAME")}_Switch'
        big_endian = _child_text(switch, 'IS-HIGH-LOW-BYTE-ORDER', 'false').lower() == 'true'
        signals = [
            Signal(
                name=switch_name,
                start=int(_child_text(switch, 'BIT-POSITION', '0'), 0),
                length=int(_child_text(switch, 'BIT-LENGTH', '0'), 0),
                byte_order='big_endian' if big_endian else 'little_endian',
                is_multiplexer=True,
            )
        ]
        for static in _descendants(multiplexer, 'STATIC-PDU-INSTANCE'):
            static_pdu = self.ids.get(_reference(static, 'PDU-REF'))
            if static_pdu is not None:
                signals += self.signal_instances(static_pdu)
        for switched in _descendants(multiplexer, 'SWITCHED-PDU-INSTANCE'):
            switched_pdu = self.ids.get(_reference(switched, 'PDU-REF'))
            code = _integer(_child_text(switched, 'SWITCH-CODE'))
            if switched_pdu is None or code is None:
                continue
            signals += self.signal_instances(switched_pdu, multiplexer_ids=[code], multiplexer_signal=switch_name)
        return signals

    def frame_pdus(self, frame, length: int) -> List[FlexRayPdu]:
        from cantools.database.can import Message

        name = _child_text(frame, 'SHORT-NAME')
        pdus = []
        direct = self.signal_instances(frame)  # FIBEX 2: the frame's own signals
        if direct:
            pdus.append(
                FlexRayPdu(name, 0, length, Message(frame_id=0, name=name, length=length, signals=direct, strict=False))
            )
        for instance in _descendants(frame, 'PDU-INSTANCE'):  # FIBEX 3/4: signals in PDUs
            pdu = self.ids.get(_reference(instance, 'PDU-REF'))
            if pdu is None:
                continue
            offset = int(_child_text(instance, 'BIT-POSITION', '0'), 0) // 8
            pdu_name = _child_text(pdu, 'SHORT-NAME') or name
            pdu_length = int(_child_text(pdu, 'BYTE-LENGTH', '0') or 0) or max(0, length - offset)
            signals = self.pdu_signals(pdu)
            message = (
                Message(frame_id=0, name=pdu_name, length=pdu_length, signals=signals, strict=False)
                if signals
                else None
            )
            update = _integer(_child_text(instance, 'PDU-UPDATE-BIT-POSITION'))
            pdus.append(FlexRayPdu(pdu_name, offset, pdu_length, message, update))
        return pdus


def _fibex_parameters(cluster) -> FlexRayClusterParameters:
    values = {_local(child): (child.text or '').strip() for child in cluster if len(child) == 0 and child.text}

    def number(name):
        return _number(values.get(name))

    def integer(name):
        return _integer(values.get(name))

    cycle, macrotick = number('CYCLE'), number('MACROTICK')
    return FlexRayClusterParameters(
        baudrate=integer('SPEED'),
        cycle=cycle * 1e-6 if cycle else None,
        macrotick=macrotick * 1e-6 if macrotick else None,
        macro_per_cycle=integer('MACRO-PER-CYCLE'),
        static_slots=integer('NUMBER-OF-STATIC-SLOTS'),
        static_slot_duration=integer('STATIC-SLOT'),
        payload_length_static=integer('PAYLOAD-LENGTH-STATIC'),
        minislots=integer('NUMBER-OF-MINISLOTS'),
        minislot_duration=integer('MINISLOT'),
        network_idle_time=integer('N-I-T'),
        symbol_window=integer('SYMBOL-WINDOW'),
        values=values,
    )


def _fibex_timing(timing) -> Tuple[int, int, int]:
    slot = int(_child_text(timing, 'SLOT-ID', '0'))
    counter = _child_text(timing, 'CYCLE-COUNTER')
    if counter and not _child_text(timing, 'BASE-CYCLE'):
        return slot, int(counter), 64  # In one cycle of the 64
    return (
        slot,
        int(_child_text(timing, 'BASE-CYCLE', '0')),
        _repetition(_child_text(timing, 'CYCLE-REPETITION', '1')),
    )


def _load_fibex(path: Path, root, cluster: Optional[str]) -> FlexRayDatabase:
    fibex = _Fibex(root)
    # FlexRay clusters and their channels (all channels when no cluster says what it is)
    clusters = [c for c in _descendants(root, 'CLUSTER') if 'flexray' in _child_text(c, 'PROTOCOL').lower()]
    names = [_child_text(c, 'SHORT-NAME') for c in clusters]
    chosen = None
    if cluster is not None:
        if cluster not in names:
            raise FlexRayError(f'{path.name} has no FlexRay cluster {cluster!r} (it has {", ".join(names) or "none"})')
        chosen = clusters[names.index(cluster)]
    elif clusters:
        chosen = clusters[0]
    flexray_channels = (
        {ref.get('ID-REF') for ref in _descendants(chosen, 'CHANNEL-REF')} if chosen is not None else set()
    )
    channels = [c for c in _descendants(root, 'CHANNEL') if not flexray_channels or c.get('ID') in flexray_channels]
    definitions: Dict[Tuple[str, int, int, int], FlexRayFrameDefinition] = {}
    for order, channel in enumerate(channels):
        letter = _child_text(channel, 'FLEXRAY-CHANNEL-NAME').upper()[-1:] or ''
        if letter not in ('A', 'B'):
            name = _child_text(channel, 'SHORT-NAME').upper()
            letter = name[-1] if name[-1:] in ('A', 'B') else ('A' if order == 0 else 'B')
        for triggering in _descendants(channel, 'FRAME-TRIGGERING'):
            frame = fibex.ids.get(_reference(triggering, 'FRAME-REF'))
            if frame is None:
                continue
            for timing in _descendants(triggering, 'ABSOLUTELY-SCHEDULED-TIMING'):
                slot, base, repetition = _fibex_timing(timing)
                name = _child_text(frame, 'SHORT-NAME')
                key = (name, slot, base, repetition)
                if key in definitions:
                    definitions[key].channels = definitions[key].channels | {letter}
                    continue
                length = int(_child_text(frame, 'BYTE-LENGTH', '0'))
                definitions[key] = FlexRayFrameDefinition(
                    name, slot, base, repetition, frozenset({letter}), length, fibex.frame_pdus(frame, length)
                )
    if not definitions:
        raise FlexRayError(f'{path.name} describes no FlexRay frames')
    return FlexRayDatabase(
        path,
        sorted(definitions.values(), key=lambda d: (d.slot, d.base_cycle)),
        _child_text(chosen, 'SHORT-NAME') if chosen is not None else '',
        _fibex_parameters(chosen) if chosen is not None else None,
        names,
    )


# --- ARXML ---


def _load_arxml(path: Path, root, cluster: Optional[str]) -> FlexRayDatabase:
    from cantools.database.can import Message
    from cantools.database.can.formats.arxml.system_loader import SystemLoader

    loader = SystemLoader(root, strict=False)
    ns = loader.xml_namespace

    def tag(name: str) -> str:
        return f'{{{ns}}}{name}'

    clusters = list(root.iter(tag('FLEXRAY-CLUSTER')))
    names = [element.findtext(tag('SHORT-NAME')) or '' for element in clusters]
    if not clusters:
        raise FlexRayError(f'{path.name} has no FlexRay cluster with frames')
    if cluster is not None and cluster not in names:
        raise FlexRayError(f'{path.name} has no FlexRay cluster {cluster!r} (it has {", ".join(names)})')
    chosen = clusters[names.index(cluster)] if cluster is not None else clusters[0]

    def pdus_of(frame, name: str, length: int) -> List[FlexRayPdu]:
        pdus = []
        for mapping in loader._get_arxml_children(frame, ['PDU-TO-FRAME-MAPPINGS', '*PDU-TO-FRAME-MAPPING']):
            pdu = loader._get_unique_arxml_child(mapping, '&PDU')
            if pdu is None:
                continue
            offset = (_integer(mapping.findtext(tag('START-POSITION'))) or 0) // 8
            update = _integer(mapping.findtext(tag('UPDATE-INDICATION-BIT-POSITION')))
            pdu_name = pdu.findtext(tag('SHORT-NAME')) or name
            _, pdu_length, signals, _, _, contained = loader._load_pdu(pdu, name, 1)
            pdu_length = pdu_length or max(0, length - offset)
            message = None
            if contained:
                message = Message(
                    frame_id=0,
                    name=pdu_name,
                    length=pdu_length,
                    signals=[],
                    contained_messages=contained,
                    strict=False,
                )
            elif signals:
                message = Message(frame_id=0, name=pdu_name, length=pdu_length, signals=list(signals), strict=False)
            pdus.append(FlexRayPdu(pdu_name, offset, pdu_length, message, update))
        return pdus

    definitions: Dict[Tuple[str, int, int, int], FlexRayFrameDefinition] = {}
    for channel in chosen.iter(tag('FLEXRAY-PHYSICAL-CHANNEL')):
        channel_name = (channel.findtext(tag('CHANNEL-NAME')) or '').upper()
        letters = {letter for letter in ('A', 'B') if channel_name.endswith(letter)} or {'A'}
        for triggering in channel.iter(tag('FLEXRAY-FRAME-TRIGGERING')):
            frame = loader._get_can_frame(triggering)  # Generic: follows FRAME-REF
            if frame is None:
                continue
            name = frame.findtext(tag('SHORT-NAME')) or loader._load_message_name(triggering)
            length = _integer(frame.findtext(tag('FRAME-LENGTH'))) or 0
            pdus = None
            for timing in triggering.iter(tag('FLEXRAY-ABSOLUTELY-SCHEDULED-TIMING')):
                slot = int(timing.findtext(tag('SLOT-ID')) or 0)
                cycle = timing.find(tag('COMMUNICATION-CYCLE'))
                base, repetition = 0, 1
                if cycle is not None and cycle.find(tag('CYCLE-REPETITION')) is not None:
                    repetition_element = cycle.find(tag('CYCLE-REPETITION'))
                    base = int(repetition_element.findtext(tag('BASE-CYCLE')) or 0)
                    repetition = _repetition(repetition_element.findtext(tag('CYCLE-REPETITION')) or '1')
                elif cycle is not None and cycle.find(tag('CYCLE-COUNTER')) is not None:
                    base = int(cycle.find(tag('CYCLE-COUNTER')).findtext(tag('CYCLE-COUNTER')) or 0)
                    repetition = 64
                key = (name, slot, base, repetition)
                if key in definitions:
                    definitions[key].channels = definitions[key].channels | letters
                    continue
                if pdus is None:
                    pdus = pdus_of(frame, name, length)
                definitions[key] = FlexRayFrameDefinition(
                    name, slot, base, repetition, frozenset(letters), length, pdus
                )
    if not definitions:
        raise FlexRayError(f'{path.name} has no FlexRay cluster with frames')
    return FlexRayDatabase(
        path,
        sorted(definitions.values(), key=lambda d: (d.slot, d.base_cycle)),
        names[clusters.index(chosen)],
        _arxml_parameters(chosen, tag),
        names,
        _arxml_controllers(root, tag),
    )


def _leaf_values(element) -> Dict[str, str]:
    return {_local(child): (child.text or '').strip() for child in element if len(child) == 0 and child.text}


def _arxml_parameters(cluster, tag) -> FlexRayClusterParameters:
    conditional = cluster.find(f'{tag("FLEXRAY-CLUSTER-VARIANTS")}/{tag("FLEXRAY-CLUSTER-CONDITIONAL")}')
    values = _leaf_values(conditional if conditional is not None else cluster)

    def integer(name):
        return _integer(values.get(name))

    return FlexRayClusterParameters(
        baudrate=integer('BAUDRATE'),
        cycle=_number(values.get('CYCLE')),
        macrotick=_number(values.get('MACROTICK-DURATION')),
        macro_per_cycle=integer('MACRO-PER-CYCLE'),
        static_slots=integer('NUMBER-OF-STATIC-SLOTS'),
        static_slot_duration=integer('STATIC-SLOT-DURATION'),
        payload_length_static=integer('PAYLOAD-LENGTH-STATIC'),
        minislots=integer('NUMBER-OF-MINISLOTS'),
        minislot_duration=integer('MINISLOT-DURATION'),
        network_idle_time=integer('NETWORK-IDLE-TIME'),
        symbol_window=integer('SYMBOL-WINDOW'),
        values=values,
    )


def _arxml_controllers(root, tag) -> Dict[str, Dict[str, str]]:
    controllers = {}
    for ecu in root.iter(tag('ECU-INSTANCE')):
        for controller in ecu.iter(tag('FLEXRAY-COMMUNICATION-CONTROLLER')):
            conditional = controller.find(
                f'{tag("FLEXRAY-COMMUNICATION-CONTROLLER-VARIANTS")}/{tag("FLEXRAY-COMMUNICATION-CONTROLLER-CONDITIONAL")}'
            )
            values = _leaf_values(conditional if conditional is not None else controller)
            if values:
                controllers[ecu.findtext(tag('SHORT-NAME')) or controller.findtext(tag('SHORT-NAME')) or ''] = values
    return controllers


# --- A simulated cluster ---


def _simulated_values(message, cycle_number: int) -> Dict[str, Any]:
    import math

    values = {}
    for index, signal in enumerate(message.signals):
        maximum = (1 << signal.length) - 1
        if signal.is_multiplexer:
            ids = sorted({i for s in message.signals for i in (s.multiplexer_ids or [])})
            values[signal.name] = ids[(cycle_number // 16) % len(ids)] if ids else 0
        elif signal.choices:
            keys = sorted(int(key) for key in signal.choices)
            values[signal.name] = keys[(cycle_number // 64) % len(keys)]
        elif signal.length <= 4:
            values[signal.name] = cycle_number % (maximum + 1)  # Counters
        else:
            wave = 0.5 + 0.45 * math.sin(cycle_number / (40.0 + 7 * index))
            raw = int(maximum * wave)
            values[signal.name] = raw - (maximum + 1) // 2 if signal.is_signed else raw
    return values


def encode_pdu(pdu: FlexRayPdu, values: Dict[str, Any], scaling: bool = True) -> bytes:
    """A PDU's bytes for signal values (missing signals are 0). Raises FlexRayError when cantools cannot encode."""
    if pdu.message is None:
        return bytes(pdu.length)
    complete = {signal.name: 0 for signal in pdu.message.signals}
    complete.update(values)
    try:
        data = pdu.message.encode(complete, scaling=scaling, strict=False)
    except Exception as e:  # noqa: BLE001 - shown to the user
        raise FlexRayError(f'{pdu.name}: {e}') from e
    return bytes(data).ljust(pdu.length, b'\0')[: pdu.length]


def encode_frame(
    definition: FlexRayFrameDefinition, values: Dict[str, Any], scaling: bool = True, update_bits: bool = True
) -> bytes:
    """A frame's payload: each PDU encoded at its offset, its update bit set; padded to an even length."""
    length = definition.length + (definition.length % 2)
    payload = bytearray(length)
    for pdu in definition.pdus:
        names = {signal.name for signal in pdu.message.signals} if pdu.message is not None else set()
        pdu_values = {name: value for name, value in values.items() if name in names}
        data = encode_pdu(pdu, pdu_values, scaling)
        payload[pdu.offset : pdu.offset + len(data)] = data[: max(0, length - pdu.offset)]
    if update_bits:
        _set_update_bits(definition, payload)  # After all PDUs: an update bit may lie in another PDU's bytes
    return bytes(payload[:length])


def _set_update_bits(definition: FlexRayFrameDefinition, payload: bytearray) -> None:
    for pdu in definition.pdus:
        if pdu.update_bit is not None and pdu.update_bit // 8 < len(payload):
            payload[pdu.update_bit // 8] |= 1 << (pdu.update_bit % 8)


class ClusterSimulator:
    """The frames of a cluster's schedule, cycle after cycle, with changing signal values: for trying the FlexRay
    tool and its decoding without a cluster. The cycle time defaults to the description's; static slots are placed
    at their offsets when the description gives the slot length, else each slot gets an equal share of the cycle.
    Every PDU is sent updated. Sync and startup frames are sent in the key slots of the description's controllers
    (KEY-SLOT-USED-FOR-SYNC / -START-UP), else in the two lowest slots sent every cycle, as a cluster needs two."""

    def __init__(self, database: FlexRayDatabase, cycle_time: Optional[float] = None, start: Optional[float] = None):
        import time

        self.database = database
        self.cycle_time = cycle_time or database.parameters.cycle_time or 0.005
        self.start = time.time() if start is None else start
        self.cycles = 0  # Communication cycles simulated so far
        self._slots = max((d.slot for d in database.definitions), default=1)
        self.sync_slots, self.startup_slots = set(), set()
        for values in database.controllers.values():
            key = _integer(values.get('KEY-SLOT-ID'))
            if key:
                if values.get('KEY-SLOT-USED-FOR-SYNC', '').lower() == 'true':
                    self.sync_slots.add(key)
                if values.get('KEY-SLOT-USED-FOR-START-UP', '').lower() == 'true':
                    self.startup_slots.add(key)
        if not self.sync_slots:
            every_cycle = sorted({d.slot for d in database.definitions if d.repetition == 1})[:2]
            self.sync_slots = self.startup_slots = set(every_cycle)
        self.transmissions: Dict[int, Tuple[int, int, str, bytes]] = {}  # slot → (base, repetition, channels, data)

    def transmit(self, slot: int, base_cycle: int, repetition: int, channels: str, payload: bytes) -> None:
        """Send `payload` in `slot` from now on (in place of the simulated values), as an interface would."""
        if len(payload) % 2 or len(payload) > 254:
            raise FlexRayError('A FlexRay payload has an even number of bytes, up to 254')
        if repetition not in CYCLE_REPETITIONS or not 0 <= base_cycle < repetition:
            raise FlexRayError(f'Not a FlexRay schedule: from cycle {base_cycle}, every {repetition}')
        self.transmissions[slot] = (base_cycle, repetition, channels, bytes(payload))

    def stop_transmit(self, slot: int) -> None:
        self.transmissions.pop(slot, None)

    def _payload(self, definition: FlexRayFrameDefinition, cycle_number: int) -> bytes:
        payload = bytearray(definition.length + (definition.length % 2))
        for pdu in definition.pdus:
            try:
                data = (
                    encode_pdu(pdu, _simulated_values(pdu.message, cycle_number), scaling=False)
                    if pdu.message is not None and not getattr(pdu.message, 'contained_messages', None)
                    else bytes(pdu.length)
                )
            except FlexRayError:
                data = bytes(pdu.length)  # A definition cantools cannot encode: zeros
            payload[pdu.offset : pdu.offset + len(data)] = data[: max(0, len(payload) - pdu.offset)]
        _set_update_bits(definition, payload)
        return bytes(payload)

    def _offset(self, slot: int) -> float:
        offset = self.database.parameters.static_slot_offset(slot)
        if offset is not None and offset < self.cycle_time:
            return offset
        return self.cycle_time * 0.6 * slot / (self._slots + 1)

    def frames_until(self, now: float) -> List[FlexRayFrame]:
        """The frames of every cycle that has ended by `now` and was not returned yet."""
        frames = []
        while self.start + (self.cycles + 1) * self.cycle_time <= now:
            cycle = self.cycles % 64
            begin = self.start + self.cycles * self.cycle_time
            for definition in self.database.definitions:
                if definition.slot in self.transmissions or not definition.in_cycle(cycle):
                    continue
                payload = self._payload(definition, self.cycles)
                when = begin + self._offset(definition.slot)
                sync, startup = definition.slot in self.sync_slots, definition.slot in self.startup_slots
                for channel in sorted(definition.channels):
                    frames.append(
                        FlexRayFrame(when, channel, definition.slot, cycle, payload, sync=sync, startup=startup)
                    )
            for slot, (base, repetition, channels, payload) in self.transmissions.items():
                if cycle % repetition != base:
                    continue
                when = begin + self._offset(slot)
                sync, startup = slot in self.sync_slots, slot in self.startup_slots
                for channel in sorted(set(channels) & {'A', 'B'}):
                    frames.append(
                        FlexRayFrame(when, channel, slot, cycle, payload, sync=sync, startup=startup, transmitted=True)
                    )
            self.cycles += 1
        frames.sort(key=lambda frame: frame.timestamp)
        return frames
