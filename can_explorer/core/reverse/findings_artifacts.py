"""Turn reverse-engineering findings into reusable artifacts.

Two conversions:

- :func:`gateway_map_to_dbc` writes a minimal DBC database naming the request and response message of each ECU
  discovered in a capture, with the services seen listed as comments — a starting point to label unknown traffic
  in the signal views instead of re-deriving the topology by hand.
- :func:`match_to_script` turns a recovered seed/key :class:`~can_explorer.core.seedkey_crack.Match` into a
  self-contained ``compute_key(level, seed)`` Python script, ready to store as a seed/key entry in the key store
  (``KeyStore.add_seed_key``) and use for security access in the Diagnostics and Flashing views. Only the
  closed-form transforms that fully describe themselves are serialised; a key-dependent or table-based match raises,
  rather than emitting a script that silently drops part of the algorithm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from can_explorer.core.reverse.gateway_security import GatewayMap
    from can_explorer.core.seedkey_crack import Match


def gateway_map_to_dbc(gateway_map: 'GatewayMap', database_name: str = 'DiscoveredGateway') -> str:
    """A minimal DBC naming each discovered ECU's request and response message. The services seen are added as
    comments. The result is valid DBC text, loadable in the signal views as a scaffold to fill in."""
    from can_explorer.core.reverse.gateway_security import _SID_NAMES

    lines = [
        'VERSION ""',
        '',
        'NS_ :',
        '',
        'BS_:',
        '',
        f'BU_: Tester {database_name}',
        '',
    ]
    comments = []
    for node in gateway_map.ecus:
        req_name = f'ECU_{node.request_id:X}_Request'
        resp_name = f'ECU_{node.request_id:X}_Response'
        lines.append(f'BO_ {node.request_id} {req_name}: 8 Tester')
        lines.append(f'BO_ {node.response_id} {resp_name}: 8 {database_name}')
        lines.append('')
        if node.services_seen:
            services = ', '.join(_SID_NAMES.get(sid, f'0x{sid:02X}') for sid in node.services_seen)
            comments.append(f'CM_ BO_ {node.response_id} "Services seen: {services}";')
    lines.extend(comments)
    lines.append('')
    return '\n'.join(lines)


def match_to_script(match: 'Match') -> str:
    """Generate a ``compute_key(level, seed)`` script for a recovered seed/key algorithm. Raises ``ValueError`` for
    a match whose algorithm is key-dependent or table-based (AES/DES KDF, GF(2) linear, per-byte lookup, LFSR, CRC,
    hash truncation), which cannot be reduced to a short closed form here."""
    algorithm = match.algorithm
    params = match.parameters
    header = f'# {algorithm}: {match.description}\n\n\ndef compute_key(level, seed):\n'

    if algorithm == 'identity':
        return header + '    return bytes(seed)\n'
    if algorithm == 'identity (reversed)':
        return header + '    return bytes(seed)[::-1]\n'
    if algorithm == 'bitwise complement':
        return header + '    return bytes(b ^ 0xFF for b in seed)\n'
    if algorithm == 'constant XOR':
        return header + (
            f'    const = bytes.fromhex("{params["constant"]}")\n    return bytes(a ^ b for a, b in zip(seed, const))\n'
        )
    if algorithm in ('constant ADD', 'constant SUB'):
        delta_hex = params['delta']
        width = len(bytes.fromhex(delta_hex)) * 8
        op = '+' if algorithm == 'constant ADD' else '-'
        return header + (
            f'    delta = 0x{int(delta_hex, 16):X}\n'
            f'    n = (int.from_bytes(bytes(seed), "big") {op} delta) & {(1 << width) - 1}\n'
            f'    return n.to_bytes({width // 8}, "big")\n'
        )
    if algorithm == 'per-byte XOR':
        return header + (
            f'    mask = bytes.fromhex("{params["mask"]}")\n    return bytes(a ^ b for a, b in zip(seed, mask))\n'
        )
    if algorithm == 'per-byte ADD':
        return header + (
            f'    mask = bytes.fromhex("{params["mask"]}")\n'
            '    return bytes((a + b) & 0xFF for a, b in zip(seed, mask))\n'
        )
    if algorithm in ('rotate left', 'rotate right'):
        # The width is the seed length; emit a rotation that reads it from the seed at call time
        left = algorithm == 'rotate left'
        amount = int(params['amount'])
        op = '<<' if left else '>>'
        back = '>>' if left else '<<'
        return header + (
            f'    width = len(seed) * 8\n'
            f'    shift = {amount} % width\n'
            '    n = int.from_bytes(bytes(seed), "big")\n'
            f'    n = ((n {op} shift) | (n {back} (width - shift))) & ((1 << width) - 1)\n'
            '    return n.to_bytes(len(seed), "big")\n'
        )
    if algorithm == 'byte shuffle':
        return header + f'    perm = {list(params["permutation"])}\n    return bytes(seed[i] for i in perm)\n'
    raise ValueError(
        f'Cannot serialise {algorithm!r} to a self-contained script '
        '(it is key-dependent or table-based; export it another way)'
    )
