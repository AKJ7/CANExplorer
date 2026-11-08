"""AUTOSAR Secure Onboard Communication (SecOC): verify the authenticator (MAC) and freshness of a secured PDU.

Where E2E (core/e2e.py) protects a PDU against corruption with a CRC and a counter, SecOC protects it against
forgery with a truncated message authentication code (MAC) and a freshness value (a monotonic counter or
timestamp). A secured PDU on the bus is the authentic payload followed by the truncated freshness value and the
truncated MAC:

    [ authentic payload | truncated freshness | truncated MAC ]

The MAC is computed over the 16-bit Data ID, the authentic payload and the *complete* freshness value. This module
implements the common CMAC/AES-128 algorithm (verified against the NIST SP 800-38B / FIPS-197 test vectors) and the
generate/verify operations; validating a live PDU needs its SecOC configuration (Data ID, the key, and the
truncated lengths) from the ARXML. Byte-aligned payload/freshness lengths and a MAC length that is a whole number
of bits are handled.
"""

import hmac
from typing import Optional, Tuple

import can

# --- AES-128 (FIPS-197), enough for CMAC: single-block encryption ---

_SBOX = bytes.fromhex(
    '637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0'
    'b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275'
    '09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf'
    'd0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2'
    'cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb'
    'e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08'
    'ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e'
    'e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16'
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _key_schedule(key: bytes) -> list:
    """Expand a 16-byte key into 11 round keys (176 bytes), column-major like the AES state."""
    schedule = list(key)
    for i in range(4, 44):
        word = schedule[(i - 1) * 4 : i * 4]
        if i % 4 == 0:
            word = word[1:] + word[:1]  # RotWord
            word = [_SBOX[b] for b in word]  # SubWord
            word[0] ^= _RCON[i // 4 - 1]
        previous = schedule[(i - 4) * 4 : (i - 3) * 4]
        schedule += [previous[j] ^ word[j] for j in range(4)]
    return schedule


def _encrypt_block(schedule: list, block: bytes) -> bytes:
    state = [block[i] ^ schedule[i] for i in range(16)]
    for rnd in range(1, 11):
        state = [_SBOX[b] for b in state]  # SubBytes
        # ShiftRows: row r (indices r, r+4, r+8, r+12) rotates left by r
        state = [state[(i % 4) + 4 * ((i // 4 + i % 4) % 4)] for i in range(16)]
        if rnd != 10:  # MixColumns
            mixed = []
            for c in range(4):
                s0, s1, s2, s3 = state[4 * c : 4 * c + 4]
                t = s0 ^ s1 ^ s2 ^ s3
                mixed += [
                    s0 ^ t ^ _xtime(s0 ^ s1),
                    s1 ^ t ^ _xtime(s1 ^ s2),
                    s2 ^ t ^ _xtime(s2 ^ s3),
                    s3 ^ t ^ _xtime(s3 ^ s0),
                ]
            state = mixed
        state = [state[i] ^ schedule[16 * rnd + i] for i in range(16)]  # AddRoundKey
    return bytes(state)


def aes128_encrypt(key: bytes, block: bytes) -> bytes:
    """Encrypt one 16-byte block with AES-128 (ECB, single block)."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError('AES-128 needs a 16-byte key and a 16-byte block')
    return _encrypt_block(_key_schedule(key), block)


# --- CMAC (NIST SP 800-38B) over AES-128 ---


def _double(block: bytes) -> bytes:
    value = int.from_bytes(block, 'big')
    shifted = (value << 1) & ((1 << 128) - 1)
    if value & (1 << 127):
        shifted ^= 0x87  # Rb for a 128-bit block
    return shifted.to_bytes(16, 'big')


def cmac_aes128(key: bytes, message: bytes) -> bytes:
    """The 16-byte CMAC of `message` under AES-128 (NIST SP 800-38B)."""
    schedule = _key_schedule(key)
    subkey = _double(_encrypt_block(schedule, bytes(16)))  # K1 = double(AES(key, 0))
    blocks = (len(message) + 15) // 16 or 1
    last = message[(blocks - 1) * 16 :]
    if len(last) == 16:
        last_block = bytes(a ^ b for a, b in zip(last, subkey, strict=True))
    else:  # Incomplete (or empty) last block: pad with 0x80 00…, use K2
        padded = last + b'\x80' + b'\x00' * (15 - len(last))
        subkey = _double(subkey)  # K2 = double(K1)
        last_block = bytes(a ^ b for a, b in zip(padded, subkey, strict=True))
    x = bytes(16)
    for index in range(blocks - 1):
        block = message[index * 16 : index * 16 + 16]
        x = _encrypt_block(schedule, bytes(a ^ b for a, b in zip(x, block, strict=True)))
    return _encrypt_block(schedule, bytes(a ^ b for a, b in zip(x, last_block, strict=True)))


# --- SecOC ---


def _truncate(mac: bytes, bits: int) -> bytes:
    """The most significant `bits` bits of a MAC, as ceil(bits/8) bytes with the unused low bits cleared."""
    out = bytearray(mac[: (bits + 7) // 8])
    if bits % 8 and out:
        out[-1] &= (0xFF << (8 - bits % 8)) & 0xFF
    return bytes(out)


def data_to_authenticator(data_id: int, authentic: bytes, freshness_value: bytes) -> bytes:
    """The input the MAC is computed over: the 16-bit Data ID, the authentic payload and the full freshness value."""
    return (data_id & 0xFFFF).to_bytes(2, 'big') + bytes(authentic) + bytes(freshness_value)


def generate_mac(key: bytes, data_id: int, authentic: bytes, freshness_value: bytes, mac_length_bits: int) -> bytes:
    """The truncated SecOC authenticator for a PDU."""
    full = cmac_aes128(key, data_to_authenticator(data_id, authentic, freshness_value))
    return _truncate(full, mac_length_bits)


def verify(
    key: bytes, data_id: int, authentic: bytes, freshness_value: bytes, received_mac: bytes, mac_length_bits: int
) -> bool:
    """Whether `received_mac` matches the authenticator recomputed from the key, Data ID, payload and freshness."""
    expected = generate_mac(key, data_id, authentic, freshness_value, mac_length_bits)
    return hmac.compare_digest(expected, _truncate(bytes(received_mac), mac_length_bits))


def split(data: bytes, mac_bytes: int, freshness_bytes: int = 0) -> Optional[Tuple[bytes, bytes, bytes]]:
    """Split a secured PDU into (authentic payload, truncated freshness, truncated MAC) given the trailing lengths."""
    if len(data) < mac_bytes + freshness_bytes:
        return None
    end = len(data)
    mac = data[end - mac_bytes :]
    freshness = data[end - mac_bytes - freshness_bytes : end - mac_bytes]
    return bytes(data[: end - mac_bytes - freshness_bytes]), bytes(freshness), bytes(mac)


class SecOcDecoder:
    """Passive SecOC decoder: shows the secured-PDU structure (authentic payload, freshness, MAC). The trailing
    truncated freshness and MAC lengths are configuration, so they are given here (defaulting to a 4-byte MAC and no
    in-PDU freshness); verifying the MAC needs the key and is done with `verify`."""

    name = 'SecOC'

    def __init__(self, mac_bytes: int = 4, freshness_bytes: int = 0):
        self.mac_bytes, self.freshness_bytes = mac_bytes, freshness_bytes

    def reset(self) -> None:
        pass

    def describe(self, msg: can.Message) -> Optional[str]:
        parts = split(bytes(msg.data), self.mac_bytes, self.freshness_bytes)
        if parts is None:
            return None
        authentic, freshness, mac = parts
        text = f'SecOC: authentic {authentic.hex(" ").upper() or "—"}'
        if freshness:
            text += f' | freshness {freshness.hex().upper()}'
        return text + f' | MAC {mac.hex().upper()}'
