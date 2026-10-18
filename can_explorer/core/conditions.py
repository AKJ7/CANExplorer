"""Conditions on frames, used to search the trace and to trigger recordings.

Syntax:

- Signal comparison: ``EngineData.EngineSpeed > 6000``, ``EngineData.GearPosition == Second`` (``==``, ``!=``,
  ``<``, ``<=``, ``>``, ``>=``; numbers compare physical values, other values the value table texts). The message may
  also be a contained PDU of a container frame.
- Frame terms in the syntax of the display filter: ``7E8``, ``100-1FF !123 ext rx fd``.
- A data pattern, alone or after frame terms: ``data=01 ?? FF`` matches frames whose data starts with 01, any byte,
  FF; ``7E8 data=?? 41 0D``.
"""

import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

import can

from can_explorer.core.filters import MessageFilter

_SIGNAL = re.compile(r'^\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(==|=|!=|>=|<=|>|<)\s*(.+?)\s*$')
_DATA = re.compile(r'(?:^|\s)data\s*[=:]\s*((?:[0-9a-fA-F?]{2}\s*)+)$')
_OPERATORS: dict = {
    '==': operator.eq,
    '=': operator.eq,
    '!=': operator.ne,
    '>=': operator.ge,
    '<=': operator.le,
    '>': operator.gt,
    '<': operator.lt,
}


class ConditionError(ValueError):
    pass


def _number(text: str) -> Optional[float]:
    try:
        return float(int(text, 0)) if re.match(r'^[+-]?0[xb]', text, re.IGNORECASE) else float(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class SignalCondition:
    message: str
    signal: str
    operator: str
    value: Any  # A number, or a value table text

    def matches(self, msg: can.Message, database) -> bool:
        if database is None or msg.is_error_frame or msg.is_remote_frame:
            return False
        definition = database.message_definition(msg)
        if definition is None:
            return False
        names = {definition.name} | {part.name for part in (getattr(definition, 'contained_messages', None) or [])}
        if self.message not in names:
            return False
        numeric = not isinstance(self.value, str)
        try:
            values = database.decode(msg, decode_choices=not numeric) or {}
        except Exception:
            return False
        if self.signal not in values:
            return False
        actual = values[self.signal]
        compare = _OPERATORS[self.operator]
        if numeric:
            try:
                return bool(compare(float(actual), self.value))
            except (TypeError, ValueError):
                return False
        return bool(compare(str(actual), self.value))

    def __str__(self) -> str:
        return f'{self.message}.{self.signal} {self.operator} {self.value!r}'


@dataclass(frozen=True)
class FrameCondition:
    """Display filter terms and/or a data prefix pattern (None bytes match anything)."""

    filter: MessageFilter
    pattern: Tuple[Optional[int], ...] = ()

    def matches(self, msg: can.Message, database=None) -> bool:
        if msg.is_error_frame:
            return False
        if not self.filter.is_empty and not self.filter.matches(msg):
            return False
        if self.pattern:
            data = msg.data
            if len(data) < len(self.pattern):
                return False
            return all(expected is None or data[i] == expected for i, expected in enumerate(self.pattern))
        return True


Condition = SignalCondition | FrameCondition


def parse_condition(text: str) -> Condition:
    """Parse a condition; raises ConditionError for anything else (e.g. free text)."""
    text = text.strip()
    if not text:
        raise ConditionError('Enter a condition')
    match = _SIGNAL.match(text)
    if match:
        message, signal, operator_text, value_text = match.groups()
        value_text = value_text.strip().strip('"\'')
        value: Any = _number(value_text)
        if value is None:
            if operator_text not in ('==', '=', '!='):
                raise ConditionError(f'{value_text!r} is not a number; texts can only be compared with == or !=')
            value = value_text
        return SignalCondition(message, signal, operator_text, value)
    pattern: Tuple[Optional[int], ...] = ()
    data = _DATA.search(text)
    if data:
        pattern = tuple(
            None if byte == '??' else int(byte, 16) for byte in re.findall(r'[0-9a-fA-F?]{2}', data.group(1))
        )
        if any(byte is not None and '?' in f'{byte:02X}' for byte in pattern):
            raise ConditionError('Use ?? for any byte')
        text = text[: data.start()].strip()
    try:
        message_filter = MessageFilter.parse(text)
    except ValueError as e:
        raise ConditionError(str(e)) from None
    if message_filter.is_empty and not pattern:
        raise ConditionError('Enter identifiers, a data pattern or a signal comparison')
    return FrameCondition(message_filter, pattern)


def condition_matcher(text: str, database: Callable[[], Any]) -> Callable[[can.Message], bool]:
    """A function testing frames against a condition, with the signal database of the moment."""
    condition = parse_condition(text)
    return lambda msg: condition.matches(msg, database())
