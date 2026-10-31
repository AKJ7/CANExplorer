import struct

import pytest

from can_explorer.core.someip_arxml import ArxmlError, load_arxml


@pytest.fixture(scope='module')
def description(data_dir):
    return load_arxml(data_dir / 'someip.arxml')


def test_service_and_message_names(description):
    assert description.service_names == {0x1234: 'RadarService'}
    assert description.message(0x1234, 0x0001).name == 'Configure'
    assert description.message(0x1234, 0x8001).name == 'Target'
    assert len(description) == 3  # Configure request + response, Target event


def test_method_request_and_response_differ(description):
    assert description.decode(0x1234, 0x0001, bytes([0x05]), is_response=False) == {'mode': 5}
    assert description.decode(0x1234, 0x0001, bytes([0x00, 0x2A]), is_response=True) == {'status': 42}


def test_decode_struct_event(description):
    payload = struct.pack('>ff', 1.0, 2.0)  # Position { float32 x, float32 y } as a big-endian pair
    assert description.decode(0x1234, 0x8001, payload) == {'Target.x': 1.0, 'Target.y': 2.0}


def test_errors(tmp_path):
    not_someip = tmp_path / 'x.arxml'
    not_someip.write_text('<AUTOSAR/>')
    with pytest.raises(ArxmlError, match='No SOME/IP service interface deployments'):
        load_arxml(not_someip)
    broken = tmp_path / 'broken.arxml'
    broken.write_text('<AUTOSAR><AR-PACKAGES')
    with pytest.raises(ArxmlError, match='Invalid XML'):
        load_arxml(broken)
