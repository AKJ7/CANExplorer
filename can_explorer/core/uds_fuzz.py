"""Mutation fuzzing of a UDS endpoint, for robustness testing.

Starting from one or more seed requests (or random ones), the fuzzer mutates them — flipping bits, substituting
boundary bytes, truncating, extending, duplicating — and sends each to the ECU over any `UdsTransport` through a
`UdsClient`. Each request becomes a `ScanResult` (kind `fuzz`), classified so the anomalies stand out: a positive
response to a malformed request, an unusual negative response, and — most important — the ECU going unresponsive,
which a periodic liveness check detects and reports as a possible hang or crash.

Fuzzing inherently sends arbitrary, state-changing requests, so it is for ECUs you own or are authorised to test,
on a bench, with the vehicle stationary. The run is reproducible: the same `seed` replays the same sequence, so a
request that upset the ECU can be found again.

Example:
    ```python
    from can_explorer.core.isotp_client import IsoTpClient
    from can_explorer.core.uds import UdsClient
    from can_explorer.core.uds_fuzz import fuzz

    client = UdsClient(IsoTpClient(session, 0x7E0, 0x7E8), timeout=0.2)
    async for result in fuzz(client, seeds=[bytes([0x22, 0xF1, 0x90])], count=500, seed=1):
        if result.interesting:
            print(result.request.hex(), result.summary)
    ```
"""

import asyncio
import random
from typing import AsyncIterator, Optional, Sequence, Tuple

from can_explorer.core.uds import SUPPRESS_POSITIVE_RESPONSE, UdsClient
from can_explorer.core.uds_scan import FUZZ, KNOWN_SERVICES, ScanResult

MAX_PAYLOAD = 32  # Longest mutated request the fuzzer builds
BOUNDARY_BYTES = (0x00, 0x01, 0x7F, 0x80, 0xFF)
DEFAULT_LIVENESS_EVERY = 25  # Check the ECU still answers after this many requests
# TesterPresent with a required (not suppressed) response: a well-behaved ECU always answers 0x7E
LIVENESS_REQUEST = bytes([0x3E, 0x00])


def _mutate(rng: random.Random, seed: bytes) -> bytes:
    """One mutation of a seed request."""
    data = bytearray(seed) if seed else bytearray([rng.randrange(256)])
    strategy = rng.randrange(7)
    if strategy == 0:  # Flip a random bit
        index = rng.randrange(len(data))
        data[index] ^= 1 << rng.randrange(8)
    elif strategy == 1:  # Set a byte to a boundary value
        data[rng.randrange(len(data))] = rng.choice(BOUNDARY_BYTES)
    elif strategy == 2:  # Set a byte to a random value
        data[rng.randrange(len(data))] = rng.randrange(256)
    elif strategy == 3 and len(data) > 1:  # Truncate
        data = data[: rng.randrange(1, len(data))]
    elif strategy == 4:  # Extend with random bytes
        data += bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
    elif strategy == 5 and len(data) > 1:  # Duplicate the payload after the service byte
        data = data[:1] + data[1:] * 2
    else:  # Replace the whole payload with random bytes, keeping a plausible service byte
        service = seed[0] if seed and rng.random() < 0.5 else rng.choice(KNOWN_SERVICES)
        data = bytearray([service]) + bytes(rng.randrange(256) for _ in range(rng.randint(0, 6)))
    if not data:
        data = bytearray([rng.randrange(256)])
    # Never suppress the positive response: a suppressed answer looks like a hang to the liveness logic
    if len(data) >= 2 and data[0] in (0x10, 0x11, 0x3E, 0x85, 0x28):
        data[1] &= ~SUPPRESS_POSITIVE_RESPONSE
    return bytes(data[:MAX_PAYLOAD])


def _seed(rng: random.Random, seeds: Sequence[bytes]) -> bytes:
    if seeds:
        return rng.choice(seeds)
    return bytes([rng.choice(KNOWN_SERVICES)])


async def _alive(client: UdsClient) -> bool:
    """Whether the ECU answers a plain TesterPresent — used to notice it has stopped responding."""
    result = await client.request(LIVENESS_REQUEST)
    return result.error is None and result.response is not None


async def fuzz(
    client: UdsClient,
    seeds: Sequence[bytes] = (),
    count: int = 500,
    *,
    seed: Optional[int] = None,
    delay: float = 0.0,
    liveness_every: int = DEFAULT_LIVENESS_EVERY,
    stop_on_unresponsive: bool = True,
    stop: Optional[asyncio.Event] = None,
) -> AsyncIterator[ScanResult]:
    """Send `count` mutated requests and yield each as a `ScanResult` (kind `fuzz`).

    `seeds` are the base requests to mutate (empty: random requests from the known services). `seed` makes the run
    reproducible. Every `liveness_every` requests the ECU is checked with a TesterPresent; if it does not answer, an
    unresponsive result is yielded (a possible hang or crash) and, unless `stop_on_unresponsive` is False, the run
    stops.
    """
    rng = random.Random(seed)
    for index in range(count):
        if stop is not None and stop.is_set():
            return
        request = _mutate(rng, _seed(rng, seeds))
        result = await client.request(request)
        yield ScanResult(FUZZ, index, request, result.response, result.error)
        if liveness_every and (index + 1) % liveness_every == 0:
            if not await _alive(client):
                yield ScanResult(
                    FUZZ,
                    index,
                    LIVENESS_REQUEST,
                    None,
                    f'ECU did not answer a liveness check after {index + 1} requests (possible hang or crash)',
                )
                if stop_on_unresponsive:
                    return
        if delay:
            await asyncio.sleep(delay)


def parse_seeds(text: str) -> Tuple[bytes, ...]:
    """Seed requests from text: one per line (or separated by ';' or ','), each as hex bytes. Empty: none."""
    from can_explorer.core.frames import parse_hex_bytes

    seeds = []
    for part in text.replace(';', '\n').replace(',', '\n').splitlines():
        part = part.strip()
        if part:
            payload = parse_hex_bytes(part)
            if payload:
                seeds.append(payload)
    return tuple(seeds)
