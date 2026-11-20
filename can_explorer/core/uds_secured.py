"""SecuredDataTransmission (UDS 0x84 / 0xC4): any diagnostic request and its response carried in a cryptographically
protected envelope — signed, encrypted, or both, with an anti-replay counter.

The envelope (ISO 14229-1:2020), for the request (0x84) and its positive response (0xC4):

| Field                              | Bytes | Notes                                                       |
|------------------------------------|-------|-------------------------------------------------------------|
| Administrative Parameter           | 2     | bit 0 request message, 3 pre-established key, 4 encrypted,  |
|                                    |       | 5 signed, 6 signature on the response requested; the other  |
|                                    |       | bits are reserved (0)                                       |
| Signature/Encryption Calculation   | 1     | the algorithm: 0x00-0x7F vehicle manufacturer, 0x80-0x8F    |
|                                    |       | system supplier specific, 0x90-0xFF reserved                |
| Signature Length                   | 2     | bytes of Signature/MAC at the end (0: not signed)           |
| Anti-replay Counter                | 2     |                                                             |
| Internal Message (Request/Response | 1 + n | the wrapped service ID and its parameters, or their         |
| SID and parameters)                |       | ciphertext when encrypted                                   |
| Signature/MAC                      | Sig.  |                                                             |
|                                    | Length|                                                             |

Checked against two free sources that agree: Wireshark's UDS dissector (packet-uds.c, the flag masks 0x0001, 0x0008,
0x0010, 0x0020, 0x0040 and the field order) and the py-uds knowledge base (the 2020 request and response tables, bit
positions b[0], b[3]..b[6]). Scapy's UDS_SDT layer has the same fields but the administrative bits in the mirrored
order (request message as the most significant bit); CANExplorer follows the two sources that agree. The negative
response codes 0x38 secureDataTransmissionRequired, 0x39 secureDataTransmissionNotAllowed and 0x3A
secureDataVerificationFailed are from udsoncan and Wireshark. The AUTOSAR Dcm (R23-11) does not support the service.
ISO 14229-1:2013 had a single opaque securityDataRequestRecord instead: a message that does not fit the 2020 layout
is labelled and kept raw.

**What the standard leaves open** — and so the provider decides: the algorithms behind the Signature/Encryption
Calculation byte, what the signature covers, how the counter feeds the cryptography, and how keys are agreed. The
cryptography is therefore a pluggable *provider* (`SecuredDataProvider`):

- a Python file (or object) of the user's, implementing the manufacturer's scheme (`load_provider`);
- `MacProvider`, CANExplorer's own *reference scheme* — a documented choice, not from the standard: the signature is
  an HMAC-SHA-256 or AES-CMAC with a shared key over every byte before the signature (SID to internal message, after
  encryption), truncated to the signature length. It has no encryption. It interoperates with the simulated ECU and
  with an ECU built to the same scheme, not with any manufacturer's.

`SecuredTransport` wraps any UdsClient transport so that every request — reads, DTCs, routines, flashing — travels
secured and every response is checked and unwrapped; `secured_request` sends a single one.
"""

import hashlib
import hmac
import importlib.util
import types
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from can_explorer.core.frames import format_hex

SID = 0x84
RESPONSE_SID = SID + 0x40

# Administrative Parameter bits
REQUEST_MESSAGE = 0x0001
PRE_ESTABLISHED_KEY = 0x0008
ENCRYPTED = 0x0010
SIGNED = 0x0020
SIGNATURE_ON_RESPONSE_REQUESTED = 0x0040
_KNOWN_BITS = REQUEST_MESSAGE | PRE_ESTABLISHED_KEY | ENCRYPTED | SIGNED | SIGNATURE_ON_RESPONSE_REQUESTED

HEADER_LENGTH = 8  # SID, Administrative Parameter, Signature/Encryption Calculation, Signature Length, counter

# Negative response codes of the secured data transmission
SECURE_DATA_TRANSMISSION_REQUIRED = 0x38
SECURE_DATA_TRANSMISSION_NOT_ALLOWED = 0x39
SECURE_DATA_VERIFICATION_FAILED = 0x3A
NEGATIVE_RESPONSE_CODES = {
    SECURE_DATA_TRANSMISSION_REQUIRED: 'secureDataTransmissionRequired',
    SECURE_DATA_TRANSMISSION_NOT_ALLOWED: 'secureDataTransmissionNotAllowed',
    SECURE_DATA_VERIFICATION_FAILED: 'secureDataVerificationFailed',
}

MACS = {'hmac-sha256': 32, 'aes-cmac': 16}  # The reference scheme's MACs and their full lengths
COUNTER_MODULO = 0x10000


class SecuredDataError(Exception):
    pass


def algorithm_range(algorithm: int) -> str:
    if algorithm <= 0x7F:
        return 'vehicle manufacturer specific'
    if algorithm <= 0x8F:
        return 'system supplier specific'
    return 'reserved'


@dataclass(frozen=True, slots=True)
class SecuredHeader:
    """The envelope's header fields (everything but the internal message and the signature)."""

    administrative: int
    algorithm: int  # Signature/Encryption Calculation
    signature_length: int
    counter: int  # Anti-replay Counter

    @property
    def request(self) -> bool:
        return bool(self.administrative & REQUEST_MESSAGE)

    @property
    def pre_established_key(self) -> bool:
        return bool(self.administrative & PRE_ESTABLISHED_KEY)

    @property
    def encrypted(self) -> bool:
        return bool(self.administrative & ENCRYPTED)

    @property
    def signed(self) -> bool:
        return bool(self.administrative & SIGNED)

    @property
    def signature_on_response_requested(self) -> bool:
        return bool(self.administrative & SIGNATURE_ON_RESPONSE_REQUESTED)

    def encode(self) -> bytes:
        return (
            bytes([SID if self.request else RESPONSE_SID])
            + self.administrative.to_bytes(2, 'big')
            + bytes([self.algorithm])
            + self.signature_length.to_bytes(2, 'big')
            + self.counter.to_bytes(2, 'big')
        )

    def flags(self) -> list:
        names = []
        if self.signed:
            names.append('signed')
        if self.encrypted:
            names.append('encrypted')
        if self.pre_established_key:
            names.append('pre-established key')
        if self.signature_on_response_requested:
            names.append('signed response requested')
        return names


def administrative_parameter(
    request: bool,
    signed: bool = False,
    encrypted: bool = False,
    signature_on_response: bool = False,
    pre_established_key: bool = False,
) -> int:
    return (
        (REQUEST_MESSAGE if request else 0)
        | (SIGNED if signed else 0)
        | (ENCRYPTED if encrypted else 0)
        | (SIGNATURE_ON_RESPONSE_REQUESTED if signature_on_response else 0)
        | (PRE_ESTABLISHED_KEY if pre_established_key else 0)
    )


@dataclass(frozen=True, slots=True)
class SecuredMessage:
    """A parsed 0x84 request or 0xC4 response. `internal` is the wrapped message as transmitted (ciphertext when
    encrypted); `signed_data` the bytes before the signature."""

    header: SecuredHeader
    internal: bytes
    signature: bytes
    raw: bytes

    @property
    def signed_data(self) -> bytes:
        return self.raw[: len(self.raw) - len(self.signature)]

    def describe(self, describe_internal: Optional[Callable[[bytes], str]] = None) -> str:
        header = self.header
        parts = header.flags() or ['plain']
        if header.signed:
            parts[0] = f'signed ({header.signature_length} bytes)'
        text = f'{", ".join(parts)}, counter {header.counter}, algorithm 0x{header.algorithm:02X}'
        if header.encrypted:
            return f'{text}: {len(self.internal)} encrypted bytes'
        inner = describe_internal(self.internal) if describe_internal else ''
        return f'{text}: {inner or format_hex(self.internal)}'


def parse(payload: bytes) -> SecuredMessage:
    """Parse a 0x84 request or 0xC4 response in the ISO 14229-1:2020 layout. Raises SecuredDataError when it does not
    fit (too short, reserved bits set, a request flag that contradicts the service ID, a signature longer than the
    message): an ISO 14229-1:2013 securityDataRecord, or a malformed message."""
    payload = bytes(payload)
    if not payload or payload[0] not in (SID, RESPONSE_SID):
        raise SecuredDataError('Not a SecuredDataTransmission message')
    if len(payload) < HEADER_LENGTH + 1:
        raise SecuredDataError('Too short for the ISO 14229-1:2020 SecuredDataTransmission layout')
    administrative = int.from_bytes(payload[1:3], 'big')
    if administrative & ~_KNOWN_BITS:
        raise SecuredDataError(f'Reserved administrative parameter bits set (0x{administrative:04X})')
    if bool(administrative & REQUEST_MESSAGE) != (payload[0] == SID):
        raise SecuredDataError('The request message bit does not match the service ID')
    signature_length = int.from_bytes(payload[4:6], 'big')
    if HEADER_LENGTH + 1 + signature_length > len(payload):
        raise SecuredDataError(f'Signature length {signature_length} exceeds the message')
    if bool(administrative & SIGNED) != (signature_length > 0):
        raise SecuredDataError('The signed bit does not match the signature length')
    header = SecuredHeader(administrative, payload[3], signature_length, int.from_bytes(payload[6:8], 'big'))
    end = len(payload) - signature_length
    return SecuredMessage(header, payload[HEADER_LENGTH:end], payload[end:], payload)


def describe(payload: bytes) -> str:
    """A passive description of a 0x84 request or 0xC4 response; unencrypted internal messages are described as the
    service they carry."""
    from can_explorer.core.uds import describe_payload

    response = payload[:1] == bytes([RESPONSE_SID])
    prefix = 'Positive response to SecuredDataTransmission' if response else 'SecuredDataTransmission'
    try:
        message = parse(payload)
    except SecuredDataError:
        rest = f': {format_hex(payload[1:])}' if len(payload) > 1 else ''
        record = 'securityDataResponseRecord' if response else 'securityDataRequestRecord'
        return f'{prefix}{rest} ({record}, not decoded: ISO 14229-1:2013 or malformed)'
    return f'{prefix}: {message.describe(describe_payload)}'


# Providers


class SecuredDataProvider(Protocol):
    """The cryptography of a secured data transmission. Every method gets the message's header (algorithm, counter,
    flags); a provider without encryption may leave out encrypt/decrypt, one without signatures sign/verify.

    - `signature_length(header) -> int`: the length of the signature `sign` makes (it is in the signed header);
    - `sign(header, data) -> bytes`: the signature over `data`, the message bytes before the signature;
    - `verify(header, data, signature) -> bool`;
    - `encrypt(header, plaintext) -> bytes` / `decrypt(header, ciphertext) -> bytes`: the internal message.
    """

    def signature_length(self, header: SecuredHeader) -> int: ...

    def sign(self, header: SecuredHeader, data: bytes) -> bytes: ...


class MacProvider:
    """CANExplorer's reference scheme (a documented choice, not from the standard): an HMAC-SHA-256 or AES-CMAC with
    a shared key over the message bytes before the signature, truncated to `length` bytes (default: the full MAC).
    No encryption."""

    def __init__(self, key: bytes, mac: str = 'hmac-sha256', length: Optional[int] = None):
        if mac not in MACS:
            raise SecuredDataError(f'Unknown MAC {mac!r}; one of {", ".join(MACS)}')
        if not key:
            raise SecuredDataError('The key is empty')
        if mac == 'aes-cmac' and len(key) not in (16, 24, 32):
            raise SecuredDataError(f'An AES-CMAC key has 16, 24 or 32 bytes, not {len(key)}')
        length = MACS[mac] if length is None else length
        if not 1 <= length <= MACS[mac]:
            raise SecuredDataError(f'The {mac} signature has 1 to {MACS[mac]} bytes, not {length}')
        self.key, self.mac, self.length = bytes(key), mac, length

    def __repr__(self) -> str:  # Never show the key
        return f'MacProvider({self.mac}, {self.length} bytes)'

    def _mac(self, data: bytes) -> bytes:
        if self.mac == 'aes-cmac':
            from can_explorer.core.secoc import cmac_aes

            return cmac_aes(self.key, data)
        return hmac.new(self.key, data, hashlib.sha256).digest()

    def signature_length(self, header: SecuredHeader) -> int:
        return self.length

    def sign(self, header: SecuredHeader, data: bytes) -> bytes:
        return self._mac(bytes(data))[: self.length]

    def verify(self, header: SecuredHeader, data: bytes, signature: bytes) -> bool:
        return len(signature) == self.length and hmac.compare_digest(self.sign(header, data), bytes(signature))


class _ModuleProvider:
    """A provider defined by the functions of a Python module (file or key store script)."""

    def __init__(self, module: types.ModuleType, name: str):
        self._module, self.name = module, name
        if not any(callable(getattr(module, f, None)) for f in ('sign', 'encrypt')):
            raise SecuredDataError(f'{name} defines neither sign(header, data) nor encrypt(header, plaintext)')
        if callable(getattr(module, 'sign', None)) and not callable(getattr(module, 'signature_length', None)):
            raise SecuredDataError(f'{name} defines sign() but not signature_length(header)')

    def __repr__(self) -> str:
        return f'provider {self.name}'

    def __getattr__(self, item: str) -> Any:
        if item in ('signature_length', 'sign', 'verify', 'encrypt', 'decrypt'):
            function = getattr(self._module, item, None)
            if callable(function):
                return function
        raise AttributeError(item)


def load_provider_file(path: str | Path) -> _ModuleProvider:
    """A provider from a Python file defining (some of) signature_length, sign, verify, encrypt and decrypt — see
    SecuredDataProvider. The file is executed."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f'can_explorer_sdt_{path.stem}', path)
    if spec is None or spec.loader is None:
        raise SecuredDataError(f'Cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise SecuredDataError(f'Error in provider file {path.name}: {e}') from e
    return _ModuleProvider(module, path.name)


KEYSTORE_PREFIX = 'keystore:'


def load_provider(source: str, store=None, length: Optional[int] = None):
    """The provider a source names: 'keystore:<entry>' for a secured data key of the key store (`store`, by default
    the application's; unlocked) using the reference scheme, otherwise a Python provider file. `length` truncates the
    reference scheme's MAC. '' gives None (unsigned, unencrypted)."""
    if not source:
        return None
    if not source.startswith(KEYSTORE_PREFIX):
        return load_provider_file(source)
    from can_explorer.core import audit, keystore

    name = source[len(KEYSTORE_PREFIX) :]
    store = store if store is not None else keystore.default_store()
    try:
        info = next((entry for entry in store.entries(keystore.SECURED_DATA_KEY) if entry.name == name), None)
        if info is None:
            raise SecuredDataError(f'{name!r} is not a secured data key of the key store')
        key = store.secrets(name)['key']
    except (keystore.KeyStoreError, KeyError) as e:
        raise SecuredDataError(f'Secured data key {name!r}: {e}') from e
    audit.record('secured data key used', entry=name)
    return MacProvider(key, info.params.get('mac', 'hmac-sha256'), length)


def _method(provider, name: str, what: str):
    function = getattr(provider, name, None) if provider is not None else None
    if not callable(function):
        raise SecuredDataError(f'{what} needs a provider with {name}()')
    return function


# Building and opening envelopes


def wrap(
    internal: bytes,
    counter: int,
    request: bool = True,
    provider=None,
    sign: bool = False,
    encrypt: bool = False,
    signature_on_response: bool = False,
    algorithm: int = 0x00,
    pre_established_key: bool = False,
) -> bytes:
    """A 0x84 request (or, with `request` False, a 0xC4 response) carrying `internal` (a service ID and its
    parameters). Encrypts the internal message, then signs every byte before the signature."""
    internal = bytes(internal)
    if not internal:
        raise SecuredDataError('The internal message is empty')
    if not 0 <= algorithm <= 0xFF:
        raise SecuredDataError('The Signature/Encryption Calculation is one byte')
    administrative = administrative_parameter(request, sign, encrypt, signature_on_response, pre_established_key)
    header = SecuredHeader(administrative, algorithm, 0, counter % COUNTER_MODULO)
    if sign:
        length = int(_method(provider, 'signature_length', 'Signing')(header))
        if not 0 < length <= 0xFFFF:
            raise SecuredDataError(f'Invalid signature length {length}')
        header = replace(header, signature_length=length)
    if encrypt:
        internal = bytes(_method(provider, 'encrypt', 'Encryption')(header, internal))
        if not internal:
            raise SecuredDataError('The encrypted internal message is empty')
    data = header.encode() + internal
    if not sign:
        return data
    signature = bytes(_method(provider, 'sign', 'Signing')(header, data))
    if len(signature) != header.signature_length:
        raise SecuredDataError(f'The provider made a {len(signature)}-byte signature, not {header.signature_length}')
    return data + signature


@dataclass(frozen=True, slots=True)
class Opened:
    """A received envelope, checked and decrypted. `verified` is None when it was not signed."""

    message: SecuredMessage
    internal: bytes  # Plaintext
    verified: Optional[bool]

    def describe(self) -> str:
        from can_explorer.core.uds import describe_payload

        header = self.message.header
        parts = [f'counter {header.counter}']
        if self.verified is not None:
            parts.append('signature verified' if self.verified else 'signature INVALID')
        if header.encrypted:
            parts.append('decrypted')
        return f'{describe_payload(self.internal)} (secured: {", ".join(parts)})'


def open_message(payload: bytes, provider=None) -> Opened:
    """Parse an envelope, verify its signature and decrypt its internal message. A bad signature raises
    SecuredDataError; so does a signed or encrypted message without a provider that can check it."""
    message = parse(payload)
    header = message.header
    verified: Optional[bool] = None
    if header.signed:
        verify = getattr(provider, 'verify', None) if provider is not None else None
        if callable(verify):
            verified = bool(verify(header, message.signed_data, message.signature))
        else:
            sign = _method(provider, 'sign', 'Verifying a signature')
            verified = hmac.compare_digest(bytes(sign(header, message.signed_data)), message.signature)
        if not verified:
            raise SecuredDataError('Signature verification failed')
    internal = message.internal
    if header.encrypted:
        internal = bytes(_method(provider, 'decrypt', 'Decryption')(header, internal))
    return Opened(message, internal, verified)


@dataclass(frozen=True, slots=True)
class SecuredOptions:
    """How SecuredTransport protects requests."""

    sign: bool = True
    encrypt: bool = False
    signature_on_response: bool = False  # Ask for signed responses, and refuse unsigned ones
    algorithm: int = 0x00
    pre_established_key: bool = False


@dataclass(frozen=True, slots=True)
class Exchange:
    """The last request sent and response received through a SecuredTransport."""

    request: bytes  # The 0x84 envelope
    response: Optional[bytes] = None  # As received (0xC4 envelope, or a negative response)
    opened: Optional[Opened] = None

    def describe(self) -> str:
        if self.opened is None:
            return f'secured, counter {int.from_bytes(self.request[6:8], "big")}'
        parts = [f'counter {self.opened.message.header.counter}']
        if self.opened.verified:
            parts.append('response signature verified')
        if self.opened.message.header.encrypted:
            parts.append('decrypted')
        return f'secured: {", ".join(parts)}'


class SecuredTransport:
    """A UdsTransport that sends every request inside a SecuredDataTransmission envelope and unwraps the responses,
    so any UdsClient service runs secured: `UdsClient(SecuredTransport(transport, provider))`.

    The anti-replay counter starts at `counter` and increases by one per request (modulo 65536); `next_counter`
    is the one the next request uses. A negative response to the envelope itself (0x7F 0x84 code, e.g.
    secureDataVerificationFailed) is handed on as a negative response to the wrapped service, so the client reports it
    for the request it sent; `last` keeps the raw exchange. A response that fails verification raises
    SecuredDataError (the client reports it as an error). Messages that are not envelopes are passed through as they
    are."""

    def __init__(self, transport, provider=None, options: SecuredOptions = SecuredOptions(), counter: int = 0):
        if (options.sign or options.encrypt or options.signature_on_response) and provider is None:
            raise SecuredDataError('Signing, encryption and signed responses need a key or provider')
        self.transport = transport
        self.provider = provider
        self.options = options
        self.next_counter = counter % COUNTER_MODULO
        self.last: Optional[Exchange] = None
        self._service: Optional[int] = None

    def __getattr__(self, item: str) -> Any:  # close() and the rest of the wrapped transport
        return getattr(self.transport, item)

    async def send(self, payload: bytes) -> None:
        payload = bytes(payload)
        options = self.options
        envelope = wrap(
            payload,
            self.next_counter,
            True,
            self.provider,
            options.sign,
            options.encrypt,
            options.signature_on_response,
            options.algorithm,
            options.pre_established_key,
        )
        self.next_counter = (self.next_counter + 1) % COUNTER_MODULO
        self._service = payload[0] if payload else None
        self.last = Exchange(envelope)
        await self.transport.send(envelope)

    async def receive(self, timeout: Optional[float] = None) -> bytes:
        response = bytes(await self.transport.receive(timeout))
        request = self.last.request if self.last else b''
        if len(response) >= 3 and response[0] == 0x7F and response[1] == SID:
            self.last = Exchange(request, response)
            service = self._service if self._service is not None else SID
            return bytes([0x7F, service]) + response[2:]
        if response[:1] != bytes([RESPONSE_SID]):
            return response
        opened = open_message(response, self.provider)
        if self.options.signature_on_response and not opened.message.header.signed:
            raise SecuredDataError('The ECU sent an unsigned response although a signed one was requested')
        self.last = Exchange(request, response, opened)
        return opened.internal


@dataclass(frozen=True, slots=True)
class SecuredResult:
    request: bytes  # The internal (plaintext) request
    envelope: bytes  # The 0x84 request as sent
    response: Optional[bytes]  # The internal response (plaintext)
    exchange: Optional[Exchange]
    error: Optional[str] = None

    def as_dict(self) -> dict:
        exchange = self.exchange
        opened = exchange.opened if exchange else None
        return {
            'request': format_hex(self.request),
            'secured_request': format_hex(self.envelope),
            'response': format_hex(self.response) if self.response else None,
            'secured_response': format_hex(exchange.response) if exchange and exchange.response else None,
            'counter': int.from_bytes(self.envelope[6:8], 'big'),
            'response_counter': opened.message.header.counter if opened else None,
            'response_signature_verified': opened.verified if opened else None,
            'error': self.error,
        }


async def secured_request(transport, internal: bytes, provider=None, options=SecuredOptions(), counter: int = 0):
    """Send one request secured over `transport` and return the unwrapped result (never raises for the ECU's
    answer: errors are in the result)."""
    from can_explorer.core.uds import UdsClient

    secured = SecuredTransport(transport, provider, options, counter)
    result = await UdsClient(secured).request(bytes(internal))
    envelope = secured.last.request if secured.last else b''
    return SecuredResult(bytes(internal), envelope, result.response, secured.last, result.error)


# Session keys from Authentication (0x29)
#
# Authentication can establish a session key: the tester asks for it with the communicationConfiguration byte of
# verifyCertificate(Uni|Bi)directional or requestChallengeForAuthentication, and the ECU answers the final step with
# sessionKeyInfo ("session key information, e.g. encrypted session keys"). Envelopes protected with that key set the
# pre-established key bit. Both the meaning of the communicationConfiguration values and the contents of
# sessionKeyInfo are vehicle manufacturer specific (py-uds knowledge base); the AUTOSAR Dcm only supports 0 (no
# session key, SWS_Dcm_01459/01513). So the key is obtained by a `session_provider(context)` hook — a security
# profile's, or a script's — or by CANExplorer's reference derivation (a documented choice, not from the standard).


@dataclass(frozen=True, slots=True)
class SessionContext:
    """What an authentication leaves for deriving or unwrapping its session key."""

    method: str  # 'certificate' or 'challenge-response'
    communication_configuration: int
    session_key_info: bytes
    server_challenge: bytes = b''
    client_challenge: bytes = b''
    algorithm_indicator: bytes = b''
    client_certificate: Any = None
    server_certificate: Any = None
    server_ephemeral_public_key: bytes = b''
    client_ephemeral_public_key: bytes = b''

    @classmethod
    def from_result(cls, result) -> 'SessionContext':
        """From a core/uds_auth.AuthenticationResult."""
        return cls(
            result.method,
            result.communication_configuration,
            bytes(result.session_key_info),
            bytes(result.server_challenge),
            bytes(result.client_challenge),
            bytes(result.algorithm_indicator),
            result.client_certificate,
            result.server_certificate,
            bytes(result.server_ephemeral_public_key),
            bytes(result.client_ephemeral_public_key),
        )

    def describe(self) -> str:
        return (
            f'session key info {len(self.session_key_info)} bytes '
            f'(communicationConfiguration 0x{self.communication_configuration:02X}, {self.method})'
        )


REFERENCE_SESSION_LABEL = b'CANExplorer UDS session key'
SESSION_KEY_INFO_LENGTH = 16  # The reference scheme's sessionKeyInfo: a random nonce of the ECU


def reference_session_key(shared_key: bytes, context: SessionContext, mac: str = 'hmac-sha256') -> bytes:
    """CANExplorer's reference derivation (a documented choice, not from the standard): HKDF-SHA-256 (RFC 5869) of the
    key shared with the ECU, salt the server challenge followed by the client challenge (empty when unidirectional),
    info the label 'CANExplorer UDS session key', the communicationConfiguration byte and the sessionKeyInfo (the ECU's
    nonce). 32 bytes for HMAC-SHA-256, 16 for AES-CMAC."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if mac not in MACS:
        raise SecuredDataError(f'Unknown MAC {mac!r}; one of {", ".join(MACS)}')
    if not context.session_key_info:
        raise SecuredDataError(
            'The ECU sent no session key information: ask for a session key with the communicationConfiguration '
            'its manufacturer defines for it'
        )
    info = REFERENCE_SESSION_LABEL + bytes([context.communication_configuration]) + context.session_key_info
    hkdf = HKDF(
        hashes.SHA256(),
        length=32 if mac == 'hmac-sha256' else 16,
        salt=context.server_challenge + context.client_challenge,
        info=info,
    )
    return hkdf.derive(bytes(shared_key))


def reference_session_provider(
    shared_key: bytes, context: SessionContext, mac: str = 'hmac-sha256', length: Optional[int] = None
) -> MacProvider:
    """A MacProvider (reference scheme) with the session key of `reference_session_key`."""
    return MacProvider(reference_session_key(shared_key, context, mac), mac, length)


def _checked_provider(provider, origin: str):
    if provider is None:
        raise SecuredDataError(f'{origin} has no session key for this authentication')
    if not any(callable(getattr(provider, name, None)) for name in ('sign', 'encrypt')):
        raise SecuredDataError(f'{origin} returned no provider (an object with sign() or encrypt())')
    return provider


def session_provider(context: SessionContext, source, store=None, length: Optional[int] = None):
    """The 0x84 provider for an authentication's session key, from `source`:
    - a function `session_provider(context) -> provider` (a script's hook);
    - 'profile:<name>': the security profile's `session_provider(context)` (e.g. unwrapping sessionKeyInfo with the
      tester's private key, or a key agreement with the ephemeral public keys);
    - the name of a UDS authentication key of the key store (`store`, by default the application's; unlocked):
      the reference derivation with that shared key and its MAC, truncated to `length`."""
    from can_explorer.core import audit

    if callable(source):
        return _checked_provider(source(context), 'The session key hook')
    source = str(source or '')
    from can_explorer.core import keystore, security_profiles

    if source.startswith(security_profiles.PREFIX):
        name = source[len(security_profiles.PREFIX) :]
        try:
            profile = security_profiles.registry().get(name)
            if not callable(getattr(profile, 'session_provider', None)):
                raise SecuredDataError(f'The security profile {name!r} has no session_provider(context)')
            provider = profile.session_provider(context)
        except security_profiles.SecurityProfileError as e:
            raise SecuredDataError(str(e)) from e
        audit.record('session key used', profile=name)
        return _checked_provider(provider, f'The security profile {name!r}')
    store = store if store is not None else keystore.default_store()
    info = next((entry for entry in store.entries(keystore.AUTH_KEY) if entry.name == source), None)
    if info is None:
        raise SecuredDataError(
            'Session keys need the shared authentication key (reference derivation) or a security profile with '
            f'session_provider(context); {source or "this authentication"!r} is neither'
        )
    try:
        shared_key = store.secrets(source)['key']
    except (keystore.KeyStoreError, KeyError) as e:
        raise SecuredDataError(f'Authentication key {source!r}: {e}') from e
    audit.record('session key used', entry=source)
    return reference_session_provider(shared_key, context, info.params.get('mac', 'hmac-sha256'), length)


@dataclass
class SessionKeyIssuer:
    """The session keys of a simulated ECU (core/uds_auth.AuthenticationServer `session_keys=`): it accepts the
    communicationConfiguration values in `configurations` (its own choice: 0x01 by default; 0 stays "no session
    key") and, on a successful authentication, issues a session key — with `issue(context) -> (session key info,
    provider)` if given, otherwise with the reference derivation from `shared_key` (a random 16-byte nonce as the
    session key info)."""

    shared_key: bytes = b''
    mac: str = 'hmac-sha256'
    configurations: tuple = (0x01,)
    issue: Optional[Callable[[SessionContext], tuple]] = None
    random: Optional[Callable[[int], bytes]] = None

    def create(self, context: SessionContext):
        if self.issue is not None:
            info, provider = self.issue(context)
            return bytes(info), provider
        import os

        info = (self.random or os.urandom)(SESSION_KEY_INFO_LENGTH)
        context = replace(context, session_key_info=info)
        return info, reference_session_provider(self.shared_key, context, self.mac)


# Simulated ECU


@dataclass
class SecuredServerConfig:
    """The simulated ECU's secured data transmission (core/simulation.py UdsServer `secured=`). Its own choices,
    documented, not from the standard: the response carries the request's counter; a request whose counter is not
    newer than the last accepted one (modulo 65536, at most 32767 ahead) is a replay and answered
    secureDataVerificationFailed, as are bad signatures and, with `require_signature`, unsigned requests;
    `secured_dids` can only be read or written inside an envelope (secureDataTransmissionRequired otherwise).
    Envelopes with the pre-established key bit use the session key of the last authentication (its
    SessionKeyIssuer); without one they are answered secureDataVerificationFailed. With `require_session_key`,
    envelopes without that bit are refused the same way."""

    provider: Any = None
    require_signature: bool = True
    secured_dids: tuple = ()
    last_counter: Optional[int] = None
    require_session_key: bool = False

    def fresh(self, counter: int) -> bool:
        if self.last_counter is None:
            return True
        return 0 < (counter - self.last_counter) % COUNTER_MODULO < COUNTER_MODULO // 2
