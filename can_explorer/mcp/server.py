"""The MCP server: exposes a CanService (service.py) as MCP tools, resources and prompts over stdio, using the MCP
SDK (v2, MCPServer).

Needs the `mcp` package (the `mcp` extra). The tools are thin async wrappers around the service, so they run on the
event loop thread, in step with the bus receive pump. Tools are registered by permission tier: the diagnostic
reads need the diagnostics tier, the active control tools the control tier, and the transmitting tools the send
tier (see CanService.can_diagnose / can_control / allow_send).
"""

import json
from typing import Any, Dict, List, Optional

from mcp.server import MCPServer

from can_explorer.mcp.service import CanService

INSTRUCTIONS = """Inspect, reverse-engineer and (when allowed) diagnose or drive a CAN bus.

If bus_status shows "connected": false, first call list_interfaces to see the available CAN interfaces, then
connect(interface, channel) to open one (for example interface "socketcan" channel "can0", or "virtual" channel
"test" to try it). The database, protocol and permissions stay across connections.

Once connected, start with bus_status (state, counts, and which capabilities are enabled) and list_identifiers.
get_frames returns
recent frames decoded into signals; decode_frame and decode_protocol interpret a payload you supply. timing shows
which identifiers are periodic or event-triggered. analyze_identifier, related_signals, capture_reaction and
correlate are the reverse-engineering tools; promote_to_dbc writes found signals to a DBC. load_database loads a
DBC/ARXML/KCD/SYM/DBF. The resources can://status, can://identifiers, can://frames and can://database mirror this
as readable data; the prompts guide the common workflows. Identifiers are hex strings such as "1A0".

Diagnostics (OBD-II obd_* and UDS uds_*) and transmitting (send_frame, send_message) are only available when the
server was started with the matching permission; each tool's description says which tier it needs.
"""


def build_mcp(service: CanService) -> MCPServer:  # noqa: C901 - a flat list of tool registrations
    mcp = MCPServer('CANExplorer', instructions=INSTRUCTIONS)

    # Read-only: inspection and reverse engineering

    @mcp.tool()
    async def bus_status() -> Dict[str, Any]:
        """The connection state, loaded database, enabled capabilities, and (when connected) frame counts, rates
        and bus load. If not connected, use list_interfaces and connect."""
        return service.status()

    if service.manages_connection:

        @mcp.tool()
        async def connect(
            interface: str, channel: str, bitrate: int = 500000, fd: bool = False, data_bitrate: Optional[int] = None
        ) -> Dict[str, Any]:
            """Open a CAN bus and start receiving, closing any current one. interface is a python-can interface
            (socketcan, pcan, vector, gvret, virtual, canexplorer_remote…) and channel its channel (can0,
            PCAN_USBBUS1, COM7…). Keeps the database and protocol; starts a fresh trace."""
            return service.connect(interface, channel, bitrate=bitrate, fd=fd, data_bitrate=data_bitrate)

        @mcp.tool()
        async def disconnect() -> Dict[str, Any]:
            """Close the current CAN bus."""
            return service.disconnect()

    @mcp.tool()
    async def list_identifiers(limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Every identifier seen, with its direction, count, cycle time, last data and database name."""
        return service.identifiers(limit)

    @mcp.tool()
    async def get_frames(
        identifier: Optional[str] = None, extended: bool = False, limit: int = 50, decode: bool = True
    ) -> List[Dict[str, Any]]:
        """The most recent frames, newest last, optionally of one identifier, decoded into signals with the database."""
        return service.frames(identifier, extended, limit, decode)

    @mcp.tool()
    async def decode_frame(identifier: str, data: str, extended: bool = False) -> Dict[str, Any]:
        """Decode a payload (hex, e.g. "A0 0F 5A 03") for an identifier with the loaded database. Sends nothing."""
        return service.decode_frame(identifier, data, extended)

    @mcp.tool()
    async def decode_protocol(protocol: str, identifier: str, data: str, extended: bool = False) -> Dict[str, Any]:
        """Interpret a payload with a protocol decoder (J1939, "NMEA 2000", CANopen, ISO-TP, UDS, LIN, J1587),
        without a database. Sends nothing."""
        return service.decode_protocol(protocol, identifier, data, extended)

    @mcp.tool()
    async def timing(identifier: Optional[str] = None, extended: bool = False) -> List[Dict[str, Any]]:
        """The inter-frame timing of each identifier (or one): mean cycle time, jitter, and whether it is periodic
        or event-triggered. Spots gateways, bursts and event messages."""
        return service.timing(identifier, extended)

    @mcp.tool()
    async def analyze_identifier(
        identifier: str, extended: bool = False, include_constant: bool = False
    ) -> Dict[str, Any]:
        """Automatically find the fields of an identifier (checksums, counters, multiplexers, physical values,
        states and flags) from its recent frames."""
        return service.analyze(identifier, extended, include_constant)

    @mcp.tool()
    async def related_signals(top: int = 20, min_correlation: float = 0.99) -> Dict[str, Any]:
        """Groups of fields that carry the same signal across the bus (the same quantity rebroadcast, or a scaled
        or lagged copy), found without a database."""
        return service.related_signals(top, min_correlation)

    @mcp.tool()
    async def capture_reaction(baseline_seconds: float = 5.0, action_seconds: float = 5.0) -> Dict[str, Any]:
        """Diff the most recent `action_seconds` of traffic against the `baseline_seconds` before it to find what
        changed on the bus (identifiers that appeared, bits that changed). Record a steady baseline, perform the
        action, wait `action_seconds`, then call this. Passive — reads only."""
        return service.capture_reaction(baseline_seconds, action_seconds)

    @mcp.tool()
    async def correlate(
        signal: Optional[str] = None,
        csv_path: Optional[str] = None,
        times: Optional[List[float]] = None,
        values: Optional[List[float]] = None,
        name: str = 'reference',
        unit: str = '',
        column: int = 0,
        identifier: Optional[str] = None,
        extended: bool = False,
        max_lag: float = 1.0,
        top: int = 10,
    ) -> Dict[str, Any]:
        """Find the field that follows a known reference series: a database signal ("Message.Signal"), a column of a
        CSV file (csv_path, column), or an inline time series (times and values). Returns the best-matching fields
        with their correlation, fitted factor and offset (into the reference's unit) and time lag."""
        return service.correlate(
            signal=signal,
            csv_path=csv_path,
            times=times,
            values=values,
            name=name,
            unit=unit,
            column=column,
            identifier=identifier,
            extended=extended,
            max_lag=max_lag,
            top=top,
        )

    @mcp.tool()
    async def load_database(path: str) -> Dict[str, Any]:
        """Load a signal database (DBC, ARXML, KCD, SYM or DBF) so frames are decoded into named signals."""
        return service.load_database(path)

    @mcp.tool()
    async def unload_database() -> Dict[str, Any]:
        """Forget the loaded database, so frames are shown as raw bytes again."""
        return service.unload_database()

    @mcp.tool()
    async def promote_to_dbc(
        path: str,
        identifier: Optional[str] = None,
        extended: bool = False,
        include_constant: bool = False,
        min_confidence: float = 0.0,
    ) -> Dict[str, Any]:
        """Write the fields the analysis finds (of one identifier, or all) as signals to a DBC at `path`, creating
        their messages. An existing database at the path is extended, not overwritten."""
        return service.promote_to_dbc(path, identifier, extended, include_constant, min_confidence)

    @mcp.tool()
    async def list_interfaces() -> List[Dict[str, Any]]:
        """The CAN interfaces python-can can detect on this machine."""
        return service.interfaces()

    # Diagnostics reads (diagnostics tier)

    if service.can_diagnose:

        @mcp.tool()
        async def query_obd(pid: int, timeout: float = 1.0) -> Dict[str, Any]:
            """Request one OBD-II value (service 01, e.g. pid 13 for vehicle speed, 12 for engine speed) with a
            single-frame request and return the ECU's decoded answer."""
            return await service.query_obd(pid, timeout)

        @mcp.tool()
        async def obd_status(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """The MIL ("check engine") state and the number of stored OBD-II trouble codes."""
            return await service.obd_status(request, response)

        @mcp.tool()
        async def obd_dtcs(pending: bool = False, request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """The stored (service 03) or pending (service 07) OBD-II trouble codes, decoded to P/C/B/U codes."""
            return await service.obd_dtcs(pending, request, response)

        @mcp.tool()
        async def obd_vin(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """The vehicle identification number (OBD service 09 PID 02)."""
            return await service.obd_vin(request, response)

        @mcp.tool()
        async def obd_supported(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """The OBD-II service-01 PIDs the ECU supports, of those this tool can decode."""
            return await service.obd_supported(request, response)

        @mcp.tool()
        async def obd_read(pids: List[int], request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """Read and decode OBD-II live values (service 01), e.g. pids [12, 13] for engine and vehicle speed."""
            return await service.obd_read(pids, request, response)

        @mcp.tool()
        async def uds_read_did(did: str, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            """Read a UDS data identifier (service 0x22), e.g. did "F190" for the VIN."""
            return await service.uds_read_did(did, request, response)

        @mcp.tool()
        async def uds_read_dtcs(status_mask: int = 0xFF, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            """Read UDS trouble codes (service 0x19 reportDTCByStatusMask)."""
            return await service.uds_read_dtcs(status_mask, request, response)

    # Active control (control tier)

    if service.can_control:

        @mcp.tool()
        async def obd_clear(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            """Clear the OBD-II trouble codes and turn the MIL off (service 04)."""
            return await service.obd_clear(request, response)

        @mcp.tool()
        async def uds_reset(reset_type: int = 1, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            """Reset an ECU (service 0x11; 1 = hard reset)."""
            return await service.uds_reset(reset_type, request, response)

        @mcp.tool()
        async def uds_routine(
            control_type: int, routine: str, data: str = '', request: str = '7E0', response: str = '7E8'
        ) -> Dict[str, Any]:
            """Routine control (service 0x31; control_type 1 = start, 2 = stop, 3 = request results)."""
            return await service.uds_routine(control_type, routine, data, request, response)

        @mcp.tool()
        async def uds_request(payload: str, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            """Send a raw UDS request (hex, e.g. "22 F1 90") and return the response."""
            return await service.uds_request(payload, request, response)

    # Transmitting (send tier)

    if service.allow_send:

        @mcp.tool()
        async def send_frame(identifier: str, data: str, extended: bool = False, fd: bool = False) -> Dict[str, Any]:
            """Send a single CAN frame with the given identifier and payload (hex). Transmits on the bus."""
            return service.send_frame(identifier, data, extended, fd)

        @mcp.tool()
        async def send_message(name: str, signals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            """Encode a database message by name from its signal values (e.g. {"EngineSpeed": 2500}) and send it."""
            return service.send_message(name, signals)

    # Resources: the same data, as readable documents

    @mcp.resource('can://status', mime_type='application/json')
    async def status_resource() -> str:
        return json.dumps(service.status(), indent=2)

    @mcp.resource('can://identifiers', mime_type='application/json')
    async def identifiers_resource() -> str:
        return json.dumps(service.identifiers(), indent=2)

    @mcp.resource('can://frames', mime_type='application/json')
    async def frames_resource() -> str:
        return json.dumps(service.frames(limit=100), indent=2)

    @mcp.resource('can://database', mime_type='application/json')
    async def database_resource() -> str:
        database = service.session.decoder.database
        if database is None:
            return json.dumps({'database': None, 'messages': []})
        messages = [
            {'id': f'{message.frame_id:X}', 'name': message.name, 'signals': [s.name for s in message.signals]}
            for message in database.messages()
        ]
        return json.dumps({'database': str(database.path), 'messages': messages}, indent=2)

    # Prompts: guided workflows

    @mcp.prompt()
    async def reverse_engineer_identifier(identifier: str) -> str:
        """Guide: reverse-engineer the signals of one identifier."""
        return (
            f"Reverse-engineer identifier {identifier} on the CAN bus.\n"
            f"1. Call timing to see how often {identifier} is sent (periodic or event-triggered).\n"
            f"2. Call analyze_identifier for {identifier} to find its fields (counters, checksums, values, flags).\n"
            "3. Use capture_reaction (after I do an action) or correlate (against a known reference) to pin down "
            "what a field means.\n"
            "4. Summarise the fields you found with their bit positions and likely meaning, and offer to "
            "promote_to_dbc."
        )

    @mcp.prompt()
    async def diagnose_trouble_codes() -> str:
        """Guide: read and explain the vehicle's trouble codes."""
        return (
            "Diagnose the vehicle over OBD-II.\n"
            "1. Call obd_status for the MIL state and code count, and obd_vin for the vehicle.\n"
            "2. Call obd_dtcs (stored) and obd_dtcs(pending=true) to list the trouble codes.\n"
            "3. Explain each code in plain language, its likely causes and severity.\n"
            "4. Read relevant live values with obd_read (e.g. coolant temperature, engine load) to support the "
            "diagnosis. Only clear codes with obd_clear if I explicitly ask."
        )

    @mcp.prompt()
    async def find_signal(description: str) -> str:
        """Guide: find which field carries a described signal."""
        return (
            f"Find the field on the bus that represents: {description}.\n"
            "If a reference series is available (a database signal, a CSV log, or values I give you), call "
            "correlate with it. Otherwise use related_signals to find fields that move together, and timing and "
            "analyze_identifier to narrow down candidates. Report the identifier, bit field, and fitted scale and "
            "offset of the best match."
        )

    return mcp
