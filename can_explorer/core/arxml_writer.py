"""Writer for AUTOSAR system descriptions (.arxml) of CAN networks, on top of cantools' database model.

cantools reads ARXML but cannot write it. This module writes an AUTOSAR R20-11 document (schema AUTOSAR_00049.xsd,
XML namespace http://autosar.org/schema/r4.0) describing the CAN part of a database:

- one CAN-CLUSTER per bus (a multi-bus database keeps its buses apart), with its physical channel, the frame
  triggerings (identifier, standard/extended addressing, CAN 2.0/CAN FD) and a PDU triggering per PDU;
- per message a CAN-FRAME carrying one PDU: an I-SIGNAL-I-PDU, a MULTIPLEXED-I-PDU (one multiplexer: a static part
  and a dynamic part alternative per multiplexer value), a CONTAINER-I-PDU (short headers) or a SECURED-I-PDU;
- per signal an I-SIGNAL (length, base type, initial and invalid value) and a SYSTEM-SIGNAL (comment, unit and
  COMPU-METHOD: IDENTICAL, LINEAR, TEXTTABLE or SCALE_LINEAR_AND_TEXTTABLE, with the limits);
- the cycle time as the PDU's cyclic timing, senders and receivers as ECU-INSTANCEs with I-SIGNAL-I-PDU-GROUPs,
  E2E protections (profile and Data IDs) and SecOC properties (Data ID, authenticator and freshness lengths).

Bit positions need no conversion: an ISignalToIPduMapping's START-POSITION is the least significant bit of a little
endian and the most significant bit of a big endian signal, counted "sawtooth" (AUTOSAR_00049.xsd, documentation of
START-POSITION and SELECTOR-FIELD-START-POSITION), exactly cantools' (and DBC's) start bit.

Not written, because the model has no place for it or AUTOSAR none for the content: nested (extended) multiplexing
(a MULTIPLEXED-I-PDU has a single selector field), DBC attributes besides the cycle time, signal groups, and
per-signal receivers (an ECU receives whole PDUs). E2E and SecOC get what cantools reads from an ARXML (profile,
Data IDs; Data ID, algorithm names, lengths), not the full transformer configuration.
"""

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from cantools.database.can import Database, Message, Signal

NAMESPACE = 'http://autosar.org/schema/r4.0'
SCHEMA = 'AUTOSAR_00049.xsd'  # AUTOSAR R20-11
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
DEFAULT_BUS = 'CAN'
DBC_NO_NODE = 'Vector__XXX'  # DBC placeholder for "no sender/receiver"
_SHORT_NAME_LIMIT = 128
# First line after the XML declaration of every file written here. A file starting with it holds nothing the writer
# would drop, so the database editor may save over it; ARXML tools re-serialise files without such comments.
MARKER = '<!-- Written by CANExplorer: the CAN signal database only. -->'


def short_name(text: str) -> str:
    """An AUTOSAR identifier ([a-zA-Z][a-zA-Z0-9_]*, at most 128 characters) for `text`."""
    name = re.sub(r'[^A-Za-z0-9_]', '_', text.strip())
    if not name or not name[0].isalpha():
        name = f'N{name}'
    return name[:_SHORT_NAME_LIMIT]


def _number(value: float) -> str:
    """A number as AUTOSAR numerical text: integers without a fraction, other values with full precision."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 2**63):
        return str(int(value))
    return repr(float(value))


def _seconds(milliseconds: float) -> str:
    """A cycle time in seconds (TIME-PERIOD). Readers that convert back with int(seconds * 1000), as cantools does,
    get e.g. 1000 ms for 1.001 s, since 1.001 is stored as 1.000999…; for such values the next double up is written,
    10⁻¹⁶ s more, which converts back exactly."""
    seconds = milliseconds / 1000
    if float(milliseconds).is_integer() and int(seconds * 1000) < milliseconds:
        seconds = math.nextafter(seconds, math.inf)
    return _number(seconds)


def _raw(physical: float, scale: float, offset: float) -> float:
    """The raw value of a physical one; nearly integral results (rounding of the division) become integers."""
    raw = (physical - offset) / scale
    nearest = round(raw)
    return nearest if math.isclose(raw, nearest, rel_tol=1e-12, abs_tol=1e-9) else raw


def _comments(item) -> Dict[Optional[str], str]:
    comments = getattr(item, 'comments', None)
    if isinstance(comments, dict):
        return {language: text for language, text in comments.items() if text}
    comment = getattr(item, 'comment', None)
    return {None: comment} if comment else {}


@dataclass
class _Package:
    element: ET.Element
    elements: Optional[ET.Element] = None
    packages: Optional[ET.Element] = None
    children: Dict[str, '_Package'] = field(default_factory=dict)
    names: set = field(default_factory=set)  # SHORT-NAMEs of the elements


class _Writer:
    def __init__(self, database: Database):
        self.database = database
        self.warnings: List[str] = []
        self.root = ET.Element('AUTOSAR', {'xmlns': NAMESPACE, 'xmlns:xsi': XSI})
        self.root.set('xsi:schemaLocation', f'{NAMESPACE} {SCHEMA}')
        self._top = ET.SubElement(self.root, 'AR-PACKAGES')
        self._packages: Dict[str, _Package] = {}
        self._channels: Dict[str, Tuple[str, ET.Element, ET.Element, ET.Element]] = {}  # bus → (path, ...)
        self._units: Dict[str, str] = {}
        self._base_types: Dict[Tuple[int, bool, bool], str] = {}
        self._signals: Dict[Tuple[str, int], str] = {}  # (PDU scope, id(signal)) → I-SIGNAL path
        self._pdu_groups: Dict[Tuple[str, str], List[str]] = {}  # (ECU, 'OUT'|'IN') → I-SIGNAL-I-PDU paths
        self._ecus: Dict[str, Dict[Optional[str], str]] = {}  # ECU name → comments
        self._e2e: List[Tuple[str, object, str]] = []  # (protection name, e2e properties, PDU path)
        self._fibex: List[Tuple[str, str]] = []  # (DEST, path)
        self._header_byte_orders: Dict[str, List[str]] = {}
        self._renamed: set = set()
        self._triggering_names: Dict[str, set] = {}  # Per bus: SHORT-NAMEs in its physical channel

    # --- XML helpers ---

    @staticmethod
    def _sub(parent: ET.Element, tag: str, text=None, **attributes) -> ET.Element:
        element = ET.SubElement(parent, tag, {key.replace('_', '-'): value for key, value in attributes.items()})
        if text is not None:
            element.text = text if isinstance(text, str) else _number(text)
        return element

    def _ref(self, parent: ET.Element, tag: str, dest: str, path: str) -> ET.Element:
        return self._sub(parent, tag, path, DEST=dest)

    def _desc(self, parent: ET.Element, comments: Dict[Optional[str], str]) -> None:
        if not comments:
            return
        desc = self._sub(parent, 'DESC')
        for language, text in comments.items():
            self._sub(desc, 'L-2', text, L='FOR-ALL' if language is None else language.upper())

    def _package(self, *names: str) -> Tuple[str, _Package]:
        """The package at /names[0]/names[1]/..., created on first use."""
        siblings, parent, path = self._packages, None, ''
        for name in names:
            name = short_name(name)
            path += f'/{name}'
            package = siblings.get(name)
            if package is None:
                if parent is None:
                    container = self._top
                else:
                    if parent.packages is None:
                        parent.packages = self._sub(parent.element, 'AR-PACKAGES')
                    container = parent.packages
                element = self._sub(container, 'AR-PACKAGE')
                self._sub(element, 'SHORT-NAME', name)
                package = siblings[name] = _Package(element)
            parent, siblings = package, package.children
        return path, parent

    def _element(self, tag: str, name: str, *package: str, label: str = '') -> Tuple[str, ET.Element]:
        """A new element named `name` (made an AUTOSAR identifier, unique in its package)."""
        path, target = self._package(*package)
        if target.elements is None:
            # ELEMENTS precedes AR-PACKAGES in a package
            target.elements = ET.Element('ELEMENTS')
            index = list(target.element).index(target.packages) if target.packages is not None else len(target.element)
            target.element.insert(index, target.elements)
        unique = self._unique(name, target.names, label)
        element = self._sub(target.elements, tag)
        self._sub(element, 'SHORT-NAME', unique)
        return f'{path}/{unique}', element

    def _unique(self, name: str, taken: set, label: str = '') -> str:
        """`name` made an AUTOSAR identifier not in `taken` (and added to it); a change of an item `label` describes
        (e.g. "Signal 'Engine.Speed'") is reported."""
        base = short_name(name)
        unique, number = base, 1
        while unique in taken:
            number += 1
            unique = f'{base[: _SHORT_NAME_LIMIT - len(str(number)) - 1]}_{number}'
        taken.add(unique)
        if label and unique != name and label not in self._renamed:
            self._renamed.add(label)
            reason = 'already used' if base == name else 'ARXML names allow only letters, digits and _'
            self.warnings.append(f'{label} written as {unique!r} ({reason})')
        return unique

    # --- Document ---

    def write(self) -> ET.ElementTree:
        buses = {bus.name: bus for bus in self.database.buses}
        default_bus = self.database.buses[0].name if len(self.database.buses) == 1 else DEFAULT_BUS
        for node in self.database.nodes:
            if node.name != DBC_NO_NODE:
                self._ecus.setdefault(node.name, _comments(node))
        for message in self.database.messages:
            if message.frame_id > (0x1FFFFFFF if message.is_extended_frame else 0x7FF):
                self.warnings.append(
                    f'Message {message.name!r} skipped: identifier 0x{message.frame_id:X} out of range'
                )
                continue
            bus = message.bus_name or default_bus
            self._message(message, buses.get(bus), bus)
        self._write_ecus()
        self._write_e2e()
        self._write_system()
        ET.indent(self.root, space='  ')
        return ET.ElementTree(self.root)

    def _channel(self, bus_name: str, bus) -> Tuple[str, ET.Element, ET.Element]:
        """(channel path, FRAME-TRIGGERINGS, PDU-TRIGGERINGS) of a bus, its cluster created on first use."""
        if bus_name in self._channels:
            path, _, frames, pdus = self._channels[bus_name]
            return path, frames, pdus
        cluster_path, cluster = self._element('CAN-CLUSTER', bus_name, 'Clusters', label=f'Bus {bus_name!r}')
        self._fibex.append(('CAN-CLUSTER', cluster_path))
        self._desc(cluster, _comments(bus) if bus is not None else {})
        conditional = self._sub(self._sub(cluster, 'CAN-CLUSTER-VARIANTS'), 'CAN-CLUSTER-CONDITIONAL')
        if bus is not None and bus.baudrate:
            self._sub(conditional, 'BAUDRATE', bus.baudrate)
        channels = self._sub(conditional, 'PHYSICAL-CHANNELS')
        channel = self._sub(channels, 'CAN-PHYSICAL-CHANNEL')
        name = f'{short_name(bus_name)}_Channel'
        self._sub(channel, 'SHORT-NAME', name)
        frames = self._sub(channel, 'FRAME-TRIGGERINGS')
        pdus = self._sub(channel, 'PDU-TRIGGERINGS')
        self._sub(conditional, 'PROTOCOL-NAME', 'CAN')
        if bus is not None and bus.fd_baudrate:
            self._sub(conditional, 'CAN-FD-BAUDRATE', bus.fd_baudrate)
        path = f'{cluster_path}/{name}'
        self._channels[bus_name] = (path, channel, frames, pdus)
        return path, frames, pdus

    def _message(self, message: Message, bus, bus_name: str) -> None:
        _, frame_triggerings, _ = self._channel(bus_name, bus)
        scope = short_name(bus_name)
        triggering_names = self._channel_names(bus_name)
        pdu_dest, pdu_path, signal_pdus = self._message_pdu(message, message.name, scope, bus_name)
        triggering = self._pdu_triggering(message.name, pdu_dest, pdu_path, bus_name)

        frame_path, frame = self._element('CAN-FRAME', message.name, 'Frames', scope, label=f'Message {message.name!r}')
        self._fibex.append(('CAN-FRAME', frame_path))
        self._desc(frame, _comments(message))
        self._sub(frame, 'FRAME-LENGTH', message.length)
        mapping = self._sub(self._sub(frame, 'PDU-TO-FRAME-MAPPINGS'), 'PDU-TO-FRAME-MAPPING')
        self._sub(mapping, 'SHORT-NAME', f'{frame_path.rsplit("/", 1)[1]}_Pdu')
        self._sub(mapping, 'PACKING-BYTE-ORDER', 'MOST-SIGNIFICANT-BYTE-LAST')
        self._ref(mapping, 'PDU-REF', pdu_dest, pdu_path)
        self._sub(mapping, 'START-POSITION', 0)

        frame_triggering = self._sub(frame_triggerings, 'CAN-FRAME-TRIGGERING')
        self._sub(
            frame_triggering, 'SHORT-NAME', self._unique(f'{frame_path.rsplit("/", 1)[1]}_Frame', triggering_names)
        )
        self._ref(frame_triggering, 'FRAME-REF', 'CAN-FRAME', frame_path)
        conditional = self._sub(self._sub(frame_triggering, 'PDU-TRIGGERINGS'), 'PDU-TRIGGERING-REF-CONDITIONAL')
        self._ref(conditional, 'PDU-TRIGGERING-REF', 'PDU-TRIGGERING', triggering)
        behavior = 'CAN-FD' if message.is_fd else 'CAN-20'
        self._sub(frame_triggering, 'CAN-ADDRESSING-MODE', 'EXTENDED' if message.is_extended_frame else 'STANDARD')
        self._sub(frame_triggering, 'CAN-FRAME-RX-BEHAVIOR', behavior)
        self._sub(frame_triggering, 'CAN-FRAME-TX-BEHAVIOR', behavior)
        self._sub(frame_triggering, 'IDENTIFIER', message.frame_id)

        for sender in message.senders:
            self._pdu_group(sender, 'OUT', signal_pdus)
        receivers = []
        for part in message.contained_messages or [message]:
            for signal in part.signals:
                receivers += [receiver for receiver in signal.receivers if receiver not in receivers]
        for receiver in receivers:
            self._pdu_group(receiver, 'IN', signal_pdus)

    def _channel_names(self, bus_name: str) -> set:
        return self._triggering_names.setdefault(bus_name, set())

    def _pdu_triggering(self, name: str, dest: str, pdu_path: str, bus_name: str) -> str:
        channel_path, _, pdus = self._channel(bus_name, None)
        triggering = self._sub(pdus, 'PDU-TRIGGERING')
        unique = self._unique(f'{short_name(name)}_PduTriggering', self._channel_names(bus_name))
        self._sub(triggering, 'SHORT-NAME', unique)
        self._ref(triggering, 'I-PDU-REF', dest, pdu_path)
        return f'{channel_path}/{unique}'

    def _pdu_group(self, ecu: str, direction: str, pdus: List[str]) -> None:
        if ecu == DBC_NO_NODE:
            return
        self._ecus.setdefault(ecu, {})
        group = self._pdu_groups.setdefault((ecu, direction), [])
        group += [pdu for pdu in pdus if pdu not in group]

    # --- PDUs ---

    def _message_pdu(self, message: Message, name: str, scope: str, bus_name: str) -> Tuple[str, str, List[str]]:
        """Write the PDU of a message (or of a contained message) on bus `bus_name`. Returns (its DEST, its path,
        the paths of the I-SIGNAL-I-PDUs holding its signals)."""
        autosar = getattr(message, 'autosar', None)
        secoc = getattr(autosar, 'secoc', None) if autosar is not None else None
        e2e = getattr(autosar, 'e2e', None) if autosar is not None else None
        if message.is_container:
            dest, path, signal_pdus = self._container_pdu(message, name, scope, bus_name)
        elif secoc is not None:
            dest, path, signal_pdus = self._secured_pdu(message, name, scope, bus_name, secoc)
        else:
            dest, path, signal_pdus = self._payload_pdu(message, name, scope, message.signals, message.length)
        if e2e is not None and not message.is_container:
            if dest == 'MULTIPLEXED-I-PDU' or (secoc is not None and len(signal_pdus) != 1):
                self.warnings.append(f'E2E protection of multiplexed message {name!r} not written')
            else:
                self._e2e.append((name, e2e, signal_pdus[0]))
        return dest, path, signal_pdus

    def _contained_props(self, pdu: ET.Element, message: Message) -> None:
        if message.header_id is not None:
            props = ET.Element('CONTAINED-I-PDU-PROPS')
            self._sub(props, 'HEADER-ID-SHORT-HEADER', message.header_id)
            # The first attribute of an I-PDU, right after the PDU's LENGTH
            index = next(i for i, child in enumerate(pdu) if child.tag == 'LENGTH')
            pdu.insert(index + 1, props)

    def _payload_pdu(
        self, message: Message, name: str, scope: str, signals: Iterable[Signal], length: int
    ) -> Tuple[str, str, List[str]]:
        """An I-SIGNAL-I-PDU, or a MULTIPLEXED-I-PDU when the signals are multiplexed."""
        signals = list(signals)
        multiplexers = [signal for signal in signals if signal.is_multiplexer]
        if not multiplexers:
            path, pdu = self._signal_pdu(name, scope, signals, length, message.cycle_time, message)
            return 'I-SIGNAL-I-PDU', path, [path]
        return self._multiplexed_pdu(message, name, scope, signals, multiplexers, length)

    def _signal_pdu(
        self,
        name: str,
        scope: str,
        signals: List[Signal],
        length: int,
        cycle_time: Optional[float],
        message: Message,
        signal_scope: Optional[str] = None,
    ) -> Tuple[str, ET.Element]:
        path, pdu = self._element('I-SIGNAL-I-PDU', name, 'Pdus', scope)
        self._fibex.append(('I-SIGNAL-I-PDU', path))
        self._sub(pdu, 'LENGTH', length)
        if cycle_time:
            timing = self._sub(self._sub(pdu, 'I-PDU-TIMING-SPECIFICATIONS'), 'I-PDU-TIMING')
            declaration = self._sub(timing, 'TRANSMISSION-MODE-DECLARATION')
            true_timing = self._sub(declaration, 'TRANSMISSION-MODE-TRUE-TIMING')
            period = self._sub(self._sub(true_timing, 'CYCLIC-TIMING'), 'TIME-PERIOD')
            self._sub(period, 'VALUE', _seconds(cycle_time))
        if signals:
            mappings = self._sub(pdu, 'I-SIGNAL-TO-PDU-MAPPINGS')
            names: set = set()
            for signal in signals:
                signal_path = self._i_signal(signal, signal_scope or path.rsplit('/', 1)[1], scope, message.name)
                mapping = self._sub(mappings, 'I-SIGNAL-TO-I-PDU-MAPPING')
                self._sub(mapping, 'SHORT-NAME', self._unique(signal_path.rsplit('/', 1)[1], names))
                self._ref(mapping, 'I-SIGNAL-REF', 'I-SIGNAL', signal_path)
                order = (
                    'MOST-SIGNIFICANT-BYTE-FIRST' if signal.byte_order == 'big_endian' else 'MOST-SIGNIFICANT-BYTE-LAST'
                )
                self._sub(mapping, 'PACKING-BYTE-ORDER', order)
                self._sub(mapping, 'START-POSITION', signal.start)
        unused = getattr(message, 'unused_bit_pattern', None)
        if unused is not None:
            self._sub(pdu, 'UNUSED-BIT-PATTERN', unused)
        return path, pdu

    def _multiplexed_pdu(
        self, message: Message, name: str, scope: str, signals: List[Signal], multiplexers: List[Signal], length: int
    ) -> Tuple[str, str, List[str]]:
        selector = next((signal for signal in multiplexers if signal.multiplexer_signal is None), multiplexers[0])
        top_level = [signal for signal in signals if signal is not selector and signal.multiplexer_signal is None]
        dynamic = [signal for signal in signals if signal.multiplexer_signal == selector.name]
        dropped = [
            signal for signal in signals if signal is not selector and signal not in top_level and signal not in dynamic
        ]
        if dropped:
            self.warnings.append(
                f'Message {name!r}: nested multiplexing cannot be expressed in ARXML; written without '
                f'{", ".join(signal.name for signal in dropped)}'
            )
        values = sorted({value for signal in dynamic for value in signal.multiplexer_ids or ()})
        signal_scope = short_name(name)
        signal_pdus: List[str] = []
        static_path = None
        if top_level:
            static_path, _ = self._signal_pdu(
                f'{name}_Static', scope, top_level, length, message.cycle_time, message, signal_scope
            )
            signal_pdus.append(static_path)
        alternatives = []
        for value in values:
            members = [selector] + [signal for signal in dynamic if value in (signal.multiplexer_ids or ())]
            path, _ = self._signal_pdu(
                f'{name}_{value}', scope, members, length, message.cycle_time, message, signal_scope
            )
            alternatives.append((value, path))
            signal_pdus.append(path)
        if not alternatives:
            # A multiplexer without multiplexed signals: an alternative with only the selector keeps it
            path, _ = self._signal_pdu(
                f'{name}_0', scope, [selector], length, message.cycle_time, message, signal_scope
            )
            alternatives.append((0, path))
            signal_pdus.append(path)

        path, pdu = self._element('MULTIPLEXED-I-PDU', name, 'Pdus', scope)
        self._fibex.append(('MULTIPLEXED-I-PDU', path))
        self._sub(pdu, 'LENGTH', length)
        dynamic_part = self._sub(self._sub(pdu, 'DYNAMIC-PARTS'), 'DYNAMIC-PART')
        alternatives_element = self._sub(dynamic_part, 'DYNAMIC-PART-ALTERNATIVES')
        initial = selector.raw_initial
        for value, alternative_path in alternatives:
            alternative = self._sub(alternatives_element, 'DYNAMIC-PART-ALTERNATIVE')
            self._ref(alternative, 'I-PDU-REF', 'I-SIGNAL-I-PDU', alternative_path)
            self._sub(alternative, 'INITIAL-DYNAMIC-PART', 'true' if value == initial else 'false')
            self._sub(alternative, 'SELECTOR-FIELD-CODE', value)
        order = 'MOST-SIGNIFICANT-BYTE-FIRST' if selector.byte_order == 'big_endian' else 'MOST-SIGNIFICANT-BYTE-LAST'
        self._sub(pdu, 'SELECTOR-FIELD-BYTE-ORDER', order)
        self._sub(pdu, 'SELECTOR-FIELD-LENGTH', selector.length)
        self._sub(pdu, 'SELECTOR-FIELD-START-POSITION', selector.start)
        if static_path is not None:
            static = self._sub(self._sub(pdu, 'STATIC-PARTS'), 'STATIC-PART')
            self._ref(static, 'I-PDU-REF', 'I-SIGNAL-I-PDU', static_path)
        self._sub(pdu, 'TRIGGER-MODE', 'DYNAMIC-PART-TRIGGER')
        unused = getattr(message, 'unused_bit_pattern', None)
        if unused is not None:
            self._sub(pdu, 'UNUSED-BIT-PATTERN', unused)
        return 'MULTIPLEXED-I-PDU', path, signal_pdus

    def _secured_pdu(self, message: Message, name: str, scope: str, bus_name: str, secoc) -> Tuple[str, str, List[str]]:
        # cantools adds the truncated freshness value and authenticator as pseudo signals; they are not payload
        pseudo = {f'{name}_Freshness', f'{name}_Authenticator'}
        payload = [signal for signal in message.signals if signal.name not in pseudo]
        payload_length = secoc.payload_length if secoc.payload_length is not None else message.length
        payload_dest, payload_path, signal_pdus = self._payload_pdu(
            message, f'{name}_Payload', scope, payload, payload_length
        )
        payload_triggering = self._pdu_triggering(f'{name}_Payload', payload_dest, payload_path, bus_name)

        # The properties' SHORT-NAMEs are the algorithm names (that is how cantools reads them), so each secured PDU
        # gets its own SECURE-COMMUNICATION-PROPS-SET: two PDUs may use one algorithm with different lengths
        auth = secoc.auth_algorithm_name or (None if secoc.auth_tx_bit_length is None else 'Authentication')
        freshness = secoc.freshness_algorithm_name or (None if secoc.freshness_bit_length is None else 'Freshness')
        for algorithm, given, kind in (
            (auth, secoc.auth_algorithm_name, 'authentication'),
            (freshness, secoc.freshness_algorithm_name, 'freshness'),
        ):
            if algorithm and not given:
                self.warnings.append(f'Secured PDU {name!r}: no {kind} algorithm, named it {algorithm!r}')
        props_path = freshness_path = None
        if auth or freshness:
            set_path, props_set = self._element('SECURE-COMMUNICATION-PROPS-SET', name, 'SecOC', scope)
            if auth:
                props = self._sub(
                    self._sub(props_set, 'AUTHENTICATION-PROPSS'), 'SECURE-COMMUNICATION-AUTHENTICATION-PROPS'
                )
                auth_name = short_name(auth)
                self._sub(props, 'SHORT-NAME', auth_name)
                self._sub(props, 'AUTH-ALGORITHM', auth)
                if secoc.auth_tx_bit_length is not None:
                    self._sub(props, 'AUTH-INFO-TX-LENGTH', secoc.auth_tx_bit_length)
                props_path = f'{set_path}/{auth_name}'
            if freshness:
                props = self._sub(self._sub(props_set, 'FRESHNESS-PROPSS'), 'SECURE-COMMUNICATION-FRESHNESS-PROPS')
                freshness_name = short_name(freshness)
                if auth and freshness_name == short_name(auth):
                    freshness_name = f'{freshness_name}_Freshness'
                self._sub(props, 'SHORT-NAME', freshness_name)
                if secoc.freshness_bit_length is not None:
                    self._sub(props, 'FRESHNESS-VALUE-LENGTH', secoc.freshness_bit_length)
                if secoc.freshness_tx_bit_length is not None:
                    self._sub(props, 'FRESHNESS-VALUE-TX-LENGTH', secoc.freshness_tx_bit_length)
                freshness_path = f'{set_path}/{freshness_name}'
            for algorithm in (auth, freshness):
                if algorithm and short_name(algorithm) != algorithm:
                    self.warnings.append(
                        f'Secured PDU {name!r}: algorithm {algorithm!r} written as {short_name(algorithm)!r}'
                    )

        path, pdu = self._element('SECURED-I-PDU', name, 'Pdus', scope)
        self._fibex.append(('SECURED-I-PDU', path))
        self._sub(pdu, 'LENGTH', message.length)
        if props_path is not None:
            self._ref(pdu, 'AUTHENTICATION-PROPS-REF', 'SECURE-COMMUNICATION-AUTHENTICATION-PROPS', props_path)
        if freshness_path is not None:
            self._ref(pdu, 'FRESHNESS-PROPS-REF', 'SECURE-COMMUNICATION-FRESHNESS-PROPS', freshness_path)
        self._ref(pdu, 'PAYLOAD-REF', 'PDU-TRIGGERING', payload_triggering)
        if secoc.data_id is not None:
            self._sub(self._sub(pdu, 'SECURE-COMMUNICATION-PROPS'), 'DATA-ID', secoc.data_id)
        return 'SECURED-I-PDU', path, signal_pdus

    def _container_pdu(self, message: Message, name: str, scope: str, bus_name: str) -> Tuple[str, str, List[str]]:
        order = getattr(message, 'header_byte_order', 'big_endian') or 'big_endian'
        self._header_byte_orders.setdefault(order, []).append(name)
        contained_triggerings, signal_pdus = [], []
        for contained in message.contained_messages or []:
            if contained.header_id is None:
                self.warnings.append(f'Contained PDU {contained.name!r} of {name!r} skipped: it has no header ID')
                continue
            dest, path, pdus = self._message_pdu(contained, contained.name, scope, bus_name)
            self._contained_props(self._find(path), contained)
            contained_triggerings.append(self._pdu_triggering(contained.name, dest, path, bus_name))
            signal_pdus += pdus
        path, pdu = self._element('CONTAINER-I-PDU', name, 'Pdus', scope)
        self._fibex.append(('CONTAINER-I-PDU', path))
        self._sub(pdu, 'LENGTH', message.length)
        if contained_triggerings:
            refs = self._sub(pdu, 'CONTAINED-PDU-TRIGGERING-REFS')
            for triggering in contained_triggerings:
                self._ref(refs, 'CONTAINED-PDU-TRIGGERING-REF', 'PDU-TRIGGERING', triggering)
        self._sub(pdu, 'HEADER-TYPE', 'SHORT-HEADER')
        return 'CONTAINER-I-PDU', path, signal_pdus

    def _find(self, path: str) -> ET.Element:
        """The element at an absolute path written before."""
        *packages, name = path.strip('/').split('/')
        siblings, package = self._packages, None
        for package_name in packages:
            package = siblings[package_name]
            siblings = package.children
        for element in package.elements:
            if element.findtext('SHORT-NAME') == name:
                return element
        raise KeyError(path)

    # --- Signals ---

    def _i_signal(self, signal: Signal, pdu_scope: str, bus_scope: str, message_name: str) -> str:
        key = (f'{bus_scope}/{pdu_scope}', id(signal))
        if key in self._signals:
            return self._signals[key]
        system_path, system_signal = self._element(
            'SYSTEM-SIGNAL',
            signal.name,
            'SystemSignals',
            bus_scope,
            pdu_scope,
            label=f'Signal {f"{message_name}.{signal.name}"!r}',
        )
        self._desc(system_signal, _comments(signal))
        self._sub(system_signal, 'DYNAMIC-LENGTH', 'false')
        compu_path = self._compu_method(signal, bus_scope, pdu_scope)
        unit_path = self._unit(signal.unit)
        if compu_path or unit_path:
            conditional = self._sub(
                self._sub(self._sub(system_signal, 'PHYSICAL-PROPS'), 'SW-DATA-DEF-PROPS-VARIANTS'),
                'SW-DATA-DEF-PROPS-CONDITIONAL',
            )
            if compu_path:
                self._ref(conditional, 'COMPU-METHOD-REF', 'COMPU-METHOD', compu_path)
            if unit_path:
                self._ref(conditional, 'UNIT-REF', 'UNIT', unit_path)

        path, i_signal = self._element('I-SIGNAL', system_path.rsplit('/', 1)[1], 'ISignals', bus_scope, pdu_scope)
        self._fibex.append(('I-SIGNAL', path))
        self._sub(i_signal, 'DATA-TYPE-POLICY', 'LEGACY')
        if signal.raw_initial is not None:
            self._sub(
                self._sub(self._sub(i_signal, 'INIT-VALUE'), 'NUMERICAL-VALUE-SPECIFICATION'),
                'VALUE',
                _number(signal.raw_initial),
            )
        self._sub(i_signal, 'LENGTH', signal.length)
        conditional = self._sub(
            self._sub(self._sub(i_signal, 'NETWORK-REPRESENTATION-PROPS'), 'SW-DATA-DEF-PROPS-VARIANTS'),
            'SW-DATA-DEF-PROPS-CONDITIONAL',
        )
        self._ref(conditional, 'BASE-TYPE-REF', 'SW-BASE-TYPE', self._base_type(signal))
        if signal.raw_invalid is not None:
            invalid = self._sub(self._sub(conditional, 'INVALID-VALUE'), 'NUMERICAL-VALUE-SPECIFICATION')
            self._sub(invalid, 'VALUE', _number(signal.raw_invalid))
        self._ref(i_signal, 'SYSTEM-SIGNAL-REF', 'SYSTEM-SIGNAL', system_path)
        self._signals[key] = path
        return path

    def _base_type(self, signal: Signal) -> str:
        key = (signal.length, bool(signal.is_signed), bool(signal.is_float))
        if key not in self._base_types:
            length, signed, is_float = key
            name = f'FLOAT{length}' if is_float else f'{"SINT" if signed else "UINT"}{length}'
            path, base_type = self._element('SW-BASE-TYPE', name, 'BaseTypes')
            self._sub(base_type, 'CATEGORY', 'FIXED_LENGTH')
            self._sub(base_type, 'BASE-TYPE-SIZE', length)
            self._sub(base_type, 'BASE-TYPE-ENCODING', 'IEEE754' if is_float else '2C' if signed else 'NONE')
            self._base_types[key] = path
        return self._base_types[key]

    def _unit(self, unit: Optional[str]) -> Optional[str]:
        if not unit:
            return None
        if unit not in self._units:
            path, element = self._element('UNIT', unit, 'Units')
            self._sub(element, 'DISPLAY-NAME', unit)
            self._units[unit] = path
        return self._units[unit]

    def _compu_method(self, signal: Signal, bus_scope: str, pdu_scope: str) -> Optional[str]:
        conversion = signal.conversion
        scale, offset = float(conversion.scale), float(conversion.offset)
        choices = signal.choices or {}
        limited = signal.minimum is not None or signal.maximum is not None
        linear = scale != 1 or offset != 0 or limited
        if choices and linear:
            category = 'SCALE_LINEAR_AND_TEXTTABLE'
        elif choices:
            category = 'TEXTTABLE'
        elif linear:
            category = 'LINEAR'
        else:
            category = 'IDENTICAL'
        if scale == 0:
            self.warnings.append(f'Signal {signal.name!r} has factor 0, written as identical')
            category, linear = ('TEXTTABLE' if choices else 'IDENTICAL'), False
        path, method = self._element('COMPU-METHOD', signal.name, 'CompuMethods', bus_scope, pdu_scope)
        self._sub(method, 'CATEGORY', category)
        unit_path = self._unit(signal.unit)
        if unit_path:
            self._ref(method, 'UNIT-REF', 'UNIT', unit_path)
        if category == 'IDENTICAL':
            return path
        scales = self._sub(self._sub(method, 'COMPU-INTERNAL-TO-PHYS'), 'COMPU-SCALES')
        if linear:
            compu_scale = self._sub(scales, 'COMPU-SCALE')
            low = None if signal.minimum is None else _raw(signal.minimum, scale, offset)
            high = None if signal.maximum is None else _raw(signal.maximum, scale, offset)
            if low is not None and high is not None and low > high:  # Negative factor
                low, high = high, low
            for tag, limit in (('LOWER-LIMIT', low), ('UPPER-LIMIT', high)):
                if limit is not None and math.isfinite(limit):
                    self._sub(compu_scale, tag, _number(limit), INTERVAL_TYPE='CLOSED')
            coefficients = self._sub(compu_scale, 'COMPU-RATIONAL-COEFFS')
            numerator = self._sub(coefficients, 'COMPU-NUMERATOR')
            self._sub(numerator, 'V', _number(offset))
            self._sub(numerator, 'V', _number(scale))
            self._sub(self._sub(coefficients, 'COMPU-DENOMINATOR'), 'V', '1')
        for value, text in choices.items():
            compu_scale = self._sub(scales, 'COMPU-SCALE')
            self._desc(compu_scale, dict(getattr(text, 'comments', None) or {}))
            self._sub(compu_scale, 'LOWER-LIMIT', _number(value), INTERVAL_TYPE='CLOSED')
            self._sub(compu_scale, 'UPPER-LIMIT', _number(value), INTERVAL_TYPE='CLOSED')
            self._sub(self._sub(compu_scale, 'COMPU-CONST'), 'VT', str(text))
        return path

    # --- ECUs, E2E, system ---

    def _write_ecus(self) -> None:
        group_paths: Dict[Tuple[str, str], str] = {}
        for (ecu, direction), pdus in self._pdu_groups.items():
            name = f'{ecu}_{"Tx" if direction == "OUT" else "Rx"}'
            path, group = self._element('I-SIGNAL-I-PDU-GROUP', name, 'PduGroups')
            self._fibex.append(('I-SIGNAL-I-PDU-GROUP', path))
            self._sub(group, 'COMMUNICATION-DIRECTION', direction)
            refs = self._sub(group, 'I-SIGNAL-I-PDUS')
            for pdu in pdus:
                conditional = self._sub(refs, 'I-SIGNAL-I-PDU-REF-CONDITIONAL')
                self._ref(conditional, 'I-SIGNAL-I-PDU-REF', 'I-SIGNAL-I-PDU', pdu)
            group_paths[(ecu, direction)] = path
        for ecu, comments in self._ecus.items():
            path, instance = self._element('ECU-INSTANCE', ecu, 'Ecus', label=f'Node {ecu!r}')
            self._fibex.append(('ECU-INSTANCE', path))
            self._desc(instance, comments)
            groups = [group_paths[(ecu, direction)] for direction in ('OUT', 'IN') if (ecu, direction) in group_paths]
            if groups:
                refs = self._sub(instance, 'ASSOCIATED-COM-I-PDU-GROUP-REFS')
                for group in groups:
                    self._ref(refs, 'ASSOCIATED-COM-I-PDU-GROUP-REF', 'I-SIGNAL-I-PDU-GROUP', group)

    def _write_e2e(self) -> None:
        if not self._e2e:
            return
        _, protection_set = self._element('END-TO-END-PROTECTION-SET', 'Protections', 'E2E')
        protections = self._sub(protection_set, 'END-TO-END-PROTECTIONS')
        names: set = set()
        for name, properties, pdu in self._e2e:
            protection = self._sub(protections, 'END-TO-END-PROTECTION')
            self._sub(protection, 'SHORT-NAME', self._unique(f'{short_name(name)}_E2E', names))
            profile = self._sub(protection, 'END-TO-END-PROFILE')
            if properties.category:
                self._sub(profile, 'CATEGORY', properties.category)
            if properties.data_ids:
                data_ids = self._sub(profile, 'DATA-IDS')
                for data_id in properties.data_ids:
                    self._sub(data_ids, 'DATA-ID', data_id)
            pdus = self._sub(protection, 'END-TO-END-PROTECTION-I-SIGNAL-I-PDUS')
            self._ref(
                self._sub(pdus, 'END-TO-END-PROTECTION-I-SIGNAL-I-PDU'), 'I-SIGNAL-I-PDU-REF', 'I-SIGNAL-I-PDU', pdu
            )

    def _write_system(self) -> None:
        _, system = self._element('SYSTEM', 'System', 'System')
        if self._header_byte_orders:
            orders = sorted(self._header_byte_orders, key=lambda order: -len(self._header_byte_orders[order]))
            if len(orders) > 1:
                self.warnings.append(
                    f'Container messages use different header byte orders; all written as {orders[0].replace("_", " ")} '
                    f'(an ARXML system has one): {", ".join(self._header_byte_orders[orders[1]])} change'
                )
            order = 'MOST-SIGNIFICANT-BYTE-LAST' if orders[0] == 'little_endian' else 'MOST-SIGNIFICANT-BYTE-FIRST'
            self._sub(system, 'CONTAINER-I-PDU-HEADER-BYTE-ORDER', order)
        if self._fibex:
            elements = self._sub(system, 'FIBEX-ELEMENTS')
            for dest, path in self._fibex:
                self._ref(self._sub(elements, 'FIBEX-ELEMENT-REF-CONDITIONAL'), 'FIBEX-ELEMENT-REF', dest, path)


def as_arxml_string(database: Database) -> Tuple[str, List[str]]:
    """The database as an ARXML document, and warnings about content ARXML (or this writer) cannot hold."""
    writer = _Writer(database)
    tree = writer.write()
    text = ET.tostring(tree.getroot(), encoding='unicode', xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{MARKER}\n{text}\n', writer.warnings


def written_by_canexplorer(path: str | Path) -> bool:
    """Whether an ARXML file was written by this module (and not changed by a tool since)."""
    try:
        with open(path, encoding='utf-8', errors='replace') as file:
            head = file.read(256)
    except OSError:
        return False
    lines = head.splitlines()
    return len(lines) > 1 and lines[1].strip() == MARKER


def save_arxml(database: Database, path: str | Path) -> List[str]:
    """Write the database to an ARXML file. Returns warnings about content that could not be written."""
    text, warnings = as_arxml_string(database)
    Path(path).write_text(text, encoding='utf-8')
    return warnings
