"""The logic behind the MCP interface: a wrapper around a BusSession that answers questions about a CAN bus and,
when allowed, sends frames. It has no Qt and no MCP dependency, so it is tested directly on the virtual bus and
reused by the MCP server in server.py.

Every method returns plain JSON-serialisable data (dicts, lists, str, int, float, bool, None).
"""

import asyncio
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import can

from can_explorer.core.database_editor import DatabaseEditor
from can_explorer.core.database_io import DatabaseFormatError, WRITE_SUFFIXES
from can_explorer.core.decoding import DbcDecoder, FrameDecoder, open_signal_database
from can_explorer.core.frames import parse_hex_bytes
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.obd import ECU_RESPONSE, FUNCTIONAL_REQUEST, ObdClient
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.reverse.analysis import analyse, collect
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.obd import PIDS_BY_NUMBER, is_obd_identifier, request as obd_request
from can_explorer.core.reverse.promote import signal_definitions, write_definitions
from can_explorer.core.reverse.reactions import capture_reaction
from can_explorer.core.reverse.reference import (
    Reference,
    candidates,
    correlate as correlate_fields,
    read_reference_csv,
    reference_from_signal,
)
from can_explorer.core.reverse.related import signal_groups
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.sources import BusConfig, LiveSource, MessageSource
from can_explorer.core.uds import UdsClient, UdsError

DEFAULT_UDS_REQUEST = 0x7E0  # Physical request to the first ECU; OBD uses the functional 0x7DF
NOT_CONNECTED = 'Not connected to a bus; use connect(interface, channel) first'

SCAN_FRAMES = 50_000  # Newest frames scanned when answering about recent frames or analysing an identifier
DEFAULT_FRAME_LIMIT = 50
ANALYSIS_FRAMES = 20_000


class SendNotAllowed(PermissionError):
    """Raised when a transmitting tool is used but the service was started read-only."""


def _parse_id(value: Any) -> int:
    """A CAN identifier from an int or a hex string ('1A0', '0x1A0')."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower().removeprefix('0x')
    try:
        return int(text, 16)
    except ValueError as e:
        raise ValueError(f'Invalid identifier {value!r}; use hex like 1A0 or 0x1A0') from e


def _id_text(arbitration_id: int, extended: bool) -> str:
    return f'{arbitration_id:08X}' if extended else f'{arbitration_id:03X}'


def _jsonable(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(' ').upper()
    return str(value)


class CanService:
    """A CAN bus for an agent to inspect and (when allowed) drive. The bus can be given up front or opened later
    with connect(), so the agent can list the interfaces and choose one itself. The database, protocol and
    permissions are service-level and persist across connections; each connection starts a fresh trace."""

    def __init__(
        self,
        source: Optional[MessageSource] = None,
        *,
        database: Optional[DbcDecoder] = None,
        protocol: str = 'Raw',
        allow_send: bool = False,
        allow_diagnostics: bool = False,
        allow_control: bool = False,
        buffer_size: int = 200_000,
    ):
        if protocol not in PROTOCOL_DECODERS:
            raise ValueError(f'Unknown protocol {protocol!r}; use one of {", ".join(PROTOCOL_DECODERS)}')
        self.protocol = protocol
        self._buffer_size = buffer_size
        self._decoder = FrameDecoder(create_protocol_decoder(protocol), database)
        self.session: Optional[BusSession] = (
            BusSession(source, self._decoder, buffer_size=buffer_size) if source is not None else None
        )
        # Permission tiers, each granting the one below: send (raw transmit) > control (active diagnostics) >
        # diagnostics (safe diagnostic reads) > read-only
        self.allow_send = allow_send
        self.allow_diagnostics = allow_diagnostics
        self.allow_control = allow_control
        self._manage = True  # False when attached to a session owned by someone else (the GUI)

    @classmethod
    def attach(
        cls,
        session: BusSession,
        *,
        allow_send: bool = False,
        allow_diagnostics: bool = False,
        allow_control: bool = False,
    ) -> 'CanService':
        """A service exposing an existing, externally-managed bus session (e.g. a GUI connection). The connect and
        disconnect tools are not offered, and stopping the service does not close the session — its owner does."""
        service = cls(allow_send=allow_send, allow_diagnostics=allow_diagnostics, allow_control=allow_control)
        service.session = session
        service._decoder = session.decoder
        service.protocol = getattr(session.decoder.protocol, 'name', None) or 'Raw'
        service._manage = False
        return service

    @classmethod
    def from_config(
        cls,
        interface: Optional[str] = None,
        channel: Optional[str] = None,
        *,
        bitrate: int = 500_000,
        fd: bool = False,
        data_bitrate: Optional[int] = None,
        database: Optional[str | Path] = None,
        protocol: str = 'Raw',
        allow_send: bool = False,
        allow_diagnostics: bool = False,
        allow_control: bool = False,
    ) -> 'CanService':
        """Build a service. With `interface` and `channel`, it opens that bus on start(); without them, it starts
        unconnected and the agent opens a bus with connect()."""
        decoder = open_signal_database(database) if database else None
        source = None
        if interface and channel:
            source = LiveSource(BusConfig(interface, channel, bitrate=bitrate, fd=fd, data_bitrate=data_bitrate))
        return cls(
            source,
            database=decoder,
            protocol=protocol,
            allow_send=allow_send,
            allow_diagnostics=allow_diagnostics,
            allow_control=allow_control,
        )

    # Permission tiers (a higher tier grants the ones below it)

    @property
    def can_control(self) -> bool:
        """Active diagnostics: clear DTCs, ECU reset, routine control, write DID, raw UDS."""
        return self.allow_control or self.allow_send

    @property
    def can_diagnose(self) -> bool:
        """Safe diagnostic reads: OBD values/DTCs/VIN and UDS reads (these still put a request on the bus)."""
        return self.allow_diagnostics or self.can_control

    def _require_send(self) -> None:
        if not self.allow_send:
            raise SendNotAllowed('Sending is disabled; start the server with --allow-send to enable it')

    def _require_diagnostics(self) -> None:
        if not self.can_diagnose:
            raise SendNotAllowed('Diagnostics are disabled; start with --allow-diagnostics (or --allow-send)')

    def _require_control(self) -> None:
        if not self.can_control:
            raise SendNotAllowed('Active control is disabled; start with --allow-control (or --allow-send)')

    # Lifecycle

    @property
    def connected(self) -> bool:
        return self.session is not None

    @property
    def manages_connection(self) -> bool:
        """True for a standalone service that owns its bus (connect/disconnect allowed); False when attached to a
        session owned elsewhere."""
        return self._manage

    def start(self) -> None:
        """Open the bus (if one was configured and owned) and start receiving. Needs a running event loop."""
        if self._manage and self.session is not None:
            self.session.start()

    def stop(self) -> None:
        if self._manage:
            self.disconnect()

    def connect(
        self,
        interface: str,
        channel: str,
        *,
        bitrate: int = 500_000,
        fd: bool = False,
        data_bitrate: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Open a bus (closing any current one) and start receiving. The database and protocol are kept; the
        trace starts fresh. Returns the new status, or an error."""
        self.disconnect()
        config = BusConfig(interface, channel, bitrate=bitrate, fd=fd, data_bitrate=data_bitrate)
        session = BusSession(LiveSource(config), self._decoder, buffer_size=self._buffer_size)
        try:
            session.start()
        except Exception as e:  # Could not open the bus (bad channel, interface not available, …)
            return {'connected': False, 'error': str(e)}
        self.session = session
        return self.status()

    def disconnect(self) -> Dict[str, Any]:
        """Close the current bus, if any."""
        if self.session is not None:
            self.session.stop()
            self.session = None
        return {'connected': False}

    @property
    def _database(self) -> Optional[DbcDecoder]:
        return self._decoder.database

    # Status

    def status(self) -> Dict[str, Any]:
        """The bus state, frame counts and rates (or that no bus is connected)."""
        database = self._database
        common = {
            'connected': self.connected,
            'protocol': self.protocol,
            'database': str(database.path) if database is not None else None,
            'sending_allowed': self.allow_send,
            'capabilities': {'diagnostics': self.can_diagnose, 'control': self.can_control, 'send': self.allow_send},
        }
        if self.session is None:
            return {
                **common,
                'state': 'disconnected',
                'open': False,
                'frames': 0,
                'identifiers': 0,
                'hint': 'Not connected. Use list_interfaces to see the interfaces, then connect(interface, channel).',
            }
        snapshot = self.session.sample_bus_statistics()
        return {
            **common,
            'state': self.session.state.value,
            'open': self.session.is_open,
            'description': self.session.source.description,
            'frames': self.session.trace.total,
            'identifiers': len(self.session.statistics.entries),
            'rx_per_s': round(snapshot.rx_rate, 1),
            'tx_per_s': round(snapshot.tx_rate, 1),
            'bus_load_percent': None if snapshot.load is None else round(snapshot.load * 100, 1),
            'error_frames': snapshot.error_frames,
        }

    # Identifiers

    def identifiers(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Every identifier seen, with its count, cycle time, last data and database name."""
        if self.session is None:
            return []
        database = self._database
        rows = []
        for (arbitration_id, extended, rx), entry in self.session.statistics.entries.items():
            message = entry.last.message
            definition = database.message_definition(message) if database is not None else None
            rows.append(
                {
                    'id': _id_text(arbitration_id, extended),
                    'extended': extended,
                    'direction': 'rx' if rx else 'tx',
                    'name': definition.name if definition is not None else None,
                    'count': entry.count,
                    'cycle_ms': round(entry.cycle_time * 1000, 2) if entry.cycle_time is not None else None,
                    'dlc': message.dlc,
                    'data': bytes(message.data).hex(' ').upper(),
                    '_sort': (extended, arbitration_id),
                }
            )
        rows.sort(key=lambda row: row.pop('_sort'))
        return rows[:limit] if limit else rows

    # Frames

    def _recent(self, identifier: Optional[int], extended: Optional[bool], limit: int) -> List[can.Message]:
        if self.session is None:
            return []
        trace = self.session.trace
        start = max(trace.first, trace.total - SCAN_FRAMES)
        kept: deque = deque(maxlen=limit)
        for frame in trace.iter_from(start):
            message = frame.message
            if identifier is not None and (
                message.arbitration_id != identifier or message.is_extended_id != bool(extended)
            ):
                continue
            kept.append(frame)
        return list(kept)

    def frames(
        self,
        identifier: Optional[Any] = None,
        extended: bool = False,
        limit: int = DEFAULT_FRAME_LIMIT,
        decode: bool = True,
    ) -> List[Dict[str, Any]]:
        """The most recent frames (optionally of one identifier), newest last, decoded with the database."""
        identifier_value = None if identifier is None else _parse_id(identifier)
        database = self._database
        start_time = self.session.start_time or 0.0
        rows = []
        for frame in self._recent(identifier_value, extended, max(1, min(limit, 1000))):
            message = frame.message
            row: Dict[str, Any] = {
                't': round(message.timestamp - start_time, 4),
                'id': _id_text(message.arbitration_id, message.is_extended_id),
                'extended': message.is_extended_id,
                'rx': bool(message.is_rx),
                'dlc': message.dlc,
                'data': bytes(message.data).hex(' ').upper(),
                'info': frame.info or None,
            }
            if decode and database is not None:
                values = database.decode(message)
                if values:
                    row['signals'] = {name: _jsonable(value) for name, value in values.items()}
            rows.append(row)
        return rows

    def decode_frame(self, identifier: Any, data: str, extended: bool = False) -> Dict[str, Any]:
        """Decode a frame's data with the loaded database (without sending anything)."""
        database = self._database
        if database is None:
            return {'error': 'No database loaded; use load_database first'}
        message = can.Message(
            arbitration_id=_parse_id(identifier),
            is_extended_id=extended,
            data=parse_hex_bytes(data),
            check=False,
        )
        definition = database.message_definition(message)
        values = database.decode(message)
        return {
            'id': _id_text(message.arbitration_id, extended),
            'name': definition.name if definition is not None else None,
            'description': database.describe(message),
            'signals': {name: _jsonable(value) for name, value in values.items()} if values else None,
        }

    # Database

    def load_database(self, path: str) -> Dict[str, Any]:
        """Load a signal database (DBC, ARXML, KCD, SYM, DBF) for decoding."""
        database = open_signal_database(path)
        self._decoder.database = database
        return {
            'database': str(database.path),
            'messages': len(database.messages()),
            'signals': len(database.signals()),
        }

    def unload_database(self) -> Dict[str, Any]:
        """Forget the loaded database, so frames are shown as raw bytes again."""
        had = self._database is not None
        self._decoder.database = None
        return {'database': None, 'unloaded': had}

    # Reverse engineering

    @staticmethod
    def _field(found) -> Dict[str, Any]:
        field: BitField = found.field
        condition = None
        if found.multiplexer is not None:
            condition = f'{found.multiplexer.key}={found.mux_value}'
        return {
            'field': field.key,
            'kind': found.kind,
            'confidence': round(found.confidence, 2),
            'details': found.details,
            'start': field.start,
            'length': field.length,
            'byte_order': field.byte_order,
            'signed': field.signed,
            'condition': condition,
        }

    def analyze(self, identifier: Any, extended: bool = False, include_constant: bool = False) -> Dict[str, Any]:
        """Run the automatic field analysis on the recent frames of one identifier (checksums, counters,
        multiplexers, physical values, states and flags)."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        key: Tuple[int, bool] = (_parse_id(identifier), bool(extended))
        messages = self._recent(key[0], key[1], ANALYSIS_FRAMES)
        frames = collect(frame.message for frame in messages)
        if key not in frames:
            return {'id': _id_text(*key), 'frames': 0, 'fields': [], 'note': 'No frames for this identifier'}
        result = analyse(frames[key])
        fields = [self._field(found) for found in result.fields if include_constant or found.kind != 'constant']
        return {
            'id': _id_text(*key),
            'frames': result.frames,
            'length': result.length,
            'note': result.note or None,
            'multiplexer': self._field(result.multiplexer) if result.multiplexer is not None else None,
            'fields': fields,
        }

    def _analyses(self, identifier: Optional[Any], extended: bool) -> List[Any]:
        """Analyse the recent frames of one identifier, or of every identifier when none is given."""
        wanted = None if identifier is None else (_parse_id(identifier), bool(extended))
        frames = collect(frame.message for frame in self._recent(*(wanted or (None, None)), ANALYSIS_FRAMES))
        if wanted is not None:
            return [analyse(frames[wanted])] if wanted in frames else []
        return [analyse(frames[key]) for key in frames]

    def correlate(
        self,
        signal: Optional[str] = None,
        csv_path: Optional[str] = None,
        times: Optional[List[float]] = None,
        values: Optional[List[float]] = None,
        name: str = 'reference',
        unit: str = '',
        column: int = 0,
        identifier: Optional[Any] = None,
        extended: bool = False,
        max_lag: float = 1.0,
        top: int = 10,
    ) -> Dict[str, Any]:
        """Rank the fields of the bus by how well they follow a reference series — a database signal
        ("Message.Signal"), a CSV column, or an inline times/values pair — with the fitted factor, offset and lag."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        if signal:
            database = self._database
            if database is None:
                return {'error': 'Load a database first to use a signal as the reference'}
            message_name, _, signal_name = signal.partition('.')
            if not signal_name:
                return {'error': f'Use "Message.Signal", got {signal!r}'}
            try:
                reference = reference_from_signal(database, messages, message_name, signal_name)
            except (KeyError, ValueError) as e:
                return {'error': f'No such signal: {e}'}
        elif csv_path:
            try:
                references = read_reference_csv(csv_path, self.session.start_time or 0.0)
            except (OSError, ValueError) as e:
                return {'error': str(e)}
            if not 0 <= column < len(references):
                return {'error': f'CSV column {column} out of range (0..{len(references) - 1})'}
            reference = references[column]
        elif times and values:
            if len(times) != len(values):
                return {'error': 'times and values must have the same length'}
            reference = Reference(name, unit, [float(t) for t in times], [float(v) for v in values])
        else:
            return {'error': 'Provide a reference: signal, csv_path, or times and values'}

        frames = collect(
            message for message in messages if not is_obd_identifier((message.arbitration_id, message.is_extended_id))
        )
        if identifier is not None:
            key = (_parse_id(identifier), bool(extended))
            frames = {key: frames[key]} if key in frames else {}
        # Include the analysis's fields (odd-width, Motorola and multiplexed) next to the byte-aligned candidates
        analyses = [analyse(idframes) for idframes in frames.values()]
        candidate_fields = candidates(frames, analyses)
        matches = correlate_fields(reference, frames, candidate_fields, max_lag=max(0.0, max_lag), top=max(1, top))
        return {
            'reference': reference.name,
            'unit': reference.unit,
            'samples': len(reference),
            'matches': [
                {
                    'id': match.field.id_text,
                    'field': match.field.key,
                    'correlation': round(match.correlation, 4),
                    'scale': match.field.scale,
                    'offset': match.field.offset,
                    'lag_ms': round(match.lag * 1000, 1),
                    'samples': match.samples,
                    'condition': f'{match.multiplexer.key}={match.mux_value}' if match.multiplexer else None,
                }
                for match in matches
            ],
        }

    def promote_to_dbc(
        self,
        path: str,
        identifier: Optional[Any] = None,
        extended: bool = False,
        include_constant: bool = False,
        min_confidence: float = 0.0,
    ) -> Dict[str, Any]:
        """Write the fields found by the analysis (of one identifier, or all) as signals to a DBC, creating their
        messages. An existing database at `path` is extended; multiplexing is recorded in the signal comment."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        target = Path(path)
        if target.suffix.lower() not in WRITE_SUFFIXES:
            return {
                'error': f'Cannot write {target.suffix or "a file without extension"}; use {", ".join(WRITE_SUFFIXES)}'
            }
        definitions = signal_definitions(self._analyses(identifier, extended), include_constant, min_confidence)
        if not definitions:
            return {'error': 'No signals found to promote'}
        try:
            editor = DatabaseEditor(target) if target.exists() else DatabaseEditor()
        except (DatabaseFormatError, OSError, ValueError) as e:
            return {'error': f'Could not open {target.name}: {e}'}
        counts = write_definitions(editor, definitions)
        try:
            warnings = editor.save(target)
        except (DatabaseFormatError, OSError) as e:
            return {'error': f'Could not save {target.name}: {e}'}
        return {'path': str(editor.path), **counts, 'warnings': warnings}

    # Transmitting (only when allowed)

    def send_frame(self, identifier: Any, data: str, extended: bool = False, fd: bool = False) -> Dict[str, Any]:
        """Send a single CAN frame (requires --allow-send)."""
        self._require_send()
        if self.session is None:
            return {'sent': False, 'error': NOT_CONNECTED}
        payload = parse_hex_bytes(data)
        message = can.Message(
            arbitration_id=_parse_id(identifier),
            is_extended_id=extended,
            is_fd=fd,
            bitrate_switch=fd,
            data=payload,
            check=False,
        )
        try:
            self.session.send(message)
        except SessionError as e:
            return {'sent': False, 'error': str(e)}
        return {'sent': True, 'id': _id_text(message.arbitration_id, extended), 'data': payload.hex(' ').upper()}

    async def query_obd(self, pid: int, timeout: float = 1.0) -> Dict[str, Any]:
        """Request one OBD-II value (service 01) and return the ECU's answer (a single-frame request, no ISO-TP)."""
        self._require_diagnostics()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        pid_number = int(pid)
        definition = PIDS_BY_NUMBER.get(pid_number)
        if definition is None:
            raise ValueError(f'Unknown PID {pid_number:#04x}')
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def listener(message: can.Message) -> None:
            if future.done() or not message.is_rx or message.is_error_frame or message.is_remote_frame:
                return
            if not is_obd_identifier((message.arbitration_id, message.is_extended_id)):
                return
            data = bytes(message.data)
            if len(data) >= 3 + definition.length and data[1] == 0x41 and data[2] == pid_number:
                future.set_result(data)

        self.session.add_message_listener(listener)
        try:
            self.session.send(obd_request(pid_number))
            data = await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, SessionError) as e:
            return {'pid': pid_number, 'name': definition.name, 'answered': False, 'error': str(e) or 'timeout'}
        finally:
            self.session.remove_message_listener(listener)
        value = definition.decode(data[3 : 3 + definition.length])
        return {
            'pid': pid_number,
            'name': definition.name,
            'value': _jsonable(float(value)),
            'unit': definition.unit,
            'answered': True,
        }

    def send_message(self, name: str, signals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Encode a database message by name from its signal values and send it (requires --allow-send)."""
        self._require_send()
        if self.session is None:
            return {'sent': False, 'error': NOT_CONNECTED}
        database = self._database
        if database is None:
            return {'error': 'Load a database first to send a message by name'}
        definition = next((message for message in database.messages() if message.name == name), None)
        if definition is None:
            return {'error': f'No message named {name!r} in the database'}
        try:
            data = definition.encode(signals or {}, strict=False, padding=True)
        except Exception as e:
            return {'error': f'Could not encode {name}: {e}'}
        message = can.Message(
            arbitration_id=definition.frame_id,
            is_extended_id=definition.is_extended_frame,
            is_fd=definition.length > 8,
            data=data,
            check=False,
        )
        try:
            self.session.send(message)
        except SessionError as e:
            return {'sent': False, 'error': str(e)}
        return {
            'sent': True,
            'name': name,
            'id': _id_text(definition.frame_id, definition.is_extended_frame),
            'data': bytes(data).hex(' ').upper(),
        }

    # Protocol decoding and more reverse engineering

    def decode_protocol(self, protocol: str, identifier: Any, data: str, extended: bool = False) -> Dict[str, Any]:
        """Interpret a frame with a protocol decoder (J1939, NMEA 2000, CANopen, ISO-TP, UDS, LIN, J1587), without
        a database. Sends nothing."""
        if protocol not in PROTOCOL_DECODERS:
            return {'error': f'Unknown protocol {protocol!r}; use one of {", ".join(PROTOCOL_DECODERS)}'}
        decoder = create_protocol_decoder(protocol)
        if decoder is None:
            return {'error': "The 'Raw' protocol has no description; choose another"}
        message = can.Message(
            arbitration_id=_parse_id(identifier), is_extended_id=extended, data=parse_hex_bytes(data), check=False
        )
        return {
            'id': _id_text(message.arbitration_id, extended),
            'protocol': protocol,
            'description': decoder.describe(message),
        }

    def timing(self, identifier: Optional[Any] = None, extended: bool = False) -> List[Dict[str, Any]]:
        """The inter-frame timing of each identifier (or one): mean cycle time, jitter, and whether it is periodic
        or event-triggered, from the recent frames."""
        wanted = None if identifier is None else (_parse_id(identifier), bool(extended))
        times: Dict[Tuple[int, bool], List[float]] = {}
        for frame in self._recent(*(wanted or (None, None)), SCAN_FRAMES):
            message = frame.message
            if message.is_error_frame or message.is_remote_frame:
                continue
            times.setdefault((message.arbitration_id, message.is_extended_id), []).append(message.timestamp)
        rows = []
        for key, stamps in times.items():
            if len(stamps) < 2:
                rows.append({'id': _id_text(*key), 'count': len(stamps), 'cycle_ms': None, 'pattern': 'single'})
                continue
            deltas = [(stamps[i] - stamps[i - 1]) * 1000 for i in range(1, len(stamps))]
            mean = sum(deltas) / len(deltas)
            low, high = min(deltas), max(deltas)
            periodic = mean > 0 and (high - low) < 0.25 * mean
            rows.append(
                {
                    'id': _id_text(*key),
                    'count': len(stamps),
                    'cycle_ms': round(mean, 2),
                    'min_ms': round(low, 2),
                    'max_ms': round(high, 2),
                    'jitter_ms': round(high - low, 2),
                    'pattern': 'periodic' if periodic else 'event',
                }
            )
        rows.sort(key=lambda row: (row['cycle_ms'] is None, row['cycle_ms'] or 0.0))
        return rows

    def related_signals(self, top: int = 20, min_correlation: float = 0.99) -> Dict[str, Any]:
        """Groups of fields that carry the same signal across the bus (the same quantity rebroadcast, or a scaled
        or lagged copy), found without a database."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        frames = collect(m for m in messages if not is_obd_identifier((m.arbitration_id, m.is_extended_id)))
        analyses = [analyse(idframes) for idframes in frames.values()]
        groups = signal_groups(frames, analyses, min_correlation=min_correlation, top=max(1, top))
        return {
            'groups': [
                {
                    'correlation': round(group.correlation, 4),
                    'members': [
                        {
                            'field': member.field.key,
                            'representative': member.representative,
                            'scale': member.field.scale,
                            'offset': member.field.offset,
                            'lag_ms': round(member.lag * 1000, 1),
                            'condition': (
                                f'{member.multiplexer.key}={member.mux_value}' if member.multiplexer else None
                            ),
                        }
                        for member in group.members
                    ],
                }
                for group in groups
            ]
        }

    def capture_reaction(self, baseline_seconds: float = 5.0, action_seconds: float = 5.0) -> Dict[str, Any]:
        """Diff the most recent `action_seconds` of traffic against the `baseline_seconds` before it, to find what
        changed on the bus (identifiers that appeared, and bits that were stable then changed). Passive: reads only.
        Record a steady baseline, perform the action, wait `action_seconds`, then call this."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, SCAN_FRAMES)]
        if not messages:
            return {'error': 'No frames captured yet'}
        end = messages[-1].timestamp
        action_start = end - max(0.1, action_seconds)
        baseline_start = action_start - max(0.1, baseline_seconds)
        baseline = [m for m in messages if baseline_start <= m.timestamp < action_start]
        action = [m for m in messages if action_start <= m.timestamp <= end]
        report = capture_reaction(baseline, action)
        return {
            'baseline_frames': report.baseline_frames,
            'action_frames': report.action_frames,
            'reactions': [
                {
                    'id': reaction.id_text,
                    'appeared': reaction.appeared,
                    'fields': [
                        {
                            'field': item.field.key,
                            'before': item.before,
                            'after': item.after,
                            'flips': item.action_flips,
                        }
                        for item in reaction.fields
                    ],
                }
                for reaction in report.reactions
            ],
        }

    @staticmethod
    def interfaces() -> List[Dict[str, Any]]:
        """The CAN interfaces python-can can detect on this machine (for choosing a channel to connect to)."""
        try:
            configs = can.detect_available_configs()
        except Exception:
            return []
        return [{'interface': config.get('interface'), 'channel': str(config.get('channel'))} for config in configs]

    # Diagnostics (OBD-II and UDS, over ISO-TP)

    def _isotp(self, request_id: Any, response_id: Any) -> IsoTpClient:
        config = getattr(self.session.source, 'config', None)
        return IsoTpClient(
            self.session, _parse_id(request_id), _parse_id(response_id), fd=bool(getattr(config, 'fd', False))
        )

    async def _with_uds(self, request_id, response_id, operation, control: bool = False) -> Dict[str, Any]:
        (self._require_control if control else self._require_diagnostics)()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        transport = self._isotp(request_id, response_id)
        try:
            return await operation(UdsClient(transport))
        except UdsError as e:
            return {'error': str(e)}
        except (OSError, ValueError) as e:
            return {'error': str(e)}
        finally:
            transport.close()

    async def obd_status(self, request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE) -> Dict[str, Any]:
        """The MIL ("check engine") state and the number of stored trouble codes (OBD service 01 PID 01)."""

        async def operation(uds):
            status = await ObdClient(uds).status()
            return {'mil_on': status.mil_on, 'dtc_count': status.dtc_count}

        return await self._with_uds(request, response, operation)

    async def obd_dtcs(
        self, pending: bool = False, request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """The stored (service 03) or pending (service 07) OBD-II trouble codes, decoded to P/C/B/U codes."""

        async def operation(uds):
            dtcs = await ObdClient(uds).read_dtcs(pending=pending)
            return {'pending': pending, 'dtcs': [dtc.code for dtc in dtcs]}

        return await self._with_uds(request, response, operation)

    async def obd_vin(self, request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE) -> Dict[str, Any]:
        """The vehicle identification number (OBD service 09 PID 02)."""

        async def operation(uds):
            return {'vin': await ObdClient(uds).read_vin()}

        return await self._with_uds(request, response, operation)

    async def obd_supported(self, request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE) -> Dict[str, Any]:
        """The OBD-II service-01 PIDs the ECU supports, of those this tool can decode."""

        async def operation(uds):
            pids = await ObdClient(uds).supported_pids()
            known = [
                {'pid': pid, 'name': PIDS_BY_NUMBER[pid].name, 'unit': PIDS_BY_NUMBER[pid].unit}
                for pid in pids
                if pid in PIDS_BY_NUMBER
            ]
            return {'supported_pids': [f'{pid:02X}' for pid in pids], 'known': known}

        return await self._with_uds(request, response, operation)

    async def obd_read(
        self, pids: List[int], request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """Read and decode OBD-II live values (service 01), e.g. pids [12, 13] for engine and vehicle speed."""

        async def operation(uds):
            client = ObdClient(uds)
            values = []
            for pid in pids:
                try:
                    value = await client.read_value(int(pid))
                except UdsError as e:
                    values.append({'pid': int(pid), 'error': str(e)})
                    continue
                if value is None:
                    values.append({'pid': int(pid), 'error': 'unknown PID or no answer'})
                else:
                    values.append(
                        {'pid': value.pid, 'name': value.name, 'value': _jsonable(value.value), 'unit': value.unit}
                    )
            return {'values': values}

        return await self._with_uds(request, response, operation)

    async def obd_clear(self, request: Any = FUNCTIONAL_REQUEST, response: Any = ECU_RESPONSE) -> Dict[str, Any]:
        """Clear the OBD-II trouble codes and turn the MIL off (service 04). Active: requires --allow-control."""

        async def operation(uds):
            await ObdClient(uds).clear_dtcs()
            return {'cleared': True}

        return await self._with_uds(request, response, operation, control=True)

    async def uds_read_did(
        self, did: Any, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """Read a UDS data identifier (service 0x22), e.g. did "F190" for the VIN. Returns the raw bytes and text."""
        did_value = _parse_id(did)

        async def operation(uds):
            data = await uds.read_data_by_identifier(did_value)
            text = bytes(byte for byte in data if 0x20 <= byte < 0x7F).decode('ascii', 'replace')
            return {'did': f'{did_value:04X}', 'data': bytes(data).hex(' ').upper(), 'text': text}

        return await self._with_uds(request, response, operation)

    async def uds_read_dtcs(
        self, status_mask: int = 0xFF, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """Read UDS trouble codes (service 0x19 reportDTCByStatusMask)."""

        async def operation(uds):
            _, dtcs = await uds.read_dtcs(int(status_mask))
            return {
                'dtcs': [{'code': dtc.text, 'status': f'0x{dtc.status:02X}', 'flags': dtc.status_flags} for dtc in dtcs]
            }

        return await self._with_uds(request, response, operation)

    async def uds_reset(
        self, reset_type: int = 1, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """Reset an ECU (service 0x11; 1 = hard reset). Active: requires --allow-control."""

        async def operation(uds):
            await uds.ecu_reset(int(reset_type))
            return {'reset': True, 'type': int(reset_type)}

        return await self._with_uds(request, response, operation, control=True)

    async def uds_routine(
        self,
        control_type: int,
        routine: Any,
        data: str = '',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
    ) -> Dict[str, Any]:
        """Routine control (service 0x31; control_type 1 = start, 2 = stop, 3 = request results). Active:
        requires --allow-control."""
        routine_value = _parse_id(routine)
        payload = parse_hex_bytes(data) if data else b''

        async def operation(uds):
            result = await uds.routine_control(int(control_type), routine_value, payload)
            return {'routine': f'{routine_value:04X}', 'response': bytes(result).hex(' ').upper()}

        return await self._with_uds(request, response, operation, control=True)

    async def uds_request(
        self, payload: str, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """Send a raw UDS request (hex, e.g. "22 F1 90") and return the response. Active: requires --allow-control
        because a raw request can do anything (reset, routines, writes)."""
        bytes_payload = parse_hex_bytes(payload)

        async def operation(uds):
            result = await uds.request(bytes_payload)
            return {
                'request': bytes_payload.hex(' ').upper(),
                'response': bytes(result.response).hex(' ').upper() if result.response else None,
                'error': result.error or None,
            }

        return await self._with_uds(request, response, operation, control=True)
