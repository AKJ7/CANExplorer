import asyncio
import logging
from pathlib import Path
from typing import Callable, Coroutine
from aiofile import async_open
import time

logger = logging.getLogger(__name__)


class Heart:

    def __init__(
        self,
        heartbeat_interval,
        heartbeat_path: Path,
        is_active: Callable[[], Coroutine],
    ):
        self._heartbeat_path = heartbeat_path
        self._is_active = is_active
        self._heartbeat_interval = heartbeat_interval
        self._last_heartbeat = 0

    @property
    async def is_alive(self) -> bool:
        now = time.perf_counter()
        alive_state = now - self._last_heartbeat < self._heartbeat_interval
        return alive_state

    async def beat(self):
        while True:
            if await self.is_alive:
                return
            logger.debug('Running Heartbeat')
            self._last_heartbeat = time.perf_counter()
            try:
                async with async_open(self._heartbeat_path, 'w+') as heartbeat_file:
                    await heartbeat_file.write('')
            except Exception as e:
                logger.warning(e)
            await asyncio.sleep(self._heartbeat_interval)
            if not await self._is_active():
                return
