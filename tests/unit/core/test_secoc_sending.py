"""Securing SecOC PDUs to send (transmit, restbus, test scripts), check statistics and manipulated frames."""

import asyncio
import dataclasses
import uuid

import can
import pytest

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.restbus import RestbusSimulator
from can_explorer.core.secoc_check import ATTACKS, SecOcConfig, SecOcError, SecOcVerifier, StaticSecOcKeys
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.testing import TestRunner, Verdict

KEY = bytes(range(16))
CONFIG = SecOcConfig('SecuredStatus', 18, 2, 28, 32, 4, 'CMAC_AES128', 'FreshnessCounter')


def verifier() -> SecOcVerifier:
    return SecOcVerifier(StaticSecOcKeys({'*': (KEY, '')}))


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out'
        await asyncio.sleep(0.01)


def session_with(secoc_arxml, channel, secoc=None) -> BusSession:
    decoder = FrameDecoder(database=DbcDecoder(secoc_arxml), secoc=secoc if secoc is not None else verifier())
    return BusSession(LiveSource(BusConfig(interface='virtual', channel=channel)), decoder)


# Statistics and manipulated frames


def test_statistics_count_the_outcomes():
    sender, receiver = verifier(), verifier()
    good = sender.secure(CONFIG, b'\1\2')
    receiver.check(CONFIG, good)
    receiver.check(CONFIG, bytes([good[0] ^ 1]) + good[1:])  # MAC error
    receiver.check(CONFIG, b'\1')  # Too short: not checked
    full = dataclasses.replace(CONFIG, freshness_bits=8, freshness_tx_bits=8)
    first = sender.secure(full, b'\1\2', 5)
    receiver.check(full, first)
    receiver.check(full, first)  # Replay: freshness error
    statistics = receiver.statistics['SecuredStatus'].as_dict()
    assert statistics == {
        'ok': 2,
        'mac_errors': 1,
        'freshness_errors': 1,
        'not_checked': 1,
        'sent': 0,
        'last': 'SecOC FRESHNESS ERROR, FV 5 not newer than 5',
    }
    assert sender.statistics['SecuredStatus'].sent == 2
    receiver.reset()
    assert receiver.statistics == {}


@pytest.mark.parametrize('attack', sorted(ATTACKS))
def test_manipulated_frames_are_rejected_by_an_ecu(attack):
    tester, ecu = verifier(), verifier()
    for _ in range(3):  # The ECU has seen genuine frames, so its freshness is synchronised
        assert ecu.check(CONFIG, tester.secure(CONFIG, b'\x10\x20')).ok
    frame = tester.manipulate(CONFIG, b'\x10\x20', attack)
    result = ecu.check(CONFIG, frame)
    assert result.checked and not result.ok, result.describe()
    assert ecu.check(CONFIG, tester.secure(CONFIG, b'\x10\x20')).ok  # Genuine frames still verify afterwards


def test_manipulated_frame_kinds():
    tester = verifier()
    with pytest.raises(SecOcError, match='needs a frame of SecuredStatus sent secured before'):
        tester.manipulate(CONFIG, b'\1\2', 'replay')
    with pytest.raises(SecOcError, match='Unknown SecOC attack'):
        tester.manipulate(CONFIG, b'\1\2', 'guess')
    genuine = tester.secure(CONFIG, b'\1\2')
    assert tester.manipulate(CONFIG, b'\1\2', 'replay') == genuine
    stale = tester.manipulate(CONFIG, b'\1\2', 'stale_freshness')
    assert stale == genuine  # Same payload and freshness value: the same valid frame
    assert tester.manipulate(CONFIG, b'\1\2', 'replay') == genuine  # A stale frame does not become "last sent"
    flipped = tester.manipulate(CONFIG, b'\1\2', 'payload_bitflip')
    assert flipped[0] == 0 and flipped[1] == 2  # The lowest bit of the first payload byte
    wrong = tester.manipulate(CONFIG, b'\1\2', 'wrong_mac')  # Freshness values 1, 2 (bit flip), 3 (wrong MAC)
    genuine_3 = tester.secure(CONFIG, b'\1\2', 3)
    # Only the last of the 32 trailer bits (4 freshness + 28 MAC) after the 2 payload bytes differs
    assert bytes(a ^ b for a, b in zip(wrong, genuine_3, strict=True)) == bytes(5) + b'\x01'


# FrameDecoder.secure and periodic frames


def test_frame_decoder_secures_secured_pdus_only(secoc_arxml):
    decoder = FrameDecoder(database=DbcDecoder(secoc_arxml), secoc=verifier())
    msg = decoder.secure(can.Message(arbitration_id=0x300, is_extended_id=False, data=b'\5\0' + bytes(4)))
    assert verifier().check(CONFIG, bytes(msg.data)).ok and msg.dlc == 6
    other = can.Message(arbitration_id=0x123, data=b'\1\2')
    assert decoder.secure(other) is other and bytes(other.data) == b'\1\2'
    with pytest.raises(SecOcError, match='no security manager'):
        FrameDecoder(database=DbcDecoder(secoc_arxml)).secure(can.Message(arbitration_id=0x300, is_extended_id=False))
    with pytest.raises(SecOcError, match='no key'):
        FrameDecoder(database=DbcDecoder(secoc_arxml), secoc=SecOcVerifier(StaticSecOcKeys({}))).secure(
            can.Message(arbitration_id=0x300, is_extended_id=False)
        )


async def test_periodic_frames_are_secured_afresh(secoc_arxml):
    channel = f'secoc-{uuid.uuid4().hex}'
    session = session_with(secoc_arxml, channel)
    session.start()
    ecu = can.Bus(interface='virtual', channel=channel)
    msg = can.Message(arbitration_id=0x300, is_extended_id=False, data=b'\5\0')
    with pytest.raises(SessionError, match='software timing'):
        session.start_periodic(msg, 0.01, hardware=True, prepare=session.decoder.secure)
    periodic = session.start_periodic(msg, 0.01, prepare=session.decoder.secure)
    received = []
    while len(received) < 5:
        frame = await asyncio.to_thread(ecu.recv, 1.0)
        assert frame is not None
        received.append(bytes(frame.data))
    periodic.stop()
    ecu.shutdown()
    session.stop()
    check = verifier()
    assert len(set(received)) == 5  # A new freshness value and MAC in every frame
    assert all(check.check(CONFIG, data).ok for data in received)


async def test_restbus_secures_secured_pdus(secoc_arxml):
    channel = f'secoc-{uuid.uuid4().hex}'
    session = session_with(secoc_arxml, channel)
    session.start()
    database = session.decoder.database
    secured = database.database.get_message_by_name('SecuredStatus')
    plain = database.database.get_message_by_name('EngineData')

    unsecured = RestbusSimulator(session, [secured, plain])  # No secure function: the secured PDU is not sent
    entry = unsecured.get((0x300, False))
    assert entry.secured and not unsecured.get((plain.frame_id, False)).secured
    unsecured.set_enabled((0x300, False), True)
    assert unsecured.start() == ['SecuredStatus: secured PDU (SecOC), but no security manager to secure it']
    unsecured.stop()

    ecu = can.Bus(interface='virtual', channel=channel)
    sim = RestbusSimulator(session, [secured], default_cycle_ms=10, secure=session.decoder.secure)
    sim.set_enabled((0x300, False), True)
    assert sim.start() == []
    frames = []
    while len(frames) < 4:
        frame = await asyncio.to_thread(ecu.recv, 1.0)
        if frame is not None and frame.arbitration_id == 0x300:
            frames.append(bytes(frame.data))
    sim.stop()
    ecu.shutdown()
    session.stop()
    check = verifier()
    assert all(check.check(CONFIG, data).ok for data in frames)

    keyless = session_with(secoc_arxml, channel, SecOcVerifier(StaticSecOcKeys({})))
    keyless.start()
    sim = RestbusSimulator(keyless, [secured], secure=keyless.decoder.secure)
    sim.set_enabled((0x300, False), True)
    [problem] = sim.start()
    assert problem.startswith('SecuredStatus: Cannot secure SecuredStatus: no key')
    sim.stop()
    keyless.stop()


# Test scripts

SCRIPT = """
from can_explorer.testing import TestModule, testcase


class SecOc(TestModule):
    @testcase
    async def ecu_frames_verify(self, t):
        await t.expect_secoc_ok('SecuredStatus', timeout=2)
        t.check(t.secoc_status('SecuredStatus')['ok'] >= 1, 'counted')

    @testcase
    async def sends_secured(self, t):
        t.send('SecuredStatus', AliveCounter=3)
        t.send(0x300, b'\\x01\\x02\\x00\\x00\\x00\\x00')  # Raw bytes: sent as given
        t.send(0x300, b'\\x01\\x02', secure=True)

    @testcase
    async def attacks(self, t):
        t.send('SecuredStatus', AliveCounter=4)
        for attack in ('replay', 'stale_freshness', 'wrong_mac', 'payload_bitflip'):
            t.send_secoc_attack('SecuredStatus', attack)

    @testcase
    async def not_secured(self, t):
        t.send_secoc_attack('EngineData', 'wrong_mac')
"""


async def test_test_scripts(secoc_arxml, tmp_path):
    channel = f'secoc-{uuid.uuid4().hex}'
    session = session_with(secoc_arxml, channel)
    session.start()
    ecu_bus = can.Bus(interface='virtual', channel=channel)
    ecu, sender = verifier(), verifier()
    seen = []
    stop = asyncio.Event()

    async def ecu_task():
        while not stop.is_set():
            data = sender.secure(CONFIG, b'\x07\x00')
            ecu_bus.send(can.Message(arbitration_id=0x300, is_extended_id=False, data=data))
            frame = await asyncio.to_thread(ecu_bus.recv, 0.02)
            while frame is not None:
                if frame.arbitration_id == 0x300:
                    seen.append(ecu.check(CONFIG, bytes(frame.data)))
                frame = await asyncio.to_thread(ecu_bus.recv, 0.0)

    task = asyncio.ensure_future(ecu_task())
    script = tmp_path / 'secoc_tests.py'
    script.write_text(SCRIPT, encoding='utf-8')
    runner = TestRunner(session, lambda: session.decoder.database)
    runner.load(script)
    report = await runner.run()
    await asyncio.sleep(0.2)
    stop.set()
    await task
    ecu_bus.shutdown()
    session.stop()

    results = {result.key: result for result in report.results}
    assert results['SecOc.ecu_frames_verify'].verdict == Verdict.PASSED, results['SecOc.ecu_frames_verify'].steps
    assert results['SecOc.sends_secured'].verdict == Verdict.PASSED
    assert results['SecOc.attacks'].verdict == Verdict.PASSED
    assert results['SecOc.not_secured'].verdict == Verdict.ERROR
    texts = [step.text for step in results['SecOc.sends_secured'].steps]
    assert texts[0].startswith('SecuredStatus (SecOC) [') and texts[1].startswith('0x300 [01 02 00')
    assert texts[2].startswith('0x300 (SecOC) [')
    assert [step.text.split(' [')[0] for step in results['SecOc.attacks'].steps][1:] == [
        'SecuredStatus (SecOC replay)',
        'SecuredStatus (SecOC stale freshness)',
        'SecuredStatus (SecOC wrong mac)',
        'SecuredStatus (SecOC payload bitflip)',
    ]
    # What the ECU made of the tester's frames: the secured ones verify, the raw and manipulated ones do not
    outcomes = [(check.ok, check.describe().split(',')[0]) for check in seen]
    assert outcomes.count((True, 'SecOC ok')) >= 3  # sends_secured (2) and attacks' genuine frame
    assert sum(1 for ok, _ in outcomes if not ok) >= 5  # The raw frame and the four attacks
