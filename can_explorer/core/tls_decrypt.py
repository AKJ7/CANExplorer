"""Decrypting TLS sessions of an Ethernet capture (e.g. DoIP on TCP port 3496) with a key log.

A key log in the NSS format — what the `SSLKEYLOGFILE` environment variable makes CANExplorer, browsers, curl and
other OpenSSL/BoringSSL/NSS programs write — holds the secrets of each session, keyed by its client random:

    CLIENT_RANDOM <client random> <master secret>                        TLS 1.2
    CLIENT_HANDSHAKE_TRAFFIC_SECRET <client random> <secret>             TLS 1.3, and SERVER_HANDSHAKE_…,
    CLIENT_TRAFFIC_SECRET_0 <client random> <secret>                     CLIENT_/SERVER_TRAFFIC_SECRET_0

`TlsDecryptor` follows the TCP connections of a capture (in capture order, reassembling each direction by sequence
number), reads the client and server randoms and the cipher suite from the handshake, derives the record keys
(the TLS 1.2 PRF of RFC 5246; the TLS 1.3 key schedule of RFC 8446, including KeyUpdate) and decrypts the records.

Supported: TLS 1.2 and 1.3 with AES-GCM and ChaCha20-Poly1305 — the AEAD suites, which TLS 1.3 requires and
DoIP (ISO 13400-2:2019) uses. Not supported: CBC suites, TLS 1.3 early data (0-RTT), compression, renegotiation, and
sessions whose handshake is not in the capture. Sessions without secrets in the key log are reported, not guessed.
"""

import hashlib
import hmac
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

CHANGE_CIPHER_SPEC, ALERT, HANDSHAKE, APPLICATION_DATA = 20, 21, 22, 23
CONTENT_TYPES = {20: 'change cipher spec', 21: 'alert', 22: 'handshake', 23: 'application data'}
CLIENT_HELLO, SERVER_HELLO, FINISHED, KEY_UPDATE = 1, 2, 20, 24
TLS12, TLS13 = 0x0303, 0x0304
_MAX_RECORD = 18432  # 2^14 plus the largest expansion; longer "records" mean the stream is not TLS
_UNSTARTED_LIMIT = 65536  # Bytes kept waiting for the start of a stream before it is taken as not TLS


@dataclass(frozen=True)
class CipherSuite:
    name: str
    cipher: str  # 'aes-gcm' or 'chacha20'
    key_length: int
    hash_name: str  # 'sha256' or 'sha384'


# TLS 1.3 suites, and the TLS 1.2 AEAD suites (IANA numbers)
SUITES: Dict[int, CipherSuite] = {
    0x1301: CipherSuite('TLS_AES_128_GCM_SHA256', 'aes-gcm', 16, 'sha256'),
    0x1302: CipherSuite('TLS_AES_256_GCM_SHA384', 'aes-gcm', 32, 'sha384'),
    0x1303: CipherSuite('TLS_CHACHA20_POLY1305_SHA256', 'chacha20', 32, 'sha256'),
    0x009C: CipherSuite('TLS_RSA_WITH_AES_128_GCM_SHA256', 'aes-gcm', 16, 'sha256'),
    0x009D: CipherSuite('TLS_RSA_WITH_AES_256_GCM_SHA384', 'aes-gcm', 32, 'sha384'),
    0x009E: CipherSuite('TLS_DHE_RSA_WITH_AES_128_GCM_SHA256', 'aes-gcm', 16, 'sha256'),
    0x009F: CipherSuite('TLS_DHE_RSA_WITH_AES_256_GCM_SHA384', 'aes-gcm', 32, 'sha384'),
    0xC02B: CipherSuite('TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256', 'aes-gcm', 16, 'sha256'),
    0xC02C: CipherSuite('TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384', 'aes-gcm', 32, 'sha384'),
    0xC02F: CipherSuite('TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256', 'aes-gcm', 16, 'sha256'),
    0xC030: CipherSuite('TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384', 'aes-gcm', 32, 'sha384'),
    0xCCA8: CipherSuite('TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256', 'chacha20', 32, 'sha256'),
    0xCCA9: CipherSuite('TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256', 'chacha20', 32, 'sha256'),
    0xCCAA: CipherSuite('TLS_DHE_RSA_WITH_CHACHA20_POLY1305_SHA256', 'chacha20', 32, 'sha256'),
}


class KeyLogError(ValueError):
    pass


class KeyLog:
    """The secrets of a key log file: {client random: {label: secret}}."""

    def __init__(self, secrets: Optional[Dict[bytes, Dict[str, bytes]]] = None):
        self.secrets: Dict[bytes, Dict[str, bytes]] = secrets or {}

    def __len__(self) -> int:
        return len(self.secrets)

    def __repr__(self) -> str:  # Never show the secrets
        return f'KeyLog({len(self.secrets)} sessions)'

    @classmethod
    def parse(cls, text: str) -> 'KeyLog':
        secrets: Dict[bytes, Dict[str, bytes]] = {}
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) != 3:
                raise KeyLogError(f'Line {number} is not "LABEL <client random> <secret>"')
            label, random_hex, secret_hex = parts
            try:
                client_random, secret = bytes.fromhex(random_hex), bytes.fromhex(secret_hex)
            except ValueError:
                raise KeyLogError(f'Line {number} is not hexadecimal') from None
            if len(client_random) != 32:
                raise KeyLogError(f'Line {number}: a client random has 32 bytes')
            secrets.setdefault(client_random, {})[label] = secret
        return cls(secrets)

    @classmethod
    def load(cls, path: str | Path) -> 'KeyLog':
        try:
            return cls.parse(Path(path).read_text(encoding='ascii', errors='replace'))
        except OSError as e:
            raise KeyLogError(f'Cannot read {path}: {e}') from e

    def get(self, client_random: bytes, label: str) -> Optional[bytes]:
        return self.secrets.get(client_random, {}).get(label)


# --- Key derivation ---


def _hash(name: str):
    return hashes.SHA384() if name == 'sha384' else hashes.SHA256()


def prf12(secret: bytes, label: bytes, seed: bytes, length: int, hash_name: str) -> bytes:
    """The TLS 1.2 PRF (RFC 5246 section 5): P_hash(secret, label + seed)."""
    digest = hashlib.sha384 if hash_name == 'sha384' else hashlib.sha256
    seed = label + seed
    output, a = b'', seed
    while len(output) < length:
        a = hmac.new(secret, a, digest).digest()
        output += hmac.new(secret, a + seed, digest).digest()
    return output[:length]


def hkdf_expand_label(secret: bytes, label: str, context: bytes, length: int, hash_name: str) -> bytes:
    """HKDF-Expand-Label of the TLS 1.3 key schedule (RFC 8446 section 7.1)."""
    full_label = b'tls13 ' + label.encode('ascii')
    info = struct.pack('>HB', length, len(full_label)) + full_label + bytes([len(context)]) + context
    return HKDFExpand(_hash(hash_name), length, info).derive(secret)


@dataclass
class _Keys:
    """The record protection of one direction: key, IV, the record sequence number, and for TLS 1.3 the
    traffic secret (for KeyUpdate)."""

    suite: CipherSuite
    key: bytes
    iv: bytes  # TLS 1.2 GCM: the 4-byte salt; otherwise 12 bytes
    tls13: bool
    secret: bytes = b''
    sequence: int = 0

    def _aead(self):
        return AESGCM(self.key) if self.suite.cipher == 'aes-gcm' else ChaCha20Poly1305(self.key)

    def _nonce(self, explicit: bytes = b'') -> bytes:
        if explicit:  # TLS 1.2 AES-GCM: salt + the record's explicit nonce
            return self.iv + explicit
        padded = self.sequence.to_bytes(12, 'big')
        return bytes(a ^ b for a, b in zip(self.iv, padded, strict=True))

    def decrypt(self, content_type: int, version: int, fragment: bytes) -> Tuple[int, bytes]:
        """(content type, plaintext) of a protected record. Raises InvalidTag or ValueError."""
        if self.tls13:
            header = struct.pack('>BHH', content_type, version, len(fragment))
            inner = self._aead().decrypt(self._nonce(), fragment, header).rstrip(b'\0')
            if not inner:
                raise ValueError('empty inner plaintext')
            self.sequence += 1
            return inner[-1], inner[:-1]
        explicit = b''
        if self.suite.cipher == 'aes-gcm':
            explicit, fragment = fragment[:8], fragment[8:]
        if len(fragment) < 16:
            raise ValueError('record shorter than its authentication tag')
        aad = struct.pack('>QBHH', self.sequence, content_type, version, len(fragment) - 16)
        plain = self._aead().decrypt(self._nonce(explicit), fragment, aad)
        self.sequence += 1
        return content_type, plain


def _keys13(suite: CipherSuite, secret: bytes) -> _Keys:
    key = hkdf_expand_label(secret, 'key', b'', suite.key_length, suite.hash_name)
    iv = hkdf_expand_label(secret, 'iv', b'', 12, suite.hash_name)
    return _Keys(suite, key, iv, True, secret)


# --- Connections ---


@dataclass(frozen=True)
class TlsRecord:
    """One record of a session: who sent it, its content type, and its plaintext (or why there is none)."""

    sender: str  # 'client' or 'server'
    content_type: int
    plaintext: Optional[bytes] = None
    error: str = ''

    @property
    def kind(self) -> str:
        return CONTENT_TYPES.get(self.content_type, f'type {self.content_type}')

    def describe(self) -> str:
        if self.error:
            return f'{self.sender} {self.kind}: {self.error}'
        size = f', {len(self.plaintext)} bytes' if self.plaintext is not None else ''
        return f'{self.sender} {self.kind}{size}'


@dataclass
class _Direction:
    next_seq: Optional[int] = None
    pending: Dict[int, bytes] = field(default_factory=dict)
    buffer: bytearray = field(default_factory=bytearray)
    handshake: bytearray = field(default_factory=bytearray)  # Decrypted TLS 1.3 handshake messages, until complete
    keys: Optional[_Keys] = None
    protected: bool = False  # TLS 1.2: after its ChangeCipherSpec
    broken: bool = False  # Not TLS, or out of sync: stop reading
    started: bool = False  # A record was read: the stream start is known

    def add(self, seq: int, payload: bytes) -> None:
        """Append in-order TCP data (retransmissions dropped, later segments kept until the gap fills). Until the
        first record is read, an earlier segment arriving late moves the start of the stream back."""
        if self.next_seq is None:
            self.next_seq = seq
        offset = seq - self.next_seq
        if offset < 0 and not self.started and seq + len(payload) <= self.next_seq - len(self.buffer):
            if self.buffer:
                self.pending[self.next_seq - len(self.buffer)] = bytes(self.buffer)
            self.buffer.clear()
            self.next_seq, offset = seq, 0
        if offset < 0:
            payload = payload[-offset:]
            offset = 0
        if offset > 0:
            self.pending[seq] = payload
            return
        self.buffer += payload
        self.next_seq += len(payload)
        while self.next_seq in self.pending:
            later = self.pending.pop(self.next_seq)
            self.buffer += later
            self.next_seq += len(later)


@dataclass
class _Session:
    client: Optional[Tuple] = None  # (address, port) of the client, once its ClientHello is seen
    client_random: Optional[bytes] = None
    server_random: Optional[bytes] = None
    suite_number: Optional[int] = None
    version: int = TLS12
    error: str = ''  # Why the session cannot be decrypted
    directions: Dict[Tuple, _Direction] = field(default_factory=dict)

    @property
    def suite(self) -> Optional[CipherSuite]:
        return SUITES.get(self.suite_number) if self.suite_number is not None else None


def _handshake_messages(buffer: bytearray) -> List[Tuple[int, bytes]]:
    """Complete handshake messages at the front of `buffer` (removed from it)."""
    messages = []
    while len(buffer) >= 4:
        length = int.from_bytes(buffer[1:4], 'big')
        if len(buffer) < 4 + length:
            break
        messages.append((buffer[0], bytes(buffer[4 : 4 + length])))
        del buffer[: 4 + length]
    return messages


def _server_hello(body: bytes) -> Tuple[bytes, int, int]:
    """(server random, cipher suite, negotiated version) from a ServerHello body."""
    version = int.from_bytes(body[0:2], 'big')
    server_random = body[2:34]
    position = 34
    position += 1 + body[position]  # Session id
    suite = int.from_bytes(body[position : position + 2], 'big')
    position += 3  # Suite and compression method
    if len(body) >= position + 2:
        end = position + 2 + int.from_bytes(body[position : position + 2], 'big')
        position += 2
        while position + 4 <= end:
            kind, length = struct.unpack_from('>HH', body, position)
            if kind == 0x002B and length == 2:  # supported_versions: the selected version
                version = int.from_bytes(body[position + 4 : position + 6], 'big')
            position += 4 + length
    return server_random, suite, version


class TlsDecryptor:
    """Decrypts the TLS sessions of a capture fed segment by segment (`feed`), in capture order."""

    def __init__(self, keylog: KeyLog):
        self.keylog = keylog
        self._sessions: Dict[FrozenSet, _Session] = {}

    def feed(self, flow: Tuple, seq: int, payload: bytes) -> List[TlsRecord]:
        """The records completed by one TCP segment of direction `flow` (src, src port, dst, dst port)."""
        if not payload:
            return []
        source, destination = (flow[0], flow[1]), (flow[2], flow[3])
        key = frozenset((source, destination))
        session = self._sessions.get(key)
        if session is None:
            session = self._sessions[key] = _Session()
        direction = session.directions.get(source)
        if direction is None:
            direction = session.directions[source] = _Direction()
        if direction.broken:
            return []
        direction.add(seq, payload)
        records = []
        while len(direction.buffer) >= 5:
            content_type, version, length = struct.unpack_from('>BHH', direction.buffer)
            if content_type not in CONTENT_TYPES or version >> 8 != 3 or length > _MAX_RECORD:
                # Not TLS, or a capture starting mid-stream; before the first record, perhaps a reordered start
                if direction.started or len(direction.buffer) > _UNSTARTED_LIMIT:
                    direction.broken = True
                    direction.buffer.clear()
                break
            if len(direction.buffer) < 5 + length:
                break
            fragment = bytes(direction.buffer[5 : 5 + length])
            del direction.buffer[: 5 + length]
            direction.started = True
            records.append(self._record(session, source, direction, content_type, version, fragment))
        return records

    def _sender(self, session: _Session, source: Tuple) -> str:
        return 'client' if session.client == source else 'server'

    def _record(
        self, session: _Session, source: Tuple, direction: _Direction, content_type: int, version: int, fragment: bytes
    ) -> TlsRecord:
        if content_type == HANDSHAKE and not direction.protected and direction.keys is None:
            for kind, body in _handshake_messages(bytearray(fragment)):
                if kind == CLIENT_HELLO and len(body) >= 34:
                    session.client, session.client_random = source, body[2:34]
                elif kind == SERVER_HELLO and len(body) >= 38:
                    session.server_random, session.suite_number, session.version = _server_hello(body)
                    self._start(session)
            return TlsRecord(self._sender(session, source), content_type, fragment)
        if content_type == CHANGE_CIPHER_SPEC:
            if session.version == TLS12:
                direction.protected = True
                direction.keys = self._keys12(session, source)
            return TlsRecord(self._sender(session, source), content_type, fragment)
        sender = self._sender(session, source)
        protected = direction.protected or (session.version == TLS13 and content_type == APPLICATION_DATA)
        if not protected:
            return TlsRecord(sender, content_type, fragment)
        if direction.keys is None:
            return TlsRecord(sender, content_type, error=session.error or 'no keys for this session')
        try:
            inner_type, plain = direction.keys.decrypt(content_type, version, fragment)
        except (InvalidTag, ValueError):
            return TlsRecord(sender, content_type, error='decryption failed (wrong secrets in the key log?)')
        if session.version == TLS13 and inner_type == HANDSHAKE:
            self._handshake13(session, source, direction, plain)
        return TlsRecord(sender, inner_type, plain)

    # Keys

    def _start(self, session: _Session) -> None:
        """After the ServerHello: check the suite and the key log, and for TLS 1.3 install the handshake keys."""
        suite = session.suite
        if suite is None:
            session.error = f'cipher suite 0x{session.suite_number:04X} is not supported (only AEAD suites)'
            return
        if session.client_random is None:
            session.error = 'the ClientHello is not in the capture'
            return
        if session.version != TLS13:
            if self.keylog.get(session.client_random, 'CLIENT_RANDOM') is None:
                session.error = 'no CLIENT_RANDOM for this session in the key log'
            return
        for endpoint, direction in session.directions.items():
            label = 'CLIENT' if endpoint == session.client else 'SERVER'
            secret = self.keylog.get(session.client_random, f'{label}_HANDSHAKE_TRAFFIC_SECRET')
            if secret is None:
                session.error = 'no TLS 1.3 handshake secrets for this session in the key log'
                return
            direction.keys = _keys13(suite, secret)

    def _keys12(self, session: _Session, source: Tuple) -> Optional[_Keys]:
        suite = session.suite
        master = self.keylog.get(session.client_random or b'', 'CLIENT_RANDOM')
        if suite is None or master is None or session.server_random is None:
            return None
        iv_length = 4 if suite.cipher == 'aes-gcm' else 12
        block = prf12(
            master,
            b'key expansion',
            session.server_random + session.client_random,
            2 * (suite.key_length + iv_length),
            suite.hash_name,
        )
        client_key, server_key = block[: suite.key_length], block[suite.key_length : 2 * suite.key_length]
        ivs = block[2 * suite.key_length :]
        if source == session.client:
            return _Keys(suite, client_key, ivs[:iv_length], False)
        return _Keys(suite, server_key, ivs[iv_length:], False)

    def _handshake13(self, session: _Session, source: Tuple, direction: _Direction, plain: bytes) -> None:
        """Follow the decrypted TLS 1.3 handshake: after Finished the application keys, after KeyUpdate the next."""
        direction.handshake += plain
        label = 'CLIENT' if source == session.client else 'SERVER'
        for kind, _body in _handshake_messages(direction.handshake):
            if kind == FINISHED and direction.keys is not None:
                secret = self.keylog.get(session.client_random or b'', f'{label}_TRAFFIC_SECRET_0')
                if secret is None:
                    session.error = 'no TLS 1.3 traffic secrets for this session in the key log'
                    direction.keys = None
                    return
                direction.keys = _keys13(session.suite, secret)
            elif kind == KEY_UPDATE and direction.keys is not None:
                suite = session.suite
                secret = hkdf_expand_label(
                    direction.keys.secret, 'traffic upd', b'', _hash(suite.hash_name).digest_size, suite.hash_name
                )
                direction.keys = _keys13(suite, secret)
