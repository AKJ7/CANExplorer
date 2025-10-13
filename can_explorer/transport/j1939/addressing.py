import logging
from dataclasses import dataclass
import enum

logger = logging.getLogger(__name__)


@enum.unique
class PDUType(enum.Enum):
    PDU1 = enum.auto()
    PDU2 = enum.auto()


@dataclass(frozen=True, slots=True)
class PGNInfo:
    group_number: int
    pdu_format: int
    default_priority: int
    data_length: int


@enum.unique
class PGN(enum.Enum):
    REQUEST = PGNInfo(0x00EA00, 234, 6, 3)  # 59904
    ACKNOWLEGMENT = PGNInfo(0x00E800, 0, 0, 0)  # 59392
    TP_CM = PGNInfo(0x00EC00, 236, 7, 8)


@dataclass(slots=True, frozen=True)
class AddressInfo:
    NULL = 0xFE
    GLOBAL = 0xFF

    priority: int
    extended_data_page: int
    data_page: int
    pdu_format: int
    pdu_specific_field: int
    source_address: int

    @classmethod
    def from_can_id(cls, arbitration_id: int, is_extended_id: bool) -> 'AddressInfo':
        priority = 0
        source_address = 0
        extended_data_page = 0
        data_page = 0
        page_format = 0
        pdu_specific_field = 0
        if is_extended_id:
            priority = (arbitration_id >> 19) & 0b111
            extended_data_page = (arbitration_id >> 18) & 0b1
            data_page = (arbitration_id >> 17) & 0b1
            page_format = (arbitration_id >> 16) & 0xFF
            pdu_specific_field = (arbitration_id >> 8) & 0xFF
            source_address = arbitration_id & 0xFF
        else:
            priority = (arbitration_id >> 8) & 0b111
            source_address = arbitration_id & 0xFF
        return cls(
            priority=priority,
            extended_data_page=extended_data_page,
            data_page=data_page,
            page_format=page_format,
            pdu_specific_field=pdu_specific_field,
            source_address=source_address,
        )

    def export(self) -> int:
        raise NotImplementedError()

    def validate(self) -> bool:
        try:
            assert 0 <= self.priority <= 0b111
            assert self.extended_data_page == 0
            assert 0 <= self.data_page <= 1
            assert 0 <= self.pdu_format <= 0xFF
            assert 0 <= self.pdu_specific_field <= 0xFF
            assert 0 <= self.source_address <= 0xFF
        except AssertionError as e:
            logger.warning(f'Invalid CAN Frame with Address info detected: {e=}, {self=}')
            return False
        return True

    @property
    def pgn(self) -> int:
        return (self.data_page << 16) | (self.pdu_format << 8) | (self.pdu_specific_field)

    @property
    def pdu_type(self) -> PDUType:
        return PDUType.PDU1 if 0 <= self.pdu_format <= 239 else PDUType.PDU2

    def is_type2(self) -> bool:
        return self.pdu_type == PDUType.PDU2

    @property
    def destination_address(self) -> None | int:
        return self.pdu_specific_field if self.is_type2() else None
