import pytest

from can_explorer.core.elm327_bluetooth import is_bluetooth_channel, parse_channel


def test_is_bluetooth_channel():
    assert is_bluetooth_channel('bt://AA:BB:CC:DD:EE:FF')
    assert is_bluetooth_channel('rfcomm://aa:bb:cc:dd:ee:ff')
    assert is_bluetooth_channel('AA:BB:CC:DD:EE:FF')  # A bare MAC
    assert not is_bluetooth_channel('/dev/ttyUSB0')
    assert not is_bluetooth_channel('COM4')
    assert not is_bluetooth_channel('socket://192.168.0.10:35000')


def test_parse_channel():
    assert parse_channel('bt://AA:BB:CC:DD:EE:FF') == ('AA:BB:CC:DD:EE:FF', 1)
    assert parse_channel('bt://aa:bb:cc:dd:ee:ff/3') == ('AA:BB:CC:DD:EE:FF', 3)
    assert parse_channel('11:22:33:44:55:66') == ('11:22:33:44:55:66', 1)
    for bad in ('bt://nope', 'bt://AA:BB', '/dev/ttyUSB0'):
        with pytest.raises(ValueError):
            parse_channel(bad)
