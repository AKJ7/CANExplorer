import asyncio


from can_explorer.core.reverse import active_prober
from can_explorer.core.reverse.active_prober import (
    DANGEROUS_SERVICES,
    ProbeStep,
    execute,
    steps_from_brute,
    steps_from_enumeration,
    steps_from_scan,
)
from can_explorer.core.reverse.gateway_security import (
    BruteForcePlan,
    BruteForceStep,
    EnumerationPlan,
    EnumerationStep,
    ScanPlan,
    ScanRange,
)


class FakeBus:
    """Records sends; answers each request with a scripted response keyed by request id."""

    def __init__(self, responses=None):
        self.sent = []
        self._responses = responses or {}

    def send(self, request_id, request):
        self.sent.append((request_id, bytes(request)))

    def observe(self, timeout):
        request_id = self.sent[-1][0]
        return self._responses.get(request_id)


def run(coro):
    return asyncio.run(coro)


def test_steps_from_enumeration_and_brute_and_scan():
    enum = EnumerationPlan(
        steps=[
            EnumerationStep(0x7E0, 'read DID', '22F190', 'positive', [], 0x03, None),
            EnumerationStep(0x7E0, 'empty', '', '', [], 0x01, None),
        ],
        ecu_count=1,
        estimated_time_seconds=1.0,
        findings=[],
    )
    steps = steps_from_enumeration(enum)
    assert len(steps) == 1 and steps[0].request == bytes.fromhex('22F190') and steps[0].session == 0x03

    brute = BruteForcePlan(
        steps=[BruteForceStep(0x7E0, 0x50, 0x07, 'OEM level', '2707')],
        ecu_count=1,
        oem_sessions=[0x50],
        non_standard_levels=[0x07],
        estimated_attempts=1,
        findings=[],
    )
    assert steps_from_brute(brute)[0].request == bytes.fromhex('2707')

    scan = ScanPlan(
        ranges=[ScanRange(0x600, 0x603, False, 'gap')], known_responders=[], estimated_probes=4, findings=[]
    )
    scan_steps = steps_from_scan(scan)
    assert [s.request_id for s in scan_steps] == [0x600, 0x601, 0x602, 0x603]
    assert all(s.request == active_prober.SCAN_PROBE for s in scan_steps)
    assert len(steps_from_scan(scan, max_ids=2)) == 2  # The cap bounds a wide scan


def test_dry_run_sends_nothing():
    bus = FakeBus()
    steps = [ProbeStep(0x7E0, bytes.fromhex('22F190'))]
    report = run(execute(steps, bus.send, bus.observe, dry_run=True))
    assert bus.sent == [] and report.sent == 0 and report.skipped == 1
    assert report.results[0].classification == 'dry-run'


def test_allowlist_blocks_other_ids():
    bus = FakeBus(responses={0x7E0: bytes.fromhex('62F19000')})
    steps = [ProbeStep(0x7E0, bytes.fromhex('22F190')), ProbeStep(0x7E5, bytes.fromhex('22F190'))]
    report = run(execute(steps, bus.send, bus.observe, allowlist={0x7E0}, min_interval=0))
    assert [s[0] for s in bus.sent] == [0x7E0]  # Only the allowlisted id was sent
    assert report.results[1].classification.startswith('skipped:not-allowlisted')


def test_dangerous_services_blocked_by_default_and_overridable():
    bus = FakeBus(responses={0x7E0: bytes.fromhex('51')})
    reset = [ProbeStep(0x7E0, bytes.fromhex('1101'))]  # ECUReset is in DANGEROUS_SERVICES
    assert 0x11 in DANGEROUS_SERVICES
    blocked_report = run(execute(reset, bus.send, bus.observe, min_interval=0))
    assert bus.sent == [] and blocked_report.results[0].classification.startswith('skipped:blocked-service')
    allowed_report = run(execute(reset, bus.send, bus.observe, blocked_services=set(), min_interval=0))
    assert bus.sent == [(0x7E0, bytes.fromhex('1101'))] and allowed_report.sent == 1


def test_positive_and_negative_classification():
    bus = FakeBus(responses={0x7E0: bytes.fromhex('62F19012'), 0x7E1: bytes.fromhex('7F2233')})
    steps = [ProbeStep(0x7E0, bytes.fromhex('22F190'), expected='positive'), ProbeStep(0x7E1, bytes.fromhex('22F190'))]
    report = run(execute(steps, bus.send, bus.observe, min_interval=0))
    assert report.results[0].classification == 'positive'
    assert report.results[0].response_hex == '62f19012'
    assert report.results[1].classification.startswith('negative:')
    assert report.responders == [0x7E0, 0x7E1]
    assert any('accepted' in f for f in report.results[0].findings)


def test_no_response_and_request_cap():
    bus = FakeBus(responses={})
    steps = [ProbeStep(0x700 + i, bytes.fromhex('3E80')) for i in range(5)]
    report = run(execute(steps, bus.send, bus.observe, min_interval=0, max_requests=2))
    assert report.sent == 2 and len(bus.sent) == 2
    assert any('Request cap' in f for f in report.findings)
    assert report.results[0].classification == 'no-response'


def test_confirm_callback_can_veto():
    bus = FakeBus(responses={0x7E0: bytes.fromhex('62F190'), 0x7E1: bytes.fromhex('62F190')})
    steps = [ProbeStep(0x7E0, bytes.fromhex('22F190')), ProbeStep(0x7E1, bytes.fromhex('22F190'))]
    report = run(execute(steps, bus.send, bus.observe, min_interval=0, confirm=lambda s: s.request_id == 0x7E0))
    assert [s[0] for s in bus.sent] == [0x7E0]
    assert report.results[1].classification == 'skipped:declined'


def test_async_send_and_observe():
    sent = []

    async def send(request_id, request):
        sent.append((request_id, bytes(request)))

    async def observe(timeout):
        return bytes.fromhex('62F190')

    report = run(execute([ProbeStep(0x7E0, bytes.fromhex('22F190'))], send, observe, min_interval=0))
    assert sent and report.results[0].classification == 'positive'
