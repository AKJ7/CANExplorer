import re
from dataclasses import dataclass
from typing import FrozenSet, Tuple

import can

IdRange = Tuple[int, int]

_KEYWORDS = {'std', 'ext', 'rx', 'tx', 'fd'}
_RANGE = re.compile(r'^(?:0x)?([0-9a-f]+)(?:-(?:0x)?([0-9a-f]+))?$', re.IGNORECASE)


@dataclass(frozen=True)
class MessageFilter:
    """Display filter.

    Syntax: comma or space separated terms. Hex identifiers (``100``, ``0x7DF``) and ranges (``100-1FF``)
    are included, prefixing ``!`` excludes them. The keywords ``std``/``ext``, ``rx``/``tx`` and ``fd``
    restrict the frame type. An empty filter matches everything.
    """

    include: Tuple[IdRange, ...] = ()
    exclude: Tuple[IdRange, ...] = ()
    keywords: FrozenSet[str] = frozenset()
    text: str = ''

    @classmethod
    def parse(cls, text: str) -> 'MessageFilter':
        include, exclude, keywords = [], [], set()
        for token in text.replace(',', ' ').split():
            lowered = token.lower()
            if lowered in _KEYWORDS:
                keywords.add(lowered)
                continue
            negate = lowered.startswith('!')
            match = _RANGE.match(lowered.removeprefix('!'))
            if match is None:
                raise ValueError(f'Invalid filter term: {token!r}')
            low = int(match.group(1), 16)
            high = int(match.group(2), 16) if match.group(2) else low
            if low > high:
                raise ValueError(f'Invalid range: {token!r}')
            (exclude if negate else include).append((low, high))
        return cls(tuple(include), tuple(exclude), frozenset(keywords), text.strip())

    @property
    def is_empty(self) -> bool:
        return not (self.include or self.exclude or self.keywords)

    def matches(self, msg: can.Message) -> bool:
        keywords = self.keywords
        if keywords:
            if ('std' in keywords or 'ext' in keywords) and ('ext' if msg.is_extended_id else 'std') not in keywords:
                return False
            if ('rx' in keywords or 'tx' in keywords) and ('rx' if msg.is_rx else 'tx') not in keywords:
                return False
            if 'fd' in keywords and not msg.is_fd:
                return False
        can_id = msg.arbitration_id
        if self.include and not any(low <= can_id <= high for low, high in self.include):
            return False
        return not any(low <= can_id <= high for low, high in self.exclude)
