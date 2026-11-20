"""SAE J1939-22 assurance data trailers: the cybersecurity and functional safety data a contained parameter group
(C-PG) of a Multi-PG frame can carry after its PG data, and their checks.

What is public, and taken as such:

- **Which trailer a C-PG carries.** Its type of service (TOS) and trailer format (TF) select it, from the table of
  SAE J1939-22 reproduced in New Eagle's "J1939-91C in Practice" (Raptor Innovation Summit 2025, slide 12):
  TOS 1 with TF 1/2/3 = 32-bit manufacturer specific (OEM) cybersecurity / functional safety / cybersecurity
  followed by functional safety assurance data, TF 5/6 = 64-bit OEM cybersecurity / functional safety assurance
  data, TF 0/4/7 reserved; TOS 2 TF 0 = "SAE J1939 with no assurance data" (TF 1..7 reserved); TOS 3..7 reserved.
  The C-PG payload length counts the PG data and the trailer, so the trailer is the last 4 or 8 (or 4 + 4) bytes
  of the payload — which the same deck's bus trace shows (a TOS 1 / TF 5 C-PG of 12 bytes: 4 data + 8 trailer).
  So the PG data of a C-PG with assurance data is known exactly and decoded like any PG; this is not a guess.
- **Functional safety: AUTOSAR E2E Profile 22.** The safety data is manufacturer specific; Profile 22 (an 8-bit CRC
  and a 4-bit counter, the public AUTOSAR PRS_E2EProtocol, core/e2e.py) is what AUTOSAR stacks and New Eagle's
  Raptor J1939-22 blocks use for it (slides 31-32: "E2E Specification: AUTOSAR E2E Profile22", 16 Data IDs, a
  byte offset, a maximum counter delta).
- **Cybersecurity: SAE J1939-91C secure messages.** The trailer is the freshness value FV, the encryption flag E and
  the truncated tag; Tag = trunc(CMAC(S_V-MAC, nonce || PT)) with nonce = ((E << 23) | PGN) || SA || FV (New Eagle,
  slide 22, a figure from J1939-91C). AES-CMAC under the network's session key.

What is not public, and therefore a **configuration** here, never assumed: SAE J1939-22 and J1939-91C are paywalled
and neither source gives the field widths or byte encodings. So a check runs only for the PGNs the user configures
(`AssuranceConfig`, saved as JSON) and with the layout the user gives:

- safety: the 16 Data IDs, the offset of the Profile 22 header (CRC byte, then the counter nibble) inside the safety
  trailer, the maximum counter delta. The data Profile 22 protects is the whole C-PG payload — PG data and
  trailers, header at the configured place in the safety trailer (the CRC then covers everything before and after
  it, PRS_E2E_00527). That coverage is CANExplorer's choice, named as such.
- security: the key (a SecOC key of the key store, or hex), the freshness value and tag widths in bits (with the
  one-bit E flag between them they fill the trailer, packed most significant bit first), and the nonce fields packed
  big endian: (E << 23 | PGN) in 3 bytes, SA in 1, FV in as many bytes as its width needs. Also a choice.

A wrong layout cannot pass: a CRC or a CMAC does not match by accident, so a configuration that verifies is right,
and one that does not is reported as "does not verify with this configuration" — not as forged data. Payloads
flagged as encrypted (E = 1) are shown as such and not checked: the cipher is not public either.

FD.TP transfers announce assurance data (type and size, in RTS/BAM/EndOfMsgStatus) but where its bytes travel is
not in any public source (the AUTOSAR J1939Tp has no FD transport); those stay labelled only
(core/protocols/j1939.py).
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Hashable, Iterable, List, Optional, Tuple, Union

from can_explorer.core.e2e import Profile22

TOS_ASSURED = 1  # Type of service 1: a trailer with assurance data
TOS_NO_ASSURANCE, TF_NO_ASSURANCE = 2, 0

CYBERSECURITY, SAFETY = 'cybersecurity', 'functional safety'


@dataclass(frozen=True)
class TrailerFormat:
    """The assurance data a (type of service, trailer format) pair stands for."""

    tos: int
    tf: int
    security_bytes: int  # The cybersecurity assurance data (first in the trailer)
    safety_bytes: int  # The functional safety assurance data (after any cybersecurity data)
    description: str

    @property
    def size(self) -> int:
        return self.security_bytes + self.safety_bytes


# SAE J1939-22 trailer formats (see the module docstring for the source); every other pair is reserved
TRAILER_FORMATS: Dict[Tuple[int, int], TrailerFormat] = {
    (1, 1): TrailerFormat(1, 1, 4, 0, '32-bit OEM cybersecurity assurance data'),
    (1, 2): TrailerFormat(1, 2, 0, 4, '32-bit OEM functional safety assurance data'),
    (1, 3): TrailerFormat(
        1, 3, 4, 4, '32-bit OEM cybersecurity followed by 32-bit OEM functional safety assurance data'
    ),
    (1, 5): TrailerFormat(1, 5, 8, 0, '64-bit OEM cybersecurity assurance data'),
    (1, 6): TrailerFormat(1, 6, 0, 8, '64-bit OEM functional safety assurance data'),
    (2, 0): TrailerFormat(2, 0, 0, 0, 'SAE J1939 with no assurance data'),
}


def trailer_format(tos: int, tf: int) -> Optional[TrailerFormat]:
    """The trailer format of a C-PG header, or None for a reserved combination."""
    return TRAILER_FORMATS.get((tos, tf))


@dataclass(frozen=True)
class AssuredPayload:
    """A C-PG payload split into its PG data and its assurance data trailers."""

    format: Optional[TrailerFormat]  # None: a reserved TOS/TF, nothing split off
    data: bytes  # The PG data (all of the payload for a reserved format)
    security: bytes = b''
    safety: bytes = b''
    error: str = ''  # Why the payload could not be split (shorter than its trailer, reserved format)

    @property
    def trailer(self) -> bytes:
        return self.security + self.safety


def split_payload(tos: int, tf: int, payload: bytes) -> AssuredPayload:
    """Split a C-PG payload (the payload-length bytes after the C-PG header) into PG data and trailers."""
    payload = bytes(payload)
    fmt = trailer_format(tos, tf)
    if fmt is None:
        return AssuredPayload(None, payload, error=f'reserved type of service {tos} / trailer format {tf}')
    if len(payload) < fmt.size:
        return AssuredPayload(fmt, payload, error=f'{len(payload)} B, shorter than its {fmt.size}-byte trailer')
    end = len(payload) - fmt.size
    return AssuredPayload(
        fmt, payload[:end], payload[end : end + fmt.security_bytes], payload[end + fmt.security_bytes :]
    )


def join_payload(data: bytes, security: bytes = b'', safety: bytes = b'') -> bytes:
    return bytes(data) + bytes(security) + bytes(safety)


# --- Configuration ---


class AssuranceError(ValueError):
    pass


def _number(value: Union[int, str], what: str) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        raise AssuranceError(f'The {what} {value!r} is not a number (e.g. 0x0B00)') from None


@dataclass(frozen=True)
class SafetyConfig:
    """Functional safety assurance data checked with AUTOSAR E2E Profile 22."""

    data_ids: Tuple[int, ...]  # 16 Data IDs, one per counter value
    offset: int = 0  # Byte offset of the Profile 22 header (CRC, then counter nibble) in the safety trailer
    max_delta: int = 1  # Largest accepted counter step (higher: wrong sequence)

    def __post_init__(self):
        if len(self.data_ids) != 16 or not all(0 <= d <= 0xFF for d in self.data_ids):
            raise AssuranceError('Profile 22 needs 16 Data IDs of one byte each')
        if self.offset < 0:
            raise AssuranceError('The Profile 22 offset cannot be negative')
        if not 1 <= self.max_delta <= 15:
            raise AssuranceError('The maximum counter delta is 1..15')

    def as_dict(self) -> Dict[str, object]:
        return {
            'profile': 22,
            'data_ids': [f'0x{d:02X}' for d in self.data_ids],
            'offset': self.offset,
            'max_delta': self.max_delta,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> 'SafetyConfig':
        profile = _number(data.get('profile', 22), 'E2E profile')
        if profile != 22:
            raise AssuranceError(f'Only E2E Profile 22 is supported for J1939-22 safety data, not {profile}')
        ids = data.get('data_ids')
        if not isinstance(ids, list):
            raise AssuranceError('The safety check needs data_ids: a list of 16 Data IDs')
        return cls(
            tuple(_number(d, 'Data ID') for d in ids),
            _number(data.get('offset', 0), 'offset'),
            _number(data.get('max_delta', 1), 'maximum counter delta'),
        )


@dataclass(frozen=True)
class SecurityConfig:
    """Cybersecurity assurance data checked as a J1939-91C secure message (FV | E | Tag, AES-CMAC)."""

    key: str  # 'keystore:<name>' (a SecOC key entry) or the key in hex
    freshness_bits: int
    tag_bits: int

    def __post_init__(self):
        if not self.key:
            raise AssuranceError('The cybersecurity check needs a key (keystore:<name> or hex)')
        if self.freshness_bits < 1 or self.tag_bits < 8 or self.tag_bits > 128:
            raise AssuranceError('The freshness value needs at least 1 bit and the tag 8..128 bits')

    @property
    def bits(self) -> int:
        return self.freshness_bits + 1 + self.tag_bits  # The E flag sits between FV and the tag

    def as_dict(self) -> Dict[str, object]:
        return {'key': self.key, 'freshness_bits': self.freshness_bits, 'tag_bits': self.tag_bits}

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> 'SecurityConfig':
        try:
            return cls(
                str(data['key']),
                _number(data['freshness_bits'], 'freshness value width'),
                _number(data['tag_bits'], 'tag width'),
            )
        except KeyError as e:
            raise AssuranceError(
                f'The cybersecurity check needs {e.args[0]} (the layout is manufacturer specific)'
            ) from None


@dataclass(frozen=True)
class AssuranceConfig:
    """The assurance data checks of one parameter group (from one source address, or any)."""

    pgn: int
    source: Optional[int] = None  # None: from any source address
    safety: Optional[SafetyConfig] = None
    security: Optional[SecurityConfig] = None
    name: str = ''

    def __post_init__(self):
        if not 0 <= self.pgn <= 0x3FFFF:
            raise AssuranceError(f'PGN 0x{self.pgn:X} is not an 18-bit PGN')
        if self.source is not None and not 0 <= self.source <= 0xFF:
            raise AssuranceError('The source address is one byte')
        if self.safety is None and self.security is None:
            raise AssuranceError(f'PGN 0x{self.pgn:05X}: configure a safety check, a cybersecurity check or both')

    @property
    def key(self) -> Tuple[int, Optional[int]]:
        return self.pgn, self.source

    def as_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {'pgn': f'0x{self.pgn:05X}'}
        if self.source is not None:
            data['source'] = f'0x{self.source:02X}'
        if self.name:
            data['name'] = self.name
        if self.safety is not None:
            data['safety'] = self.safety.as_dict()
        if self.security is not None:
            data['security'] = self.security.as_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> 'AssuranceConfig':
        if 'pgn' not in data:
            raise AssuranceError('An assurance configuration needs a pgn')
        source = data.get('source')
        safety, security = data.get('safety'), data.get('security')
        return cls(
            _number(data['pgn'], 'PGN'),
            None if source in (None, '', '*') else _number(source, 'source address'),
            SafetyConfig.from_dict(safety) if isinstance(safety, dict) else None,
            SecurityConfig.from_dict(security) if isinstance(security, dict) else None,
            str(data.get('name', '')),
        )


CONFIG_VARIABLE = 'CANEXPLORER_J1939_ASSURANCE'
CONFIG_FILE = 'j1939_assurance.json'


def default_config_path(environ=os.environ) -> Path:
    """`CANEXPLORER_J1939_ASSURANCE`, else j1939_assurance.json next to the key store (the application data
    directory)."""
    if environ.get(CONFIG_VARIABLE):
        return Path(environ[CONFIG_VARIABLE])
    from can_explorer.core import keystore

    return keystore.default_path(environ).parent / CONFIG_FILE


def load_configs(path: Union[str, Path]) -> List[AssuranceConfig]:
    """The assurance configurations of a JSON file: {"pgs": [...]} or a bare list (see `AssuranceConfig.as_dict`)."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise AssuranceError(f'Cannot read {path}: {e}') from e
    if isinstance(data, dict):
        data = data.get('pgs', [])
    if not isinstance(data, list):
        raise AssuranceError(f'{path} does not hold a list of assurance configurations')
    return [AssuranceConfig.from_dict(entry) for entry in data]


def save_configs(path: Union[str, Path], configs: Iterable[AssuranceConfig]) -> None:
    text = json.dumps({'pgs': [config.as_dict() for config in configs]}, indent=2)
    Path(path).write_text(text + '\n', encoding='utf-8')


# --- Cybersecurity: J1939-91C secure messages ---


@dataclass(frozen=True)
class SecureTrailer:
    freshness: int
    encrypted: bool
    tag: int


def unpack_security(trailer: bytes, config: SecurityConfig) -> SecureTrailer:
    """FV, E and the tag of a cybersecurity trailer, packed most significant bit first."""
    if config.bits != len(trailer) * 8:
        raise AssuranceError(
            f'{config.freshness_bits} + 1 + {config.tag_bits} bits do not fill the {len(trailer) * 8}-bit trailer'
        )
    value = int.from_bytes(trailer, 'big')
    tag = value & ((1 << config.tag_bits) - 1)
    encrypted = bool((value >> config.tag_bits) & 1)
    return SecureTrailer(value >> (config.tag_bits + 1), encrypted, tag)


def pack_security(freshness: int, encrypted: bool, tag: int, config: SecurityConfig) -> bytes:
    if not 0 <= freshness < 1 << config.freshness_bits:
        raise AssuranceError(f'Freshness value {freshness} does not fit {config.freshness_bits} bits')
    value = (freshness << (config.tag_bits + 1)) | (int(encrypted) << config.tag_bits) | tag
    return value.to_bytes(config.bits // 8 + (1 if config.bits % 8 else 0), 'big')


def nonce(pgn: int, source: int, freshness: int, encrypted: bool, freshness_bits: int) -> bytes:
    """((E << 23) | PGN) || SA || FV (J1939-91C, as shown by New Eagle), packed big endian (CANExplorer's choice)."""
    head = ((int(encrypted) << 23) | (pgn & 0x3FFFF)).to_bytes(3, 'big')
    return head + bytes([source & 0xFF]) + freshness.to_bytes((freshness_bits + 7) // 8, 'big')


def secure_tag(key: bytes, pgn: int, source: int, freshness: int, encrypted: bool, data: bytes, config) -> int:
    """trunc(CMAC(key, nonce || PT)): the leading `tag_bits` bits of the AES-CMAC."""
    from can_explorer.core.secoc import cmac_aes

    full = cmac_aes(key, nonce(pgn, source, freshness, encrypted, config.freshness_bits) + bytes(data))
    return int.from_bytes(full, 'big') >> (128 - config.tag_bits)


def resolve_key(source: str) -> bytes:
    """A key from 'keystore:<name>' (a SecOC key entry of the application's key store, unlocked) or hex."""
    if source.startswith('keystore:'):
        from can_explorer.core import keystore

        name = source[len('keystore:') :]
        store = keystore.default_store()
        try:
            info = store.entries_by_name().get(name)
            if info is None or info.purpose != keystore.SECOC:
                raise AssuranceError(f'No SecOC key named {name!r} in the Security Manager')
            return bytes(store.secrets(name)['key'])
        except keystore.KeyStoreError as e:
            raise AssuranceError(f'Key {name!r}: {e}') from e
    try:
        return bytes.fromhex(source.replace(' ', ''))
    except ValueError:
        raise AssuranceError('The key is keystore:<name> or hex') from None


# --- Checking ---


@dataclass
class AssuranceCheck:
    """The outcome of the checks of one C-PG."""

    config: AssuranceConfig
    ok: bool = True
    parts: List[str] = field(default_factory=list)

    def fail(self, text: str) -> None:
        self.ok = False
        self.parts.append(text)

    def describe(self) -> str:
        return '; '.join(self.parts)


class AssuranceChecker:
    """Checks the assurance data trailers of configured parameter groups (feed C-PGs in arrival order): the
    Profile 22 CRC and counter sequence of safety data, and the J1939-91C tag and freshness of cybersecurity data."""

    def __init__(self, configs: Iterable[AssuranceConfig] = (), keys: Callable[[str], bytes] = resolve_key):
        self.configs: Dict[Tuple[int, Optional[int]], AssuranceConfig] = {}
        for config in configs:
            if config.key in self.configs:
                raise AssuranceError(f'Two assurance configurations for PGN 0x{config.pgn:05X}')
            self.configs[config.key] = config
        self._keys = keys
        self._key_cache: Dict[str, bytes] = {}
        self._counters: Dict[Hashable, int] = {}
        self._freshness: Dict[Hashable, int] = {}

    def __bool__(self) -> bool:
        return bool(self.configs)

    def reset(self) -> None:
        self._counters.clear()
        self._freshness.clear()

    def config_for(self, pgn: int, source: int) -> Optional[AssuranceConfig]:
        return self.configs.get((pgn, source)) or self.configs.get((pgn, None))

    def _key(self, source: str) -> bytes:
        if source not in self._key_cache:
            self._key_cache[source] = self._keys(source)  # Raises AssuranceError (e.g. key store locked)
        return self._key_cache[source]

    def check(self, pgn: int, source: int, tos: int, tf: int, payload: bytes) -> Optional[AssuranceCheck]:
        """The checks of a C-PG, or None when its PGN has no configuration."""
        config = self.config_for(pgn, source)
        if config is None:
            return None
        result = AssuranceCheck(config)
        split = split_payload(tos, tf, payload)
        if split.error:
            result.fail(f'not checked: {split.error}')
            return result
        stream = (pgn, source)
        if config.safety is not None:
            if not split.safety:
                result.fail('no functional safety data (expected by the configuration)')
            else:
                self._check_safety(result, config.safety, split, stream)
        if config.security is not None:
            if not split.security:
                result.fail('no cybersecurity data (expected by the configuration)')
            else:
                self._check_security(result, config.security, pgn, source, split, stream)
        return result

    def _check_safety(self, result: AssuranceCheck, safety: SafetyConfig, split, stream) -> None:
        if safety.offset + 2 > len(split.safety):
            result.fail(
                f'E2E P22: the header at byte {safety.offset} does not fit the {len(split.safety)}-byte trailer'
            )
            return
        protected = split.data + split.security + split.safety
        offset = len(split.data) + len(split.security) + safety.offset
        check = Profile22(safety.data_ids, offset).check(protected)
        if not check.ok:
            result.fail(
                f'E2E P22 CRC 0x{check.crc:02X} does not verify with this configuration (expected '
                f'0x{check.expected_crc:02X}), counter {check.counter}'
            )
            return
        previous = self._counters.get(stream)
        self._counters[stream] = check.counter
        text = f'E2E P22 OK, counter {check.counter}'
        if previous is not None:
            delta = (check.counter - previous) % 16
            if delta == 0:
                result.fail(f'E2E P22 REPEATED counter {check.counter}')
                return
            if delta > safety.max_delta:
                result.fail(f'E2E P22 WRONG SEQUENCE: counter {check.counter} after {previous}')
                return
            if delta > 1:
                text += f' ({delta - 1} lost)'
        result.parts.append(text)

    def _check_security(self, result: AssuranceCheck, security: SecurityConfig, pgn, source, split, stream) -> None:
        try:
            fields = unpack_security(split.security, security)
        except AssuranceError as e:
            result.fail(f'cybersecurity: {e}')
            return
        if fields.encrypted:
            result.parts.append(f'encrypted payload (FV {fields.freshness}): not decrypted, tag not checked')
            return
        try:
            key = self._key(security.key)
            expected = secure_tag(key, pgn, source, fields.freshness, False, split.data, security)
        except (AssuranceError, ValueError) as e:
            result.parts.append(f'tag not checked: {e}')
            return
        width = (security.tag_bits + 3) // 4
        if expected != fields.tag:
            result.fail(f'tag {fields.tag:0{width}X} does not verify with this configuration (FV {fields.freshness})')
            return
        previous = self._freshness.get(stream)
        if previous is not None and fields.freshness <= previous:
            result.fail(f'tag OK but FV {fields.freshness} not above {previous}: replayed')
            return
        self._freshness[stream] = fields.freshness
        result.parts.append(f'tag OK, FV {fields.freshness}')


# --- Building (simulations, tests, sending) ---


def protect(
    config: AssuranceConfig,
    tos: int,
    tf: int,
    data: bytes,
    source: int,
    counter: int = 0,
    freshness: int = 0,
    keys: Callable[[str], bytes] = resolve_key,
    security_trailer: bytes = b'',
    safety_trailer: bytes = b'',
) -> bytes:
    """The C-PG payload of `data` with the trailers of `tos`/`tf` filled per `config`: the J1939-91C tag over the
    data, then the Profile 22 header in the safety trailer. Trailers without a configured check are taken from
    `security_trailer`/`safety_trailer` (zeros by default)."""
    fmt = trailer_format(tos, tf)
    if fmt is None:
        raise AssuranceError(f'Type of service {tos} / trailer format {tf} is reserved')
    security = bytes(security_trailer).ljust(fmt.security_bytes, b'\x00')[: fmt.security_bytes]
    safety = bytes(safety_trailer).ljust(fmt.safety_bytes, b'\x00')[: fmt.safety_bytes]
    if fmt.security_bytes and config.security is not None:
        tag = secure_tag(keys(config.security.key), config.pgn, source, freshness, False, data, config.security)
        security = pack_security(freshness, False, tag, config.security)
        if len(security) != fmt.security_bytes:
            raise AssuranceError(f'The configured layout does not fill the {fmt.security_bytes}-byte trailer')
    payload = join_payload(data, security, safety)
    if fmt.safety_bytes and config.safety is not None:
        offset = len(data) + len(security) + config.safety.offset
        if config.safety.offset + 2 > fmt.safety_bytes:
            raise AssuranceError('The Profile 22 header does not fit the safety trailer')
        payload = Profile22(config.safety.data_ids, offset).protect(payload, counter)
    return payload
