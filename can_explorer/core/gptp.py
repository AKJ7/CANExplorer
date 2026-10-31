"""gPTP / IEEE 802.1AS (generalized Precision Time Protocol) decoding and a time-sync summary.

Parses the PTP common header and the message bodies used in automotive time sync, and aggregates a capture into
the sync picture an engineer wants: the grandmaster and its quality, the clocks (ports) seen, the configured and
observed Sync rate, and the peer-delay (Pdelay) activity. Qt-free; reused by the Ethernet capture tool.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MESSAGE_TYPES = {
    0x0: 'Sync',
    0x1: 'Delay_Req',
    0x2: 'Pdelay_Req',
    0x3: 'Pdelay_Resp',
    0x8: 'Follow_Up',
    0x9: 'Delay_Resp',
    0xA: 'Pdelay_Resp_Follow_Up',
    0xB: 'Announce',
    0xC: 'Signaling',
    0xD: 'Management',
}
CLOCK_CLASSES = {6: 'synchronized to a primary reference', 7: 'holdover', 248: 'default', 255: 'slave-only'}
TIME_SOURCES = {0x10: 'atomic clock', 0x20: 'GPS', 0x30: 'terrestrial radio', 0x40: 'PTP', 0x60: 'internal oscillator'}
HEADER_SIZE = 34


@dataclass
class GptpMessage:
    message_type: int
    type: str
    domain: int
    source_clock: str  # Clock identity, e.g. 'aa:bb:cc:ff:fe:dd:ee:ff'
    source_port: int
    sequence: int
    log_interval: int  # log2 of the message interval in seconds
    correction_ns: float
    timestamp: float = 0.0  # Capture time, set by the caller
    # Announce only
    grandmaster: str = ''
    gm_priority1: int = 0
    gm_priority2: int = 0
    gm_clock_class: int = 0
    gm_clock_accuracy: int = 0
    steps_removed: int = 0
    time_source: int = 0

    @property
    def interval_ms(self) -> Optional[float]:
        return 2.0**self.log_interval * 1000.0 if -16 <= self.log_interval <= 16 else None


def _clock_identity(data: bytes) -> str:
    return ':'.join(f'{byte:02x}' for byte in data)


def parse(payload: bytes) -> Optional[GptpMessage]:
    """Parse a gPTP/PTP message (the Ethernet payload after the 0x88F7 ethertype). Returns None if too short."""
    if len(payload) < HEADER_SIZE:
        return None
    message_type = payload[0] & 0x0F
    domain = payload[4]
    correction = int.from_bytes(payload[8:16], 'big', signed=True) / 65536.0  # Scaled nanoseconds
    source_clock = _clock_identity(payload[20:28])
    source_port = int.from_bytes(payload[28:30], 'big')
    sequence = int.from_bytes(payload[30:32], 'big')
    log_interval = int.from_bytes(payload[33:34], 'big', signed=True)
    message = GptpMessage(
        message_type=message_type,
        type=MESSAGE_TYPES.get(message_type, f'type {message_type}'),
        domain=domain,
        source_clock=source_clock,
        source_port=source_port,
        sequence=sequence,
        log_interval=log_interval,
        correction_ns=round(correction, 3),
    )
    if message_type == 0xB and len(payload) >= 64:  # Announce
        message.gm_priority1 = payload[47]
        message.gm_clock_class = payload[48]
        message.gm_clock_accuracy = payload[49]
        message.gm_priority2 = payload[52]
        message.grandmaster = _clock_identity(payload[53:61])
        message.steps_removed = int.from_bytes(payload[61:63], 'big')
        message.time_source = payload[63]
    return message


def describe(message: GptpMessage) -> str:
    """A one-line summary for the packet list."""
    domain = f'domain {message.domain} ' if message.domain else ''
    if message.message_type == 0xB:  # Announce
        short = message.grandmaster.rsplit(':', 3)[-1] if message.grandmaster else '?'
        return f'{domain}Announce GM …{short} prio {message.gm_priority1}/{message.gm_priority2} class {message.gm_clock_class} steps {message.steps_removed}'
    return f'{domain}{message.type} seq {message.sequence}'


@dataclass
class GptpSummary:
    grandmaster: str = ''
    gm_priority1: int = 0
    gm_clock_class: int = 0
    gm_time_source: int = 0
    steps_removed: int = 0
    domains: List[int] = field(default_factory=list)
    clocks: List[str] = field(default_factory=list)
    sync_interval_ms: Optional[float] = None
    announce_interval_ms: Optional[float] = None
    sync_rate_hz: Optional[float] = None
    counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'grandmaster': self.grandmaster,
            'gm_priority1': self.gm_priority1,
            'gm_clock_class': self.gm_clock_class,
            'gm_clock_class_name': CLOCK_CLASSES.get(self.gm_clock_class, ''),
            'gm_time_source': TIME_SOURCES.get(self.gm_time_source, ''),
            'steps_removed': self.steps_removed,
            'domains': self.domains,
            'clocks': self.clocks,
            'sync_interval_ms': self.sync_interval_ms,
            'announce_interval_ms': self.announce_interval_ms,
            'sync_rate_hz': self.sync_rate_hz,
            'counts': self.counts,
        }


def summarize(messages: List[GptpMessage]) -> GptpSummary:
    """Aggregate gPTP messages (with capture timestamps) into a time-sync summary."""
    summary = GptpSummary()
    if not messages:
        return summary
    for message in messages:
        summary.counts[message.type] = summary.counts.get(message.type, 0) + 1
    summary.domains = sorted({message.domain for message in messages})
    summary.clocks = sorted({message.source_clock for message in messages})

    announces = [message for message in messages if message.message_type == 0xB]
    if announces:
        best = min(announces, key=lambda message: (message.gm_priority1, message.gm_clock_class, message.steps_removed))
        summary.grandmaster = best.grandmaster
        summary.gm_priority1 = best.gm_priority1
        summary.gm_clock_class = best.gm_clock_class
        summary.gm_time_source = best.time_source
        summary.steps_removed = best.steps_removed
        summary.announce_interval_ms = best.interval_ms

    syncs = [message for message in messages if message.message_type == 0x0]
    if syncs:
        summary.sync_interval_ms = syncs[-1].interval_ms
        span = syncs[-1].timestamp - syncs[0].timestamp
        if span > 0 and len(syncs) > 1:
            summary.sync_rate_hz = round((len(syncs) - 1) / span, 3)
    return summary
