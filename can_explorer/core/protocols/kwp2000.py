"""Passive Keyword Protocol 2000 (KWP2000, ISO 14230) decoder: the diagnostic protocol that preceded UDS, still
found in vehicles from roughly 1996–2006. Over CAN it uses the same ISO 15765-2 (ISO-TP) transport as UDS and the
same request/response convention — a positive response echoes the service id plus 0x40, a negative response is
0x7F followed by the service id and a response code — but with the older ISO 14230-3 service set and response codes.

Point it at the diagnostic identifiers of a bus; the reassembled ISO-TP payloads are named by their KWP service.
"""

from typing import Optional

import can

from can_explorer.core.frames import format_hex

NEGATIVE_RESPONSE = 0x7F

# ISO 14230-3 (KWP2000) services
SERVICES = {
    0x10: 'StartDiagnosticSession',
    0x11: 'ECUReset',
    0x12: 'ReadFreezeFrameData',
    0x13: 'ReadDiagnosticTroubleCodes',
    0x14: 'ClearDiagnosticInformation',
    0x17: 'ReadStatusOfDTC',
    0x18: 'ReadDTCByStatus',
    0x1A: 'ReadECUIdentification',
    0x20: 'StopDiagnosticSession',
    0x21: 'ReadDataByLocalIdentifier',
    0x22: 'ReadDataByCommonIdentifier',
    0x23: 'ReadMemoryByAddress',
    0x25: 'StopRepeatedDataTransmission',
    0x26: 'SetDataRates',
    0x27: 'SecurityAccess',
    0x28: 'DisableNormalMessageTransmission',
    0x29: 'EnableNormalMessageTransmission',
    0x2C: 'DynamicallyDefineLocalIdentifier',
    0x2E: 'WriteDataByCommonIdentifier',
    0x2F: 'InputOutputControlByCommonIdentifier',
    0x30: 'InputOutputControlByLocalIdentifier',
    0x31: 'StartRoutineByLocalIdentifier',
    0x32: 'StopRoutineByLocalIdentifier',
    0x33: 'RequestRoutineResultsByLocalIdentifier',
    0x34: 'RequestDownload',
    0x35: 'RequestUpload',
    0x36: 'TransferData',
    0x37: 'RequestTransferExit',
    0x38: 'StartRoutineByAddress',
    0x39: 'StopRoutineByAddress',
    0x3A: 'RequestRoutineResultsByAddress',
    0x3B: 'WriteDataByLocalIdentifier',
    0x3D: 'WriteMemoryByAddress',
    0x3E: 'TesterPresent',
    0x81: 'StartCommunication',
    0x82: 'StopCommunication',
    0x83: 'AccessTimingParameter',
}

# ISO 14230-3 negative response codes
NEGATIVE_RESPONSE_CODES = {
    0x10: 'generalReject',
    0x11: 'serviceNotSupported',
    0x12: 'subFunctionNotSupported-invalidFormat',
    0x21: 'busy-repeatRequest',
    0x22: 'conditionsNotCorrect-requestSequenceError',
    0x23: 'routineNotComplete-serviceInProgress',
    0x31: 'requestOutOfRange',
    0x33: 'securityAccessDenied',
    0x35: 'invalidKey',
    0x36: 'exceedNumberOfAttempts',
    0x37: 'requiredTimeDelayNotExpired',
    0x40: 'downloadNotAccepted',
    0x41: 'improperDownloadType',
    0x42: 'cantDownloadToSpecifiedAddress',
    0x43: 'cantDownloadNumberOfBytesRequested',
    0x50: 'uploadNotAccepted',
    0x51: 'improperUploadType',
    0x71: 'transferSuspended',
    0x72: 'generalProgrammingFailure',
    0x73: 'wrongBlockSequenceCounter',
    0x78: 'requestCorrectlyReceived-responsePending',
    0x7E: 'subFunctionNotSupportedInActiveSession',
    0x7F: 'serviceNotSupportedInActiveSession',
}


def service_name(sid: int) -> str:
    return SERVICES.get(sid, f'service 0x{sid:02X}')


def nrc_name(code: int) -> str:
    return NEGATIVE_RESPONSE_CODES.get(code, f'0x{code:02X}')


def describe_payload(payload: bytes) -> str:
    """Describe a KWP2000 payload of unknown direction (passive decoding)."""
    if not payload:
        return ''
    if payload[0] == NEGATIVE_RESPONSE and len(payload) >= 3:
        return f'Negative response to {service_name(payload[1])}: {nrc_name(payload[2])}'
    rest = payload[1:]
    if payload[0] in SERVICES:
        return service_name(payload[0]) + (f' {format_hex(rest)}' if rest else '')
    if (payload[0] - 0x40) in SERVICES:
        return f'Positive response to {service_name(payload[0] - 0x40)}' + (f': {format_hex(rest)}' if rest else '')
    return ''


class Kwp2000Decoder:
    """Passive decoder: ISO-TP on CAN with the reassembled payloads described as KWP2000 (ISO 14230) services."""

    name = 'KWP2000'

    def __init__(self):
        from can_explorer.core.protocols.isotp import IsoTpDecoder

        self._isotp = IsoTpDecoder()

    def reset(self) -> None:
        self._isotp.reset()

    def describe(self, msg: can.Message) -> Optional[str]:
        description, payload = self._isotp.feed(msg)
        if payload is None:
            return description
        kwp = describe_payload(payload)
        return f'{description} | KWP {kwp}' if kwp else description
