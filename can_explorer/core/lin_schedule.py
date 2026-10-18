"""LIN master: runs an LDF schedule table on a session.

Frames the master publishes are sent as data frames (header plus response) with the current values of
the master signals; for frames published by slaves only the header is sent (RTR frame), the slave answers.
"""

import asyncio
from typing import Dict, List, Optional

import can
from ldfparser.schedule import LinFrameEntry, MasterRequestEntry, SlaveResponseEntry

from can_explorer.core.ldf import LdfDecoder
from can_explorer.core.lin import SLAVE_RESPONSE_ID
from can_explorer.core.session import BusSession, SessionError
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)


class LinScheduler:
    def __init__(self, session: BusSession, ldf: LdfDecoder, table: str):
        self.session = session
        self.ldf = ldf
        try:
            self.table = ldf.ldf.get_schedule_table(table)
        except LookupError:
            raise ValueError(f'Unknown schedule table {table!r}') from None
        self.values: Dict[str, object] = {entry['signal']: entry['value'] for entry in ldf.master_signals()}
        self.cycles = 0
        self._task: Optional[asyncio.Task] = None
        self._warned: set = set()

    @staticmethod
    def tables(ldf: LdfDecoder) -> List[str]:
        return [table.name for table in ldf.ldf.get_schedule_tables()]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_signal(self, name: str, value) -> None:
        if name not in self.values:
            raise KeyError(f'{name!r} is not published by the master')
        frame = next(entry['frame'] for entry in self.ldf.master_signals() if entry['signal'] == name)
        values = {signal: self.values[signal] for signal in self.ldf.frame(frame).signal_names}
        values[name] = value
        self.ldf.frame(frame).encode(values)  # Validates the value before it is used
        self.values[name] = value

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._run(), name=f'LIN schedule {self.table.name}')

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _entry_message(self, entry) -> Optional[can.Message]:
        if isinstance(entry, LinFrameEntry):
            frame = entry.frame
            if frame.publisher.name == self.ldf.master:
                definition = self.ldf.frame(frame.name)
                data = definition.encode({name: self.values[name] for name in definition.signal_names})
                return can.Message(arbitration_id=frame.frame_id, is_extended_id=False, data=data)
            return can.Message(
                arbitration_id=frame.frame_id, is_extended_id=False, is_remote_frame=True, dlc=frame.length
            )
        if isinstance(entry, SlaveResponseEntry):
            return can.Message(arbitration_id=SLAVE_RESPONSE_ID, is_extended_id=False, is_remote_frame=True, dlc=8)
        if isinstance(entry, MasterRequestEntry):
            return None  # Diagnostic requests are sent by the diagnostics client when needed
        kind = type(entry).__name__
        if kind not in self._warned:
            self._warned.add(kind)
            logger.warning(f'Schedule entry {kind} is not supported and is skipped')
        return None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while True:
                for entry in self.table.schedule:
                    msg = self._entry_message(entry)
                    if msg is not None:
                        self.session.send(msg)
                    deadline += entry.delay
                    await asyncio.sleep(max(0.0, deadline - loop.time()))
                self.cycles += 1
        except SessionError as e:
            logger.warning(f'LIN schedule {self.table.name} stopped: {e}')
