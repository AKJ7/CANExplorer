"""ResponseOnEvent (UDS 0x86): ask an ECU to send a service's response by itself when an event occurs — a DTC
status change, a change of a data identifier, a comparison of values.

Request: eventType (bit 6 storageState: keep the event over a power cycle; bit 7 suppresses the positive response),
eventWindowTime, an eventTypeRecord whose length depends on the event type, then serviceToRespondToRecord: the
request whose response the ECU sends on the event (e.g. `22 F1 90`, or `19 0E` for the most recent confirmed DTC).
The setup response echoes eventType, then numberOfIdentifiedEvents and eventWindowTime; what follows is kept raw.
The event-triggered responses arrive later as ordinary responses of the service to respond to, which reach a
UdsClient as unsolicited messages (`EventCollector`).

Checked against Wireshark's UDS dissector (event types, window times, the eventTypeRecord layouts) and the AUTOSAR
Dcm specification R23-11 §7.6.2.25. The window times other than infinite (0x02) are timer and cycle selections
whose durations are manufacturer specific; they are named, not interpreted. The record lengths of
reportActivatedEvents depend on each event's service to respond to, so its records are kept raw.
"""

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from can_explorer.core.frames import format_hex
from can_explorer.core.uds import SUPPRESS_POSITIVE_RESPONSE, UdsError, describe_response

SID = 0x86
STORE_EVENT = 0x40

STOP = 0x00
ON_DTC_STATUS_CHANGE = 0x01
ON_CHANGE_OF_DATA_IDENTIFIER = 0x03
REPORT_ACTIVATED_EVENTS = 0x04
START = 0x05
CLEAR = 0x06
ON_COMPARISON_OF_VALUES = 0x07
REPORT_MOST_RECENT_DTC_ON_STATUS_CHANGE = 0x08
REPORT_DTC_RECORD_INFORMATION_ON_DTC_STATUS_CHANGE = 0x09
EVENT_TYPES = {
    STOP: 'stopResponseOnEvent',
    ON_DTC_STATUS_CHANGE: 'onDTCStatusChange',
    ON_CHANGE_OF_DATA_IDENTIFIER: 'onChangeOfDataIdentifier',
    REPORT_ACTIVATED_EVENTS: 'reportActivatedEvents',
    START: 'startResponseOnEvent',
    CLEAR: 'clearResponseOnEvent',
    ON_COMPARISON_OF_VALUES: 'onComparisonOfValues',
    REPORT_MOST_RECENT_DTC_ON_STATUS_CHANGE: 'reportMostRecentDtcOnStatusChange',
    REPORT_DTC_RECORD_INFORMATION_ON_DTC_STATUS_CHANGE: 'reportDTCRecordInformationOnDtcStatusChange',
}

INFINITE = 0x02
WINDOW_TIMES = {
    0x02: 'infiniteTimeToResponse',
    0x03: 'shortEventWindowTime',
    0x04: 'mediumEventWindowTime',
    0x05: 'longEventWindowTime',
    0x06: 'powerWindowTime',
    0x07: 'ignitionWindowTime',
    0x08: 'manufacturerTriggerEventWindowTime',
}

COMPARISONS = {'<': 0x01, '>': 0x02, '=': 0x03, '<>': 0x04}  # comparisonLogic of onComparisonOfValues


def event_type_name(event_type: int) -> str:
    return EVENT_TYPES.get(event_type & 0x3F, f'event type 0x{event_type & 0x3F:02X}')


def window_time_name(window: int) -> str:
    return WINDOW_TIMES.get(window, f'window time 0x{window:02X}')


# Event type records


def dtc_status_record(status_mask: int) -> bytes:
    """onDTCStatusChange: the DTC status bits whose change triggers the event."""
    return bytes([status_mask])


def data_identifier_record(did: int) -> bytes:
    """onChangeOfDataIdentifier: the DID whose change triggers the event."""
    return did.to_bytes(2, 'big')


def comparison_record(
    did: int,
    comparison: str,
    value: int,
    hysteresis: int = 0,
    signed: bool = False,
    length: int = 0,
    offset: int = 0,
) -> bytes:
    """onComparisonOfValues: DID (2), comparisonLogic (1), comparison value (4), hysteresis (1, percent), and the
    localization (2): sign (bit 15), length in bits of the compared value (bits 14-10, 0: the whole value) and its
    bit offset in the DID data (bits 9-0)."""
    if comparison not in COMPARISONS:
        raise UdsError(f'Comparison {comparison!r} is not one of {", ".join(COMPARISONS)}')
    if not 0 <= length < 32 or not 0 <= offset < 1024 or not 0 <= hysteresis <= 0xFF:
        raise UdsError('Localization length 0-31 bits, offset 0-1023 bits, hysteresis 0-255')
    localization = (0x8000 if signed else 0) | (length << 10) | offset
    return (
        did.to_bytes(2, 'big')
        + bytes([COMPARISONS[comparison]])
        + (value & 0xFFFFFFFF).to_bytes(4, 'big')
        + bytes([hysteresis])
        + localization.to_bytes(2, 'big')
    )


def roe_request(
    event_type: int,
    window_time: int = INFINITE,
    event_record: bytes = b'',
    service_to_respond: bytes = b'',
    store: bool = False,
    suppress: bool = False,
) -> bytes:
    if event_type & 0x3F not in EVENT_TYPES:
        raise UdsError(f'Unknown ResponseOnEvent event type 0x{event_type:02X}')
    sub = event_type & 0x3F
    if sub in (ON_DTC_STATUS_CHANGE, ON_CHANGE_OF_DATA_IDENTIFIER, ON_COMPARISON_OF_VALUES) and not service_to_respond:
        raise UdsError(f'{EVENT_TYPES[sub]} needs a service to respond to')
    sub |= (STORE_EVENT if store else 0) | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)
    return bytes([SID, sub, window_time]) + bytes(event_record) + bytes(service_to_respond)


@dataclass(frozen=True, slots=True)
class RoeResponse:
    event_type: int
    identified_events: int  # numberOfIdentifiedEvents (reportActivatedEvents: numberOfActivatedEvents)
    window_time: Optional[int]
    rest: bytes  # The echoed eventTypeRecord and serviceToRespondTo (or, for reportActivatedEvents, the events)

    def describe(self) -> str:
        text = f'{event_type_name(self.event_type)}: {self.identified_events} event{"s" if self.identified_events != 1 else ""}'
        if self.window_time is not None:
            text += f', {window_time_name(self.window_time)}'
        return text + (f' ({format_hex(self.rest)})' if self.rest else '')


def parse_roe_response(response: bytes) -> RoeResponse:
    if len(response) < 3 or response[0] != SID + 0x40:
        raise UdsError('Not a ResponseOnEvent response')
    event_type = response[1]
    if event_type & 0x3F == REPORT_ACTIVATED_EVENTS:
        return RoeResponse(event_type, response[2], None, bytes(response[3:]))
    if len(response) < 4:
        return RoeResponse(event_type, response[2], None, b'')
    return RoeResponse(event_type, response[2], response[3], bytes(response[4:]))


async def response_on_event(
    client,
    event_type: int,
    window_time: int = INFINITE,
    event_record: bytes = b'',
    service_to_respond: bytes = b'',
    store: bool = False,
) -> Optional[RoeResponse]:
    """Set up, start, stop, clear or report events. Typical use: set up an event (e.g. ON_CHANGE_OF_DATA_IDENTIFIER
    with `data_identifier_record(did)` and `service_to_respond=b'\\x22' + did bytes`), then START; the
    event-triggered responses arrive as unsolicited messages (EventCollector)."""
    response = await client.call(roe_request(event_type, window_time, event_record, service_to_respond, store))
    return parse_roe_response(response) if response else None


@dataclass(frozen=True, slots=True)
class EventResponse:
    payload: bytes
    timestamp: float

    def describe(self) -> str:
        return describe_response(self.payload)


class EventCollector:
    """Collects the event-triggered responses reaching a UdsClient as unsolicited messages (call `client.listen`
    to receive between requests). Periodic data (0x6A) is left to the PeriodicDataReceiver."""

    def __init__(self, client, on_event: Optional[Callable[[EventResponse], None]] = None, limit: int = 1000):
        self.client = client
        self.on_event = on_event
        self.limit = limit
        self.events: List[EventResponse] = []
        client.add_unsolicited_listener(self._on_message)

    def _on_message(self, payload: bytes) -> None:
        if not payload or payload[0] == 0x6A:
            return
        event = EventResponse(bytes(payload), time.time())
        self.events.append(event)
        del self.events[: -self.limit]
        if self.on_event is not None:
            self.on_event(event)

    def close(self) -> None:
        self.client.remove_unsolicited_listener(self._on_message)

    def __enter__(self) -> 'EventCollector':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
