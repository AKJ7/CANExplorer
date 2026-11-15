"""Breakpoints of a playback: the replay of a recording, or a recording transmitted on a bus, pauses right after a
frame that hits one, and can then be resumed or stepped frame by frame.

A breakpoint is one of:

- a **frame**: identifiers in the display filter syntax (``7DF``, ``100-1FF ext``, ``!7E8 rx``), optionally with a
  **data pattern** — hex bytes from the first byte on, ``?`` for any nibble (``02 10 ??``, ``?3``); frames shorter
  than the pattern do not match. It hits on every matching frame;
- a **signal condition** of the loaded database, ``Message.Signal <op> value`` with ``==``, ``!=``, ``<``, ``<=``,
  ``>``, ``>=`` (a number, or a value text of the signal), or ``Message.Signal changes``. It hits when the condition
  becomes true (not again on every frame while it stays true), or when the value changes;
- a **time**: the playback position in seconds since the first frame. It hits once, on the first frame at or after
  that time, and again after seeking back before it.

Frames skipped over by seeking forward do not hit breakpoints. `hit_count` counts the hits of each breakpoint;
`skip` lets a breakpoint pass its first n hits (e.g. stop at the 10th request).
"""

import math
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

import can

from can_explorer.core.filters import MessageFilter

OPERATORS = {
    '==': operator.eq,
    '!=': operator.ne,
    '<=': operator.le,
    '>=': operator.ge,
    '<': operator.lt,
    '>': operator.gt,
}
_CONDITION = re.compile(r'^\s*([^\s<>=!]+)\.([^\s<>=!.]+)\s*(==|!=|<=|>=|<|>|=|changes)\s*(.*?)\s*$', re.IGNORECASE)

FRAME, SIGNAL, TIME = 'frame', 'signal', 'time'


def parse_data_pattern(text: str) -> Tuple[Tuple[int, int], ...]:
    """(value, mask) per byte of a pattern like "02 10 ??" or "021?"; raises ValueError."""
    cleaned = ''.join(text.split()).replace(',', '')
    if cleaned.lower().startswith('0x'):
        cleaned = cleaned[2:]
    if len(cleaned) % 2:
        raise ValueError(f'A data pattern has two hex digits (or ?) per byte: {text!r}')
    pattern = []
    for index in range(0, len(cleaned), 2):
        value = mask = 0
        for nibble in cleaned[index : index + 2]:
            value, mask = value << 4, mask << 4
            if nibble in '?xX*':
                continue
            try:
                value |= int(nibble, 16)
            except ValueError:
                raise ValueError(f'Not a hex digit or ? in the data pattern: {nibble!r}') from None
            mask |= 0xF
        pattern.append((value, mask))
    return tuple(pattern)


def _format_pattern(pattern: Tuple[Tuple[int, int], ...]) -> str:
    def nibble(value, mask, shift):
        return f'{(value >> shift) & 0xF:X}' if (mask >> shift) & 0xF else '?'

    return ' '.join(nibble(v, m, 4) + nibble(v, m, 0) for v, m in pattern)


@dataclass
class Breakpoint:
    kind: str  # FRAME, SIGNAL or TIME
    filter: MessageFilter = field(default_factory=MessageFilter)
    data: Tuple[Tuple[int, int], ...] = ()
    message: str = ''
    signal: str = ''
    op: str = ''  # One of OPERATORS, or 'changes'
    value: Any = None  # A number or a value text
    time: float = 0.0
    enabled: bool = True
    skip: int = 0  # Hits to let pass before stopping
    hit_count: int = 0
    _armed: bool = True  # Time breakpoints: not hit since the last seek before them
    _previous: Any = None  # Signal breakpoints: the condition (or value) at the previous frame of the message

    @classmethod
    def frame(cls, filter_text: str, data: str = '', skip: int = 0) -> 'Breakpoint':
        message_filter = MessageFilter.parse(filter_text)
        pattern = parse_data_pattern(data) if data.strip() else ()
        if message_filter.is_empty and not pattern:
            raise ValueError('Give the identifiers (e.g. 7DF or 100-1FF) or a data pattern')
        return cls(FRAME, filter=message_filter, data=pattern, skip=skip)

    @classmethod
    def condition(cls, text: str, skip: int = 0) -> 'Breakpoint':
        match = _CONDITION.match(text)
        if match is None:
            raise ValueError('Write the condition as Message.Signal > value (or ==, !=, <, <=, >=, or "changes")')
        message, signal, op, value = match.groups()
        op = '==' if op == '=' else op.lower()
        if op == 'changes':
            if value:
                raise ValueError('"changes" takes no value')
            return cls(SIGNAL, message=message, signal=signal, op=op, skip=skip)
        if not value:
            raise ValueError(f'The condition needs a value after {op}')
        try:
            parsed: Any = float(value)
        except ValueError:
            if op not in ('==', '!='):
                raise ValueError(f'Compare value texts with == or != only, not {op}') from None
            parsed = value.strip('"\'')
        return cls(SIGNAL, message=message, signal=signal, op=op, value=parsed, skip=skip)

    @classmethod
    def at(cls, seconds: float) -> 'Breakpoint':
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('A time breakpoint is at a position of 0 s or later')
        return cls(TIME, time=float(seconds))

    @property
    def label(self) -> str:
        if self.kind == TIME:
            return f'At {self.time:.3f} s'
        if self.kind == SIGNAL:
            if self.op == 'changes':
                return f'{self.message}.{self.signal} changes'
            value = f'{self.value:g}' if isinstance(self.value, float) else self.value
            return f'{self.message}.{self.signal} {self.op} {value}'
        parts = [self.filter.text] if self.filter.text else []
        if self.data:
            parts.append(f'data {_format_pattern(self.data)}')
        return ' '.join(parts)

    # Matching

    def _frame_matches(self, msg: can.Message) -> bool:
        if not self.filter.is_empty and not self.filter.matches(msg):
            return False
        if self.data:
            data = msg.data
            if len(data) < len(self.data):
                return False
            return all(data[i] & mask == value for i, (value, mask) in enumerate(self.data))
        return True

    def _signal_hit(self, msg: can.Message, database: Any) -> bool:
        if database is None:
            return False
        definition = database.message_definition(msg)
        if definition is None or definition.name != self.message:
            return False
        values = database.decode(msg)
        if not values or self.signal not in values:
            return False  # Not in this frame (e.g. another multiplexer value)
        value = values[self.signal]
        if self.op == 'changes':
            previous, self._previous = self._previous, value
            return previous is not None and value != previous
        number = getattr(value, 'value', value)  # A value text's number
        if isinstance(self.value, str):
            holds = OPERATORS[self.op](str(value), self.value)
        else:
            try:
                holds = OPERATORS[self.op](float(number), self.value)
            except (TypeError, ValueError):
                holds = False
        became = holds and not self._previous
        self._previous = holds
        return became

    def check(self, msg: can.Message, position: float, database: Any) -> bool:
        """Whether this frame hits the breakpoint (counting it, and passing the first `skip` hits)."""
        if not self.enabled:
            return False
        if self.kind == TIME:
            hit = self._armed and position >= self.time
            if hit:
                self._armed = False
        elif self.kind == SIGNAL:
            hit = self._signal_hit(msg, database)
        else:
            hit = self._frame_matches(msg)
        if not hit:
            return False
        self.hit_count += 1
        return self.hit_count > self.skip

    def rearm(self, position: float) -> None:
        """After seeking to `position`: time breakpoints after it hit again; signal conditions start afresh."""
        self._armed = position < self.time
        self._previous = None

    def reset(self) -> None:
        self.hit_count = 0
        self._armed = True
        self._previous = None


@dataclass
class Hit:
    breakpoint: Optional[Breakpoint]  # None: a single step
    position: float
    msg: can.Message

    @property
    def text(self) -> str:
        msg = self.msg
        identifier = f'{msg.arbitration_id:08X}' if msg.is_extended_id else f'{msg.arbitration_id:03X}'
        frame = f'{identifier} {msg.data.hex(" ").upper()}'.rstrip()
        reason = 'Step' if self.breakpoint is None else f'Breakpoint {self.breakpoint.label}'
        return f'{reason} — {self.position:.6f} s: {frame}'


class BreakpointSet:
    """The breakpoints of a playback, and single stepping. `database` returns the signal database (for signal
    conditions) when called."""

    def __init__(self, database: Optional[Callable[[], Any]] = None):
        self.items: List[Breakpoint] = []
        self.database = database or (lambda: None)
        self.last_hit: Optional[Hit] = None
        self._skip_before = -math.inf  # Frames before this position were skipped over by seeking
        self.version = 0  # Changes whenever the breakpoints or the last hit change

    @property
    def active(self) -> bool:
        return any(item.enabled for item in self.items)

    def _changed(self) -> None:
        self.version += 1

    def add(self, breakpoint: Breakpoint) -> Breakpoint:
        self.items.append(breakpoint)
        self._changed()
        return breakpoint

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.items):
            del self.items[index]
            self._changed()

    def clear(self) -> None:
        self.items.clear()
        self._changed()

    def set_enabled(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self.items):
            self.items[index].enabled = enabled
            self._changed()

    def restart(self) -> None:
        """A new playback from the beginning: hit counts and conditions start afresh."""
        for item in self.items:
            item.reset()
        self.last_hit = None
        self._skip_before = -math.inf
        self._changed()

    def seeked(self, position: float) -> None:
        """The playback jumps to `position`: frames before it do not hit."""
        self._skip_before = position
        for item in self.items:
            item.rearm(position)
        self._changed()

    def check(self, msg: can.Message, position: float, stepping: bool = False) -> Optional[Hit]:
        """The hit of a frame just played (None: play on). `stepping`: a single step, which always stops."""
        if position < self._skip_before - 1e-9:
            return None
        hit = None
        if self.items:
            database = self.database()
            for item in self.items:
                if item.check(msg, position, database) and hit is None:
                    hit = Hit(item, position, msg)  # Every breakpoint still counts its hit
        if hit is None and stepping:
            hit = Hit(None, position, msg)
        if hit is not None:
            self.last_hit = hit
            self._changed()
        return hit

    def rows(self) -> List[dict]:
        return [
            {'label': item.label, 'kind': item.kind, 'enabled': item.enabled, 'hits': item.hit_count, 'skip': item.skip}
            for item in self.items
        ]
