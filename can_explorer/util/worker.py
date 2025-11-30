from abc import ABC, abstractmethod
import asyncio
import logging

logger = logging.getLogger(__name__)


class Worker(ABC):
    def __init__(self, loop: asyncio.AbstractEventLoop, *, name: str | None = None):
        self._loop = loop
        self._task: asyncio.Task = self._loop.create_task(self.__run(), name=name)
        self._task.add_done_callback(self._on_done)
        self._exception : Exception | None = None

    async def __run(self):
        try:
            await self._run()
        except asyncio.CancelledError:
            self._exception = None
        except Exception as e:
            self._exception = e

    @abstractmethod
    async def _run(self):
        raise NotImplementedError('Method not implemented in subclass')

    def _on_done(self, task: asyncio.Task):
        if self._exception is not None:
            logger.exception(self._exception)
            raise self._exception

    async def cancel(self) -> bool:
        alive_state = self._task.cancel()
        if alive_state:
            await self._task
            return self._task.cancelled()
        return alive_state