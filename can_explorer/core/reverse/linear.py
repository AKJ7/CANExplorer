"""GF(2) linear dependency search in CAN frame payloads: finds affine bit relations such as CRCs, XOR masks and
linear obfuscation.

Generalises the checksum search of checksums.py: instead of trying known CRC-8 polynomials, it builds a matrix of
all payload bits and uses Gaussian elimination over GF(2) to find which output bits are an affine function of which
input bits.  After finding a relation, it tries to match it against known CRC polynomials from the AUTOSAR E2E
library (0x1D, 0x2F, 0x1021, 0x8005, 0x04C11DB7, 0xF4ACFB13, 0x42F0E1EBA9EA3693).
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

KNOWN_POLYNOMIALS: dict = {
    # CRC-8
    0x1D: 'CRC-8 SAE J1850',
    0x2F: 'CRC-8H2F',
    0x07: 'CRC-8 (SMBus)',
    0x31: 'CRC-8 Maxim',
    0x9B: 'CRC-8 CDMA2000',
    # CRC-16
    0x1021: 'CRC-16/CCITT',
    0x8005: 'CRC-16/IBM',
    # CRC-32
    0x04C11DB7: 'CRC-32',
    0xF4ACFB13: 'CRC-32P4 (AUTOSAR)',
    # CRC-64
    0x42F0E1EBA9EA3693: 'CRC-64 (ECMA)',
}

MIN_FRAMES_DEFAULT = 50


@dataclass
class LinearRelation:
    """An affine bit relation found in the payload: output_bits = f(input_bits) + constant."""

    output_bits: List[int]  # The bit positions that are a linear function of the input bits
    input_bits: List[int]
    constant: bytes  # The affine constant
    algorithm: str  # 'CRC-16', 'CRC-32', 'XOR mask', 'affine', etc.
    polynomial: Optional[int]  # For CRC matches
    description: str
    confidence: float = 1.0  # Share of verification frames that matched (0 to 1)


def _payload_to_bits(payload: bytes) -> List[int]:
    """Convert a payload to a list of bits (LSB of byte 0 first)."""
    result = []
    for byte in payload:
        for bit in range(8):
            result.append((byte >> bit) & 1)
    return result


def _bits_to_bytes(bits: List[int]) -> bytes:
    """Convert a list of bits back to bytes."""
    n_bytes = (len(bits) + 7) // 8
    result = bytearray(n_bytes)
    for i, b in enumerate(bits):
        if b:
            result[i // 8] |= 1 << (i % 8)
    return bytes(result)


def _gf2_gauss(matrix: List[List[int]], n_cols: int) -> List[List[int]]:
    """GF(2) Gaussian elimination on a binary matrix (list of rows, each row a list of 0/1 values).
    Returns the matrix in row echelon form."""
    rows = [row[:] for row in matrix]
    pivot_row = 0
    for col in range(n_cols):
        # Find a row with a 1 in this column
        found = None
        for r in range(pivot_row, len(rows)):
            if rows[r][col]:
                found = r
                break
        if found is None:
            continue
        # Swap to pivot position
        rows[pivot_row], rows[found] = rows[found], rows[pivot_row]
        # Eliminate this column from all other rows
        for r in range(len(rows)):
            if r != pivot_row and rows[r][col]:
                rows[r] = [a ^ b for a, b in zip(rows[r], rows[pivot_row], strict=False)]
        pivot_row += 1
    return rows


def _try_crc(input_data: List[bytes], output_values: List[int], width: int) -> Optional[Tuple[int, str]]:
    """Try to match the relation against known CRC polynomials.

    input_data: the input bytes for each frame (the non-output bytes).
    output_values: the computed output value per frame.
    width: the width of the output in bits.
    """
    for poly, name in KNOWN_POLYNOMIALS.items():
        if poly.bit_length() > width:
            continue
        # Only try polynomials of the right width
        poly_width = poly.bit_length()
        if poly_width > width:
            continue
        # Compute CRC for each frame and check if it matches output ^ constant
        try:
            computed = _compute_crc_batch(input_data, poly, width)
        except Exception:
            continue
        if computed is None:
            continue
        # Check if output = computed ^ constant for some constant
        if not computed:
            continue
        constant = output_values[0] ^ computed[0]
        if all(output_values[i] ^ computed[i] == constant for i in range(len(output_values))):
            return poly, name
    return None


def _compute_crc_batch(data_list: List[bytes], polynomial: int, width: int) -> Optional[List[int]]:
    """Compute CRC for a batch of data, returning the CRC values."""
    results = []
    mask = (1 << width) - 1
    for data in data_list:
        crc = 0
        for byte in data:
            crc ^= byte << (width - 8) if width >= 8 else byte >> (8 - width)
            for _ in range(8):
                if crc & (1 << (width - 1)):
                    crc = ((crc << 1) ^ polynomial) & mask
                else:
                    crc = (crc << 1) & mask
        results.append(crc)
    return results


def find_linear_relations(payloads: Sequence[bytes], min_frames: int = MIN_FRAMES_DEFAULT) -> List[LinearRelation]:
    """Find affine bit relations in the payloads using GF(2) Gaussian elimination.

    Looks for groups of output bits that are a linear (affine) function of the other bits. After finding a relation,
    tries to match it against known CRC polynomials.
    """
    if len(payloads) < min_frames or not payloads:
        return []

    length = len(payloads[0])
    if length < 2:
        return []

    # Only use payloads of the same length
    payloads = [p for p in payloads if len(p) == length]
    if len(payloads) < min_frames:
        return []

    total_bits = length * 8
    relations: List[LinearRelation] = []

    # Try byte-aligned output ranges: 1-byte, 2-byte and 4-byte outputs
    for out_width in (8, 16, 32):
        if out_width > total_bits:
            continue
        for out_start_byte in range(length - out_width // 8 + 1):
            out_start_bit = out_start_byte * 8
            out_end_bit = out_start_bit + out_width
            output_bits = list(range(out_start_bit, out_end_bit))
            input_bits = [b for b in range(total_bits) if b not in output_bits]

            if not input_bits:
                continue

            # Build the system: for each pair of frames, check if the XOR of output bits is a linear function
            # of the XOR of input bits. Use the first frame as reference.
            ref_payload_bits = _payload_to_bits(payloads[0])
            ref_output = ref_payload_bits[out_start_bit:out_end_bit]

            # Build matrix: each row is (input_bit_diffs | output_bit_diffs) for a frame pair
            n_input = len(input_bits)
            n_output = out_width
            matrix: List[List[int]] = []

            for i in range(1, min(len(payloads), n_input + n_output + 10)):
                bits = _payload_to_bits(payloads[i])
                row = []
                for b in input_bits:
                    row.append(bits[b] ^ ref_payload_bits[b])
                for b in output_bits:
                    row.append(bits[b] ^ ref_payload_bits[b])
                matrix.append(row)

            if len(matrix) < out_width:
                continue

            # Gaussian elimination
            reduced = _gf2_gauss(matrix, n_input + n_output)

            # Check if the output bits are determined by the input bits:
            # After elimination, if any row has zeros in input columns but non-zero in output columns,
            # then the output is NOT a linear function of the input.
            is_linear = True
            for row in reduced:
                if not any(row[:n_input]) and any(row[n_input:]):
                    is_linear = False
                    break

            if not is_linear:
                continue

            # Verify on ALL frames (not just those used for elimination)
            # Extract the linear mapping: for each output bit, which input bits contribute
            # Re-do elimination to get the actual mapping coefficients
            # Build augmented matrix [input_diffs | output_diffs] and solve for output = M * input + constant
            # The constant comes from the reference frame

            # Verify: compute the output for each frame using the linear relation
            # First, extract the mapping from the reduced matrix
            mapping: List[List[int]] = [[] for _ in range(n_output)]
            pivot_cols: List[int] = []
            for row in reduced:
                # Find pivot column
                pivot = -1
                for c in range(n_input):
                    if row[c]:
                        pivot = c
                        break
                if pivot >= 0:
                    pivot_cols.append(pivot)
                    for out_idx in range(n_output):
                        if row[n_input + out_idx]:
                            mapping[out_idx].append(pivot)

            # Verify on all frames
            match_count = 0
            total_check = min(len(payloads), 400)
            for i in range(total_check):
                bits = _payload_to_bits(payloads[i])
                predicted_output = list(ref_output)
                for out_idx in range(n_output):
                    for inp_idx in mapping[out_idx]:
                        predicted_output[out_idx] ^= bits[input_bits[inp_idx]] ^ ref_payload_bits[input_bits[inp_idx]]
                actual_output = bits[out_start_bit:out_end_bit]
                if predicted_output == actual_output:
                    match_count += 1

            if match_count / total_check < 0.95:
                continue

            # Compute constant (from reference frame)
            constant_bits = list(ref_output)
            for out_idx in range(n_output):
                for inp_idx in mapping[out_idx]:
                    constant_bits[out_idx] ^= ref_payload_bits[input_bits[inp_idx]]
            constant = _bits_to_bytes(constant_bits)

            # Try to identify the algorithm
            # Extract output values and input data for CRC matching
            output_values = []
            input_data_list: List[bytes] = []
            for payload in payloads[:total_check]:
                # Output value
                out_val = 0
                for bit_idx in range(out_width):
                    if payload[(out_start_bit + bit_idx) // 8] & (1 << ((out_start_bit + bit_idx) % 8)):
                        out_val |= 1 << bit_idx
                output_values.append(out_val)
                # Input data (the bytes not in the output)
                input_bytes = bytearray()
                for b in range(length):
                    if b < out_start_byte or b >= out_start_byte + out_width // 8:
                        input_bytes.append(payload[b])
                input_data_list.append(bytes(input_bytes))

            crc_match = _try_crc(input_data_list, output_values, out_width)

            # Check for simple XOR mask
            algorithm = 'affine'
            polynomial = None
            if crc_match:
                polynomial, algorithm = crc_match
                algorithm = f'{algorithm} ({out_width}-bit)'
            elif out_width == 8:
                # Check if it is a simple XOR of the input bytes
                xor_match = True
                for i in range(total_check):
                    xor_val = 0
                    for b in range(length):
                        if b != out_start_byte:
                            xor_val ^= payloads[i][b]
                    if (xor_val ^ output_values[i]) != (output_values[0] ^ 0):
                        # Check with constant
                        pass
                    expected = xor_val ^ int(constant[0])
                    if expected != output_values[i]:
                        xor_match = False
                        break
                if xor_match:
                    algorithm = 'XOR mask'

            # Build a description
            desc_parts = [f'{out_width}-bit {algorithm} in byte(s) {out_start_byte}']
            if out_width > 8:
                desc_parts[0] += f'-{out_start_byte + out_width // 8 - 1}'
            desc_parts.append(f'of the other {length - out_width // 8} byte(s)')
            if polynomial is not None:
                desc_parts.append(f'(polynomial 0x{polynomial:X})')
            desc_parts.append(f'constant 0x{constant.hex().upper()}')
            desc_parts.append(f'verified in {match_count}/{total_check} frames')
            description = ', '.join(desc_parts)

            relations.append(
                LinearRelation(
                    output_bits=output_bits,
                    input_bits=input_bits,
                    constant=constant,
                    algorithm=algorithm,
                    polynomial=polynomial,
                    description=description,
                    confidence=match_count / total_check if total_check else 0.0,
                )
            )

    # Deduplicate: keep the most specific relation for each output range
    seen_outputs: set = set()
    unique: List[LinearRelation] = []
    # Prefer CRC matches and larger output widths
    relations.sort(key=lambda r: (r.polynomial is not None, len(r.output_bits)), reverse=True)
    for rel in relations:
        key = tuple(sorted(rel.output_bits))
        if key not in seen_outputs:
            seen_outputs.add(key)
            unique.append(rel)
    return unique
