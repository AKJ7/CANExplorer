from abc import ABC, abstractmethod
import asyncio
import logging

logger = logging.getLogger(__name__)


class Worker(ABC):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._task: asyncio.Task = self._loop.create_task(self._run())

    @abstractmethod
    async def _run(self):
        pass

    def cancel(self):
        self._task.cancel()