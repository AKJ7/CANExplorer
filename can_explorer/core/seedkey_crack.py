"""Seed/key algorithm recovery from captured UDS 0x27 SecurityAccess pairs, for benches you are authorised to test.

Given a list of (seed, key) pairs observed between a tester and an ECU, this module tests a series of hypotheses —
from identity to linear algebra over GF(2), AES-based KDFs with candidate keys, and LFSR-based scramblers — and
reports each algorithm that predicts the held-back verification pairs correctly.

Manufacturer seed/key algorithms are proprietary: this tool tests hypotheses from captured pairs, it does not ship
guessed algorithms and cannot recover algorithms it has no hypothesis for.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from can_explorer.core.secoc import aes128_encrypt, cmac_aes


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Match:
    """A confirmed seed-to-key algorithm."""

    algorithm: str
    description: str
    parameters: Dict
    pairs_tested: int
    pairs_confirmed: int
    compute_key: Callable[[int, bytes], bytes]


@dataclass
class CrackReport:
    """The result of :func:`crack`: every confirmed algorithm, best first."""

    level: int
    pairs: List[Tuple[bytes, bytes]]
    matches: List[Match] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f'Seed/key crack for level {self.level}: {len(self.pairs)} pairs, {len(self.matches)} matches']
        for m in self.matches:
            lines.append(f'  {m.algorithm}: {m.description} (confirmed on {m.pairs_confirmed} held-back pairs)')
        if not self.matches:
            lines.append('  No algorithm recovered — the transform may be non-linear or key-dependent.')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MIN_PAIRS = 3
FIT_FRACTION = 0.75  # First 75 % are used for fitting; the rest for verification
MIN_VERIFY = 2


def _split(pairs: List[Tuple[bytes, bytes]]) -> Tuple[List[Tuple[bytes, bytes]], List[Tuple[bytes, bytes]]]:
    """Split into fit and verify sets (verify has at least *MIN_VERIFY* pairs)."""
    n = len(pairs)
    split = max(1, min(n - MIN_VERIFY, int(n * FIT_FRACTION)))
    return pairs[:split], pairs[split:]


def _verify(fn: Callable[[bytes], bytes], verify: List[Tuple[bytes, bytes]]) -> bool:
    """Return True when *fn* predicts every verification pair."""
    return all(fn(seed) == key for seed, key in verify)


def _rotate_left(value: int, n: int, bits: int) -> int:
    n %= bits
    return ((value << n) | (value >> (bits - n))) & ((1 << bits) - 1)


def _rotate_right(value: int, n: int, bits: int) -> int:
    return _rotate_left(value, bits - n, bits)


# ---------------------------------------------------------------------------
# Hypothesis families (cheapest first)
# ---------------------------------------------------------------------------


def _try_identity(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key == seed, reversed, or bitwise complement."""
    matches: List[Match] = []

    # Direct identity
    if all(s == k for s, k in fit):

        def _id(seed: bytes) -> bytes:
            return seed

        if _verify(_id, verify):
            matches.append(
                Match(
                    algorithm='identity',
                    description='key = seed',
                    parameters={},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed: seed,
                )
            )

    # Reversed
    if all(s[::-1] == k for s, k in fit):

        def _rev(seed: bytes) -> bytes:
            return seed[::-1]

        if _verify(_rev, verify):
            matches.append(
                Match(
                    algorithm='identity (reversed)',
                    description='key = reversed(seed)',
                    parameters={},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed: seed[::-1],
                )
            )

    # Complement
    if all(bytes(b ^ 0xFF for b in s) == k for s, k in fit):

        def _comp(seed: bytes) -> bytes:
            return bytes(b ^ 0xFF for b in seed)

        if _verify(_comp, verify):
            matches.append(
                Match(
                    algorithm='bitwise complement',
                    description='key = ~seed',
                    parameters={},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed: bytes(b ^ 0xFF for b in seed),
                )
            )

    return matches


def _try_constant_xor(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key = seed XOR constant (whole value)."""
    s0, k0 = fit[0]
    constant = int.from_bytes(s0, 'big') ^ int.from_bytes(k0, 'big')
    mask = (1 << (8 * length)) - 1

    if all((int.from_bytes(s, 'big') ^ constant) == int.from_bytes(k, 'big') for s, k in fit[1:]):

        def fn(seed: bytes) -> bytes:
            return ((int.from_bytes(seed, 'big') ^ constant) & mask).to_bytes(length, 'big')

        if _verify(fn, verify):
            return [
                Match(
                    algorithm='constant XOR',
                    description=f'key = seed ^ 0x{constant:0{2 * length}X}',
                    parameters={'constant': constant.to_bytes(length, 'big').hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, c=constant, ln=length: (int.from_bytes(seed, 'big') ^ c).to_bytes(
                        ln, 'big'
                    ),
                )
            ]
    return []


def _try_constant_add_sub(
    fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int
) -> List[Match]:
    """key = (seed + constant) mod 2^n or key = (seed - constant) mod 2^n."""
    modulus = 1 << (8 * length)
    s0, k0 = fit[0]
    sv, kv = int.from_bytes(s0, 'big'), int.from_bytes(k0, 'big')
    matches: List[Match] = []

    # ADD
    delta = (kv - sv) % modulus
    if all((int.from_bytes(s, 'big') + delta) % modulus == int.from_bytes(k, 'big') for s, k in fit[1:]):

        def fn_add(seed: bytes, d=delta, m=modulus, ln=length) -> bytes:
            return ((int.from_bytes(seed, 'big') + d) % m).to_bytes(ln, 'big')

        if _verify(fn_add, verify):
            matches.append(
                Match(
                    algorithm='constant ADD',
                    description=f'key = (seed + 0x{delta:0{2 * length}X}) mod 2^{8 * length}',
                    parameters={'delta': delta.to_bytes(length, 'big').hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, d=delta, m=modulus, ln=length: (
                        (int.from_bytes(seed, 'big') + d) % m
                    ).to_bytes(ln, 'big'),
                )
            )

    # SUB
    delta_sub = (sv - kv) % modulus
    if delta_sub != delta and all(
        (int.from_bytes(s, 'big') - delta_sub) % modulus == int.from_bytes(k, 'big') for s, k in fit[1:]
    ):

        def fn_sub(seed: bytes, d=delta_sub, m=modulus, ln=length) -> bytes:
            return ((int.from_bytes(seed, 'big') - d) % m).to_bytes(ln, 'big')

        if _verify(fn_sub, verify):
            matches.append(
                Match(
                    algorithm='constant SUB',
                    description=f'key = (seed - 0x{delta_sub:0{2 * length}X}) mod 2^{8 * length}',
                    parameters={'delta': delta_sub.to_bytes(length, 'big').hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, d=delta_sub, m=modulus, ln=length: (
                        (int.from_bytes(seed, 'big') - d) % m
                    ).to_bytes(ln, 'big'),
                )
            )

    return matches


def _try_per_byte_xor(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key[i] = seed[i] ^ mask[i]."""
    s0, k0 = fit[0]
    mask = bytes(a ^ b for a, b in zip(s0, k0, strict=True))

    if all(bytes(a ^ m for a, m in zip(s, mask, strict=True)) == k for s, k in fit[1:]):

        def fn(seed: bytes, m=mask) -> bytes:
            return bytes(a ^ b for a, b in zip(seed, m, strict=True))

        if _verify(fn, verify):
            return [
                Match(
                    algorithm='per-byte XOR',
                    description=f'key[i] = seed[i] ^ mask[i], mask = {mask.hex().upper()}',
                    parameters={'mask': mask.hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, m=mask: bytes(a ^ b for a, b in zip(seed, m, strict=True)),
                )
            ]
    return []


def _try_per_byte_add(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key[i] = (seed[i] + mask[i]) & 0xFF."""
    s0, k0 = fit[0]
    mask = bytes((b - a) & 0xFF for a, b in zip(s0, k0, strict=True))

    if all(bytes((a + m) & 0xFF for a, m in zip(s, mask, strict=True)) == k for s, k in fit[1:]):

        def fn(seed: bytes, m=mask) -> bytes:
            return bytes((a + b) & 0xFF for a, b in zip(seed, m, strict=True))

        if _verify(fn, verify):
            return [
                Match(
                    algorithm='per-byte ADD',
                    description=f'key[i] = (seed[i] + mask[i]) & 0xFF, mask = {mask.hex().upper()}',
                    parameters={'mask': mask.hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, m=mask: bytes(
                        (a + b) & 0xFF for a, b in zip(seed, m, strict=True)
                    ),
                )
            ]
    return []


def _try_bit_rotation(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key = rotate_left(seed, n) or rotate_right(seed, n)."""
    bits = 8 * length
    matches: List[Match] = []
    for n in range(1, bits):
        # Left rotation
        if all(_rotate_left(int.from_bytes(s, 'big'), n, bits) == int.from_bytes(k, 'big') for s, k in fit):

            def fn_l(seed: bytes, rot=n, b=bits, ln=length) -> bytes:
                return _rotate_left(int.from_bytes(seed, 'big'), rot, b).to_bytes(ln, 'big')

            if _verify(fn_l, verify):
                matches.append(
                    Match(
                        algorithm='rotate left',
                        description=f'key = rotate_left(seed, {n})',
                        parameters={'direction': 'left', 'amount': n},
                        pairs_tested=len(fit) + len(verify),
                        pairs_confirmed=len(verify),
                        compute_key=lambda _level, seed, rot=n, b=bits, ln=length: _rotate_left(
                            int.from_bytes(seed, 'big'), rot, b
                        ).to_bytes(ln, 'big'),
                    )
                )
                break  # One left rotation is enough

        # Right rotation
        if all(_rotate_right(int.from_bytes(s, 'big'), n, bits) == int.from_bytes(k, 'big') for s, k in fit):

            def fn_r(seed: bytes, rot=n, b=bits, ln=length) -> bytes:
                return _rotate_right(int.from_bytes(seed, 'big'), rot, b).to_bytes(ln, 'big')

            if _verify(fn_r, verify):
                matches.append(
                    Match(
                        algorithm='rotate right',
                        description=f'key = rotate_right(seed, {n})',
                        parameters={'direction': 'right', 'amount': n},
                        pairs_tested=len(fit) + len(verify),
                        pairs_confirmed=len(verify),
                        compute_key=lambda _level, seed, rot=n, b=bits, ln=length: _rotate_right(
                            int.from_bytes(seed, 'big'), rot, b
                        ).to_bytes(ln, 'big'),
                    )
                )
                break

    return matches


def _try_byte_shuffle(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key bytes are seed bytes in a fixed permutation."""
    if length > 8:
        return []  # Too many permutations for brute force, and deduction needs many pairs

    if length <= 4:
        # Brute-force all permutations
        for perm in itertools.permutations(range(length)):
            if all(bytes(s[i] for i in perm) == k for s, k in fit):

                def fn(seed: bytes, p=perm) -> bytes:
                    return bytes(seed[i] for i in p)

                if _verify(fn, verify):
                    return [
                        Match(
                            algorithm='byte shuffle',
                            description=f'key[j] = seed[perm[j]], perm = {list(perm)}',
                            parameters={'permutation': list(perm)},
                            pairs_tested=len(fit) + len(verify),
                            pairs_confirmed=len(verify),
                            compute_key=lambda _level, seed, p=perm: bytes(seed[i] for i in p),
                        )
                    ]
    else:
        # Deduce the permutation: for each key position j, find which seed position i maps to it.
        # Requires that every seed position has at least one unique byte value across fit pairs.
        perm: List[Optional[int]] = [None] * length
        for j in range(length):
            key_values = [k[j] for _, k in fit]
            candidates = []
            for i in range(length):
                seed_values = [s[i] for s, _ in fit]
                if seed_values == key_values:
                    candidates.append(i)
            if len(candidates) == 1:
                perm[j] = candidates[0]
            else:
                return []  # Ambiguous or no match
        if None in perm or len(set(perm)) != length:
            return []

        perm_tuple = tuple(perm)  # type: ignore[arg-type]

        def fn(seed: bytes, p=perm_tuple) -> bytes:
            return bytes(seed[i] for i in p)

        if _verify(fn, verify):
            return [
                Match(
                    algorithm='byte shuffle',
                    description=f'key[j] = seed[perm[j]], perm = {list(perm_tuple)}',
                    parameters={'permutation': list(perm_tuple)},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, p=perm_tuple: bytes(seed[i] for i in p),
                )
            ]

    return []


def _try_gf2_linear(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """key = M * seed XOR c over GF(2): a bit-matrix transform with an additive constant.

    This covers XOR with arbitrary mixing, CRC-based transforms and LFSR-based schemes. The system
    is solved with Gaussian elimination over GF(2) using numpy.
    """
    bits = 8 * length
    n_fit = len(fit)

    # Need at least bits + 1 fit pairs (one per column of M plus the constant) to have a unique solution.
    # With fewer, the system is underdetermined and the particular solution may not generalise.
    if n_fit < bits + 1:
        return []

    # Build the system: for each pair, seed_bits is a row vector, key_bits is the target.
    # Model: key = M @ seed ^ c, all over GF(2).
    # Rewrite as: [seed | 1] @ [M^T | c^T]^T = key, per bit column of the output.

    def to_bits(data: bytes) -> np.ndarray:
        return np.array([int(b) for byte in data for b in format(byte, '08b')], dtype=np.uint8)

    # Build the augmented seed matrix: each row is [seed_bits..., 1]
    seed_matrix = np.zeros((n_fit, bits + 1), dtype=np.uint8)
    key_matrix = np.zeros((n_fit, bits), dtype=np.uint8)
    for i, (s, k) in enumerate(fit):
        seed_matrix[i, :bits] = to_bits(s)
        seed_matrix[i, bits] = 1  # constant term
        key_matrix[i] = to_bits(k)

    # Solve for each output bit independently: find x such that seed_matrix @ x = key_col (mod 2)
    solution = np.zeros((bits + 1, bits), dtype=np.uint8)  # columns are the solutions for each output bit
    for col in range(bits):
        target = key_matrix[:, col].copy()
        # Gaussian elimination over GF(2) on [seed_matrix | target]
        aug = np.hstack([seed_matrix.copy(), target.reshape(-1, 1)])
        rows, cols_aug = aug.shape
        pivot_row = 0
        for pivot_col in range(cols_aug - 1):
            # Find a row with a 1 in this column at or below pivot_row
            found = False
            for r in range(pivot_row, rows):
                if aug[r, pivot_col]:
                    if r != pivot_row:
                        aug[[pivot_row, r]] = aug[[r, pivot_row]]
                    found = True
                    break
            if not found:
                continue
            # Eliminate all other rows
            for r in range(rows):
                if r != pivot_row and aug[r, pivot_col]:
                    aug[r] = aug[r] ^ aug[pivot_row]
            pivot_row += 1

        # Check consistency: any row with all-zero LHS but nonzero RHS means no solution
        consistent = True
        for r in range(rows):
            if not aug[r, :-1].any() and aug[r, -1]:
                consistent = False
                break
        if not consistent:
            return []

        # Extract solution: for each pivot column, the solution bit is the RHS value
        x = np.zeros(bits + 1, dtype=np.uint8)
        pr = 0
        for pc in range(bits + 1):
            if pr < rows and aug[pr, pc]:
                # Check it's actually a pivot (leading 1 and only 1 in its column among active rows)
                x[pc] = aug[pr, -1]
                pr += 1
        solution[:, col] = x

    # Extract M (bits x bits) and c (bits,) from the solution
    m_matrix = solution[:bits, :]  # Transposed: M^T, so M[j][i] = m_matrix[i][j]
    c_vector = solution[bits, :]

    # Verify on fit pairs first (should be exact by construction, but check for underdetermined cases)
    for s, k in fit:
        s_bits = to_bits(s)
        predicted = (m_matrix.T @ s_bits ^ c_vector) % 2
        expected = to_bits(k)
        if not np.array_equal(predicted, expected):
            return []

    def _bits_to_bytes(bits_arr: np.ndarray, ln: int) -> bytes:
        result = bytearray(ln)
        for i in range(ln):
            byte_val = 0
            for bit in range(8):
                byte_val = (byte_val << 1) | int(bits_arr[i * 8 + bit])
            result[i] = byte_val
        return bytes(result)

    # Build the compute function with captured matrix and constant
    m_list = m_matrix.tolist()
    c_list = c_vector.tolist()
    nbits = bits

    def compute(seed: bytes, m=m_list, c=c_list, nb=nbits, ln=length) -> bytes:
        s_bits = [int(b) for byte in seed for b in format(byte, '08b')]
        result = [0] * nb
        for j in range(nb):
            val = c[j]
            for i in range(nb):
                val ^= m[i][j] & s_bits[i]
            result[j] = val & 1
        out = bytearray(ln)
        for i in range(ln):
            byte_val = 0
            for bit in range(8):
                byte_val = (byte_val << 1) | result[i * 8 + bit]
            out[i] = byte_val
        return bytes(out)

    # Verify on the held-back pairs
    if not all(compute(s) == k for s, k in verify):
        return []

    # Describe the transform
    is_pure_xor = all(
        sum(int(m_matrix[i, j]) for i in range(bits)) <= 1 and int(m_matrix[j, j]) == 1 for j in range(bits)
    )
    if is_pure_xor:
        # It's just XOR with a constant -- already covered by earlier families
        return []

    # Serialise the matrix and constant for the parameters dict
    m_hex = bytes(np.packbits(m_matrix.flatten())).hex()
    c_hex = _bits_to_bytes(c_vector, length).hex()

    return [
        Match(
            algorithm='GF(2) linear',
            description=f'key = M * seed XOR c over GF(2), {bits}x{bits} bit matrix',
            parameters={'matrix_hex': m_hex, 'constant_hex': c_hex, 'bits': bits},
            pairs_tested=len(fit) + len(verify),
            pairs_confirmed=len(verify),
            compute_key=lambda _level, seed, fn=compute: fn(seed),
        )
    ]


def _try_per_byte_lookup(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """Each key byte is a fixed function of the corresponding seed byte (a lookup table)."""
    tables: List[Dict[int, int]] = [{} for _ in range(length)]

    for s, k in fit:
        for i in range(length):
            if s[i] in tables[i]:
                if tables[i][s[i]] != k[i]:
                    return []  # Inconsistent: same seed byte maps to different key bytes
            else:
                tables[i][s[i]] = k[i]

    def fn(seed: bytes, t=tables) -> bytes:
        result = bytearray(len(seed))
        for i, byte in enumerate(seed):
            if byte not in t[i]:
                raise KeyError(f'Seed byte {byte} at position {i} not in the learned lookup table')
            result[i] = t[i][byte]
        return bytes(result)

    # Verify: every verify seed byte must have been seen during fitting
    try:
        if not _verify(fn, verify):
            return []
    except KeyError:
        return []

    # Describe coverage
    coverage = [len(t) for t in tables]
    serialised = {str(i): {f'{k:02X}': f'{v:02X}' for k, v in sorted(t.items())} for i, t in enumerate(tables)}

    return [
        Match(
            algorithm='per-byte lookup',
            description=f'key[i] = table[i][seed[i]], coverage per byte: {coverage}',
            parameters={'tables': serialised},
            pairs_tested=len(fit) + len(verify),
            pairs_confirmed=len(verify),
            compute_key=lambda _level, seed, t=tables: bytes(t[i][seed[i]] for i in range(len(seed))),
        )
    ]


# ---------------------------------------------------------------------------
# AES-based KDF hypothesis (candidate key testing)
# ---------------------------------------------------------------------------

COMMON_KEYS: List[bytes] = [
    bytes(16),  # All zeros
    bytes([0xFF] * 16),  # All ones
    bytes(range(16)),  # 0x00..0x0F
    bytes(range(15, -1, -1)),  # 0x0F..0x00
    b'\x01' * 16,  # All 0x01
]


def _pad_zero(data: bytes, block: int = 16) -> bytes:
    """Zero-pad `data` to `block` bytes."""
    if len(data) >= block:
        return data[:block]
    return data + b'\x00' * (block - len(data))


def _pad_pkcs7(data: bytes, block: int = 16) -> bytes:
    """PKCS#7-pad `data` to `block` bytes."""
    pad_len = block - (len(data) % block)
    return data + bytes([pad_len] * pad_len)


_AES_KDF_PATTERNS: List[tuple] = [
    # (name, description_template, function(candidate, seed) -> bytes)
    (
        'AES-ECB',
        'key = AES_ECB({candidate}, seed)[:key_len]',
        lambda candidate, seed: aes128_encrypt(candidate, _pad_zero(seed)),
    ),
    (
        'AES-ECB (PKCS#7 pad)',
        'key = AES_ECB({candidate}, PKCS7(seed))[:key_len]',
        lambda candidate, seed: aes128_encrypt(candidate, _pad_pkcs7(seed)[:16]),
    ),
    (
        'AES-ECB (seed XOR key)',
        'key = AES_ECB({candidate}, seed ^ {candidate})[:key_len]',
        lambda candidate, seed: aes128_encrypt(
            candidate, bytes(a ^ b for a, b in zip(_pad_zero(seed), candidate, strict=True))
        ),
    ),
    (
        'CMAC-AES',
        'key = CMAC_AES({candidate}, seed)[:key_len]',
        lambda candidate, seed: cmac_aes(candidate, seed),
    ),
    (
        'HMAC-SHA-256',
        'key = HMAC_SHA256({candidate}, seed)[:key_len]',
        lambda candidate, seed: hmac_mod.new(candidate, seed, hashlib.sha256).digest(),
    ),
]


def _try_aes_kdf(
    fit: List[Tuple[bytes, bytes]],
    verify: List[Tuple[bytes, bytes]],
    length: int,
    candidate_keys: Sequence[bytes],
) -> List[Match]:
    """Test AES-based KDF patterns with each candidate key."""
    matches: List[Match] = []
    key_len = length

    for candidate in candidate_keys:
        if len(candidate) != 16:
            continue  # Only AES-128 for now
        for pattern_name, desc_template, derive_fn in _AES_KDF_PATTERNS:
            # Test on all fit pairs
            try:
                if not all(derive_fn(candidate, s)[:key_len] == k for s, k in fit):
                    continue
            except Exception:
                continue

            # Build the compute function
            def _make_fn(c: bytes, fn=derive_fn, kl: int = key_len):
                def compute(seed: bytes) -> bytes:
                    return fn(c, seed)[:kl]

                return compute

            fn = _make_fn(candidate)

            if not _verify(fn, verify):
                continue

            desc = desc_template.format(candidate=candidate.hex().upper())
            matches.append(
                Match(
                    algorithm=f'AES KDF ({pattern_name})',
                    description=desc,
                    parameters={'pattern': pattern_name, 'candidate_key': candidate.hex()},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=lambda _level, seed, f=fn: f(seed),
                )
            )
            # One pattern match per candidate is enough — don't report the same candidate twice
            break

    return matches


# Also add HMAC-SHA-256 with level byte prefix
def _try_aes_kdf_with_level(
    fit: List[Tuple[bytes, bytes]],
    verify: List[Tuple[bytes, bytes]],
    length: int,
    candidate_keys: Sequence[bytes],
    level: int,
) -> List[Match]:
    """Test HMAC-SHA-256(candidate, level_byte || seed) pattern."""
    matches: List[Match] = []
    key_len = length
    level_byte = bytes([level & 0xFF])

    for candidate in candidate_keys:
        if len(candidate) != 16:
            continue
        try:
            if not all(hmac_mod.new(candidate, level_byte + s, hashlib.sha256).digest()[:key_len] == k for s, k in fit):
                continue
        except Exception:
            continue

        def _make_fn(c: bytes, lb: bytes = level_byte, kl: int = key_len):
            def compute(seed: bytes) -> bytes:
                return hmac_mod.new(c, lb + seed, hashlib.sha256).digest()[:kl]

            return compute

        fn = _make_fn(candidate)
        if not _verify(fn, verify):
            continue

        matches.append(
            Match(
                algorithm='AES KDF (HMAC-SHA-256 with level)',
                description=f'key = HMAC_SHA256({candidate.hex().upper()}, level_byte || seed)[:key_len]',
                parameters={'pattern': 'HMAC-SHA-256 with level', 'candidate_key': candidate.hex(), 'level': level},
                pairs_tested=len(fit) + len(verify),
                pairs_confirmed=len(verify),
                compute_key=lambda _level, seed, f=fn: f(seed),
            )
        )
        break

    return matches


# ---------------------------------------------------------------------------
# Berlekamp-Massey LFSR detection
# ---------------------------------------------------------------------------


def _berlekamp_massey_gf2(sequence: List[int]) -> List[int]:
    """Return the connection polynomial coefficients [c1, c2, ..., cL] of the shortest LFSR
    over GF(2) that generates the sequence.

    The LFSR recurrence is: s[n] = c1*s[n-1] XOR c2*s[n-2] XOR ... XOR cL*s[n-L]  (all mod 2).
    """
    n = len(sequence)
    # Current connection polynomial C(x), best polynomial B(x)
    c_poly = [1]  # C(x) = 1
    b_poly = [1]  # B(x) = 1
    lfsr_len = 0
    m = 1  # Delay since last length change
    b_val = 1  # Last discrepancy

    for i in range(n):
        # Compute discrepancy
        d = sequence[i]
        for j in range(1, lfsr_len + 1):
            if j < len(c_poly):
                d ^= c_poly[j] & sequence[i - j]

        if d == 0:
            m += 1
        else:
            t_poly = list(c_poly)
            # C(x) = C(x) - d * b^{-1} * x^m * B(x); over GF(2), d=b=1
            shift = [0] * m + b_poly
            # XOR
            while len(c_poly) < len(shift):
                c_poly.append(0)
            for j in range(len(shift)):
                c_poly[j] ^= shift[j]

            if 2 * lfsr_len <= i:
                lfsr_len = i + 1 - lfsr_len
                b_poly = t_poly
                b_val = d  # noqa: F841
                m = 1
            else:
                m += 1

    # Return the feedback coefficients c1..cL (skip the leading 1)
    return c_poly[1 : lfsr_len + 1]


def _try_lfsr(fit: List[Tuple[bytes, bytes]], verify: List[Tuple[bytes, bytes]], length: int) -> List[Match]:
    """Detect LFSR-based seed-to-key transforms.

    The seed bits are the initial state; the key bits are produced by continuing a linear recurrence.
    The recurrence coefficients are found by solving the system over GF(2) using multiple pairs, then
    verified against the held-back pairs.
    """
    bits_per_value = 8 * length

    if len(fit) < 2 or bits_per_value > 64:
        return []

    def to_bits(data: bytes) -> List[int]:
        return [int(b) for byte in data for b in format(byte, '08b')]

    def from_bits(bits_list: List[int], byte_count: int) -> bytes:
        result = bytearray(byte_count)
        for i in range(byte_count):
            val = 0
            for bit in range(8):
                val = (val << 1) | (bits_list[i * 8 + bit] & 1)
            result[i] = val
        return bytes(result)

    # Try LFSR lengths from 1 up to seed bit length
    for lfsr_len in range(1, bits_per_value + 1):
        # For each candidate LFSR length L, build a linear system over GF(2) from the fit pairs.
        # For each pair, the seed bits are the initial state. Each key bit k[i] must equal
        # c1*prev[-1] XOR c2*prev[-2] XOR ... XOR cL*prev[-L] where prev is the running state.
        # We collect equations (one per key bit per pair) and solve for c1..cL.

        equations: List[List[int]] = []
        targets: List[int] = []

        for s, k in fit:
            state = to_bits(s)
            key_bits = to_bits(k)
            for i in range(bits_per_value):
                if len(state) < lfsr_len:
                    break
                # Equation: key_bit[i] = sum(c[j] * state[-1-j]) for j in 0..L-1
                row = [state[len(state) - 1 - j] if (len(state) - 1 - j) >= 0 else 0 for j in range(lfsr_len)]
                equations.append(row)
                targets.append(key_bits[i])
                state.append(key_bits[i])

        if len(equations) < lfsr_len:
            continue

        # Solve using GF(2) Gaussian elimination
        n_eq = len(equations)
        aug = [equations[i] + [targets[i]] for i in range(n_eq)]

        pivot_row = 0
        pivot_cols = []
        for col in range(lfsr_len):
            found = False
            for r in range(pivot_row, n_eq):
                if aug[r][col]:
                    if r != pivot_row:
                        aug[pivot_row], aug[r] = aug[r], aug[pivot_row]
                    found = True
                    break
            if not found:
                continue
            pivot_cols.append(col)
            for r in range(n_eq):
                if r != pivot_row and aug[r][col]:
                    aug[r] = [aug[r][j] ^ aug[pivot_row][j] for j in range(lfsr_len + 1)]
            pivot_row += 1

        # Check consistency
        consistent = True
        for r in range(pivot_row, n_eq):
            if aug[r][lfsr_len]:
                consistent = False
                break
        if not consistent:
            continue

        # Check that we have a unique solution (all columns are pivots)
        if len(pivot_cols) < lfsr_len:
            continue

        # Extract coefficients
        coeffs = [0] * lfsr_len
        for i, col in enumerate(pivot_cols):
            coeffs[col] = aug[i][lfsr_len]

        if not any(coeffs):
            continue

        def _make_predict(c: List[int], bpv: int = bits_per_value, ln: int = length):
            def predict(seed: bytes) -> bytes:
                state = to_bits(seed)
                for _ in range(bpv):
                    new_bit = 0
                    for j, coeff in enumerate(c):
                        if coeff and (len(state) - 1 - j) >= 0:
                            new_bit ^= state[len(state) - 1 - j]
                    state.append(new_bit & 1)
                return from_bits(state[bpv:], ln)

            return predict

        predict_fn = _make_predict(coeffs)

        # Verify on fit pairs
        if not all(predict_fn(s) == k for s, k in fit):
            continue

        # Verify on held-back pairs
        if not _verify(predict_fn, verify):
            continue

        coeffs_copy = list(coeffs)
        return [
            Match(
                algorithm='LFSR',
                description=f'LFSR(L={lfsr_len}): seed bits are the initial state, key bits are the continuation',
                parameters={'lfsr_length': lfsr_len, 'coefficients': coeffs_copy},
                pairs_tested=len(fit) + len(verify),
                pairs_confirmed=len(verify),
                compute_key=lambda _level, seed, fn=predict_fn: fn(seed),
            )
        ]

    return []


# ---------------------------------------------------------------------------
# Hash-truncation transforms: key = H(seed)[:key_len], no secret involved
# ---------------------------------------------------------------------------

_HASHES = (('MD5', hashlib.md5), ('SHA-1', hashlib.sha1), ('SHA-256', hashlib.sha256))


def _try_hash_truncated(fit, verify, length):
    """A naive scheme that hashes the seed and truncates: key = MD5/SHA-1/SHA-256(seed)[:key_len]."""
    matches: List[Match] = []
    for name, hash_fn in _HASHES:

        def compute(_level, seed, fn=hash_fn, kl=length):
            return fn(bytes(seed)).digest()[:kl]

        if all(compute(0, s) == k for s, k in fit) and _verify(lambda s, c=compute: c(0, s), verify):
            matches.append(
                Match(
                    algorithm=f'hash truncation ({name})',
                    description=f'key = {name}(seed)[:key_len]',
                    parameters={'hash': name},
                    pairs_tested=len(fit) + len(verify),
                    pairs_confirmed=len(verify),
                    compute_key=compute,
                )
            )
            break
    return matches


# ---------------------------------------------------------------------------
# CRC transforms: key = CRC(seed), common parameter sets, xorout solved from a pair
# ---------------------------------------------------------------------------

_CRC_POLYS = {
    2: (0x1021, 0x8005, 0x3D65),  # width 16: CCITT, IBM/ANSI, DNP/profibus
    4: (0x04C11DB7, 0x1EDC6F41),  # width 32: standard, Castagnoli
}


def _reflect(value: int, width: int) -> int:
    result = 0
    for _ in range(width):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _crc(data: bytes, width: int, poly: int, init: int, refin: bool, refout: bool) -> int:
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    crc = init
    for byte in data:
        crc ^= (_reflect(byte, 8) if refin else byte) << (width - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
    return _reflect(crc, width) if refout else crc


def _try_crc(fit, verify, length):
    """A CRC-based transform: key (2 or 4 bytes) = CRC(seed) ^ xorout, searching common CRC parameter sets and
    solving xorout from the first pair."""
    if length not in (2, 4):
        return []
    width = length * 8
    mask = (1 << width) - 1
    (s0, k0) = fit[0]
    k0_int = int.from_bytes(k0, 'big')
    for poly in _CRC_POLYS[length]:
        for init in (0, mask):
            for refin in (False, True):
                for refout in (False, True):
                    xorout = (_crc(s0, width, poly, init, refin, refout) ^ k0_int) & mask

                    def compute(_level, seed, p=poly, i=init, ri=refin, ro=refout, x=xorout, w=width, le=length):
                        return ((_crc(bytes(seed), w, p, i, ri, ro) ^ x) & ((1 << w) - 1)).to_bytes(le, 'big')

                    if all(compute(0, s) == k for s, k in fit) and _verify(lambda s, c=compute: c(0, s), verify):
                        return [
                            Match(
                                algorithm='CRC',
                                description=(
                                    f'key = CRC{width}(poly=0x{poly:X}, init=0x{init:X}, refin={refin}, '
                                    f'refout={refout}, xorout=0x{xorout:X})(seed)'
                                ),
                                parameters={
                                    'width': width,
                                    'poly': poly,
                                    'init': init,
                                    'refin': refin,
                                    'refout': refout,
                                    'xorout': xorout,
                                },
                                pairs_tested=len(fit) + len(verify),
                                pairs_confirmed=len(verify),
                                compute_key=compute,
                            )
                        ]
    return []


def _try_des_kdf(fit, verify, length, candidate_keys):
    """key = DES/3DES-ECB(candidate, seed-block)[:key_len], the DES analogue of the AES KDF (8-byte block)."""
    if length != 8:
        return []
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    try:  # cryptography >= 43 moved TripleDES to the decrepit namespace
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

    matches: List[Match] = []
    for candidate in candidate_keys:
        if len(candidate) not in (8, 16, 24):
            continue

        def compute(_level, seed, key=candidate, kl=length):
            full = key * 3 if len(key) == 8 else key  # Single DES = 3DES with K1=K2=K3 (avoids the 8-byte deprecation)
            encryptor = Cipher(TripleDES(full), modes.ECB()).encryptor()
            block = bytes(seed)[:8].ljust(8, b'\x00')
            return (encryptor.update(block) + encryptor.finalize())[:kl]

        try:
            if all(compute(0, s) == k for s, k in fit) and _verify(lambda s, c=compute: c(0, s), verify):
                matches.append(
                    Match(
                        algorithm='DES KDF',
                        description=f'key = 3DES-ECB({candidate.hex().upper()}, seed)[:key_len]',
                        parameters={'candidate_key': candidate.hex()},
                        pairs_tested=len(fit) + len(verify),
                        pairs_confirmed=len(verify),
                        compute_key=compute,
                    )
                )
                break
        except Exception:
            continue
    return matches


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

FAMILIES = [
    _try_identity,
    _try_constant_xor,
    _try_constant_add_sub,
    _try_per_byte_xor,
    _try_per_byte_add,
    _try_bit_rotation,
    _try_byte_shuffle,
    _try_gf2_linear,
    _try_per_byte_lookup,
    _try_hash_truncated,
    _try_crc,
]


def crack(
    pairs: List[Tuple[bytes, bytes]],
    level: int = 1,
    candidate_keys: Optional[List[bytes]] = None,
) -> CrackReport:
    """Recover the seed/key algorithm from captured (seed, key) pairs.

    Splits the pairs into a fit set (first 75 %) and a verification set (last 25 %, minimum 2). A
    hypothesis is reported only when it predicts every verification pair correctly. At least 3 pairs
    are needed.

    *candidate_keys*: optional list of 16-byte master keys to test against AES-based KDF patterns
    (AES-ECB, CMAC-AES, HMAC-SHA-256). When not provided but pairs are 16 bytes, the well-known
    :data:`COMMON_KEYS` are tried automatically.

    Returns a :class:`CrackReport` with the confirmed :class:`Match` objects, best (simplest) first.
    """
    report = CrackReport(level=level, pairs=list(pairs))

    if len(pairs) < MIN_PAIRS:
        return report

    # Validate: all seeds must have the same length, all keys must have the same length,
    # and seed length must equal key length
    seed_lengths = {len(s) for s, _ in pairs}
    key_lengths = {len(k) for _, k in pairs}
    if len(seed_lengths) != 1 or len(key_lengths) != 1:
        return report
    seed_len = seed_lengths.pop()
    key_len = key_lengths.pop()
    if seed_len != key_len or seed_len == 0:
        return report

    length = seed_len
    fit, verify = _split(pairs)

    for family in FAMILIES:
        try:
            found = family(fit, verify, length)
        except Exception:
            continue
        report.matches.extend(found)

    # AES-based KDF with candidate keys
    keys_to_try = list(candidate_keys) if candidate_keys is not None else []
    if not keys_to_try and length <= 16:
        keys_to_try = list(COMMON_KEYS)
    if keys_to_try:
        try:
            report.matches.extend(_try_aes_kdf(fit, verify, length, keys_to_try))
        except Exception:
            pass
        try:
            report.matches.extend(_try_aes_kdf_with_level(fit, verify, length, keys_to_try, level))
        except Exception:
            pass
        try:
            report.matches.extend(_try_des_kdf(fit, verify, length, keys_to_try))
        except Exception:
            pass

    # LFSR-based transform
    try:
        report.matches.extend(_try_lfsr(fit, verify, length))
    except Exception:
        pass

    return report


# ---------------------------------------------------------------------------
# Item 9: Seed entropy assessment
# ---------------------------------------------------------------------------


@dataclass
class SeedQualityReport:
    """Assessment of the quality of seeds returned by an ECU."""

    length: int
    entropy_bits: float
    unique_ratio: float  # Fraction of unique seeds (0 to 1)
    sequential_pattern: bool  # True if seeds look like a counter
    predictable: bool  # True if any weakness was found
    findings: List[str] = field(default_factory=list)


def assess_seed_quality(seeds: List[bytes]) -> SeedQualityReport:
    """Assess the quality of seeds returned by an ECU's 0x27 SecurityAccess.

    Checks for: low entropy, repeated seeds, sequential (counter-based) patterns, fixed bytes and
    truncated counters. Returns a report with findings.
    """
    if not seeds:
        return SeedQualityReport(
            length=0,
            entropy_bits=0.0,
            unique_ratio=0.0,
            sequential_pattern=False,
            predictable=True,
            findings=['No seeds provided'],
        )

    length = len(seeds[0])
    n = len(seeds)
    findings: List[str] = []
    predictable = False

    # 1. Unique ratio
    unique = len(set(seeds))
    unique_ratio = unique / n if n > 0 else 0.0
    if unique_ratio < 0.5:
        findings.append(f'Only {unique}/{n} unique seeds ({unique_ratio:.0%}): high repetition rate')
        predictable = True
    if unique == 1:
        findings.append(f'All {n} seeds are identical ({seeds[0].hex().upper()}): fixed seed')
        predictable = True

    # 2. Shannon entropy per byte position
    total_entropy = 0.0
    for pos in range(length):
        counts: Dict[int, int] = {}
        for seed in seeds:
            if pos < len(seed):
                b = seed[pos]
                counts[b] = counts.get(b, 0) + 1
        byte_entropy = 0.0
        for count in counts.values():
            p = count / n
            if p > 0:
                byte_entropy -= p * math.log2(p)
        total_entropy += byte_entropy

    # 3. Sequential pattern detection (counter-based seeds)
    sequential = False
    if n >= 3:
        values = [int.from_bytes(s, 'big') for s in seeds]
        diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
        if len(set(diffs)) == 1 and diffs[0] != 0:
            sequential = True
            findings.append(f'Seeds form a counter with step {diffs[0]}: fully predictable')
            predictable = True
        elif len(set(diffs)) <= 2 and all(0 < d < 256 for d in diffs):
            sequential = True
            findings.append('Seeds appear sequential (counter-based): largely predictable')
            predictable = True

    # 4. Fixed bytes (bytes that never change across seeds)
    fixed_positions: List[int] = []
    for pos in range(length):
        vals = {seed[pos] for seed in seeds if pos < len(seed)}
        if len(vals) == 1:
            fixed_positions.append(pos)
    if fixed_positions and len(fixed_positions) < length:
        effective_bytes = length - len(fixed_positions)
        findings.append(
            f'{len(fixed_positions)} of {length} byte(s) are fixed '
            f'(positions {fixed_positions}): only {effective_bytes} byte(s) of effective entropy'
        )
        if effective_bytes <= 1:
            predictable = True

    # 5. Low entropy warning
    max_entropy = length * 8.0
    if max_entropy > 0 and total_entropy < max_entropy * 0.3 and n >= 10:
        findings.append(
            f'Low entropy: {total_entropy:.1f} bits of {max_entropy:.0f} possible ({total_entropy / max_entropy:.0%})'
        )
        predictable = True

    if not findings:
        findings.append(f'{unique}/{n} unique seeds, {total_entropy:.1f} bits entropy: no obvious weakness detected')

    return SeedQualityReport(
        length=length,
        entropy_bits=round(total_entropy, 2),
        unique_ratio=round(unique_ratio, 4),
        sequential_pattern=sequential,
        predictable=predictable,
        findings=findings,
    )
