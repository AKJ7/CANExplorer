"""Live AUTOSAR SecOC checks of secured PDUs configured in an ARXML system description, and securing PDUs to send.

cantools reads a SECURED-I-PDU's configuration into `message.autosar.secoc`: the Data ID, the length of the
authentic payload, the full and transmitted (truncated) freshness value lengths, the transmitted authenticator
length and the names of the authentication and freshness algorithms. A secured PDU is the payload followed by a
bit-packed trailer, most significant bit first:

    [ authentic payload | truncated freshness value (FreshnessValueTxLength bits) | truncated MAC (AuthInfoTxLength) ]

The MAC is computed over the Data ID (16 bits), the authentic payload and the *full* freshness value, packed the
same way and padded with zero bits to whole bytes, then truncated to its most significant bits.

What AUTOSAR leaves to the manufacturer, and how it is handled here:

- **The algorithm.** The ARXML only names it. Names containing CMAC are taken as CMAC over AES (NIST SP 800-38B)
  and names containing HMAC with SHA-256 as HMAC-SHA-256; a key in the key store can set the algorithm for PDUs
  whose ARXML name is not recognised. Anything else is shown as not checked, never guessed.
- **The keys** come from the key store (core/keystore.py), looked up by PDU name, Data ID or '*'.
- **The freshness value.** The built-in manager treats it as a monotonic counter: when only its low bits are sent,
  the high bits are taken from the last verified value of that PDU, incremented when the low bits wrapped (the
  usual counter reconstruction). Until a frame of a PDU has been verified the high bits are unknown and assumed
  zero, and a failure is reported as possibly unsynchronised. Manufacturer schemes (trip/reset counters,
  synchronisation messages, time-based values) are proprietary: give them as a Python file (`load_freshness_script`).
"""

import hashlib
import hmac
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Hashable, List, Optional, Protocol, Tuple

from can_explorer.core import audit, keystore
from can_explorer.core.keystore import KeyStore, KeyStoreError
from can_explorer.core.secoc import cmac_aes

ALGORITHMS = ('cmac-aes', 'hmac-sha256')


class SecOcError(Exception):
    pass


def algorithm_of(name: Optional[str]) -> Optional[str]:
    """The authentication algorithm an ARXML name stands for: 'CMAC_AES128' -> 'cmac-aes', 'HMAC-SHA256' ->
    'hmac-sha256'; None when it is not recognised."""
    folded = ''.join(character for character in (name or '').lower() if character.isalnum())
    if 'cmac' in folded:
        return 'cmac-aes'
    if 'hmac' in folded and 'sha256' in folded:
        return 'hmac-sha256'
    return None


def compute_mac(algorithm: str, key: bytes, data: bytes) -> bytes:
    """The full (untruncated) MAC of `data`."""
    if algorithm == 'cmac-aes':
        return cmac_aes(key, data)
    if algorithm == 'hmac-sha256':
        return hmac.new(key, data, hashlib.sha256).digest()
    raise SecOcError(f'Unknown SecOC algorithm {algorithm!r}')


def _bits(value: bytes, start: int, length: int) -> int:
    """`length` bits of `value` starting `start` bits from its most significant bit."""
    width = len(value) * 8
    return (int.from_bytes(value, 'big') >> (width - start - length)) & ((1 << length) - 1) if length else 0


@dataclass(frozen=True)
class SecOcConfig:
    pdu: str
    data_id: Optional[int]
    payload_length: Optional[int]  # Bytes of authentic payload
    auth_tx_bits: Optional[int]  # Transmitted (truncated) MAC bits
    freshness_bits: Optional[int]  # Full freshness value bits
    freshness_tx_bits: Optional[int]  # Transmitted (truncated) freshness bits
    auth_algorithm: str = ''  # As named in the ARXML
    freshness_algorithm: str = ''

    @property
    def problem(self) -> str:
        """What is missing from the ARXML to check this PDU; '' when complete."""
        missing = [
            what
            for what, value in (
                ('Data ID', self.data_id),
                ('payload length', self.payload_length),
                ('authenticator length', self.auth_tx_bits),
                ('freshness value length', self.freshness_bits),
            )
            if value is None
        ]
        if missing:
            return f'the ARXML gives no {", ".join(missing)}'
        if not self.auth_tx_bits:
            return 'the ARXML transmits no authenticator bits'
        if (self.freshness_tx_bits or 0) > (self.freshness_bits or 0):
            return 'the transmitted freshness value is longer than the full one'
        return ''

    @property
    def trailer_bits(self) -> int:
        return (self.freshness_tx_bits or 0) + (self.auth_tx_bits or 0)

    @property
    def length(self) -> int:
        """Bytes of the secured PDU: the payload and the trailer rounded up to whole bytes."""
        return (self.payload_length or 0) + (self.trailer_bits + 7) // 8

    def split(self, data: bytes) -> Optional[Tuple[bytes, int, int]]:
        """(authentic payload, truncated freshness value, truncated MAC as an integer), or None if too short."""
        payload_length = self.payload_length or 0
        if len(data) * 8 < payload_length * 8 + self.trailer_bits:
            return None
        start = payload_length * 8
        freshness = _bits(data, start, self.freshness_tx_bits or 0)
        mac = _bits(data, start + (self.freshness_tx_bits or 0), self.auth_tx_bits or 0)
        return bytes(data[:payload_length]), freshness, mac

    def data_to_authenticator(self, payload: bytes, freshness: int) -> bytes:
        """Data ID (16 bits) | payload | full freshness value, zero-padded to whole bytes."""
        payload_bits = (self.payload_length or 0) * 8
        freshness_bits = self.freshness_bits or 0
        total = 16 + payload_bits + freshness_bits
        value = ((self.data_id or 0) & 0xFFFF) << (payload_bits + freshness_bits)
        value |= int.from_bytes(
            bytes(payload)[: self.payload_length].ljust(self.payload_length or 0, b'\0'), 'big'
        ) << (freshness_bits)
        value |= freshness & ((1 << freshness_bits) - 1)
        padding = -total % 8
        return (value << padding).to_bytes((total + padding) // 8, 'big')

    def truncated_mac(
        self, algorithm: str, key: bytes, payload: bytes, freshness: int, authenticator: Optional[Callable] = None
    ) -> int:
        """The transmitted bits of the MAC; `authenticator` computes the full MAC instead (e.g. a security profile
        or a key server)."""
        data = self.data_to_authenticator(payload, freshness)
        full = bytes(authenticator(data)) if authenticator is not None else compute_mac(algorithm, key, data)
        bits = self.auth_tx_bits or 0
        if bits > len(full) * 8:
            raise SecOcError(f'the ARXML transmits {bits} authenticator bits, the algorithm gives {len(full) * 8}')
        return int.from_bytes(full, 'big') >> (len(full) * 8 - bits)

    def label(self) -> str:
        return f'SecOC {self.pdu}'


def secoc_config_of(message) -> Optional[SecOcConfig]:
    """The SecOC configuration cantools read for a database message (`message.autosar.secoc`), if it is secured."""
    autosar = getattr(message, 'autosar', None)
    properties = getattr(autosar, 'secoc', None) if autosar is not None else None
    if properties is None:
        return None
    return SecOcConfig(
        pdu=message.name,
        data_id=properties.data_id,
        payload_length=properties.payload_length,
        auth_tx_bits=properties.auth_tx_bit_length,
        freshness_bits=properties.freshness_bit_length,
        freshness_tx_bits=properties.freshness_tx_bit_length or 0,
        auth_algorithm=properties.auth_algorithm_name or '',
        freshness_algorithm=properties.freshness_algorithm_name or '',
    )


# --- Keys ---


@dataclass(frozen=True)
class SecOcKey:
    key: bytes
    algorithm: str  # '' to take it from the ARXML
    entry: str  # The key store entry (or 'profile:<name>'), for messages
    authenticator: Optional[Callable[[bytes], bytes]] = None  # Computes the full MAC elsewhere (no key here)

    def __repr__(self) -> str:  # Never show the key
        return f'SecOcKey(entry={self.entry!r}, algorithm={self.algorithm!r})'


class SecOcKeys(Protocol):
    def key_for(self, config: SecOcConfig) -> Tuple[Optional[SecOcKey], str]:
        """The key of a PDU, or None and why there is none."""
        ...


def _scope_matches(scope: str, config: SecOcConfig) -> int:
    """How specifically a key's scope names a PDU: 3 by name, 2 by Data ID, 1 for '*', 0 not at all."""
    best = 0
    for token in (part.strip() for part in scope.split(',')):
        if not token:
            continue
        if token == config.pdu:
            return 3
        if token == '*':
            best = max(best, 1)
            continue
        try:
            if config.data_id is not None and int(token, 0) == config.data_id:
                best = max(best, 2)
        except ValueError:
            pass
    return best


class KeyStoreSecOcKeys:
    """SecOC keys from the key store, cached per PDU until `invalidate()` (call it when the store changes)."""

    def __init__(self, store: KeyStore):
        self.store = store
        self._cache: Dict[Tuple[str, Optional[int]], Tuple[Optional[SecOcKey], str]] = {}

    def invalidate(self) -> None:
        self._cache.clear()

    def key_for(self, config: SecOcConfig) -> Tuple[Optional[SecOcKey], str]:
        selector = (config.pdu, config.data_id)
        cached = self._cache.get(selector)
        if cached is not None:
            return cached
        result = self._lookup(config)
        if result[0] is not None or not result[1].startswith('the key store is locked'):
            self._cache[selector] = result  # Not while locked: unlocking must take effect
        return result

    def _lookup(self, config: SecOcConfig) -> Tuple[Optional[SecOcKey], str]:
        try:
            entries = self.store.entries(keystore.SECOC)
        except KeyStoreError as e:
            return None, str(e)
        ranked = sorted(
            ((_scope_matches(info.params.get('scope', '*'), config), info) for info in entries),
            key=lambda pair: -pair[0],
        )
        if not ranked or ranked[0][0] == 0:
            data_id = f'0x{config.data_id:X}' if config.data_id is not None else '?'
            return None, f'no key for {config.pdu} (Data ID {data_id}) in the key store'
        info = ranked[0][1]
        if not self.store.unlocked:
            return None, 'the key store is locked'
        try:
            key = self.store.secrets(info.name)['key']
        except (KeyStoreError, KeyError) as e:
            return None, str(e)
        audit.record('secoc key used', entry=info.name, pdu=config.pdu)
        return SecOcKey(key, info.params.get('algorithm', ''), info.name), ''


class ChainedSecOcKeys:
    """Asks several key sources in turn (e.g. the security profiles, then the key store)."""

    def __init__(self, *sources):
        self.sources = list(sources)

    def invalidate(self) -> None:
        for source in self.sources:
            if callable(getattr(source, 'invalidate', None)):
                source.invalidate()

    def key_for(self, config: SecOcConfig) -> Tuple[Optional[SecOcKey], str]:
        reasons = []
        for source in self.sources:
            key, reason = source.key_for(config)
            if key is not None:
                return key, ''
            reasons.append(reason)
        return None, reasons[-1] if reasons else f'no key for {config.pdu}'


class StaticSecOcKeys:
    """Keys given directly (scripts, tests): {PDU name or Data ID or '*': (key, algorithm)}."""

    def __init__(self, keys: Dict[object, Tuple[bytes, str]]):
        self._keys = keys

    def key_for(self, config: SecOcConfig) -> Tuple[Optional[SecOcKey], str]:
        for selector in (config.pdu, config.data_id, '*'):
            if selector in self._keys:
                key, algorithm = self._keys[selector]
                return SecOcKey(bytes(key), algorithm, str(selector)), ''
        return None, f'no key for {config.pdu}'


# --- Freshness ---


class FreshnessManager(Protocol):
    def received(self, config: SecOcConfig, truncated: int) -> Tuple[int, bool]:
        """The full freshness value to verify a received frame with, and whether it is synchronised (known)."""
        ...

    def accepted(self, config: SecOcConfig, value: int) -> Optional[int]:
        """A frame with this full freshness value was verified. Returns the previous verified value."""
        ...

    def next_value(self, config: SecOcConfig) -> int:
        """The freshness value to send the next frame of this PDU with."""
        ...

    def reset(self) -> None: ...


def _freshness_key(config: SecOcConfig) -> Hashable:
    return config.data_id if config.data_id is not None else config.pdu


class CounterFreshness:
    """Freshness values as monotonic counters per Data ID, reconstructed from their transmitted low bits."""

    def __init__(self) -> None:
        self._latest: Dict[Hashable, int] = {}
        self._sent: Dict[Hashable, int] = {}

    def reset(self) -> None:
        self._latest.clear()
        self._sent.clear()

    def received(self, config: SecOcConfig, truncated: int) -> Tuple[int, bool]:
        full_bits, tx_bits = config.freshness_bits or 0, config.freshness_tx_bits or 0
        if tx_bits >= full_bits:
            return truncated, True  # The whole value is in the frame
        latest = self._latest.get(_freshness_key(config))
        if latest is None:
            return truncated, False  # High bits unknown until a frame verifies
        mask = (1 << tx_bits) - 1
        high = latest >> tx_bits
        if truncated <= (latest & mask):
            high += 1  # The low bits wrapped (or the frame repeats an old value, which then fails)
        return ((high << tx_bits) | truncated) & ((1 << full_bits) - 1), True

    def accepted(self, config: SecOcConfig, value: int) -> Optional[int]:
        key = _freshness_key(config)
        previous = self._latest.get(key)
        self._latest[key] = value if previous is None else max(previous, value)
        return previous

    def next_value(self, config: SecOcConfig) -> int:
        key = _freshness_key(config)
        value = max(self._sent.get(key, 0), self._latest.get(key, 0)) + 1
        self._sent[key] = value & ((1 << (config.freshness_bits or 64)) - 1)
        return self._sent[key]


class ScriptFreshness:
    """A manufacturer freshness scheme from a Python file (see `load_freshness_script`)."""

    def __init__(self, module, path: Path):
        self.path = path
        self._received: Callable = module.freshness
        self._accepted: Optional[Callable] = getattr(module, 'accepted', None)
        self._next: Optional[Callable] = getattr(module, 'next_value', None)
        self._reset: Optional[Callable] = getattr(module, 'reset', None)
        self._counter = CounterFreshness()  # Keeps the previous value for the replay check

    def reset(self) -> None:
        self._counter.reset()
        if self._reset is not None:
            self._reset()

    def received(self, config: SecOcConfig, truncated: int) -> Tuple[int, bool]:
        value = self._received(
            config.pdu, config.data_id, truncated, config.freshness_tx_bits or 0, config.freshness_bits or 0
        )
        return int(value), True

    def accepted(self, config: SecOcConfig, value: int) -> Optional[int]:
        if self._accepted is not None:
            self._accepted(config.pdu, config.data_id, value)
        return self._counter.accepted(config, value)

    def next_value(self, config: SecOcConfig) -> int:
        if self._next is None:
            raise SecOcError(f'{self.path.name} does not define next_value(pdu, data_id, full_bits) for sending')
        return int(self._next(config.pdu, config.data_id, config.freshness_bits or 0))


def load_freshness_script(path: str | Path) -> ScriptFreshness:
    """Load a freshness value manager from a Python file. It defines

        freshness(pdu: str, data_id: int, truncated: int, tx_bits: int, full_bits: int) -> int

    returning the full freshness value of a received frame, and optionally `accepted(pdu, data_id, value)` (a frame
    with that value verified), `next_value(pdu, data_id, full_bits) -> int` (to send) and `reset()`. The file is
    executed: only load files you trust.
    """
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'can_explorer_freshness_{path.stem}', path)
    if spec is None or spec.loader is None:
        raise SecOcError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise SecOcError(f'Error in freshness file {path.name}: {e}') from e
    if not callable(getattr(module, 'freshness', None)):
        raise SecOcError(f'{path.name} does not define freshness(pdu, data_id, truncated, tx_bits, full_bits)')
    return ScriptFreshness(module, path)


@dataclass(frozen=True)
class TripResetConfig:
    """The counters of the AUTOSAR trip/reset freshness value (bit lengths), and the synchronisation message."""

    trip_bits: int = 16
    reset_bits: int = 20
    message_bits: int = 24
    flag_bits: int = 2
    sync_pdu: str = ''  # The secured PDU carrying TripResetSyncMsg (trip counter | reset counter | authenticator)
    clear_acceptance_window: int = 0  # Trip counter values accepted around its wrap (ClearAcceptanceWindow)
    trip_initial: int = 1  # The trip counter's initial value at the FV management master

    def __post_init__(self):
        if not 0 < self.trip_bits <= 24 or not 0 < self.reset_bits <= 24:
            raise SecOcError('The trip and reset counters have 1 to 24 bits')
        if not 0 < self.message_bits <= 48 or not 0 <= self.flag_bits <= 2:
            raise SecOcError('The message counter has 1 to 48 bits and the reset flag 0 to 2')
        if self.full_bits > 64:
            raise SecOcError('The freshness value (trip + reset + message counter + reset flag) has at most 64 bits')

    @property
    def full_bits(self) -> int:
        return self.trip_bits + self.reset_bits + self.message_bits + self.flag_bits


class TripResetFreshness:
    """Freshness values built from a trip counter, a reset counter and a message counter, synchronised by a
    TripResetSyncMsg from the FV management master — the multiple freshness counter scheme with a truncated
    freshness value of the AUTOSAR SecOC specification (SWS SecureOnboardCommunication R23-11, Annex A, §11.4,
    UC_SecOC_00202; an application hint, not a normative part).

    Freshness value (most significant bit first): trip counter | reset counter | message counter | reset flag (the
    low bits of the reset counter). A secured PDU transmits the low bits of the message counter and the reset flag.
    Received values are reconstructed as in its table 9 and figures 27-29 (the reset flag is matched against the
    latest reset counter + X, X = 0, -1, +1, -2, +2; each match is a candidate, tried in turn), values to send as in
    its table 8. The synchronisation message itself carries the trip and reset counters in full and is verified
    like any secured PDU (its authentication data is Message ID | trip counter | reset counter); a verified one
    becomes the latest trip and reset counter if it is newer (or within the clear acceptance window after the trip
    counter wrapped). The single-master method is supported: one synchronisation message for all secured PDUs.
    """

    def __init__(self, config: TripResetConfig):
        self.config = config
        self.reset()

    def reset(self) -> None:
        self.latest: Tuple[int, int] = (0, 0)  # Trip, reset counter of the last verified sync message (slave: 0)
        self.synchronised = False
        self._received: Dict[Hashable, Tuple[int, int, int]] = {}  # Previously received trip, reset, message counter
        self._sent: Dict[Hashable, Tuple[int, int, int]] = {}

    # Layout

    def compose(self, trip: int, reset: int, message: int, flag: Optional[int] = None) -> int:
        c = self.config
        if flag is None:
            flag = reset & ((1 << c.flag_bits) - 1)
        value = trip & ((1 << c.trip_bits) - 1)
        value = (value << c.reset_bits) | (reset & ((1 << c.reset_bits) - 1))
        value = (value << c.message_bits) | min(message, (1 << c.message_bits) - 1)
        return (value << c.flag_bits) | (flag & ((1 << c.flag_bits) - 1))

    def decompose(self, value: int) -> Tuple[int, int, int, int]:
        """(trip, reset, message counter, reset flag) of a freshness value."""
        c = self.config
        flag = value & ((1 << c.flag_bits) - 1)
        value >>= c.flag_bits
        message = value & ((1 << c.message_bits) - 1)
        value >>= c.message_bits
        return value >> c.reset_bits, value & ((1 << c.reset_bits) - 1), message, flag

    def _lower_bits(self, config: SecOcConfig) -> int:
        """Bits of the message counter in the transmitted freshness value."""
        c = self.config
        if (config.freshness_bits or 0) != c.full_bits:
            raise SecOcError(
                f'{config.pdu} has a {config.freshness_bits}-bit freshness value; the trip/reset counters make '
                f'{c.full_bits} bits'
            )
        lower = (config.freshness_tx_bits or 0) - c.flag_bits
        if not 0 <= lower <= c.message_bits:
            raise SecOcError(
                f'{config.pdu} transmits {config.freshness_tx_bits} freshness bits, fewer than the reset flag'
            )
        return lower

    def _is_sync(self, config: SecOcConfig) -> bool:
        return bool(self.config.sync_pdu) and config.pdu == self.config.sync_pdu

    # FreshnessManager

    def candidates(self, config: SecOcConfig, truncated: int) -> List[Tuple[int, bool]]:
        """The freshness values to verify a received frame with, in the order of the AUTOSAR flow."""
        if self._is_sync(config):
            return [(truncated, True)]  # Trip and reset counter are sent in full
        c, lower_bits = self.config, self._lower_bits(config)
        flag_mask = (1 << c.flag_bits) - 1
        received_flag, received_lower = truncated & flag_mask, truncated >> c.flag_bits
        previous = self._received.get(_freshness_key(config), (0, 0, 0))
        previous_counters = (previous[0] << c.reset_bits) | previous[1]
        values = []
        for offset in (0, -1, 1, -2, 2):
            reset = self.latest[1] + offset
            if reset < 0 or reset >= 1 << c.reset_bits or (reset & flag_mask) != received_flag:
                continue
            counters = (self.latest[0] << c.reset_bits) | reset
            if counters == previous_counters:
                upper, previous_lower = previous[2] >> lower_bits, previous[2] & ((1 << lower_bits) - 1)
                if previous_lower >= received_lower:
                    upper += 1  # Format 2: the low bits wrapped
                message = (upper << lower_bits) | received_lower
                values.append(self.compose(previous[0], previous[1], message, received_flag))
            elif counters > previous_counters:  # Format 3: new trip/reset counters, message counter from 0
                values.append(self.compose(self.latest[0], reset, received_lower, received_flag))
        return [(value, self.synchronised) for value in dict.fromkeys(values)]

    def received(self, config: SecOcConfig, truncated: int) -> Tuple[int, bool]:
        values = self.candidates(config, truncated)
        if not values:
            raise SecOcError('no freshness value fits the received reset flag')
        return values[0]

    def accepted(self, config: SecOcConfig, value: int) -> Optional[int]:
        c = self.config
        if self._is_sync(config):
            trip, reset = value >> c.reset_bits, value & ((1 << c.reset_bits) - 1)
            latest_trip = self.latest[0]
            wrapped = (
                trip < latest_trip
                and latest_trip >= (1 << c.trip_bits) - 1 - c.clear_acceptance_window
                and trip <= c.trip_initial + c.clear_acceptance_window
            )
            if (trip, reset) <= self.latest and self.synchronised and not wrapped:
                return (latest_trip << c.reset_bits) | self.latest[1]  # Not newer: the verifier reports a replay
            if wrapped:
                self._received.clear()
                self._sent.clear()
            self.latest, self.synchronised = (trip, reset), True
            return None
        trip, reset, message, _flag = self.decompose(value)
        self._received[_freshness_key(config)] = (trip, reset, message)
        return None

    def next_value(self, config: SecOcConfig) -> int:
        """The freshness value to send (its table 8): the message counter continues while the trip and reset
        counters are unchanged, and starts at 1 when they changed."""
        c = self.config
        if self._is_sync(config):
            return (self.latest[0] << c.reset_bits) | self.latest[1]
        self._lower_bits(config)
        key = _freshness_key(config)
        previous = self._sent.get(key)
        if previous is not None and previous[:2] == self.latest:
            trip, reset, message = previous[0], previous[1], previous[2] + 1
        else:
            trip, reset, message = self.latest[0], self.latest[1], 1
        if reset == (1 << c.reset_bits) - 1:
            message = (1 << c.message_bits) - 1  # At the maximum reset counter the message counter is fixed
        message = min(message, (1 << c.message_bits) - 1)
        self._sent[key] = (trip, reset, message)
        return self.compose(trip, reset, message)


# --- Checking and securing ---


@dataclass(frozen=True)
class SecOcCheck:
    checked: bool
    ok: bool = False
    freshness: int = 0
    synchronised: bool = True
    previous: Optional[int] = None  # The last verified freshness value, when the frame is a replay
    reason: str = ''  # Why the frame was not checked

    def describe(self) -> str:
        if not self.checked:
            return f'SecOC: not checked ({self.reason})'
        if self.reason:
            return f'SecOC FRESHNESS ERROR: {self.reason}'
        if self.ok:
            return f'SecOC ok, FV {self.freshness}'
        if self.previous is not None:
            return f'SecOC FRESHNESS ERROR, FV {self.freshness} not newer than {self.previous}'
        if not self.synchronised:
            return f'SecOC MAC ERROR, FV {self.freshness} (freshness not synchronised yet)'
        return f'SecOC MAC ERROR, FV {self.freshness}'


@dataclass
class SecOcStatistics:
    """The outcome of the checks of one secured PDU, and the frames of it that were secured to send."""

    ok: int = 0
    mac_errors: int = 0
    freshness_errors: int = 0
    not_checked: int = 0
    sent: int = 0
    last: Optional[SecOcCheck] = None

    def record(self, check: SecOcCheck) -> None:
        self.last = check
        if not check.checked:
            self.not_checked += 1
        elif check.ok:
            self.ok += 1
        elif check.previous is not None:
            self.freshness_errors += 1
        else:
            self.mac_errors += 1

    def as_dict(self) -> Dict[str, object]:
        return {
            'ok': self.ok,
            'mac_errors': self.mac_errors,
            'freshness_errors': self.freshness_errors,
            'not_checked': self.not_checked,
            'sent': self.sent,
            'last': self.last.describe() if self.last is not None else '',
        }


#: Manipulated secured frames for testing that an ECU rejects them (`SecOcVerifier.manipulate`)
ATTACKS = {
    'replay': 'a secured frame sent before, sent again unchanged',
    'stale_freshness': 'a correct MAC over a freshness value that was used already',
    'wrong_mac': 'a fresh freshness value with the last MAC bit flipped',
    'payload_bitflip': 'a correctly secured frame with one payload bit flipped afterwards',
}


def _flip_bit(data: bytes, bit: int) -> bytes:
    """Flip bit `bit` of `data`, counted from the most significant bit of the first byte."""
    flipped = bytearray(data)
    flipped[bit // 8] ^= 0x80 >> (bit % 8)
    return bytes(flipped)


class SecOcVerifier:
    """Checks received secured PDUs (in arrival order: the freshness state follows the frames) and secures
    PDUs to send. `statistics` counts the outcomes per PDU name."""

    def __init__(self, keys: SecOcKeys, freshness: Optional[FreshnessManager] = None):
        self.keys = keys
        self.freshness: FreshnessManager = freshness if freshness is not None else CounterFreshness()
        self.statistics: Dict[str, SecOcStatistics] = {}
        self._last_sent: Dict[str, Tuple[int, bytes]] = {}  # PDU -> (freshness value, secured frame)

    def reset(self) -> None:
        self.freshness.reset()
        self.statistics.clear()
        self._last_sent.clear()

    def _statistics(self, pdu: str) -> SecOcStatistics:
        statistics = self.statistics.get(pdu)
        if statistics is None:
            statistics = self.statistics[pdu] = SecOcStatistics()
        return statistics

    def _algorithm(self, config: SecOcConfig, key: SecOcKey) -> Tuple[Optional[str], str]:
        if key.authenticator is not None:
            return 'profile', ''  # The profile computes the MAC with its own algorithm
        algorithm = key.algorithm or algorithm_of(config.auth_algorithm)
        if algorithm is None:
            name = config.auth_algorithm or '(none)'
            return None, f'algorithm {name!r} not recognised; set it on the key {key.entry!r}'
        return algorithm, ''

    def check(self, config: SecOcConfig, data: bytes) -> SecOcCheck:
        check = self._check(config, data)
        self._statistics(config.pdu).record(check)
        return check

    def _check(self, config: SecOcConfig, data: bytes) -> SecOcCheck:
        problem = config.problem
        if problem:
            return SecOcCheck(False, reason=problem)
        parts = config.split(bytes(data))
        if parts is None:
            return SecOcCheck(False, reason=f'frame shorter than the {config.length} bytes of the secured PDU')
        payload, truncated_freshness, received_mac = parts
        key, reason = self.keys.key_for(config)
        if key is None:
            return SecOcCheck(False, reason=reason)
        algorithm, reason = self._algorithm(config, key)
        if algorithm is None:
            return SecOcCheck(False, reason=reason)
        try:
            if hasattr(self.freshness, 'candidates'):  # Several reconstructions, re-verified in turn
                candidates = self.freshness.candidates(config, truncated_freshness)
            else:
                candidates = [self.freshness.received(config, truncated_freshness)]
            if not candidates:
                return SecOcCheck(True, False, synchronised=False, reason='no freshness value fits the frame')
            verified = None
            for freshness, synchronised in candidates:
                expected = config.truncated_mac(algorithm, key.key, payload, freshness, key.authenticator)
                if hmac.compare_digest(expected.to_bytes(64, 'big'), received_mac.to_bytes(64, 'big')):
                    verified = (freshness, synchronised)
                    break
        except Exception as e:  # A bad key length, or an error in a freshness script
            return SecOcCheck(False, reason=str(e))
        if verified is None:
            return SecOcCheck(True, False, candidates[0][0], candidates[0][1])
        freshness = verified[0]
        previous = self.freshness.accepted(config, freshness)
        if previous is not None and freshness <= previous:
            return SecOcCheck(True, False, freshness, True, previous)
        return SecOcCheck(True, True, freshness)

    def secure(self, config: SecOcConfig, payload: bytes, freshness: Optional[int] = None) -> bytes:
        """The secured PDU for an authentic payload: payload | truncated freshness | truncated MAC. Without
        `freshness`, the freshness manager's next value is used."""
        problem = config.problem
        if problem:
            raise SecOcError(f'Cannot secure {config.pdu}: {problem}')
        key, reason = self.keys.key_for(config)
        if key is None:
            raise SecOcError(f'Cannot secure {config.pdu}: {reason}')
        algorithm, reason = self._algorithm(config, key)
        if algorithm is None:
            raise SecOcError(f'Cannot secure {config.pdu}: {reason}')
        if freshness is None:
            freshness = self.freshness.next_value(config)
        payload = bytes(payload)[: config.payload_length].ljust(config.payload_length or 0, b'\0')
        mac = config.truncated_mac(algorithm, key.key, payload, freshness, key.authenticator)
        tx_bits, auth_bits = config.freshness_tx_bits or 0, config.auth_tx_bits or 0
        trailer_bytes = (tx_bits + auth_bits + 7) // 8
        trailer = (freshness & ((1 << tx_bits) - 1)) << auth_bits | mac
        trailer <<= trailer_bytes * 8 - tx_bits - auth_bits
        frame = payload + trailer.to_bytes(trailer_bytes, 'big')
        self._last_sent[config.pdu] = (freshness, frame)
        self._statistics(config.pdu).sent += 1
        return frame

    def manipulate(self, config: SecOcConfig, payload: bytes, attack: str) -> bytes:
        """A secured frame an ECU must reject, for security tests (see ATTACKS). `replay` and `stale_freshness`
        need a frame of the PDU secured before (`secure`)."""
        if attack not in ATTACKS:
            raise SecOcError(f'Unknown SecOC attack {attack!r}; one of {", ".join(ATTACKS)}')
        audit.record('secoc attack frame', pdu=config.pdu, attack=attack)
        previous = self._last_sent.get(config.pdu)
        if attack in ('replay', 'stale_freshness') and previous is None:
            raise SecOcError(f'{attack} needs a frame of {config.pdu} sent secured before')
        if attack == 'replay':
            return previous[1]
        if attack == 'stale_freshness':
            last = self._last_sent[config.pdu]
            frame = self.secure(config, payload, previous[0])  # Valid MAC, freshness value not newer
            self._last_sent[config.pdu] = last
            return frame
        frame = self.secure(config, payload)
        if attack == 'wrong_mac':
            last_mac_bit = (config.payload_length or 0) * 8 + config.trailer_bits - 1
            return _flip_bit(frame, last_mac_bit)
        return _flip_bit(frame, 7)  # payload_bitflip: the lowest bit of the first payload byte


def secured_pdus(database) -> List[SecOcConfig]:
    """The secured PDUs of a cantools database."""
    return [config for message in database.messages if (config := secoc_config_of(message)) is not None]
