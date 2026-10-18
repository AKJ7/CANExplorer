"""The trace: all frames of a session, the most recent ones in memory and older ones in temporary files.

Frames have absolute indices (0 for the first frame since the last clear), which stay valid while the frame is
retained; views keep indices instead of frames, so they can page through millions of frames. With a history
limit equal to the memory limit nothing goes to disk.
"""

import struct
import tempfile
from collections import OrderedDict, deque
from pathlib import Path
from typing import Callable, Deque, Iterator, List, Optional

import can

from can_explorer.core.frames import Frame

# timestamp, identifier, flags, DLC, data length, protocol text offset and length, data
RECORD = struct.Struct('<dIBBBxQI64s')
SEGMENT_FRAMES = 1 << 18  # Frames per temporary file pair (about 24 MB of records)
SPILL_CHUNK = 4096  # Frames moved to disk at once
PAGE_FRAMES = 256  # Frames read from disk at once
CACHED_PAGES = 64

_EXTENDED, _RX, _FD, _BRS, _ESI, _REMOTE, _ERROR = (1 << bit for bit in range(7))


def _pack(frame: Frame, text_offset: int, text_length: int) -> bytes:
    msg = frame.message
    flags = (
        (_EXTENDED if msg.is_extended_id else 0)
        | (_RX if msg.is_rx else 0)
        | (_FD if msg.is_fd else 0)
        | (_BRS if msg.bitrate_switch else 0)
        | (_ESI if msg.error_state_indicator else 0)
        | (_REMOTE if msg.is_remote_frame else 0)
        | (_ERROR if msg.is_error_frame else 0)
    )
    data = bytes(msg.data)
    return RECORD.pack(msg.timestamp, msg.arbitration_id, flags, msg.dlc, len(data), text_offset, text_length, data)


def _unpack(record: tuple, text: str) -> Frame:
    timestamp, arbitration_id, flags, dlc, length, _, _, data = record
    msg = can.Message(
        timestamp=timestamp,
        arbitration_id=arbitration_id,
        is_extended_id=bool(flags & _EXTENDED),
        is_rx=bool(flags & _RX),
        is_fd=bool(flags & _FD),
        bitrate_switch=bool(flags & _BRS),
        error_state_indicator=bool(flags & _ESI),
        is_remote_frame=bool(flags & _REMOTE),
        is_error_frame=bool(flags & _ERROR),
        dlc=dlc,
        data=data[:length],
        check=False,
    )
    return Frame(msg, text)


class _Segment:
    """Up to SEGMENT_FRAMES frames on disk, from absolute index `start` on."""

    def __init__(self, start: int, directory: Optional[Path]):
        self.start = start
        self.count = 0
        self._records = tempfile.TemporaryFile(prefix='canexplorer-trace-', dir=directory)
        self._texts = tempfile.TemporaryFile(prefix='canexplorer-text-', dir=directory)
        self._text_size = 0

    @property
    def end(self) -> int:
        return self.start + self.count

    def append(self, frames: List[Frame]) -> None:
        records = []
        texts = []
        for frame in frames:
            text = frame.info.encode('utf-8') if frame.info else b''
            records.append(_pack(frame, self._text_size, len(text)))
            texts.append(text)
            self._text_size += len(text)
        self._records.seek(0, 2)
        self._records.write(b''.join(records))
        self._texts.seek(0, 2)
        self._texts.write(b''.join(texts))
        self.count += len(frames)

    def read(self, start: int, count: int) -> List[Frame]:
        """Frames from absolute index `start` on."""
        first = start - self.start
        count = min(count, self.count - first)
        if count <= 0:
            return []
        self._records.seek(first * RECORD.size)
        records = list(RECORD.iter_unpack(self._records.read(count * RECORD.size)))
        text_start = records[0][5]
        text_end = records[-1][5] + records[-1][6]
        self._texts.seek(text_start)
        texts = self._texts.read(text_end - text_start)
        return [
            _unpack(record, texts[record[5] - text_start : record[5] - text_start + record[6]].decode('utf-8'))
            for record in records
        ]

    def close(self) -> None:
        self._records.close()
        self._texts.close()


class TraceStore:
    """Frames of a session: the newest `memory_frames` in memory, up to `history_frames` in total.

    `directory` is where the temporary files go (default: the system's temporary directory, which is often in
    memory; prefer a directory on disk). When writing fails, e.g. because the disk is full, the store keeps only
    the frames in memory from then on and calls `on_error` once.
    """

    def __init__(
        self,
        memory_frames: int = 100_000,
        history_frames: int = 0,
        directory: Optional[Path] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self._memory_limit = max(1, memory_frames)
        self._history_limit = max(self._memory_limit, history_frames)
        self._directory = Path(directory) if directory else None
        self._on_error = on_error
        self._memory: Deque[Frame] = deque()
        self._segments: Deque[_Segment] = deque()
        self._pending: List[Frame] = []
        self._pages: OrderedDict = OrderedDict()
        self._first = 0
        self.total = 0  # Frames appended since the last clear; the absolute index of the next frame

    # Limits

    @property
    def maxlen(self) -> int:
        """Frames kept in memory."""
        return self._memory_limit

    @property
    def history_limit(self) -> int:
        """Frames kept in total."""
        return self._history_limit

    @property
    def uses_disk(self) -> bool:
        return self._history_limit > self._memory_limit

    @property
    def disk_frames(self) -> int:
        return sum(segment.count for segment in self._segments) - max(
            0, self._first - (self._segments[0].start if self._segments else self._first)
        )

    # Indices

    @property
    def first(self) -> int:
        """Absolute index of the oldest retained frame."""
        return self._first

    def __len__(self) -> int:
        return self.total - self._first

    @property
    def _memory_start(self) -> int:
        return self.total - len(self._memory)

    # Adding

    def append(self, frame: Frame) -> None:
        self._memory.append(frame)
        self._pending.append(frame)
        self.total += 1
        if len(self._pending) > 2 * self._memory_limit:
            del self._pending[: -self._memory_limit]
        if len(self._memory) > self._memory_limit:
            if self.uses_disk:
                if len(self._memory) >= self._memory_limit + SPILL_CHUNK:
                    try:
                        self._spill(SPILL_CHUNK)
                    except OSError as e:
                        self._disk_failed(e)
            else:
                self._memory.popleft()
                self._first += 1

    def _disk_failed(self, error: OSError) -> None:
        """Continue with the frames in memory only."""
        self._history_limit = self._memory_limit
        self._pages.clear()
        while self._segments:
            self._segments.popleft().close()
        while len(self._memory) > self._memory_limit:
            self._memory.popleft()
        self._first = self.total - len(self._memory)
        if self._on_error is not None:
            self._on_error(f'The trace history on disk was turned off: {error}')

    def _spill(self, count: int) -> None:
        if self._directory is not None:
            self._directory.mkdir(parents=True, exist_ok=True)
        frames = [self._memory[index] for index in range(count)]
        start = self.total - len(self._memory)
        while frames:
            segment = self._segments[-1] if self._segments else None
            if segment is None or segment.count >= SEGMENT_FRAMES:
                segment = _Segment(start, self._directory)
                self._segments.append(segment)
            room = SEGMENT_FRAMES - segment.count
            segment.append(frames[:room])
            start += len(frames[:room])
            frames = frames[room:]
        for _ in range(count):  # Only removed from memory once they are on disk
            self._memory.popleft()
        # Drop the oldest frames beyond the history limit; whole files are deleted once all their frames are gone
        self._first = max(self._first, self.total - self._history_limit)
        while self._segments and self._segments[0].end <= self._first:
            self._segments.popleft().close()
            self._pages.clear()

    def drain(self) -> List[Frame]:
        """Frames appended since the last drain (at most `maxlen`)."""
        pending, self._pending = self._pending[-self._memory_limit :], []
        return pending

    def clear(self) -> None:
        self._memory.clear()
        self._pending.clear()
        self._pages.clear()
        while self._segments:
            self._segments.popleft().close()
        self._first = 0
        self.total = 0

    close = clear

    # Reading

    def get(self, index: int) -> Optional[Frame]:
        """The frame with this absolute index, or None when it is not retained."""
        if index < self._first or index >= self.total:
            return None
        memory_start = self._memory_start
        if index >= memory_start:
            return self._memory[index - memory_start]
        page = index // PAGE_FRAMES
        frames = self._pages.get(page)
        if frames is None:
            frames = self._read_page(page)
            self._pages[page] = frames
            if len(self._pages) > CACHED_PAGES:
                self._pages.popitem(last=False)
        else:
            self._pages.move_to_end(page)
        offset = index - page * PAGE_FRAMES
        return frames[offset] if offset < len(frames) else None

    def _read_page(self, page: int) -> List[Frame]:
        start = page * PAGE_FRAMES
        end = min(start + PAGE_FRAMES, self._memory_start)
        frames: List[Frame] = []
        index = start
        for segment in self._segments:
            if segment.end <= index or index >= end:
                continue
            if segment.start > index:
                break
            chunk = segment.read(index, end - index)
            frames += chunk
            index += len(chunk)
        return frames

    def iter_from(self, index: int) -> Iterator[Frame]:
        """Retained frames from absolute index `index` on, oldest first."""
        index = max(index, self._first)
        memory_start = self._memory_start
        for segment in list(self._segments):
            while index < min(segment.end, memory_start):
                chunk = segment.read(index, PAGE_FRAMES)
                if not chunk:
                    break
                yield from chunk
                index += len(chunk)
        memory_start = self._memory_start  # Unchanged unless frames were added while iterating
        for offset in range(max(0, index - memory_start), len(self._memory)):
            yield self._memory[offset]

    def __iter__(self) -> Iterator[Frame]:
        return self.iter_from(self._first)

    def __bool__(self) -> bool:
        return len(self) > 0
