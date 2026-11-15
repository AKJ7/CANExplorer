"""Functional addressing: one UDS request to all ECUs (or a group) at once, and the collection of their responses.

A functional request goes to a functional address — on CAN e.g. 0x7DF (11-bit OBD) or 0x18DB33F1 (29-bit OBD), on
DoIP a functional logical address — and every ECU that supports it answers on its own physical response address.
On CAN a functional request must fit a single frame (ISO 15765-2: no flow control from several receivers), so at
most 7 bytes (62 on CAN FD). The responses may be segmented; the flow control for each goes to that ECU's physical
request identifier.

ECUs do not answer a functional request with the negative responses serviceNotSupported, subFunctionNotSupported,
requestOutOfRange and the "not supported in active session" codes (ISO 14229-1), so silence is the usual answer
from an ECU without the service.

Typical uses: TesterPresent (0x3E 0x80) to all ECUs during flashing, entering the extended session, disabling
communication (0x28) and DTC setting (0x85) on the whole bus before programming, and finding the ECUs present.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Tuple

import can

from can_explorer.core.isotp_client import CLASSIC_FRAME, FD_FRAME, IsoTpClient, IsoTpError
from can_explorer.core.uds import (
    NEGATIVE_RESPONSE,
    P2_EXTENDED_TIMEOUT,
    P2_TIMEOUT,
    SUBFUNCTION_SERVICES,
    SUPPRESS_POSITIVE_RESPONSE,
    Dtc,
    UdsResult,
    _is_pending,
    _is_response_to,
)

OBD_FUNCTIONAL_11BIT = 0x7DF
OBD_FUNCTIONAL_29BIT = 0x18DB33F1


class FunctionalTransport(Protocol):
    async def send(self, payload: bytes) -> None: ...

    async def receive_from(self, timeout: float) -> Tuple[int, bytes]: ...

    def close(self) -> None: ...


class IsoTpFunctional:
    """Functional requests on CAN and the responses of every ECU, each reassembled by its own ISO-TP receiver.

    `response_to_request(response_id)` gives an ECU's physical request identifier (for flow control), or None for a
    frame that is not an ECU response. See `obd_11bit` and `obd_29bit` for the OBD conventions."""

    def __init__(
        self,
        session,
        functional_id: int,
        response_to_request: Callable[[int], Optional[int]],
        extended: bool = False,
        fd: bool = False,
        padding: Optional[int] = 0xCC,
    ):
        self.session = session
        self.functional_id = functional_id
        self.extended, self.fd = extended, fd
        self._response_to_request = response_to_request
        self._sender = IsoTpClient(session, functional_id, -1, extended, fd, padding, listen=False)
        self._padding = padding
        self._receivers: Dict[int, IsoTpClient] = {}
        self._received: asyncio.Queue[Tuple[int, bytes]] = asyncio.Queue()
        session.add_message_listener(self._on_message)

    def close(self) -> None:
        self.session.remove_message_listener(self._on_message)

    def __enter__(self) -> 'IsoTpFunctional':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    async def send(self, payload: bytes) -> None:
        limit = (FD_FRAME - 2) if self.fd else (CLASSIC_FRAME - 1)
        if len(payload) > limit:
            raise IsoTpError(f'A functional request must fit one frame ({limit} bytes), not {len(payload)}')
        while not self._received.empty():
            self._received.get_nowait()
        await self._sender.send(payload)

    async def receive_from(self, timeout: float) -> Tuple[int, bytes]:
        try:
            return await asyncio.wait_for(self._received.get(), timeout)
        except asyncio.TimeoutError:
            raise IsoTpError('Timeout waiting for a response') from None

    def _on_message(self, msg: can.Message) -> None:
        if not msg.is_rx or msg.is_extended_id != self.extended or msg.arbitration_id == self.functional_id:
            return
        receiver = self._receivers.get(msg.arbitration_id)
        if receiver is None:
            request_id = self._response_to_request(msg.arbitration_id)
            if request_id is None:
                return
            response_id = msg.arbitration_id
            receiver = IsoTpClient(
                self.session,
                request_id,
                response_id,
                self.extended,
                self.fd,
                self._padding,
                listen=False,
                on_payload=lambda payload, source=response_id: self._received.put_nowait((source, payload)),
            )
            self._receivers[response_id] = receiver
        receiver.feed(msg)


def obd_11bit(session, fd: bool = False) -> IsoTpFunctional:
    """Functional 0x7DF; ECUs answer on 0x7E8-0x7EF and take physical requests on the response id - 8."""
    return IsoTpFunctional(
        session, OBD_FUNCTIONAL_11BIT, lambda response: response - 8 if 0x7E8 <= response <= 0x7EF else None, fd=fd
    )


def obd_29bit(session, tester: int = 0xF1, fd: bool = False) -> IsoTpFunctional:
    """Functional 0x18DB33F1 (normal fixed addressing); an ECU with address xx answers on 0x18DA<tester>xx and takes
    physical requests on 0x18DAxx<tester>."""

    def request_of(response: int) -> Optional[int]:
        if response & 0x1FFFFF00 != (0x18DA0000 | (tester << 8)):
            return None
        return 0x18DA0000 | ((response & 0xFF) << 8) | tester

    return IsoTpFunctional(session, (OBD_FUNCTIONAL_29BIT & ~0xFF) | tester, request_of, extended=True, fd=fd)


def mapped(session, functional_id: int, ecus: Mapping[int, int], extended: bool = False, fd: bool = False):
    """Functional requests to `functional_id` with explicit ECUs: {response id: physical request id}."""
    table = dict(ecus)
    return IsoTpFunctional(session, functional_id, table.get, extended=extended, fd=fd)


class DoipFunctional:
    """Functional requests over a DoIP connection: diagnostic messages to a functional logical address, answered by
    the ECUs behind the entity with their own source addresses. The functional addresses are vehicle specific; use
    the one documented for the vehicle."""

    def __init__(self, client, functional_address: int):
        self.client = client
        self.functional_address = functional_address
        self._received: asyncio.Queue[Tuple[int, bytes]] = asyncio.Queue()
        client.add_any_receiver(self._on_message)

    def _on_message(self, source: int, payload: bytes) -> None:
        self._received.put_nowait((source, payload))

    def close(self) -> None:
        self.client.remove_any_receiver(self._on_message)

    async def send(self, payload: bytes) -> None:
        while not self._received.empty():
            self._received.get_nowait()
        await self.client.send_diagnostic(self.functional_address, payload)

    async def receive_from(self, timeout: float) -> Tuple[int, bytes]:
        from can_explorer.core.doip import DoipError

        try:
            return await asyncio.wait_for(self._received.get(), timeout)
        except asyncio.TimeoutError:
            raise DoipError('Timeout waiting for a response') from None


@dataclass(frozen=True, slots=True)
class FunctionalResults:
    """The final response of every ECU that answered, by response address."""

    request: bytes
    results: Dict[int, UdsResult]

    @property
    def responders(self) -> List[int]:
        return sorted(self.results)

    @property
    def positive(self) -> Dict[int, bytes]:
        return {address: result.response for address, result in self.results.items() if result.positive}

    def describe(self) -> str:
        if not self.results:
            return 'No ECU answered'
        return '; '.join(f'0x{address:X}: {result.description}' for address, result in sorted(self.results.items()))


class FunctionalUdsClient:
    """Sends functional requests and collects the responses of all ECUs until `timeout` (P2) passed without one —
    longer for ECUs that answered "response pending" (P2* each)."""

    def __init__(
        self, transport: FunctionalTransport, timeout: float = P2_TIMEOUT, extended_timeout: float = P2_EXTENDED_TIMEOUT
    ):
        self.transport = transport
        self.timeout = timeout
        self.extended_timeout = extended_timeout
        self._unsolicited: List[Callable[[int, bytes], None]] = []

    def add_unsolicited_listener(self, listener: Callable[[int, bytes], None]) -> None:
        self._unsolicited.append(listener)

    async def send(self, payload: bytes) -> None:
        """Send without waiting (e.g. a suppressed TesterPresent); also lets SessionKeepAlive use this client."""
        await self.transport.send(bytes(payload))

    async def request(self, payload: bytes, timeout: Optional[float] = None) -> FunctionalResults:
        payload = bytes(payload)
        await self.transport.send(payload)
        suppressed = (
            len(payload) >= 2 and payload[0] in SUBFUNCTION_SERVICES and payload[1] & SUPPRESS_POSITIVE_RESPONSE
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timeout if timeout is None else timeout)
        pending: Dict[int, float] = {}
        results: Dict[int, UdsResult] = {}
        while True:
            now = loop.time()
            wait = max([deadline, *pending.values()]) - now
            if wait <= 0:
                break
            try:
                source, response = await self.transport.receive_from(wait)
            except Exception:
                break
            if _is_pending(response) and response[1] == payload[0]:
                pending[source] = loop.time() + self.extended_timeout
                continue
            if not _is_response_to(payload, response):
                for listener in list(self._unsolicited):
                    listener(source, response)
                continue
            pending.pop(source, None)
            results[source] = UdsResult(payload, response)
        for source in pending:
            results.setdefault(source, UdsResult(payload, None, 'Timeout after "response pending"'))
        if suppressed:
            # A suppressed request is answered only negatively (or not at all)
            results = {k: v for k, v in results.items() if v.response and v.response[0] == NEGATIVE_RESPONSE}
        return FunctionalResults(payload, results)

    # Common functional requests

    async def tester_present(self, suppress_response: bool = True) -> FunctionalResults:
        return await self.request(bytes([0x3E, SUPPRESS_POSITIVE_RESPONSE if suppress_response else 0x00]))

    async def discover(self, timeout: Optional[float] = None) -> List[int]:
        """The response addresses of the ECUs answering TesterPresent (not suppressed)."""
        return (await self.request(bytes([0x3E, 0x00]), timeout)).responders

    async def diagnostic_session_control(self, session: int, suppress: bool = True) -> FunctionalResults:
        return await self.request(bytes([0x10, session | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)]))

    async def control_dtc_setting(self, on: bool, suppress: bool = True) -> FunctionalResults:
        setting = (0x01 if on else 0x02) | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)
        return await self.request(bytes([0x85, setting]))

    async def communication_control(
        self, control_type: int, communication_type: int = 0x01, suppress: bool = True
    ) -> FunctionalResults:
        sub = control_type | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)
        return await self.request(bytes([0x28, sub, communication_type]))

    async def ecu_reset(self, reset_type: int = 0x01, suppress: bool = True) -> FunctionalResults:
        return await self.request(bytes([0x11, reset_type | (SUPPRESS_POSITIVE_RESPONSE if suppress else 0)]))

    async def read_dtcs(self, status_mask: int = 0xFF) -> Dict[int, List[Dtc]]:
        """reportDTCByStatusMask from every ECU: {response address: DTCs}."""
        results = await self.request(bytes([0x19, 0x02, status_mask]))
        dtcs: Dict[int, List[Dtc]] = {}
        for address, response in results.positive.items():
            records = response[3:]
            dtcs[address] = [
                Dtc(int.from_bytes(records[i : i + 3], 'big'), records[i + 3]) for i in range(0, len(records) - 3, 4)
            ]
        return dtcs

    async def read_data_by_identifier(self, did: int) -> Dict[int, bytes]:
        results = await self.request(bytes([0x22, did >> 8, did & 0xFF]))
        return {
            address: response[3:]
            for address, response in results.positive.items()
            if response[1:3] == bytes([did >> 8, did & 0xFF])
        }
