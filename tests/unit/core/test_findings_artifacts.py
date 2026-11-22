import pytest

from can_explorer.core.reverse.findings_artifacts import gateway_map_to_dbc, match_to_script
from can_explorer.core.reverse.gateway_security import EcuNode, GatewayMap
from can_explorer.core.seedkey_crack import crack


def _pairs(fn, length=4):
    seeds = [bytes((i, i + 1, i + 2, i + 3))[:length] for i in range(1, 13)]
    return [(s, fn(s)) for s in seeds]


def _script_fn(source):
    namespace = {}
    exec(compile(source, 'generated', 'exec'), namespace)  # noqa: S102 - exercising generated code in a test
    return namespace['compute_key']


def _roundtrip(fn):
    """Crack a known transform, serialise the match, and check the script reproduces the key on a fresh seed."""
    pairs = _pairs(fn)
    report = crack(pairs)
    match = report.matches[0]
    compute = _script_fn(match_to_script(match))
    seed = b'\x09\x08\x07\x06'
    return compute(1, seed), fn(seed), match


def test_gateway_map_to_dbc_is_loadable():
    gmap = GatewayMap(
        ecus=[
            EcuNode(0x7E0, 0x7E8, [0x01, 0x03], 'seed_key', [0x22, 0x27], 2.0),
            EcuNode(0x7E1, 0x7E9, [0x01], 'none', [0x22], 1.0),
        ],
        segments={},
        diagnostic_ids=[0x7E0, 0x7E1],
        functional_ids_seen=[0x7DF],
    )
    dbc = gateway_map_to_dbc(gmap)
    assert 'BO_ 2016 ECU_7E0_Request: 8 Tester' in dbc  # 0x7E0 == 2016
    assert 'BO_ 2024 ECU_7E0_Response: 8 DiscoveredGateway' in dbc
    assert 'SecurityAccess' in dbc  # Service 0x27 named in a comment

    import cantools

    db = cantools.database.load_string(dbc, database_format='dbc')
    assert {m.frame_id for m in db.messages} == {0x7E0, 0x7E8, 0x7E1, 0x7E9}


def test_script_roundtrip_constant_xor():
    const = b'\xaa\xbb\xcc\xdd'
    got, expected, match = _roundtrip(lambda s: bytes(a ^ b for a, b in zip(s, const, strict=False)))
    assert match.algorithm == 'constant XOR'
    assert got == expected


def test_script_roundtrip_per_byte_add():
    mask = b'\x01\x02\x03\x04'
    got, expected, _ = _roundtrip(lambda s: bytes((a + b) & 0xFF for a, b in zip(s, mask, strict=False)))
    assert got == expected


def test_script_roundtrip_identity_and_complement():
    assert _roundtrip(lambda s: bytes(s))[0] == b'\x09\x08\x07\x06'
    got, expected, _ = _roundtrip(lambda s: bytes(b ^ 0xFF for b in s))
    assert got == expected


def test_script_roundtrip_rotation():
    def rot_left(s):
        width = len(s) * 8
        n = int.from_bytes(s, 'big')
        n = ((n << 3) | (n >> (width - 3))) & ((1 << width) - 1)
        return n.to_bytes(len(s), 'big')

    got, expected, match = _roundtrip(rot_left)
    assert 'rotate' in match.algorithm
    assert got == expected


def test_unserialisable_match_raises():
    from can_explorer.core.seedkey_crack import Match

    gf2 = Match(
        'GF(2) linear', 'matrix', {'matrix_hex': '00', 'constant_hex': '00', 'bits': 32}, 10, 2, lambda _l, s: s
    )
    with pytest.raises(ValueError, match='Cannot serialise'):
        match_to_script(gf2)
