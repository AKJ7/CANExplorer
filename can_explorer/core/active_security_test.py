"""Active security tests of CAN frames, for benches you are authorised to test: replay attacks and MAC forgery
attempts against an ECU.

**These tests transmit frames on the bus.** They are for lab benches where you have explicit authorisation to send
traffic, not for production vehicles. The send callable must be provided by the caller (the MCP tool or test harness);
this module never opens a bus itself.

**Replay test**: replays a captured frame with its original payload and observes whether the ECU acts on it. A frame
protected by a freshness counter should be rejected after a short time (the counter will be stale); one without
freshness protection may be accepted.

**Forgery test**: sends frames with the authentic payload but random MAC bytes and observes whether any are accepted.
A properly implemented MAC should reject every forged frame.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class ReplayResult:
    """The outcome of replaying a captured frame."""

    frame_id: int
    replayed: bool  # Whether the ECU accepted the replayed frame
    findings: List[str] = field(default_factory=list)


@dataclass
class ForgeryResult:
    """The outcome of sending frames with random MAC bytes."""

    frame_id: int
    attempts: int
    accepted: int  # Should be 0 for a real MAC
    findings: List[str] = field(default_factory=list)


async def replay_test(
    send: Callable,
    frame_id: int,
    payload: bytes,
    count: int = 10,
    interval: float = 0.1,
    observe: Optional[Callable] = None,
) -> ReplayResult:
    """Replay a captured frame and observe whether the ECU acts on it.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable() -> bool that returns True if the ECU accepted the last frame.
    count: how many times to replay.
    interval: seconds between replays.

    For benches you are authorised to test.
    """
    findings: List[str] = []
    accepted = False

    for i in range(count):
        result = send(frame_id, payload)
        if asyncio.iscoroutine(result):
            await result
        if observe is not None:
            obs = observe()
            if asyncio.iscoroutine(obs):
                obs = await obs
            if obs:
                accepted = True
                findings.append(f'Frame accepted on replay attempt {i + 1}/{count}')
                break
        if i < count - 1:
            await asyncio.sleep(interval)

    if not accepted and observe is not None:
        findings.append(f'Frame was not accepted in {count} replay attempts')
    elif observe is None:
        findings.append(f'Replayed {count} time(s); no observer provided to check acceptance')

    return ReplayResult(frame_id=frame_id, replayed=accepted, findings=findings)


async def forgery_test(
    send: Callable,
    frame_id: int,
    authentic_payload: bytes,
    mac_range: Tuple[int, int],
    attempts: int = 100,
    observe: Optional[Callable] = None,
    interval: float = 0.01,
) -> ForgeryResult:
    """Send frames with random MAC bytes and observe whether any are accepted.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable() -> bool that returns True if the ECU accepted the last frame.
    mac_range: (first_byte, last_byte) inclusive — the bytes to randomise.
    attempts: how many forged frames to send.
    interval: seconds between attempts.

    For benches you are authorised to test.
    """
    mac_first, mac_last = mac_range
    findings: List[str] = []
    accepted_count = 0

    for i in range(attempts):
        forged = bytearray(authentic_payload)
        # Randomise the MAC bytes
        for b in range(mac_first, mac_last + 1):
            if b < len(forged):
                forged[b] = int.from_bytes(os.urandom(1), 'big')
        result = send(frame_id, bytes(forged))
        if asyncio.iscoroutine(result):
            await result
        if observe is not None:
            obs = observe()
            if asyncio.iscoroutine(obs):
                obs = await obs
            if obs:
                accepted_count += 1
        if i < attempts - 1 and interval > 0:
            await asyncio.sleep(interval)

    if accepted_count > 0:
        findings.append(f'{accepted_count}/{attempts} forged frame(s) accepted: MAC verification may be weak or absent')
    elif observe is not None:
        findings.append(f'No forged frames accepted in {attempts} attempts: MAC appears effective')
    else:
        findings.append(f'Sent {attempts} forged frame(s); no observer provided to check acceptance')

    return ForgeryResult(
        frame_id=frame_id,
        attempts=attempts,
        accepted=accepted_count,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(fn: Callable, *args):
    """Call a sync or async callable."""
    result = fn(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _classify_response(data: Optional[bytes]) -> Optional[str]:
    """Classify a UDS response as 'positive', 'negative:0xNN' or None."""
    if data is None or len(data) == 0:
        return None
    if data[0] == 0x7F and len(data) >= 3:
        return f'negative:0x{data[2]:02X}'
    if data[0] & 0x40:
        return 'positive'
    return f'unknown:0x{data[0]:02X}'


# ---------------------------------------------------------------------------
# 3.2 — Functional addressing bypass test
# ---------------------------------------------------------------------------


@dataclass
class FunctionalBypassResult:
    """The outcome of testing one service via physical vs. functional addressing."""

    service: int
    physical_response: Optional[str]
    functional_response: Optional[str]
    bypass_found: bool
    findings: List[str] = field(default_factory=list)


async def functional_bypass_test(
    send: Callable,
    physical_id: int,
    functional_id: int,
    service_requests: List[bytes],
    observe: Optional[Callable] = None,
    interval: float = 0.5,
) -> List[FunctionalBypassResult]:
    """Test whether services filtered via physical addressing pass via functional addressing.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable(timeout) -> Optional[bytes] that returns the response data or None.
    physical_id: the physical request ID (e.g. 0x7E0).
    functional_id: the functional request ID (e.g. 0x7DF).
    service_requests: list of UDS request payloads to test.
    interval: seconds between requests.

    For benches you are authorised to test.
    """
    results: List[FunctionalBypassResult] = []

    for payload in service_requests:
        sid = payload[0] if payload else 0

        # Send via physical addressing
        await _call(send, physical_id, payload)
        physical_resp = None
        if observe is not None:
            raw = await _call(observe, 0.5)
            physical_resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
        await asyncio.sleep(interval)

        # Send via functional addressing
        await _call(send, functional_id, payload)
        functional_resp = None
        if observe is not None:
            raw = await _call(observe, 0.5)
            functional_resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
        await asyncio.sleep(interval)

        # Detect bypass: physical denied but functional accepted
        phys_denied = physical_resp is not None and physical_resp.startswith('negative')
        func_ok = functional_resp == 'positive'
        bypass = phys_denied and func_ok

        findings: List[str] = []
        if bypass:
            findings.append(
                f'Service 0x{sid:02X} denied via physical (0x{physical_id:03X}) '
                f'but accepted via functional (0x{functional_id:03X}): bypass confirmed'
            )
        elif observe is None:
            findings.append(f'Service 0x{sid:02X}: sent via both paths; no observer to check responses')
        else:
            findings.append(
                f'Service 0x{sid:02X}: physical={physical_resp or "no response"}, '
                f'functional={functional_resp or "no response"}'
            )

        results.append(
            FunctionalBypassResult(
                service=sid,
                physical_response=physical_resp,
                functional_response=functional_resp,
                bypass_found=bypass,
                findings=findings,
            )
        )

    return results


# ---------------------------------------------------------------------------
# 3.3 — Session race condition test
# ---------------------------------------------------------------------------


@dataclass
class SessionRaceResult:
    """The outcome of testing for session transition race conditions."""

    target_service: int
    race_window_ms: float
    service_accepted: bool
    findings: List[str] = field(default_factory=list)


async def session_race_test(
    send: Callable,
    request_id: int,
    target_service_payload: bytes,
    observe: Optional[Callable] = None,
    attempts: int = 10,
    delay_ms_range: Tuple[float, float] = (0.0, 50.0),
) -> SessionRaceResult:
    """Test for race conditions between session transitions and security enforcement.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable(timeout) -> Optional[bytes] that returns the response data or None.
    request_id: the ECU's request ID.
    target_service_payload: the locked service request to try.
    attempts: how many race attempts with varying delays.
    delay_ms_range: (min, max) delay in ms between session switch and service request.

    For benches you are authorised to test.
    """
    sid = target_service_payload[0] if target_service_payload else 0
    findings: List[str] = []
    accepted = False
    best_window_ms = 0.0
    min_delay, max_delay = delay_ms_range

    # First, send the service in default session to confirm it is locked
    await _call(send, request_id, target_service_payload)
    if observe is not None:
        raw = await _call(observe, 0.5)
        baseline = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
        if baseline == 'positive':
            findings.append(f'Service 0x{sid:02X} already accessible in default session; no race condition needed')
            return SessionRaceResult(target_service=sid, race_window_ms=0.0, service_accepted=True, findings=findings)
    await asyncio.sleep(0.1)

    for i in range(attempts):
        # Compute delay for this attempt (sweep linearly through the range)
        delay_ms = min_delay + (max_delay - min_delay) * i / max(attempts - 1, 1)

        # Switch back to default session first
        await _call(send, request_id, b'\x10\x01')
        await asyncio.sleep(0.2)

        # Switch to extended session
        t_start = time.monotonic()
        await _call(send, request_id, b'\x10\x03')

        # Wait the computed delay
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        # Immediately send the locked service
        await _call(send, request_id, target_service_payload)
        t_sent = time.monotonic()

        if observe is not None:
            raw = await _call(observe, 0.5)
            resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
            if resp == 'positive':
                window = (t_sent - t_start) * 1000.0
                accepted = True
                best_window_ms = max(best_window_ms, window)
                findings.append(
                    f'Attempt {i + 1}/{attempts}: service 0x{sid:02X} accepted at delay {delay_ms:.1f} ms '
                    f'(window ~{window:.1f} ms)'
                )
                break

        await asyncio.sleep(0.1)

    if not accepted and observe is not None:
        findings.append(
            f'No race condition found for service 0x{sid:02X} across {attempts} attempts '
            f'(delays {min_delay:.0f}-{max_delay:.0f} ms)'
        )
    elif observe is None:
        findings.append(f'Sent {attempts} race attempt(s); no observer provided to check acceptance')

    return SessionRaceResult(
        target_service=sid,
        race_window_ms=best_window_ms,
        service_accepted=accepted,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 3.4 — ECU reset bypass test
# ---------------------------------------------------------------------------


@dataclass
class ResetBypassResult:
    """The outcome of testing for ECU reset bypass."""

    target_service: int
    bypass_found: bool
    window_ms: float
    services_accessible: List[int]
    findings: List[str] = field(default_factory=list)


async def reset_bypass_test(
    send: Callable,
    request_id: int,
    target_service_payloads: List[bytes],
    observe: Optional[Callable] = None,
    reset_type: int = 1,
    attempts: int = 5,
    max_window_ms: float = 5000.0,
    probe_interval_ms: float = 50.0,
) -> ResetBypassResult:
    """Test for ECU reset bypass: send ECUReset, then probe locked services during boot-up.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable(timeout) -> Optional[bytes] that returns the response data or None.
    request_id: the ECU's request ID.
    target_service_payloads: locked service requests to try during boot-up.
    reset_type: ECUReset sub-function (1 = hard reset).
    attempts: how many reset cycles to try.
    max_window_ms: how long to probe after reset.
    probe_interval_ms: interval between probes.

    For benches you are authorised to test.
    """
    findings: List[str] = []
    accessible: Dict[int, float] = {}
    best_window_ms = 0.0
    primary_sid = target_service_payloads[0][0] if target_service_payloads else 0

    for attempt in range(attempts):
        # Send ECUReset
        reset_payload = bytes([0x11, reset_type & 0xFF])
        await _call(send, request_id, reset_payload)
        t_reset = time.monotonic()

        # Brief initial wait for the ECU to begin rebooting
        await asyncio.sleep(0.01)

        # Probe services during the boot-up window
        probes = int(max_window_ms / probe_interval_ms)
        for _ in range(probes):
            elapsed_ms = (time.monotonic() - t_reset) * 1000.0
            if elapsed_ms > max_window_ms:
                break

            for payload in target_service_payloads:
                sid = payload[0] if payload else 0
                await _call(send, request_id, payload)

                if observe is not None:
                    timeout = min(0.1, probe_interval_ms / 1000.0)
                    raw = await _call(observe, timeout)
                    resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
                    if resp == 'positive' and sid not in accessible:
                        window = (time.monotonic() - t_reset) * 1000.0
                        accessible[sid] = window
                        best_window_ms = max(best_window_ms, window)
                        findings.append(
                            f'Attempt {attempt + 1}/{attempts}: service 0x{sid:02X} accepted {window:.0f} ms '
                            f'after reset'
                        )

            await asyncio.sleep(probe_interval_ms / 1000.0)

        # Wait for ECU to fully recover before next attempt
        await asyncio.sleep(max(1.0, max_window_ms / 1000.0))

    bypass = len(accessible) > 0
    if not bypass and observe is not None:
        findings.append(
            f'No boot-up bypass found across {attempts} reset cycle(s) '
            f'(probed for {max_window_ms:.0f} ms after each reset)'
        )
    elif observe is None:
        findings.append(f'Sent {attempts} reset cycle(s); no observer provided to check acceptance')

    return ResetBypassResult(
        target_service=primary_sid,
        bypass_found=bypass,
        window_ms=best_window_ms,
        services_accessible=sorted(accessible.keys()),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# 3.8 — Gateway DoS resilience assessment
# ---------------------------------------------------------------------------


@dataclass
class DosResilienceResult:
    """The outcome of testing gateway resilience under high bus load."""

    max_sustained_rate_hz: float
    degradation_threshold_hz: float
    frames_sent: int
    frames_dropped: int
    gateway_crashed: bool
    recovery_time_ms: float
    findings: List[str] = field(default_factory=list)


async def dos_resilience_test(
    send: Callable,
    request_id: int,
    test_payload: bytes,
    observe: Optional[Callable] = None,
    rates_hz: Optional[List[float]] = None,
    duration_per_rate: float = 2.0,
) -> DosResilienceResult:
    """Test the gateway's resilience under high diagnostic request rates.

    send: async or sync callable(frame_id, payload) that transmits a frame.
    observe: optional async or sync callable(timeout) -> Optional[bytes] that returns the response data or None.
    request_id: the ECU's request ID.
    test_payload: a valid diagnostic request (e.g. TesterPresent 3E 00).
    rates_hz: list of rates to test, ascending (default: [10, 50, 100, 200, 500]).
    duration_per_rate: seconds to test at each rate.

    For benches you are authorised to test.
    """
    if rates_hz is None:
        rates_hz = [10.0, 50.0, 100.0, 200.0, 500.0]

    findings: List[str] = []
    total_sent = 0
    total_dropped = 0
    max_sustained = 0.0
    degradation_threshold = 0.0
    gateway_crashed = False
    recovery_time_ms = 0.0
    degradation_found = False

    for rate in sorted(rates_hz):
        interval = 1.0 / rate if rate > 0 else 1.0
        count = max(1, int(rate * duration_per_rate))
        responded = 0

        for i in range(count):
            await _call(send, request_id, test_payload)
            total_sent += 1

            if observe is not None:
                timeout = min(0.5, interval * 2)
                raw = await _call(observe, timeout)
                resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
                if resp is not None:
                    responded += 1

            if i < count - 1 and interval > 0.001:
                await asyncio.sleep(interval)

        if observe is not None:
            dropped = count - responded
            total_dropped += dropped
            response_rate = responded / count if count > 0 else 0.0

            if response_rate >= 0.9:
                max_sustained = rate
            elif not degradation_found:
                degradation_threshold = rate
                degradation_found = True

            if responded == 0 and count >= 5:
                gateway_crashed = True
                findings.append(f'Gateway stopped responding at {rate:.0f} Hz ({count} frames, 0 responses)')

                # Measure recovery time
                t_crash = time.monotonic()
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    await _call(send, request_id, test_payload)
                    raw = await _call(observe, 0.5)
                    resp = _classify_response(raw if isinstance(raw, (bytes, bytearray)) else None)
                    if resp is not None:
                        recovery_time_ms = (time.monotonic() - t_crash) * 1000.0
                        findings.append(f'Gateway recovered after {recovery_time_ms:.0f} ms')
                        break
                else:
                    recovery_time_ms = 5000.0
                    findings.append('Gateway did not recover within 5 seconds')
                break
            else:
                findings.append(
                    f'Rate {rate:.0f} Hz: {responded}/{count} responses ({response_rate * 100:.0f}%), {dropped} dropped'
                )
        else:
            findings.append(f'Rate {rate:.0f} Hz: sent {count} frames; no observer to check responses')

        await asyncio.sleep(0.5)

    if not degradation_found:
        degradation_threshold = max_sustained

    if observe is not None and not gateway_crashed and max_sustained > 0:
        findings.append(f'Maximum sustained rate: {max_sustained:.0f} Hz')
        if degradation_found:
            findings.append(f'Degradation starts at {degradation_threshold:.0f} Hz')

    return DosResilienceResult(
        max_sustained_rate_hz=max_sustained,
        degradation_threshold_hz=degradation_threshold,
        frames_sent=total_sent,
        frames_dropped=total_dropped,
        gateway_crashed=gateway_crashed,
        recovery_time_ms=recovery_time_ms,
        findings=findings,
    )
