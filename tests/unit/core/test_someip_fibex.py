import pytest

from can_explorer.core.someip_fibex import FibexError, load_fibex


@pytest.fixture(scope='module')
def description(data_dir):
    return load_fibex(data_dir / 'someip.fibex')


def test_service_and_message_names(description):
    assert description.service_names == {0x1234: 'EngineService'}
    assert description.message(0x1234, 0x8001).name == 'SpeedEvent'
    assert len(description) >= 6  # GetSpeed request + response, four events


def test_decode_scalar_event(description):
    assert description.decode(0x1234, 0x8001, bytes([0x0F, 0xA0])) == {'speed': 0x0FA0}


def test_method_request_and_response_differ(description):
    assert description.decode(0x1234, 0x0045, bytes([0x02]), is_response=False) == {'req': 2}
    assert description.decode(0x1234, 0x0045, bytes([0x0F, 0xA0]), is_response=True) == {'speed': 0x0FA0}


def test_decode_nested_struct(description):
    assert description.decode(0x1234, 0x8002, bytes([0x03, 0xFF, 0xFB])) == {'status.flags': 3, 'status.temp': -5}


def test_decode_dynamic_array(description):
    payload = (4).to_bytes(4, 'big') + bytes([0x00, 0x01, 0x00, 0x02])
    assert description.decode(0x1234, 0x8003, payload) == {'samples[0]': 1, 'samples[1]': 2}


def test_decode_dynamic_string(description):
    payload = (3).to_bytes(4, 'big') + b'ABC'
    assert description.decode(0x1234, 0x8004, payload) == {'name': 'ABC'}


def test_decode_union(description):
    # Reading union: length(4) + type(4) + value; type 1 selects the uint16 alternative `as_count`.
    payload = (2).to_bytes(4, 'big') + (1).to_bytes(4, 'big') + bytes([0x01, 0x02])
    assert description.decode(0x1234, 0x8005, payload) == {'reading.as_count': 0x0102}


def test_decode_tlv_optional_members(description):
    def tag(wire_type, data_id):
        return ((wire_type << 12) | data_id).to_bytes(2, 'big')

    # Config struct: members carry DATA-IDs, so they are tagged and optional.
    both = tag(1, 1) + bytes([0x0F, 0xA0]) + tag(0, 2) + bytes([0x07])
    assert description.decode(0x1234, 0x8006, both) == {'config.speed': 0x0FA0, 'config.mode': 0x07}
    # `mode` omitted: only the present member is decoded.
    assert description.decode(0x1234, 0x8006, tag(1, 1) + bytes([0x0F, 0xA0])) == {'config.speed': 0x0FA0}


def test_errors(data_dir, tmp_path):
    not_fibex = tmp_path / 'x.fibex'
    not_fibex.write_text('<something/>')
    with pytest.raises(FibexError, match='No SOME/IP service interfaces'):
        load_fibex(not_fibex)
    broken = tmp_path / 'broken.fibex'
    broken.write_text('<FIBEX><ELEMENTS')
    with pytest.raises(FibexError, match='Invalid XML'):
        load_fibex(broken)
