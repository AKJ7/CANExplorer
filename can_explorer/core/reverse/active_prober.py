"""Execute a probing plan on the bus, with safety rails, for benches you are authorised to test.

The planners in :mod:`can_explorer.core.reverse.gateway_security` (``plan_probing``, ``enumerate_plan``,
``brute_plan``, ``scan_plan_non_standard``) are passive: they read a capture and emit the *steps* worth trying, but
never transmit. This module runs those steps — it turns planned intelligence into results — and is therefore the
one reverse-engineering primitive here that sends traffic.

**It transmits on the bus.** It is for lab benches where you have explicit authorisation, not production vehicles,
and never opens a bus itself: the caller supplies a ``send`` callable (the GUI or MCP tool wires it to a session).
Several safety rails bound what it does:

- **dry run** (the default is *off*, but the GUI/MCP default it *on*): plan everything, send nothing.
- **allowlist**: only request IDs in the allowlist may be sent; anything else is skipped.
- **blocked services**: state-changing UDS services (ECU reset, writes, routines, transfers, …) are skipped by
  default — a probe should read, not alter the ECU — unless the caller opts into them for an engagement.
- **rate limit**: at least ``min_interval`` seconds between frames.
- **request cap**: at most ``max_requests`` frames in one run.
- **per-step confirmation**: an optional ``confirm`` callback can veto any individual step.

Every send is recorded in the audit log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, Set, Union

from can_explorer.core import audit
from can_explorer.core.reverse.gateway_security import (
    SID_TESTER_PRESENT,
    BruteForcePlan,
    EnumerationPlan,
    ScanPlan,
    decode_nrc,
)

# UDS services that change ECU state: skipped by default so a probe reads rather than alters the ECU.
DANGEROUS_SERVICES: Set[int] = {
    0x11,  # ECUReset
    0x14,  # ClearDiagnosticInformation
    0x2E,  # WriteDataByIdentifier
    0x2F,  # InputOutputControlByIdentifier
    0x31,  # RoutineControl
    0x34,  # RequestDownload
    0x35,  # RequestUpload
    0x36,  # TransferData
    0x37,  # RequestTransferExit
    0x38,  # RequestFileTransfer
    0x3D,  # WriteMemoryByAddress
    0x85,  # ControlDTCSetting
    0x28,  # CommunicationControl
}

# A benign probe for scanning whether an ID answers at all: TesterPresent, suppress-positive-response.
SCAN_PROBE = bytes([SID_TESTER_PRESENT, 0x80])

SendFn = Callable[[int, bytes], Union[None, Awaitable[None]]]
ObserveFn = Callable[[float], Union[Optional[bytes], Awaitable[Optional[bytes]]]]


@dataclass
class ProbeStep:
    """One executable probe: send ``request`` to ``request_id`` and read the answer."""

    request_id: int
    request: bytes
    description: str = ''
    session: int = 0x01
    expected: str = ''

    @property
    def service(self) -> int:
        return self.request[0] if self.request else -1


@dataclass
class ProbeOutcome:
    """What one step did on the bus."""

    step: ProbeStep
    sent: bool
    classification: str  # 'positive', 'negative:<nrc>', 'no-response', 'skipped:<reason>', 'dry-run'
    response_hex: str = ''
    findings: List[str] = field(default_factory=list)


@dataclass
class ProbeExecutionReport:
    results: List[ProbeOutcome]
    sent: int
    skipped: int
    responders: List[int]  # Request IDs that produced any response
    findings: List[str] = field(default_factory=list)


def steps_from_enumeration(plan: EnumerationPlan) -> List[ProbeStep]:
    return [
        ProbeStep(
            request_id=step.ecu_id,
            request=bytes.fromhex(step.uds_request_hex),
            description=step.description,
            session=step.session,
            expected=step.expected_response,
        )
        for step in plan.steps
        if step.uds_request_hex
    ]


def steps_from_brute(plan: BruteForcePlan) -> List[ProbeStep]:
    return [
        ProbeStep(
            request_id=step.ecu_id,
            request=bytes.fromhex(step.uds_request_hex),
            description=step.description,
            session=step.session,
        )
        for step in plan.steps
        if step.uds_request_hex
    ]


def steps_from_scan(plan: ScanPlan, probe: bytes = SCAN_PROBE, max_ids: int = 256) -> List[ProbeStep]:
    """One benign probe per candidate ID in the plan's ranges, capped at ``max_ids`` to bound a wide scan."""
    steps: List[ProbeStep] = []
    for scan_range in plan.ranges:
        for request_id in range(scan_range.start_id, scan_range.end_id + 1):
            steps.append(
                ProbeStep(
                    request_id=request_id,
                    request=probe,
                    description=f'scan 0x{request_id:X}: {scan_range.rationale}',
                )
            )
            if len(steps) >= max_ids:
                return steps
    return steps


async def _call(fn: Callable, *args):
    result = fn(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _classify(request: bytes, response: Optional[bytes]) -> str:
    if not response:
        return 'no-response'
    if response[0] == 0x7F:
        nrc = response[2] if len(response) >= 3 else 0
        return f'negative:{decode_nrc(nrc)}'
    if request and response[0] == (request[0] + 0x40) & 0xFF:
        return 'positive'
    return 'unexpected'


async def execute(
    steps: Sequence[ProbeStep],
    send: SendFn,
    observe: Optional[ObserveFn] = None,
    *,
    allowlist: Optional[Set[int]] = None,
    blocked_services: Optional[Set[int]] = None,
    min_interval: float = 0.1,
    max_requests: int = 200,
    dry_run: bool = False,
    confirm: Optional[Callable[[ProbeStep], bool]] = None,
    observe_timeout: float = 0.5,
) -> ProbeExecutionReport:
    """Run a probing plan on an authorised bench. ``send(request_id, request)`` transmits a frame (sync or async);
    ``observe(timeout)`` optionally returns the response bytes. See the module docstring for the safety rails.
    ``blocked_services`` defaults to :data:`DANGEROUS_SERVICES`; pass an empty set to allow them for an engagement.
    """
    blocked = DANGEROUS_SERVICES if blocked_services is None else blocked_services
    results: List[ProbeOutcome] = []
    responders: Set[int] = set()
    sent = skipped = 0
    findings: List[str] = []

    for step in steps:
        if sent >= max_requests:
            findings.append(f'Request cap ({max_requests}) reached; {len(steps) - len(results)} steps not run')
            break
        reason = None
        if allowlist is not None and step.request_id not in allowlist:
            reason = f'skipped:not-allowlisted (0x{step.request_id:X})'
        elif step.service in blocked:
            reason = f'skipped:blocked-service (0x{step.service:02X})'
        elif confirm is not None and not confirm(step):
            reason = 'skipped:declined'
        elif dry_run:
            reason = 'dry-run'
        if reason is not None:
            skipped += 1
            results.append(ProbeOutcome(step=step, sent=False, classification=reason))
            continue

        if sent > 0:
            await asyncio.sleep(min_interval)
        await _call(send, step.request_id, step.request)
        audit.record('active probe sent', request_id=f'0x{step.request_id:X}', service=f'0x{step.service:02X}')
        sent += 1
        response = None
        if observe is not None:
            raw = await _call(observe, observe_timeout)
            response = bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
        classification = _classify(step.request, response)
        outcome_findings: List[str] = []
        if response is not None:
            responders.add(step.request_id)
            if classification == 'positive':
                outcome_findings.append(
                    f'0x{step.request_id:X} service 0x{step.service:02X} accepted'
                    + (f' (expected {step.expected})' if step.expected else '')
                )
        results.append(
            ProbeOutcome(
                step=step,
                sent=True,
                classification=classification,
                response_hex=response.hex() if response else '',
                findings=outcome_findings,
            )
        )

    if dry_run:
        findings.append(f'Dry run: {len(results)} steps planned, none sent')
    return ProbeExecutionReport(
        results=results,
        sent=sent,
        skipped=skipped,
        responders=sorted(responders),
        findings=findings,
    )
