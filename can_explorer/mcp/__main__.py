"""The `can-mcp` command: run the MCP server for one CAN bus over stdio, for use by an MCP client (an agent).

Example (read-only, bus given up front):
    can-mcp --interface socketcan --channel can0 --bitrate 500000 --database vehicle.dbc

Without --interface/--channel the server starts unconnected and the agent chooses a bus itself (list_interfaces
then connect). Add --allow-diagnostics / --allow-control / --allow-send to grant more. The `mcp` extra is needed.
"""

import asyncio
import logging
import sys

import click

from can_explorer.core.protocols import PROTOCOL_DECODERS
from can_explorer.mcp.service import CanService

logger = logging.getLogger(__name__)


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.option(
    '--interface', '-i', default=None, help='python-can interface (e.g. socketcan); omit to let the agent connect.'
)
@click.option(
    '--channel', '-c', default=None, help='Channel, e.g. can0, PCAN_USBBUS1, COM7; omit to let the agent connect.'
)
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
@click.option(
    '--allow-diagnostics', is_flag=True, help='Allow safe diagnostic reads (OBD and UDS reads; these send requests).'
)
@click.option(
    '--allow-control', is_flag=True, help='Allow active diagnostics (clear DTCs, ECU reset, routines, raw UDS).'
)
@click.option('--allow-send', is_flag=True, help='Allow the agent to transmit arbitrary frames (grants everything).')
def main(
    interface, channel, bitrate, fd, data_bitrate, database, protocol, allow_diagnostics, allow_control, allow_send
):
    """Run the CANExplorer MCP server for one bus over stdio."""
    if bool(interface) != bool(channel):
        raise SystemExit('Give both --interface and --channel, or neither (the agent connects with connect()).')
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
        allow_diagnostics=allow_diagnostics,
        allow_control=allow_control,
    )
    server = build_mcp(service)
    try:
        asyncio.run(_serve(service, server))
    except KeyboardInterrupt:
        pass


async def _serve(service: CanService, server) -> None:
    service.start()
    tier = (
        'send'
        if service.allow_send
        else 'control' if service.allow_control else 'diagnostics' if service.allow_diagnostics else 'read-only'
    )
    where = 'connected' if service.connected else 'not connected (the agent will connect)'
    logger.info('CANExplorer MCP server ready (%s, %s)', tier, where)
    try:
        await server.run_stdio_async()
    finally:
        service.stop()


if __name__ == '__main__':
    main()
