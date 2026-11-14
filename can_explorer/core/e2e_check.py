"""Live AUTOSAR E2E checks of signal PDUs, configured from the E2E protections of an ARXML system description.

An ARXML END-TO-END-PROTECTION-SET names, per protected I-SIGNAL-I-PDU, the E2E profile (the CATEGORY of its
END-TO-END-PROFILE) and its Data IDs; cantools reads them into `message.autosar.e2e` when it loads the ARXML (with
the protected length). This module turns that configuration into a check of each received frame with the profiles
of core/e2e.py (2, 5 and 22, verified against the AUTOSAR E2E Protocol Specification examples).

The profile is taken from the CATEGORY only (e.g. PROFILE_05). It is never inferred from the number of Data IDs:
16 IDs fit both profile 2 and 22, and one ID fits profile 5 as well as 1 and 11. A protection whose profile is not
recognised, or not implemented, is shown as not checked. Frames are checked one by one: the CRC is verified and the
counter shown, but lost or repeated frames (gaps in the counter sequence) are not tracked.
"""

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union

from can_explorer.core.e2e import Profile2, Profile5, Profile22

SUPPORTED_PROFILES = (2, 5, 22)
_PROFILE = re.compile(r'profile[\s_-]*0*(\d+)', re.IGNORECASE)


def profile_of(category: Optional[str]) -> Optional[int]:
    """The profile number of an E2E CATEGORY or profile name: 'PROFILE_05' -> 5, 'Profile22' -> 22; None if absent."""
    match = _PROFILE.search(category or '')
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class E2eCheck:
    checked: bool
    ok: bool = False
    counter: int = 0
    crc: int = 0
    expected_crc: int = 0
    reason: str = ''  # Why the frame was not checked

    def describe(self, label: str) -> str:
        if not self.checked:
            return f'{label}: not checked ({self.reason})'
        if self.ok:
            return f'{label} ok, counter {self.counter}'
        width = 4 if max(self.crc, self.expected_crc) > 0xFF else 2
        return (
            f'{label} CRC ERROR 0x{self.crc:0{width}X}, expected 0x{self.expected_crc:0{width}X}, '
            f'counter {self.counter}'
        )


@dataclass(frozen=True)
class E2eConfig:
    category: str
    data_ids: Tuple[int, ...]
    length: Optional[int] = None  # Protected bytes from the start of the PDU (None: the whole frame)

    @property
    def profile(self) -> Optional[int]:
        return profile_of(self.category)

    @property
    def label(self) -> str:
        profile = self.profile
        return f'E2E P{profile:02d}' if profile is not None else f'E2E {self.category or "(no category)"}'

    def _checker(self) -> Union[Profile2, Profile5, Profile22, str]:
        """The profile's checker, or why there is none."""
        profile = self.profile
        if profile is None:
            return f'profile not recognised from category {self.category!r}' if self.category else 'no profile category'
        if profile not in SUPPORTED_PROFILES:
            return f'profile {profile} is not implemented'
        try:
            if profile == 5:
                if len(self.data_ids) != 1:
                    return f'profile 5 needs one Data ID, the ARXML gives {len(self.data_ids)}'
                return Profile5(self.data_ids[0])
            return (Profile2 if profile == 2 else Profile22)(self.data_ids)
        except ValueError as e:  # Profiles 2 and 22 need 16 Data IDs
            return f'{e}, the ARXML gives {len(self.data_ids)}'

    @property
    def problem(self) -> str:
        """Why frames with this configuration cannot be checked; '' when they can."""
        checker = self._checker()
        return checker if isinstance(checker, str) else ''

    def check(self, data: bytes) -> E2eCheck:
        checker = self._checker()
        if isinstance(checker, str):
            return E2eCheck(checked=False, reason=checker)
        protected = bytes(data[: self.length] if self.length else data)
        minimum = 3 if isinstance(checker, Profile5) else 2
        if len(protected) < minimum:
            return E2eCheck(checked=False, reason=f'frame shorter than the {minimum}-byte E2E header')
        result = checker.check(protected)
        return E2eCheck(True, result.ok, result.counter, result.crc, result.expected_crc)

    def describe(self, data: bytes) -> str:
        return self.check(data).describe(self.label)


def e2e_config_of(message) -> Optional[E2eConfig]:
    """The E2E configuration cantools read for a database message (`message.autosar.e2e`), if it has one."""
    autosar = getattr(message, 'autosar', None)
    properties = getattr(autosar, 'e2e', None) if autosar is not None else None
    if properties is None:
        return None
    data_ids: Sequence[int] = properties.data_ids or ()
    return E2eConfig(properties.category or '', tuple(data_ids), properties.payload_length or None)
