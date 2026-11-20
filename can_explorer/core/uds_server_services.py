"""The services of the simulated UDS server (core/simulation.py UdsServer) beyond the basic ones: the
ReadDTCInformation reports, ReadScalingDataByIdentifier, CommunicationControl, ControlDTCSetting,
DynamicallyDefineDataIdentifier, ReadDataByPeriodicIdentifier, RequestFileTransfer, ResponseOnEvent,
LinkControl and SecuredDataTransmission. A mixin of UdsServer; the simulated ECU's own choices (rates, directory format) are documented here, they
are not taken from a standard.
"""

from typing import Dict, List, Optional, Tuple

from can_explorer.core import uds_secured
from can_explorer.core.uds import NEGATIVE_RESPONSE

# The simulator's periodic rates in seconds (sendAtSlowRate, Medium, Fast): its own choice, ECUs configure theirs
PERIODIC_RATES = {0x01: 1.0, 0x02: 0.2, 0x03: 0.05}
# Negative responses an ECU does not send to a functional request (ISO 14229-1)
FUNCTIONAL_SILENT_CODES = (0x11, 0x12, 0x31, 0x7E, 0x7F)


class ExtendedServices:
    """State and handlers of the extended services; UdsServer calls `_init_extended_services`."""

    def _init_extended_services(self) -> None:
        # ReadDTCInformation detail: per DTC its snapshot records {number: [(did, data)]}, extended data records
        # {number: data}, fault detection counter, severity; user-defined memories {selection: [(code, status)]}
        # and permanent (WWH-OBD) DTCs
        self.snapshots: Dict[int, Dict[int, List[Tuple[int, bytes]]]] = {
            0x012345: {0x01: [(0x0100, b'\x0b\xb8'), (0x0101, b'\x5a')], 0x02: [(0x0100, b'\x00\x00')]}
        }
        self.extended_data: Dict[int, Dict[int, bytes]] = {0x012345: {0x01: b'\x03', 0x02: b'\x10\x20'}}
        self.fault_counters: Dict[int, int] = {0x012345: 127, 0xC10000: -20}
        self.severities: Dict[int, int] = {0x012345: 0x40, 0xC10000: 0x20}
        self.user_memories: Dict[int, List[Tuple[int, int]]] = {0x01: [(0x9A0011, 0x2F)]}
        self.permanent_dtcs: List[Tuple[int, int]] = [(0x012345, 0x08)]
        self.scaling: Dict[int, bytes] = {0x0100: bytes([0x02, 0xA1, 0x20])}  # Unsigned 2 bytes, a unit/format byte
        self.communication = {'control': 0x00, 'type': 0x03}
        self.dtc_setting_on = True
        self.link_mode: Optional[int] = None
        self.link_transitions = 0
        # Dynamically defined DIDs: DDDID -> [('did', did, position, size) | ('memory', address, size)]
        self.dynamic_dids: Dict[int, List[Tuple]] = {}
        self.periodic: Dict[int, int] = {}  # PDID -> transmission mode
        self.files: Dict[str, bytes] = {'/logs/trip.txt': b'trip 1\n'}
        self._file_transfer: Optional[Dict] = None
        self.events: List[Dict] = []  # ResponseOnEvent set up
        self.events_started = False
        self.outbox: List[bytes] = []  # Event-triggered responses waiting to be sent
        self._inside_secured = False  # Handling the internal message of a SecuredDataTransmission

    def _extended_handlers(self):
        return {
            0x24: self._read_scaling,
            0x28: self._communication_control,
            0x2A: self._read_periodic,
            0x2C: self._dynamically_define,
            0x38: self._request_file_transfer,
            0x85: self._control_dtc_setting,
            0x86: self._response_on_event,
            0x87: self._link_control,
            0x84: self._secured_data_transmission,
        }

    def _reset_extended_state(self) -> None:
        """A return to the default session (or a reset) ends communication control, DTC setting and periodic data."""
        self.communication = {'control': 0x00, 'type': 0x03}
        self.dtc_setting_on = True
        self.periodic.clear()
        self.link_mode = None

    def _end_transfers(self) -> None:
        """A session change (or a reset) ends an unfinished download, upload or file transfer."""
        self._download = None
        self._file_transfer = None

    # SecuredDataTransmission

    def _needs_secured(self, did: int) -> bool:
        secured = getattr(self, 'secured', None)
        return secured is not None and did in secured.secured_dids and not self._inside_secured

    def _secured_data_transmission(self, request: bytes) -> List[bytes]:
        """Open the envelope (checking the counter and the signature, decrypting), handle the internal request and
        wrap each response in a 0xC4 envelope with the request's counter, signed when the request asked for it and
        encrypted when the request was. See uds_secured.SecuredServerConfig for the simulator's choices."""
        config = self.secured
        if config is None:
            return self._negative(0x84, 0x11)
        if self._inside_secured:
            return self._negative(0x84, uds_secured.SECURE_DATA_TRANSMISSION_NOT_ALLOWED)  # Nested envelope
        try:
            message = uds_secured.parse(request)
        except uds_secured.SecuredDataError:
            return self._negative(0x84, 0x13)
        header = message.header
        if not header.request:
            return self._negative(0x84, 0x13)
        if config.require_signature and not header.signed:
            return self._negative(0x84, uds_secured.SECURE_DATA_VERIFICATION_FAILED)
        if not config.fresh(header.counter):
            return self._negative(0x84, uds_secured.SECURE_DATA_VERIFICATION_FAILED)  # A replayed counter
        provider = config.provider
        if header.pre_established_key or config.require_session_key:
            # The session key of the last authentication (communicationConfiguration with a session key)
            authentication = getattr(self, 'authentication', None)
            provider = getattr(authentication, 'session_provider', None) if authentication is not None else None
            if provider is None or not header.pre_established_key:
                return self._negative(0x84, uds_secured.SECURE_DATA_VERIFICATION_FAILED)
        try:
            opened = uds_secured.open_message(request, provider)
        except uds_secured.SecuredDataError:
            return self._negative(0x84, uds_secured.SECURE_DATA_VERIFICATION_FAILED)
        if not opened.internal:
            return self._negative(0x84, 0x13)
        config.last_counter = header.counter
        self._inside_secured = True
        try:
            responses = self._handle(opened.internal)
        finally:
            self._inside_secured = False
        try:
            return [
                uds_secured.wrap(
                    response,
                    header.counter,
                    request=False,
                    provider=provider,
                    sign=header.signature_on_response_requested,
                    encrypt=header.encrypted,
                    signature_on_response=header.signature_on_response_requested,
                    algorithm=header.algorithm,
                    pre_established_key=header.pre_established_key,
                )
                for response in responses
            ]
        except uds_secured.SecuredDataError:
            return self._negative(0x84, uds_secured.SECURE_DATA_TRANSMISSION_NOT_ALLOWED)  # Cannot protect it

    # ReadDTCInformation

    def _dtc_status(self, code: int) -> Optional[int]:
        return next((status for dtc, status in self.dtcs if dtc == code), None)

    def _read_dtc_information(self, request: bytes) -> List[bytes]:
        sub = request[1]
        head = bytes([0x59, sub])
        records = lambda dtcs: b''.join(code.to_bytes(3, 'big') + bytes([status]) for code, status in dtcs)  # noqa: E731
        if sub in (0x01, 0x02):
            if len(request) != 3:
                return self._negative(0x19, 0x13)
            matching = [(code, status) for code, status in self.dtcs if status & request[2]]
            if sub == 0x02:
                return [head + bytes([0xFF]) + records(matching)]
            return [head + bytes([0xFF, 0x01]) + len(matching).to_bytes(2, 'big')]
        if sub == 0x07:
            if len(request) != 4:
                return self._negative(0x19, 0x13)
            count = sum(
                1 for code, status in self.dtcs if status & request[3] and self.severities.get(code, 0) & request[2]
            )
            return [head + bytes([0xFF, 0x01]) + count.to_bytes(2, 'big')]
        if sub == 0x0A:
            return [head + bytes([0xFF]) + records(self.dtcs)]
        if sub == 0x03:
            return [
                head
                + b''.join(
                    code.to_bytes(3, 'big') + bytes([number])
                    for code in self.snapshots
                    for number in self.snapshots[code]
                )
            ]
        if sub == 0x14:
            return [
                head
                + b''.join(
                    code.to_bytes(3, 'big') + bytes([value & 0xFF]) for code, value in self.fault_counters.items()
                )
            ]
        if sub in (0x04, 0x06, 0x18, 0x19):
            memory = sub in (0x18, 0x19)
            if len(request) != (7 if memory else 6):
                return self._negative(0x19, 0x13)
            code, number = int.from_bytes(request[2:5], 'big'), request[5]
            if memory:
                dtcs = dict(self.user_memories.get(request[6], []))
                status = dtcs.get(code)
            else:
                status = self._dtc_status(code)
            if status is None:
                return self._negative(0x19, 0x31)
            body = (bytes([request[6]]) if memory else b'') + code.to_bytes(3, 'big') + bytes([status])
            if sub in (0x04, 0x18):
                for record, values in (self.snapshots.get(code, {}) if not memory else {}).items():
                    if number in (0xFF, record):
                        body += bytes([record, len(values)])
                        body += b''.join(did.to_bytes(2, 'big') + data for did, data in values)
            else:
                for record, data in (self.extended_data.get(code, {}) if not memory else {}).items():
                    if number in (0xFF, 0xFE, record):
                        body += bytes([record]) + data
            return [head + body]
        if sub == 0x17:
            if len(request) != 4:
                return self._negative(0x19, 0x13)
            dtcs = [(code, status) for code, status in self.user_memories.get(request[3], []) if status & request[2]]
            return [head + bytes([request[3], 0xFF]) + records(dtcs)]
        if sub == 0x42:
            if len(request) != 5:
                return self._negative(0x19, 0x13)
            body = bytes([request[2], 0xFF, 0xE0, 0x04])
            for code, status in self.dtcs:
                severity = self.severities.get(code, 0)
                if status & request[3] and (severity & request[4] or request[4] == 0xFF):
                    body += bytes([severity]) + code.to_bytes(3, 'big') + bytes([status])
            return [head + body]
        if sub == 0x55:
            if len(request) != 3:
                return self._negative(0x19, 0x13)
            return [head + bytes([request[2], 0xFF, 0x04]) + records(self.permanent_dtcs)]
        return self._negative(0x19, 0x12)

    # Scaling, communication, DTC setting, link control

    def _read_scaling(self, request: bytes) -> List[bytes]:
        if len(request) != 3:
            return self._negative(0x24, 0x13)
        did = int.from_bytes(request[1:3], 'big')
        if did not in self.scaling:
            return self._negative(0x24, 0x31)
        return [bytes([0x64]) + request[1:3] + self.scaling[did]]

    def _communication_control(self, request: bytes) -> List[bytes]:
        if len(request) < 3:
            return self._negative(0x28, 0x13)
        control = request[1] & 0x7F
        if control > 0x05:
            return self._negative(0x28, 0x12)
        if self.session == 0x01:
            return self._negative(0x28, 0x7F)
        if control in (0x04, 0x05) and len(request) != 5:
            return self._negative(0x28, 0x13)
        self.communication = {'control': control, 'type': request[2]}
        return [bytes([0x68, control])]

    def _control_dtc_setting(self, request: bytes) -> List[bytes]:
        setting = request[1] & 0x7F
        if setting not in (0x01, 0x02):
            return self._negative(0x85, 0x12)
        if self.session == 0x01:
            return self._negative(0x85, 0x7F)
        self.dtc_setting_on = setting == 0x01
        return [bytes([0xC5, setting])]

    def _link_control(self, request: bytes) -> List[bytes]:
        control = request[1] & 0x7F
        if control == 0x01:
            if len(request) != 3:
                return self._negative(0x87, 0x13)
            if request[2] not in (0x10, 0x11, 0x12, 0x13):
                return self._negative(0x87, 0x31)
            self.link_mode = request[2]
        elif control == 0x02:
            if len(request) != 5:
                return self._negative(0x87, 0x13)
            self.link_mode = int.from_bytes(request[2:5], 'big')
        elif control == 0x03:
            if self.link_mode is None:
                return self._negative(0x87, 0x24)
            self.link_transitions += 1
        else:
            return self._negative(0x87, 0x12)
        return [bytes([0xC7, control])]

    # Dynamically defined and periodic data

    def _read_memory(self, address: int, size: int) -> bytes:
        for start, data in self.memory.items():
            if start <= address and address + size <= start + len(data):
                return data[address - start : address - start + size]
        return bytes(size)

    def _dynamic_data(self, dddid: int) -> Optional[bytes]:
        if dddid not in self.dynamic_dids:
            return None
        data = b''
        for source in self.dynamic_dids[dddid]:
            if source[0] == 'did':
                _, did, position, size = source
                value = self.dids.get(did, b'')
                data += value[position - 1 : position - 1 + size]
            else:
                _, address, size = source
                data += self._read_memory(address, size)
        return data

    def _dynamically_define(self, request: bytes) -> List[bytes]:
        if len(request) < 2:
            return self._negative(0x2C, 0x13)
        sub = request[1] & 0x7F
        if sub == 0x03:
            if len(request) == 4:
                self.dynamic_dids.pop(int.from_bytes(request[2:4], 'big'), None)
            elif len(request) == 2:
                self.dynamic_dids.clear()
            else:
                return self._negative(0x2C, 0x13)
            return [bytes([0x6C, 0x03]) + bytes(request[2:4])]
        if len(request) < 5:
            return self._negative(0x2C, 0x13)
        dddid = int.from_bytes(request[2:4], 'big')
        if not 0xF200 <= dddid <= 0xF3FF:
            return self._negative(0x2C, 0x31)
        sources = self.dynamic_dids.setdefault(dddid, [])
        if sub == 0x01:
            elements = request[4:]
            if len(elements) % 4:
                return self._negative(0x2C, 0x13)
            for i in range(0, len(elements), 4):
                did = int.from_bytes(elements[i : i + 2], 'big')
                if did not in self.dids:
                    return self._negative(0x2C, 0x31)
                sources.append(('did', did, elements[i + 2], elements[i + 3]))
        elif sub == 0x02:
            address_bytes, size_bytes = request[4] & 0x0F, request[4] >> 4
            areas, step = request[5:], address_bytes + size_bytes
            if not address_bytes or not size_bytes or not areas or len(areas) % step:
                return self._negative(0x2C, 0x13)
            for i in range(0, len(areas), step):
                address = int.from_bytes(areas[i : i + address_bytes], 'big')
                sources.append(('memory', address, int.from_bytes(areas[i + address_bytes : i + step], 'big')))
        else:
            return self._negative(0x2C, 0x12)
        return [bytes([0x6C, sub]) + request[2:4]]

    def periodic_data(self, pdid: int) -> Optional[bytes]:
        """The data of a scheduled PDID (a static 0xF2xx DID or a dynamically defined one)."""
        did = 0xF200 | pdid
        return self._dynamic_data(did) if did in self.dynamic_dids else self.dids.get(did)

    def _read_periodic(self, request: bytes) -> List[bytes]:
        if len(request) < 2:
            return self._negative(0x2A, 0x13)
        mode, pdids = request[1], list(request[2:])
        if mode == 0x04:
            for pdid in pdids or list(self.periodic):
                self.periodic.pop(pdid, None)
            return [bytes([0x6A])]
        if mode not in PERIODIC_RATES or not pdids:
            return self._negative(0x2A, 0x31 if pdids else 0x13)
        if self.session == 0x01:
            return self._negative(0x2A, 0x7F)
        if any(self.periodic_data(pdid) is None for pdid in pdids):
            return self._negative(0x2A, 0x31)
        for pdid in pdids:
            self.periodic[pdid] = mode
        return [bytes([0x6A])]

    # RequestFileTransfer

    def _request_file_transfer(self, request: bytes) -> List[bytes]:
        if len(request) < 5:
            return self._negative(0x38, 0x13)
        mode, length = request[1], int.from_bytes(request[2:4], 'big')
        if len(request) < 4 + length:
            return self._negative(0x38, 0x13)
        path = bytes(request[4 : 4 + length]).decode('ascii', 'replace')
        rest = request[4 + length :]
        block = self.max_block_length.to_bytes(2, 'big')
        if mode == 0x02:
            if path not in self.files:
                return self._negative(0x38, 0x31)
            del self.files[path]
            return [bytes([0x78, 0x02])]
        if mode in (0x01, 0x03):
            if len(rest) < 2 or rest[0] != 0x00:
                return self._negative(0x38, 0x31)  # No compression or encryption
            width = rest[1]
            if len(rest) != 2 + 2 * width:
                return self._negative(0x38, 0x13)
            if (mode == 0x01) == (path in self.files):
                return self._negative(0x38, 0x22)  # Add an existing file / replace a missing one
            size = int.from_bytes(rest[2 : 2 + width], 'big')
            self._download = {'address': None, 'path': path, 'size': size, 'data': bytearray(), 'counter': 1}
            return [bytes([0x78, mode, 0x02]) + block + bytes([0x00])]
        if mode in (0x04, 0x05):
            if mode == 0x04:
                if path not in self.files:
                    return self._negative(0x38, 0x31)
                data = self.files[path]
            else:
                prefix = path.rstrip('/') + '/'
                names = sorted(name for name in self.files if name.startswith(prefix))
                if not names:
                    return self._negative(0x38, 0x31)
                data = '\n'.join(names).encode('ascii')  # The simulator's own directory format: one path per line
            self._file_transfer = {'data': data, 'offset': 0, 'counter': 1}
            size = len(data).to_bytes(4, 'big')
            response = bytes([0x78, mode, 0x02]) + block + bytes([0x00, 0x00, 0x04]) + size
            return [response + (size if mode == 0x04 else b'')]
        return self._negative(0x38, 0x31)

    def _upload_block(self, request: bytes) -> Optional[List[bytes]]:
        """TransferData during a file read: the next block to the tester (None: no read in progress)."""
        transfer = self._file_transfer
        if transfer is None:
            return None
        if len(request) != 2:
            return self._negative(0x36, 0x13)
        if request[1] != transfer['counter']:
            return self._negative(0x36, 0x73)
        chunk = self.max_block_length - 2
        data = transfer['data'][transfer['offset'] : transfer['offset'] + chunk]
        if not data and transfer['offset']:
            return self._negative(0x36, 0x24)
        transfer['offset'] += len(data)
        transfer['counter'] = (transfer['counter'] + 1) & 0xFF
        return [bytes([0x76, request[1]]) + data]

    def _finish_file_transfer(self) -> Optional[List[bytes]]:
        """RequestTransferExit of a file transfer (None: none in progress)."""
        if self._file_transfer is not None:
            self._file_transfer = None
            return [bytes([0x77])]
        download = self._download
        if download is not None and download.get('path') is not None:
            if len(download['data']) != download['size']:
                return self._negative(0x37, 0x24)
            self.files[download['path']] = bytes(download['data'])
            self._download = None
            return [bytes([0x77])]
        return None

    # ResponseOnEvent

    _EVENT_RECORD_LENGTHS = {0x01: 1, 0x03: 2, 0x07: 10}

    def _response_on_event(self, request: bytes) -> List[bytes]:
        if len(request) < 3:
            return self._negative(0x86, 0x13)
        sub = request[1] & 0x3F
        window = request[2]
        if sub in self._EVENT_RECORD_LENGTHS:
            length = self._EVENT_RECORD_LENGTHS[sub]
            record, service = bytes(request[3 : 3 + length]), bytes(request[3 + length :])
            if len(record) != length or not service:
                return self._negative(0x86, 0x13)
            self.events.append({'type': sub, 'window': window, 'record': record, 'service': service})
            return [bytes([0xC6, request[1] & 0x7F, 0x00, window]) + record + service]
        if sub == 0x05:
            if not self.events:
                return self._negative(0x86, 0x24)
            self.events_started = True
            return [bytes([0xC6, 0x05, len(self.events), window])]
        if sub == 0x00:
            self.events_started = False
            return [bytes([0xC6, 0x00, len(self.events), window])]
        if sub == 0x06:
            count = len(self.events)
            self.events.clear()
            self.events_started = False
            return [bytes([0xC6, 0x06, count, window])]
        if sub == 0x04:
            body = b''.join(bytes([e['type'], e['window']]) + e['record'] + e['service'] for e in self.events)
            return [bytes([0xC6, 0x04, len(self.events)]) + body]
        return self._negative(0x86, 0x12)

    def _event_responses(self, kind: int, match) -> None:
        if not self.events_started:
            return
        for event in self.events:
            if event['type'] == kind and match(event['record']):
                responses = self.handle(event['service'])
                self.outbox += [response for response in responses if response[0] != NEGATIVE_RESPONSE]

    def did_changed(self, did: int) -> None:
        self._event_responses(0x03, lambda record: int.from_bytes(record, 'big') == did)

    def set_dtc_status(self, code: int, status: int) -> None:
        """Change (or add) a DTC's status, triggering onDTCStatusChange events for the changed bits."""
        old = self._dtc_status(code) or 0
        self.dtcs = [(dtc, value) for dtc, value in self.dtcs if dtc != code] + [(code, status)]
        changed = old ^ status
        self._event_responses(0x01, lambda record: bool(record[0] & changed))
