"""A bit-transition raster: the value of every bit of one identifier's payload across the whole capture at once.

Where the bit grid shows how often each bit changes and the temporal replay shows one frame at a time, the raster
shows the structure over time — counters ramp as diagonal stripes, constant bits are solid, toggling bits
alternate, multiplexed regions switch — in a single image. The columns are the frames (downsampled to a target
width); the rows are the bits, byte 0 at the top and the most significant bit of each byte first, as the grid
orders them.

Each cell is 0..3: the bit's value (0 or 1) in the column's representative frame, plus a "changed" flag set when
the bit flipped anywhere within the column's frames (so a fast toggle reads as active even between samples).
"""

from dataclasses import dataclass
from typing import List, Sequence

MAX_COLUMNS = 1200  # Frames are bucketed into at most this many columns; the view scales the image to its width

CELL_ZERO = 0
CELL_ONE = 1
CELL_ZERO_CHANGED = 2
CELL_ONE_CHANGED = 3


@dataclass
class Raster:
    columns: int
    rows: int  # length * 8
    length: int  # payload bytes
    frames: int
    start: float  # First and last timestamp, for a time label
    end: float
    cells: bytes  # rows * columns, row-major; each 0..3 = value | (changed << 1)

    def cell(self, row: int, column: int) -> int:
        return self.cells[row * self.columns + column]


def build_raster(times: Sequence[float], payloads: Sequence[bytes], max_columns: int = MAX_COLUMNS) -> Raster:
    """Build the raster of a list of frames (of one identifier), in time order. The payload length is the longest
    frame seen; shorter frames are padded with zero bytes."""
    frames = len(payloads)
    length = max((len(payload) for payload in payloads), default=0)
    rows = length * 8
    if frames == 0 or rows == 0:
        return Raster(0, rows, length, frames, 0.0, 0.0, b'')

    # Each frame as one integer, little-endian, so bit `position` is (value >> position) & 1
    ints: List[int] = [int.from_bytes(bytes(payload).ljust(length, b'\x00')[:length], 'little') for payload in payloads]
    # Row r shows byte r // 8, bit 7 - (r % 8): the most significant bit of each byte at the top
    positions = [(r // 8) * 8 + (7 - (r % 8)) for r in range(rows)]

    columns = min(max_columns, frames)
    cells = bytearray(rows * columns)
    previous_last = None
    for column in range(columns):
        low = column * frames // columns
        high = (column + 1) * frames // columns
        if high <= low:
            high = low + 1
        representative = ints[high - 1]
        # Which bits changed anywhere in this column: XOR of consecutive frames, starting from the previous
        # column's representative so a change between columns is caught too
        mask = 0
        previous = previous_last if previous_last is not None else ints[low]
        for index in range(low, high):
            mask |= previous ^ ints[index]
            previous = ints[index]
        previous_last = representative
        for row in range(rows):
            position = positions[row]
            value = (representative >> position) & 1
            changed = (mask >> position) & 1
            cells[row * columns + column] = value | (changed << 1)

    return Raster(columns, rows, length, frames, times[0], times[-1], bytes(cells))
