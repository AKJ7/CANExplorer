import asyncio
from dataclasses import dataclass
import can
import logging
import enum
from typing import Tuple, Optional
from can_explorer.transport.isotp.addressing import AddressInfo, TargetAddressingType
from can_explorer.transport.can_message import CanMessage
from can_explorer.transport.isotp.errors import NResult
from can_explorer.transport.isotp.pdu import PDU, FlowStatus, PCIType

logger = logging.getLogger(__name__)


# @enum.unique
# class IsoTpTransportState(enum.IntEnum):
#     INIT = enum.auto()
#     IDLE = enum.auto()
#    STOP = enum.auto()
#    CLOSING = enum.auto()
#    CLOSED = enum.auto()


@enum.unique
class TransportState(enum.IntEnum):
    IDLE = 0x00
    SEGMENTED_TX = 0x01
    SEGMENTED_RX = 0x02
    CLOSING = 0x03
    COMPLETE = 0x04


class TransmitData:
    def __init__(self, data):
        self._data = data


class WatchdogTimer:
    def reset(self):
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()


@dataclass(slots=True)
class TransmitConfig:
    block_size: Optional[int] = None
    flow_status: Optional[FlowStatus] = None
    min_separation_time_us: Optional[int] = None
    first_frame_length: Optional[int] = None
    transmit_state_rx: TransportState = TransportState.IDLE
    transmit_timer: WatchdogTimer = WatchdogTimer()
    last_sequence_number: int = 0
    block_count: int = 0

    @staticmethod
    def parse_st_min(st_min: int) -> int:
        if 0 <= st_min <= 0x7F:
            return st_min * 1_000
        elif 0xF1 <= st_min <= 0xF9:
            return (st_min - 0xF0) * 100
        raise ValueError(f'Invalid {st_min=}')


class IsoTpTransport(asyncio.Transport):

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol, bus: can.BusABC, addressing: AddressInfo
    ):
        self._protocol = protocol
        self._loop = loop
        self._bus = bus
        self._poll_task: Optional[asyncio.Task] = None
        self._notifier = None
        self._addressing = addressing
        self._tx_queue = asyncio.Queue()
        self._rx_queue = asyncio.Queue()
        self._rx_reader = can.AsyncBufferedReader()
        self._txrx_queue = can.AsyncBufferedReader()
        self._state = TransportState.IDLE
        self._rx_timeout = 1.0
        self._should_read = True
        self._keep_polling_for_data = True
        self._transmit_config = TransmitConfig()
        super().__init__()

    def run(self):
        pass

    def is_closing(self) -> bool:
        return self._state is TransportState.CLOSING

    def close(self):
        """Close the transport.

        Buffered data will be flushed asynchronously.  No more data
        will be received.  After all buffered data is flushed, the
        protocol's connection_lost() method will (eventually) be
        called with None as its argument.
        """
        raise NotImplementedError

    def set_protocol(self, protocol) -> None:
        """Set a new protocol."""
        self._protocol = protocol

    def get_protocol(self) -> asyncio.Protocol:
        """Return the current protocol."""
        return self._protocol

    def is_reading(self):
        """Return True if the transport is receiving."""
        raise NotImplementedError

    def pause_reading(self):
        """Pause the receiving end.

        No data will be passed to the protocol's data_received()
        method until resume_reading() is called.
        """
        self._should_read = False

    def resume_reading(self):
        """Resume the receiving end.

        Data received will once again be passed to the protocol's
        data_received() method.
        """
        self._should_read = True

    def set_write_buffer_limits(self, high=None, low=None):
        """Set the high- and low-water limits for write flow control.

        These two values control when to call the protocol's
        pause_writing() and resume_writing() methods.  If specified,
        the low-water limit must be less than or equal to the
        high-water limit.  Neither value can be negative.

        The defaults are implementation-specific.  If only the
        high-water limit is given, the low-water limit defaults to an
        implementation-specific value less than or equal to the
        high-water limit.  Setting high to zero forces low to zero as
        well, and causes pause_writing() to be called whenever the
        buffer becomes non-empty.  Setting low to zero causes
        resume_writing() to be called only once the buffer is empty.
        Use of zero for either limit is generally sub-optimal as it
        reduces opportunities for doing I/O and computation
        concurrently.
        """
        raise NotImplementedError

    def get_write_buffer_size(self):
        """Return the current size of the write buffer."""
        raise NotImplementedError

    def get_write_buffer_limits(self):
        """Get the high and low watermarks for write flow control.
        Return a tuple (low, high) where low and high are
        positive number of bytes."""
        raise NotImplementedError

    def write(self, data):
        """Write some data bytes to the transport.

        This does not block; it buffers the data and arranges for it
        to be sent out asynchronously.
        """
        logger.info(f'Request to send: {data}')
        transmit_pdu = TransmitData(data)
        self._txrx_queue.buffer.put_nowait(transmit_pdu)

    def writelines(self, list_of_data):
        """Write a list (or any iterable) of data bytes to the transport.

        The default implementation concatenates the arguments and
        calls write() on the result.
        """
        data = b''.join(list_of_data)
        self.write(data)

    def write_eof(self):
        """Close the write end after flushing buffered data.

        (This is like typing ^D into a UNIX program reading from stdin.)

        Data may still be received.
        """
        raise NotImplementedError

    def can_write_eof(self) -> bool:
        """Return True if this transport supports write_eof(), False if not."""
        return False

    def abort(self):
        """Close the transport immediately.

        Buffered data will be lost.  No more data will be received.
        The protocol's connection_lost() method will (eventually) be
        called with None as its argument.
        """
        raise NotImplementedError

    def _process_tx_data(self, data: TransmitData) -> None:
        logger.info(f'Processing Tx Data: {data}')

    def _reset_segmented_rx(self, msg: Optional[can.Message], error_code: NResult = NResult.N_OK):
        raise NotImplementedError('Reset segmented Rx not yet implemented')

    def _transmit_flow_control(self, msg: can.Message, flow_status: FlowStatus):
        raise NotImplementedError()

    def _process_rx_data(self, msg: can.Message):
        logger.info(f'Processing Rx data: {msg}')
        if not self._should_read:
            logger.debug(f'Skipping data')
            return
        try:
            pdu = PDU.from_can(msg, self._addressing)
        except AssertionError as e:
            logger.warning(f'Skipping invalid IsoTp frame: {msg}. Cause: {e=}')
            return
        except Exception as e:
            logger.error(f'An error occurred while parsing can frame: {msg}. Cause: {e=}. Aborting transfer!')
            self._should_read = False
            return
        assert self._protocol is not None, f'Invalid protocol value: {self._protocol=}'
        config = self._transmit_config
        try:
            match (config.transmit_state_rx, pdu.msg_type):
                case (TransportState.IDLE, PCIType.SINGLE_FRAME):
                    data = pdu.export(self._addressing)
                    self._protocol.data_received(data)
                case (TransportState.IDLE, PCIType.FIRST_FRAME):
                    config.first_frame_length = pdu.can_dl
                    data = pdu.export(self._addressing)
                    self._protocol.data_received(data)
                    flow_control_pdu = PDU.build_flow_control_frame(
                        flow_status=FlowStatus.CONTINUE_TO_SEND,
                        st_min_us=127_000,
                        block_size=0xFF,
                        addressing=self._addressing,
                    )
                    flow_control_frame = flow_control_pdu.export(self._addressing)
                    self._txrx_queue.buffer.put_nowait(flow_control_frame)
                case (TransportState.SEGMENTED_RX, PCIType.SINGLE_FRAME):
                    self._reset_segmented_rx(msg)
                    data = pdu.export(self._addressing)
                    self._protocol.data_received(data)
                case (TransportState.SEGMENTED_RX, PCIType.FIRST_FRAME):
                    self._reset_segmented_rx(msg)
                    config.first_frame_length = pdu.can_dl
                    data = pdu.export(self._addressing)
                    self._protocol.data_received(data)
                    flow_control_pdu = PDU.build_flow_control_frame(
                        flow_status=FlowStatus.CONTINUE_TO_SEND,
                        st_min_us=127_000,
                        block_size=0xFF,
                        addressing=self._addressing,
                    )
                    flow_control_frame = flow_control_pdu.export(self._addressing)
                    self._txrx_queue.buffer.put_nowait(flow_control_frame)
                case (TransportState.SEGMENTED_RX, PCIType.CONSECUTIVE_FRAME):
                    rx_config = self._transmit_config
                    expected_sn = (self._transmit_config.last_sequence_number + 1) & 0xF
                    if pdu.sequence_number != expected_sn:
                        self._reset_segmented_rx(msg, NResult.N_WRONG_SN)
                    elif pdu.can_dl != self._transmit_config.first_frame_length:
                        self._reset_segmented_rx(msg, NResult.N_UNEXP_PDU)
                    else:
                        data = pdu.export(self._addressing)
                        self._protocol.data_received(data)
                        if len(pdu.data) <= rx_config.first_frame_length:
                            rx_config.transmit_state_rx = TransportState.COMPLETE
                        else:
                            rx_config.block_count += 1
                            if rx_config.block_size > 0 and rx_config.block_count % rx_config.block_size == 0:
                                self._transmit_flow_control(msg, FlowStatus.CONTINUE_TO_SEND)
                                rx_config.transmit_timer.stop()
                        rx_config.transmit_timer.reset()
                        rx_config.last_sequence_number = expected_sn
                case _:
                    logger.warning(f'Ignoring frame: {msg=}, {pdu=}')
                    self._reset_segmented_rx(None)
        except Exception as e:
            logger.error(e)
        logger.info('Reached end')

    async def _poll_for_data(self):
        logger.info('Polling for data')
        async for data in self._txrx_queue:
            if isinstance(data, can.Message):
                self._process_rx_data(data)
            elif isinstance(data, TransmitData):
                self._process_tx_data(data)
            else:
                raise ValueError(f'Could not process data: {data}')

    def setup(self) -> None:
        self._notifier = can.Notifier(
            self._bus, listeners=[self._txrx_queue], timeout=self._rx_timeout, loop=self._loop
        )
        self._poll_task = asyncio.create_task(self._poll_for_data())
        self._poll_task.set_name(f'IsoTpTransport-{id(self)} polling task')
        self._loop.call_soon(self._protocol.connection_made, self)

    def shutdown(self):
        self._notifier.stop()


def create_isotp_endpoint(
    loop: asyncio.AbstractEventLoop, protocol_factory, can_bus: can.BusABC, **kwargs
) -> Tuple[asyncio.Protocol, asyncio.Transport]:
    protocol = protocol_factory()
    addressing = AddressInfo(**kwargs)
    transport = IsoTpTransport(loop, protocol, can_bus, addressing=addressing)
    loop.call_soon(transport.setup)
    return protocol, transport


if __name__ == '__main__':
    from can_explorer.transport.base_protocol import BaseCanProtocol

    logging.basicConfig(level=logging.INFO)

    async def main():
        bus = can.Bus(interface='socketcan', channel='vcan0')
        try:
            protocol, transport = create_isotp_endpoint(
                asyncio.get_running_loop(),
                lambda: BaseCanProtocol(),
                bus,
                source_address=0x01,
                target_address=0x02,
                address_extension=0x03,
                target_address_type=TargetAddressingType.PHYSICAL,
            )
            transport.write(b'This is a text to send over isoTP')

            await asyncio.Future()
        finally:
            logger.info('Exiting main')
            bus.shutdown()

    asyncio.run(main())


"""
transports.create_isotp_endpoint(
    protocol_factory,
)

transport = IsoTPTansport()
transport.on_received_complete.connect()
transport.on_transmit_complete.connect()

transport.start()

result = transport.send()

transport.stop()

isotp_transport.send()
"""
