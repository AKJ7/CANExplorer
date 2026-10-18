"""Recording frames to log files: split into numbered files by size or duration, and triggered by conditions."""

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional

import can

from can_explorer.core.conditions import Condition, parse_condition
from can_explorer.core.log_formats import log_suffix

SIZE_CHECK_INTERVAL = 512  # Frames between checks of the file size


def numbered_path(path: Path, number: int) -> Path:
    """'trace.asc.gz', 3 -> 'trace_003.asc.gz'."""
    suffix = log_suffix(path) or path.suffix
    stem = path.name[: len(path.name) - len(suffix)] if suffix else path.name
    return path.with_name(f'{stem}_{number:03d}{suffix}')


class Recorder:
    """Writes frames to a log file. With `split_bytes` or `split_seconds` the recording is split into numbered
    files (trace_001.blf, trace_002.blf, ...); the duration counts in frame timestamps, so replays split the same
    way as live recordings.
    """

    def __init__(self, path: str | Path, split_bytes: int = 0, split_seconds: float = 0.0, first_number: int = 1):
        self.path = Path(path)
        self.split_bytes = max(0, int(split_bytes))
        self.split_seconds = max(0.0, float(split_seconds))
        self.files: List[Path] = []
        self._number = first_number
        self._writer: Optional[can.Listener] = None
        self._file_start: Optional[float] = None
        self._since_size_check = 0
        self.frames = 0

    @property
    def splits(self) -> bool:
        return bool(self.split_bytes or self.split_seconds)

    @property
    def current_path(self) -> Optional[Path]:
        return self.files[-1] if self._writer is not None and self.files else None

    def _open(self) -> None:
        path = numbered_path(self.path, self._number) if self.splits else self.path
        self._number += 1
        self._writer = can.Logger(path)
        self.files.append(path)
        self._file_start = None
        self._since_size_check = 0

    def _full(self, msg: can.Message) -> bool:
        if (
            self.split_seconds
            and self._file_start is not None
            and msg.timestamp - self._file_start >= self.split_seconds
        ):
            return True
        if self.split_bytes:
            self._since_size_check += 1
            if self._since_size_check >= SIZE_CHECK_INTERVAL:
                self._since_size_check = 0
                return self._size() >= self.split_bytes
        return False

    def _size(self) -> int:
        """Bytes written so far, including those still buffered (the size on disk lags behind the buffer).
        Approximate for formats that collect frames before writing them (BLF) or compress them (.gz)."""
        file = getattr(self._writer, 'file', None)
        try:
            return file.tell() if file is not None else os.path.getsize(self.files[-1])
        except (OSError, ValueError, AttributeError):
            try:
                return os.path.getsize(self.files[-1])
            except OSError:
                return 0

    def on_message_received(self, msg: can.Message) -> None:
        if self._writer is not None and self.splits and self._full(msg):
            self._close_file()
        if self._writer is None:
            self._open()
        if self._file_start is None:
            self._file_start = msg.timestamp
        self._writer.on_message_received(msg)
        self.frames += 1

    def _close_file(self) -> None:
        if self._writer is not None:
            self._writer.stop()
            self._writer = None

    def stop(self) -> None:
        self._close_file()

    def describe(self) -> str:
        if not self.files:
            return f'recording to {self.path.name}'
        if len(self.files) == 1:
            return f'recording to {self.files[0].name}'
        return f'recording to {self.files[-1].name} ({len(self.files)} files)'


@dataclass
class Trigger:
    """When a triggered recording records: from `pre` seconds before the start condition matches until `post`
    seconds after the stop condition matches (or after the start, without a stop condition)."""

    start: str
    stop: str = ''
    pre: float = 1.0
    post: float = 1.0
    repeat: bool = False  # Arm again after each recording, writing numbered files

    def conditions(self) -> tuple:
        return parse_condition(self.start), (parse_condition(self.stop) if self.stop.strip() else None)


class TriggeredRecorder:
    """Records around trigger events. States: 'armed', 'recording', 'stopping' (the post-trigger time), 'done'."""

    def __init__(
        self,
        path: str | Path,
        trigger: Trigger,
        database: Callable[[], Any],
        split_bytes: int = 0,
        split_seconds: float = 0.0,
        on_event: Optional[Callable[[str], None]] = None,
    ):
        self.path = Path(path)
        self.trigger = trigger
        self._start: Condition
        self._stop: Optional[Condition]
        self._start, self._stop = trigger.conditions()
        self._database = database
        self._split = (split_bytes, split_seconds)
        self._on_event = on_event or (lambda text: None)
        self._buffer: Deque[can.Message] = deque()
        self._recorder: Optional[Recorder] = None
        self._deadline: Optional[float] = None
        self._number = 1
        self.files: List[Path] = []
        self.events = 0
        self.state = 'armed'

    def on_message_received(self, msg: can.Message) -> None:
        if self.state == 'armed':
            self._buffer.append(msg)
            while self._buffer and self._buffer[0].timestamp < msg.timestamp - self.trigger.pre:
                self._buffer.popleft()
            if self._start.matches(msg, self._database()):
                self._begin(msg)
        elif self.state == 'recording':
            if self._stop is None and msg.timestamp > self._deadline:
                self._end()  # Frames up to `post` seconds after the start are recorded, this one is later
                return
            self._write(msg)
            if self._stop is not None and self._stop.matches(msg, self._database()):
                self._deadline = msg.timestamp + self.trigger.post
                self.state = 'stopping'
                self._on_event(f'Stop condition met; recording {self.trigger.post:g} s more')
        elif self.state == 'stopping':
            if msg.timestamp > self._deadline:
                self._end()
                return
            self._write(msg)

    def _begin(self, msg: can.Message) -> None:
        self.events += 1
        path = numbered_path(self.path, self._number) if self.trigger.repeat else self.path
        self._number += 1
        split_bytes, split_seconds = self._split
        self._recorder = Recorder(path, split_bytes, split_seconds)
        for buffered in self._buffer:
            self._recorder.on_message_received(buffered)
        self._buffer.clear()
        self.state = 'recording'
        self._deadline = msg.timestamp + self.trigger.post if self._stop is None else None
        self._on_event(f'Trigger: {self.trigger.start} — recording to {path.name}')

    def _write(self, msg: can.Message) -> None:
        self._recorder.on_message_received(msg)

    def _end(self) -> None:
        self._close()
        if self.trigger.repeat:
            self.state = 'armed'
            self._on_event(f'Recording {self.events} finished; armed again')
        else:
            self.state = 'done'
            self._on_event(f'Triggered recording finished: {", ".join(path.name for path in self.files[-1:])}')

    def _close(self) -> None:
        if self._recorder is not None:
            self._recorder.stop()
            self.files += self._recorder.files
            self._recorder = None

    def stop(self) -> None:
        self._close()

    @property
    def current_path(self) -> Optional[Path]:
        return self._recorder.current_path if self._recorder else None

    def describe(self) -> str:
        if self.state == 'armed':
            done = f', {self.events} recorded' if self.events else ''
            return f'armed: {self.trigger.start}{done}'
        if self.state in ('recording', 'stopping'):
            return f'triggered, recording to {self.current_path.name if self.current_path else self.path.name}'
        return f'trigger recorded {", ".join(path.name for path in self.files)}'
