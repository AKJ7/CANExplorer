"""UDS Authentication (service 0x29, ISO 14229-1:2020): authentication by certificate exchange (PKI).

ISO 14229-1 is not freely available, so nothing here is taken from it directly. The message layouts, the
sub-function numbers, the authenticationReturnParameter values and the authentication NRCs (0x50..0x5D) follow
the open-source python-udsoncan implementation of the service (MIT licence); the certificate exchange itself —
which sub-functions, which parameters are used, and what the proof of ownership is computed over — follows the
AUTOSAR Diagnostic Communication Manager specification (AUTOSAR_CP_SWS_DiagnosticCommunicationManager R23-11,
§7.6.2.10), which is public. Both sources agree wherever they overlap (the parameter order of verifyCertificate
uni-/bidirectional and proofOfOwnership, NRC 0x59 challengeCalculationFailed, NRC 0x34 authenticationRequired).

The certificate exchange (as in the AUTOSAR Dcm):

1. verifyCertificateUnidirectional (0x01): the client sends its certificate; the server verifies it and answers
   with a random challenge (return parameter 0x11, "certificate verified, ownership verification necessary").
   verifyCertificateBidirectional (0x02) also carries a client challenge; the server answers with its own
   certificate and its proof of ownership: its signature over the client challenge.
2. proofOfOwnership (0x03): the client sends its signature over the server challenge, made with the private key of
   its certificate; the server verifies it with the certificate's public key and answers 0x12 ("ownership
   verified, authentication complete"). The session is then authenticated with the roles of the certificate.
3. deAuthenticate (0x00) ends it.

Authentication by challenge-response (ACR, without certificates):

1. requestChallengeForAuthentication (0x05): the client names an algorithm (algorithmIndicator); the server
   answers with the algorithm it uses, a challenge and the additional parameter it needs, if any.
2. verifyProofOfOwnershipUnidirectional (0x06): the client sends its proof over the challenge; the server answers
   0x12 when it verifies. verifyProofOfOwnershipBidirectional (0x07) also carries a client challenge, and the
   server answers with its own proof over it.

The message layouts of 0x05..0x07 follow python-udsoncan. What the proof is computed over is left to the vehicle
manufacturer (udsoncan: "vehicle manufacturer specific formatted server data"), and the AUTOSAR Dcm does not
implement ACR, so the built-in provers are a choice, named as such: the proof over the peer's challenge, as in the
certificate exchange — a MAC with a key shared with the ECU (`PROOF_MACS`, symmetric) or a signature with a private
key (`SIGNATURE_SCHEMES`, asymmetric). Any other construction comes from a security profile (`prove_challenge`).

What stays manufacturer-specific — and is therefore a choice here, not a guess: the certificate encoding on the
wire (X.509 DER by default), the signature scheme of the proof (`SIGNATURE_SCHEMES`; the AUTOSAR Dcm uses
whatever its Csm signature job is configured with), the communicationConfiguration value (0 in the AUTOSAR Dcm:
no session key), what an ACR proof covers (above), and the encoding of an algorithmIndicator given as a dotted OID
(`algorithm_indicator`: the DER encoding with its tag and length; give the 16 bytes in hex when an ECU expects
something else).
"""

import hashlib
import hmac
import inspect
import os
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Sequence, Tuple

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

SID = 0x29

# Sub-functions (authentication tasks)
DE_AUTHENTICATE = 0x00
VERIFY_CERTIFICATE_UNIDIRECTIONAL = 0x01
VERIFY_CERTIFICATE_BIDIRECTIONAL = 0x02
PROOF_OF_OWNERSHIP = 0x03
TRANSMIT_CERTIFICATE = 0x04
REQUEST_CHALLENGE_FOR_AUTHENTICATION = 0x05
VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL = 0x06
VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL = 0x07
AUTHENTICATION_CONFIGURATION = 0x08

TASKS = {
    DE_AUTHENTICATE: 'deAuthenticate',
    VERIFY_CERTIFICATE_UNIDIRECTIONAL: 'verifyCertificateUnidirectional',
    VERIFY_CERTIFICATE_BIDIRECTIONAL: 'verifyCertificateBidirectional',
    PROOF_OF_OWNERSHIP: 'proofOfOwnership',
    TRANSMIT_CERTIFICATE: 'transmitCertificate',
    REQUEST_CHALLENGE_FOR_AUTHENTICATION: 'requestChallengeForAuthentication',
    VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL: 'verifyProofOfOwnershipUnidirectional',
    VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL: 'verifyProofOfOwnershipBidirectional',
    AUTHENTICATION_CONFIGURATION: 'authenticationConfiguration',
}

# authenticationReturnParameter
RETURN_PARAMETERS = {
    0x00: 'request accepted',
    0x01: 'general reject',
    0x02: 'authentication configuration APCE (certificate exchange)',
    0x03: 'authentication configuration ACR with asymmetric cryptography',
    0x04: 'authentication configuration ACR with symmetric cryptography',
    0x10: 'de-authentication successful',
    0x11: 'certificate verified, ownership verification necessary',
    0x12: 'ownership verified, authentication complete',
    0x13: 'certificate verified',
}
REQUEST_ACCEPTED, GENERAL_REJECT = 0x00, 0x01
APCE, ACR_ASYMMETRIC, ACR_SYMMETRIC = 0x02, 0x03, 0x04  # authenticationConfiguration answers
DEAUTHENTICATED = 0x10
OWNERSHIP_VERIFICATION_NECESSARY = 0x11
AUTHENTICATION_COMPLETE = 0x12

# Negative response codes of the service (and authenticationRequired for services needing it)
NEGATIVE_RESPONSE_CODES = {
    0x34: 'authenticationRequired',
    0x50: 'certificateVerificationFailed-InvalidTimePeriod',
    0x51: 'certificateVerificationFailed-InvalidSignature',
    0x52: 'certificateVerificationFailed-InvalidChainOfTrust',
    0x53: 'certificateVerificationFailed-InvalidType',
    0x54: 'certificateVerificationFailed-InvalidFormat',
    0x55: 'certificateVerificationFailed-InvalidContent',
    0x56: 'certificateVerificationFailed-InvalidScope',
    0x57: 'certificateVerificationFailed-InvalidCertificate',
    0x58: 'ownershipVerificationFailed',
    0x59: 'challengeCalculationFailed',
    0x5A: 'settingAccessRightsFailed',
    0x5B: 'sessionKeyCreation/DerivationFailed',
    0x5C: 'configurationDataUsageFailed',
    0x5D: 'deAuthenticationFailed',
}
INVALID_TIME_PERIOD, INVALID_SIGNATURE, INVALID_CHAIN_OF_TRUST, INVALID_FORMAT = 0x50, 0x51, 0x52, 0x54
INVALID_CERTIFICATE = 0x57  # Also for a revoked certificate (as the AUTOSAR Dcm answers a revoked one)
OWNERSHIP_VERIFICATION_FAILED = 0x58


class UdsAuthenticationError(ValueError):
    pass


# --- Messages ---


def _length_prefixed(value: Optional[bytes]) -> bytes:
    value = bytes(value or b'')
    if len(value) > 0xFFFF:
        raise UdsAuthenticationError(f'A parameter of {len(value)} bytes does not fit the 16-bit length')
    return struct.pack('>H', len(value)) + value


def _algorithm_indicator(value: Optional[bytes]) -> bytes:
    value = bytes(value or b'')
    if len(value) != 16:
        raise UdsAuthenticationError('The algorithm indicator is 16 bytes (the OID, left aligned, zero padded)')
    return value


def request(
    task: int,
    communication_configuration: int = 0,
    certificate: bytes = b'',
    challenge: bytes = b'',
    proof_of_ownership: bytes = b'',
    ephemeral_public_key: bytes = b'',
    algorithm_indicator: Optional[bytes] = None,
    certificate_evaluation_id: int = 0,
    additional_parameter: bytes = b'',
) -> bytes:
    """An Authentication request for a sub-function, with the parameters it carries."""
    if task not in TASKS:
        raise UdsAuthenticationError(f'Unknown authentication task 0x{task:02X}')
    data = bytes([SID, task])
    if task in (VERIFY_CERTIFICATE_UNIDIRECTIONAL, VERIFY_CERTIFICATE_BIDIRECTIONAL):
        return data + bytes([communication_configuration]) + _length_prefixed(certificate) + _length_prefixed(challenge)
    if task == REQUEST_CHALLENGE_FOR_AUTHENTICATION:
        return data + bytes([communication_configuration]) + _algorithm_indicator(algorithm_indicator)
    if task == PROOF_OF_OWNERSHIP:
        return data + _length_prefixed(proof_of_ownership) + _length_prefixed(ephemeral_public_key)
    if task == TRANSMIT_CERTIFICATE:
        return data + struct.pack('>H', certificate_evaluation_id) + _length_prefixed(certificate)
    if task in (VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL, VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL):
        return (
            data
            + _algorithm_indicator(algorithm_indicator)
            + _length_prefixed(proof_of_ownership)
            + _length_prefixed(challenge)
            + _length_prefixed(additional_parameter)
        )
    return data  # deAuthenticate, authenticationConfiguration


@dataclass
class AuthenticationResponse:
    """A positive Authentication response, decoded."""

    task: int
    return_parameter: int
    challenge: bytes = b''
    ephemeral_public_key: bytes = b''
    certificate: bytes = b''
    proof_of_ownership: bytes = b''
    session_key_info: bytes = b''
    algorithm_indicator: bytes = b''
    needed_additional_parameter: bytes = b''

    @property
    def return_text(self) -> str:
        return RETURN_PARAMETERS.get(self.return_parameter, f'return parameter 0x{self.return_parameter:02X}')


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def take(self, count: int, what: str) -> bytes:
        if self.position + count > len(self.data):
            raise UdsAuthenticationError(f'The response ends before its {what}')
        value = self.data[self.position : self.position + count]
        self.position += count
        return bytes(value)

    def prefixed(self, what: str) -> bytes:
        return self.take(int.from_bytes(self.take(2, f'length of {what}'), 'big'), what)


def parse_response(response: bytes) -> AuthenticationResponse:
    """Decode a positive response (0x69, task, authenticationReturnParameter, parameters)."""
    if len(response) < 3 or response[0] != SID + 0x40:
        raise UdsAuthenticationError('Not a positive Authentication response')
    task, reader = response[1], _Reader(bytes(response[3:]))
    decoded = AuthenticationResponse(task, response[2])
    if task == VERIFY_CERTIFICATE_UNIDIRECTIONAL:
        decoded.challenge = reader.prefixed('server challenge')
        decoded.ephemeral_public_key = reader.prefixed('server ephemeral public key')
    elif task == VERIFY_CERTIFICATE_BIDIRECTIONAL:
        decoded.challenge = reader.prefixed('server challenge')
        decoded.certificate = reader.prefixed('server certificate')
        decoded.proof_of_ownership = reader.prefixed('server proof of ownership')
        decoded.ephemeral_public_key = reader.prefixed('server ephemeral public key')
    elif task == PROOF_OF_OWNERSHIP:
        decoded.session_key_info = reader.prefixed('session key info')
    elif task in (
        REQUEST_CHALLENGE_FOR_AUTHENTICATION,
        VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL,
        VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL,
    ):
        decoded.algorithm_indicator = reader.take(16, 'algorithm indicator')
        if task == REQUEST_CHALLENGE_FOR_AUTHENTICATION:
            decoded.challenge = reader.prefixed('server challenge')
            decoded.needed_additional_parameter = reader.prefixed('needed additional parameter')
        else:
            if task == VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL:
                decoded.proof_of_ownership = reader.prefixed('server proof of ownership')
            decoded.session_key_info = reader.prefixed('session key info')
    elif task not in TASKS:
        raise UdsAuthenticationError(f'Unknown authentication task 0x{task:02X}')
    if reader.position != len(reader.data):
        raise UdsAuthenticationError(
            f'{len(reader.data) - reader.position} unexpected bytes at the end of the response'
        )
    return decoded


def describe(payload: bytes) -> str:
    """A short description of an Authentication request or positive response, for traces and logs."""
    if len(payload) < 2:
        return ''
    task = TASKS.get(payload[1], f'task 0x{payload[1]:02X}')
    if payload[0] == SID + 0x40 and len(payload) >= 3:
        text = f'Authentication {task}: {RETURN_PARAMETERS.get(payload[2], f"0x{payload[2]:02X}")}'
        try:
            response = parse_response(payload)
        except UdsAuthenticationError:
            return text
        sizes = [
            (name, len(value))
            for name, value in (
                ('challenge', response.challenge),
                ('certificate', response.certificate),
                ('proof', response.proof_of_ownership),
                ('session key info', response.session_key_info),
            )
            if value
        ]
        return text + ''.join(f', {name} {size} bytes' for name, size in sizes)
    if payload[0] != SID:
        return ''
    text = f'Authentication {task}'
    try:
        if payload[1] in (VERIFY_CERTIFICATE_UNIDIRECTIONAL, VERIFY_CERTIFICATE_BIDIRECTIONAL):
            reader = _Reader(bytes(payload[3:]))
            certificate, challenge = reader.prefixed('certificate'), reader.prefixed('challenge')
            text += f', certificate {len(certificate)} bytes' + (
                f', challenge {len(challenge)} bytes' if challenge else ''
            )
        elif payload[1] == PROOF_OF_OWNERSHIP:
            text += f', proof {len(_Reader(bytes(payload[2:])).prefixed("proof"))} bytes'
        elif payload[1] in (VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL, VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL):
            reader = _Reader(bytes(payload[2:]))
            reader.take(16, 'algorithm indicator')
            text += f', proof {len(reader.prefixed("proof"))} bytes'
    except UdsAuthenticationError:
        pass
    return text


# --- Proof of ownership: signatures ---

SIGNATURE_SCHEMES = {
    'ecdsa-sha256': 'ECDSA with SHA-256, DER-encoded signature',
    'ecdsa-sha256-raw': 'ECDSA with SHA-256, raw r ‖ s signature',
    'ed25519': 'Ed25519',
    'rsa-pss-sha256': 'RSASSA-PSS with SHA-256',
    'rsa-pkcs1-sha256': 'RSASSA-PKCS1-v1_5 with SHA-256',
}


def check_scheme(scheme: str, key) -> None:
    if scheme not in SIGNATURE_SCHEMES:
        raise UdsAuthenticationError(f'Unknown signature scheme {scheme!r}; one of {", ".join(SIGNATURE_SCHEMES)}')
    kind = {
        'ecdsa-sha256': (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey),
        'ecdsa-sha256-raw': (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey),
        'ed25519': (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey),
        'rsa-pss-sha256': (rsa.RSAPrivateKey, rsa.RSAPublicKey),
        'rsa-pkcs1-sha256': (rsa.RSAPrivateKey, rsa.RSAPublicKey),
    }[scheme]
    if not isinstance(key, kind):
        raise UdsAuthenticationError(f'The certificate key does not fit the signature scheme {scheme}')


def sign(private_key, data: bytes, scheme: str) -> bytes:
    """A proof of ownership: the signature over `data` (the peer's challenge)."""
    check_scheme(scheme, private_key)
    if scheme == 'ed25519':
        return private_key.sign(data)
    if scheme.startswith('ecdsa'):
        signature = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        if scheme == 'ecdsa-sha256-raw':
            r, s = decode_dss_signature(signature)
            size = (private_key.curve.key_size + 7) // 8
            return r.to_bytes(size, 'big') + s.to_bytes(size, 'big')
        return signature
    if scheme == 'rsa-pss-sha256':
        return private_key.sign(data, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def verify(public_key, signature: bytes, data: bytes, scheme: str) -> bool:
    """Whether `signature` proves ownership of the key over `data`."""
    check_scheme(scheme, public_key)
    try:
        if scheme == 'ed25519':
            public_key.verify(signature, data)
        elif scheme.startswith('ecdsa'):
            if scheme == 'ecdsa-sha256-raw':
                size = (public_key.curve.key_size + 7) // 8
                if len(signature) != 2 * size:
                    return False
                signature = encode_dss_signature(
                    int.from_bytes(signature[:size], 'big'), int.from_bytes(signature[size:], 'big')
                )
            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        elif scheme == 'rsa-pss-sha256':
            public_key.verify(signature, data, padding.PSS(padding.MGF1(hashes.SHA256()), 32), hashes.SHA256())
        else:
            public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return False
    return True


# --- Certificates ---


def load_certificate(data: bytes) -> x509.Certificate:
    """An X.509 certificate from DER (as sent on the wire) or PEM."""
    data = bytes(data)
    try:
        if data.lstrip().startswith(b'-----BEGIN'):
            return x509.load_pem_x509_certificate(data)
        return x509.load_der_x509_certificate(data)
    except ValueError as e:
        raise UdsAuthenticationError(f'Not an X.509 certificate: {e}') from e


def revoked(certificate: x509.Certificate, issuer: x509.Certificate, crls: Sequence) -> bool:
    """Whether a revocation list of `issuer` (its signature verified) lists the certificate."""
    for crl in crls:
        if crl.issuer != issuer.subject:
            continue
        try:
            if not crl.is_signature_valid(issuer.public_key()):
                continue
        except (TypeError, ValueError):
            continue
        if crl.get_revoked_certificate_by_serial_number(certificate.serial_number) is not None:
            return True
    return False


def certificate_problem(
    certificate: x509.Certificate, trusted: Sequence[x509.Certificate], now=None, crls: Sequence = ()
) -> Optional[int]:
    """The certificate verification NRC for a certificate checked against trusted issuers, or None if it is
    valid: within its validity period, signed by one of them (or one of them itself), and not revoked by a
    revocation list of its issuer."""
    now = now or datetime.now(timezone.utc)
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        return INVALID_TIME_PERIOD
    fingerprints = {issuer.fingerprint(hashes.SHA256()) for issuer in trusted}
    if certificate.fingerprint(hashes.SHA256()) in fingerprints:
        return None
    issuers = [issuer for issuer in trusted if issuer.subject == certificate.issuer]
    if not issuers:
        return INVALID_CHAIN_OF_TRUST
    for issuer in issuers:
        try:
            certificate.verify_directly_issued_by(issuer)
        except (ValueError, TypeError, InvalidSignature):
            continue
        return INVALID_CERTIFICATE if revoked(certificate, issuer, crls) else None
    return INVALID_SIGNATURE


# --- The client side ---


@dataclass
class AuthenticationResult:
    authenticated: bool
    return_parameter: int
    steps: List[str] = field(default_factory=list)
    server_certificate: Optional[x509.Certificate] = None
    session_key_info: bytes = b''

    @property
    def description(self) -> str:
        return '; '.join(self.steps)


async def authenticate(
    client,
    certificate: x509.Certificate,
    private_key,
    scheme: str,
    bidirectional: bool = False,
    trusted: Sequence[x509.Certificate] = (),
    communication_configuration: int = 0,
    challenge_length: int = 32,
    random: Callable[[int], bytes] = os.urandom,
    crls: Sequence = (),
) -> AuthenticationResult:
    """Authenticate on a UdsClient by certificate exchange: verifyCertificate(Uni|Bi)directional, then
    proofOfOwnership with the signature over the server challenge. With `bidirectional`, the server's certificate
    is checked against `trusted` (when given) and its proof of ownership over our challenge is verified.
    Raises UdsError/UdsNegativeResponse (e.g. NRC 0x51 for a certificate the ECU refuses) or UdsAuthenticationError."""
    task = VERIFY_CERTIFICATE_BIDIRECTIONAL if bidirectional else VERIFY_CERTIFICATE_UNIDIRECTIONAL
    our_challenge = random(challenge_length) if bidirectional else b''
    der = certificate.public_bytes(serialization.Encoding.DER)
    response = parse_response(
        await client.call(request(task, communication_configuration, certificate=der, challenge=our_challenge))
    )
    result = AuthenticationResult(False, response.return_parameter, [f'{TASKS[task]}: {response.return_text}'])
    if response.return_parameter == AUTHENTICATION_COMPLETE:
        result.authenticated = True
        return result
    if response.return_parameter != OWNERSHIP_VERIFICATION_NECESSARY:
        raise UdsAuthenticationError(f'The ECU answered {response.return_text}')
    if not response.challenge:
        raise UdsAuthenticationError('The ECU sent no challenge to prove ownership of the certificate with')
    if bidirectional:
        server_certificate = load_certificate(response.certificate)
        if trusted:
            problem = certificate_problem(server_certificate, trusted, crls=crls)
            if problem is not None:
                raise UdsAuthenticationError(f'The ECU certificate is not trusted: {NEGATIVE_RESPONSE_CODES[problem]}')
        if not verify(server_certificate.public_key(), response.proof_of_ownership, our_challenge, scheme):
            raise UdsAuthenticationError("The ECU's proof of ownership of its certificate does not verify")
        result.server_certificate = server_certificate
        subject = server_certificate.subject.rfc4514_string()
        result.steps.append(f'ECU certificate {subject} ' + ('trusted and ' if trusted else '') + 'proven')
    if callable(private_key):  # A signer (e.g. a security profile or a key server) instead of a key
        proof = private_key(response.challenge, scheme)
        if inspect.isawaitable(proof):
            proof = await proof
        proof = bytes(proof)
    else:
        proof = sign(private_key, response.challenge, scheme)
    final = parse_response(await client.call(request(PROOF_OF_OWNERSHIP, proof_of_ownership=proof)))
    result.return_parameter, result.session_key_info = final.return_parameter, final.session_key_info
    result.steps.append(f'proofOfOwnership: {final.return_text}')
    result.authenticated = final.return_parameter == AUTHENTICATION_COMPLETE
    return result


async def deauthenticate(client) -> str:
    response = parse_response(await client.call(request(DE_AUTHENTICATE)))
    return response.return_text


async def authentication_configuration(client) -> str:
    response = parse_response(await client.call(request(AUTHENTICATION_CONFIGURATION)))
    return response.return_text


# --- Authentication by challenge-response (ACR) ---

PROOF_MACS = {
    'hmac-sha256': 'HMAC-SHA-256 with a shared key',
    'aes-cmac': 'AES-CMAC with a shared key',
}

Prover = Callable[[bytes, bytes], Any]  # (challenge, algorithm indicator) -> proof (bytes, or awaitable bytes)
ProofVerifier = Callable[[bytes, bytes, bytes], bool]  # (challenge, proof, algorithm indicator) -> verified


def proof_mac(key: bytes, data: bytes, algorithm: str) -> bytes:
    """A symmetric proof of ownership: the MAC of `data` (the peer's challenge) with a shared key."""
    if algorithm == 'hmac-sha256':
        return hmac.new(bytes(key), bytes(data), hashlib.sha256).digest()
    if algorithm == 'aes-cmac':
        from can_explorer.core.secoc import cmac_aes

        try:
            return cmac_aes(bytes(key), bytes(data))
        except ValueError as e:
            raise UdsAuthenticationError(str(e)) from e
    raise UdsAuthenticationError(f'Unknown MAC {algorithm!r}; one of {", ".join(PROOF_MACS)}')


def symmetric_prover(key: bytes, algorithm: str = 'hmac-sha256') -> Prover:
    proof_mac(key, b'', algorithm)  # Fail now on a bad key or algorithm
    return lambda challenge, _indicator: proof_mac(key, challenge, algorithm)


def symmetric_verifier(key: bytes, algorithm: str = 'hmac-sha256') -> ProofVerifier:
    proof_mac(key, b'', algorithm)
    return lambda challenge, proof, _indicator: hmac.compare_digest(proof_mac(key, challenge, algorithm), bytes(proof))


def asymmetric_prover(private_key, scheme: str) -> Prover:
    check_scheme(scheme, private_key)
    return lambda challenge, _indicator: sign(private_key, challenge, scheme)


def asymmetric_verifier(public_keys: Sequence, scheme: str) -> ProofVerifier:
    """Verifies a signature made with the private key of any of `public_keys` (e.g. the ECU certificates)."""
    keys = [key for key in public_keys if _fits(scheme, key)]
    if not keys:
        raise UdsAuthenticationError(f'None of the public keys fits the signature scheme {scheme}')
    return lambda challenge, proof, _indicator: any(verify(key, bytes(proof), challenge, scheme) for key in keys)


def _fits(scheme: str, key) -> bool:
    try:
        check_scheme(scheme, key)
    except UdsAuthenticationError:
        return False
    return True


def _encode_oid(oid: str) -> bytes:
    try:
        arcs = [int(part) for part in oid.strip().split('.')]
    except ValueError:
        raise UdsAuthenticationError(f'Not an object identifier: {oid!r}') from None
    if len(arcs) < 2 or arcs[0] > 2 or (arcs[0] < 2 and arcs[1] >= 40) or min(arcs) < 0:
        raise UdsAuthenticationError(f'Not an object identifier: {oid!r}')
    content = bytearray()
    for arc in [arcs[0] * 40 + arcs[1]] + arcs[2:]:
        chunk = [arc & 0x7F]
        while arc > 0x7F:
            arc >>= 7
            chunk.append(0x80 | (arc & 0x7F))
        content += bytes(reversed(chunk))
    return bytes([0x06, len(content)]) + bytes(content)


def algorithm_indicator(text: str) -> bytes:
    """The 16-byte algorithmIndicator from hex ('' for zeros) or a dotted OID such as 2.16.840.1.101.3.4.2.1.
    Both are left aligned and zero padded; an OID is DER encoded with its tag and length (a choice: give the bytes
    in hex when an ECU expects only the OID's value)."""
    text = text.strip()
    if text and '.' in text and all(part.isdigit() for part in text.split('.')):
        value = _encode_oid(text)
    else:
        try:
            value = bytes.fromhex(text.replace(' ', '').removeprefix('0x'))
        except ValueError:
            raise UdsAuthenticationError(f'The algorithm indicator is hex or a dotted OID, not {text!r}') from None
    if len(value) > 16:
        raise UdsAuthenticationError(f'The algorithm indicator has 16 bytes, not {len(value)}')
    return value.ljust(16, b'\0')


async def authenticate_challenge_response(
    client,
    prove: Prover,
    bidirectional: bool = False,
    verify_server: Optional[ProofVerifier] = None,
    indicator: bytes = bytes(16),
    communication_configuration: int = 0,
    additional_parameter: bytes = b'',
    challenge_length: int = 32,
    random: Callable[[int], bytes] = os.urandom,
) -> AuthenticationResult:
    """Authenticate on a UdsClient by challenge-response: requestChallengeForAuthentication, then
    verifyProofOfOwnership(Uni|Bi)directional with `prove(challenge, algorithm indicator)`. The proof is made for the
    algorithm the ECU answers with. With `bidirectional`, the ECU's proof over our challenge is checked with
    `verify_server` (when given; otherwise the result says it was not checked).
    Raises UdsError/UdsNegativeResponse (e.g. NRC 0x58 for a proof the ECU refuses) or UdsAuthenticationError."""
    challenge_request = request(
        REQUEST_CHALLENGE_FOR_AUTHENTICATION, communication_configuration, algorithm_indicator=indicator
    )
    response = parse_response(await client.call(challenge_request))
    result = AuthenticationResult(
        False, response.return_parameter, [f'requestChallengeForAuthentication: {response.return_text}']
    )
    if response.return_parameter == GENERAL_REJECT:
        raise UdsAuthenticationError(f'The ECU answered {response.return_text}')
    if not response.challenge:
        raise UdsAuthenticationError('The ECU sent no challenge to prove ownership with')
    if response.needed_additional_parameter and not additional_parameter:
        raise UdsAuthenticationError(
            f'The ECU needs an additional parameter ({response.needed_additional_parameter.hex(" ").upper()}); '
            'what it holds is manufacturer specific'
        )
    used = response.algorithm_indicator  # The ECU's algorithm
    if used != bytes(indicator) and any(used):
        result.steps.append(f'the ECU uses algorithm {used.rstrip(bytes(1)).hex(" ").upper()}')
    proof = prove(response.challenge, used)
    if inspect.isawaitable(proof):
        proof = await proof
    our_challenge = random(challenge_length) if bidirectional else b''
    task = VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL if bidirectional else VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL
    final = parse_response(
        await client.call(
            request(
                task,
                algorithm_indicator=used,
                proof_of_ownership=bytes(proof),
                challenge=our_challenge,
                additional_parameter=additional_parameter,
            )
        )
    )
    result.return_parameter, result.session_key_info = final.return_parameter, final.session_key_info
    result.steps.append(f'{TASKS[task]}: {final.return_text}')
    result.authenticated = final.return_parameter == AUTHENTICATION_COMPLETE
    if bidirectional and result.authenticated:
        if verify_server is None:
            result.steps.append("the ECU's proof was not checked")
        elif not verify_server(our_challenge, final.proof_of_ownership, used):
            raise UdsAuthenticationError("The ECU's proof of ownership does not verify: it may not be the ECU")
        else:
            result.steps.append("the ECU's proof verified")
    return result


@dataclass
class ChallengeResponseConfig:
    """The challenge-response side of a simulated ECU (AuthenticationServer)."""

    verify: ProofVerifier  # The client's proof over the server challenge
    prove: Optional[Prover] = None  # The ECU's proof over the client challenge (verifyProofOfOwnershipBidirectional)
    symmetric: bool = True  # What authenticationConfiguration reports: ACR with symmetric or asymmetric cryptography
    indicator: bytes = bytes(16)  # The algorithmIndicator the ECU answers with
    needed_additional_parameter: bytes = b''  # Announced; requests without an additional parameter are refused


def symmetric_challenge_response(key: bytes, algorithm: str = 'hmac-sha256', **kwargs) -> ChallengeResponseConfig:
    """An ECU sharing `key` with the tester (both proofs are MACs with it)."""
    return ChallengeResponseConfig(symmetric_verifier(key, algorithm), symmetric_prover(key, algorithm), True, **kwargs)


def asymmetric_challenge_response(
    client_public_keys: Sequence, scheme: str, private_key=None, **kwargs
) -> ChallengeResponseConfig:
    """An ECU knowing the testers' public keys, and proving with its own private key (bidirectional) if given."""
    prover = asymmetric_prover(private_key, scheme) if private_key is not None else None
    return ChallengeResponseConfig(asymmetric_verifier(client_public_keys, scheme), prover, False, **kwargs)


# --- The server side (simulated ECUs), after the AUTOSAR Dcm ---


class AuthenticationServer:
    """The certificate-exchange side of an ECU: verifies client certificates against trusted issuers, challenges
    the client and verifies its proof of ownership. `handle` returns the response to an Authentication request."""

    def __init__(
        self,
        trusted: Sequence[x509.Certificate],
        scheme: str = 'ecdsa-sha256',
        certificate: Optional[x509.Certificate] = None,
        private_key=None,
        challenge_length: int = 32,
        random: Callable[[int], bytes] = os.urandom,
        crls: Sequence = (),
        challenge_response: Optional[ChallengeResponseConfig] = None,
    ):
        self.trusted = list(trusted)
        self.challenge_response = challenge_response  # Offers the challenge-response sub-functions (0x05..0x07)
        self.crls = list(crls)  # Revocation lists: a revoked client certificate is refused with NRC 0x57
        self.scheme = scheme
        self.certificate, self.private_key = certificate, private_key  # For verifyCertificateBidirectional
        self.challenge_length = challenge_length
        self.random = random
        self.authenticated = False
        self.client_certificate: Optional[x509.Certificate] = None
        self._challenge: Optional[bytes] = None

    @staticmethod
    def _negative(code: int) -> bytes:
        return bytes([0x7F, SID, code])

    @staticmethod
    def _positive(task: int, return_parameter: int, *parameters: bytes) -> bytes:
        return bytes([SID + 0x40, task, return_parameter]) + b''.join(parameters)

    def handle(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return self._negative(0x13)
        task = payload[1] & 0x7F
        if task == DE_AUTHENTICATE:
            self.authenticated, self.client_certificate, self._challenge = False, None, None
            return self._positive(task, DEAUTHENTICATED)
        if task == AUTHENTICATION_CONFIGURATION:
            acr = self.challenge_response
            if acr is not None and not self.trusted:
                return self._positive(task, ACR_SYMMETRIC if acr.symmetric else ACR_ASYMMETRIC)
            return self._positive(task, APCE)  # Authentication by certificate exchange
        if task in (VERIFY_CERTIFICATE_UNIDIRECTIONAL, VERIFY_CERTIFICATE_BIDIRECTIONAL):
            return self._verify_certificate(task, payload)
        if task == PROOF_OF_OWNERSHIP:
            return self._proof_of_ownership(payload)
        if self.challenge_response is None:
            return self._negative(0x12)  # Challenge-response is not offered (as in the AUTOSAR Dcm)
        if task == REQUEST_CHALLENGE_FOR_AUTHENTICATION:
            return self._request_challenge(payload)
        if task in (VERIFY_PROOF_OF_OWNERSHIP_UNIDIRECTIONAL, VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL):
            return self._verify_proof(task, payload)
        return self._negative(0x12)

    def _request_challenge(self, payload: bytes) -> bytes:
        self.authenticated, self.client_certificate, self._challenge = False, None, None
        if len(payload) != 2 + 1 + 16:
            return self._negative(0x13)
        if payload[2] != 0x00:
            return self._negative(0x31)  # Only communicationConfiguration 0 (no session key)
        acr = self.challenge_response
        self._challenge = self.random(self.challenge_length)
        return self._positive(
            REQUEST_CHALLENGE_FOR_AUTHENTICATION,
            REQUEST_ACCEPTED,
            _algorithm_indicator(acr.indicator),
            _length_prefixed(self._challenge),
            _length_prefixed(acr.needed_additional_parameter),
        )

    def _verify_proof(self, task: int, payload: bytes) -> bytes:
        if self._challenge is None or self.client_certificate is not None:
            return self._negative(0x24)  # requestSequenceError: no challenge requested
        try:
            reader = _Reader(bytes(payload[2:]))
            indicator = reader.take(16, 'algorithm indicator')
            proof, client_challenge = reader.prefixed('proof'), reader.prefixed('challenge')
            additional = reader.prefixed('additional parameter')
        except UdsAuthenticationError:
            return self._negative(0x13)
        if reader.position != len(reader.data):
            return self._negative(0x13)
        acr = self.challenge_response
        challenge, self._challenge = self._challenge, None  # One proof per challenge
        if acr.needed_additional_parameter and not additional:
            return self._negative(0x31)
        if task == VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL and (acr.prove is None or not client_challenge):
            return self._negative(0x12 if acr.prove is None else 0x31)
        if not acr.verify(challenge, proof, indicator):
            return self._negative(OWNERSHIP_VERIFICATION_FAILED)
        self.authenticated = True
        parameters = [_algorithm_indicator(acr.indicator)]
        if task == VERIFY_PROOF_OF_OWNERSHIP_BIDIRECTIONAL:
            parameters.append(_length_prefixed(bytes(acr.prove(client_challenge, indicator))))
        return self._positive(task, AUTHENTICATION_COMPLETE, *parameters, _length_prefixed(b''))

    def _verify_certificate(self, task: int, payload: bytes) -> bytes:
        self.authenticated, self._challenge = False, None
        if len(payload) < 3:
            return self._negative(0x13)
        if payload[2] != 0x00:
            return self._negative(0x31)  # Only communicationConfiguration 0 (no session key), as in the AUTOSAR Dcm
        try:
            reader = _Reader(bytes(payload[3:]))
            raw_certificate, client_challenge = reader.prefixed('certificate'), reader.prefixed('challenge')
        except UdsAuthenticationError:
            return self._negative(0x13)
        if reader.position != len(reader.data) or not raw_certificate:
            return self._negative(0x13)
        try:
            certificate = load_certificate(raw_certificate)
        except UdsAuthenticationError:
            return self._negative(INVALID_FORMAT)
        problem = certificate_problem(certificate, self.trusted, crls=self.crls)
        if problem is not None:
            return self._negative(problem)
        try:
            check_scheme(self.scheme, certificate.public_key())
        except UdsAuthenticationError:
            return self._negative(0x53)  # A key the configured signature scheme cannot use: invalid type
        self.client_certificate = certificate
        self._challenge = self.random(self.challenge_length)
        if task == VERIFY_CERTIFICATE_UNIDIRECTIONAL:
            return self._positive(
                task, OWNERSHIP_VERIFICATION_NECESSARY, _length_prefixed(self._challenge), _length_prefixed(b'')
            )
        if self.certificate is None or self.private_key is None:
            return self._negative(0x12)
        proof = sign(self.private_key, client_challenge, self.scheme)
        own = self.certificate.public_bytes(serialization.Encoding.DER)
        return self._positive(
            task,
            OWNERSHIP_VERIFICATION_NECESSARY,
            _length_prefixed(self._challenge),
            _length_prefixed(own),
            _length_prefixed(proof),
            _length_prefixed(b''),
        )

    def _proof_of_ownership(self, payload: bytes) -> bytes:
        if self._challenge is None or self.client_certificate is None:
            return self._negative(0x24)  # requestSequenceError: no verified certificate yet
        try:
            reader = _Reader(bytes(payload[2:]))
            proof, _ephemeral = reader.prefixed('proof'), reader.prefixed('ephemeral public key')
        except UdsAuthenticationError:
            return self._negative(0x13)
        challenge, self._challenge = self._challenge, None  # One proof per challenge
        if not verify(self.client_certificate.public_key(), proof, challenge, self.scheme):
            return self._negative(OWNERSHIP_VERIFICATION_FAILED)
        self.authenticated = True
        return self._positive(PROOF_OF_OWNERSHIP, AUTHENTICATION_COMPLETE, _length_prefixed(b''))


def certificates_from_pem(pem: str) -> List[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(pem.encode('ascii'))
    except ValueError as e:
        raise UdsAuthenticationError(f'Cannot read the trusted certificates: {e}') from e


def private_key_from_pem(pem: bytes):
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as e:
        raise UdsAuthenticationError(f'Cannot read the private key: {e}') from e


def scheme_for(key) -> str:
    """The usual scheme for a key type (a starting point; the ECU decides)."""
    if isinstance(key, (ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey)):
        return 'ed25519'
    if isinstance(key, (rsa.RSAPrivateKey, rsa.RSAPublicKey)):
        return 'rsa-pss-sha256'
    return 'ecdsa-sha256'


# --- Credentials from the key store ---


def _profile(source: str):
    """The security profile a 'profile:<name>' source names, or None for other sources."""
    from can_explorer.core import security_profiles

    if not source.startswith(security_profiles.PREFIX):
        return None
    name = source[len(security_profiles.PREFIX) :]
    try:
        return security_profiles.registry().get(name)
    except security_profiles.SecurityProfileError as e:
        raise UdsAuthenticationError(str(e)) from e


def _profile_call(profile, method: str, *args):
    from can_explorer.core import security_profiles

    try:
        return getattr(profile, method)(*args)
    except security_profiles.SecurityProfileError as e:
        raise UdsAuthenticationError(str(e)) from e


def credentials(store, name: str):
    """(certificate, private key) of a TLS client certificate entry of the key store (it must be unlocked) — or,
    for 'profile:<name>', the security profile's certificate and a signer calling its `sign` (the private key
    stays with the profile, e.g. on a key server). `authenticate` takes either."""
    from can_explorer.core import keystore

    profile = _profile(name)
    if profile is not None:
        if not (callable(getattr(profile, 'certificate', None)) and callable(getattr(profile, 'sign', None))):
            raise UdsAuthenticationError(f'The security profile {name!r} has no certificate to authenticate with')
        certificate = _profile_call(profile, 'certificate')
        if certificate is None:
            raise UdsAuthenticationError(f'The security profile {name!r} has no certificate')
        from can_explorer.core import audit

        audit.record('authentication certificate used', profile=name)
        return certificate, lambda data, scheme: _profile_call(profile, 'sign', data, scheme)
    info = store.entries_by_name().get(name)
    if info is None or info.purpose != keystore.TLS_CLIENT:
        raise UdsAuthenticationError(f'No client certificate named {name!r} in the Security Manager')
    try:
        private_key = private_key_from_pem(store.secrets(name)['private_key'])
    except keystore.KeyStoreError as e:
        raise UdsAuthenticationError(str(e)) from e
    from can_explorer.core import audit

    audit.record('authentication certificate used', entry=name)
    return certificates_from_pem(info.params['certificate'])[0], private_key


def trusted_certificates(store, name: str) -> List[x509.Certificate]:
    """The certificates of a trusted-certificates entry of the key store (readable while it is locked)."""
    from can_explorer.core import keystore

    info = store.entries_by_name().get(name)
    if info is None or info.purpose != keystore.TLS_CA:
        raise UdsAuthenticationError(f'No trusted certificates named {name!r} in the Security Manager')
    return certificates_from_pem(info.params['certificate'])


def challenge_response_credentials(
    store, source: str, scheme: str = '', trusted: str = ''
) -> Tuple[Prover, Optional[ProofVerifier], str]:
    """(prove, verify the ECU's proof or None, description) for authentication by challenge-response from:
    - a shared key entry (purpose uds-auth-key): symmetric, both proofs are MACs with the key;
    - a client certificate entry (tls-client): asymmetric, its private key signs (`scheme`); the ECU's proof is
      checked against the certificates of the trusted-certificates entry `trusted`, if given;
    - 'profile:<name>': the profile's `prove_challenge(challenge, indicator)` (or its `sign`), and its
      `verify_challenge(challenge, proof, indicator)` (or the `trusted` certificates) for the ECU's proof."""
    from can_explorer.core import audit, keystore

    def trusted_verifier() -> Optional[ProofVerifier]:
        if not trusted:
            return None
        return asymmetric_verifier([c.public_key() for c in trusted_certificates(store, trusted)], scheme)

    profile = _profile(source)
    if profile is not None:
        can_prove, can_sign = (callable(getattr(profile, name, None)) for name in ('prove_challenge', 'sign'))
        if not can_prove and not can_sign:
            raise UdsAuthenticationError(f'The security profile {source!r} cannot prove a challenge')
        fallback = trusted_verifier()

        def prove(challenge: bytes, indicator: bytes) -> bytes:
            proof = _profile_call(profile, 'prove_challenge', challenge, indicator) if can_prove else None
            if proof is None:  # No proof of its own: a signature over the challenge
                if not can_sign or not scheme:
                    raise UdsAuthenticationError(
                        f'The security profile {source!r} gives no proof; choose the signature scheme it signs with'
                        if can_sign
                        else f'The security profile {source!r} cannot prove this challenge'
                    )
                proof = _profile_call(profile, 'sign', challenge, scheme)
            return bytes(proof)

        def verify_with_profile(challenge: bytes, proof: bytes, indicator: bytes) -> bool:
            valid = _profile_call(profile, 'verify_challenge', challenge, proof, indicator)
            if valid is None:
                if fallback is None:
                    raise UdsAuthenticationError(
                        f"The security profile {source!r} cannot check the ECU's proof; choose trusted certificates"
                    )
                return fallback(challenge, proof, indicator)
            return bool(valid)

        verifier = verify_with_profile if callable(getattr(profile, 'verify_challenge', None)) else fallback
        audit.record('authentication by challenge-response', profile=source)
        return prove, verifier, f'security profile {source[len("profile:") :]}'
    info = store.entries_by_name().get(source)
    if info is None or info.purpose not in (keystore.AUTH_KEY, keystore.TLS_CLIENT):
        raise UdsAuthenticationError(f'No authentication key or client certificate named {source!r}')
    if info.purpose == keystore.AUTH_KEY:
        try:
            key = store.secrets(source)['key']
        except keystore.KeyStoreError as e:
            raise UdsAuthenticationError(str(e)) from e
        algorithm = info.params.get('mac', 'hmac-sha256')
        audit.record('authentication key used', entry=source)
        return symmetric_prover(key, algorithm), symmetric_verifier(key, algorithm), f'shared key ({algorithm})'
    _certificate, private_key = credentials(store, source)
    scheme = scheme or scheme_for(private_key)
    return asymmetric_prover(private_key, scheme), trusted_verifier(), f'private key ({scheme})'
