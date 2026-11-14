import threading

import pytest
from can.exceptions import CanInterfaceNotImplementedError

from can_explorer.core import channel_detection
from can_explorer.core.channel_detection import (
    ERROR,
    FOUND,
    NONE,
    TIMEOUT,
    UNAVAILABLE,
    UNSUPPORTED,
    BackendProbe,
    configs_of,
    detect_channels,
    probe_backend,
)


def test_probe_a_backend_that_finds_channels():
    probe = probe_backend('virtual')
    assert probe.status in (FOUND, NONE) and probe.interface == 'virtual'
    assert all(config['interface'] == 'virtual' for config in probe.configs)


def test_probe_an_unknown_backend():
    probe = probe_backend('no-such-backend')
    assert probe.status == UNAVAILABLE and 'no-such-backend' in probe.detail


class _Bus:
    configs = []
    error = None

    @classmethod
    def _detect_available_configs(cls):
        if cls.error is not None:
            raise cls.error
        return [dict(config) for config in cls.configs]


@pytest.mark.parametrize(
    'load_error, detect_error, configs, status, detail',
    [
        (CanInterfaceNotImplementedError('vxlapi64 not found'), None, [], UNAVAILABLE, 'vxlapi64 not found'),
        (OSError('cannot load etas.dll'), None, [], UNAVAILABLE, "OSError('cannot load etas.dll')"),
        (None, NotImplementedError(), [], UNSUPPORTED, 'does not support channel detection'),
        (None, RuntimeError('CSI_CreateProtocolTree failed'), [], ERROR, 'CSI_CreateProtocolTree failed'),
        (None, None, [], NONE, ''),
        (None, None, [{'channel': 'ETAS://USB/SuperCAN:3c04469d/CAN:0'}], FOUND, ''),
    ],
)
def test_probe_outcomes(monkeypatch, load_error, detect_error, configs, status, detail):
    def load(interface):
        if load_error is not None:
            raise load_error
        return type('Bus', (_Bus,), {'configs': configs, 'error': detect_error})

    monkeypatch.setattr(channel_detection, '_get_class_for_interface', load)
    probe = probe_backend('etas')
    assert probe.status == status and detail in probe.detail
    assert probe.configs == [dict(config, interface='etas') for config in configs]


def _fake(outcomes):
    """A probe returning the next outcome per interface: a status, or an Event to block on (a hanging driver)."""
    calls = []

    def probe(interface):
        calls.append(interface)
        outcome = outcomes[interface].pop(0)
        if isinstance(outcome, threading.Event):
            outcome.wait(5)
            return BackendProbe(interface, NONE)
        status, detail = outcome
        configs = [{'interface': interface, 'channel': f'{interface}0'}] if status == FOUND else []
        return BackendProbe(interface, status, configs, detail)

    return probe, calls


def test_failed_backends_are_retried_alone():
    probe, calls = _fake(
        {
            'etas': [(ERROR, 'OSError(access violation)'), (FOUND, '')],
            'kvaser': [(UNAVAILABLE, 'canlib32 not found'), (UNAVAILABLE, 'canlib32 not found')],
            'pcan': [(NONE, '')],
            'slcan': [(UNSUPPORTED, 'does not support channel detection')],
        }
    )
    etas, kvaser, pcan, slcan = detect_channels(['pcan', 'etas', 'kvaser', 'slcan'], timeout=2, probe=probe)
    assert (etas.status, etas.alone, etas.detail) == (
        FOUND,
        True,
        'failed alongside the other backends: OSError(access violation)',
    )
    assert (kvaser.status, kvaser.alone, kvaser.detail) == (UNAVAILABLE, True, 'canlib32 not found')
    assert (pcan.status, pcan.alone) == (NONE, False) and (slcan.status, slcan.alone) == (UNSUPPORTED, False)
    assert sorted(calls) == ['etas', 'etas', 'kvaser', 'kvaser', 'pcan', 'slcan']  # Only failures retried
    assert configs_of([etas, kvaser, pcan, slcan]) == [{'interface': 'etas', 'channel': 'etas0'}]


def test_no_retry_when_disabled():
    probe, calls = _fake({'etas': [(ERROR, 'boom')]})
    [etas] = detect_channels(['etas'], timeout=2, probe=probe, retry_alone=False)
    assert etas.status == ERROR and not etas.alone and calls == ['etas']


def test_a_hanging_backend_times_out_without_losing_the_others():
    hang = threading.Event()
    probe, _ = _fake({'neovi': [hang], 'pcan': [(FOUND, '')]})
    try:
        neovi, pcan = detect_channels(['neovi', 'pcan'], timeout=0.2, probe=probe)
    finally:
        hang.set()
    assert neovi.status == TIMEOUT and neovi.seconds == 0.2
    assert pcan.status == FOUND


def test_retries_stop_at_their_deadline():
    hang = threading.Event()
    probe, calls = _fake({'etas': [(ERROR, 'first'), hang], 'kvaser': [(ERROR, 'first')]})
    try:
        etas, kvaser = detect_channels(['etas', 'kvaser'], timeout=0.2, probe=probe)
    finally:
        hang.set()
    assert (etas.status, etas.alone) == (TIMEOUT, True)
    assert (kvaser.status, kvaser.alone, kvaser.detail) == (ERROR, False, 'first')  # No time left to retry it
    assert calls.count('kvaser') == 1


def test_a_timed_out_retry_ends_the_retries_on_a_coarse_clock(monkeypatch):
    # On Windows the clock may not have moved when a retry's wait has timed out: no time is left all the same
    monkeypatch.setattr(channel_detection.time, 'monotonic', lambda: 1000.0)
    hang = threading.Event()
    probe, calls = _fake({'etas': [(ERROR, 'first'), hang], 'kvaser': [(ERROR, 'first')]})
    try:
        etas, kvaser = detect_channels(['etas', 'kvaser'], timeout=0.2, probe=probe)
    finally:
        hang.set()
    assert (etas.status, etas.alone) == (TIMEOUT, True)
    assert (kvaser.status, kvaser.alone, kvaser.detail) == (ERROR, False, 'first')
    assert calls.count('kvaser') == 1


def test_a_retry_that_raises_is_an_error():
    def probe(interface):
        if calls.append(interface) or len(calls) > 1:
            raise RuntimeError('unexpected')
        return BackendProbe(interface, ERROR, detail='first')

    calls = []
    [result] = detect_channels(['etas'], timeout=1, probe=probe)
    assert (result.status, result.alone) == (ERROR, True) and 'unexpected' in result.detail


def test_a_probe_that_raises_is_an_error():
    def probe(interface):
        raise RuntimeError('unexpected')

    [result] = detect_channels(['etas'], timeout=1, probe=probe, retry_alone=False)
    assert result.status == ERROR and 'unexpected' in result.detail


def test_summaries():
    found = BackendProbe('etas', FOUND, [{'channel': 'ETAS://USB/CAN:0'}, {'channel': 'ETAS://USB/CAN:1'}], seconds=0.5)
    assert found.summary() == 'etas: 2 channel(s) in 0.50 s: ETAS://USB/CAN:0, ETAS://USB/CAN:1'
    assert BackendProbe('pcan', NONE, seconds=0.1).summary() == 'pcan: no channels (0.10 s)'
    assert BackendProbe('neovi', TIMEOUT, seconds=15).summary() == 'neovi: timed out after 15.00 s'
    error = BackendProbe('etas', ERROR, detail='OSError(5)', seconds=0.3, alone=True)
    assert error.summary() == 'etas: error after 0.30 s: OSError(5) [probed alone]'
