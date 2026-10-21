import asyncio
import uuid

import can
import pytest

from can_explorer.core.filters import MessageFilter
from can_explorer.core.gateway import Gateway, GatewayRule
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource


def _session(channel: str) -> BusSession:
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    return session


async def _recv(peer: can.BusABC, timeout: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        msg = peer.recv(0)
        if msg is not None:
            return msg
        await asyncio.sleep(0.01)
    return None


@pytest.fixture
async def buses():
    a_channel, b_channel = f'gw-a-{uuid.uuid4().hex}', f'gw-b-{uuid.uuid4().hex}'
    sessions = {'A': _session(a_channel), 'B': _session(b_channel)}
    peer_a = can.Bus(interface='virtual', channel=a_channel)
    peer_b = can.Bus(interface='virtual', channel=b_channel)
    gateway = Gateway(sessions.get)
    yield gateway, sessions, peer_a, peer_b
    gateway.stop()
    peer_a.shutdown()
    peer_b.shutdown()
    for session in sessions.values():
        session.stop()


def _frame(arbitration_id, data=b'\x00', extended=False):
    return can.Message(arbitration_id=arbitration_id, is_extended_id=extended, data=data)


async def test_forwards_only_matching_frames(buses):
    gateway, _, peer_a, peer_b = buses
    gateway.set_rules([GatewayRule('r', 'A', 'B', MessageFilter.parse('100-1FF'))])
    gateway.start()
    peer_a.send(_frame(0x123, b'\x01\x02'))
    peer_a.send(_frame(0x600, b'\xff'))  # Outside the filter
    msg = await _recv(peer_b)
    assert msg is not None and msg.arbitration_id == 0x123 and bytes(msg.data) == b'\x01\x02'
    assert await _recv(peer_b, 0.3) is None  # The 0x600 frame was not forwarded
    assert (gateway.rules[0].forwarded, gateway.rules[0].dropped) == (1, 0)


async def test_rewrites_identifier(buses):
    gateway, _, peer_a, peer_b = buses
    gateway.set_rules(
        [
            GatewayRule('rewrite', 'A', 'B', MessageFilter.parse('200'), new_id=0x400),
            GatewayRule('offset', 'A', 'B', MessageFilter.parse('201'), id_offset=0x10),
        ]
    )
    gateway.start()
    peer_a.send(_frame(0x200, b'\x09'))
    peer_a.send(_frame(0x201, b'\x0a'))
    seen = {}
    for _ in range(2):
        msg = await _recv(peer_b)
        assert msg is not None
        seen[msg.arbitration_id] = bytes(msg.data)
    assert seen == {0x400: b'\x09', 0x211: b'\x0a'}


async def test_rate_limits_each_identifier(buses):
    gateway, _, peer_a, peer_b = buses
    gateway.set_rules([GatewayRule('slow', 'A', 'B', min_interval=10.0)])  # At most one per 10 s per id
    gateway.start()
    for _ in range(3):
        peer_a.send(_frame(0x111))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    forwarded = 0
    while await _recv(peer_b, 0.1) is not None:
        forwarded += 1
    assert forwarded == 1
    assert (gateway.rules[0].forwarded, gateway.rules[0].dropped) == (1, 2)


async def test_mirror_rules_do_not_loop(buses):
    gateway, _, peer_a, peer_b = buses
    gateway.set_rules([GatewayRule('a2b', 'A', 'B'), GatewayRule('b2a', 'B', 'A')])
    gateway.start()
    peer_a.send(_frame(0x321, b'\x11'))
    msg = await _recv(peer_b)
    assert msg is not None and msg.arbitration_id == 0x321
    # The forwarded frame is transmitted (not received) on B, so the B→A rule does not forward it back
    assert await _recv(peer_a, 0.3) is None
    await asyncio.sleep(0.1)
    assert gateway.rules[0].forwarded == 1 and gateway.rules[1].forwarded == 0


async def test_disabled_rule_forwards_nothing(buses):
    gateway, _, peer_a, peer_b = buses
    gateway.set_rules([GatewayRule('off', 'A', 'B', enabled=False)])
    gateway.start()
    peer_a.send(_frame(0x100))
    assert await _recv(peer_b, 0.3) is None


async def test_dropped_when_destination_is_closed(buses):
    gateway, sessions, peer_a, _ = buses
    gateway.set_rules([GatewayRule('r', 'A', 'B')])
    gateway.start()
    sessions['B'].stop()
    peer_a.send(_frame(0x100))
    await asyncio.sleep(0.1)
    assert gateway.rules[0].dropped >= 1 and gateway.rules[0].forwarded == 0


async def test_unresolved_source_is_skipped():
    gateway = Gateway(lambda key: None)  # Nothing resolves
    gateway.set_rules([GatewayRule('r', 'X', 'Y')])
    gateway.start()  # Must not raise
    assert gateway.running
    gateway.stop()


def test_echo_guard_consumes_reflected_frames():
    gateway = Gateway(lambda key: None)
    session = object()
    msg = _frame(0x1, b'\x01')
    gateway._record_send(session, msg)
    assert gateway._is_echo(session, msg) is True  # Recognised as our own injected frame
    assert gateway._is_echo(session, msg) is False  # Consumed; a genuine later frame is not swallowed
