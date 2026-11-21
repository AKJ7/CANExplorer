"""Passive VW TP 2.0 decoder: the channel setups on 0x200 / 0x200 + module, and — once it has seen a channel's setup
response — the channel's parameters, channel tests, ACKs, disconnects and data packets, with each reassembled
message described as a KWP2000 service.

Point it at a VW-group diagnostic capture that includes the channel setup: TP 2.0 data travels on identifiers the
module assigns in its setup response (typically 0x300 and 0x740), which nothing else marks as TP 2.0, so the
decoder learns them from the setup response (as the XCP decoder learns from a CONNECT). Broadcast frames (0x23 /
0x24) are labelled but not expanded, and negative setup responses carry no reason: the community description the
transport follows (see `core/tp20.py`) documents neither.
"""

from typing import Dict, Optional, Tuple

import can

from can_explorer.core.frames import format_hex
from can_explorer.core.protocols.kwp2000 import describe_payload as describe_kwp
from can_explorer.core.tp20 import (
    DATA_OPCODES,
    OP_BREAK,
    OP_BROADCAST_REQUEST,
    OP_BROADCAST_RESPONSE,
    OP_DISCONNECT,
    OP_SETUP_POSITIVE,
    SETUP_ID,
    ChannelSetup,
    Tp20Reassembler,
    describe_channel_frame,
)

SETUP_LENGTH = 7


class Tp20Decoder:
    """Passive decoder of VW TP 2.0 (channel setup, channel control and data) with KWP2000 payloads."""

    name = 'VW TP 2.0'

    def __init__(self):
        # Channel CAN id -> (direction label, reassembler), learnt from setup responses
        self._channels: Dict[int, Tuple[str, Tp20Reassembler]] = {}

    def reset(self) -> None:
        self._channels.clear()

    def describe(self, msg: can.Message) -> Optional[str]:
        if msg.is_extended_id or not msg.data:
            return None
        data = bytes(msg.data)
        if msg.arbitration_id in self._channels:
            return self._describe_channel(msg.arbitration_id, data)
        if SETUP_ID <= msg.arbitration_id <= SETUP_ID + 0xFF and len(data) >= SETUP_LENGTH:
            return self._describe_setup(msg.arbitration_id, data)
        return None

    def _describe_setup(self, can_id: int, data: bytes) -> Optional[str]:
        setup = ChannelSetup.parse(data)
        if setup is not None:
            if setup.opcode == OP_SETUP_POSITIVE and setup.tx_id is not None and setup.rx_id is not None:
                module = can_id - SETUP_ID
                # The response tells the tester: transmit on tx_id, listen on rx_id
                self._channels[setup.tx_id] = (f'tester to 0x{module:02X}', Tp20Reassembler())
                self._channels[setup.rx_id] = (f'0x{module:02X} to tester', Tp20Reassembler())
            return f'TP2.0 {setup.describe()}'
        if data[1] in (OP_BROADCAST_REQUEST, OP_BROADCAST_RESPONSE):
            kind = 'request' if data[1] == OP_BROADCAST_REQUEST else 'response'
            return f'TP2.0 broadcast {kind}, module 0x{data[0]:02X}: {format_hex(data[2:])} (not expanded)'
        return None

    def _describe_channel(self, can_id: int, data: bytes) -> str:
        label, reassembler = self._channels[can_id]
        control = describe_channel_frame(data)
        if control is not None:
            if data[0] in (OP_BREAK, OP_DISCONNECT):
                reassembler.reset()
            return f'TP2.0 {label} {control}'
        if data[0] >> 4 not in DATA_OPCODES:
            return f'TP2.0 {label} unknown opcode 0x{data[0]:02X}'
        description, payload = reassembler.feed(data)
        text = f'TP2.0 {label} {description}'
        if payload is not None:
            kwp = describe_kwp(payload)
            text += f' | KWP {kwp}' if kwp else f' | {format_hex(payload)}'
        return text
