"""The MCP server: exposes a CanService (service.py) as MCP tools, resources and prompts over stdio, using the MCP
SDK (v2, MCPServer).

Needs the `mcp` package (the `mcp` extra). The tools are thin async wrappers around the service, so they run on the
event loop thread, in step with the bus receive pump. Tools are registered by permission tier: the diagnostic
reads need the diagnostics tier, the active control tools the control tier, and the transmitting tools the send
tier (see CanService.can_diagnose / can_control / allow_send).

Every tool, resource and prompt is registered with an explicit name (what an agent calls it by), a title (what a
client shows a person) and a description (what the agent reads to decide when to use it). The names are the public
interface: renaming one breaks clients and scripts that call it.
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
get_frames returns recent frames decoded into signals; decode_frame and decode_protocol interpret a payload you
supply. timing shows which identifiers are periodic or event-triggered (series=true for the Δt over time). The
reverse-engineering tools: analyze_identifier (automatic fields) and bit_activity (raw per-bit behaviour) to see
what is in a frame; extract_field to decode and test a candidate bit range over time without a database;
related_signals, correlate, and capture_reaction (single action) or mark + reaction_at_markers (a repeated action)
to pin down meaning; promote_to_dbc to write found or hand-specified fields to a DBC. load_database loads a
DBC/ARXML/KCD/SYM/DBF. FlexRay is read from files: flexray_load_cluster (FIBEX/ARXML), then flexray_read_log,
flexray_analyze_log and flexray_decode on pcap, BLF or ASC logs. The resources can://status, can://identifiers, can://frames and can://database mirror this
as readable data; the prompts guide the common workflows. Identifiers are hex strings such as "1A0".

Diagnostics (OBD-II obd_*, UDS uds_* and J1939 j1939_request) and transmitting (send_frame, send_message,
j1939_send) are only available when the server was started with the matching permission; each tool's description
says which tier it needs. The request/response identifiers of the obd_* and uds_* tools take an ISO-TP address byte
after a colon for extended or mixed addressing, e.g. request "6F1:12", response "612:F1".
"""

# Appended to the description of each gated tool, so the agent knows what a call does to the bus
DIAGNOSTICS_TIER = ' Diagnostics tier (--allow-diagnostics): puts a request on the bus, changes nothing in the ECU.'
CONTROL_TIER = ' Control tier (--allow-control): changes the state of the ECU.'
SEND_TIER = ' Send tier (--allow-send): transmits on the bus.'


def build_mcp(service: CanService) -> MCPServer:  # noqa: C901 - a flat list of tool registrations
    mcp = MCPServer('CANExplorer', instructions=INSTRUCTIONS)

    # Read-only: inspection and reverse engineering

    @mcp.tool(
        name='bus_status',
        title='Bus status',
        description='The connection state, loaded database, enabled capabilities, and (when connected) frame '
        'counts, rates and bus load. If not connected, use list_interfaces and connect.',
    )
    async def bus_status() -> Dict[str, Any]:
        return service.status()

    if service.manages_connection:

        @mcp.tool(
            name='connect',
            title='Connect to a bus',
            description='Open a CAN bus and start receiving, closing any current one. interface is a python-can '
            'interface (socketcan, pcan, vector, gvret, virtual, canexplorer_remote…) and channel its channel '
            '(can0, PCAN_USBBUS1, COM7…). Keeps the database and protocol; starts a fresh trace.',
        )
        async def connect(
            interface: str, channel: str, bitrate: int = 500000, fd: bool = False, data_bitrate: Optional[int] = None
        ) -> Dict[str, Any]:
            return service.connect(interface, channel, bitrate=bitrate, fd=fd, data_bitrate=data_bitrate)

        @mcp.tool(name='disconnect', title='Disconnect', description='Close the current CAN bus.')
        async def disconnect() -> Dict[str, Any]:
            return service.disconnect()

    @mcp.tool(
        name='list_identifiers',
        title='List identifiers',
        description='Every identifier seen, with its direction, count, cycle time, last data and database name.',
    )
    async def list_identifiers(limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return service.identifiers(limit)

    @mcp.tool(
        name='get_frames',
        title='Recent frames',
        description='The most recent frames, newest last, optionally of one identifier, decoded into signals with '
        'the database.',
    )
    async def get_frames(
        identifier: Optional[str] = None, extended: bool = False, limit: int = 50, decode: bool = True
    ) -> List[Dict[str, Any]]:
        return service.frames(identifier, extended, limit, decode)

    @mcp.tool(
        name='decode_frame',
        title='Decode a frame',
        description='Decode a payload (hex, e.g. "A0 0F 5A 03") for an identifier with the loaded database. Sends '
        'nothing.',
    )
    async def decode_frame(identifier: str, data: str, extended: bool = False) -> Dict[str, Any]:
        return service.decode_frame(identifier, data, extended)

    # The protocol list is written out by hand: keep it in step with core/protocols/__init__.py PROTOCOL_DECODERS
    @mcp.tool(
        name='decode_protocol',
        title='Decode with a protocol',
        description='Interpret a payload with a protocol decoder (J1939, J1939-22, "NMEA 2000", CANopen, ISO-TP, '
        'UDS, KWP2000, "ISO-TP (extended)", "UDS (extended)", "KWP2000 (extended)" (extended or mixed addressing: '
        'the first data byte is an address byte), "K-Line", LIN, J1587, NM, SecOC, XCP, CCP), without a database. '
        'A single XCP or CCP frame is only labelled when it is a CONNECT (the decoder learns the identifier pair '
        'from a session start). Sends nothing.',
    )
    async def decode_protocol(protocol: str, identifier: str, data: str, extended: bool = False) -> Dict[str, Any]:
        return service.decode_protocol(protocol, identifier, data, extended)

    @mcp.tool(
        name='timing',
        title='Frame timing',
        description='The inter-frame timing of each identifier (or one): mean cycle time, jitter, and whether it is '
        'periodic or event-triggered. Spots gateways, bursts and event messages. With series=true, each row also '
        'carries a downsampled intervals_ms series of the inter-frame gaps over time.',
    )
    async def timing(
        identifier: Optional[str] = None, extended: bool = False, series: bool = False
    ) -> List[Dict[str, Any]]:
        return service.timing(identifier, extended, series)

    @mcp.tool(
        name='analyze_identifier',
        title='Analyse an identifier',
        description='Automatically find the fields of an identifier (checksums, counters, multiplexers, physical '
        'values, states and flags) from its recent frames. Secured PDUs of a loaded ARXML get their SecOC freshness '
        "value and MAC labelled (kinds 'freshness', 'mac'); without one, random-looking trailing bytes are labelled "
        "'random' (possibly a truncated MAC or encrypted data, a hint only).",
    )
    async def analyze_identifier(
        identifier: str, extended: bool = False, include_constant: bool = False
    ) -> Dict[str, Any]:
        return service.analyze(identifier, extended, include_constant)

    @mcp.tool(
        name='bit_activity',
        title='Bit activity',
        description='Per-bit behaviour of one identifier: for each bit how often it is set and flips (constant, '
        'toggling and counter bits stand out), plus a per-byte summary (distinct values, range, entropy). The raw '
        'view behind analyze_identifier — use it when a byte does not parse as a clean field.',
    )
    async def bit_activity(identifier: str, extended: bool = False) -> Dict[str, Any]:
        return service.bit_activity(identifier, extended)

    @mcp.tool(
        name='extract_field',
        title='Extract a field',
        description='Decode a candidate field (a bit range with byte order, sign and scaling) over the recent frames '
        'without a database, to test a hypothesis: returns the value range, how often it changes, and a downsampled '
        'value series over time. start is the least significant bit (little_endian) or most significant bit '
        '(big_endian). Confirm a field here, then write it with promote_to_dbc(fields=[…]).',
    )
    async def extract_field(
        identifier: str,
        start: int,
        length: int,
        byte_order: str = 'little_endian',
        signed: bool = False,
        scale: float = 1.0,
        offset: float = 0.0,
        extended: bool = False,
        max_samples: int = 200,
    ) -> Dict[str, Any]:
        return service.extract_field(
            identifier, start, length, byte_order, signed, scale, offset, extended, max_samples
        )

    @mcp.tool(
        name='mark',
        title='Mark the moment',
        description="Mark the current moment (the newest frame's time) to line up with a physical action. Mark the "
        'same action a few times, then call reaction_at_markers to find what reacts.',
    )
    async def mark(label: str = '') -> Dict[str, Any]:
        return service.mark(label)

    @mcp.tool(name='clear_markers', title='Clear markers', description='Forget all the markers set with mark().')
    async def clear_markers() -> Dict[str, Any]:
        return service.clear_markers()

    @mcp.tool(
        name='reaction_at_markers',
        title='Reaction at markers',
        description='The fields that change around the moments set with mark() — most specific first — with how '
        'many markers each hits and its background change rate, to tell a real reaction from coincidence. '
        'Passive — reads only.',
    )
    async def reaction_at_markers(before: float = 0.5, after: float = 1.5, top: int = 30) -> Dict[str, Any]:
        return service.reaction_at_markers(before, after, top)

    @mcp.tool(
        name='related_signals',
        title='Related signals',
        description='Groups of fields that carry the same signal across the bus (the same quantity rebroadcast, or a '
        'scaled or lagged copy), found without a database.',
    )
    async def related_signals(top: int = 20, min_correlation: float = 0.99) -> Dict[str, Any]:
        return service.related_signals(top, min_correlation)

    @mcp.tool(
        name='capture_reaction',
        title='Capture a reaction',
        description='Diff the most recent `action_seconds` of traffic against the `baseline_seconds` before it to '
        'find what changed on the bus (identifiers that appeared, bits that changed). Record a steady baseline, '
        'perform the action, wait `action_seconds`, then call this. Passive — reads only.',
    )
    async def capture_reaction(baseline_seconds: float = 5.0, action_seconds: float = 5.0) -> Dict[str, Any]:
        return service.capture_reaction(baseline_seconds, action_seconds)

    @mcp.tool(
        name='correlate',
        title='Correlate with a reference',
        description='Find the field that follows a known reference series: a database signal ("Message.Signal"), a '
        'column of a CSV file (csv_path, column), or an inline time series (times and values). Returns the '
        "best-matching fields with their correlation, fitted factor and offset (into the reference's unit) and "
        'time lag.',
    )
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

    @mcp.tool(
        name='flexray_load_cluster',
        title='Load a FlexRay cluster',
        description='Load a FlexRay cluster description (FIBEX or AUTOSAR ARXML; `cluster` picks one of several) '
        'for the flexray_* tools: returns its timing parameters and frames (slot, cycles, channels, PDUs with '
        'offset, update bit and signals). FlexRay is read from files, separately from the CAN bus.',
    )
    async def flexray_load_cluster(path: str, cluster: Optional[str] = None) -> Dict[str, Any]:
        return service.flexray_load_cluster(path, cluster)

    @mcp.tool(
        name='flexray_read_log',
        title='Read a FlexRay log',
        description='The FlexRay frames and events (symbols, controller states, errors) of a pcap/pcapng '
        '(LINKTYPE_FLEXRAY or IEEE 1722), Vector BLF or ASC file, optionally of one slot or channel (A/B), decoded '
        'into PDUs and signals with the loaded cluster (PDUs whose update bit is clear show "not updated").',
    )
    async def flexray_read_log(
        path: str, slot: Optional[int] = None, channel: Optional[str] = None, limit: int = 50, decode: bool = True
    ) -> Dict[str, Any]:
        return service.flexray_read_log(path, slot, channel, limit, decode)

    @mcp.tool(
        name='flexray_analyze_log',
        title='Check a FlexRay log',
        description='Check a FlexRay log against the loaded cluster: missing, undescribed and wrong-length frames, '
        'static slot timing, dynamic slot order, channel A/B mismatches, and the bus health per channel (null and '
        'erroneous frames, sync frames per cycle, cycle counter jumps, cycle time) and its events.',
    )
    async def flexray_analyze_log(path: str) -> Dict[str, Any]:
        return service.flexray_analyze_log(path)

    @mcp.tool(
        name='flexray_decode',
        title='Decode a FlexRay payload',
        description='Decode a FlexRay payload (hex) of a slot, cycle (0-63) and channel with the loaded cluster '
        "description: the frame and its PDUs' signal values. Sends nothing.",
    )
    async def flexray_decode(slot: int, cycle: int, data: str, channel: str = 'A') -> Dict[str, Any]:
        return service.flexray_decode(slot, cycle, data, channel)

    @mcp.tool(
        name='load_database',
        title='Load a database',
        description='Load a signal database (DBC, ARXML, KCD, SYM or DBF) so frames are decoded into named signals.',
    )
    async def load_database(path: str) -> Dict[str, Any]:
        return service.load_database(path)

    @mcp.tool(
        name='unload_database',
        title='Unload the database',
        description='Forget the loaded database, so frames are shown as raw bytes again.',
    )
    async def unload_database() -> Dict[str, Any]:
        return service.unload_database()

    @mcp.tool(
        name='secoc_status',
        title='SecOC status',
        description='The AUTOSAR SecOC secured PDUs of the loaded ARXML database and the outcome of their live '
        'checks: per PDU its Data ID, trailer lengths, the key store entry verifying it (never the key) or why it '
        'is not checked, and counts of frames verified ok, MAC errors and freshness (replay) errors, with the last '
        'result.',
    )
    async def secoc_status() -> Dict[str, Any]:
        return service.secoc_status()

    @mcp.tool(
        name='promote_to_dbc',
        title='Write fields to a DBC',
        description='Write signals to a DBC at `path`, creating their messages (an existing database is extended, '
        'not overwritten). Without `fields`, the fields the analysis finds (of one identifier, or all) are written. '
        'With `fields`, exactly the given specs are written — a hypothesis you confirmed with extract_field: each '
        'is {identifier, start, length, byte_order?, signed?, scale?, offset?, name?, unit?, comment?, extended?}.',
    )
    async def promote_to_dbc(
        path: str,
        identifier: Optional[str] = None,
        extended: bool = False,
        include_constant: bool = False,
        min_confidence: float = 0.0,
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        return service.promote_to_dbc(path, identifier, extended, include_constant, min_confidence, fields)

    @mcp.tool(
        name='list_interfaces',
        title='List interfaces',
        description='The CAN interfaces python-can can detect on this machine.',
    )
    async def list_interfaces() -> List[Dict[str, Any]]:
        return service.interfaces()

    # Diagnostics reads (diagnostics tier)

    if service.can_diagnose:

        @mcp.tool(
            name='query_obd',
            title='Query an OBD-II value',
            description='Request one OBD-II value (service 01, e.g. pid 13 for vehicle speed, 12 for engine speed) '
            "with a single-frame request and return the ECU's decoded answer." + DIAGNOSTICS_TIER,
        )
        async def query_obd(pid: int, timeout: float = 1.0) -> Dict[str, Any]:
            return await service.query_obd(pid, timeout)

        @mcp.tool(
            name='obd_status',
            title='OBD-II status',
            description='The MIL ("check engine") state and the number of stored OBD-II trouble codes.'
            + DIAGNOSTICS_TIER,
        )
        async def obd_status(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_status(request, response)

        @mcp.tool(
            name='obd_dtcs',
            title='OBD-II trouble codes',
            description='The stored (service 03) or pending (service 07) OBD-II trouble codes, decoded to P/C/B/U '
            'codes.' + DIAGNOSTICS_TIER,
        )
        async def obd_dtcs(pending: bool = False, request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_dtcs(pending, request, response)

        @mcp.tool(
            name='obd_vin',
            title='Vehicle identification number',
            description='The vehicle identification number (OBD service 09 PID 02).' + DIAGNOSTICS_TIER,
        )
        async def obd_vin(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_vin(request, response)

        @mcp.tool(
            name='obd_supported',
            title='Supported OBD-II PIDs',
            description='The OBD-II service-01 PIDs the ECU supports, of those this tool can decode.'
            + DIAGNOSTICS_TIER,
        )
        async def obd_supported(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_supported(request, response)

        @mcp.tool(
            name='obd_read',
            title='Read OBD-II live values',
            description='Read and decode OBD-II live values (service 01), e.g. pids [12, 13] for engine and vehicle '
            'speed.' + DIAGNOSTICS_TIER,
        )
        async def obd_read(pids: List[int], request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_read(pids, request, response)

        @mcp.tool(
            name='uds_read_did',
            title='Read a UDS data identifier',
            description='Read a UDS data identifier (service 0x22), e.g. did "F190" for the VIN.' + DIAGNOSTICS_TIER,
        )
        async def uds_read_did(did: str, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_read_did(did, request, response)

        @mcp.tool(
            name='uds_read_dtcs',
            title='Read UDS trouble codes',
            description='Read UDS trouble codes (service 0x19 reportDTCByStatusMask).' + DIAGNOSTICS_TIER,
        )
        async def uds_read_dtcs(status_mask: int = 0xFF, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_read_dtcs(status_mask, request, response)

        @mcp.tool(
            name='uds_dtc_details',
            title="Read a DTC's snapshot and extended data",
            description='Read the snapshot (freeze frame) and extended data records of one DTC (service 0x19 0x04 and '
            '0x06), e.g. dtc "012345". Records whose lengths are unknown are returned undecoded.' + DIAGNOSTICS_TIER,
        )
        async def uds_dtc_details(dtc: str, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_dtc_details(dtc, request, response)

        @mcp.tool(
            name='uds_find_ecus',
            title='Find the ECUs on the bus',
            description='Send a functional TesterPresent (0x7DF, or 0x18DB33F1 with extended=true) and list the '
            'response identifiers of the ECUs that answer.' + DIAGNOSTICS_TIER,
        )
        async def uds_find_ecus(extended: bool = False, timeout: float = 0.5) -> Dict[str, Any]:
            return await service.uds_find_ecus(extended, timeout)

        @mcp.tool(
            name='j1939_request',
            title='Request a J1939 parameter group',
            description='Request a J1939 parameter group (PGN 59904) from one node (destination, hex address) or all '
            '(FF) and return the answers: the data, reassembled from BAM, RTS/CTS or ETP (the tool answers transport '
            'sessions to its source address), decoded for address claims (NAMEs), DM1/DM2 and identification text; '
            'or the acknowledgment (e.g. NACK) of a node that does not send the group. pgn: a number or a name such '
            'as "Address Claimed", "DM2", "Software Identification". A request to all nodes collects answers for '
            '`timeout` seconds. Requesting DM3 or DM11 clears trouble codes and needs the control tier.'
            + DIAGNOSTICS_TIER,
        )
        async def j1939_request(
            pgn: str, destination: str = 'FF', source: str = 'F9', timeout: float = 1.25
        ) -> Dict[str, Any]:
            return await service.j1939_request(pgn, destination, source, timeout)

    # XCP / CCP measurement and calibration

    @mcp.tool(
        name='xcp_list_variables',
        title='List A2L variables',
        description='The measurements, characteristics (values, curves, maps, blocks, strings) and axes of an A2L '
        'file: name, kind, address, type, unit, limits, verbal values, and why one cannot be read. a2l loads the '
        'file (kept for the other xcp_* tools); pattern filters by name (glob or substring), kind by kind '
        '(MEASUREMENT, VALUE, CURVE, MAP, VAL_BLK, ASCII, AXIS_PTS). Sends nothing.',
    )
    async def xcp_list_variables(a2l: str = '', pattern: str = '', kind: str = '', limit: int = 200) -> Dict[str, Any]:
        return service.xcp_list_variables(a2l, pattern, kind, limit)

    if service.can_diagnose:

        @mcp.tool(
            name='xcp_connect',
            title='Connect to an XCP or CCP slave',
            description='Connect to an ECU\'s XCP slave (transport CAN, "CAN FD", UDP or TCP with host/port) or '
            'CCP slave (CAN, station address, byte order), and keep the session for the other xcp_* tools. '
            'request/response: the CAN identifiers (hex). key: a seed & key algorithm (Python file, '
            '"keystore:<entry>" or "profile:<name>") used when a command needs a locked resource. a2l: the ECU '
            'description; the answer says whether it matches the ECU (EPK).' + DIAGNOSTICS_TIER,
        )
        async def xcp_connect(
            protocol: str = 'XCP',
            transport: str = 'CAN',
            request: str = '7E0',
            response: str = '7E1',
            extended: bool = False,
            station: str = '0',
            byte_order: str = 'big',
            host: str = '',
            port: int = 5555,
            key: str = '',
            a2l: str = '',
        ) -> Dict[str, Any]:
            return await service.xcp_connect(
                protocol, transport, request, response, extended, station, byte_order, host, port, key, a2l
            )

        @mcp.tool(name='xcp_disconnect', title='Disconnect the XCP/CCP slave', description='End the XCP/CCP session.'
                  + DIAGNOSTICS_TIER)  # fmt: skip
        async def xcp_disconnect() -> Dict[str, Any]:
            return await service.xcp_disconnect()

        @mcp.tool(
            name='xcp_status',
            title='XCP/CCP session status',
            description='The XCP/CCP session: protocol, byte order, address granularity, packet sizes, locked '
            'resources, and the EPK check of the loaded A2L.' + DIAGNOSTICS_TIER,
        )
        async def xcp_status() -> Dict[str, Any]:
            return await service.xcp_status()

        @mcp.tool(
            name='xcp_read',
            title='Read A2L variables',
            description='Read A2L variables by name over XCP/CCP and convert them to physical values: scalars, '
            'arrays, value blocks, strings, curves and maps (values[y][x]) with their axis points.' + DIAGNOSTICS_TIER,
        )
        async def xcp_read(names: List[str]) -> Dict[str, Any]:
            return await service.xcp_read(names)

        @mcp.tool(
            name='xcp_read_memory',
            title='Read ECU memory',
            description="Read 1 to 4096 bytes of the ECU's memory at a hex address (with an address extension) "
            'over XCP/CCP.' + DIAGNOSTICS_TIER,
        )
        async def xcp_read_memory(address: str, size: int, extension: int = 0) -> Dict[str, Any]:
            return await service.xcp_read_memory(address, size, extension)

        @mcp.tool(
            name='xcp_measure',
            title='Measure A2L measurements',
            description='Measure A2L measurements for a duration (seconds, at most 30) — by DAQ on an event channel '
            '(default the first A2L event) when the ECU supports it, else by polling — and return per measurement '
            'the number of samples, min, max, mean and last value. Configures DAQ lists in the ECU (measurement '
            'only, no calibration change).' + DIAGNOSTICS_TIER,
        )
        async def xcp_measure(names: List[str], duration: float = 1.0, event: Optional[int] = None) -> Dict[str, Any]:
            return await service.xcp_measure(names, duration, event)

        @mcp.tool(
            name='xcp_daq_info',
            title='XCP DAQ configuration',
            description="The XCP slave's DAQ configuration: dynamic or static, its DAQ lists (direction, size, fixed "
            'event) and what its predefined lists carry (from the A2L, or read with READ_DAQ), its events, and '
            'whether it offers STIM. Reads only.' + DIAGNOSTICS_TIER,
        )
        async def xcp_daq_info() -> Dict[str, Any]:
            return await service.xcp_daq_info()

        @mcp.tool(
            name='xcp_flash_info',
            title='XCP programming capabilities',
            description='Whether the XCP slave offers flash programming (PGM), whether it is locked, its PGM '
            'properties (absolute/functional erasing, compression and encryption supported or required) and its '
            'flash sectors. Reads only.' + DIAGNOSTICS_TIER,
        )
        async def xcp_flash_info() -> Dict[str, Any]:
            return await service.xcp_flash_info()

    # Active control (control tier)

    if service.can_control:

        @mcp.tool(
            name='xcp_write',
            title='Write a calibration value',
            description='Write an A2L characteristic over XCP/CCP and read it back: value is a number, a verbal '
            'value, a list (curves, blocks, axis points), a list of rows (maps, values[y][x]) or a string (ASCII); '
            'axes optionally rewrites the points of standard axes. Values outside the A2L limits are refused. '
            'Changes the running ECU immediately.' + CONTROL_TIER,
        )
        async def xcp_write(name: str, value: Any, axes: Optional[List[Any]] = None) -> Dict[str, Any]:
            return await service.xcp_write(name, value, axes)

        @mcp.tool(
            name='xcp_write_memory',
            title='Write ECU memory',
            description="Write raw bytes (hex) to the ECU's memory at a hex address over XCP/CCP. Changes the "
            'running ECU immediately; prefer xcp_write, which checks the A2L limits.' + CONTROL_TIER,
        )
        async def xcp_write_memory(address: str, data: str, extension: int = 0) -> Dict[str, Any]:
            return await service.xcp_write_memory(address, data, extension)

        @mcp.tool(
            name='xcp_stimulate',
            title='Stimulate calibration values (XCP STIM)',
            description='Write scalar A2L calibration values by XCP STIM: the ECU writes them at an event channel '
            '(default the first A2L event) for duration seconds (at most 30), then STIM stops and the values are '
            'read back. values maps names to numbers or verbal values; values outside the A2L limits are refused. '
            'Reports STIM timeouts the ECU signalled. Changes the running ECU immediately.' + CONTROL_TIER,
        )
        async def xcp_stimulate(
            values: Dict[str, Any], event: Optional[int] = None, duration: float = 1.0
        ) -> Dict[str, Any]:
            return await service.xcp_stimulate(values, event, duration)

        @mcp.tool(
            name='xcp_program',
            title='Program the ECU flash over XCP',
            description='Replace software in the ECU: program an Intel HEX, S-record or binary file (path; '
            'base_address hex for a binary) into its flash over XCP PGM, then check it and reset the ECU. erase: '
            '"sectors" (every sector the file touches, default), "segments" (the file ranges), "functional" '
            '(clear_areas bit mask: 1 calibration, 2 code, 4 NVRAM) or "none". verify compares BUILD_CHECKSUM '
            'with the file; ecu_check "internal" or "value" (check_type, check_value hex) sends PROGRAM_VERIFY. '
            'kernel: a flash kernel file downloaded first. compression/encryption: the manufacturer method codes '
            '(PROGRAM_FORMAT) the file was prepared with, or that encoder (a Python file defining encode(data, '
            'address)) applies. program_max sends full packets as PROGRAM_MAX. reset ends the session. A wrong '
            'file can leave the ECU unable to start: only program a file meant for this ECU, and only when the user '
            'asked for it.' + CONTROL_TIER,
        )
        async def xcp_program(
            path: str,
            erase: str = 'sectors',
            verify: bool = True,
            reset: bool = True,
            base_address: str = '0',
            clear_areas: int = 2,
            kernel: str = '',
            compression: int = 0,
            encryption: int = 0,
            encoder: str = '',
            program_max: bool = False,
            ecu_check: str = 'none',
            check_type: int = 4,
            check_value: str = '0',
        ) -> Dict[str, Any]:
            return await service.xcp_program(
                path, erase=erase, verify=verify, reset=reset, base_address=base_address, clear_areas=clear_areas,
                kernel=kernel, compression=compression, encryption=encryption, encoder=encoder,
                program_max=program_max, ecu_check=ecu_check, check_type=check_type, check_value=check_value,
            )  # fmt: skip

        @mcp.tool(
            name='obd_clear',
            title='Clear OBD-II trouble codes',
            description='Clear the OBD-II trouble codes and turn the MIL off (service 04).' + CONTROL_TIER,
        )
        async def obd_clear(request: str = '7DF', response: str = '7E8') -> Dict[str, Any]:
            return await service.obd_clear(request, response)

        @mcp.tool(
            name='uds_reset',
            title='Reset an ECU',
            description='Reset an ECU (service 0x11; 1 = hard reset).' + CONTROL_TIER,
        )
        async def uds_reset(reset_type: int = 1, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_reset(reset_type, request, response)

        @mcp.tool(
            name='uds_routine',
            title='UDS routine control',
            description='Routine control (service 0x31; control_type 1 = start, 2 = stop, 3 = request results).'
            + CONTROL_TIER,
        )
        async def uds_routine(
            control_type: int, routine: str, data: str = '', request: str = '7E0', response: str = '7E8'
        ) -> Dict[str, Any]:
            return await service.uds_routine(control_type, routine, data, request, response)

        @mcp.tool(
            name='uds_request',
            title='Raw UDS request',
            description='Send a raw UDS request (hex, e.g. "22 F1 90") and return the response. Any service, so it '
            'may change the state of the ECU.' + CONTROL_TIER,
        )
        async def uds_request(payload: str, request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_request(payload, request, response)

        @mcp.tool(
            name='uds_secured_request',
            title='Secured UDS request (0x84)',
            description='Send a UDS request (hex, e.g. "22 F1 90") inside a SecuredDataTransmission (0x84) envelope '
            '(ISO 14229-1:2020) and return the unwrapped response, with both envelopes, the anti-replay counters and '
            "whether the response signature verified. key: a secured data key of the key store (CANExplorer's "
            'reference scheme, a MAC over the message; signature_length truncates it) or a Python provider file with '
            "the manufacturer's scheme; 'session' uses the session key of the last uds_authenticate on this "
            'request id; empty sends it unsigned. The counter continues per request id unless given. '
            'The algorithms behind the algorithm byte are manufacturer specific.' + CONTROL_TIER,
        )
        async def uds_secured_request(
            payload: str,
            key: str = '',
            sign: bool = True,
            encrypt: bool = False,
            signed_response: bool = False,
            algorithm: int = 0,
            signature_length: int = 0,
            counter: Optional[int] = None,
            request: str = '7E0',
            response: str = '7E8',
        ) -> Dict[str, Any]:
            return await service.uds_secured_request(
                payload, key, sign, encrypt, signed_response, algorithm, signature_length, counter, request, response
            )

        @mcp.tool(
            name='uds_authenticate',
            title='UDS Authentication (0x29)',
            description='Authenticate on an ECU with service 0x29 (ISO 14229-1:2020). method: "certificate" '
            '(certificate exchange; credentials = a client certificate entry of the key store or "profile:<name>"), '
            '"challenge-response" (credentials = a shared authentication key, a client certificate entry or '
            '"profile:<name>"; algorithm = the algorithmIndicator in hex or as a dotted OID; additional = hex) or '
            '"configuration" (only asks which authentication the ECU offers). bidirectional also checks the ECU, '
            'against the trusted-certificates entry `trusted` if given. A session key asked for with '
            'communication_configuration is kept for uds_secured_request(key="session"). Returns the steps, '
            "and the certificate's private extensions (where an ECU may read the tester's role and white lists). "
            'The key store is unlocked with CANEXPLORER_KEYSTORE_PASSWORD.' + CONTROL_TIER,
        )
        async def uds_authenticate(
            method: str = 'certificate',
            credentials: str = '',
            scheme: str = '',
            bidirectional: bool = False,
            trusted: str = '',
            algorithm: str = '',
            additional: str = '',
            communication_configuration: int = 0,
            request: str = '7E0',
            response: str = '7E8',
        ) -> Dict[str, Any]:
            return await service.uds_authenticate(
                method,
                credentials,
                scheme,
                bidirectional,
                trusted,
                algorithm,
                additional,
                communication_configuration,
                request,
                response,
            )

        @mcp.tool(
            name='uds_deauthenticate',
            title='UDS deAuthenticate',
            description='End the authentication on an ECU (0x29 deAuthenticate) and forget its session key.'
            + CONTROL_TIER,
        )
        async def uds_deauthenticate(request: str = '7E0', response: str = '7E8') -> Dict[str, Any]:
            return await service.uds_deauthenticate(request, response)

        @mcp.tool(
            name='uds_transmit_certificate',
            title='UDS transmitCertificate',
            description='Send a general-purpose certificate to an ECU (0x29 transmitCertificate): a certificate entry '
            'of the key store or a PEM/DER file, for a 16-bit certificate evaluation id (manufacturer specific: what '
            'the certificate is for, e.g. secure coding, SecOC or TLS).' + CONTROL_TIER,
        )
        async def uds_transmit_certificate(
            certificate: str, evaluation_id: str, request: str = '7E0', response: str = '7E8'
        ) -> Dict[str, Any]:
            return await service.uds_transmit_certificate(certificate, evaluation_id, request, response)

    # Transmitting (send tier)

    if service.allow_send:

        @mcp.tool(
            name='send_frame',
            title='Send a frame',
            description='Send a single CAN frame with the given identifier and payload (hex).' + SEND_TIER,
        )
        async def send_frame(identifier: str, data: str, extended: bool = False, fd: bool = False) -> Dict[str, Any]:
            return service.send_frame(identifier, data, extended, fd)

        @mcp.tool(
            name='j1939_send',
            title='Send a J1939 parameter group',
            description='Send a J1939 parameter group (data in hex) from the source address: up to 8 bytes as one '
            'frame, more with the transport protocol: BAM to all nodes (destination FF, at most 1785 bytes), RTS/CTS '
            'to one node, or the extended transport protocol (ETP) to one node for more than 1785 bytes. Groups from '
            'PGN 0xF000 (PDU2) always go to all nodes.' + SEND_TIER,
        )
        async def j1939_send(
            pgn: str, data: str, destination: str = 'FF', priority: int = 6, source: str = 'F9'
        ) -> Dict[str, Any]:
            return await service.j1939_send(pgn, data, destination, priority, source)

        @mcp.tool(
            name='send_message',
            title='Send a database message',
            description='Encode a database message by name from its signal values (e.g. {"EngineSpeed": 2500}) and '
            'send it.' + SEND_TIER,
        )
        async def send_message(name: str, signals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            return service.send_message(name, signals)

    # Resources: the same data, as readable documents

    @mcp.resource(
        'can://status',
        name='status',
        title='Bus status',
        description='The connection state, loaded database, enabled capabilities, frame counts, rates and bus load '
        '(the bus_status tool, as a document).',
        mime_type='application/json',
    )
    async def status_resource() -> str:
        return json.dumps(service.status(), indent=2)

    @mcp.resource(
        'can://identifiers',
        name='identifiers',
        title='Identifiers',
        description='Every identifier seen, with its direction, count, cycle time, last data and database name (the '
        'list_identifiers tool, as a document).',
        mime_type='application/json',
    )
    async def identifiers_resource() -> str:
        return json.dumps(service.identifiers(), indent=2)

    @mcp.resource(
        'can://frames',
        name='frames',
        title='Recent frames',
        description='The 100 most recent frames, newest last, decoded into signals with the loaded database (the '
        'get_frames tool, as a document).',
        mime_type='application/json',
    )
    async def frames_resource() -> str:
        return json.dumps(service.frames(limit=100), indent=2)

    @mcp.resource(
        'can://database',
        name='database',
        title='Signal database',
        description='The loaded signal database: its path and every message with its identifier, name and signal '
        'names; null and no messages when none is loaded.',
        mime_type='application/json',
    )
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

    @mcp.prompt(
        name='reverse_engineer_identifier',
        title='Reverse-engineer an identifier',
        description='Guide: reverse-engineer the signals of one identifier — its timing, automatic field analysis, '
        'pinning down meaning with a reaction or a reference, and writing the result to a DBC.',
    )
    async def reverse_engineer_identifier(identifier: str) -> str:
        return (
            f'Reverse-engineer identifier {identifier} on the CAN bus.\n'
            f'1. Call timing to see how often {identifier} is sent (periodic or event-triggered).\n'
            f'2. Call analyze_identifier for {identifier} to find its fields (counters, checksums, values, flags).\n'
            '3. Use capture_reaction (after I do an action) or correlate (against a known reference) to pin down '
            'what a field means.\n'
            '4. Summarise the fields you found with their bit positions and likely meaning, and offer to '
            'promote_to_dbc.'
        )

    @mcp.prompt(
        name='diagnose_trouble_codes',
        title='Diagnose trouble codes',
        description="Guide: read and explain the vehicle's OBD-II trouble codes, backed by live values. Needs the "
        'diagnostics tier; codes are only cleared on request (control tier).',
    )
    async def diagnose_trouble_codes() -> str:
        return (
            'Diagnose the vehicle over OBD-II.\n'
            '1. Call obd_status for the MIL state and code count, and obd_vin for the vehicle.\n'
            '2. Call obd_dtcs (stored) and obd_dtcs(pending=true) to list the trouble codes.\n'
            '3. Explain each code in plain language, its likely causes and severity.\n'
            '4. Read relevant live values with obd_read (e.g. coolant temperature, engine load) to support the '
            'diagnosis. Only clear codes with obd_clear if I explicitly ask.'
        )

    @mcp.prompt(
        name='find_signal',
        title='Find a signal',
        description='Guide: find which field carries a described signal, by correlating with a reference or by '
        'narrowing down candidates without one.',
    )
    async def find_signal(description: str) -> str:
        return (
            f'Find the field on the bus that represents: {description}.\n'
            'If a reference series is available (a database signal, a CSV log, or values I give you), call '
            'correlate with it. Otherwise use related_signals to find fields that move together, and timing and '
            'analyze_identifier to narrow down candidates. Report the identifier, bit field, and fitted scale and '
            'offset of the best match.'
        )

    return mcp
