import can
import pytest

from can_explorer.core.protocols.canopen import CanOpenDecoder


def msg(can_id, data=()):
    return can.Message(arbitration_id=can_id, is_extended_id=False, data=bytes(data))


@pytest.mark.parametrize(
    'can_id, data, expected',
    [
        (0x000, [0x01, 0x00], 'NMT Start -> all nodes'),
        (0x000, [0x81, 0x05], 'NMT Reset Node -> node 5'),
        (0x080, [], 'SYNC'),
        (0x085, [0x10, 0x81, 0x11, 0, 0, 0, 0, 0], 'EMCY node 5: error 0x8110 register 0x11'),
        (0x185, [1, 2], 'TPDO1 node 5: 01 02'),
        (0x605, [0x40, 0x18, 0x10, 0x01, 0, 0, 0, 0], 'SDO request node 5: read 0x1018:01'),
        (
            0x585,
            [0x43, 0x18, 0x10, 0x01, 0x78, 0x56, 0x34, 0x12],
            'SDO response node 5: read response 0x1018:01 = 78 56 34 12',
        ),
        (0x585, [0x4F, 0x00, 0x10, 0x00, 0x91, 0, 0, 0], 'SDO response node 5: read response 0x1000:00 = 91'),
        (0x605, [0x2B, 0x17, 0x10, 0x00, 0xE8, 0x03, 0, 0], 'SDO request node 5: write 0x1017:00 = E8 03'),
        (
            0x585,
            [0x80, 0x00, 0x20, 0x00, 0x00, 0x00, 0x02, 0x06],
            'SDO response node 5: abort 0x2000:00 code 0x06020000',
        ),
        (0x705, [0x05], 'Heartbeat node 5: Operational'),
        (0x705, [0x00], 'Heartbeat node 5: Boot-up'),
    ],
)
def test_describe(can_id, data, expected):
    assert CanOpenDecoder().describe(msg(can_id, data)) == expected


def test_extended_frames_are_ignored():
    assert CanOpenDecoder().describe(can.Message(arbitration_id=0x185, is_extended_id=True)) is None
