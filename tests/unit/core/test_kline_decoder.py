import can

from can_explorer.core.protocols import create_protocol_decoder
from can_explorer.core.protocols.kline import KLineDecoder, describe_kline


def _msg(data: bytes) -> can.Message:
    return can.Message(arbitration_id=0x686AF1, is_extended_id=True, data=data)


def test_registered():
    decoder = create_protocol_decoder('K-Line')
    assert isinstance(decoder, KLineDecoder)


def test_describe_kwp_service():
    # StartDiagnosticSession request and its positive response (KWP2000, ISO 14230)
    assert describe_kline(bytes([0x10, 0x81])) == 'StartDiagnosticSession 81'
    assert describe_kline(bytes([0x50, 0x81])) == 'Positive response to StartDiagnosticSession: 81'
    # Negative response: service not supported
    assert describe_kline(bytes([0x7F, 0x10, 0x11])).startswith('Negative response to StartDiagnosticSession')


def test_describe_obd_service():
    # OBD-II/J1979 mode 0x01 PID 0x0C (engine speed) response, decoded via the shared J1979 describer
    description = describe_kline(bytes([0x41, 0x0C, 0x0C, 0x6B]))
    assert 'rpm' in description.lower()


def test_describe_empty_and_unknown():
    assert describe_kline(b'') is None
    assert describe_kline(bytes([0x00])) is None  # Neither a KWP nor a J1979 service


def test_decoder_prefixes_and_passes_through_none():
    decoder = KLineDecoder()
    assert decoder.describe(_msg(bytes([0x10, 0x81]))) == 'K-Line StartDiagnosticSession 81'
    assert decoder.describe(_msg(b'')) is None
    decoder.reset()  # No state, must not raise
