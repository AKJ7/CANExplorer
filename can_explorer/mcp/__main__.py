"""The `can-mcp` command: run the MCP server for one CAN bus over stdio, for use by an MCP client (an agent).

Example (read-only):
    can-mcp --interface socketcan --channel can0 --bitrate 500000 --database vehicle.dbc

Add --allow-send to let the agent transmit (send_frame, query_obd). The `mcp` extra must be installed.
"""

import asyncio
import logging
import sys

import click

from can_explorer.core.protocols import PROTOCOL_DECODERS
from can_explorer.mcp.service import CanService

logger = logging.getLogger(__name__)


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.option('--interface', '-i', required=True, help='python-can interface, e.g. socketcan, pcan, virtual, gvret.')
@click.option('--channel', '-c', required=True, help='Channel, e.g. can0, PCAN_USBBUS1, COM7.')
@click.option('--bitrate', '-b', default=500_000, show_default=True, help='Nominal bitrate.')
@click.option('--fd', is_flag=True, help='CAN FD.')
@click.option('--data-bitrate', type=int, default=None, help='CAN FD data bitrate (implies --fd).')
@click.option('--database', '-d', type=click.Path(exists=True, dir_okay=False), help='Signal database (DBC, …).')
@click.option(
    '--protocol',
    default='Raw',
    show_default=True,
    type=click.Choice(list(PROTOCOL_DECODERS)),
    help='Protocol decoder for the frames.',
)
@click.option('--allow-send', is_flag=True, help='Allow the agent to transmit frames (send_frame, query_obd).')
def main(interface, channel, bitrate, fd, data_bitrate, database, protocol, allow_send):
    """Run the CANExplorer MCP server for one bus over stdio."""
    # Logs must not go to stdout: stdout carries the MCP protocol.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        from can_explorer.mcp.server import build_mcp
    except ImportError as e:
        raise SystemExit("The MCP SDK is not installed. Install it with:  pip install 'can-explorer[mcp]'") from e

    service = CanService.from_config(
        interface,
        channel,
        bitrate=bitrate,
        fd=fd or data_bitrate is not None,
        data_bitrate=data_bitrate,
        database=database,
        protocol=protocol,
        allow_send=allow_send,
    )
    server = build_mcp(service)
    try:
        asyncio.run(_serve(service, server))
    except KeyboardInterrupt:
        pass


async def _serve(service: CanService, server) -> None:
    service.start()
    logger.info('CANExplorer MCP server ready (%s)', 'read-write' if service.allow_send else 'read-only')
    try:
        await server.run_stdio_async()
    finally:
        service.stop()


if __name__ == '__main__':
    main()
