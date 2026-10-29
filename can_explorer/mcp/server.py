"""The MCP server: exposes a CanService (service.py) as MCP tools over stdio, using the MCP SDK (v2, MCPServer).

Needs the `mcp` package (the `mcp` extra). The tools are thin async wrappers around the service, so they run on
the event loop thread, in step with the bus receive pump. The transmitting tools (send_frame, query_obd) are only
registered when the service was started with sending allowed.
"""

from typing import Any, Dict, List, Optional

from mcp.server import MCPServer

from can_explorer.mcp.service import CanService

INSTRUCTIONS = """Inspect and (when allowed) drive a CAN bus.

Start with bus_status to see the bus state and how many frames and identifiers have been seen, and
list_identifiers for the identifiers with their cycle times and (if a database is loaded) names. get_frames
returns recent frames decoded into signals; decode_frame decodes a payload you supply. analyze_identifier runs
the automatic field analysis (checksums, counters, multiplexers, physical values) on an identifier's recent
frames. load_database loads a DBC (or ARXML/KCD/SYM/DBF) for decoding. Identifiers are hex strings such as "1A0".
"""


def build_mcp(service: CanService) -> MCPServer:
    mcp = MCPServer('CANExplorer', instructions=INSTRUCTIONS)

    @mcp.tool()
    async def bus_status() -> Dict[str, Any]:
        """The bus state, loaded database, and current frame counts, rates and bus load."""
        return service.status()

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
    async def analyze_identifier(
        identifier: str, extended: bool = False, include_constant: bool = False
    ) -> Dict[str, Any]:
        """Automatically find the fields of an identifier (checksums, counters, multiplexers, physical values,
        states and flags) from its recent frames."""
        return service.analyze(identifier, extended, include_constant)

    @mcp.tool()
    async def load_database(path: str) -> Dict[str, Any]:
        """Load a signal database (DBC, ARXML, KCD, SYM or DBF) so frames are decoded into named signals."""
        return service.load_database(path)

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

    if service.allow_send:

        @mcp.tool()
        async def send_frame(identifier: str, data: str, extended: bool = False, fd: bool = False) -> Dict[str, Any]:
            """Send a single CAN frame with the given identifier and payload (hex). Transmits on the bus."""
            return service.send_frame(identifier, data, extended, fd)

        @mcp.tool()
        async def query_obd(pid: int, timeout: float = 1.0) -> Dict[str, Any]:
            """Request one OBD-II value (service 01, e.g. pid 13 for vehicle speed, 12 for engine speed) and
            return the ECU's decoded answer. Transmits a diagnostic request on the bus."""
            return await service.query_obd(pid, timeout)

    return mcp
