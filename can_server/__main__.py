import sys
import asyncio
import logging
from can_server import PROJECT_NAME
from can_server.cli.cli import cli
from can_explorer import LOG_FORMAT, LOG_LEVEL
from can_server.core.heart import Heart
from can_server.core.server import create_server
from aiohttp import web
import pathlib

logger = logging.getLogger(__name__)


async def is_alive():
    return True


async def main():
    heart = Heart(pathlib.Path("test.txt"), is_active=is_alive)
    await heart.beat()


if __name__ == "__main__":
    logging.basicConfig(format=LOG_FORMAT, level=logging.DEBUG)
    logger.info(f'Starting "{PROJECT_NAME}" App')
    # cli()
    # asyncio.run(main())
    web.run_app(create_server())
    logger.info(f'Exiting "{PROJECT_NAME}" App. Have a nice day!')
