"""Security event monitoring of a bus: the frames that suggest an attack or a misbehaving node, collected as events.

A `SecurityMonitor` watches the received frames of a session (after its decoder checked them) and records:

- **SecOC failures** — MAC and freshness (replay) errors of secured PDUs (core/secoc_check.py).
- **E2E errors** — CRC errors of the E2E-protected signal PDUs of an ARXML (core/e2e_check.py).
- **Unknown identifiers** — frames whose identifier the signal database does not define (only with a database).
- **Frames too fast** — a database message arriving much sooner than its cycle time: a sign of injected frames
  competing with the real sender.
- **Refused access** — negative responses refusing security access or authentication (NRCs securityAccessDenied,
  invalidKey, exceedNumberOfAttempts, requiredTimeDelayNotExpired, authenticationRequired and the Authentication
  NRCs 0x50-0x5D) in single-frame ISO-TP responses; repeated ones suggest someone trying keys or certificates.

Events of the same kind and identifier are aggregated (count, first and last time); an event becomes *high*
severity when it repeats `burst` times within `window` seconds. This is a monitor for test benches and analysis,
not an intrusion detection system in the AUTOSAR IdsM sense: it reports what it sees, with no response.
"""

import csv
import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import can

from can_explorer.core import uds_auth
from can_explorer.core.decoding import contained_parts
from can_explorer.core.e2e_check import e2e_config_of

SECURITY_NRCS = {
    0x33: 'securityAccessDenied',
    0x35: 'invalidKey',
    0x36: 'exceedNumberOfAttempts',
    0x37: 'requiredTimeDelayNotExpired',
    **uds_auth.NEGATIVE_RESPONSE_CODES,
}
KINDS = {
    'secoc': 'SecOC verification failed',
    'e2e': 'E2E check failed',
    'unknown': 'Unknown identifier',
    'too-fast': 'Frames faster than their cycle time',
    'refused': 'Security access or authentication refused',
    # Ethernet captures
    'decrypt': 'IPsec or MACsec integrity check failed',
    'tls': 'TLS record failed to decrypt',
    'doip-refused': 'DoIP routing activation refused',
    'unknown-service': 'SOME/IP service not offered',
}
SEVERITIES = {
    'secoc': 'high',
    'e2e': 'high',
    'decrypt': 'high',
    'tls': 'high',
    'unknown': 'medium',
    'too-fast': 'medium',
    'refused': 'medium',
    'doip-refused': 'medium',
    'unknown-service': 'medium',
}


@dataclass
class SecurityEvent:
    kind: str
    identifier: str  # Hex identifier ('123', '18DAF110x')
    detail: str
    severity: str  # 'low', 'medium' or 'high'
    first: float
    last: float
    count: int = 1
    _recent: Deque[float] = field(default_factory=deque, repr=False)

    def as_dict(self, start: float = 0.0) -> Dict[str, object]:
        data = {key: value for key, value in asdict(self).items() if not key.startswith('_')}
        data['title'] = KINDS.get(self.kind, self.kind)
        data['first'] = round(self.first - start, 6)
        data['last'] = round(self.last - start, 6)
        return data


def _identifier(msg: can.Message) -> str:
    return f'{msg.arbitration_id:08X}x' if msg.is_extended_id else f'{msg.arbitration_id:03X}'


class EventCollector:
    """Security events aggregated per kind and identifier, escalated to high severity when they come in bursts."""

    def __init__(self, window: float = 1.0, burst: int = 3):
        self.window, self.burst = window, burst
        self.events: Dict[Tuple[str, str, str], SecurityEvent] = {}
        self.start: Optional[float] = None
        self.enabled = {kind: True for kind in KINDS}

    def clear(self) -> None:
        self.events.clear()
        self.start = None

    def add(
        self, kind: str, identifier: str, detail: str, now: float, severity: str = '', key_detail: str = ''
    ) -> None:
        if not self.enabled.get(kind, True):
            return
        if self.start is None:
            self.start = now
        key = (kind, identifier, key_detail)
        event = self.events.get(key)
        if event is None:
            severity = severity or SEVERITIES.get(kind, 'medium')
            event = self.events[key] = SecurityEvent(kind, identifier, detail, severity, now, now)
        else:
            event.count += 1
            event.last, event.detail = now, detail
        event._recent.append(now)
        while event._recent and now - event._recent[0] > self.window:
            event._recent.popleft()
        if len(event._recent) >= self.burst:
            event.severity = 'high'

    def rows(self) -> List[Dict[str, object]]:
        """The events, most recent first, with times relative to the first frame seen."""
        start = self.start or 0.0
        events = sorted(self.events.values(), key=lambda event: event.last, reverse=True)
        return [event.as_dict(start) for event in events]

    def export(self, path: Path) -> int:
        """Write the events to a CSV or JSON file (by its suffix). Returns how many."""
        rows = self.rows()
        path = Path(path)
        if path.suffix.lower() == '.json':
            path.write_text(json.dumps({'events': rows}, indent=2) + '\n', encoding='utf-8')
        else:
            columns = ['severity', 'title', 'kind', 'identifier', 'count', 'first', 'last', 'detail']
            with path.open('w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
        return len(rows)


class SecurityMonitor(EventCollector):
    """Collects security events from the frames of a session. Call `on_message` with each received frame after
    the session's FrameDecoder described it (a session message listener does this)."""

    def __init__(self, decoder, window: float = 1.0, burst: int = 3, too_fast_factor: float = 0.5):
        super().__init__(window, burst)
        self.decoder = decoder
        self.too_fast_factor = too_fast_factor
        self._last_seen: Dict[Tuple[int, bool], float] = {}

    def clear(self) -> None:
        super().clear()
        self._last_seen.clear()

    def _record(self, kind: str, msg: can.Message, detail: str, severity: str, key_detail: str = '') -> None:
        self.add(kind, _identifier(msg), detail, msg.timestamp, severity, key_detail)

    def on_message(self, msg: can.Message) -> None:
        if msg.is_error_frame or not getattr(msg, 'is_rx', True):
            return  # Error frames are bus health; own frames are not attacks on us
        if self.start is None:
            self.start = msg.timestamp  # Times are relative to the first frame seen
        self._check_secoc(msg)
        database = self.decoder.database
        definition = database.message_definition(msg) if hasattr(database, 'message_definition') else None
        if database is not None and hasattr(database, 'message_definition') and not msg.is_remote_frame:
            if definition is None:
                self._record('unknown', msg, 'not in the signal database', 'medium')
            else:
                self._check_e2e(msg, definition)
                self._check_cycle(msg, definition)
        self._check_refusal(msg)

    def _check_secoc(self, msg: can.Message) -> None:
        result = self.decoder.secoc_result(msg) if hasattr(self.decoder, 'secoc_result') else None
        if result is not None and result.checked and not result.ok:
            self._record('secoc', msg, result.describe(), 'high')

    def _check_e2e(self, msg: can.Message, definition) -> None:
        if getattr(definition, 'is_container', False):  # The protected PDUs inside a container frame (not LIN)
            parts = [
                (contained, raw, f'{contained.name}: ') for contained, raw in contained_parts(definition, msg.data)
            ]
        else:
            parts = [(definition, bytes(msg.data), '')]
        for message, data, prefix in parts:
            config = e2e_config_of(message)
            if config is None or config.problem:
                continue
            check = config.check(data)
            if check.checked and not check.ok:
                self._record('e2e', msg, prefix + check.describe(config.label), 'high', key_detail=prefix)

    def _check_cycle(self, msg: can.Message, definition) -> None:
        key = (msg.arbitration_id, msg.is_extended_id)
        previous = self._last_seen.get(key)
        self._last_seen[key] = msg.timestamp
        cycle = getattr(definition, 'cycle_time', None)
        if not cycle or previous is None:
            return
        interval_ms = (msg.timestamp - previous) * 1000
        if interval_ms < cycle * self.too_fast_factor:
            detail = f'{interval_ms:.1f} ms after the previous frame, cycle time {cycle} ms ({definition.name})'
            self._record('too-fast', msg, detail, 'medium')

    def _check_refusal(self, msg: can.Message) -> None:
        data = bytes(msg.data)
        # A single-frame ISO-TP negative response: length nibble, 0x7F, the service, the NRC
        if len(data) >= 4 and data[0] >> 4 == 0 and 3 <= (data[0] & 0x0F) <= 7 and data[1] == 0x7F:
            service, code = data[2], data[3]
            name = SECURITY_NRCS.get(code)
            if name is not None:
                from can_explorer.core.uds import service_name

                detail = f'{service_name(service)} refused: {name} (NRC 0x{code:02X})'
                self._record('refused', msg, detail, 'medium', key_detail=f'{service:02X}{code:02X}')


class EthernetSecurityMonitor(EventCollector):
    """Collects security events from the packets of an Ethernet capture: the findings the decoder recorded on each
    packet (core/ethernet.py): SOME/IP E2E failures, IPsec/MACsec integrity failures, TLS records that fail to
    decrypt, refused DoIP routing activations, UDS refusals over DoIP, and SOME/IP services not offered by
    Service Discovery."""

    def on_packet(self, packet) -> None:
        if self.start is None:
            self.start = packet.timestamp  # Times are relative to the first packet seen
        for kind, identifier, detail in getattr(packet, 'security', ()):
            self.add(kind, identifier, detail, packet.timestamp, key_detail=detail if kind == 'refused' else '')
