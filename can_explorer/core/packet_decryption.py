"""Decrypting IPsec ESP and MACsec frames of an Ethernet capture with security associations from the key store.

A decrypted frame is rebuilt as the plaintext Ethernet frame it protected, so the normal dissector (core/ethernet.py)
decodes what it carried (SOME/IP, DoIP, …). Nothing here establishes a connection: IKE and MKA, which negotiate
these keys, are the operating system's job; the keys are entered in the key store (e.g. from `ip xfrm state` or
the MKA logs of a test setup).

IPsec ESP (RFC 4303), with the SA chosen by its SPI:

- AES-GCM (RFC 4106) with a 16-byte ICV and an 8-byte IV; the key is followed by its 4-byte salt.
- AES-CBC (RFC 3602) with a 16-byte IV, or NULL encryption (RFC 2410), each with HMAC-SHA-256-128 (RFC 4868) or
  HMAC-SHA-1-96 (RFC 2404). The ICV is verified before decrypting.
- Tunnel mode (an inner IPv4/IPv6 packet) and transport mode (the outer IP header is rebuilt with the inner
  protocol). Extended sequence numbers, ESP after IPv6 extension headers and other algorithms are not supported.

MACsec (IEEE 802.1AE) with GCM-AES-128 or GCM-AES-256 (not the XPN suites), both encrypted (E bit) and
integrity-only frames. The IV is the SCI and the packet number. A frame without an explicit SCI from an end station
(ES bit) has the implicit SCI of its source address and port 1; any other frame without one is decrypted only with
an SA whose SCI is entered, since its port does not show in a capture. The tests check both paths against the
IEEE 802.1AE GCM-AES-128 test vectors.
"""

import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from can_explorer.core import audit, keystore
from can_explorer.core.ethernet import ETH_IPV4, ETH_IPV6, ETH_MACSEC, ETH_RTAG, ETH_VLAN
from can_explorer.core.keystore import KeyStore, KeyStoreError

ESP_PROTOCOL = 50
GCM_ICV = 16
_INTEGRITY = {'hmac-sha256-128': (hashlib.sha256, 16), 'hmac-sha1-96': (hashlib.sha1, 12)}


@dataclass(frozen=True)
class EspSa:
    name: str
    spi: int
    encryption: str  # 'aes-gcm', 'aes-cbc', 'null'
    encryption_key: bytes = b''
    integrity: str = 'none'
    integrity_key: bytes = b''

    def __repr__(self) -> str:
        return f'EspSa({self.name!r}, spi=0x{self.spi:08X}, {self.encryption}, {self.integrity})'


@dataclass(frozen=True)
class MacsecSa:
    name: str
    sak: bytes
    sci: Optional[int] = None  # None: any secure channel

    def __repr__(self) -> str:
        sci = f'{self.sci:016X}' if self.sci is not None else 'any'
        return f'MacsecSa({self.name!r}, sci={sci}, gcm-aes-{len(self.sak) * 8})'


@dataclass(frozen=True)
class Decrypted:
    """The outcome for a protected frame: `frame` is the plaintext Ethernet frame, or None with `error`."""

    label: str  # e.g. 'ESP SPI 0x00001000 (lab-sa)'
    frame: Optional[bytes] = None
    error: str = ''

    def describe(self) -> str:
        return f'{self.label} decrypted' if self.frame is not None else f'{self.label}: {self.error}'


def _l3(data: bytes) -> Tuple[int, int, int]:
    """(EtherType, offset of the layer-3 header, offset of the EtherType field), past VLAN tags and the R-TAG."""
    position = 12
    ethertype = struct.unpack('>H', data[12:14])[0]
    offset = 14
    while True:
        if ethertype == ETH_VLAN and len(data) >= offset + 4:
            position = offset + 2
            ethertype = struct.unpack('>H', data[offset + 2 : offset + 4])[0]
            offset += 4
        elif ethertype == ETH_RTAG and len(data) >= offset + 6:
            position = offset + 4
            ethertype = struct.unpack('>H', data[offset + 4 : offset + 6])[0]
            offset += 6
        else:
            return ethertype, offset, position


def _ipv4_checksum(header: bytes) -> int:
    total = sum(struct.unpack(f'>{len(header) // 2}H', header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


class PacketDecryptor:
    """Decrypts ESP and MACsec frames with the given security associations."""

    def __init__(self, esp: Sequence[EspSa] = (), macsec: Sequence[MacsecSa] = ()):
        self.esp: Dict[int, EspSa] = {sa.spi: sa for sa in esp}
        self.macsec: List[MacsecSa] = list(macsec)

    def __bool__(self) -> bool:
        return bool(self.esp or self.macsec)

    @classmethod
    def from_keystore(cls, store: KeyStore) -> 'PacketDecryptor':
        """The IPsec and MACsec associations of an unlocked key store (none while it is locked)."""
        if not store.unlocked or not store.exists:
            return cls()
        esp, macsec = [], []
        for info in store.entries(keystore.IPSEC):
            try:
                secrets = store.secrets(info.name)
                esp.append(
                    EspSa(
                        info.name,
                        int(info.params['spi'], 16),
                        info.params['encryption'],
                        secrets.get('encryption_key', b''),
                        info.params.get('integrity', 'none'),
                        secrets.get('integrity_key', b''),
                    )
                )
            except (KeyStoreError, KeyError, ValueError):
                continue
        for info in store.entries(keystore.MACSEC):
            try:
                sci = info.params.get('sci', '')
                macsec.append(MacsecSa(info.name, store.secrets(info.name)['sak'], int(sci, 16) if sci else None))
            except (KeyStoreError, KeyError, ValueError):
                continue
        if esp or macsec:
            audit.record(
                'decryption associations used', ipsec=[sa.name for sa in esp], macsec=[sa.name for sa in macsec]
            )
        return cls(esp, macsec)

    def decrypt(self, data: bytes) -> Optional[Decrypted]:
        """The decrypted frame, or why it could not be decrypted; None if the frame is neither ESP nor MACsec or
        no association is configured for its kind."""
        if len(data) < 14:
            return None
        try:
            ethertype, offset, position = _l3(data)
            if ethertype == ETH_MACSEC and self.macsec:
                return self._macsec(data, offset)
            if ethertype == ETH_IPV4 and self.esp and len(data) >= offset + 20 and data[offset + 9] == ESP_PROTOCOL:
                ihl = (data[offset] & 0x0F) * 4
                end = min(len(data), offset + struct.unpack('>H', data[offset + 2 : offset + 4])[0])
                return self._esp(data, offset, ihl, end, position, ipv6=False)
            if ethertype == ETH_IPV6 and self.esp and len(data) >= offset + 40 and data[offset + 6] == ESP_PROTOCOL:
                end = min(len(data), offset + 40 + struct.unpack('>H', data[offset + 4 : offset + 6])[0])
                return self._esp(data, offset, 40, end, position, ipv6=True)
        except (struct.error, IndexError, ValueError) as e:
            return Decrypted('Decryption', error=f'malformed frame ({e})')
        return None

    # ESP

    def _esp(self, data: bytes, offset: int, header: int, end: int, position: int, ipv6: bool) -> Optional[Decrypted]:
        esp = bytes(data[offset + header : end])
        if len(esp) < 8:
            return Decrypted('ESP', error='shorter than the ESP header')
        spi = struct.unpack('>I', esp[:4])[0]
        sa = self.esp.get(spi)
        label = f'ESP SPI 0x{spi:08X}'
        if sa is None:
            return Decrypted(label, error='no security association for this SPI')
        label += f' ({sa.name})'
        try:
            plain = self._esp_plaintext(sa, esp)
        except _Failed as e:
            return Decrypted(label, error=str(e))
        if len(plain) < 2 or plain[-2] + 2 > len(plain):
            return Decrypted(label, error='bad ESP trailer (wrong key?)')
        pad_length, next_header = plain[-2], plain[-1]
        payload = plain[: -2 - pad_length]
        if next_header == 59:
            return Decrypted(label, error='dummy packet (no next header)')
        if next_header == 4:  # Tunnel mode, inner IPv4
            return Decrypted(label, bytes(data[:position]) + struct.pack('>H', ETH_IPV4) + payload)
        if next_header == 41:  # Tunnel mode, inner IPv6
            return Decrypted(label, bytes(data[:position]) + struct.pack('>H', ETH_IPV6) + payload)
        outer = bytearray(data[offset : offset + header])  # Transport mode: the outer header carries the payload
        if ipv6:
            outer[6] = next_header
            outer[4:6] = struct.pack('>H', len(payload))
        else:
            outer[9] = next_header
            outer[2:4] = struct.pack('>H', header + len(payload))
            outer[10:12] = b'\0\0'
            outer[10:12] = struct.pack('>H', _ipv4_checksum(bytes(outer)))
        return Decrypted(label, bytes(data[:offset]) + bytes(outer) + payload)

    @staticmethod
    def _esp_plaintext(sa: EspSa, esp: bytes) -> bytes:
        if sa.encryption == 'aes-gcm':
            if len(esp) < 8 + 8 + GCM_ICV:
                raise _Failed('too short for AES-GCM')
            key, salt = sa.encryption_key[:-4], sa.encryption_key[-4:]
            try:
                return AESGCM(key).decrypt(salt + esp[8:16], esp[16:], esp[:8])
            except InvalidTag:
                raise _Failed('integrity check failed (wrong key, or not AES-GCM with a 16-byte ICV)') from None
        hash_function, icv_length = _INTEGRITY.get(sa.integrity, (None, 0))
        if hash_function is None:
            raise _Failed(f'{sa.encryption} needs an integrity algorithm')
        if len(esp) < 8 + icv_length:
            raise _Failed('too short for its integrity check value')
        body, icv = esp[:-icv_length], esp[-icv_length:]
        expected = hmac.new(sa.integrity_key, body, hash_function).digest()[:icv_length]
        if not hmac.compare_digest(expected, icv):
            raise _Failed('integrity check failed (wrong integrity key or algorithm)')
        if sa.encryption == 'null':
            return body[8:]
        iv, ciphertext = body[8:24], body[24:]
        if len(iv) != 16 or len(ciphertext) % 16:
            raise _Failed('the ciphertext is not whole AES-CBC blocks')
        decryptor = Cipher(algorithms.AES(sa.encryption_key), modes.CBC(iv)).decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()

    # MACsec

    def _macsec(self, data: bytes, offset: int) -> Decrypted:
        if offset != 14:
            return Decrypted('MACsec', error='a VLAN tag before the SecTAG is not supported')
        if len(data) < offset + 6 + GCM_ICV:
            return Decrypted('MACsec', error='shorter than the SecTAG and ICV')
        tci, short_length = data[offset], data[offset + 1] & 0x3F
        packet_number = data[offset + 2 : offset + 6]
        has_sci = bool(tci & 0x20)
        sectag = 6 + (8 if has_sci else 0)
        if has_sci:
            frame_sci: Optional[int] = int.from_bytes(data[offset + 6 : offset + 14], 'big')
        elif tci & 0x40:  # End station: the source address with port identifier 1
            frame_sci = (int.from_bytes(data[6:12], 'big') << 16) | 0x0001
        else:
            frame_sci = None
        start = offset + sectag
        end = start + short_length if short_length else len(data) - GCM_ICV
        if end + GCM_ICV > len(data) or end < start:
            return Decrypted('MACsec', error='inconsistent length')
        secure_data, icv = bytes(data[start:end]), bytes(data[end : end + GCM_ICV])
        aad = bytes(data[:start])
        encrypted = bool(tci & 0x08)
        label = f'MACsec AN {tci & 0x03} PN {int.from_bytes(packet_number, "big")}'
        candidates = [sa for sa in self.macsec if frame_sci is None or sa.sci is None or sa.sci == frame_sci]
        if not candidates:
            return Decrypted(label, error=f'no secure association for SCI {frame_sci:016X}')
        tried_without_sci = False
        for sa in candidates:
            sci = frame_sci if frame_sci is not None else sa.sci
            if sci is None:
                tried_without_sci = True
                continue
            nonce = sci.to_bytes(8, 'big') + packet_number
            try:
                if encrypted:
                    plain = AESGCM(sa.sak).decrypt(nonce, secure_data + icv, aad)
                else:
                    AESGCM(sa.sak).decrypt(nonce, icv, aad + secure_data)  # Integrity only
                    plain = secure_data
            except InvalidTag:
                continue
            return Decrypted(f'{label} ({sa.name})', bytes(data[:12]) + plain)
        if tried_without_sci and len(candidates) == 1:
            return Decrypted(label, error='the frame has no SCI; enter the SCI of the secure association')
        return Decrypted(label, error='integrity check failed (wrong SAK, or an XPN cipher suite)')


class _Failed(Exception):
    pass
