"""A CAN bus gateway: forward frames received on one bus onto another, with filtering, identifier rewriting and
rate limiting. Each rule bridges a source session to a destination session.

The engine listens on the source sessions and, for every *received* frame (not ones the application transmits),
sends a copy on the destination session of each matching rule. Only received frames are forwarded, so a frame the
gateway injects on a bus is not forwarded again; a short echo guard additionally ignores an injected frame that a
bus with `receive_own_messages` reflects back, so two mirror rules between the same pair of buses cannot loop.

The core works over `BusSession` objects resolved by a key (the GUI uses connection names), so it is driven from a
test with virtual buses without any GUI.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

import can

from can_explorer.core.filters import MessageFilter
from can_explorer.core.session import BusSession, SessionError

# Seconds for which an injected frame is remembered, to recognise it when a bus reflects it back (own reception)
ECHO_WINDOW = 0.25

_FrameKey = Tuple[int, bool, bytes]


@dataclass
class GatewayRule:
    """One forwarding rule: received frames on `source` matching `message_filter` are sent on `destination`.

    `new_id` rewrites the identifier (useful with a single-identifier filter); otherwise `id_offset` is added to it.
    `min_interval` rate-limits each source identifier to at most one frame per that many seconds (0: no limit).
    """

    name: str
    source: str
    destination: str
    message_filter: MessageFilter = field(default_factory=MessageFilter)
    new_id: Optional[int] = None
    id_offset: int = 0
    min_interval: float = 0.0
    enabled: bool = True
    forwarded: int = 0
    dropped: int = 0
    _last_sent: Dict[int, float] = field(default_factory=dict, repr=False)

    def matches(self, msg: can.Message) -> bool:
        return self.enabled and self.message_filter.matches(msg)

    def target_id(self, msg: can.Message) -> Tuple[int, bool]:
        if self.new_id is not None:
            return self.new_id, self.new_id > 0x7FF
        mask = 0x1FFFFFFF if msg.is_extended_id else 0x7FF
        return (msg.arbitration_id + self.id_offset) & mask, msg.is_extended_id

    def build(self, msg: can.Message) -> can.Message:
        arbitration_id, extended = self.target_id(msg)
        return can.Message(
            arbitration_id=arbitration_id,
            is_extended_id=extended,
            is_fd=msg.is_fd,
            bitrate_switch=msg.bitrate_switch,
            dlc=msg.dlc,
            data=bytes(msg.data),
        )

    def reset_counters(self) -> None:
        self.forwarded = self.dropped = 0
        self._last_sent.clear()


class Gateway:
    """Forwards frames between sessions according to its rules. `resolve` maps a rule's source/destination key to a
    `BusSession`, or None when it is not available (e.g. a connection that was removed)."""

    def __init__(
        self,
        resolve: Callable[[str], Optional[BusSession]],
        on_forward: Optional[Callable[[GatewayRule, can.Message], None]] = None,
    ):
        self._resolve = resolve
        self._on_forward = on_forward
        self._rules: List[GatewayRule] = []
        self._running = False
        self._listeners: Dict[str, Tuple[BusSession, Callable[[can.Message], None]]] = {}
        self._recent: Dict[int, Deque[Tuple[float, _FrameKey]]] = {}  # id(session) -> injected frames, for echoes

    @property
    def running(self) -> bool:
        return self._running

    @property
    def rules(self) -> List[GatewayRule]:
        return self._rules

    def set_rules(self, rules: List[GatewayRule]) -> None:
        self._rules = list(rules)
        if self._running:
            self._sync_listeners()

    def start(self) -> None:
        self._running = True
        self._sync_listeners()

    def stop(self) -> None:
        for session, listener in self._listeners.values():
            session.remove_message_listener(listener)
        self._listeners.clear()
        self._recent.clear()
        self._running = False

    def refresh(self) -> None:
        """Re-resolve the sessions, e.g. after connections were added, removed or reopened (new session objects)."""
        if not self._running:
            return
        for session, listener in self._listeners.values():
            session.remove_message_listener(listener)
        self._listeners.clear()
        self._sync_listeners()

    def _sync_listeners(self) -> None:
        wanted = {rule.source for rule in self._rules if rule.enabled}
        for key in list(self._listeners):
            if key not in wanted:
                session, listener = self._listeners.pop(key)
                session.remove_message_listener(listener)
        for key in wanted:
            if key in self._listeners:
                continue
            session = self._resolve(key)
            if session is None:
                continue
            listener = self._make_listener(key)
            session.add_message_listener(listener)
            self._listeners[key] = (session, listener)

    def _make_listener(self, source_key: str) -> Callable[[can.Message], None]:
        def listener(msg: can.Message) -> None:
            if msg.is_rx:  # Forward only received frames, never ones the gateway or the app transmitted
                self._on_frame(source_key, msg)

        return listener

    def _on_frame(self, source_key: str, msg: can.Message) -> None:
        attached = self._listeners.get(source_key)
        if attached is not None and self._is_echo(attached[0], msg):
            return
        now = time.monotonic()
        for rule in self._rules:
            if rule.source != source_key or not rule.matches(msg):
                continue
            if rule.min_interval > 0:
                last = rule._last_sent.get(msg.arbitration_id)
                if last is not None and now - last < rule.min_interval:
                    rule.dropped += 1
                    continue
            destination = self._resolve(rule.destination)
            if destination is None or not destination.is_open:
                rule.dropped += 1
                continue
            out = rule.build(msg)
            try:
                destination.send(out)
            except SessionError:
                rule.dropped += 1
                continue
            rule._last_sent[msg.arbitration_id] = now
            rule.forwarded += 1
            self._record_send(destination, out)
            if self._on_forward is not None:
                self._on_forward(rule, out)

    # Echo guard

    @staticmethod
    def _key(msg: can.Message) -> _FrameKey:
        return msg.arbitration_id, msg.is_extended_id, bytes(msg.data)

    def _record_send(self, session: BusSession, msg: can.Message) -> None:
        self._recent.setdefault(id(session), deque()).append((time.monotonic() + ECHO_WINDOW, self._key(msg)))

    def _is_echo(self, session: BusSession, msg: can.Message) -> bool:
        queue = self._recent.get(id(session))
        if not queue:
            return False
        now = time.monotonic()
        while queue and queue[0][0] < now:
            queue.popleft()
        key = self._key(msg)
        for index, (_, remembered) in enumerate(queue):
            if remembered == key:
                del queue[index]
                return True
        return False
