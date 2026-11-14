"""Security access (0x27) tests of an ECU, for benches you are authorised to test: the quality of its seeds, and how
it locks out wrong keys.

**Seed analysis** requests many seeds and looks for what makes a key guessable: repeated seeds, seed bytes that
never change, seeds that follow a constant step (a counter or a clock), short seeds and zero seeds. Many ECUs keep
the same seed until a key is sent, so `between` chooses how the ECU's seed state is renewed between requests:
`'session'` (back to the default session and into `session` again), `'reset'` (ECUReset, then `session`) or
`'none'` (repeated requests).

**Lockout test** sends wrong keys and records after how many attempts the ECU answers exceedNumberOfAttempts
(NRC 0x36), whether it then refuses seeds with requiredTimeDelayNotExpired (NRC 0x37), and — optionally — whether
that delay survives an ECU reset. A wrong key is the correct length of all zero bytes unless `wrong_key` is given.

ISO 14229-1 lets manufacturers choose the attempt counts and delays, so the findings say what was observed; which
behaviour is acceptable is the test plan's decision.
"""

import asyncio
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from can_explorer.core import audit
from can_explorer.core.uds import NEGATIVE_RESPONSE, UdsClient, nrc_name

BETWEEN = ('none', 'session', 'reset')
EXCEED_NUMBER_OF_ATTEMPTS, REQUIRED_TIME_DELAY_NOT_EXPIRED, INVALID_KEY = 0x36, 0x37, 0x35


class SecurityTestError(Exception):
    pass


@dataclass
class SeedReport:
    level: int
    seeds: List[bytes]
    findings: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.seeds)

    @property
    def distinct(self) -> int:
        return len(set(self.seeds))

    @property
    def weak(self) -> bool:
        return bool(self.findings)

    def describe(self) -> str:
        head = f'Seed analysis of level {self.level}: {self.count} seeds, {self.distinct} distinct'
        return head + ('; ' + '; '.join(self.findings) if self.findings else '; no weakness found')


def analyse_seeds(level: int, seeds: List[bytes]) -> SeedReport:
    """The findings about a list of seeds (see the module documentation)."""
    report = SeedReport(level, list(seeds))
    nonzero = [seed for seed in seeds if any(seed)]
    if len(nonzero) < len(seeds):
        report.findings.append(f'{len(seeds) - len(nonzero)} zero seeds (the level was unlocked)')
    if len(nonzero) < 2:
        return report
    lengths = {len(seed) for seed in nonzero}
    if len(lengths) > 1:
        report.findings.append(f'seed lengths vary: {", ".join(map(str, sorted(lengths)))} bytes')
    length = min(lengths)
    if length < 4:
        report.findings.append(f'short seeds: {length} bytes, at most {256**length} values')
    counts = Counter(nonzero)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    if repeated:
        seed, times = counts.most_common(1)[0]
        report.findings.append(f'{repeated} repeated seeds (e.g. {seed.hex().upper()} {times} times)')
    constant = [position for position in range(length) if len({seed[position] for seed in nonzero}) == 1]
    if constant and len(constant) < length:
        report.findings.append(f'seed bytes {", ".join(map(str, constant))} never change')
    values = [int.from_bytes(seed[:length], 'big') for seed in nonzero]
    steps = {(later - earlier) % (1 << (8 * length)) for earlier, later in zip(values, values[1:], strict=False)}
    if len(nonzero) >= 3 and len(steps) == 1 and 0 not in steps:
        report.findings.append(f'each seed is the previous one + {steps.pop()} (predictable)')
    elif len(nonzero) >= 8 and len(counts) > 1:
        # Few distinct step sizes among many seeds: a counter or timer with jitter rather than random numbers
        if len(steps) <= len(nonzero) // 4 and max(steps) < (1 << (8 * length)) // 1024:
            report.findings.append('seeds grow by small steps (a counter or timer, not random)')
    if len(nonzero) >= 16:
        varied = [position for position in range(length) if position not in constant]
        bits = [_entropy([seed[position] for seed in nonzero]) for position in varied]
        ceiling = math.log2(len(nonzero))
        low = [position for position, entropy in zip(varied, bits, strict=True) if entropy < 0.5 * min(8.0, ceiling)]
        if low:
            report.findings.append(f'seed bytes {", ".join(map(str, low))} vary little (low entropy)')
    return report


def _entropy(values: List[int]) -> float:
    counts = Counter(values)
    total = len(values)
    return -sum(count / total * math.log2(count / total) for count in counts.values())


def _nrc(response: Optional[bytes]) -> Optional[int]:
    if response and len(response) >= 3 and response[0] == NEGATIVE_RESPONSE:
        return response[2]
    return None


async def _enter(client: UdsClient, session: int) -> None:
    if session:
        await client.call(bytes([0x10, session]))


async def collect_seeds(
    client: UdsClient, level: int, count: int = 32, between: str = 'session', session: int = 0x03
) -> List[bytes]:
    """Request `count` seeds of a security level, renewing the ECU's seed state as `between` says."""
    if level % 2 == 0:
        raise SecurityTestError('Security access levels for requesting a seed are odd')
    if between not in BETWEEN:
        raise SecurityTestError(f'Unknown renewal {between!r}; one of {", ".join(BETWEEN)}')
    seeds = []
    await _enter(client, session)
    for index in range(count):
        if index and between == 'session':
            await client.call(bytes([0x10, 0x01]))
            await _enter(client, session)
        elif index and between == 'reset':
            await client.call(bytes([0x11, 0x01]))
            await asyncio.sleep(0.05)
            await _enter(client, session)
        response = await client.call(bytes([0x27, level]))
        seeds.append(bytes(response[2:]))
    return seeds


async def seed_analysis(
    client: UdsClient, level: int, count: int = 32, between: str = 'session', session: int = 0x03
) -> SeedReport:
    report = analyse_seeds(level, await collect_seeds(client, level, count, between, session))
    audit.record('seed analysis', level=level, seeds=report.count, findings=report.findings)
    return report


@dataclass
class LockoutReport:
    level: int
    attempts: List[str] = field(default_factory=list)  # The answer to each wrong key
    locked_after: Optional[int] = None  # Wrong keys until exceedNumberOfAttempts (or a refused seed)
    delay_enforced: Optional[bool] = None  # Seeds refused with requiredTimeDelayNotExpired after the lockout
    survives_reset: Optional[bool] = None  # Still refused after an ECU reset (None: not tested)
    findings: List[str] = field(default_factory=list)

    def describe(self) -> str:
        return f'Lockout test of level {self.level}: ' + '; '.join(self.findings)


async def lockout_test(
    client: UdsClient,
    level: int,
    max_attempts: int = 10,
    wrong_key: Optional[Callable[[bytes], bytes]] = None,
    test_reset: bool = False,
    session: int = 0x03,
) -> LockoutReport:
    """Send up to `max_attempts` wrong keys; then check that seeds are refused, and with `test_reset` whether an
    ECU reset ends the delay. Leaves the ECU locked out for its delay time."""
    if level % 2 == 0:
        raise SecurityTestError('Security access levels for requesting a seed are odd')
    wrong_key = wrong_key or (lambda seed: bytes(len(seed) or 2))
    report = LockoutReport(level)
    audit.record('lockout test started', level=level, max_attempts=max_attempts)
    await _enter(client, session)
    for attempt in range(1, max_attempts + 1):
        seed_result = await client.request(bytes([0x27, level]))
        code = _nrc(seed_result.response)
        if seed_result.error or seed_result.response is None:
            raise SecurityTestError(seed_result.error or 'No answer to the seed request')
        if code is not None:
            report.attempts.append(f'seed refused: {nrc_name(code)}')
            report.locked_after = attempt - 1
            break
        seed = bytes(seed_result.response[2:])
        if not any(seed):
            raise SecurityTestError(f'Level {level} is unlocked already (zero seed); reset the ECU first')
        key_result = await client.request(bytes([0x27, level + 1]) + wrong_key(seed))
        code = _nrc(key_result.response)
        if code is None:
            raise SecurityTestError('The ECU accepted the wrong key')
        report.attempts.append(nrc_name(code))
        if code == EXCEED_NUMBER_OF_ATTEMPTS:
            report.locked_after = attempt
            break
    if report.locked_after is not None:
        report.findings.append(f'locked after {report.locked_after} wrong keys')
    else:
        report.findings.append(f'no lockout within {max_attempts} wrong keys (invalidKey every time)')
        return report
    after = _nrc((await client.request(bytes([0x27, level]))).response)
    report.delay_enforced = after == REQUIRED_TIME_DELAY_NOT_EXPIRED
    report.findings.append(
        'seeds refused with requiredTimeDelayNotExpired after the lockout'
        if report.delay_enforced
        else f'seeds after the lockout: {nrc_name(after) if after is not None else "granted"} (no delay)'
    )
    if test_reset and report.delay_enforced:
        await client.call(bytes([0x11, 0x01]))
        await asyncio.sleep(0.05)
        await _enter(client, session)
        after_reset = _nrc((await client.request(bytes([0x27, level]))).response)
        report.survives_reset = after_reset == REQUIRED_TIME_DELAY_NOT_EXPIRED
        report.findings.append(
            'the delay survives an ECU reset'
            if report.survives_reset
            else 'an ECU reset ends the delay (an attacker can reset and retry)'
        )
    return report
