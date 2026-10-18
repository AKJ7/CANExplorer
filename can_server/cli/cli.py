import logging

import click
from aiohttp import web

from can_server import PROJECT_NAME
from can_server.core.server import BusSpec, CanServer

logger = logging.getLogger(__name__)


def _buses(ctx, param, values):
    try:
        return [BusSpec.parse(value) for value in values]
    except ValueError as e:
        raise click.BadParameter(str(e)) from None


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.option(
    '--bus',
    'buses',
    multiple=True,
    required=True,
    callback=_buses,
    metavar='NAME=INTERFACE:CHANNEL[:BITRATE[:DATA_BITRATE]]',
    help='A bus to share, e.g. vehicle=socketcan:can0:500000 (repeat for several; a data bitrate enables CAN FD).',
)
@click.option('--host', default='0.0.0.0', show_default=True, help='Address to listen on.')
@click.option('--port', default=8765, show_default=True, help='TCP port.')
@click.option('--token', envvar='CAN_SERVER_TOKEN', help='Clients must present this token (or set CAN_SERVER_TOKEN).')
@click.option('-v', '--verbose', is_flag=True, help='Log every client connection and bus event.')
def cli(buses, host, port, token, verbose):
    """Share CAN interfaces of this machine with CANExplorer instances on the network.

    In CANExplorer, connect with the interface RemoteBus and the channel HOST:PORT/NAME
    (add ?token=TOKEN when the server requires one).
    """
    logging.basicConfig(
        format='%(asctime)s [%(levelname)s] %(message)s', level=logging.DEBUG if verbose else logging.INFO
    )
    if not token and host not in ('127.0.0.1', 'localhost', '::1'):
        logger.warning('No token set: anyone who can reach this machine can read and send on the buses')
    server = CanServer(buses, token)
    logger.info(f'Starting {PROJECT_NAME} on {host}:{port} with buses: {", ".join(spec.name for spec in buses)}')
    web.run_app(server.application(), host=host, port=port, print=None)
