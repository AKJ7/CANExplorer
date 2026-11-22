"""Synthetic traffic from a signal database, with known signals, for testing the reverse engineering tools.

Every signal of the database gets a behaviour (smooth like a sine, ramp or random walk; constant; a counter;
toggling between a few values; noise; a step at a given time; or a CRC-8 of the other bytes), messages are sent at
their cycle time, and the true fields and raw values are kept for comparing what the tools found with what was
sent. Smooth signals move within part of their range over 10 to 60 seconds, with some noise like real
measurements.
"""

import hashlib
import hmac
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import can

from can_explorer.core.reverse.checksums import sae_j1850
from can_explorer.core.reverse.fields import BitField


def _crc16_ccitt(data: bytes, start: int = 0xFFFF) -> int:
    """CRC-16/CCITT (polynomial 0x1021) of data."""
    crc = start
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _cmac_truncated(key: bytes, data: bytes, mac_bytes: int) -> bytes:
    """A truncated HMAC-SHA256 standing in for CMAC-AES in the synthetic traffic (AES is not needed for testing the
    analysis tools; the statistical properties are the same)."""
    return hmac.new(key, data, hashlib.sha256).digest()[:mac_bytes]


KINDS = (
    'sine',
    'ramp',
    'random_walk',
    'constant',
    'counter',
    'toggle',
    'noise',
    'step',
    'checksum',
    'cmac',
    'cmac_no_freshness',
    'xor_obfuscated',
    'crc16',
)
SMOOTH_KINDS = ('sine', 'ramp', 'random_walk')
DEFAULT_CYCLES = (0.01, 0.02, 0.05, 0.1)  # Seconds, for messages without a cycle time


DEFAULT_KEYSTREAM = b'\x5a\xa5\x3c\xc3\x96\x69\xf0\x0f'


@dataclass(frozen=True)
class Behaviour:
    """How a signal's raw value develops; `period` in seconds, `at` the time of a step, `amplitude` the share of
    the range a smooth signal sweeps (default: random, 20 to 80 %). A checksum is the CRC-8 SAE J1850 (AUTOSAR) of
    the other bytes and must be a byte of its own.

    For ``xor_obfuscated``, *keystream* overrides the default XOR keystream (repeats cyclically).
    """

    kind: str
    period: float = 20.0
    at: float = 0.0
    amplitude: Optional[float] = None
    keystream: Optional[bytes] = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f'Unknown behaviour {self.kind!r}; use one of {", ".join(KINDS)}')


@dataclass
class SyntheticTraffic:
    messages: List[can.Message] = field(default_factory=list)
    fields: Dict[str, BitField] = field(default_factory=dict)  # 'Message.Signal': the true field
    behaviours: Dict[str, Behaviour] = field(default_factory=dict)
    raw: Dict[str, List[Tuple[float, int]]] = field(default_factory=dict)  # Raw values sent, per signal

    def write(self, path: str | Path) -> None:
        """Write the frames to a log file (any format python-can writes)."""
        with can.Logger(str(path)) as logger:
            for msg in self.messages:
                logger.on_message_received(msg)


def _raw_range(signal) -> Tuple[int, int]:
    """The raw values a signal can take: its minimum and maximum when given, otherwise all its bits allow."""
    if signal.is_signed:
        low, high = -(1 << (signal.length - 1)), (1 << (signal.length - 1)) - 1
    else:
        low, high = 0, (1 << signal.length) - 1
    raw = []
    for physical in (signal.minimum, signal.maximum):
        raw.append(None if physical is None else round((physical - signal.offset) / signal.scale))
    if raw[0] is not None and raw[1] is not None:
        first, second = sorted(raw)
        if low <= first < second <= high:
            return first, second
    return low, high


class _Signal:
    def __init__(self, signal, behaviour: Behaviour, rng: random.Random):
        self.signal = signal
        self.behaviour = behaviour
        self.low, self.high = _raw_range(signal)
        self.rng = rng
        self.phase = rng.random()
        # Smooth signals stay around a centre, within part of the range
        if behaviour.amplitude is None:
            self.center = rng.uniform(0.3, 0.7)
            self.amplitude = rng.uniform(0.1, min(self.center, 1 - self.center) - 0.05)
        else:
            self.center, self.amplitude = 0.5, min(0.5, behaviour.amplitude / 2)
        self.walk = self.center
        self.position = rng.random()
        self.last_time = 0.0
        self.toggle_values = sorted(rng.sample(range(self.low, self.high + 1), min(3, self.high - self.low + 1)))
        self.toggle_value = self.toggle_values[0]
        self.constant = rng.randint(self.low, self.high)
        self.count = 0

    def raw(self, time: float) -> int:
        behaviour, low, high = self.behaviour, self.low, self.high
        span = high - low
        if behaviour.kind == 'sine':
            share = self.center + self.amplitude * math.sin(2 * math.pi * (time / behaviour.period + self.phase))
        elif behaviour.kind == 'ramp':
            # Rising with a slowly varying slope (an exactly constant step would look like a counter)
            speed = 1 + 0.5 * math.sin(2 * math.pi * (time / (3.7 * behaviour.period) + self.phase))
            self.position = (self.position + (time - self.last_time) / behaviour.period * speed) % 1.0
            self.last_time = time
            share = self.center - self.amplitude + 2 * self.amplitude * self.position
        elif behaviour.kind == 'random_walk':
            self.walk += self.rng.gauss(0, 0.002)
            lowest, highest = self.center - self.amplitude, self.center + self.amplitude
            if not lowest <= self.walk <= highest:  # Reflect at the edges
                self.walk = 2 * (lowest if self.walk < lowest else highest) - self.walk
            share = self.walk
        elif behaviour.kind in ('constant', 'checksum', 'cmac', 'cmac_no_freshness', 'xor_obfuscated', 'crc16'):
            return self.constant
        elif behaviour.kind == 'counter':
            value = self.count % (1 << self.signal.length)
            self.count += 1
            if self.signal.is_signed and value >> (self.signal.length - 1):
                value -= 1 << self.signal.length  # Two's complement
            return value
        elif behaviour.kind == 'toggle':
            if self.rng.random() < 0.01:
                self.toggle_value = self.rng.choice(self.toggle_values)
            return self.toggle_value
        elif behaviour.kind == 'noise':
            return self.rng.randint(low, high)
        else:  # step
            return high if time >= behaviour.at else low
        value = low + round(share * span)
        if span >= 256:  # Measurement noise of a few steps
            value += round(self.rng.gauss(0, max(0.7, span / 20000)))
        return min(high, max(low, value))


def _default_behaviour(signal, rng: random.Random) -> Behaviour:
    if signal.length <= 4:
        return Behaviour('toggle')
    return Behaviour(rng.choice(SMOOTH_KINDS), period=rng.uniform(10.0, 60.0))


def generate(
    database,
    duration: float = 10.0,
    behaviours: Optional[Dict[str, Behaviour]] = None,
    seed: int = 0,
    start: float = 0.0,
    messages: Optional[Iterable[str]] = None,
) -> SyntheticTraffic:
    """Frames of the messages of a cantools database (or a DbcDecoder) over `duration` seconds.

    behaviours: per 'Message.Signal', otherwise smooth signals, and toggling for signals of up to 4 bits.
    Multiplexers cycle through their multiplexed values. Floating point signals are sent but have no BitField.
    """
    database = getattr(database, 'database', database)
    behaviours = behaviours or {}
    rng = random.Random(seed)
    traffic = SyntheticTraffic()
    events: List[Tuple[float, int, can.Message]] = []
    wanted = set(messages) if messages is not None else None
    for index, message in enumerate(database.messages):
        if wanted is not None and message.name not in wanted:
            continue
        cycle = message.cycle_time / 1000 if message.cycle_time else rng.choice(DEFAULT_CYCLES)
        multiplexer = next((signal for signal in message.signals if signal.is_multiplexer), None)
        mux_values = sorted({value for signal in message.signals for value in signal.multiplexer_ids or ()})
        generators = {}
        for signal in message.signals:
            if signal is multiplexer:
                continue
            name = f'{message.name}.{signal.name}'
            behaviour = behaviours.get(name) or _default_behaviour(signal, rng)
            if behaviour.kind in ('checksum', 'crc16') and (signal.length % 8 or signal.start % 8 or signal.is_float):
                raise ValueError(f'{name}: a {behaviour.kind} must be byte-aligned')
            if behaviour.kind in ('cmac', 'cmac_no_freshness', 'xor_obfuscated') and (
                signal.start % 8 or signal.is_float
            ):
                raise ValueError(f'{name}: a {behaviour.kind} must be byte-aligned')
            generators[signal.name] = _Signal(signal, behaviour, rng)
            traffic.behaviours[name] = behaviour
            traffic.raw[name] = []
            if not signal.is_float:
                traffic.fields[name] = BitField.from_signal(message.frame_id, message.is_extended_frame, signal)
        if multiplexer is not None:
            name = f'{message.name}.{multiplexer.name}'
            traffic.raw[name] = []
            traffic.fields[name] = BitField.from_signal(message.frame_id, message.is_extended_frame, multiplexer)
        offset = rng.uniform(0, cycle)
        for step in range(int((duration - offset) / cycle) + 1):
            time = start + offset + step * cycle
            values = {}
            if multiplexer is not None:
                mux = mux_values[step % len(mux_values)] if mux_values else 0
                values[multiplexer.name] = mux
                traffic.raw[f'{message.name}.{multiplexer.name}'].append((time, mux))
            for signal in message.signals:
                if signal is multiplexer:
                    continue
                if signal.multiplexer_ids and values.get(multiplexer.name) not in signal.multiplexer_ids:
                    continue  # Not sent with this multiplexer value
                generator = generators[signal.name]
                raw = generator.raw(time - start)
                values[signal.name] = float(raw) if signal.is_float else raw
                traffic.raw[f'{message.name}.{signal.name}'].append((time, raw))
            data = bytearray(message.encode(values, scaling=False, padding=False, strict=False))
            for signal in message.signals:
                name = f'{message.name}.{signal.name}'
                kind = traffic.behaviours.get(name, Behaviour('constant')).kind
                if signal.name not in values:
                    continue
                byte = signal.start // 8
                sig_bytes = signal.length // 8 if signal.length >= 8 else 1
                if kind == 'checksum':
                    data[byte] = sae_j1850(data[:byte] + data[byte + 1 :])
                    traffic.raw[name][-1] = (time, data[byte])
                elif kind == 'crc16':
                    other = data[:byte] + data[byte + sig_bytes :]
                    crc = _crc16_ccitt(bytes(other))
                    data[byte] = crc & 0xFF
                    if sig_bytes > 1:
                        data[byte + 1] = (crc >> 8) & 0xFF
                    traffic.raw[name][-1] = (time, crc & ((1 << signal.length) - 1))
                elif kind == 'cmac':
                    # Bytes before the MAC are the authentic payload (including the counter)
                    mac_key = b'\xab' * 16
                    authentic = bytes(data[:byte])
                    mac_val = _cmac_truncated(mac_key, authentic, sig_bytes)
                    for k in range(sig_bytes):
                        data[byte + k] = mac_val[k]
                    raw_val = int.from_bytes(mac_val, 'big')
                    traffic.raw[name][-1] = (time, raw_val)
                elif kind == 'cmac_no_freshness':
                    # Like cmac but no counter in the authentic data — just the payload bytes before the MAC
                    mac_key = b'\xcd' * 16
                    authentic = bytes(data[:byte])
                    mac_val = _cmac_truncated(mac_key, authentic, sig_bytes)
                    for k in range(sig_bytes):
                        data[byte + k] = mac_val[k]
                    raw_val = int.from_bytes(mac_val, 'big')
                    traffic.raw[name][-1] = (time, raw_val)
                elif kind == 'xor_obfuscated':
                    # XOR with a fixed keystream (configurable via Behaviour.keystream)
                    ks = traffic.behaviours.get(name, Behaviour('constant')).keystream or DEFAULT_KEYSTREAM
                    for k in range(sig_bytes):
                        data[byte + k] = data[byte + k] ^ ks[k % len(ks)]
                    raw_val = int.from_bytes(data[byte : byte + sig_bytes], 'big')
                    traffic.raw[name][-1] = (time, raw_val)
            msg = can.Message(
                timestamp=time,
                arbitration_id=message.frame_id,
                is_extended_id=message.is_extended_frame,
                is_fd=message.is_fd or len(data) > 8,
                bitrate_switch=message.is_fd,
                data=data,
            )
            events.append((time, index, msg))
    events.sort(key=lambda event: (event[0], event[1]))
    traffic.messages = [msg for _, _, msg in events]
    return traffic


def discoverable(fields: Iterable[BitField], messages: Iterable[can.Message]) -> List[BitField]:
    """The fields as far as they can be found in the frames: without the bits at either end that never changed
    (e.g. a signal not using its whole range), and without fields that never changed at all."""
    changed: Dict[Tuple[int, bool], int] = {}
    previous: Dict[Tuple[int, bool], int] = {}
    for msg in messages:
        key = (msg.arbitration_id, msg.is_extended_id)
        value = int.from_bytes(msg.data, 'little')
        if key in previous:
            changed[key] = changed.get(key, 0) | (value ^ previous[key])
        previous[key] = value
    result = []
    for item in fields:
        mask = changed.get((item.frame_id, item.extended), 0)
        bits = item.bits
        while bits and not mask >> bits[0] & 1:
            bits = bits[1:]
        while bits and not mask >> bits[-1] & 1:
            bits = bits[:-1]
        if not bits:
            continue
        if item.byte_order == 'little_endian' or len({bit // 8 for bit in bits}) == 1:
            start = min(bits)
            order = 'little_endian' if len({bit // 8 for bit in bits}) == 1 else item.byte_order
        else:
            start, order = bits[0], item.byte_order
        result.append(
            BitField(item.frame_id, item.extended, start, len(bits), order, item.signed, item.scale, item.offset)
        )
    return result


@dataclass
class FieldScore:
    """Fields found compared with the true fields: the same identifier and bits in the same order."""

    matched: List[BitField]
    missed: List[BitField]
    spurious: List[BitField]
    wrong_sign: List[BitField]  # Matched, but with the other signedness

    @property
    def precision(self) -> float:
        found = len(self.matched) + len(self.spurious)
        return len(self.matched) / found if found else 1.0

    @property
    def recall(self) -> float:
        total = len(self.matched) + len(self.missed)
        return len(self.matched) / total if total else 1.0


def score_fields(truth: Iterable[BitField], found: Iterable[BitField]) -> FieldScore:
    def location(item: BitField):
        # The bits from the most to the least significant one: a field within a byte reads the same in both orders
        return item.frame_id, item.extended, tuple(item.bits)

    expected = {location(item): item for item in truth}
    matched, spurious, wrong_sign = [], [], []
    seen = set()
    for item in found:
        key = location(item)
        if key in expected and key not in seen:
            seen.add(key)
            matched.append(item)
            if item.signed != expected[key].signed:
                wrong_sign.append(item)
        else:
            spurious.append(item)
    missed = [item for key, item in expected.items() if key not in seen]
    return FieldScore(matched, missed, spurious, wrong_sign)


# ---------------------------------------------------------------------------
# SecOC-aware synthetic traffic (roadmap 2.8, 5.3)
# ---------------------------------------------------------------------------


@dataclass
class SecOcProfile:
    """Configuration for synthetic SecOC-protected traffic."""

    key: bytes = field(default_factory=lambda: b'\xab' * 16)
    mac_bytes: int = 3
    freshness_bytes: int = 1
    freshness_width_bits: int = 8
    wrap_at: Optional[int] = None
    weak_key: bool = False
    skip_mac_start: int = 0
    static_mac: bool = False
    counter_rate: float = 1.0


def generate_secoc_traffic(
    identifier: int = 0x200,
    payload_bytes: int = 4,
    frame_count: int = 500,
    cycle_time: float = 0.01,
    profile: Optional[SecOcProfile] = None,
    seed: int = 42,
    start: float = 0.0,
    fd: bool = False,
) -> SyntheticTraffic:
    """Generate SecOC-protected CAN frames with configurable weakness levels.

    Layout: [payload_bytes of signal data] [freshness_bytes] [mac_bytes].
    """
    prof = profile or SecOcProfile()
    rng = random.Random(seed)
    traffic = SyntheticTraffic()
    effective_key = b'\x00' * 16 if prof.weak_key else prof.key
    counter_max = 1 << prof.freshness_width_bits
    wrap_point = prof.wrap_at if prof.wrap_at is not None else counter_max
    total_len = payload_bytes + prof.freshness_bytes + prof.mac_bytes

    static_mac_value: Optional[bytes] = None
    phase = rng.random()
    accumulator = 0.0

    for i in range(frame_count):
        time = start + i * cycle_time

        # Signal data: sine wave with noise
        signal_data = bytearray(payload_bytes)
        for b in range(payload_bytes):
            period = 10.0 + b * 3.7
            value = 128 + int(100 * math.sin(2 * math.pi * (time / period + phase + b * 0.3)))
            value += round(rng.gauss(0, 2))
            signal_data[b] = max(0, min(255, value))

        # Freshness counter
        accumulator += prof.counter_rate
        counter_value = int(accumulator) % wrap_point
        freshness = counter_value.to_bytes(prof.freshness_bytes, 'big')

        # MAC computation
        authentic = bytes(signal_data) + freshness
        if i < prof.skip_mac_start:
            mac_val = b'\x00' * prof.mac_bytes
        elif prof.static_mac:
            if static_mac_value is None:
                static_mac_value = _cmac_truncated(effective_key, authentic, prof.mac_bytes)
            mac_val = static_mac_value
        else:
            mac_val = _cmac_truncated(effective_key, authentic, prof.mac_bytes)

        data = bytearray(signal_data) + bytearray(freshness) + bytearray(mac_val)
        msg = can.Message(
            timestamp=time,
            arbitration_id=identifier,
            is_extended_id=False,
            is_fd=fd or total_len > 8,
            bitrate_switch=fd,
            data=bytes(data),
        )
        traffic.messages.append(msg)

    return traffic


def generate_gateway_traffic(
    external_ids: Optional[List[int]] = None,
    internal_ids: Optional[List[int]] = None,
    forwarded_ids: Optional[List[int]] = None,
    blocked_ids: Optional[List[int]] = None,
    rewritten_ids: Optional[Dict[int, int]] = None,
    frame_count: int = 200,
    cycle_time: float = 0.01,
    seed: int = 42,
    start: float = 0.0,
) -> Tuple[SyntheticTraffic, SyntheticTraffic]:
    """Generate paired external/internal bus traffic for testing gateway analysis.

    Returns (external_traffic, internal_traffic).
    """
    external_ids = external_ids if external_ids is not None else []
    internal_ids = internal_ids if internal_ids is not None else []
    forwarded_ids = forwarded_ids if forwarded_ids is not None else [0x100, 0x200]
    blocked_ids = blocked_ids if blocked_ids is not None else [0x300]
    rewritten_ids = rewritten_ids if rewritten_ids is not None else {0x400: 0x410}

    rng = random.Random(seed)
    external = SyntheticTraffic()
    internal = SyntheticTraffic()

    gateway_latency = 0.0005  # 0.5 ms forwarding latency

    all_ext_ids = list(external_ids) + list(forwarded_ids) + list(blocked_ids) + list(rewritten_ids.keys())
    all_int_ids = list(internal_ids) + list(forwarded_ids) + list(rewritten_ids.values())

    ext_events: List[Tuple[float, can.Message]] = []
    int_events: List[Tuple[float, can.Message]] = []

    for i in range(frame_count):
        time = start + i * cycle_time

        for eid in all_ext_ids:
            payload = bytearray(8)
            for b in range(8):
                value = int(128 + 80 * math.sin(2 * math.pi * (time / (8.0 + eid % 7) + b * 0.2)))
                payload[b] = max(0, min(255, value + round(rng.gauss(0, 1))))
            msg = can.Message(timestamp=time, arbitration_id=eid, is_extended_id=False, data=bytes(payload))
            ext_events.append((time, msg))

            if eid in forwarded_ids:
                fwd = can.Message(
                    timestamp=time + gateway_latency,
                    arbitration_id=eid,
                    is_extended_id=False,
                    data=bytes(payload),
                )
                int_events.append((time + gateway_latency, fwd))
            elif eid in rewritten_ids:
                fwd = can.Message(
                    timestamp=time + gateway_latency,
                    arbitration_id=rewritten_ids[eid],
                    is_extended_id=False,
                    data=bytes(payload),
                )
                int_events.append((time + gateway_latency, fwd))

        for iid in all_int_ids:
            if iid in forwarded_ids or iid in rewritten_ids.values():
                continue  # Already generated from forwarding
            payload = bytearray(8)
            for b in range(8):
                value = int(100 + 60 * math.sin(2 * math.pi * (time / (6.0 + iid % 5) + b * 0.15)))
                payload[b] = max(0, min(255, value + round(rng.gauss(0, 1))))
            msg = can.Message(timestamp=time, arbitration_id=iid, is_extended_id=False, data=bytes(payload))
            int_events.append((time, msg))

    ext_events.sort(key=lambda e: e[0])
    int_events.sort(key=lambda e: e[0])
    external.messages = [msg for _, msg in ext_events]
    internal.messages = [msg for _, msg in int_events]
    return external, internal


def generate_multi_ecu_secoc_traffic(
    ecu_configs: Optional[List[Dict[str, Any]]] = None,
    shared_key_groups: Optional[List[List[int]]] = None,
    frame_count: int = 300,
    cycle_time: float = 0.01,
    seed: int = 42,
    start: float = 0.0,
) -> SyntheticTraffic:
    """Generate traffic from multiple ECUs with shared and independent SecOC keys.

    ecu_configs: list of dicts with 'identifier', 'payload_bytes', optionally 'profile'.
    shared_key_groups: list of lists of ECU indices that share the same key.
    Default: 4 ECUs (0x200-0x203), two pairs sharing keys.
    """
    if ecu_configs is None:
        ecu_configs = [
            {'identifier': 0x200, 'payload_bytes': 4},
            {'identifier': 0x201, 'payload_bytes': 4},
            {'identifier': 0x202, 'payload_bytes': 5},
            {'identifier': 0x203, 'payload_bytes': 5},
        ]
    if shared_key_groups is None:
        shared_key_groups = [[0, 1], [2, 3]]

    rng = random.Random(seed)

    # Assign keys: shared groups get the same key, others get independent keys
    key_map: Dict[int, bytes] = {}
    for group in shared_key_groups:
        group_key = rng.randbytes(16)
        for idx in group:
            if idx < len(ecu_configs):
                key_map[idx] = group_key

    traffic = SyntheticTraffic()

    for idx, cfg in enumerate(ecu_configs):
        identifier = cfg['identifier']
        payload_bytes = cfg.get('payload_bytes', 4)
        prof = cfg.get('profile', SecOcProfile())

        if idx in key_map:
            prof = SecOcProfile(
                key=key_map[idx],
                mac_bytes=prof.mac_bytes,
                freshness_bytes=prof.freshness_bytes,
                freshness_width_bits=prof.freshness_width_bits,
                wrap_at=prof.wrap_at,
                weak_key=prof.weak_key,
                skip_mac_start=prof.skip_mac_start,
                static_mac=prof.static_mac,
                counter_rate=prof.counter_rate,
            )
        elif not cfg.get('profile'):
            prof = SecOcProfile(key=rng.randbytes(16))

        ecu_traffic = generate_secoc_traffic(
            identifier=identifier,
            payload_bytes=payload_bytes,
            frame_count=frame_count,
            cycle_time=cycle_time,
            profile=prof,
            seed=seed + idx,
            start=start,
        )
        traffic.messages.extend(ecu_traffic.messages)

    traffic.messages.sort(key=lambda m: m.timestamp)
    return traffic
