import can

from can_explorer.core.protocols.kwp2000 import Kwp2000Decoder, describe_payload


def _single_frame(payload):
    """An ISO-TP single frame (first nibble 0, low nibble = length) carrying a KWP payload."""
    body = bytes([len(payload)]) + bytes(payload)
    return can.Message(arbitration_id=0x7E0, is_extended_id=False, data=body + bytes(8 - len(body)))


def test_describe_payload_request_response_negative():
    assert describe_payload(bytes([0x10, 0x81])) == 'StartDiagnosticSession 81'
    assert describe_payload(bytes([0x50, 0x81])) == 'Positive response to StartDiagnosticSession: 81'
    assert describe_payload(bytes([0x21, 0x01])) == 'ReadDataByLocalIdentifier 01'
    assert (
        describe_payload(bytes([0x7F, 0x21, 0x11]))
        == 'Negative response to ReadDataByLocalIdentifier: serviceNotSupported'
    )
    assert describe_payload(bytes([0x7F, 0x27, 0x35])) == 'Negative response to SecurityAccess: invalidKey'
    assert describe_payload(b'') == ''


def test_decoder_reassembles_isotp():
    decoder = Kwp2000Decoder()
    description = decoder.describe(_single_frame([0x3E]))  # TesterPresent
    assert description == 'ISO-TP SF (1 B): 3E | KWP TesterPresent'
    # An unknown service id leaves the ISO-TP description without a KWP suffix
    assert decoder.describe(_single_frame([0x99, 0x00])) == 'ISO-TP SF (2 B): 99 00'
