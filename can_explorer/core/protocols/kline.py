"""Passive K-Line decoder: describes the service payloads of a K-Line bus (ISO 9141-2 / ISO 14230 KWP2000) as read
through the `elm327_kline` interface.

On K-Line, unlike CAN, there is no ISO-TP transport: the ELM327 adapter removes the three-byte header (which the
`elm327_kline` backend puts in the frame's identifier) and the checksum, so each frame's data is already one
complete diagnostic service payload. This decoder describes that payload directly — first as a KWP2000 (ISO 14230)
service, then, if that does not match, as an SAE J1979 (OBD-II) service — reusing the payload describers of the
KWP2000 and J1850 subsystems so the naming matches those views.

It therefore differs from the **KWP2000** decoder, which reassembles ISO-TP frames on CAN. Point this one at a
K-Line connection (or any traffic whose frames carry a bare service payload).
"""

from typing import Optional

import can

from can_explorer.core.j1850 import (
    NEGATIVE_RESPONSE,
    SERVICES as OBD_SERVICES,
    describe_payload as describe_obd_payload,
)
from can_explorer.core.protocols.kwp2000 import describe_payload as describe_kwp_payload


def _is_obd_service(data: bytes) -> bool:
    """Whether the payload starts with a recognised SAE J1979 (OBD-II) service: a request mode, a positive response
    (mode + 0x40) or a negative response. Avoids echoing an unrecognised byte as if it were a service."""
    first = data[0]
    return first in OBD_SERVICES or (first - 0x40) in OBD_SERVICES or first == NEGATIVE_RESPONSE


def describe_kline(data: bytes) -> Optional[str]:
    """Describe one K-Line service payload (no transport header): a KWP2000 service if recognised, otherwise an
    SAE J1979 (OBD-II) service. Returns None if neither names it."""
    data = bytes(data)
    if not data:
        return None
    kwp = describe_kwp_payload(data)
    if kwp:
        return kwp
    return describe_obd_payload(data) if _is_obd_service(data) else None


class KLineDecoder:
    """Passive K-Line decoder (ISO 9141-2 / ISO 14230). Each frame's data is one service payload; see the module
    docstring on why this is not the same as the ISO-TP-based KWP2000 decoder."""

    name = 'K-Line'

    def reset(self) -> None:
        pass

    def describe(self, msg: can.Message) -> Optional[str]:
        description = describe_kline(msg.data)
        return f'K-Line {description}' if description else None
