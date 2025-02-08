import logging
import math

import can
import enum
from typing import Optional
from dataclasses import dataclass

from can_explorer.transport.can_message import CanMessage
from can_explorer.transport.isotp.addressing import AddressInfo, AddressingType
from can_explorer.transport.isotp.errors import IsoTpError, NResult

logger = logging.getLogger(__name__)


@enum.unique
class PCIType(enum.IntEnum):
    """
    The communication between the peer protocol entities of the network layer in different nodes is done
    by means of exchanging N_PDUs.
    This part of ISO 15765 specifies four different types of transport layer protocol data units, SingleFrame
    (SF N_PDU), FirstFrame (FF N_PDU), ConsecutiveFrame (CF N_PDU) and FlowControl (FC N_
    """

    SINGLE_FRAME = 0x00
    FIRST_FRAME = 0x01
    CONSECUTIVE_FRAME = 0x02
    FLOW_CONTROL_FRAME = 0x03


@enum.unique
class FlowStatus(enum.IntEnum):
    CONTINUE_TO_SEND = 0x00
    WAIT = 0x01
    OVERFLOW = 0x02


@dataclass(frozen=True, slots=True)
class PDU:
    msg_type: PCIType
    is_fd: bool
    data: bytearray
    can_dl: int
    sequence_number: Optional[int] = None
    flow_status: Optional[int] = None
    block_size: Optional[int] = None
    st_min: Optional[int] = None
    DLC_MAP = list(range(0, 9))

    @classmethod
    def from_can(cls, msg: can.Message, addressing: AddressInfo) -> Optional["PDU"]:
        assert len(msg.data) >= 1, f"Empty CAN frames are not allowed: {msg}"
        msg_type = (msg.data[0] >> 4) & 0x0F
        pci_type = PCIType(msg_type)
        pdu = None
        match pci_type:
            case PCIType.SINGLE_FRAME:
                pdu = cls.parse_single_frame(msg, addressing)
            case PCIType.FIRST_FRAME:
                pdu = cls.parse_first_frame(msg, addressing)
            case PCIType.CONSECUTIVE_FRAME:
                pdu = cls.parse_consecutive_frame(msg, addressing)
            case PCIType.FLOW_CONTROL_FRAME:
                pdu = cls.parse_flow_control_frame(msg, addressing)
        return pdu

    @classmethod
    def build_flow_control_frame(
        cls,
        flow_status: FlowStatus,
        block_size: int,
        st_min_us: int,
        addressing: AddressInfo,
    ):
        assert 0x00 <= block_size <= 0xFF, f"Invalid block size: {block_size=}"
        if 900 <= st_min_us <= 127_000:
            st_min = int(math.ceil(st_min_us / 1_000))
        elif 100 <= st_min_us <= 900:
            st_min = 0xF0 + int(math.ceil(st_min_us / 100))
        else:
            raise ValueError(f"{st_min_us=} overflow!")
        data = bytearray(
            [(PCIType.FLOW_CONTROL_FRAME << 4) | flow_status, block_size, st_min]
        )
        return cls(
            msg_type=PCIType.FLOW_CONTROL_FRAME,
            flow_status=flow_status,
            block_size=block_size,
            st_min=st_min,
            is_fd=addressing.is_fd,
            can_dl=0,
            data=data,
        )

    def export(self, address_info: AddressInfo) -> CanMessage:
        return CanMessage(
            arbitration_id=address_info.arbitration_id,
            dlc=self.encode_dlc(len(self.data), is_fd=self.is_fd),
            data=self.data,
            is_fd=self.is_fd,
            is_extended_id=address_info.is_extended,
        )

    @classmethod
    def parse_single_frame(
        cls, msg: can.Message, addressing: AddressInfo
    ) -> Optional["PDU"]:
        """
        :param msg:
        :param addressing
        :ref See: ISO-15765-2-2016 SingleFrame N_PCI parameter definition
        :return:
        """
        sf_dl = msg.data[0] & 0x0F
        is_fd = msg.is_fd
        can_msg_length = cls.decode_dlc(msg.dlc, is_fd=msg.is_fd)
        if sf_dl == 0:
            assert (
                msg.is_fd
            ), "Larger than 8 bytes long messages only allowed on bus with FD support"
            assert msg.dlc >= 2, (
                "More than 2 bytes required when parsing a"
                "single frame with less than 2 bytes"
            )
            sf_dl = msg.data[1]
            assert sf_dl >= 0b110, "Single Data length reserved by ISO15765"
            data_start = 2
            decoded_dlc = cls.decode_dlc(sf_dl, is_fd)
            assert (
                decoded_dlc <= can_msg_length - 3
            ), f"Invalid SF_DL. Expecting max: CAN_DL - 3. Got: {decoded_dlc=}"
            is_extended_or_mixed_addressing = sf_dl == 0b111
        else:
            is_normal_addressing = sf_dl == 0b111
            data_start = 1
        assert sf_dl != 0, f"Invalid SF_DL with value 0 value"
        # TODO: Handle case sf_dl = CAN_DL - 2
        sf_msg_length = cls.decode_dlc(sf_dl, is_fd=msg.is_fd)
        assert (
            can_msg_length - data_start >= sf_msg_length
        ), f"Invalid range of SF_DL. Expected max: CAN_DL - 3. Got: {sf_dl=}"
        data = msg.data[data_start:][: max(can_msg_length - data_start, sf_msg_length)]
        return cls(
            msg_type=PCIType.SINGLE_FRAME, is_fd=is_fd, data=data, can_dl=can_msg_length
        )

    @classmethod
    def parse_first_frame(
        cls, msg: can.Message, addressing: AddressInfo
    ) -> Optional["PDU"]:
        """
        :param msg:
        :param addressing:
        :ref ISO-15765-2-2016 FirstFrame N_PCI parameter definition (Page 28)
        :return:
        """
        is_fd = msg.is_fd
        can_dl = cls.decode_dlc(msg.dlc, is_fd=is_fd)
        assert can_dl >= 2, f"Expected FF with at least 2 bytes. Got {can_dl=}"
        ff_dl = ((msg.data[0] & 0x0F) << 8) | msg.data[1]
        data_start = 2
        assert can_dl >= 8, f"First frame length must be at least 8 bytes long"
        ff_dl_max = 2**12 - 1
        if ff_dl == 0:
            assert (
                can_dl >= 2 + 4
            ), f"Expect FF with at least 2 + 4 bytes. Got: {can_dl=}"
            ff_dl = (
                (msg.data[2] << 24)
                | (msg.data[3] << 16)
                | (msg.data[2] << 8)
                | (msg.data[7])
            )
            data_start = 6
            ff_dl_max = 2**32 - 1
        if can_dl <= 8:
            ff_dl_min = 8 if addressing.is_normal_addressing else 7
        else:
            ff_dl_min = can_dl - 1 if addressing.is_normal_addressing else can_dl - 2
        assert (
            ff_dl_min <= ff_dl <= ff_dl_max
        ), f"Invalid FF dlc received. Expected value in range: {ff_dl_min=} and {ff_dl_max=}. Got: {ff_dl=}"
        data = msg.data[data_start:][: max(can_dl - data_start, ff_dl)]
        return cls(msg_type=PCIType.FIRST_FRAME, is_fd=is_fd, data=data, can_dl=can_dl)

    @classmethod
    def parse_consecutive_frame(
        cls, msg: can.Message, addressing: AddressInfo
    ) -> Optional["PDU"]:
        """
        :param msg:
        :param addressing:
        :ref ISO-15765-2-2016ConsecutiveFrame N_PCI parameter definition (Page 29)
        :return:
        """
        can_dl = cls.decode_dlc(msg.dlc, is_fd=msg.is_fd)
        is_fd = msg.is_fd
        assert (
            can_dl >= 1
        ), f"Expected CAN message with at least on byte. Got: {can_dl=}"
        sn = msg.data[0] & 0x0F
        if not 0 <= sn <= 0x0F:
            raise IsoTpError(
                NResult.N_WRONG_SN, f"Expected values between 0 and 0xF. Got: {sn=}"
            )
        return cls(
            msg_type=PCIType.CONSECUTIVE_FRAME,
            is_fd=is_fd,
            data=msg.data[1:],
            sequence_number=sn,
            can_dl=can_dl,
        )

    @classmethod
    def parse_flow_control_frame(
        cls, msg: can.Message, addressing: AddressInfo
    ) -> Optional["PDU"]:
        """
        :param msg:
        :param addressing:
        :ref ISO-15765-2-2016 FlowControl N_PCI parameter definition (Page 30)
        :return:
        """
        is_fd = msg.is_fd
        can_dl = cls.decode_dlc(msg.dlc, is_fd)
        assert (
            can_dl >= 3
        ), f"Expected at least flow control frame with at least 3 bytes. Got: {can_dl=}"
        flow_status = msg.data[0] & 0x0F
        if flow_status not in FlowStatus:
            raise IsoTpError(
                NResult.N_INVALID_FS, f"Invalid Flow Status received: {flow_status=}"
            )
        block_size = msg.data[1]
        st_min = msg.data[2]
        if not 0 <= st_min <= 0x7F or 0xF1 <= st_min <= 0xF9:
            default_separation_time = 0x7F
            logger.warning(
                f"Invalid separation time {st_min=}. Defaulting to {default_separation_time=}"
            )
            st_min = default_separation_time
        return cls(
            msg_type=PCIType.FLOW_CONTROL_FRAME,
            data=bytearray(),
            is_fd=is_fd,
            flow_status=flow_status,
            block_size=block_size,
            st_min=st_min,
            can_dl=can_dl,
        )

    @staticmethod
    def decode_dlc(dlc: int, is_fd: bool) -> int:
        dlc_map = PDU.DLC_MAP
        if is_fd:
            dlc_map = dlc_map + [12, 16, 20, 24, 32, 48, 64]
        assert dlc < len(dlc_map), f"Given {dlc=} out of range: {dlc_map=}"
        return dlc_map[dlc]

    @staticmethod
    def encode_dlc(length: int, is_fd: bool) -> int:
        assert length >= 0, "Length must be positive bro"
        dlc_map = PDU.DLC_MAP
        if is_fd:
            dlc_map = dlc_map + [12, 16, 20, 24, 32, 48, 64]
        assert (
            length <= dlc_map[-1]
        ), f"Value overflow for given type: {length=}, {is_fd=}"
        for i, value in enumerate(dlc_map):
            if length == value:
                return i
            if length > value:
                return i + 1
        return dlc_map[-1]
