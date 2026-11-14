"""AUTOSAR E2E checks of SOME/IP messages: the CRC, Length and Data ID of the E2E header, and the counter sequence.

For SOME/IP, the data the E2E profile protects is the SOME/IP header from the Request ID on (Client ID, Session
ID, protocol and interface version, message type, return code: the 8-byte "upper header") followed by the payload,
and the E2E header sits right after the SOME/IP header — at bit offset 64 of that data. This is the "SOME/IP use
case" of the AUTOSAR E2E protocol examples (PRS_E2EProtocol R23-11, tables 6.43, 6.65 and 6.72), which the tests
reproduce. The Message ID and Length of the SOME/IP header are not protected (the E2E Length field covers the
protected data instead).

Profiles 4, 6 and 7 are supported — the profiles meant for SOME/IP — with a configurable header offset. Which
messages are protected, with which profile and Data ID, is configured per service and method/event
(`SomeIpE2eConfig`), saved as JSON. It is not read from ARXML: there, E2E for SOME/IP goes through the E2E
transformer of the ISignals serialized into the PDU, and the PDU's SOME/IP message id through the socket
connection's PDU identifiers — a chain CANExplorer does not resolve yet.

Messages are checked in arrival order: besides the CRC, each message's counter is compared with the previous one
of the same service, method and client (PRS state machine statuses): OK, REPEATED, OK with lost messages (a gap
up to `max_delta_counter`), or WRONG SEQUENCE.
"""

import json
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Hashable, Iterable, List, Optional, Tuple, Union

from can_explorer.core.e2e import LENGTH_PROFILES, Profile4, Profile6, Profile7
from can_explorer.core.someip import SomeIpMessage

PROFILES = tuple(sorted(LENGTH_PROFILES))
UPPER_HEADER = 8  # Bytes of the SOME/IP header protected with the payload (Request ID .. Return Code)


class SomeIpE2eError(ValueError):
    pass


def _number(value: Union[int, str], what: str) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        raise SomeIpE2eError(f'The {what} {value!r} is not a number (e.g. 0x1234)') from None


@dataclass(frozen=True)
class SomeIpE2eConfig:
    """The E2E protection of one SOME/IP method or event."""

    service_id: int
    method_id: int  # Method or event id (events have the high bit set, e.g. 0x8001)
    profile: int  # 4, 6 or 7
    data_id: int
    offset: int = UPPER_HEADER * 8  # Bit offset of the E2E header in the protected data (64: after the SOME/IP header)
    max_delta_counter: int = 1
    name: str = ''

    def __post_init__(self):
        if self.profile not in LENGTH_PROFILES:
            raise SomeIpE2eError(
                f'Profile {self.profile} is not supported for SOME/IP; use {", ".join(map(str, PROFILES))}'
            )
        if not 0 <= self.service_id <= 0xFFFF or not 0 <= self.method_id <= 0xFFFF:
            raise SomeIpE2eError('Service and method ids are 16-bit numbers')
        if self.offset % 8 or self.offset < 0:
            raise SomeIpE2eError('The E2E header offset is a whole number of bytes, in bits (e.g. 64)')
        if self.max_delta_counter < 1:
            raise SomeIpE2eError('The maximum counter delta is at least 1')
        self.profile_checker()  # Validates the Data ID width

    @property
    def key(self) -> Tuple[int, int]:
        return self.service_id, self.method_id

    @property
    def label(self) -> str:
        return f'E2E P{self.profile:02d}'

    def profile_checker(self) -> Union[Profile4, Profile6, Profile7]:
        try:
            return LENGTH_PROFILES[self.profile](self.data_id, self.offset // 8)
        except ValueError as e:
            raise SomeIpE2eError(str(e)) from None

    def as_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data['service_id'] = f'0x{self.service_id:04X}'
        data['method_id'] = f'0x{self.method_id:04X}'
        data['data_id'] = f'0x{self.data_id:X}'
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> 'SomeIpE2eConfig':
        try:
            return cls(
                service_id=_number(data['service_id'], 'service id'),
                method_id=_number(data['method_id'], 'method id'),
                profile=_number(data['profile'], 'profile'),
                data_id=_number(data['data_id'], 'Data ID'),
                offset=_number(data.get('offset', UPPER_HEADER * 8), 'offset'),
                max_delta_counter=_number(data.get('max_delta_counter', 1), 'maximum counter delta'),
                name=str(data.get('name', '')),
            )
        except KeyError as e:
            raise SomeIpE2eError(f'An E2E protection needs {e.args[0]}') from None


def protected_data(message: SomeIpMessage) -> bytes:
    """The bytes E2E protects in a SOME/IP message: the upper header (Request ID .. Return Code) and the payload."""
    upper = struct.pack(
        '>HHBBBB',
        message.client_id,
        message.session_id,
        message.protocol_version,
        message.interface_version,
        message.message_type,
        message.return_code,
    )
    return upper + bytes(message.payload)


@dataclass(frozen=True)
class SomeIpE2eResult:
    config: SomeIpE2eConfig
    ok: bool
    status: str  # 'OK', 'OK (n lost)', 'REPEATED', 'WRONG SEQUENCE', 'CRC ERROR', 'ERROR'
    counter: int = 0
    crc: int = 0
    expected_crc: int = 0
    detail: str = ''

    def describe(self) -> str:
        label = self.config.label
        if self.status == 'CRC ERROR':
            width = {4: 8, 6: 4, 7: 16}[self.config.profile]
            return (
                f'{label} CRC ERROR 0x{self.crc:0{width}X}, expected 0x{self.expected_crc:0{width}X}, '
                f'counter {self.counter}'
            )
        if self.status == 'ERROR':
            return f'{label} ERROR: {self.detail}'
        text = f'{label} {self.status.lower() if self.status.startswith("OK") else self.status}, counter {self.counter}'
        return f'{text} ({self.detail})' if self.detail else text


class SomeIpE2eChecker:
    """Checks the SOME/IP messages that have an E2E configuration (feed them in arrival order)."""

    def __init__(self, configs: Iterable[SomeIpE2eConfig] = ()):
        self.configs: Dict[Tuple[int, int], SomeIpE2eConfig] = {}
        for config in configs:
            if config.key in self.configs:
                raise SomeIpE2eError(f'Two E2E protections for {config.service_id:04X}.{config.method_id:04X}')
            self.configs[config.key] = config
        self._checkers = {key: config.profile_checker() for key, config in self.configs.items()}
        self._last: Dict[Hashable, int] = {}

    def __bool__(self) -> bool:
        return bool(self.configs)

    def reset(self) -> None:
        self._last.clear()

    def check(self, message: SomeIpMessage) -> Optional[SomeIpE2eResult]:
        """The E2E result of a message, or None if its method/event has no E2E configuration."""
        config = self.configs.get((message.service_id, message.method_id))
        if config is None:
            return None
        result = self._checkers[config.key].check(protected_data(message))
        if result.error:
            return SomeIpE2eResult(
                config, False, 'ERROR', result.counter, result.crc, result.expected_crc, result.error
            )
        if not result.ok:
            return SomeIpE2eResult(config, False, 'CRC ERROR', result.counter, result.crc, result.expected_crc)
        stream = (message.service_id, message.method_id, message.client_id)
        previous = self._last.get(stream)
        self._last[stream] = result.counter
        if previous is None:
            return SomeIpE2eResult(config, True, 'OK', result.counter, result.crc, result.expected_crc)
        modulus = 1 << (8 * self._checkers[config.key].COUNTER[1])
        delta = (result.counter - previous) % modulus
        if delta == 0:
            return SomeIpE2eResult(config, False, 'REPEATED', result.counter, result.crc, result.expected_crc)
        if delta > config.max_delta_counter:
            return SomeIpE2eResult(
                config, False, 'WRONG SEQUENCE', result.counter, result.crc, result.expected_crc, f'after {previous}'
            )
        lost = f'{delta - 1} lost' if delta > 1 else ''
        return SomeIpE2eResult(config, True, 'OK', result.counter, result.crc, result.expected_crc, lost)


def load_configs(path: Union[str, Path]) -> List[SomeIpE2eConfig]:
    """The E2E protections of a JSON file: a list of objects with service_id, method_id, profile, data_id and
    optionally offset (bits), max_delta_counter and name; numbers as integers or strings such as "0x8001"."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise SomeIpE2eError(f'Cannot read {path}: {e}') from e
    if isinstance(data, dict):
        data = data.get('protections', [])
    if not isinstance(data, list):
        raise SomeIpE2eError(f'{path} does not hold a list of E2E protections')
    return [SomeIpE2eConfig.from_dict(entry) for entry in data]


def save_configs(path: Union[str, Path], configs: Iterable[SomeIpE2eConfig]) -> None:
    text = json.dumps({'protections': [config.as_dict() for config in configs]}, indent=2)
    Path(path).write_text(text + '\n', encoding='utf-8')
