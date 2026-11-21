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

from can_explorer.core.channel_detection import configs_of, detect_channels
from can_explorer.core.database_editor import DatabaseEditor
from can_explorer.core.database_io import DatabaseFormatError, WRITE_SUFFIXES
from can_explorer.core.decoding import DbcDecoder, FrameDecoder, open_signal_database
from can_explorer.core.frames import parse_hex_bytes
from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.kwp2000_client import KwpClient, KwpError
from can_explorer.core.coding import CodingError, CodingRecord, write_record
from can_explorer.core.diagnostic_labels import LabelFile
from can_explorer.core.measuring_blocks import MeasuringBlockError, read_measuring_block
from can_explorer.core.obd import ECU_RESPONSE, FUNCTIONAL_REQUEST, ObdClient
from can_explorer.core.protocols import PROTOCOL_DECODERS, create_protocol_decoder
from can_explorer.core.reverse.analysis import analyse, collect
from can_explorer.core.reverse.bitstats import BitStatistics
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.markers import Marker, find_marker_changes
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
from can_explorer.core.secoc_check import KeyStoreSecOcKeys, SecOcVerifier
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.calibration_session import CalibrationSession, a2l_path
from can_explorer.core.sources import BusConfig, LiveSource, MessageSource
from can_explorer.core.tp20 import Tp20Client, Tp20Error
from can_explorer.core.tp20_scan import SETUP_TIMEOUT, parse_addresses, scan_modules
from can_explorer.core.uds import UdsClient, UdsError, load_key_source
from can_explorer.sys.log_handler import get_logger

DEFAULT_UDS_REQUEST = 0x7E0  # Physical request to the first ECU; OBD uses the functional 0x7DF
NOT_CONNECTED = 'Not connected to a bus; use connect(interface, channel) first'

SCAN_FRAMES = 50_000  # Newest frames scanned when answering about recent frames or analysing an identifier
DEFAULT_FRAME_LIMIT = 50
ANALYSIS_FRAMES = 20_000

logger = get_logger(__name__)


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


def _parse_isotp_id(value: Any) -> Tuple[int, Optional[int]]:
    """A CAN identifier with an optional ISO-TP address byte ('6F1:12', extended or mixed addressing)."""
    if isinstance(value, int):
        return value, None
    can_id, colon, address = str(value).partition(':')
    if not colon:
        return _parse_id(can_id), None
    try:
        byte = int(address.strip(), 16)
    except ValueError:
        raise ValueError(f'Invalid address byte {address.strip()!r}; use hex like 6F1:12') from None
    if not 0 <= byte <= 0xFF:
        raise ValueError(f'Address byte out of range: 0x{byte:X}')
    return _parse_id(can_id), byte


def _parse_j1939_address(value: Any) -> int:
    """A J1939 address from an int or hex ('F9'); FF is the global address."""
    if isinstance(value, int):
        address = value
    else:
        try:
            address = int(str(value).strip().lower().removeprefix('0x'), 16)
        except ValueError:
            raise ValueError(f'Invalid J1939 address {value!r}; use hex like F9') from None
    if not 0 <= address <= 0xFF:
        raise ValueError(f'J1939 address out of range: {address:#x}')
    return address


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


def _field_definition(spec: Dict[str, Any]) -> Dict[str, Any]:
    """An explicit field spec {identifier, start, length, byte_order?, …} as a promote.write_definitions entry,
    validating the bit range through BitField."""
    frame_id = _parse_id(spec['identifier'] if 'identifier' in spec else spec['id'])
    extended = bool(spec.get('extended', False))
    byte_order = spec.get('byte_order', 'little_endian')
    signed, scale, offset = (
        bool(spec.get('signed', False)),
        float(spec.get('scale', 1.0)),
        float(spec.get('offset', 0.0)),
    )
    field = BitField(frame_id, extended, int(spec['start']), int(spec['length']), byte_order, signed, scale, offset)
    return {
        'frame_id': frame_id,
        'extended': extended,
        'frame_length': int(spec.get('frame_length', field.min_length)),
        'name': spec.get('name') or f'Signal_{field.id_text}_{field.start}',
        'start': field.start,
        'length': field.length,
        'byte_order': byte_order,
        'is_signed': signed,
        'scale': scale,
        'offset': offset,
        'unit': spec.get('unit', ''),
        'comment': spec.get('comment', ''),
    }


def _downsample(values: List[float], count: int = 200) -> List[float]:
    """At most `count` evenly spaced values from `values`, keeping the first and last (so a series stays compact
    in a tool result without losing its shape)."""
    if len(values) <= count:
        return list(values)
    step = (len(values) - 1) / (count - 1)
    return [values[round(index * step)] for index in range(count)]


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
        self._decoder = FrameDecoder(create_protocol_decoder(protocol), database, self._secoc_verifier())
        self.session: Optional[BusSession] = (
            BusSession(source, self._decoder, buffer_size=buffer_size) if source is not None else None
        )
        # Permission tiers, each granting the one below: send (raw transmit) > control (active diagnostics) >
        # diagnostics (safe diagnostic reads) > read-only
        self.allow_send = allow_send
        self.allow_diagnostics = allow_diagnostics
        self.allow_control = allow_control
        self._manage = True  # False when attached to a session owned by someone else (the GUI)
        self._markers: List[Marker] = []  # Moments the agent marked, for reaction_at_markers
        self._flexray = None  # The FlexRay cluster description (a FlexRayDatabase), for the flexray_* tools
        self._sdt_counters: Dict[int, int] = {}  # SecuredDataTransmission anti-replay counter per request id
        # The session key context of the last Authentication (0x29) per request id, with its credentials source
        self._auth_sessions: Dict[int, Tuple[Any, str]] = {}
        self._xcp = CalibrationSession()  # The XCP/CCP slave of the xcp_* tools, kept open between calls

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
        self._xcp.close()
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

    # FlexRay (files only: the cluster description and capture or log files)

    def flexray_load_cluster(self, path: str, cluster: Optional[str] = None) -> Dict[str, Any]:
        """Load a FlexRay cluster description (FIBEX or ARXML) for the other flexray_* tools."""
        from can_explorer.core.flexray import FlexRayError
        from can_explorer.core.flexray_database import load_database

        try:
            database = load_database(path, cluster)
        except FlexRayError as e:
            raise ValueError(str(e)) from e
        self._flexray = database
        return {
            'file': str(database.path),
            'cluster': database.cluster,
            'clusters': database.clusters,
            'parameters': dict(database.parameters.rows()),
            'controllers': sorted(database.controllers),
            'frames': [
                {
                    'name': d.name,
                    'slot': d.slot,
                    'cycles': d.cycles,
                    'channels': '+'.join(sorted(d.channels)),
                    'length': d.length,
                    'pdus': [
                        {
                            'name': pdu.name,
                            'offset': pdu.offset,
                            'length': pdu.length,
                            'update_bit': pdu.update_bit,
                            'signals': pdu.signal_names,
                        }
                        for pdu in d.pdus
                    ],
                }
                for d in database.definitions
            ],
        }

    def _flexray_frame(self, frame, start: float, decode: bool) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            'time': round(frame.timestamp - start, 6),
            'channel': frame.channel,
            'slot': frame.slot,
            'cycle': frame.cycle,
            'data': frame.payload.hex(' ').upper(),
        }
        for flag in ('null_frame', 'sync', 'startup', 'transmitted'):
            if getattr(frame, flag):
                row[flag] = True
        if frame.errors:
            row['errors'] = frame.errors
        if decode and self._flexray is not None:
            definition, pdus = self._flexray.decode_pdus(frame)
            if definition is not None:
                row['name'] = definition.name
                row['pdus'] = {
                    entry['pdu'].name: entry['values'] if entry['updated'] else 'not updated' for entry in pdus
                }
        return row

    def flexray_read_log(
        self, path: str, slot: Optional[int] = None, channel: Optional[str] = None, limit: int = 50, decode: bool = True
    ) -> Dict[str, Any]:
        """The FlexRay frames (first `limit`, optionally of one slot or channel) and events of a pcap, BLF or ASC
        file, decoded with the loaded cluster description."""
        from can_explorer.core.flexray import FlexRayError, read_log

        try:
            log = read_log(path)
        except FlexRayError as e:
            raise ValueError(str(e)) from e
        frames = [
            f for f in log.frames if (slot is None or f.slot == slot) and (channel is None or f.channel == channel)
        ]
        start = log.frames[0].timestamp if log.frames else 0.0
        return {
            'format': log.format,
            'frames_in_file': len(log.frames),
            'matching': len(frames),
            'skipped_records': log.skipped,
            'frames': [self._flexray_frame(frame, start, decode) for frame in frames[: max(0, limit)]],
            'events': [
                {'time': round(e.timestamp - start, 6), 'kind': e.kind, 'channel': e.channel, 'text': e.text}
                for e in log.events[: max(0, limit)]
            ],
        }

    def flexray_analyze_log(self, path: str) -> Dict[str, Any]:
        """Check a log's frames against the loaded cluster's schedule and report the bus health."""
        from can_explorer.core.flexray import FlexRayError, read_log
        from can_explorer.core.flexray_analysis import analyse

        try:
            log = read_log(path)
        except FlexRayError as e:
            raise ValueError(str(e)) from e
        report = analyse(log.frames, self._flexray, log.events)
        report['described'] = self._flexray is not None
        return report

    def flexray_decode(self, slot: int, cycle: int, data: str, channel: str = 'A') -> Dict[str, Any]:
        """Decode a FlexRay payload of a slot and cycle with the loaded cluster description."""
        from can_explorer.core.flexray import FlexRayError, FlexRayFrame

        if self._flexray is None:
            raise ValueError('Load the FlexRay cluster description first (flexray_load_cluster)')
        try:
            frame = FlexRayFrame(0.0, channel.upper(), slot, cycle, parse_hex_bytes(data))
        except FlexRayError as e:
            raise ValueError(str(e)) from e
        row = self._flexray_frame(frame, 0.0, True)
        if 'name' not in row:
            row['name'] = None
            row['note'] = 'The cluster description has no frame in this slot, channel and cycle'
        return row

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

    @staticmethod
    def _secoc_verifier() -> Optional[SecOcVerifier]:
        """SecOC checks with the user's key store, unlocked with CANEXPLORER_KEYSTORE_PASSWORD (else secured PDUs
        are reported as not checked: the key store is locked)."""
        from can_explorer.core.keystore import KeyStore, KeyStoreError

        store = KeyStore()
        try:
            store.unlock_from_environment()
        except KeyStoreError as e:
            logger.warning(f'Key store: {e}')
        return SecOcVerifier(KeyStoreSecOcKeys(store))

    def secoc_status(self) -> Dict[str, Any]:
        """The SecOC secured PDUs of the loaded ARXML database: Data ID, the key store entry that verifies them (its
        name only; keys are never returned) or why they are not checked, and the outcome of the checks so far."""
        database = self._database
        configs = database.secoc_protected() if hasattr(database, 'secoc_protected') else []
        verifier = self._decoder.secoc
        pdus = []
        for config in configs:
            key, reason = (None, 'no security manager') if verifier is None else verifier.keys.key_for(config)
            problem = config.problem or ('' if key is not None else reason)
            statistics = verifier.statistics.get(config.pdu) if verifier is not None else None
            pdus.append(
                {
                    'pdu': config.pdu,
                    'data_id': config.data_id,
                    'checked': not problem,
                    'key': key.entry if key is not None else None,
                    'reason': problem,
                    'freshness_bits': config.freshness_bits,
                    'freshness_tx_bits': config.freshness_tx_bits,
                    'mac_bits': config.auth_tx_bits,
                    'algorithm': config.auth_algorithm,
                    **(statistics.as_dict() if statistics is not None else {}),
                }
            )
        return {'secured_pdus': pdus}

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
        result = analyse(frames[key], secoc=self._secured().get(key))
        fields = [self._field(found) for found in result.fields if include_constant or found.kind != 'constant']
        return {
            'id': _id_text(*key),
            'frames': result.frames,
            'length': result.length,
            'note': result.note or None,
            'multiplexer': self._field(result.multiplexer) if result.multiplexer is not None else None,
            'fields': fields,
        }

    def _secured(self) -> Dict[Tuple[int, bool], Any]:
        """The SecOC configuration of the database's secured PDUs (an ARXML), so the analysis labels their
        freshness value and MAC."""
        return self._decoder.secured_pdus if self._decoder is not None else {}

    def _analyses(self, identifier: Optional[Any], extended: bool) -> List[Any]:
        """Analyse the recent frames of one identifier, or of every identifier when none is given."""
        wanted = None if identifier is None else (_parse_id(identifier), bool(extended))
        frames = collect(frame.message for frame in self._recent(*(wanted or (None, None)), ANALYSIS_FRAMES))
        if wanted is not None:
            return [analyse(frames[wanted], secoc=self._secured().get(wanted))] if wanted in frames else []
        return [analyse(frames[key], secoc=self._secured().get(key)) for key in frames]

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
        analyses = [analyse(idframes, secoc=self._secured().get(idframes.key)) for idframes in frames.values()]
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
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Write signals to a DBC, creating their messages (an existing database at `path` is extended). Without
        `fields`, the fields the analysis found (of one identifier, or all) are written. With `fields`, exactly the
        given field specs are written — a hypothesis you formed yourself: each is {identifier, start, length,
        byte_order?, signed?, scale?, offset?, name?, unit?, comment?, extended?}, matching extract_field."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        target = Path(path)
        if target.suffix.lower() not in WRITE_SUFFIXES:
            return {
                'error': f'Cannot write {target.suffix or "a file without extension"}; use {", ".join(WRITE_SUFFIXES)}'
            }
        if fields:
            try:
                definitions = [_field_definition(spec) for spec in fields]
            except (KeyError, ValueError, TypeError) as e:
                return {'error': f'Invalid field spec: {e}'}
        else:
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
        """Interpret a frame with a protocol decoder (J1939, J1939-22, NMEA 2000, CANopen, ISO-TP, UDS, KWP2000, LIN, J1587, NM, SecOC), without
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

    def timing(
        self, identifier: Optional[Any] = None, extended: bool = False, series: bool = False
    ) -> List[Dict[str, Any]]:
        """The inter-frame timing of each identifier (or one): mean cycle time, jitter, and whether it is periodic
        or event-triggered, from the recent frames. With `series`, each row also carries a downsampled
        `intervals_ms` list of the inter-frame gaps over time, to spot bursts, gateways and event triggers."""
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
            row = {
                'id': _id_text(*key),
                'count': len(stamps),
                'cycle_ms': round(mean, 2),
                'min_ms': round(low, 2),
                'max_ms': round(high, 2),
                'jitter_ms': round(high - low, 2),
                'pattern': 'periodic' if periodic else 'event',
            }
            if series:
                row['intervals_ms'] = [round(delta, 2) for delta in _downsample(deltas)]
            rows.append(row)
        rows.sort(key=lambda row: (row['cycle_ms'] is None, row['cycle_ms'] or 0.0))
        return rows

    def related_signals(self, top: int = 20, min_correlation: float = 0.99) -> Dict[str, Any]:
        """Groups of fields that carry the same signal across the bus (the same quantity rebroadcast, or a scaled
        or lagged copy), found without a database."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        frames = collect(m for m in messages if not is_obd_identifier((m.arbitration_id, m.is_extended_id)))
        analyses = [analyse(idframes, secoc=self._secured().get(idframes.key)) for idframes in frames.values()]
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

    def bit_activity(self, identifier: Any, extended: bool = False) -> Dict[str, Any]:
        """Per-bit behaviour of one identifier over the recent frames: for each bit, how often it is set and how
        often it flips (so constant, toggling and counter-like bits stand out before fields are grouped), and a
        per-byte summary (distinct values, range, entropy). The raw view behind analyze_identifier."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        key = (_parse_id(identifier), bool(extended))
        messages = self._recent(key[0], key[1], ANALYSIS_FRAMES)
        statistics = BitStatistics()
        statistics.update_all(message.message for message in messages)
        identifier_stats = statistics.get(key)
        if identifier_stats is None or not messages:
            return {'id': _id_text(*key), 'frames': 0, 'bits': [], 'bytes': [], 'note': 'No frames for this identifier'}
        bits = [
            {
                'bit': bit.bit,
                'value': bit.value,
                'ones': round(bit.ones, 3),
                'flips': round(bit.flips, 4),
                'constant': bit.constant,
            }
            for bit in identifier_stats.bits()
        ]
        bytes_summary = [
            {
                'byte': info.index,
                'value': info.value,
                'distinct': info.distinct,
                'min': info.minimum,
                'max': info.maximum,
                'entropy_bits': round(info.entropy, 2),
            }
            for info in identifier_stats.bytes()
        ]
        return {
            'id': _id_text(*key),
            'frames': len(messages),
            'length': identifier_stats.length,
            'bits': bits,
            'bytes': bytes_summary,
        }

    def assess_frame_security(self, identifier: Any, extended: bool = False) -> Dict[str, Any]:
        """Run a passive security assessment on the recent frames of one identifier: randomness tests on trailing
        bytes, freshness/replay analysis, linear dependency search and keystream reuse detection."""
        from can_explorer.core.reverse.crypto import (
            analyse_freshness,
            classify_mac,
            detect_keystream_reuse,
            test_randomness,
        )
        from can_explorer.core.reverse.linear import find_linear_relations

        if self.session is None:
            return {'error': NOT_CONNECTED}
        key: Tuple[int, bool] = (_parse_id(identifier), bool(extended))
        messages = self._recent(key[0], key[1], ANALYSIS_FRAMES)
        frames = collect(frame.message for frame in messages)
        if key not in frames:
            return {'id': _id_text(*key), 'frames': 0, 'assessment': {}, 'note': 'No frames for this identifier'}

        id_frames = frames[key]
        payloads = id_frames.payloads
        timestamps = id_frames.times
        n = len(payloads)

        if n < 20:
            return {
                'id': _id_text(*key),
                'frames': n,
                'assessment': {},
                'note': f'Too few frames ({n}); need at least 20',
            }

        length = len(payloads[0]) if payloads else 0
        assessment: Dict[str, Any] = {}

        # 1. Run the automatic analysis to find fields
        result = analyse(id_frames, secoc=self._secured().get(key))
        fields_info = [self._field(found) for found in result.fields if found.kind != 'constant']
        assessment['fields'] = fields_info

        # 2. Randomness tests on trailing bytes (last 2+ bytes that look random)
        if length >= 3:
            randomness = test_randomness(payloads, (max(0, length - 4), length - 1))
            assessment['randomness'] = {
                'byte_range': list(randomness.byte_range),
                'monobit_p': randomness.monobit_p,
                'runs_p': randomness.runs_p,
                'chi_square_p': randomness.chi_square_p,
                'serial_correlation': randomness.serial_correlation,
                'counter_correlation': randomness.counter_correlation,
                'verdict': randomness.verdict,
                'confidence': randomness.confidence,
            }

        # 3. Freshness analysis (if a counter and trailing random bytes are found)
        counter_field = None
        mac_range = None
        for found in result.fields:
            if found.kind == 'counter' and counter_field is None:
                counter_field = found.field
            if found.kind in ('random', 'mac'):
                first_byte = min(found.field.bits) // 8
                last_byte = max(found.field.bits) // 8
                mac_range = (first_byte, last_byte)

        if counter_field is not None and mac_range is not None:
            freshness = analyse_freshness(payloads, timestamps, counter_field, mac_range)
            assessment['freshness'] = {
                'has_freshness': freshness.has_freshness,
                'counter_wraps': freshness.counter_wraps,
                'mac_repeats_on_wrap': freshness.mac_repeats_on_wrap,
                'mac_bits': freshness.mac_bits,
                'forgery_probability': freshness.forgery_probability,
                'frames_per_second': freshness.frames_per_second,
                'expected_forgery_time': freshness.expected_forgery_time,
                'replay_possible': freshness.replay_possible,
                'findings': freshness.findings,
            }

        # 4. MAC classification (if a suspected MAC range is found)
        if mac_range is not None:
            mac_class = classify_mac(payloads, mac_range, has_counter=counter_field is not None)
            assessment['mac_classification'] = {
                'byte_range': list(mac_range),
                'mac_bits': mac_class.mac_bits,
                'likely_algorithm': mac_class.likely_algorithm,
                'truncated': mac_class.truncated,
                'findings': mac_class.findings,
            }

        # 5. SecOC configuration cross-check
        secoc_config = self._secured().get(key)
        if secoc_config is not None:
            assessment['secoc'] = {
                'configured': True,
                'note': 'This identifier has a SecOC configuration from the loaded ARXML',
            }

        # 6. Linear dependency search
        linear = find_linear_relations(payloads)
        if linear:
            assessment['linear_relations'] = [
                {
                    'output_bits': rel.output_bits[:8],  # Truncate for readability
                    'algorithm': rel.algorithm,
                    'polynomial': f'0x{rel.polynomial:X}' if rel.polynomial else None,
                    'description': rel.description,
                    'confidence': rel.confidence,
                }
                for rel in linear
            ]

        # 7. Keystream reuse detection (if a counter is found)
        if counter_field is not None and length >= 3:
            enc_first = 0
            enc_last = length - 1
            # Exclude the counter bytes from the encrypted range
            counter_bytes = {b // 8 for b in counter_field.bits}
            enc_first = min(b for b in range(length) if b not in counter_bytes)
            enc_last = max(b for b in range(length) if b not in counter_bytes)
            reuse = detect_keystream_reuse(payloads, counter_field, (enc_first, enc_last))
            assessment['keystream_reuse'] = {
                'reused_counter_values': reuse.reused_counter_values[:20],
                'structured_xor_pairs': reuse.structured_xor_pairs,
                'findings': reuse.findings,
            }

        return {
            'id': _id_text(*key),
            'frames': n,
            'length': length,
            'assessment': assessment,
        }

    def extract_field(
        self,
        identifier: Any,
        start: int,
        length: int,
        byte_order: str = 'little_endian',
        signed: bool = False,
        scale: float = 1.0,
        offset: float = 0.0,
        extended: bool = False,
        max_samples: int = 200,
    ) -> Dict[str, Any]:
        """Decode a candidate field (a bit range with a byte order, sign and scaling) over the recent frames,
        without a database, to test a hypothesis. Returns the value range, how often it changes, and a downsampled
        value series over time. `start` is the least significant bit (little endian) or most significant bit (big
        endian); `byte_order` is 'little_endian' (Intel) or 'big_endian' (Motorola)."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        key = (_parse_id(identifier), bool(extended))
        try:
            field = BitField(
                key[0], key[1], int(start), int(length), byte_order, bool(signed), float(scale), float(offset)
            )
        except ValueError as e:
            return {'error': str(e)}
        messages = self._recent(key[0], key[1], ANALYSIS_FRAMES)
        start_time = self.session.start_time or 0.0
        samples = []
        for frame in messages:
            value = field.value(bytes(frame.message.data))
            if value is not None:
                samples.append((round(frame.message.timestamp - start_time, 4), value))
        if not samples:
            return {'id': _id_text(*key), 'field': field.key, 'frames': 0, 'note': 'No frames decode this field'}
        values = [value for _time, value in samples]
        changes = sum(1 for index in range(1, len(values)) if values[index] != values[index - 1])
        indices = (
            [
                round(index * (len(samples) - 1) / (min(max_samples, len(samples)) - 1))
                for index in range(min(max_samples, len(samples)))
            ]
            if len(samples) > 1
            else [0]
        )
        return {
            'id': _id_text(*key),
            'field': field.key,
            'frames': len(samples),
            'min': _jsonable(min(values)),
            'max': _jsonable(max(values)),
            'distinct': len(set(values)),
            'changes': changes,
            'series': [{'t': samples[i][0], 'value': _jsonable(samples[i][1])} for i in indices],
        }

    def mark(self, label: str = '') -> Dict[str, Any]:
        """Mark the current moment on the bus (the time of the newest frame), to line up with an action you do —
        press a button, open a door. Mark the same action a few times, then call reaction_at_markers to find the
        identifiers and bits that react at those moments."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        recent = self._recent(None, None, 1)
        time = recent[-1].message.timestamp if recent else 0.0
        self._markers.append(Marker(time, label))
        start_time = self.session.start_time or 0.0
        return {
            'marked': True,
            'count': len(self._markers),
            'markers': [{'t': round(m.time - start_time, 4), 'label': m.label} for m in self._markers],
        }

    def clear_markers(self) -> Dict[str, Any]:
        """Forget all the markers set with mark()."""
        self._markers = []
        return {'cleared': True, 'count': 0}

    def reaction_at_markers(self, before: float = 0.5, after: float = 1.5, top: int = 30) -> Dict[str, Any]:
        """The fields that change around the moments marked with mark() — most specific first — with the share of
        markers they hit and their background change rate, to tell a real reaction from coincidence. Passive: reads
        only. Mark one action several times for a confident result."""
        if self.session is None:
            return {'error': NOT_CONNECTED}
        if not self._markers:
            return {'error': 'No markers set; call mark() at the moments of an action first'}
        frames = collect(frame.message for frame in self._recent(None, None, SCAN_FRAMES))
        matches = find_marker_changes(frames, self._markers, before=before, after=after, top=max(1, top))
        return {
            'markers': len(self._markers),
            'reactions': [
                {
                    'field': match.field.key,
                    'hits': match.hits,
                    'of_markers': match.markers,
                    'background_per_s': round(match.background, 3),
                    'score': round(match.score, 3),
                    'transitions': list(match.transitions),
                }
                for match in matches
            ],
        }

    @staticmethod
    def interfaces() -> List[Dict[str, Any]]:
        """The CAN interfaces python-can can detect on this machine (for choosing a channel to connect to)."""
        # Each backend on its own: one failing backend no longer hides the channels of all the others
        configs = configs_of(detect_channels())
        return [{'interface': config.get('interface'), 'channel': str(config.get('channel'))} for config in configs]

    @staticmethod
    def crack_seed_key(
        pairs: List[Dict[str, str]], level: int = 1, candidate_keys: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Recover the seed/key algorithm from captured UDS 0x27 SecurityAccess pairs."""
        from can_explorer.core.seedkey_crack import crack

        try:
            decoded = [(bytes.fromhex(p['seed']), bytes.fromhex(p['key'])) for p in pairs]
        except (KeyError, ValueError) as e:
            return {'error': f'Invalid pair: {e}. Each pair must be {{seed: hex, key: hex}}.'}
        keys = None
        if candidate_keys:
            try:
                keys = [bytes.fromhex(k) for k in candidate_keys]
            except ValueError as e:
                return {'error': f'Invalid candidate key: {e}. Provide 16-byte hex keys.'}
        report = crack(decoded, level, candidate_keys=keys)
        return {
            'level': report.level,
            'pairs': len(report.pairs),
            'matches': [
                {
                    'algorithm': m.algorithm,
                    'description': m.description,
                    'parameters': m.parameters,
                    'pairs_tested': m.pairs_tested,
                    'pairs_confirmed': m.pairs_confirmed,
                }
                for m in report.matches
            ],
            'summary': report.describe(),
        }

    def analyse_gateway_auth(self) -> Dict[str, Any]:
        """Passive analysis of UDS traffic for gateway authentication patterns."""
        from can_explorer.core.reverse.gateway_security import analyse_gateway_auth

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        patterns = analyse_gateway_auth(messages)
        return {
            'ecus': len(patterns),
            'patterns': [
                {
                    'request_id': f'{p.request_id:03X}',
                    'response_id': f'{p.response_id:03X}',
                    'auth_type': p.auth_type,
                    'session_type': p.session_type,
                    'security_level': p.security_level,
                    'locked_services': [f'0x{s:02X}' for s in p.locked_services],
                    'unlocked_services': [f'0x{s:02X}' for s in p.unlocked_services],
                    'details': p.details,
                }
                for p in patterns
            ],
        }

    def analyse_gateway_filtering(self) -> Dict[str, Any]:
        """Passive analysis of UDS traffic for gateway filtering weaknesses."""
        from can_explorer.core.reverse.gateway_security import analyse_gateway_filtering

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        findings = analyse_gateway_filtering(messages)
        return {
            'findings': len(findings),
            'results': [
                {
                    'finding_type': f.finding_type,
                    'description': f.description,
                    'evidence': f.evidence,
                    'severity': f.severity,
                }
                for f in findings
            ],
        }

    @staticmethod
    def correlate_ecu_seeds(seed_key_pairs: Dict[str, List[Dict[str, str]]]) -> Dict[str, Any]:
        """Check whether different ECUs share the same seed/key algorithm or master key."""
        from can_explorer.core.reverse.gateway_security import correlate_ecu_seeds

        try:
            decoded: Dict[int, List[tuple]] = {}
            for ecu_hex, pairs in seed_key_pairs.items():
                ecu_id = int(ecu_hex.strip().lower().removeprefix('0x'), 16)
                decoded[ecu_id] = [(bytes.fromhex(p['seed']), bytes.fromhex(p['key'])) for p in pairs]
        except (KeyError, ValueError) as e:
            return {'error': f'Invalid input: {e}'}
        correlations = correlate_ecu_seeds(decoded)
        return {
            'ecus': len(decoded),
            'correlations': [
                {
                    'ecu_pairs': [(f'0x{a:03X}', f'0x{b:03X}') for a, b in c.ecu_pairs],
                    'finding': c.finding,
                    'details': c.details,
                }
                for c in correlations
            ],
        }

    # --- Items 1-8: new gateway analysis tools ---

    def map_gateway(self) -> Dict[str, Any]:
        """Build a topology map of ECUs from captured UDS traffic."""
        from can_explorer.core.reverse.gateway_security import map_gateway

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        gw_map = map_gateway(messages)
        return {
            'ecus': [
                {
                    'request_id': f'0x{e.request_id:03X}',
                    'response_id': f'0x{e.response_id:03X}',
                    'supported_sessions': e.supported_sessions,
                    'auth_type': e.auth_type,
                    'services_seen': [f'0x{s:02X}' for s in e.services_seen],
                    'mean_response_ms': e.mean_response_ms,
                }
                for e in gw_map.ecus
            ],
            'segments': gw_map.segments,
            'diagnostic_ids': [f'0x{i:03X}' for i in gw_map.diagnostic_ids],
            'functional_ids_seen': [f'0x{i:03X}' for i in gw_map.functional_ids_seen],
        }

    def access_matrix(self) -> Dict[str, Any]:
        """Build a per-ECU, per-service, per-session security classification."""
        from can_explorer.core.reverse.gateway_security import build_access_matrix

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        matrix = build_access_matrix(messages)
        return {
            'ecus': [f'0x{e:03X}' for e in matrix.ecus],
            'sessions': matrix.sessions,
            'services': [f'0x{s:02X}' for s in matrix.services],
            'rows': [
                {
                    'ecu_id': f'0x{r.ecu_id:03X}',
                    'service': f'0x{r.service:02X}',
                    'service_name': r.service_name,
                    'session': r.session,
                    'auth_required': r.auth_required,
                    'security_level': r.security_level,
                    'status': r.status,
                }
                for r in matrix.rows
            ],
        }

    def extract_unlock_sequence(self, ecu_id: Any) -> Dict[str, Any]:
        """Extract the authentication sequence for one ECU."""
        from can_explorer.core.reverse.gateway_security import extract_unlock_sequence

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        seq = extract_unlock_sequence(messages, eid)
        if seq is None:
            return {'ecu_id': f'0x{eid:03X}', 'found': False, 'note': 'No authentication flow observed'}
        return {
            'ecu_id': f'0x{eid:03X}',
            'found': True,
            'auth_type': seq.auth_type,
            'total_time_ms': seq.total_time_ms,
            'steps': [
                {
                    'action': s.action,
                    'sid': f'0x{s.sid:02X}',
                    'sub_function': s.sub_function,
                    'request': s.request_data,
                    'response': s.response_data,
                    'timing_constraint_ms': s.timing_constraint_ms,
                }
                for s in seq.steps
            ],
        }

    def gateway_full_report(self) -> Dict[str, Any]:
        """Run all gateway analyses and produce a combined report."""
        from can_explorer.core.reverse.gateway_security import gateway_report

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        report = gateway_report(messages)
        return {
            'ecus': len(report.gateway_map.ecus),
            'auth_patterns': len(report.auth_patterns),
            'bypass_findings': len(report.bypass_findings),
            'correlations': len(report.correlations),
            'unlock_sequences': len(report.unlock_sequences),
            'severity_summary': report.severity_summary,
            'segments': report.gateway_map.segments,
            'access_rows': len(report.access_matrix.rows),
        }

    def classify_did_security(self, ecu_id: Any) -> Dict[str, Any]:
        """Classify the security posture of DIDs and routines for one ECU."""
        from can_explorer.core.reverse.gateway_security import classify_did_security

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        dids = classify_did_security(messages, eid)
        return {
            'ecu_id': f'0x{eid:03X}',
            'identifiers': [
                {
                    'id': f'0x{d.identifier:04X}',
                    'service': f'0x{d.service:02X}',
                    'service_name': d.service_name,
                    'readable': d.readable,
                    'writable': d.writable,
                    'auth_required': d.auth_required,
                    'nrc': f'0x{d.nrc:02X}' if d.nrc is not None else None,
                }
                for d in dids
            ],
        }

    def detect_bus_segments(self) -> Dict[str, Any]:
        """Detect physical bus segments from CAN ID timing characteristics."""
        from can_explorer.core.reverse.gateway_security import detect_bus_segments

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        segments = detect_bus_segments(messages)
        return {
            'segments': [
                {
                    'name': s.name,
                    'ids': [f'0x{i:03X}' for i in s.ids],
                    'id_range': [f'0x{s.id_range[0]:03X}', f'0x{s.id_range[1]:03X}'],
                    'mean_rate_hz': s.mean_rate_hz,
                    'periodic_ratio': s.periodic_ratio,
                    'event_driven_ids': [f'0x{i:03X}' for i in s.event_driven_ids],
                }
                for s in segments
            ],
            'count': len(segments),
        }

    def analyse_reset_behaviour(self) -> Dict[str, Any]:
        """Detect services that succeed shortly after an ECU reset."""
        from can_explorer.core.reverse.gateway_security import analyse_reset_behaviour

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        findings = analyse_reset_behaviour(messages)
        return {
            'findings': [
                {
                    'ecu_id': f'0x{f.ecu_id:03X}',
                    'service': f'0x{f.service:02X}',
                    'service_name': f.service_name,
                    'session': f.session,
                    'success_time_after_reset_ms': f.success_time_after_reset_ms,
                    'finding': f.finding,
                    'severity': f.severity,
                }
                for f in findings
            ],
            'count': len(findings),
        }

    def export_gateway_report(self, fmt: str = 'json') -> Dict[str, Any]:
        """Export the full gateway report as JSON or CSV."""
        from can_explorer.core.reverse.gateway_security import export_report, gateway_report

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        report = gateway_report(messages)
        try:
            output = export_report(report, fmt=fmt)
        except ValueError as e:
            return {'error': str(e)}
        return {'format': fmt, 'content': output}

    @staticmethod
    def assess_seed_quality(seeds: List[str]) -> Dict[str, Any]:
        """Assess the quality of seeds from an ECU's 0x27 SecurityAccess."""
        from can_explorer.core.seedkey_crack import assess_seed_quality

        try:
            decoded = [bytes.fromhex(s) for s in seeds]
        except ValueError as e:
            return {'error': f'Invalid seed hex: {e}'}
        report = assess_seed_quality(decoded)
        return {
            'length': report.length,
            'entropy_bits': report.entropy_bits,
            'unique_ratio': report.unique_ratio,
            'sequential_pattern': report.sequential_pattern,
            'predictable': report.predictable,
            'findings': report.findings,
        }

    def oem_token_providers(self) -> Dict[str, Any]:
        """The registered OEM right-to-repair token providers: the legitimate path to diagnostic access through a
        vehicle security gateway. Lists each provider's name and OEM; returns no secrets."""
        from can_explorer.core import oem_tokens

        registry = oem_tokens.registry()
        registry.load_entry_points()
        return {
            'providers': [
                {'name': loaded.provider.name, 'oem': getattr(loaded.provider, 'oem', ''), 'source': loaded.source}
                for loaded in registry.providers
            ]
        }

    def request_oem_token(self, provider: str, credentials: str, vin: str = '', make: str = '') -> Dict[str, Any]:
        """Request a diagnostic access token for a vehicle from an OEM right-to-repair portal through a registered
        provider, using the shop login stored in the key store entry `credentials`. Returns the token's metadata
        only (its secret value is never returned); `gateway_unlock` reports whether the provider knows how to
        present it to that OEM's gateway."""
        from can_explorer.core import oem_tokens

        registry = oem_tokens.registry()
        registry.load_entry_points()
        try:
            chosen = registry.get(provider)
        except oem_tokens.OemTokenError as e:
            return {'error': str(e)}
        store = self._auth_store()
        if not store.unlocked:
            return {'error': 'Unlock the key store to use OEM credentials'}
        try:
            secrets = store.secrets(credentials)
        except Exception as e:  # noqa: BLE001 - surface any key store error to the caller
            return {'error': f'Credentials {credentials!r}: {e}'}
        try:
            request = oem_tokens.TokenRequest(vin=vin, make=make)
            token = chosen.request_token(request, secrets)
        except oem_tokens.OemTokenError as e:
            return {'error': str(e)}
        unlock = chosen.gateway_unlock(token)
        return {
            'oem': token.oem,
            'kind': token.kind,
            'scope': token.scope,
            'expires_at': token.expires_at,
            'expired': token.expired(),
            'gateway_unlock': None
            if unlock is None
            else {'service': f'0x{unlock.service:02X}', 'description': unlock.description},
        }

    def fingerprint_ecu(self, ecu_id: Any) -> Dict[str, Any]:
        """Build an NRC pattern and timing fingerprint for one ECU."""
        from can_explorer.core.reverse.gateway_security import fingerprint_ecu

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        fp = fingerprint_ecu(messages, eid)
        return {
            'request_id': f'0x{fp.request_id:03X}',
            'response_id': f'0x{fp.response_id:03X}',
            'nrc_set': [f'0x{n:02X}' for n in fp.nrc_set],
            'nrc_counts': {f'0x{k:02X}': v for k, v in fp.nrc_counts.items()},
            'supported_sessions': fp.supported_sessions,
            'timing_profile': fp.timing_profile,
            'vendor_hints': fp.vendor_hints,
        }

    def analyse_auth_timing(self) -> Dict[str, Any]:
        """Measure the timing of each step of authentication flows for all ECUs."""
        from can_explorer.core.reverse.gateway_security import analyse_auth_timing

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        results = analyse_auth_timing(messages)
        return {
            'ecus': len(results),
            'results': [
                {
                    'ecu_id': f'0x{r.ecu_id:03X}',
                    'auth_type': r.auth_type,
                    'request_to_seed_ms': r.request_to_seed_ms,
                    'seed_to_key_ms': r.seed_to_key_ms,
                    'key_to_response_ms': r.key_to_response_ms,
                    'total_ms': r.total_ms,
                    'jitter_ms': r.jitter_ms,
                    'findings': r.findings,
                }
                for r in results
            ],
        }

    def plan_probing(self, priority: str = 'coverage') -> Dict[str, Any]:
        """Suggest which services to try next to maximize attack-surface coverage."""
        from can_explorer.core.reverse.gateway_security import build_access_matrix, plan_probing

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        matrix = build_access_matrix(messages)
        steps = plan_probing(matrix, priority=priority)
        return {
            'steps': [
                {
                    'ecu_id': f'0x{s.ecu_id:03X}',
                    'service': f'0x{s.service:02X}',
                    'service_name': s.service_name,
                    'session': s.session,
                    'auth_level': s.auth_level,
                    'rationale': s.rationale,
                    'priority': s.priority,
                }
                for s in steps
            ],
            'count': len(steps),
        }

    def auth_state_machine(self, ecu_id: Any) -> Dict[str, Any]:
        """Reconstruct the authentication state machine for one ECU."""
        from can_explorer.core.reverse.gateway_security import recover_auth_state_machine

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        sm = recover_auth_state_machine(messages, eid)
        if sm is None:
            return {'ecu_id': f'0x{eid:03X}', 'found': False, 'note': 'No authentication flow observed'}
        return {
            'ecu_id': f'0x{sm.ecu_id:03X}',
            'found': True,
            'states': [
                {
                    'name': s.name,
                    'session': s.session,
                    'security_level': s.security_level,
                    'services_available': [f'0x{svc:02X}' for svc in s.services_available],
                }
                for s in sm.states
            ],
            'transitions': [
                {
                    'from_state': t.from_state,
                    'to_state': t.to_state,
                    'action': t.action,
                    'sid': f'0x{t.sid:02X}',
                    'sub_function': t.sub_function,
                    'timing_constraint_ms': t.timing_constraint_ms,
                    'mandatory': t.mandatory,
                }
                for t in sm.transitions
            ],
            'initial_state': sm.initial_state,
            'tester_present_required': sm.tester_present_required,
            'tester_present_interval_ms': sm.tester_present_interval_ms,
            'total_unlock_time_ms': sm.total_unlock_time_ms,
            'findings': sm.findings,
        }

    def differential_analysis(self, captures: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """Compare two or more captures to find security-relevant changes."""
        from can_explorer.core.reverse.gateway_security import differential_analysis

        # Convert dicts back to can.Messages
        capture_messages: List[List[can.Message]] = []
        for cap in captures:
            msgs = []
            for entry in cap:
                arb_id = _parse_id(entry.get('id', '0'))
                data = bytes.fromhex(entry.get('data', ''))
                ts = float(entry.get('timestamp', 0.0))
                msgs.append(can.Message(arbitration_id=arb_id, data=data, timestamp=ts, is_extended_id=False))
            capture_messages.append(msgs)

        result = differential_analysis(capture_messages)
        return {
            'capture_count': result.capture_count,
            'changes': [
                {
                    'identifier': f'0x{c.identifier:03X}',
                    'change_type': c.change_type,
                    'description': c.description,
                    'auth_correlated': c.auth_correlated,
                    'details': c.details,
                }
                for c in result.changes
            ],
            'summary': result.summary,
            'auth_events_detected': result.auth_events_detected,
        }

    def infer_filter_rules(self) -> Dict[str, Any]:
        """Infer the gateway's filter rules from captured UDS traffic."""
        from can_explorer.core.reverse.gateway_security import infer_filter_rules

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        ruleset = infer_filter_rules(messages)
        return {
            'rules': [
                {
                    'ecu_id': f'0x{r.ecu_id:03X}',
                    'service': f'0x{r.service:02X}',
                    'service_name': r.service_name,
                    'session': r.session,
                    'action': r.action,
                    'granularity': r.granularity,
                    'sub_function': r.sub_function,
                    'did': f'0x{r.did:04X}' if r.did is not None else None,
                    'max_length': r.max_length,
                    'max_rate_hz': r.max_rate_hz,
                    'evidence': r.evidence,
                }
                for r in ruleset.rules
            ],
            'ecus': [f'0x{e:03X}' for e in ruleset.ecus],
            'coverage': ruleset.coverage,
            'findings': ruleset.findings,
        }

    @staticmethod
    def detect_secoc_layout(payloads_hex: List[str], identifier: str = '0') -> Dict[str, Any]:
        """Detect SecOC layout (freshness + MAC boundary) without ARXML."""
        from can_explorer.core.reverse.crypto import detect_secoc_layout

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        layout = detect_secoc_layout(payloads, identifier=can_id)
        if layout is None:
            return {'detected': False, 'note': 'No SecOC layout detected with sufficient confidence'}
        return {
            'detected': True,
            'identifier': f'0x{layout.identifier:03X}',
            'payload_end': layout.payload_end,
            'freshness_start': layout.freshness_start,
            'freshness_end': layout.freshness_end,
            'freshness_bits': layout.freshness_bits,
            'mac_start': layout.mac_start,
            'mac_end': layout.mac_end,
            'mac_bits': layout.mac_bits,
            'confidence': layout.confidence,
            'findings': layout.findings,
        }

    @staticmethod
    def assess_secoc_strength(
        payloads_hex: List[str],
        timestamps: List[float],
        mac_start: int,
        mac_end: int,
        freshness_start: int,
        freshness_end: int,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Assess the practical security margin of a SecOC-protected identifier."""
        from can_explorer.core.reverse.crypto import secoc_strength_assessment

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = secoc_strength_assessment(
            payloads, timestamps, mac_start, mac_end, freshness_start, freshness_end, identifier=can_id
        )
        return {
            'identifier': f'0x{result.identifier:03X}',
            'mac_bits': result.mac_bits,
            'freshness_bits': result.freshness_bits,
            'forgery_probability': result.forgery_probability,
            'expected_forgery_time': result.expected_forgery_time,
            'frames_per_second': result.frames_per_second,
            'freshness_window': result.freshness_window,
            'replay_before_forgery': result.replay_before_forgery,
            'meets_autosar': result.meets_autosar,
            'severity': result.severity,
            'findings': result.findings,
        }

    @staticmethod
    def detect_secoc_bypass(
        payloads_hex: List[str],
        timestamps: List[float],
        expected_length: Optional[int] = None,
        mac_start: Optional[int] = None,
        mac_end: Optional[int] = None,
        freshness_start: Optional[int] = None,
        freshness_end: Optional[int] = None,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Detect SecOC bypass paths."""
        from can_explorer.core.reverse.crypto import detect_secoc_bypass

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        findings = detect_secoc_bypass(
            payloads,
            timestamps,
            expected_length=expected_length,
            mac_start=mac_start,
            mac_end=mac_end,
            freshness_start=freshness_start,
            freshness_end=freshness_end,
            identifier=can_id,
        )
        return {
            'findings': [
                {
                    'identifier': f'0x{f.identifier:03X}',
                    'bypass_type': f.bypass_type,
                    'description': f.description,
                    'evidence': f.evidence,
                    'severity': f.severity,
                }
                for f in findings
            ],
            'count': len(findings),
        }

    def attack_surface_report(self) -> Dict[str, Any]:
        """Produce a consolidated attack surface report."""
        from can_explorer.core.reverse.gateway_security import attack_surface_report

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        report = attack_surface_report(messages)
        return {
            'entries': [
                {
                    'ecu_id': f'0x{e.ecu_id:03X}',
                    'category': e.category,
                    'finding': e.finding,
                    'severity': e.severity,
                    'details': e.details,
                }
                for e in report.entries
            ],
            'ecu_scores': {f'0x{k:03X}': v for k, v in report.ecu_scores.items()},
            'overall_score': report.overall_score,
            'summary': report.summary,
            'ecu_count': report.ecu_count,
        }

    def enumerate_plan(self) -> Dict[str, Any]:
        """Generate a minimal UDS enumeration plan from captured traffic analysis."""
        from can_explorer.core.reverse.gateway_security import enumerate_plan

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        plan = enumerate_plan(messages)
        return {
            'steps': [
                {
                    'ecu_id': f'0x{s.ecu_id:03X}',
                    'description': s.description,
                    'uds_request_hex': s.uds_request_hex,
                    'expected_response': s.expected_response,
                    'prerequisites': s.prerequisites,
                    'session': s.session,
                    'security_level': s.security_level,
                }
                for s in plan.steps
            ],
            'ecu_count': plan.ecu_count,
            'estimated_time_seconds': plan.estimated_time_seconds,
            'findings': plan.findings,
        }

    def identify_ecu(self, ecu_id: Any) -> Dict[str, Any]:
        """Build a full ECU identity card from captured traffic."""
        from can_explorer.core.reverse.gateway_security import identify_ecu

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        identity = identify_ecu(messages, eid)
        return {
            'request_id': f'0x{identity.request_id:03X}',
            'response_id': f'0x{identity.response_id:03X}',
            'did_values': {f'0x{k:04X}': v for k, v in identity.did_values.items()},
            'vendor_hints': identity.vendor_hints,
            'hardware_version': identity.hardware_version,
            'software_version': identity.software_version,
            'ecu_serial': identity.ecu_serial,
            'spare_part': identity.spare_part,
            'system_supplier': identity.system_supplier,
            'findings': identity.findings,
        }

    def brute_plan(self) -> Dict[str, Any]:
        """Generate a brute-force plan for OEM sessions and security levels."""
        from can_explorer.core.reverse.gateway_security import brute_plan

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        plan = brute_plan(messages)
        return {
            'steps': [
                {
                    'ecu_id': f'0x{s.ecu_id:03X}',
                    'session': s.session,
                    'security_level': s.security_level,
                    'description': s.description,
                    'uds_request_hex': s.uds_request_hex,
                }
                for s in plan.steps
            ],
            'ecu_count': plan.ecu_count,
            'oem_sessions': plan.oem_sessions,
            'non_standard_levels': plan.non_standard_levels,
            'estimated_attempts': plan.estimated_attempts,
            'findings': plan.findings,
        }

    def detect_timing_side_channel(self, ecu_id: Any) -> Dict[str, Any]:
        """Detect timing side channels in key verification."""
        from can_explorer.core.reverse.gateway_security import detect_timing_side_channel

        if self.session is None:
            return {'error': NOT_CONNECTED}
        eid = _parse_id(ecu_id)
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        result = detect_timing_side_channel(messages, eid)
        if result is None:
            return {'ecu_id': f'0x{eid:03X}', 'found': False, 'note': 'Insufficient auth attempts for analysis'}
        return {
            'ecu_id': f'0x{result.ecu_id:03X}',
            'found': True,
            'constant_time': result.constant_time,
            'max_timing_delta_ms': result.max_timing_delta_ms,
            'byte_correlations': {str(k): v for k, v in result.byte_correlations.items()},
            'sample_count': result.sample_count,
            'findings': result.findings,
        }

    def infer_routing_table(
        self, external_captures: List[Dict[str, Any]], internal_captures: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Infer the gateway routing table from two-segment captures."""
        from can_explorer.core.reverse.gateway_security import infer_routing_table

        def _to_messages(entries: List[Dict[str, Any]]) -> List[can.Message]:
            msgs = []
            for entry in entries:
                arb_id = _parse_id(entry.get('id', '0'))
                data = bytes.fromhex(entry.get('data', ''))
                ts = float(entry.get('timestamp', 0.0))
                msgs.append(can.Message(arbitration_id=arb_id, data=data, timestamp=ts, is_extended_id=False))
            return msgs

        external = _to_messages(external_captures)
        internal = _to_messages(internal_captures)
        table = infer_routing_table(external, internal)
        return {
            'entries': [
                {
                    'request_id': f'0x{e.request_id:03X}',
                    'forwarded': e.forwarded,
                    'rewritten_id': f'0x{e.rewritten_id:03X}' if e.rewritten_id is not None else None,
                    'latency_ms': e.latency_ms,
                    'direction': e.direction,
                }
                for e in table.entries
            ],
            'forwarded_ids': [f'0x{i:03X}' for i in table.forwarded_ids],
            'blocked_ids': [f'0x{i:03X}' for i in table.blocked_ids],
            'rewritten_ids': [(f'0x{a:03X}', f'0x{b:03X}') for a, b in table.rewritten_ids],
            'findings': table.findings,
        }

    def detect_rewriting(
        self, external_captures: List[Dict[str, Any]], internal_captures: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect gateway message rewriting from two-interface captures."""
        from can_explorer.core.reverse.gateway_security import detect_rewriting

        def _to_messages(entries: List[Dict[str, Any]]) -> List[can.Message]:
            msgs = []
            for entry in entries:
                arb_id = _parse_id(entry.get('id', '0'))
                data = bytes.fromhex(entry.get('data', ''))
                ts = float(entry.get('timestamp', 0.0))
                msgs.append(can.Message(arbitration_id=arb_id, data=data, timestamp=ts, is_extended_id=False))
            return msgs

        external = _to_messages(external_captures)
        internal = _to_messages(internal_captures)
        result = detect_rewriting(external, internal)
        return {
            'findings': [
                {
                    'external_id': f'0x{f.external_id:03X}',
                    'internal_id': f'0x{f.internal_id:03X}',
                    'rewriting_type': f.rewriting_type,
                    'description': f.description,
                    'evidence': f.evidence,
                }
                for f in result.findings
            ],
            'matched_pairs': result.matched_pairs,
            'unmatched_external': [f'0x{i:03X}' for i in result.unmatched_external],
            'findings_summary': result.findings_summary,
        }

    def discover_unlock_chains(self) -> Dict[str, Any]:
        """Discover multi-session unlock chains."""
        from can_explorer.core.reverse.gateway_security import discover_unlock_chains

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        chains = discover_unlock_chains(messages)
        return {
            'chains': [
                {
                    'ecu_id': f'0x{c.ecu_id:03X}',
                    'steps': [
                        {
                            'session': s.session,
                            'security_level': s.security_level,
                            'service': f'0x{s.service:02X}',
                            'description': s.description,
                        }
                        for s in c.steps
                    ],
                    'target_service': f'0x{c.target_service:02X}',
                    'target_description': c.target_description,
                    'findings': c.findings,
                }
                for c in chains
            ],
            'count': len(chains),
        }

    def scan_plan_non_standard(self) -> Dict[str, Any]:
        """Generate a plan to scan non-standard ID ranges."""
        from can_explorer.core.reverse.gateway_security import scan_plan_non_standard

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        plan = scan_plan_non_standard(messages)
        return {
            'ranges': [
                {
                    'start_id': f'0x{r.start_id:03X}',
                    'end_id': f'0x{r.end_id:03X}',
                    'extended': r.extended,
                    'rationale': r.rationale,
                }
                for r in plan.ranges
            ],
            'known_responders': [f'0x{i:03X}' for i in plan.known_responders],
            'estimated_probes': plan.estimated_probes,
            'findings': plan.findings,
        }

    def analyse_doip_gateway(self) -> Dict[str, Any]:
        """Analyse DoIP gateway properties."""
        from can_explorer.core.reverse.gateway_security import analyse_doip_gateway

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        result = analyse_doip_gateway(messages)
        return {
            'activation_lines': result.activation_lines,
            'vehicle_id': result.vehicle_id,
            'routing_activations': result.routing_activations,
            'entity_addresses': result.entity_addresses,
            'access_control_findings': result.access_control_findings,
            'findings': result.findings,
        }

    def gateway_compliance_report(self) -> Dict[str, Any]:
        """Produce a gateway compliance report."""
        from can_explorer.core.reverse.gateway_security import gateway_compliance_report

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        report = gateway_compliance_report(messages)
        return {
            'findings': [
                {
                    'category': f.category,
                    'finding': f.finding,
                    'severity': f.severity,
                    'remediation': f.remediation,
                    'reference': f.reference,
                }
                for f in report.findings
            ],
            'wp29_coverage': report.wp29_coverage,
            'overall_compliance': report.overall_compliance,
            'summary': report.summary,
        }

    def disclosure_report(
        self, vehicle: str = '', researcher: str = '', contact: str = '', format: str = 'markdown'
    ) -> Dict[str, Any]:
        """A responsible-disclosure document (Markdown or JSON) for an OEM PSIRT, built from the captured gateway
        findings. States only what the capture shows; names no exploit."""
        from can_explorer.core.reverse.gateway_security import disclosure_report

        if self.session is None:
            return {'error': NOT_CONNECTED}
        if format not in ('markdown', 'json'):
            return {'error': "format must be 'markdown' or 'json'"}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        return {
            'report': disclosure_report(messages, vehicle=vehicle, researcher=researcher, contact=contact, fmt=format)
        }

    def gateway_findings_to_dbc(self, database_name: str = 'DiscoveredGateway') -> Dict[str, Any]:
        """A minimal DBC naming each discovered ECU's request and response message, to label unknown traffic in the
        signal views instead of re-deriving the topology."""
        from can_explorer.core.reverse.findings_artifacts import gateway_map_to_dbc
        from can_explorer.core.reverse.gateway_security import map_gateway

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        gmap = map_gateway(messages)
        return {'dbc': gateway_map_to_dbc(gmap, database_name), 'ecus': len(gmap.ecus)}

    def preview_probe_plan(self, plan: str = 'enumerate') -> Dict[str, Any]:
        """Preview how a probing plan's steps would be executed on a bench — a dry run that transmits nothing. The
        live run (which sends frames) is driven from the GUI with per-step confirmation, not from MCP. `plan` is
        'enumerate', 'brute' or 'scan'."""
        from can_explorer.core.reverse import active_prober
        from can_explorer.core.reverse.gateway_security import brute_plan, enumerate_plan, scan_plan_non_standard

        if self.session is None:
            return {'error': NOT_CONNECTED}
        messages = [frame.message for frame in self._recent(None, None, ANALYSIS_FRAMES)]
        builders = {
            'enumerate': lambda: active_prober.steps_from_enumeration(enumerate_plan(messages)),
            'brute': lambda: active_prober.steps_from_brute(brute_plan(messages)),
            'scan': lambda: active_prober.steps_from_scan(scan_plan_non_standard(messages)),
        }
        if plan not in builders:
            return {'error': "plan must be 'enumerate', 'brute' or 'scan'"}
        steps = builders[plan]()
        return {
            'plan': plan,
            'steps_planned': len(steps),
            'would_skip_dangerous': sum(1 for s in steps if s.service in active_prober.DANGEROUS_SERVICES),
            'steps': [
                {
                    'request_id': f'0x{s.request_id:X}',
                    'request': s.request.hex(),
                    'service': f'0x{s.service:02X}',
                    'description': s.description,
                }
                for s in steps[:50]
            ],
        }

    @staticmethod
    def assess_fd_security(payloads_hex: List[str], identifier: str = '0') -> Dict[str, Any]:
        """Assess CAN FD payload security properties."""
        from can_explorer.core.reverse.crypto import assess_fd_security

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = assess_fd_security(payloads, identifier=can_id)
        return {
            'identifier': f'0x{result.identifier:03X}',
            'payload_length': result.payload_length,
            'full_mac_possible': result.full_mac_possible,
            'mac_byte_range': list(result.mac_byte_range) if result.mac_byte_range else None,
            'freshness_byte_range': list(result.freshness_byte_range) if result.freshness_byte_range else None,
            'findings': result.findings,
        }

    @staticmethod
    def try_secoc_key_recovery(
        payloads_hex: List[str],
        mac_start: int,
        mac_end: int,
        freshness_start: int,
        freshness_end: int,
        data_id: Optional[int] = None,
        ecu_serial_hex: Optional[str] = None,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Try to recover the SecOC MAC key."""
        from can_explorer.core.reverse.crypto import try_secoc_key_recovery

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        ecu_serial = bytes.fromhex(ecu_serial_hex) if ecu_serial_hex else None
        result = try_secoc_key_recovery(
            payloads,
            mac_start,
            mac_end,
            freshness_start,
            freshness_end,
            data_id=data_id,
            ecu_serial=ecu_serial,
            identifier=can_id,
        )
        return {
            'identifier': f'0x{result.identifier:03X}',
            'recovered_key': result.recovered_key.hex() if result.recovered_key else None,
            'key_source': result.key_source,
            'attempts': result.attempts,
            'mac_algorithm': result.mac_algorithm,
            'confidence': result.confidence,
            'verified_frames': result.verified_frames,
            'findings': result.findings,
        }

    @staticmethod
    def detect_shared_secoc_keys(
        per_id_payloads_hex: Dict[str, List[str]],
        per_id_mac_range: Dict[str, List[int]],
        per_id_freshness_range: Dict[str, List[int]],
    ) -> Dict[str, Any]:
        """Detect whether multiple SecOC-protected PDUs share the same MAC key."""
        from can_explorer.core.reverse.crypto import detect_shared_secoc_keys

        try:
            payloads_map: Dict[int, List[bytes]] = {}
            for id_str, hexes in per_id_payloads_hex.items():
                can_id = int(id_str.strip().lower().removeprefix('0x'), 16)
                payloads_map[can_id] = [bytes.fromhex(p) for p in hexes]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        mac_map = {int(k.strip().lower().removeprefix('0x'), 16): (v[0], v[1]) for k, v in per_id_mac_range.items()}
        freshness_map = {
            int(k.strip().lower().removeprefix('0x'), 16): (v[0], v[1]) for k, v in per_id_freshness_range.items()
        }
        result = detect_shared_secoc_keys(payloads_map, mac_map, freshness_map)
        return {
            'groups': [
                {
                    'identifiers': [f'0x{i:03X}' for i in g.identifiers],
                    'shared_key': g.shared_key.hex() if g.shared_key else None,
                    'key_source': g.key_source,
                    'verified_frames': g.verified_frames,
                    'findings': g.findings,
                }
                for g in result.groups
            ],
            'total_identifiers': result.total_identifiers,
            'shared_count': result.shared_count,
            'findings': result.findings,
        }

    @staticmethod
    def predict_freshness(
        payloads_hex: List[str],
        timestamps: List[float],
        freshness_start: int,
        freshness_end: int,
        prediction_time: float = 10.0,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Predict future freshness counter values."""
        from can_explorer.core.reverse.crypto import predict_freshness

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = predict_freshness(
            payloads,
            timestamps,
            freshness_start,
            freshness_end,
            prediction_time=prediction_time,
            identifier=can_id,
        )
        return {
            'identifier': f'0x{result.identifier:03X}',
            'current_counter': result.current_counter,
            'predicted_counter': result.predicted_counter,
            'prediction_time': result.prediction_time,
            'counter_rate': result.counter_rate,
            'counter_width_bits': result.counter_width_bits,
            'wrap_time_seconds': result.wrap_time_seconds,
            'confidence': result.confidence,
            'findings': result.findings,
        }

    @staticmethod
    def replay_window(
        payloads_hex: List[str],
        timestamps: List[float],
        freshness_start: int,
        freshness_end: int,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Compute the replay window for a SecOC-protected identifier."""
        from can_explorer.core.reverse.crypto import replay_window

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = replay_window(payloads, timestamps, freshness_start, freshness_end, identifier=can_id)
        return {
            'identifier': f'0x{result.identifier:03X}',
            'window_seconds': result.window_seconds,
            'window_frames': result.window_frames,
            'counter_tolerance': result.counter_tolerance,
            'effective_replay_rate': result.effective_replay_rate,
            'findings': result.findings,
        }

    @staticmethod
    def classify_secoc_profile(
        mac_bits: int,
        freshness_bits: int,
        payload_length: int,
        algorithm_hint: Optional[str] = None,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Classify the AUTOSAR SecOC profile."""
        from can_explorer.core.reverse.crypto import classify_secoc_profile

        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = classify_secoc_profile(mac_bits, freshness_bits, payload_length, algorithm_hint, identifier=can_id)
        return {
            'identifier': f'0x{result.identifier:03X}',
            'profile': result.profile,
            'mac_bits': result.mac_bits,
            'freshness_bits': result.freshness_bits,
            'likely_algorithm': result.likely_algorithm,
            'autosar_compliant': result.autosar_compliant,
            'findings': result.findings,
        }

    @staticmethod
    def analyse_e2e_secoc_interaction(
        payloads_hex: List[str],
        e2e_counter_start: int,
        e2e_counter_end: int,
        secoc_freshness_start: int,
        secoc_freshness_end: int,
        identifier: str = '0',
    ) -> Dict[str, Any]:
        """Analyse E2E + SecOC counter interaction."""
        from can_explorer.core.reverse.crypto import analyse_e2e_secoc_interaction

        try:
            payloads = [bytes.fromhex(p) for p in payloads_hex]
        except ValueError as e:
            return {'error': f'Invalid hex payload: {e}'}
        can_id = int(identifier.strip().lower().removeprefix('0x'), 16) if identifier != '0' else 0
        result = analyse_e2e_secoc_interaction(
            payloads, e2e_counter_start, e2e_counter_end, secoc_freshness_start, secoc_freshness_end, identifier=can_id
        )
        return {
            'identifier': f'0x{result.identifier:03X}',
            'has_e2e': result.has_e2e,
            'has_secoc': result.has_secoc,
            'e2e_counter_range': list(result.e2e_counter_range) if result.e2e_counter_range else None,
            'secoc_freshness_range': list(result.secoc_freshness_range) if result.secoc_freshness_range else None,
            'counters_independent': result.counters_independent,
            'shared_counter_risk': result.shared_counter_risk,
            'correlation': result.correlation,
            'findings': result.findings,
        }

    # Active security tests (send tier)

    async def replay_test(
        self, identifier: str, data: str, count: int = 10, interval: float = 0.1, extended: bool = False
    ) -> Dict[str, Any]:
        """Replay a captured frame on the bus and report whether a response appeared (send tier)."""
        from can_explorer.core.active_security_test import replay_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        frame_id = _parse_id(identifier)
        payload = parse_hex_bytes(data)

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        result = await replay_test(_send, frame_id, payload, count=count, interval=interval)
        return {
            'id': _id_text(frame_id, extended),
            'replayed': result.replayed,
            'findings': result.findings,
        }

    async def forgery_test(
        self,
        identifier: str,
        data: str,
        mac_first: int,
        mac_last: int,
        attempts: int = 100,
        interval: float = 0.01,
        extended: bool = False,
    ) -> Dict[str, Any]:
        """Send frames with random MAC bytes and check if any are accepted (send tier)."""
        from can_explorer.core.active_security_test import forgery_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        frame_id = _parse_id(identifier)
        payload = parse_hex_bytes(data)

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        result = await forgery_test(
            _send, frame_id, payload, mac_range=(mac_first, mac_last), attempts=attempts, interval=interval
        )
        return {
            'id': _id_text(frame_id, extended),
            'attempts': result.attempts,
            'accepted': result.accepted,
            'findings': result.findings,
        }

    async def functional_bypass_test(
        self,
        physical_id: str,
        functional_id: str,
        service_requests_hex: List[str],
        extended: bool = False,
        interval: float = 0.5,
    ) -> Dict[str, Any]:
        """Test for functional addressing bypass (send tier)."""
        from can_explorer.core.active_security_test import functional_bypass_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        phys_id = _parse_id(physical_id)
        func_id = _parse_id(functional_id)
        requests = [bytes.fromhex(r) for r in service_requests_hex]

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        results = await functional_bypass_test(_send, phys_id, func_id, requests, interval=interval)
        return {
            'results': [
                {
                    'service': f'0x{r.service:02X}',
                    'physical_response': r.physical_response,
                    'functional_response': r.functional_response,
                    'bypass_found': r.bypass_found,
                    'findings': r.findings,
                }
                for r in results
            ],
            'bypass_count': sum(1 for r in results if r.bypass_found),
        }

    async def session_race_test(
        self,
        request_id: str,
        target_service_hex: str,
        extended: bool = False,
        attempts: int = 10,
        min_delay_ms: float = 0.0,
        max_delay_ms: float = 50.0,
    ) -> Dict[str, Any]:
        """Test for session race conditions (send tier)."""
        from can_explorer.core.active_security_test import session_race_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        req_id = _parse_id(request_id)
        target = bytes.fromhex(target_service_hex)

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        result = await session_race_test(
            _send, req_id, target, attempts=attempts, delay_ms_range=(min_delay_ms, max_delay_ms)
        )
        return {
            'target_service': f'0x{result.target_service:02X}',
            'race_window_ms': result.race_window_ms,
            'service_accepted': result.service_accepted,
            'findings': result.findings,
        }

    async def reset_bypass_test(
        self,
        request_id: str,
        target_services_hex: List[str],
        extended: bool = False,
        reset_type: int = 1,
        attempts: int = 5,
        max_window_ms: float = 5000.0,
        probe_interval_ms: float = 50.0,
    ) -> Dict[str, Any]:
        """Test for ECU reset bypass (send tier)."""
        from can_explorer.core.active_security_test import reset_bypass_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        req_id = _parse_id(request_id)
        targets = [bytes.fromhex(s) for s in target_services_hex]

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        result = await reset_bypass_test(
            _send,
            req_id,
            targets,
            reset_type=reset_type,
            attempts=attempts,
            max_window_ms=max_window_ms,
            probe_interval_ms=probe_interval_ms,
        )
        return {
            'target_service': f'0x{result.target_service:02X}',
            'bypass_found': result.bypass_found,
            'window_ms': result.window_ms,
            'services_accessible': [f'0x{s:02X}' for s in result.services_accessible],
            'findings': result.findings,
        }

    async def dos_resilience_test(
        self,
        request_id: str,
        test_payload_hex: str,
        extended: bool = False,
        rates_hz: Optional[List[float]] = None,
        duration_per_rate: float = 2.0,
    ) -> Dict[str, Any]:
        """Test gateway DoS resilience (send tier)."""
        from can_explorer.core.active_security_test import dos_resilience_test

        self._require_send()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        req_id = _parse_id(request_id)
        payload = bytes.fromhex(test_payload_hex)

        def _send(fid: int, pld: bytes) -> None:
            msg = can.Message(arbitration_id=fid, is_extended_id=extended, data=pld, check=False)
            self.session.send(msg)

        result = await dos_resilience_test(
            _send,
            req_id,
            payload,
            rates_hz=rates_hz,
            duration_per_rate=duration_per_rate,
        )
        return {
            'max_sustained_rate_hz': result.max_sustained_rate_hz,
            'degradation_threshold_hz': result.degradation_threshold_hz,
            'frames_sent': result.frames_sent,
            'frames_dropped': result.frames_dropped,
            'gateway_crashed': result.gateway_crashed,
            'recovery_time_ms': result.recovery_time_ms,
            'findings': result.findings,
        }

    # Diagnostics (OBD-II and UDS, over ISO-TP)

    def _isotp(self, request_id: Any, response_id: Any) -> IsoTpClient:
        """Identifiers as int or hex; a hex identifier may carry the address byte of extended or mixed addressing
        after a colon ('6F1:12')."""
        config = getattr(self.session.source, 'config', None)
        tx_id, tx_address = _parse_isotp_id(request_id)
        rx_id, rx_address = _parse_isotp_id(response_id)
        return IsoTpClient(
            self.session,
            tx_id,
            rx_id,
            fd=bool(getattr(config, 'fd', False)),
            tx_address=tx_address,
            rx_address=rx_address,
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

    async def uds_secured_request(
        self,
        payload: str,
        key: str = '',
        sign: bool = True,
        encrypt: bool = False,
        signed_response: bool = False,
        algorithm: int = 0,
        signature_length: int = 0,
        counter: Optional[int] = None,
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
    ) -> Dict[str, Any]:
        """Send a UDS request (hex) inside a SecuredDataTransmission (0x84) envelope and return the unwrapped,
        verified response. `key`: a secured data key of the key store (its name, or 'keystore:<name>'; the reference
        scheme, a MAC over the message, truncated to `signature_length` when not 0) or a Python provider file with the
        manufacturer's scheme; '' sends unsigned and unencrypted. The anti-replay counter continues per request id
        unless `counter` is given. Active: requires --allow-control."""
        from can_explorer.core import uds_secured

        bytes_payload = parse_hex_bytes(payload)
        source = str(key or '')
        request_id = _parse_id(request)
        if source == 'session':  # The session key of the last uds_authenticate on this request id
            if request_id not in self._auth_sessions:
                return {'error': 'No authentication with a session key on this request id; call uds_authenticate'}
        elif source and not source.startswith(uds_secured.KEYSTORE_PREFIX) and not Path(source).is_file():
            source = uds_secured.KEYSTORE_PREFIX + source
        try:
            if source == 'session':
                context, origin = self._auth_sessions[request_id]
                provider = uds_secured.session_provider(
                    context, origin, self._auth_store(), length=int(signature_length) or None
                )
            else:
                provider = uds_secured.load_provider(source, length=int(signature_length) or None)
            options = uds_secured.SecuredOptions(
                sign=bool(sign) and provider is not None,
                encrypt=bool(encrypt),
                signature_on_response=bool(signed_response),
                algorithm=int(algorithm),
                pre_established_key=source == 'session',
            )
        except uds_secured.SecuredDataError as e:
            return {'error': str(e)}

        async def operation(uds):
            start = self._sdt_counters.get(request_id, 0) if counter is None else int(counter)
            try:
                result = await uds_secured.secured_request(uds.transport, bytes_payload, provider, options, start)
            except uds_secured.SecuredDataError as e:
                return {'error': str(e)}
            self._sdt_counters[request_id] = (start + 1) % uds_secured.COUNTER_MODULO
            return result.as_dict()

        return await self._with_uds(request, response, operation, control=True)

    @staticmethod
    def _auth_store():
        """The key store for authentication credentials: the application's (the GUI's, when attached), unlocked
        from CANEXPLORER_KEYSTORE_PASSWORD if still locked."""
        from can_explorer.core import keystore

        store = keystore.default_store()
        if not store.unlocked:
            try:
                store.unlock_from_environment()
            except keystore.KeyStoreError as e:
                logger.warning(f'Key store: {e}')
        return store

    async def uds_authenticate(
        self,
        method: str = 'certificate',
        credentials: str = '',
        scheme: str = '',
        bidirectional: bool = False,
        trusted: str = '',
        algorithm: str = '',
        additional: str = '',
        communication_configuration: int = 0,
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
    ) -> Dict[str, Any]:
        """Authentication (0x29). `method`: 'certificate' (certificate exchange, `credentials` a client certificate
        entry of the key store or 'profile:<name>'), 'challenge-response' (`credentials` a shared authentication
        key, a client certificate entry or 'profile:<name>'; `algorithm` the algorithmIndicator, hex or a dotted
        OID; `additional` hex) or 'configuration' (authenticationConfiguration only). `trusted`: a
        trusted-certificates entry checking the ECU (bidirectional). A session key asked for with
        `communication_configuration` is kept for uds_secured_request(key='session'). Active: --allow-control."""
        from can_explorer.core import audit, uds_auth, uds_secured

        method = str(method or 'certificate').lower()
        store = self._auth_store()
        request_id = _parse_id(request)
        configuration = int(communication_configuration)
        if not 0 <= configuration <= 0xFF:
            return {'error': 'communication_configuration is one byte'}
        try:
            if method == 'configuration':
                prepared = None
            elif method == 'certificate':
                certificate, private_key = uds_auth.credentials(store, str(credentials))
                trusted_certificates = uds_auth.trusted_certificates(store, str(trusted)) if trusted else []
                scheme = str(scheme or uds_auth.scheme_for(certificate.public_key()))
                uds_auth.check_scheme(scheme, certificate.public_key())
                prepared = (certificate, private_key, trusted_certificates)
            elif method == 'challenge-response':
                prove, verify_server, how = uds_auth.challenge_response_credentials(
                    store, str(credentials), str(scheme), str(trusted)
                )
                prepared = (prove, verify_server, how, uds_auth.algorithm_indicator(str(algorithm)))
            else:
                return {'error': "method is 'certificate', 'challenge-response' or 'configuration'"}
            extra = parse_hex_bytes(str(additional or ''))
        except (uds_auth.UdsAuthenticationError, ValueError) as e:
            return {'error': str(e)}

        async def operation(uds):
            if method == 'configuration':
                return {'configuration': await uds_auth.authentication_configuration(uds)}
            self._auth_sessions.pop(request_id, None)  # The ECU ends an authentication when a new one starts
            try:
                if method == 'certificate':
                    certificate, private_key, trusted_certificates = prepared
                    result = await uds.authenticate(
                        certificate,
                        private_key,
                        scheme,
                        bool(bidirectional),
                        trusted_certificates,
                        store.revocation_lists(),
                        communication_configuration=configuration,
                    )
                else:
                    prove, verify_server, how, indicator = prepared
                    result = await uds.authenticate_challenge_response(
                        prove,
                        bool(bidirectional),
                        verify_server,
                        indicator=indicator,
                        additional_parameter=extra,
                        communication_configuration=configuration,
                    )
            except uds_auth.UdsAuthenticationError as e:
                return {'authenticated': False, 'error': str(e)}
            audit.record(
                'uds authentication', source=str(credentials), method=method, authenticated=result.authenticated
            )
            answer: Dict[str, Any] = {
                'authenticated': result.authenticated,
                'method': method,
                'steps': result.steps,
                'return_parameter': f'0x{result.return_parameter:02X}',
            }
            if result.server_certificate is not None:
                answer['ecu_certificate'] = result.server_certificate.subject.rfc4514_string()
            if result.client_certificate is not None:
                extensions = uds_auth.private_extensions(result.client_certificate)
                if extensions:  # Where an ECU may read roles and white lists from
                    answer['certificate_private_extensions'] = [f'{oid} ({size} B)' for oid, size in extensions]
            if result.authenticated and result.session_key_info:
                self._auth_sessions[request_id] = (
                    uds_secured.SessionContext.from_result(result),
                    str(credentials),
                )
                answer['session_key_info_bytes'] = len(result.session_key_info)
                answer['session_key'] = "kept: uds_secured_request(key='session')"
            return answer

        return await self._with_uds(request, response, operation, control=True)

    async def uds_deauthenticate(self, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE):
        """deAuthenticate (0x29 0x00): ends the authentication and forgets its session key. Active."""
        from can_explorer.core import uds_auth

        request_id = _parse_id(request)

        async def operation(uds):
            self._auth_sessions.pop(request_id, None)
            return {'result': await uds_auth.deauthenticate(uds)}

        return await self._with_uds(request, response, operation, control=True)

    async def uds_transmit_certificate(
        self, certificate: str, evaluation_id: Any, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """transmitCertificate (0x29 0x04): send a general-purpose certificate (a key store certificate entry or a
        PEM/DER file) for a certificateEvaluationId (manufacturer specific). Active: --allow-control."""
        from can_explorer.core import uds_auth

        try:
            certificate_value = uds_auth.certificate_to_transmit(self._auth_store(), str(certificate))
            evaluation = evaluation_id if isinstance(evaluation_id, int) else int(str(evaluation_id), 0)
        except (uds_auth.UdsAuthenticationError, ValueError) as e:
            return {'error': str(e)}

        async def operation(uds):
            try:
                text = await uds_auth.transmit_certificate(uds, certificate_value, evaluation)
            except uds_auth.UdsAuthenticationError as e:
                return {'error': str(e)}
            return {
                'result': text,
                'evaluation_id': f'0x{evaluation:04X}',
                'certificate': certificate_value.subject.rfc4514_string(),
            }

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

    async def uds_dtc_details(
        self, dtc: Any, request: Any = DEFAULT_UDS_REQUEST, response: Any = ECU_RESPONSE
    ) -> Dict[str, Any]:
        """A DTC's snapshot (freeze frame) and extended data records (0x19 0x04 and 0x06, all records). Records whose
        lengths the response does not give are returned undecoded (hex) instead of split at a guess."""
        from can_explorer.core import uds_dtc

        try:
            code = int(str(dtc).strip().removeprefix('0x').removeprefix('0X'), 16) if isinstance(dtc, str) else int(dtc)
        except ValueError:
            return {'error': f'Invalid DTC {dtc!r}: expected 3 bytes in hex, e.g. "012345"'}

        async def operation(uds):
            result: Dict[str, Any] = {'dtc': uds_dtc.DtcRecord(code).text}
            for key, subfunction in (('snapshots', 0x04), ('extended_data', 0x06)):
                try:
                    details = await uds.read_dtc_information(subfunction, dtc=code)
                except UdsError as e:
                    result[key] = {'error': str(e)}
                    continue
                result['status'] = f'0x{details.status:02X}'
                if key == 'snapshots':
                    result[key] = [
                        {
                            'record': snapshot.number,
                            'values': [
                                {'did': f'{v.did:04X}', 'data': v.data.hex(' ').upper()} for v in snapshot.values
                            ],
                            'undecoded': snapshot.undecoded.hex(' ').upper(),
                        }
                        for snapshot in details.snapshots
                    ]
                else:
                    result[key] = {
                        'records': [
                            {'record': r.number, 'data': r.data.hex(' ').upper()} for r in details.extended_data
                        ],
                        'undecoded': details.undecoded.hex(' ').upper(),
                    }
            return result

        return await self._with_uds(request, response, operation)

    async def uds_find_ecus(self, extended: bool = False, timeout: float = 0.5) -> Dict[str, Any]:
        """The ECUs answering a functional TesterPresent: 0x7DF (11-bit), or 0x18DB33F1 with `extended`."""
        from can_explorer.core import uds_functional

        self._require_diagnostics()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        config = getattr(self.session.source, 'config', None)
        fd = bool(getattr(config, 'fd', False))
        transport = (
            uds_functional.obd_29bit(self.session, fd=fd) if extended else uds_functional.obd_11bit(self.session, fd=fd)
        )
        try:
            responders = await uds_functional.FunctionalUdsClient(transport, timeout=float(timeout)).discover()
        except (UdsError, OSError, ValueError) as e:
            return {'error': str(e)}
        finally:
            transport.close()
        width = 8 if extended else 3
        return {'ecus': [f'{address:0{width}X}' for address in responders]}

    # J1939

    async def j1939_request(
        self, pgn: Any, destination: Any = 'FF', source: Any = 'F9', timeout: float = 1.25
    ) -> Dict[str, Any]:
        """Request a J1939 parameter group (PGN 59904) and return the answers, reassembled and decoded where known.
        The tool answers transport sessions to its address, so replies longer than 8 bytes arrive too."""
        from can_explorer.core.j1939_tools import PGN_DM3, PGN_DM11, parse_pgn, pgn_label, request_pgn
        from can_explorer.core.protocols.j1939 import J1939Decoder

        number = pgn if isinstance(pgn, int) else parse_pgn(str(pgn))
        # DM3 and DM11 requests clear trouble codes
        (self._require_control if number in (PGN_DM3, PGN_DM11) else self._require_diagnostics)()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        target, tool = _parse_j1939_address(destination), _parse_j1939_address(source)
        answers = await request_pgn(self.session, number, target, tool, float(timeout))
        decoder = J1939Decoder()
        result = []
        for answer in answers:
            entry: Dict[str, Any] = {'source': f'{answer.source:02X}'}
            if answer.payload is None:
                entry['acknowledgment'] = answer.acknowledgment
            else:
                entry['data'] = answer.payload.hex(' ').upper()
                entry['length'] = len(answer.payload)
                decoded = decoder.describe_payload(number, answer.payload)
                if decoded:
                    entry['decoded'] = decoded
            result.append(entry)
        return {'pgn': f'0x{number:05X}', 'label': pgn_label(number), 'answers': result}

    async def j1939_send(
        self, pgn: Any, data: str, destination: Any = 'FF', priority: int = 6, source: Any = 'F9'
    ) -> Dict[str, Any]:
        """Send a J1939 parameter group: a single frame up to 8 bytes, BAM to all nodes, RTS/CTS or (over 1785
        bytes) ETP to one node."""
        from can_explorer.core.j1939_tools import TP_MAX, J1939Sender, is_pdu1, parse_pgn

        self._require_send()
        if self.session is None:
            return {'sent': False, 'error': NOT_CONNECTED}
        number = pgn if isinstance(pgn, int) else parse_pgn(str(pgn))
        payload = parse_hex_bytes(data)
        target, tool = _parse_j1939_address(destination), _parse_j1939_address(source)
        if not is_pdu1(number):
            target = 0xFF
        try:
            await J1939Sender(self.session).send(number, payload, tool, target, int(priority))
        except (TimeoutError, ConnectionError, ValueError, SessionError) as e:
            return {'sent': False, 'error': str(e)}
        if len(payload) <= 8:
            transport = 'single frame'
        elif target == 0xFF:
            transport = 'BAM'
        else:
            transport = 'ETP' if len(payload) > TP_MAX else 'RTS/CTS'
        return {'sent': True, 'pgn': f'0x{number:05X}', 'bytes': len(payload), 'transport': transport}

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

    # KWP2000 (VW TP 2.0 or ISO-TP)

    async def _with_kwp(
        self, transport, module, request_id, response_id, session, operation, control: bool = False
    ) -> Dict[str, Any]:
        """Run `operation(KwpClient)` over a TP 2.0 channel to `module` (opened and closed around it) or over ISO-TP,
        after StartDiagnosticSession `session` (hex; '' skips it)."""
        (self._require_control if control else self._require_diagnostics)()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        kind = str(transport).strip().lower().replace('.', '').replace(' ', '')
        if kind not in ('tp20', 'isotp'):
            return {'error': f'Unknown KWP2000 transport {transport!r}; use "tp20" or "isotp"'}
        try:
            session_byte = _parse_id(session) if str(session).strip() else None
            if kind == 'tp20':
                channel = Tp20Client(self.session, _parse_id(module))
                await channel.open()
            else:
                channel = self._isotp(request_id, response_id)
        except (Tp20Error, ValueError) as e:
            return {'error': str(e)}
        try:
            client = KwpClient(channel)
            if session_byte is not None:
                await client.start_diagnostic_session(session_byte)
            return await operation(client)
        except (KwpError, Tp20Error, MeasuringBlockError, UdsError, OSError, ValueError) as e:
            return {'error': str(e)}
        finally:
            if kind == 'tp20':
                await channel.disconnect()
            else:
                channel.close()

    async def kwp_read_measuring_block(
        self,
        group: int,
        module: Any = '01',
        formulas: str = '',
        transport: str = 'tp20',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
        session: Any = '89',
    ) -> Dict[str, Any]:
        """Read a measuring block (KWP2000 0x21 group): each value's formula byte and data bytes a, b — scaled and
        named with a user-supplied JSON formula table or label file (`formulas`, the module's entry of a label file),
        raw without one."""
        try:
            table = LabelFile.load(formulas).formula_table(_parse_id(module)) if str(formulas).strip() else None
        except ValueError as e:  # LabelError, an invalid module
            return {'error': str(e)}

        async def operation(kwp):
            return (await read_measuring_block(kwp, int(group), table)).as_dict()

        return await self._with_kwp(transport, module, request, response, session, operation)

    async def kwp_read_identification(
        self,
        option: Any,
        module: Any = '01',
        transport: str = 'tp20',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
        session: Any = '89',
    ) -> Dict[str, Any]:
        """ReadECUIdentification (KWP2000 0x1A) with an identification option (hex); the raw bytes and their
        printable text."""
        option_byte = _parse_id(option)

        async def operation(kwp):
            data = await kwp.read_ecu_identification(option_byte)
            text = bytes(byte for byte in data if 0x20 <= byte < 0x7F).decode('ascii', 'replace')
            return {'option': f'{option_byte:02X}', 'data': bytes(data).hex(' ').upper(), 'text': text}

        return await self._with_kwp(transport, module, request, response, session, operation)

    async def kwp_request(
        self,
        payload: str,
        module: Any = '01',
        transport: str = 'tp20',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
        session: Any = '',
    ) -> Dict[str, Any]:
        """Send a raw KWP2000 request (hex, e.g. "21 01") and return the response. Active: requires
        --allow-control because a raw request can do anything (resets, routines, writes)."""
        bytes_payload = parse_hex_bytes(payload)

        async def operation(kwp):
            result = await kwp.request(bytes_payload)
            return {
                'request': bytes_payload.hex(' ').upper(),
                'response': result.response.hex(' ').upper() if result.response else None,
                'description': result.description,
                'error': result.error or None,
            }

        return await self._with_kwp(transport, module, request, response, session, operation, control=True)

    async def kwp_scan_modules(
        self,
        addresses: str = '01-FF',
        identification: Any = '',
        session: Any = '89',
        labels: str = '',
        setup_timeout: float = SETUP_TIMEOUT,
    ) -> Dict[str, Any]:
        """Scan logical addresses for VW TP 2.0 modules: a channel setup to each, closed again. With
        `identification` (an option, hex), each found module is asked for ReadECUIdentification after
        StartDiagnosticSession `session`; `labels` names the modules."""
        self._require_diagnostics()
        if self.session is None:
            return {'error': NOT_CONNECTED}
        try:
            targets = parse_addresses(addresses)
            option = _parse_id(identification) if str(identification).strip() else None
            session_byte = _parse_id(session) if option is not None and str(session).strip() else None
            label_file = LabelFile.load(labels) if str(labels).strip() else None
        except ValueError as e:
            return {'error': str(e)}
        found = [
            result.as_dict()
            async for result in scan_modules(
                self.session, targets, float(setup_timeout), option, session_byte, label_file
            )
            if result.found
        ]
        return {'scanned': len(targets), 'modules': found}

    # Coding records (KWP2000 local identifiers or UDS data identifiers)

    @staticmethod
    def _coding_record(labels: str, module: Any, record: str) -> CodingRecord:
        """A record of a label file, or — without one — a raw location 'kwp:9A' or 'uds:0600' (no fields)."""
        if str(labels).strip():
            return LabelFile.load(labels).coding_record(_parse_id(module), str(record).strip())
        protocol, colon, identifier = str(record).strip().partition(':')
        if not colon:
            raise CodingError('Give a label file, or the record as "kwp:<local identifier>" or "uds:<DID>" (hex)')
        return CodingRecord(f'{protocol.lower()}:{identifier}', protocol.strip().lower(), _parse_id(identifier))

    async def _with_coding(
        self, record: CodingRecord, transport, module, request_id, response_id, session, key, level, operation,
        control: bool = False,
    ) -> Dict[str, Any]:  # fmt: skip
        """Run `operation(client)` with the client of the record's protocol: KwpClient (TP 2.0 or ISO-TP) or UdsClient
        (ISO-TP), after the session `session` and, with `key`, security access at `level`."""
        try:
            compute_key = load_key_source(key) if str(key).strip() else None
        except UdsError as e:
            return {'error': str(e)}

        async def prepared(client):
            if compute_key is not None:
                await client.security_access(int(level), compute_key)
            return await operation(client)

        if record.protocol == 'kwp':
            return await self._with_kwp(transport, module, request_id, response_id, session, prepared, control)

        async def uds_operation(uds):
            if str(session).strip():
                await uds.diagnostic_session_control(_parse_id(session))
            return await prepared(uds)

        return await self._with_uds(request_id, response_id, uds_operation, control)

    async def coding_read(
        self,
        module: Any = '01',
        record: str = '',
        labels: str = '',
        transport: str = 'tp20',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
        session: Any = '',
        key: str = '',
        level: int = 1,
    ) -> Dict[str, Any]:
        """Read a module's coding record and decode its fields with the label file (raw bytes without fields)."""
        try:
            definition = self._coding_record(labels, module, record)
        except ValueError as e:  # LabelError, CodingError, an invalid id
            return {'error': str(e)}

        async def operation(client):
            data = await definition.read(client)
            return {
                'record': definition.name,
                'location': definition.location(),
                'data': bytes(data).hex(' ').upper(),
                'fields': [value.as_dict() for value in definition.decode(data)],
            }

        return await self._with_coding(definition, transport, module, request, response, session, key, level, operation)

    async def coding_write(
        self,
        module: Any = '01',
        record: str = '',
        labels: str = '',
        changes: Optional[Dict[str, int]] = None,
        data: str = '',
        transport: str = 'tp20',
        request: Any = DEFAULT_UDS_REQUEST,
        response: Any = ECU_RESPONSE,
        session: Any = '',
        key: str = '',
        level: int = 1,
    ) -> Dict[str, Any]:
        """Change a coding record: set fields by name (`changes`) or give the whole record (`data`, hex). Reads the
        current record first (returned as `before`, the backup), writes, reads back and reports `verified`.
        Active: requires --allow-control."""
        self._require_control()
        if bool(changes) == bool(str(data).strip()):
            return {'error': 'Give either changes (field name -> value) or data (the whole record, hex)'}
        try:
            definition = self._coding_record(labels, module, record)
            new_data = parse_hex_bytes(data) if str(data).strip() else None
        except ValueError as e:
            return {'error': str(e)}

        async def operation(client):
            current = await definition.read(client)
            target = new_data if new_data is not None else definition.apply(current, dict(changes or {}))
            result = await write_record(client, definition, target, expected_before=current)
            return result.as_dict()

        return await self._with_coding(
            definition, transport, module, request, response, session, key, level, operation, control=True
        )

    # XCP / CCP measurement and calibration

    async def _with_xcp(self, operation, control: bool = False, needs_bus: bool = True) -> Dict[str, Any]:
        (self._require_control if control else self._require_diagnostics)()
        try:
            return await operation(self._xcp)
        except SendNotAllowed:
            raise
        except Exception as e:  # XCP/CCP errors, timeouts, A2L and conversion errors: reported to the agent
            return {'error': str(e) or type(e).__name__}

    def xcp_list_variables(self, a2l: str = '', pattern: str = '', kind: str = '', limit: int = 200) -> Dict[str, Any]:
        """The measurements and characteristics of the A2L (loading `a2l` first when given). Sends nothing."""
        try:
            loaded = self._xcp.load(a2l_path(a2l)) if a2l else None
            result = self._xcp.variables(pattern, kind, limit)
        except Exception as e:
            return {'error': str(e)}
        if loaded is not None:
            result['a2l'] = loaded
        return result

    async def xcp_connect(
        self,
        protocol: str = 'XCP',
        transport: str = 'CAN',
        request: Any = '7E0',
        response: Any = '7E1',
        extended: bool = False,
        station: str = '0',
        byte_order: str = 'big',
        host: str = '',
        port: int = 5555,
        key: str = '',
        a2l: str = '',
    ) -> Dict[str, Any]:
        """Connect to an XCP (CAN, CAN FD, UDP, TCP) or CCP slave and keep the session for the other xcp_* tools.
        `key`: a seed & key algorithm (a Python file, 'keystore:<entry>' or 'profile:<name>'), used when a command
        needs a locked resource. `a2l` loads the ECU description."""

        async def operation(xcp: CalibrationSession):
            if a2l:
                xcp.load(a2l_path(a2l))
            compute_key = load_key_source(key) if key else None
            if transport.upper() not in ('UDP', 'TCP') and self.session is None:
                return {'error': NOT_CONNECTED}
            config = getattr(self.session.source, 'config', None) if self.session is not None else None
            return await xcp.connect(
                self.session, protocol, transport, request, response, extended, station, byte_order, host, port,
                compute_key, fd=bool(getattr(config, 'fd', False)) and transport.upper() == 'CAN FD',
            )  # fmt: skip

        return await self._with_xcp(operation)

    async def xcp_disconnect(self) -> Dict[str, Any]:
        async def operation(xcp: CalibrationSession):
            await xcp.disconnect()
            return {'connected': False}

        return await self._with_xcp(operation)

    async def xcp_status(self) -> Dict[str, Any]:
        """The XCP/CCP session: protocol, byte order, locked resources, and whether the A2L matches (EPK)."""
        return await self._with_xcp(lambda xcp: xcp.status())

    async def xcp_read(self, names: List[str]) -> Dict[str, Any]:
        """Physical values of A2L variables (scalars, arrays, curves and maps with their axes)."""
        return await self._with_xcp(lambda xcp: xcp.read(list(names)))

    async def xcp_read_memory(self, address: str, size: int, extension: int = 0) -> Dict[str, Any]:
        return await self._with_xcp(lambda xcp: xcp.read_memory(address, int(size), int(extension)))

    async def xcp_measure(self, names: List[str], duration: float = 1.0, event: Optional[int] = None) -> Dict[str, Any]:
        return await self._with_xcp(lambda xcp: xcp.measure(list(names), duration, event))

    async def xcp_daq_info(self) -> Dict[str, Any]:
        """The XCP slave's DAQ configuration: dynamic or static, its lists (and what predefined ones carry), events."""
        return await self._with_xcp(lambda xcp: xcp.daq_info())

    async def xcp_flash_info(self) -> Dict[str, Any]:
        """What the XCP slave offers for programming: PGM properties and flash sectors."""
        return await self._with_xcp(lambda xcp: xcp.flash_info())

    async def xcp_stimulate(
        self, values: Dict[str, Any], event: Optional[int] = None, duration: float = 1.0
    ) -> Dict[str, Any]:
        """Write scalar calibration values by XCP STIM on an event. Active: requires --allow-control."""
        return await self._with_xcp(lambda xcp: xcp.stimulate(dict(values), event, duration), control=True)

    async def xcp_program(self, path: str, **options: Any) -> Dict[str, Any]:
        """Program a file into the ECU's flash over XCP. Active: requires --allow-control."""
        return await self._with_xcp(lambda xcp: xcp.program(path, **options), control=True)

    async def xcp_write(self, name: str, value: Any, axes: Optional[List] = None) -> Dict[str, Any]:
        """Write a calibration value (checked against its A2L limits) and read it back. Active: --allow-control."""
        return await self._with_xcp(lambda xcp: xcp.write(name, value, axes), control=True)

    async def xcp_write_memory(self, address: str, data: str, extension: int = 0) -> Dict[str, Any]:
        """Write raw bytes to the ECU's memory. Active: requires --allow-control."""
        return await self._with_xcp(lambda xcp: xcp.write_memory(address, data, int(extension)), control=True)

    # ------------------------------------------------------------------
    # E2E reverse engineering
    # ------------------------------------------------------------------

    def detect_e2e(self, payloads_hex: List[str], identifier: str = '0') -> Dict[str, Any]:
        """Run the full E2E detection pipeline on one identifier's payloads. Read-only."""
        from can_explorer.core.reverse.e2e_reverse import detect_e2e as _detect_e2e

        payloads = [parse_hex_bytes(h) for h in payloads_hex]
        result = _detect_e2e(payloads)
        profiles = []
        for p in result.profiles:
            entry: Dict[str, Any] = {
                'profile': p.profile,
                'crc_position': p.crc_position,
                'crc_width': p.crc_width,
                'confidence': p.confidence,
                'confidence_label': p.confidence_label,
                'findings': p.findings,
            }
            if p.counter:
                entry['counter'] = {
                    'position': p.counter.position,
                    'width': p.counter.width,
                    'modulus': p.counter.modulus,
                    'nibble_high': p.counter.nibble_high,
                    'increment_rate': p.counter.increment_rate,
                }
            if p.data_ids:
                entry['data_ids'] = p.data_ids.data_ids
                entry['data_id_verification_rate'] = p.data_ids.verification_rate
            profiles.append(entry)
        bypasses = [
            {'type': b.bypass_type, 'description': b.description, 'severity': b.severity} for b in result.bypasses
        ]
        counter_info = None
        if result.counter:
            counter_info = {
                'position': result.counter.position,
                'width': result.counter.width,
                'modulus': result.counter.modulus,
                'nibble_high': result.counter.nibble_high,
                'increment_rate': result.counter.increment_rate,
                'confidence': result.counter.confidence,
            }
        return {
            'identifier': identifier,
            'frames': len(payloads),
            'profiles': profiles,
            'counter': counter_info,
            'bypasses': bypasses,
            'findings': result.findings,
        }

    def compute_e2e(
        self,
        profile: int,
        data_hex: str,
        counter: int,
        data_ids: object,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Compute a valid E2E header for arbitrary payload data using recovered parameters. Read-only."""
        from can_explorer.core.reverse.e2e_reverse import compute_e2e_header

        data = parse_hex_bytes(data_hex)
        protected = compute_e2e_header(profile, data, counter, data_ids, offset=offset)
        return {
            'profile': profile,
            'counter': counter,
            'protected': protected.hex(' ').upper(),
        }
