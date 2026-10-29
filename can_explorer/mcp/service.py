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
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.reverse.analysis import analyse, collect
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.obd import PIDS_BY_NUMBER, is_obd_identifier, request as obd_request
from can_explorer.core.reverse.promote import signal_definitions, write_definitions
from can_explorer.core.reverse.reference import (
    Reference,
    candidates,
    correlate as correlate_fields,
    read_reference_csv,
    reference_from_signal,
)
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.sources import BusConfig, LiveSource, MessageSource

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
    """One CAN bus, opened once and kept running; the tools query the trace and statistics it accumulates."""

    def __init__(
        self,
        source: MessageSource,
        *,
        database: Optional[DbcDecoder] = None,
        protocol: str = 'Raw',
        allow_send: bool = False,
        buffer_size: int = 200_000,
    ):
        if protocol not in PROTOCOL_DECODERS:
            raise ValueError(f'Unknown protocol {protocol!r}; use one of {", ".join(PROTOCOL_DECODERS)}')
        decoder = FrameDecoder(create_protocol_decoder(protocol), database)
        self.session = BusSession(source, decoder, buffer_size=buffer_size)
        self.protocol = protocol
        self.allow_send = allow_send

    @classmethod
    def from_config(
        cls,
        interface: str,
        channel: str,
        *,
        bitrate: int = 500_000,
        fd: bool = False,
        data_bitrate: Optional[int] = None,
        database: Optional[str | Path] = None,
        protocol: str = 'Raw',
        allow_send: bool = False,
    ) -> 'CanService':
        decoder = open_signal_database(database) if database else None
        config = BusConfig(interface, channel, bitrate=bitrate, fd=fd, data_bitrate=data_bitrate)
        return cls(LiveSource(config), database=decoder, protocol=protocol, allow_send=allow_send)

    # Lifecycle

    def start(self) -> None:
        """Open the bus and start receiving. Must be called with a running event loop."""
        self.session.start()

    def stop(self) -> None:
        self.session.stop()

    @property
    def _database(self) -> Optional[DbcDecoder]:
        return self.session.decoder.database

    # Status

    def status(self) -> Dict[str, Any]:
        """The bus state, frame counts and rates."""
        snapshot = self.session.sample_bus_statistics()
        database = self._database
        return {
            'state': self.session.state.value,
            'open': self.session.is_open,
            'description': self.session.source.description,
            'protocol': self.protocol,
            'database': str(database.path) if database is not None else None,
            'sending_allowed': self.allow_send,
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
        self.session.decoder.database = database
        return {
            'database': str(database.path),
            'messages': len(database.messages()),
            'signals': len(database.signals()),
        }

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

    def _require_send(self) -> None:
        if not self.allow_send:
            raise SendNotAllowed('Sending is disabled; start the server with --allow-send to enable it')

    def send_frame(self, identifier: Any, data: str, extended: bool = False, fd: bool = False) -> Dict[str, Any]:
        """Send a single CAN frame (requires --allow-send)."""
        self._require_send()
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
        """Request one OBD-II value (service 01) and return the ECU's answer (requires --allow-send)."""
        self._require_send()
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
