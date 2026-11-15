"""ReadDTCInformation (UDS 0x19) beyond reportDTCByStatusMask: counts, supported DTCs, snapshot (freeze frame) and
extended data records, fault detection counters, user-defined memories and the WWH-OBD reports.

ISO 14229-1 is paywalled. The request and response layouts here were checked against the free references: the
udsoncan library (ReadDTCInformation service) and the AUTOSAR Dcm specification (R23-11, §7.6.2.5) — the latter for
0x42 and 0x55, where it names the records (DTCAndSeverityRecord, DTCAndStatusRecord). The two disagree on 0x55:
udsoncan reads 5-byte records (with a severity byte), AUTOSAR returns DTCAndStatusRecords (4 bytes); this module
follows AUTOSAR.

Snapshot and extended data records hold manufacturer-defined data whose lengths are not in the response. A snapshot
record is a list of DIDs with their data; a DID's data length comes from a diagnostic description (CDD/ODX) or from
`did_lengths`. An extended data record's length comes from `record_lengths`. Requesting a single record number
makes the length unambiguous (the rest of the response); otherwise records whose lengths are unknown are kept
undecoded (`undecoded`) instead of split at a guess.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from can_explorer.core.frames import format_hex
from can_explorer.core.uds import DTC_STATUS_BITS, UdsError, did_name, format_dtc

SID = 0x19
RESPONSE_SID = 0x59

REPORT_NUMBER_BY_STATUS_MASK = 0x01
REPORT_BY_STATUS_MASK = 0x02
REPORT_SNAPSHOT_IDENTIFICATION = 0x03
REPORT_SNAPSHOT_BY_DTC = 0x04
REPORT_EXTENDED_DATA_BY_DTC = 0x06
REPORT_NUMBER_BY_SEVERITY = 0x07
REPORT_SUPPORTED = 0x0A
REPORT_FAULT_DETECTION_COUNTER = 0x14
REPORT_USER_MEMORY_BY_STATUS_MASK = 0x17
REPORT_USER_MEMORY_SNAPSHOT_BY_DTC = 0x18
REPORT_USER_MEMORY_EXTENDED_DATA_BY_DTC = 0x19
REPORT_WWH_OBD_BY_MASK = 0x42
REPORT_WWH_OBD_PERMANENT = 0x55

SUBFUNCTIONS = {
    0x01: 'reportNumberOfDTCByStatusMask',
    0x02: 'reportDTCByStatusMask',
    0x03: 'reportDTCSnapshotIdentification',
    0x04: 'reportDTCSnapshotRecordByDTCNumber',
    0x05: 'reportDTCStoredDataByRecordNumber',
    0x06: 'reportDTCExtDataRecordByDTCNumber',
    0x07: 'reportNumberOfDTCBySeverityMaskRecord',
    0x08: 'reportDTCBySeverityMaskRecord',
    0x09: 'reportSeverityInformationOfDTC',
    0x0A: 'reportSupportedDTC',
    0x0B: 'reportFirstTestFailedDTC',
    0x0C: 'reportFirstConfirmedDTC',
    0x0D: 'reportMostRecentTestFailedDTC',
    0x0E: 'reportMostRecentConfirmedDTC',
    0x14: 'reportDTCFaultDetectionCounter',
    0x15: 'reportDTCWithPermanentStatus',
    0x16: 'reportDTCExtDataRecordByRecordNumber',
    0x17: 'reportUserDefMemoryDTCByStatusMask',
    0x18: 'reportUserDefMemoryDTCSnapshotRecordByDTCNumber',
    0x19: 'reportUserDefMemoryDTCExtDataRecordByDTCNumber',
    0x1A: 'reportSupportedDTCExtDataRecord',
    0x42: 'reportWWHOBDDTCByMaskRecord',
    0x55: 'reportWWHOBDDTCWithPermanentStatus',
    0x56: 'reportDTCInformationByDTCReadinessGroupIdentifier',
}

DTC_FORMATS = {
    0x00: 'SAE J2012-DA DTC format 00',
    0x01: 'ISO 14229-1 DTC format',
    0x02: 'SAE J1939-73 DTC format',
    0x03: 'ISO 11992-4 DTC format',
    0x04: 'SAE J2012-DA DTC format 04',
}

ALL_RECORDS = 0xFF  # Snapshot / extended data record number: all records
WWH_OBD_EMISSIONS = 0x33  # FunctionalGroupIdentifier of the emissions-related system (AUTOSAR Dcm, Table 7.18)


def subfunction_name(subfunction: int) -> str:
    return SUBFUNCTIONS.get(subfunction & 0x7F, f'sub-function 0x{subfunction:02X}')


def status_flags(status: int) -> List[str]:
    return [name for bit, name in enumerate(DTC_STATUS_BITS) if status & (1 << bit)]


@dataclass(frozen=True, slots=True)
class DtcCount:
    status_availability: int
    format: int
    count: int

    @property
    def format_name(self) -> str:
        return DTC_FORMATS.get(self.format, f'format 0x{self.format:02X}')

    def describe(self) -> str:
        return f'{self.count} DTC{"s" if self.count != 1 else ""} ({self.format_name})'


@dataclass(frozen=True, slots=True)
class DtcRecord:
    """A DTC with its status and, depending on the report, a severity, a snapshot record number or a fault
    detection counter."""

    code: int
    status: Optional[int] = None
    severity: Optional[int] = None
    record_number: Optional[int] = None  # reportDTCSnapshotIdentification
    fault_detection_counter: Optional[int] = None  # Signed, -128..127 (127: failed, -128: passed)

    @property
    def text(self) -> str:
        return format_dtc(self.code)

    @property
    def status_flags(self) -> List[str]:
        return status_flags(self.status) if self.status is not None else []

    def describe(self) -> str:
        parts = [self.text]
        if self.status is not None:
            parts.append(f'status 0x{self.status:02X}')
        if self.severity is not None:
            parts.append(f'severity 0x{self.severity:02X}')
        if self.record_number is not None:
            parts.append(f'snapshot record 0x{self.record_number:02X}')
        if self.fault_detection_counter is not None:
            parts.append(f'fault detection counter {self.fault_detection_counter}')
        return ', '.join(parts)


@dataclass(frozen=True, slots=True)
class DtcList:
    status_availability: Optional[int]
    dtcs: Tuple[DtcRecord, ...]
    memory_selection: Optional[int] = None  # User-defined memory reports
    functional_group: Optional[int] = None  # WWH-OBD reports
    severity_availability: Optional[int] = None
    format: Optional[int] = None

    def describe(self) -> str:
        text = f'{len(self.dtcs)} DTC{"s" if len(self.dtcs) != 1 else ""}'
        if self.memory_selection is not None:
            text += f' in memory 0x{self.memory_selection:02X}'
        return text


@dataclass(frozen=True, slots=True)
class SnapshotValue:
    did: int
    data: bytes
    decoded: Optional[str] = None  # From a diagnostic description

    def describe(self) -> str:
        return self.decoded or f'{did_name(self.did)}: {format_hex(self.data)}'


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    number: int
    values: Tuple[SnapshotValue, ...]
    undecoded: bytes = b''  # The record's DIDs from the first one of unknown length on
    identifiers: int = 0  # The number of DIDs the record announced


@dataclass(frozen=True, slots=True)
class ExtendedDataRecord:
    number: int
    data: bytes


@dataclass(frozen=True, slots=True)
class DtcDetails:
    """A DTC's snapshot or extended data records (reportDTCSnapshotRecordByDTCNumber, reportDTCExtDataRecordByDTCNumber
    and their user-defined memory versions)."""

    code: int
    status: int
    snapshots: Tuple[SnapshotRecord, ...] = ()
    extended_data: Tuple[ExtendedDataRecord, ...] = ()
    memory_selection: Optional[int] = None
    undecoded: bytes = b''  # Records whose lengths are unknown

    @property
    def text(self) -> str:
        return format_dtc(self.code)

    def describe(self) -> str:
        parts = []
        for snapshot in self.snapshots:
            values = [value.describe() for value in snapshot.values]
            if snapshot.undecoded:
                values.append(f'not decoded (DID lengths unknown): {format_hex(snapshot.undecoded)}')
            parts.append(f'snapshot 0x{snapshot.number:02X}: ' + ('; '.join(values) or 'empty'))
        for record in self.extended_data:
            parts.append(f'extended data 0x{record.number:02X}: {format_hex(record.data)}')
        if self.undecoded:
            parts.append(f'not decoded (record lengths unknown): {format_hex(self.undecoded)}')
        return f'{self.text} status 0x{self.status:02X}' + (': ' + ' | '.join(parts) if parts else ': no records')


# Requests


def _dtc_bytes(code: int) -> bytes:
    if not 0 <= code <= 0xFFFFFF:
        raise UdsError(f'DTC 0x{code:X} does not fit 3 bytes')
    return code.to_bytes(3, 'big')


def request(
    subfunction: int,
    status_mask: int = 0xFF,
    dtc: Optional[int] = None,
    record_number: int = ALL_RECORDS,
    severity_mask: int = 0xFF,
    memory_selection: int = 0,
    functional_group: int = WWH_OBD_EMISSIONS,
) -> bytes:
    """The request of a supported sub-function, with the parameters it takes."""
    head = bytes([SID, subfunction])
    if subfunction in (REPORT_NUMBER_BY_STATUS_MASK, REPORT_BY_STATUS_MASK):
        return head + bytes([status_mask])
    if subfunction in (REPORT_SNAPSHOT_IDENTIFICATION, REPORT_SUPPORTED, REPORT_FAULT_DETECTION_COUNTER):
        return head
    if subfunction in (REPORT_SNAPSHOT_BY_DTC, REPORT_EXTENDED_DATA_BY_DTC):
        if dtc is None:
            raise UdsError(f'{subfunction_name(subfunction)} needs a DTC')
        return head + _dtc_bytes(dtc) + bytes([record_number])
    if subfunction == REPORT_NUMBER_BY_SEVERITY:
        return head + bytes([severity_mask, status_mask])
    if subfunction == REPORT_USER_MEMORY_BY_STATUS_MASK:
        return head + bytes([status_mask, memory_selection])
    if subfunction in (REPORT_USER_MEMORY_SNAPSHOT_BY_DTC, REPORT_USER_MEMORY_EXTENDED_DATA_BY_DTC):
        if dtc is None:
            raise UdsError(f'{subfunction_name(subfunction)} needs a DTC')
        return head + _dtc_bytes(dtc) + bytes([record_number, memory_selection])
    if subfunction == REPORT_WWH_OBD_BY_MASK:
        return head + bytes([functional_group, status_mask, severity_mask])
    if subfunction == REPORT_WWH_OBD_PERMANENT:
        return head + bytes([functional_group])
    raise UdsError(f'ReadDTCInformation {subfunction_name(subfunction)} is not supported')


# Responses


def _check(response: bytes, subfunction: int, minimum: int) -> None:
    if len(response) < 2 or response[0] != RESPONSE_SID or response[1] != subfunction:
        raise UdsError(f'Not a response to ReadDTCInformation {subfunction_name(subfunction)}')
    if len(response) < minimum:
        raise UdsError(f'ReadDTCInformation {subfunction_name(subfunction)} response too short')


def _records(data: bytes, size: int, subfunction: int) -> List[bytes]:
    if len(data) % size:
        raise UdsError(
            f'ReadDTCInformation {subfunction_name(subfunction)}: {len(data)} bytes of records, not a multiple of {size}'
        )
    return [data[i : i + size] for i in range(0, len(data), size)]


def parse_count(response: bytes, subfunction: int = REPORT_NUMBER_BY_STATUS_MASK) -> DtcCount:
    """reportNumberOfDTCByStatusMask / BySeverityMaskRecord: availability mask, DTC format, count (2 bytes)."""
    _check(response, subfunction, 6)
    return DtcCount(response[2], response[3], int.from_bytes(response[4:6], 'big'))


def parse_list(response: bytes, subfunction: int) -> DtcList:
    """The reports listing DTCs: by status mask, supported, snapshot identification, fault detection counters,
    user-defined memory by status mask, and the WWH-OBD reports."""
    if subfunction in (REPORT_BY_STATUS_MASK, REPORT_SUPPORTED):
        _check(response, subfunction, 3)
        dtcs = [DtcRecord(int.from_bytes(r[:3], 'big'), r[3]) for r in _records(response[3:], 4, subfunction)]
        return DtcList(response[2], tuple(dtcs))
    if subfunction == REPORT_USER_MEMORY_BY_STATUS_MASK:
        _check(response, subfunction, 4)
        dtcs = [DtcRecord(int.from_bytes(r[:3], 'big'), r[3]) for r in _records(response[4:], 4, subfunction)]
        return DtcList(response[3], tuple(dtcs), memory_selection=response[2])
    if subfunction == REPORT_SNAPSHOT_IDENTIFICATION:
        _check(response, subfunction, 2)
        records = _records(response[2:], 4, subfunction)
        return DtcList(None, tuple(DtcRecord(int.from_bytes(r[:3], 'big'), record_number=r[3]) for r in records))
    if subfunction == REPORT_FAULT_DETECTION_COUNTER:
        _check(response, subfunction, 2)
        records = _records(response[2:], 4, subfunction)
        counters = [DtcRecord(int.from_bytes(r[:3], 'big'), fault_detection_counter=_signed(r[3])) for r in records]
        return DtcList(None, tuple(counters))
    if subfunction == REPORT_WWH_OBD_BY_MASK:
        # FunctionalGroupIdentifier, status availability, severity availability, DTC format, then
        # DTCAndSeverityRecords: severity, DTC (3), status
        _check(response, subfunction, 6)
        records = _records(response[6:], 5, subfunction)
        dtcs = [DtcRecord(int.from_bytes(r[1:4], 'big'), r[4], severity=r[0]) for r in records]
        return DtcList(
            response[3],
            tuple(dtcs),
            functional_group=response[2],
            severity_availability=response[4],
            format=response[5],
        )
    if subfunction == REPORT_WWH_OBD_PERMANENT:
        # FunctionalGroupIdentifier, status availability, DTC format, then DTCAndStatusRecords (AUTOSAR Table 7.21)
        _check(response, subfunction, 5)
        dtcs = [DtcRecord(int.from_bytes(r[:3], 'big'), r[3]) for r in _records(response[5:], 4, subfunction)]
        return DtcList(response[3], tuple(dtcs), functional_group=response[2], format=response[4])
    raise UdsError(f'ReadDTCInformation {subfunction_name(subfunction)} does not list DTCs')


def _signed(byte: int) -> int:
    return byte - 0x100 if byte & 0x80 else byte


def parse_snapshots(
    response: bytes,
    subfunction: int = REPORT_SNAPSHOT_BY_DTC,
    did_length: Optional[Callable[[int], Optional[int]]] = None,
    describe: Optional[Callable[[int, bytes], Optional[str]]] = None,
    single_record: bool = False,
) -> DtcDetails:
    """reportDTCSnapshotRecordByDTCNumber (and the user-defined memory version): DTC, status, then records of
    record number, number of identifiers, and per identifier a DID (2 bytes) and its data.

    `did_length(did)` gives a DID's data length (None: unknown). With `single_record` (a single record number was
    requested) and one identifier, the rest of the response is that DID's data. A record is decoded up to the first
    DID of unknown length; the bytes from there on are kept in `undecoded`, as are all further records."""
    memory = subfunction == REPORT_USER_MEMORY_SNAPSHOT_BY_DTC
    start = 3 if memory else 2
    _check(response, subfunction, start + 4)
    code = int.from_bytes(response[start : start + 3], 'big')
    status = response[start + 3]
    offset = start + 4
    records: List[SnapshotRecord] = []
    while offset < len(response):
        if len(response) - offset < 2:
            raise UdsError('Snapshot record without its number of identifiers')
        number, count = response[offset], response[offset + 1]
        offset += 2
        values: List[SnapshotValue] = []
        for index in range(count):
            if len(response) - offset < 2:
                raise UdsError(f'Snapshot record 0x{number:02X} ends before its identifier {index + 1} of {count}')
            did = int.from_bytes(response[offset : offset + 2], 'big')
            length = did_length(did) if did_length is not None else None
            if length is None and single_record and count == 1:
                length = len(response) - offset - 2
            if length is None or len(response) - offset - 2 < length:
                # Unknown (or impossible) length: keep the rest instead of splitting at a guess
                records.append(SnapshotRecord(number, tuple(values), bytes(response[offset:]), count))
                return DtcDetails(code, status, tuple(records), memory_selection=response[2] if memory else None)
            data = bytes(response[offset + 2 : offset + 2 + length])
            values.append(SnapshotValue(did, data, describe(did, data) if describe is not None else None))
            offset += 2 + length
        records.append(SnapshotRecord(number, tuple(values), identifiers=count))
    return DtcDetails(code, status, tuple(records), memory_selection=response[2] if memory else None)


def parse_extended_data(
    response: bytes,
    subfunction: int = REPORT_EXTENDED_DATA_BY_DTC,
    record_lengths: Optional[Mapping[int, int]] = None,
    single_record: bool = False,
) -> DtcDetails:
    """reportDTCExtDataRecordByDTCNumber (and the user-defined memory version): DTC, status, then records of record
    number and data. A record's length comes from `record_lengths`; with `single_record` the rest of the response is
    the record. Records from the first one of unknown length on are kept in `undecoded`."""
    memory = subfunction == REPORT_USER_MEMORY_EXTENDED_DATA_BY_DTC
    start = 3 if memory else 2
    _check(response, subfunction, start + 4)
    code = int.from_bytes(response[start : start + 3], 'big')
    status = response[start + 3]
    offset = start + 4
    records: List[ExtendedDataRecord] = []
    undecoded = b''
    lengths = dict(record_lengths or {})
    while offset < len(response):
        number = response[offset]
        length = lengths.get(number)
        if length is None and single_record and not records:
            length = len(response) - offset - 1
        if length is None or len(response) - offset - 1 < length:
            undecoded = bytes(response[offset:])
            break
        records.append(ExtendedDataRecord(number, bytes(response[offset + 1 : offset + 1 + length])))
        offset += 1 + length
    return DtcDetails(
        code,
        status,
        extended_data=tuple(records),
        memory_selection=response[2] if memory else None,
        undecoded=undecoded,
    )


# Client


@dataclass
class DtcReader:
    """Reads ReadDTCInformation reports with a UdsClient; `description` (a DiagnosticDescription) gives snapshot DID
    lengths and decodes their data, `did_lengths`/`record_lengths` give lengths it does not know."""

    client: object
    description: object = None
    did_lengths: Dict[int, int] = field(default_factory=dict)
    record_lengths: Dict[int, int] = field(default_factory=dict)

    def _did_length(self, did: int) -> Optional[int]:
        if did in self.did_lengths:
            return self.did_lengths[did]
        if self.description is not None and hasattr(self.description, 'byte_length'):
            return self.description.byte_length(did)
        return None

    def _describe(self, did: int, data: bytes) -> Optional[str]:
        return self.description.describe(did, data) if self.description is not None else None

    async def read(self, subfunction: int, **parameters):
        payload = request(subfunction, **parameters)
        response = await self.client.call(payload)
        if subfunction in (REPORT_NUMBER_BY_STATUS_MASK, REPORT_NUMBER_BY_SEVERITY):
            return parse_count(response, subfunction)
        single = parameters.get('record_number', ALL_RECORDS) != ALL_RECORDS
        if subfunction in (REPORT_SNAPSHOT_BY_DTC, REPORT_USER_MEMORY_SNAPSHOT_BY_DTC):
            return parse_snapshots(response, subfunction, self._did_length, self._describe, single)
        if subfunction in (REPORT_EXTENDED_DATA_BY_DTC, REPORT_USER_MEMORY_EXTENDED_DATA_BY_DTC):
            return parse_extended_data(response, subfunction, self.record_lengths, single)
        return parse_list(response, subfunction)


async def read(
    client,
    subfunction: int,
    description=None,
    did_lengths: Optional[Dict[int, int]] = None,
    record_lengths: Optional[Dict[int, int]] = None,
    **parameters,
):
    """Send a ReadDTCInformation request and parse its response: a DtcCount, DtcList or DtcDetails.

    Parameters by sub-function: status_mask (0x01, 0x02, 0x07, 0x17, 0x42), dtc and record_number (0x04, 0x06,
    0x18, 0x19; 0xFF: all records), severity_mask (0x07, 0x42), memory_selection (0x17-0x19), functional_group
    (0x42, 0x55)."""
    reader = DtcReader(client, description, dict(did_lengths or {}), dict(record_lengths or {}))
    return await reader.read(subfunction, **parameters)
