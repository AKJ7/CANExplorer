"""AUTOSAR Secure Onboard Communication (SecOC): verify the authenticator (MAC) and freshness of a secured PDU.

Where E2E (core/e2e.py) protects a PDU against corruption with a CRC and a counter, SecOC protects it against
forgery with a truncated message authentication code (MAC) and a freshness value (a monotonic counter or
timestamp). A secured PDU on the bus is the authentic payload followed by the truncated freshness value and the
truncated MAC:

    [ authentic payload | truncated freshness | truncated MAC ]

The MAC is computed over the 16-bit Data ID, the authentic payload and the *complete* freshness value. This module
holds the primitives: CMAC over AES (with the `cryptography` package; the tests check the NIST SP 800-38B and FIPS-197
vectors) and byte-aligned generate/verify/split. The live check of secured PDUs configured in an ARXML, with
bit-packed trailers and freshness reconstruction, is in core/secoc_check.py.
"""

import hmac
from typing import Optional, Tuple

import can
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

AES_KEY_LENGTHS = (16, 24, 32)


def aes128_encrypt(key: bytes, block: bytes) -> bytes:
    """Encrypt one 16-byte block with AES-128 (ECB, single block)."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError('AES-128 needs a 16-byte key and a 16-byte block')
    encryptor = Cipher(algorithms.AES(bytes(key)), modes.ECB()).encryptor()
    return encryptor.update(bytes(block)) + encryptor.finalize()


def cmac_aes(key: bytes, message: bytes) -> bytes:
    """The 16-byte CMAC (NIST SP 800-38B) of `message` under AES with a 16, 24 or 32-byte key."""
    if len(key) not in AES_KEY_LENGTHS:
        raise ValueError(f'CMAC/AES needs a 16, 24 or 32-byte key, not {len(key)} bytes')
    mac = CMAC(algorithms.AES(bytes(key)))
    mac.update(bytes(message))
    return mac.finalize()


def cmac_aes128(key: bytes, message: bytes) -> bytes:
    """The 16-byte CMAC of `message` under AES-128 (NIST SP 800-38B)."""
    if len(key) != 16:
        raise ValueError('AES-128 needs a 16-byte key')
    return cmac_aes(key, message)


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
