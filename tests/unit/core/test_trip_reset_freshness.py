"""The AUTOSAR trip/reset freshness value (SWS SecOC R23-11, Annex A §11.4): a master's synchronisation message, a
sender and a receiver, each with its own freshness value manager."""

import pytest

from can_explorer.core.secoc_check import (
    SecOcConfig,
    SecOcError,
    SecOcVerifier,
    StaticSecOcKeys,
    TripResetConfig,
    TripResetFreshness,
)

KEYS = StaticSecOcKeys({'*': (bytes(range(16)), 'cmac-aes')})
COUNTERS = TripResetConfig(trip_bits=16, reset_bits=20, message_bits=24, flag_bits=2, sync_pdu='TripResetSyncMsg')
# A data PDU sending 4 message counter bits and the 2-bit reset flag; the sync message with both counters in full
DATA = SecOcConfig('Speed', 0x10, 2, 24, COUNTERS.full_bits, 6, 'CMAC')
SYNC = SecOcConfig('TripResetSyncMsg', 0x20, 0, 32, 36, 36, 'CMAC')


def node(config=COUNTERS) -> SecOcVerifier:
    return SecOcVerifier(KEYS, TripResetFreshness(config))


def sync(master: SecOcVerifier, trip: int, reset: int) -> bytes:
    return master.secure(SYNC, b'', (trip << 20) | reset)


def test_layout():
    manager = TripResetFreshness(COUNTERS)
    value = manager.compose(trip=3, reset=5, message=7)
    assert value == (((3 << 20 | 5) << 24 | 7) << 2) | (5 & 3)  # trip | reset | message | reset flag
    assert manager.decompose(value) == (3, 5, 7, 1)
    with pytest.raises(SecOcError, match='at most 64 bits'):
        TripResetConfig(trip_bits=24, reset_bits=24, message_bits=48, flag_bits=2)


def test_sender_and_receiver_stay_in_step():
    master, sender, receiver = node(), node(), node()
    for verifier in (sender, receiver):
        assert verifier.check(SYNC, sync(master, 1, 1)).ok
    assert receiver.freshness.latest == (1, 1) and receiver.freshness.synchronised
    for number in range(1, 41):  # The 4 transmitted message counter bits wrap twice
        frame = sender.secure(DATA, b'\x12\x34')
        check = receiver.check(DATA, frame)
        assert check.ok, (number, check.describe())
        assert receiver.freshness.decompose(check.freshness)[:3] == (1, 1, number)
    # The master moves the reset counter on: the message counter starts at 1 again (table 8), format 3 on reception
    for verifier in (sender, receiver):
        assert verifier.check(SYNC, sync(master, 1, 2)).ok
    first = sender.secure(DATA, b'\x12\x34')
    assert sender.freshness._sent[0x10] == (1, 2, 1)  # Trip, reset and message counter sent
    check = receiver.check(DATA, first)
    assert check.ok and receiver.freshness.decompose(check.freshness)[:3] == (1, 2, 1)


def test_receiver_one_sync_behind_or_ahead():
    master, sender, receiver = node(), node(), node()
    for verifier in (sender, receiver):
        verifier.check(SYNC, sync(master, 1, 1))
    assert receiver.check(DATA, sender.secure(DATA, b'\x00\x01')).ok
    sender.check(SYNC, sync(master, 1, 2))  # The receiver misses this one: its latest reset counter lags by 1
    check = receiver.check(DATA, sender.secure(DATA, b'\x00\x02'))
    assert check.ok and receiver.freshness.decompose(check.freshness)[:2] == (1, 2)  # Found with X = +1


def test_replays_and_forgeries_fail():
    master, sender, receiver = node(), node(), node()
    for verifier in (sender, receiver):
        verifier.check(SYNC, sync(master, 1, 1))
    frame = sender.secure(DATA, b'\x00\x01')
    assert receiver.check(DATA, frame).ok
    assert not receiver.check(DATA, frame).ok  # A replayed frame: the message counter has moved on
    assert not receiver.check(DATA, bytes([frame[0] ^ 1]) + frame[1:]).ok
    # An old synchronisation message is a replay too
    receiver.check(SYNC, sync(master, 1, 3))
    stale = receiver.check(SYNC, sync(master, 1, 2))
    assert not stale.ok and stale.describe().startswith('SecOC FRESHNESS ERROR, FV')
    assert receiver.freshness.latest == (1, 3)


def test_no_candidate_and_wrong_geometry():
    receiver = node()
    receiver.check(SYNC, sync(node(), 1, 1))  # Latest reset counter 1: flags 1, 0, 2, 3 (X = 0, -1, +1, +2)
    frame = node().secure(DATA, b'\x00\x01', TripResetFreshness(COUNTERS).compose(9, 9, 1))
    assert not receiver.check(DATA, frame).ok
    other = SecOcConfig('Other', 0x30, 2, 24, 32, 6, 'CMAC')
    assert 'trip/reset counters make 62 bits' in receiver.check(other, bytes(8)).reason


def test_trip_counter_wrap_within_the_clear_acceptance_window():
    counters = TripResetConfig(
        trip_bits=4, reset_bits=4, message_bits=8, flag_bits=2, sync_pdu='Sync', clear_acceptance_window=1
    )
    sync_config = SecOcConfig('Sync', 0x20, 0, 32, 8, 8, 'CMAC')
    master, receiver = node(counters), node(counters)
    assert receiver.check(sync_config, master.secure(sync_config, b'', (15 << 4) | 3)).ok  # Trip counter at its maximum
    wrapped = receiver.check(sync_config, master.secure(sync_config, b'', (1 << 4) | 1))
    assert wrapped.ok and receiver.freshness.latest == (1, 1)  # Back to the initial value: accepted
    assert not receiver.check(sync_config, master.secure(sync_config, b'', (1 << 4) | 0)).ok  # Older again: a replay
