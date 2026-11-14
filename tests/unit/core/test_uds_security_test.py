import os

import pytest

from can_explorer.core.simulation import UdsServer
from can_explorer.core.uds import UdsClient
from can_explorer.core.uds_security_test import SecurityTestError, analyse_seeds, lockout_test, seed_analysis


class Loopback:
    def __init__(self, server):
        self.server, self.responses = server, []

    async def send(self, payload):
        self.responses += self.server.handle(bytes(payload))

    async def receive(self, timeout=None):
        return self.responses.pop(0)


def client(**kwargs):
    server = UdsServer(**kwargs)
    return UdsClient(Loopback(server)), server


def test_analysis_findings():
    good = analyse_seeds(1, [os.urandom(8) for _ in range(64)])
    assert not good.weak and good.describe().endswith('no weakness found')
    constant = analyse_seeds(1, [b'\x12\x34'] * 10)
    assert constant.findings == ['short seeds: 2 bytes, at most 65536 values', '9 repeated seeds (e.g. 1234 10 times)']
    counter = analyse_seeds(1, [(1000 + 3 * index).to_bytes(4, 'big') for index in range(6)])
    assert 'each seed is the previous one + 3 (predictable)' in counter.findings
    assert 'seed bytes 0, 1, 2 never change' in counter.findings  # 1000 to 1015: 0x0003E8 to 0x0003F7
    jittery = analyse_seeds(1, [(5000 + 37 * index + index % 3).to_bytes(4, 'big') for index in range(20)])
    assert 'seeds grow by small steps (a counter or timer, not random)' in jittery.findings
    zero = analyse_seeds(1, [bytes(4), os.urandom(4)])
    assert zero.findings == ['1 zero seeds (the level was unlocked)']
    mixed = analyse_seeds(1, [os.urandom(4), os.urandom(6)])
    assert mixed.findings == ['seed lengths vary: 4, 6 bytes']


async def test_seed_analysis_against_the_simulated_ecu():
    tester, server = client()
    report = await seed_analysis(tester, 1, count=5)  # The default simulator always sends the same seed
    assert report.seeds == [b'\x12\x34'] * 5 and report.weak
    assert sum(request[:2] == b'\x10\x01' for request in server.requests) == 4  # A new session between seeds
    tester, server = client(seed_generator=lambda: os.urandom(8))
    report = await seed_analysis(tester, 1, count=20, between='reset')
    assert report.distinct == 20 and not report.weak
    assert sum(request[:1] == b'\x11' for request in server.requests) == 19
    with pytest.raises(SecurityTestError, match='odd'):
        await seed_analysis(tester, 2)
    with pytest.raises(SecurityTestError, match='Unknown renewal'):
        await seed_analysis(tester, 1, between='power cycle')


async def test_lockout():
    now = [0.0]
    tester, server = client(
        seed_generator=lambda: os.urandom(4), max_attempts=3, lockout_delay=10, clock=lambda: now[0]
    )
    report = await lockout_test(tester, 1, test_reset=True)
    assert report.attempts == ['invalidKey', 'invalidKey', 'exceedNumberOfAttempts']
    assert report.locked_after == 3 and report.delay_enforced and report.survives_reset
    assert report.describe() == (
        'Lockout test of level 1: locked after 3 wrong keys; seeds refused with requiredTimeDelayNotExpired after '
        'the lockout; the delay survives an ECU reset'
    )
    now[0] = 11.0  # After the delay, the ECU grants seeds again
    assert (await tester.call(b'\x27\x01'))[0] == 0x67

    weak, _ = client(seed_generator=lambda: os.urandom(4), max_attempts=2, lockout_survives_reset=False)
    report = await lockout_test(weak, 1, test_reset=True)
    assert report.survives_reset is False and 'an ECU reset ends the delay' in report.describe()

    endless, _ = client()
    report = await lockout_test(endless, 1, max_attempts=4)
    assert report.locked_after is None and report.attempts == ['invalidKey'] * 4
    assert report.describe() == 'Lockout test of level 1: no lockout within 4 wrong keys (invalidKey every time)'


async def test_lockout_refuses_to_run_in_odd_situations():
    tester, server = client()
    with pytest.raises(SecurityTestError, match='accepted the wrong key'):
        await lockout_test(tester, 1, wrong_key=lambda seed: bytes(b ^ 0xFF for b in seed))  # The right key
    with pytest.raises(SecurityTestError, match='unlocked already'):
        await lockout_test(tester, 1)
