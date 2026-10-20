import logging
from pathlib import Path

import click
from aiohttp import web

from can_explorer.core.mqtt import MqttSettings
from can_server import PROJECT_NAME
from can_server.core.server import BusSpec, CanServer

logger = logging.getLogger(__name__)


def _buses(ctx, param, values):
    try:
        return [BusSpec.parse(value) for value in values]
    except ValueError as e:
        raise click.BadParameter(str(e)) from None


def _databases(ctx, param, values):
    databases = {}
    for value in values:
        name, separator, path = value.partition('=')
        if not separator or not name.strip() or not path:
            raise click.BadParameter(f'Invalid database {value!r}; expected NAME=FILE')
        if not Path(path).is_file():
            raise click.BadParameter(f'No such file: {path}')
        databases[name.strip()] = Path(path)
    return databases


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
@click.option(
    '--database',
    'databases',
    multiple=True,
    callback=_databases,
    metavar='NAME=FILE',
    help='Signal database of a bus (DBC, DBF, KCD, SYM, ARXML, LDF), to publish its signals over MQTT.',
)
@click.option(
    '--mqtt',
    'mqtt_url',
    envvar='CAN_SERVER_MQTT',
    metavar='mqtt[s]://[USER[:PASSWORD]@]HOST[:PORT][/PREFIX]',
    help='Publish the buses to this MQTT broker (or set CAN_SERVER_MQTT); the buses are then always open.',
)
@click.option('--mqtt-frames', is_flag=True, help='Also publish the frames (topic PREFIX/BUS/frames).')
@click.option('--mqtt-retain', is_flag=True, help='Retain the signal values on the broker.')
@click.option(
    '--mqtt-interval', default=0.1, show_default=True, help='Seconds between two values of a signal, at most.'
)
@click.option(
    '--mqtt-send',
    'mqtt_send',
    multiple=True,
    metavar='MESSAGE|ID|*',
    help='Send the frames published to PREFIX/BUS/send for this message name or hex identifier (repeatable; '
    '* allows all). Off by default.',
)
@click.option('-v', '--verbose', is_flag=True, help='Log every client connection and bus event.')
def cli(buses, host, port, token, databases, mqtt_url, mqtt_frames, mqtt_retain, mqtt_interval, mqtt_send, verbose):
    """Share CAN interfaces of this machine with CANExplorer instances on the network.

    In CANExplorer, connect with the interface RemoteBus and the channel HOST:PORT/NAME
    (add ?token=TOKEN when the server requires one).
    """
    logging.basicConfig(
        format='%(asctime)s [%(levelname)s] %(message)s', level=logging.DEBUG if verbose else logging.INFO
    )
    if not token and host not in ('127.0.0.1', 'localhost', '::1'):
        logger.warning('No token set: anyone who can reach this machine can read and send on the buses')
    mqtt = None
    if mqtt_url:
        try:
            mqtt = MqttSettings.from_url(
                mqtt_url,
                frames=mqtt_frames,
                retain=mqtt_retain,
                interval=mqtt_interval,
                allow_send=bool(mqtt_send),
                send_allowlist=list(mqtt_send),
            )
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint='--mqtt') from None
        if mqtt_send:
            logger.warning(f'Frames published over MQTT are sent on the buses (allowed: {", ".join(mqtt_send)})')
    elif databases or mqtt_frames or mqtt_send:
        raise click.UsageError('--database, --mqtt-frames and --mqtt-send need --mqtt')
    try:
        server = CanServer(buses, token, mqtt, databases)
    except ValueError as e:
        raise click.UsageError(str(e)) from None
    logger.info(f'Starting {PROJECT_NAME} on {host}:{port} with buses: {", ".join(spec.name for spec in buses)}')
    web.run_app(server.application(), host=host, port=port, print=None)
