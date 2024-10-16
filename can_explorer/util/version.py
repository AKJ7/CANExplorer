import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
INVALID_VERSION = 0xFF


@dataclass(frozen=True, slots=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def from_str(cls, version: str) -> 'Version':
        matches = re.match(r'([0-9]+)\.([0-9]+)\.?([0-9])*', version)
        if matches is not None:
            parts = matches.group(0).split('.', 3)
        else:
            parts = ''
        parts = list(map(int, parts))
        major, minor, patch = (lambda x=INVALID_VERSION, y=INVALID_VERSION, z=INVALID_VERSION: (x, y, z))(*parts)
        return cls(major=major, minor=minor, patch=patch)

    def validate(self) -> bool:
        is_byte = lambda x:  0x00 <= x <= 0xFF
        try:
            for part, name in zip([self.major, self.minor, self.patch], ['major', 'minor', 'patch']):
                assert is_byte(part), f'Semantic version part: "{name}" is not a byte. Got: {part}'
        except AssertionError as e:
            logger.error(e)
            return False
        return True

    def stringify(self, include_dots: True):
        parts = list(map(str, [self.major, self.minor, self.patch]))
        return '.'.join(parts) if include_dots else ''.join(parts)

    def __str__(self):
        return self.stringify(include_dots=True)
