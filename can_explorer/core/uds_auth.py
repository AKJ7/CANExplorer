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

Also after the AUTOSAR Dcm (§7.6.2.10.4-7): the tester's **role and white lists** taken from its certificate
(`AccessRights`; the extensions holding them are configured by OID, `AccessOids`), **transmitCertificate** (0x04,
general-purpose certificates stored per certificateEvaluationId; NRC 0x31 for an unknown id), and, in the simulated
ECU (core/simulation.py), role checks per service and de-authentication on idle or an S3server timeout.

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
CERTIFICATE_VERIFIED = 0x13  # transmitCertificate

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
INVALID_CONTENT = 0x55  # Also for a role of the wrong size (SWS_Dcm_01515)
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
    # What session key handling needs (core/uds_secured.py SessionContext): how the tester authenticated, the
    # communicationConfiguration asked for, both challenges and the algorithm the ECU used
    method: str = ''  # 'certificate' or 'challenge-response'
    communication_configuration: int = 0
    server_challenge: bytes = b''
    client_challenge: bytes = b''
    algorithm_indicator: bytes = b''
    client_certificate: Optional[x509.Certificate] = None
    server_ephemeral_public_key: bytes = b''  # Certificate exchange: for a key agreement, if the ECU sends one
    client_ephemeral_public_key: bytes = b''

    @property
    def description(self) -> str:
        text = '; '.join(self.steps)
        if self.session_key_info:
            text += f'; session key info {len(self.session_key_info)} bytes'
        return text


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
    ephemeral_public_key=b'',
) -> AuthenticationResult:
    """Authenticate on a UdsClient by certificate exchange: verifyCertificate(Uni|Bi)directional, then
    proofOfOwnership with the signature over the server challenge. With `bidirectional`, the server's certificate
    is checked against `trusted` (when given) and its proof of ownership over our challenge is verified.
    `ephemeral_public_key` (bytes, or a function of the ECU's ephemeral public key returning ours) is sent with the
    proof for a key agreement; how a session key follows from it is manufacturer specific (core/uds_secured.py).
    Raises UdsError/UdsNegativeResponse (e.g. NRC 0x51 for a certificate the ECU refuses) or UdsAuthenticationError."""
    task = VERIFY_CERTIFICATE_BIDIRECTIONAL if bidirectional else VERIFY_CERTIFICATE_UNIDIRECTIONAL
    our_challenge = random(challenge_length) if bidirectional else b''
    der = certificate.public_bytes(serialization.Encoding.DER)
    response = parse_response(
        await client.call(request(task, communication_configuration, certificate=der, challenge=our_challenge))
    )
    result = AuthenticationResult(False, response.return_parameter, [f'{TASKS[task]}: {response.return_text}'])
    result.method, result.communication_configuration = 'certificate', communication_configuration
    result.client_challenge, result.server_challenge = our_challenge, response.challenge
    result.client_certificate = certificate
    result.server_ephemeral_public_key = response.ephemeral_public_key
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
    ephemeral = (
        ephemeral_public_key(response.ephemeral_public_key) if callable(ephemeral_public_key) else ephemeral_public_key
    )
    result.client_ephemeral_public_key = bytes(ephemeral or b'')
    final = parse_response(
        await client.call(
            request(
                PROOF_OF_OWNERSHIP, proof_of_ownership=proof, ephemeral_public_key=result.client_ephemeral_public_key
            )
        )
    )
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


async def transmit_certificate(client, certificate, evaluation_id: int) -> str:
    """transmitCertificate (0x04): give the ECU a general-purpose certificate (e.g. for secure coding, SecOC or TLS)
    under `evaluation_id`, which tells the ECU what it is for (manufacturer specific). `certificate` is an
    x509.Certificate (sent DER encoded) or bytes. Returns the authenticationReturnParameter text ("certificate
    verified"); an ECU refusing it answers a negative response (e.g. NRC 0x31 for an unknown evaluation id)."""
    if not 0 <= int(evaluation_id) <= 0xFFFF:
        raise UdsAuthenticationError('The certificate evaluation id is a 16-bit number')
    der = certificate.public_bytes(serialization.Encoding.DER) if hasattr(certificate, 'public_bytes') else certificate
    if not der:
        raise UdsAuthenticationError('No certificate to transmit')
    response = parse_response(
        await client.call(
            request(TRANSMIT_CERTIFICATE, certificate=bytes(der), certificate_evaluation_id=evaluation_id)
        )
    )
    return response.return_text


# --- Access rights from the certificate: roles and white lists (AUTOSAR Dcm §7.6.2.10.4-5) ---
#
# After a certificate exchange the AUTOSAR Dcm takes the tester's role and white lists from the certificate:
# - the role, a bit field of DcmDspAuthenticationRoleSize bytes read as a little-endian integer (SWS_Dcm_01521); a
#   service runs when the role AND the service's configured role is not 0, else NRC 0x34 authenticationRequired
#   (SWS_Dcm_01522/01523); a role of another size is refused with NRC 0x55 InvalidContent (SWS_Dcm_01515);
# - white lists: services (values of 1..4 bytes, a request is allowed when it starts with one, SWS_Dcm_01524), DIDs
#   (2-byte DID big endian + an access byte: bit 0 read, bit 1 write, bit 2 IO control, SWS_Dcm_01525), RIDs (2-byte
#   RID + bit 0 start, bit 1 stop, bit 2 request results, SWS_Dcm_01526) and fault memory selections (1 byte,
#   SWS_Dcm_01527).
# Where in the certificate these are is a configuration of the ECU (KeyM certificate elements, by OID), so the OIDs
# are given (`AccessOids`); nothing is assumed. Each element's content is read as the Dcm example shows it: the
# extension value an OCTET STRING, or a SEQUENCE/SET of OCTET STRINGs (one per white list entry).

DID_READ, DID_WRITE, DID_CONTROL = 0x01, 0x02, 0x04
RID_START, RID_STOP, RID_RESULTS = 0x01, 0x02, 0x04


@dataclass(frozen=True)
class AccessOids:
    """The certificate extensions (dotted OIDs) holding the role and the white lists; '' when not used."""

    role: str = ''
    services: str = ''
    dids: str = ''
    rids: str = ''
    memory_selections: str = ''

    def __bool__(self) -> bool:
        return any((self.role, self.services, self.dids, self.rids, self.memory_selections))


def _der_items(data: bytes) -> List[Tuple[int, bytes]]:
    """The (tag, content) items of DER `data` at one level (definite lengths only)."""
    items, position = [], 0
    while position < len(data):
        if position + 2 > len(data):
            raise UdsAuthenticationError('Truncated DER in a certificate extension')
        tag, length = data[position], data[position + 1]
        position += 2
        if length & 0x80:
            count = length & 0x7F
            if not 1 <= count <= 4 or position + count > len(data):
                raise UdsAuthenticationError('Unsupported DER length in a certificate extension')
            length = int.from_bytes(data[position : position + count], 'big')
            position += count
        if position + length > len(data):
            raise UdsAuthenticationError('Truncated DER in a certificate extension')
        items.append((tag, bytes(data[position : position + length])))
        position += length
    return items


def _extension_elements(certificate: x509.Certificate, oid: str) -> Optional[List[bytes]]:
    """The element values of a private extension: [content] for an OCTET STRING (or INTEGER), the contents of each
    OCTET STRING of a SEQUENCE/SET; None when the certificate has no such extension."""
    try:
        extension = certificate.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid))
    except x509.ExtensionNotFound:
        return None
    except ValueError as e:
        raise UdsAuthenticationError(f'Invalid OID {oid!r}: {e}') from e
    raw = getattr(extension.value, 'value', None)
    if not isinstance(raw, bytes):
        raise UdsAuthenticationError(f'The extension {oid} is a standard extension, not access rights')
    items = _der_items(raw)
    if len(items) != 1:
        raise UdsAuthenticationError(f'The extension {oid} is not a single DER value')
    tag, content = items[0]
    if tag in (0x04, 0x02):  # OCTET STRING, INTEGER
        return [content]
    if tag in (0x30, 0x31):  # SEQUENCE, SET
        elements = []
        for inner_tag, inner in _der_items(content):
            if inner_tag != 0x04:
                raise UdsAuthenticationError(f'The extension {oid} holds a non-OCTET STRING element')
            elements.append(inner)
        return elements
    raise UdsAuthenticationError(f'The extension {oid} is neither an OCTET STRING nor a list of them')


@dataclass
class AccessRights:
    """A tester's role and white lists, from its certificate."""

    role: Optional[int] = None  # None: the certificate has no role
    role_size: int = 0
    services: List[bytes] = field(default_factory=list)
    dids: dict = field(default_factory=dict)  # DID -> access bits
    rids: dict = field(default_factory=dict)  # RID -> sub-function bits
    memory_selections: List[int] = field(default_factory=list)

    def white_listed(self, request: bytes) -> bool:
        """Whether a white list allows the request (services by prefix, DIDs per operation, RIDs per
        sub-function, fault memory selections of 0x19 0x17..0x19)."""
        request = bytes(request)
        if any(request.startswith(entry) for entry in self.services if entry):
            return True
        if not request:
            return False
        sid = request[0]
        if sid in (0x22, 0x2E, 0x2F) and len(request) >= 3:
            bit = {0x22: DID_READ, 0x2E: DID_WRITE, 0x2F: DID_CONTROL}[sid]
            if sid == 0x22:  # Every DID of a multi-DID read must be allowed
                dids = [int.from_bytes(request[i : i + 2], 'big') for i in range(1, len(request) - 1, 2)]
            else:
                dids = [int.from_bytes(request[1:3], 'big')]
            return bool(dids) and all(self.dids.get(did, 0) & bit for did in dids)
        if sid == 0x31 and len(request) >= 4:
            bit = {1: RID_START, 2: RID_STOP, 3: RID_RESULTS}.get(request[1] & 0x7F, 0)
            return bool(self.rids.get(int.from_bytes(request[2:4], 'big'), 0) & bit)
        if sid == 0x19 and len(request) >= 3 and request[1] & 0x7F in (0x17, 0x18, 0x19):
            return request[-1] in self.memory_selections
        return False

    def allows(self, request: bytes, service_role: Optional[int]) -> bool:
        """A request needing `service_role` (None: no role configured, always allowed): allowed by the role
        (bitwise AND not 0) or a white list."""
        if service_role is None:
            return True
        return bool((self.role or 0) & service_role) or self.white_listed(request)

    def describe(self) -> str:
        parts = []
        if self.role is not None:
            parts.append(f'role 0x{self.role:0{2 * max(self.role_size, 1)}X}')
        if self.services:
            parts.append('services ' + ', '.join(entry.hex().upper() for entry in self.services))
        if self.dids:
            parts.append('DIDs ' + ', '.join(f'{did:04X}:{bits:X}' for did, bits in self.dids.items()))
        if self.rids:
            parts.append('RIDs ' + ', '.join(f'{rid:04X}:{bits:X}' for rid, bits in self.rids.items()))
        if self.memory_selections:
            parts.append('memory selections ' + ', '.join(f'0x{m:02X}' for m in self.memory_selections))
        return '; '.join(parts) or 'no role or white list'


def access_rights(certificate: x509.Certificate, oids: AccessOids, role_size: int = 0) -> AccessRights:
    """The role and white lists a certificate carries at `oids`. With `role_size`, a role of another size is
    invalid content (NRC 0x55 at an ECU). Raises UdsAuthenticationError for malformed content."""
    rights = AccessRights()
    if oids.role:
        elements = _extension_elements(certificate, oids.role)
        if elements is not None:
            if len(elements) != 1:
                raise UdsAuthenticationError('The role extension holds more than one value')
            value = elements[0]
            if role_size and len(value) != role_size:
                raise UdsAuthenticationError(f'The role is {len(value)} bytes, the ECU expects {role_size}')
            rights.role, rights.role_size = int.from_bytes(value, 'little'), len(value)
    for oid, sizes, what in (
        (oids.services, (1, 2, 3, 4), 'service white list'),
        (oids.dids, (3,), 'DID white list'),
        (oids.rids, (3,), 'RID white list'),
        (oids.memory_selections, (1,), 'memory selection white list'),
    ):
        if not oid:
            continue
        for element in _extension_elements(certificate, oid) or []:
            if len(element) not in sizes:
                raise UdsAuthenticationError(f'A {what} entry of {len(element)} bytes')
            if what.startswith('service'):
                rights.services.append(element)
            elif what.startswith('DID'):
                rights.dids[int.from_bytes(element[:2], 'big')] = element[2]
            elif what.startswith('RID'):
                rights.rids[int.from_bytes(element[:2], 'big')] = element[2]
            else:
                rights.memory_selections.append(element[0])
    return rights


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 0x80:
        return bytes([tag, length]) + content
    size = (length.bit_length() + 7) // 8
    return bytes([tag, 0x80 | size]) + length.to_bytes(size, 'big') + content


def access_extensions(
    oids: AccessOids,
    role: Optional[bytes] = None,
    services: Sequence[bytes] = (),
    dids: Sequence[Tuple[int, int]] = (),
    rids: Sequence[Tuple[int, int]] = (),
    memory_selections: Sequence[int] = (),
) -> List[x509.UnrecognizedExtension]:
    """Certificate extensions carrying a role and white lists at `oids`, in the layout `access_rights` reads (for
    test certificates and simulated testers). `role` is the role bit field as it is sent (little endian)."""

    def listing(oid: str, elements: Sequence[bytes]) -> x509.UnrecognizedExtension:
        content = b''.join(_der(0x04, bytes(element)) for element in elements)
        return x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), _der(0x31, content))

    extensions = []
    if role is not None and oids.role:
        extensions.append(x509.UnrecognizedExtension(x509.ObjectIdentifier(oids.role), _der(0x04, bytes(role))))
    if services and oids.services:
        extensions.append(listing(oids.services, services))
    if dids and oids.dids:
        extensions.append(listing(oids.dids, [did.to_bytes(2, 'big') + bytes([bits]) for did, bits in dids]))
    if rids and oids.rids:
        extensions.append(listing(oids.rids, [rid.to_bytes(2, 'big') + bytes([bits]) for rid, bits in rids]))
    if memory_selections and oids.memory_selections:
        extensions.append(listing(oids.memory_selections, [bytes([m]) for m in memory_selections]))
    return extensions


def private_extensions(certificate: x509.Certificate) -> List[Tuple[str, int]]:
    """The (OID, size) of a certificate's non-standard extensions: where an ECU may find roles and white lists."""
    return [
        (extension.oid.dotted_string, len(extension.value.value))
        for extension in certificate.extensions
        if isinstance(extension.value, x509.UnrecognizedExtension)
    ]


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
    result.method, result.communication_configuration = 'challenge-response', communication_configuration
    result.server_challenge = response.challenge
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
    result.client_challenge, result.algorithm_indicator = our_challenge, used
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
        session_keys=None,
        access_oids: Optional[AccessOids] = None,
        role_size: int = 0,
        deauthenticated_role: int = 0,
        certificate_evaluation_ids: Sequence[int] = (),
    ):
        # Session keys (core/uds_secured.py SessionKeyIssuer): the communicationConfiguration values it accepts
        # besides 0, and the session key it issues on success (`session_provider`, for SecuredDataTransmission)
        self.session_keys = session_keys
        self.session_provider = None
        self._communication_configuration = 0
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
        self._client_challenge = b''
        # Access control (AUTOSAR Dcm): where client certificates carry the role and white lists, the role size
        # (0: any), the role used while not authenticated, and the rights of the authenticated tester
        self.access_oids = access_oids or AccessOids()
        self.role_size = role_size
        self.deauthenticated_role = deauthenticated_role
        self.access: Optional[AccessRights] = None
        # transmitCertificate (0x04): the certificate evaluation ids the ECU knows, and what it received for them
        self.certificate_evaluation_ids = set(certificate_evaluation_ids)
        self.received_certificates: dict = {}

    def deauthenticate(self) -> None:
        """Back to the deauthenticated state (deAuthenticate, a new authentication, a timeout): role, white list
        and session key are discarded (SWS_Dcm_01485)."""
        self.authenticated, self.client_certificate, self._challenge = False, None, None
        self.session_provider, self.access = None, None

    def permits(self, request: bytes, service_role: Optional[int]) -> bool:
        """Whether a request needing `service_role` (None: none configured) may run: with the authenticated
        tester's role or white lists (SWS_Dcm_01522), or the deauthenticated role (SWS_Dcm_01538). A tester
        authenticated without access rights (no OIDs configured, or challenge-response) may run everything."""
        if service_role is None:
            return True
        if self.authenticated:
            return self.access.allows(request, service_role) if self.access is not None else True
        return bool(self.deauthenticated_role & service_role)

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
            self.deauthenticate()
            return self._positive(task, DEAUTHENTICATED)
        if task == TRANSMIT_CERTIFICATE:
            return self._transmit_certificate(payload)
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

    def _accepts_configuration(self, value: int) -> bool:
        """communicationConfiguration 0 (no session key), or one the session key issuer accepts."""
        self._communication_configuration = value
        return value == 0x00 or (self.session_keys is not None and value in self.session_keys.configurations)

    def _session_key_info(self, method: str, challenge: bytes, client_challenge: bytes = b'', **context) -> bytes:
        """Issue the session key asked for (if any): its sessionKeyInfo; the provider is kept for 0x84."""
        if self._communication_configuration == 0x00 or self.session_keys is None:
            return b''
        from can_explorer.core.uds_secured import SessionContext

        info, self.session_provider = self.session_keys.create(
            SessionContext(method, self._communication_configuration, b'', challenge, client_challenge, **context)
        )
        return info

    def _transmit_certificate(self, payload: bytes) -> bytes:
        """Store a general-purpose certificate for its evaluation id after verifying it (SWS_Dcm_01681..01683)."""
        try:
            reader = _Reader(bytes(payload[2:]))
            evaluation_id = int.from_bytes(reader.take(2, 'certificate evaluation id'), 'big')
            raw = reader.prefixed('certificate')
        except UdsAuthenticationError:
            return self._negative(0x13)
        if reader.position != len(reader.data) or not raw:
            return self._negative(0x13)
        if evaluation_id not in self.certificate_evaluation_ids:
            return self._negative(0x31)  # requestOutOfRange: no such certificate evaluation id (SWS_Dcm_01682)
        try:
            certificate = load_certificate(raw)
        except UdsAuthenticationError:
            return self._negative(INVALID_FORMAT)
        problem = certificate_problem(certificate, self.trusted, crls=self.crls)
        if problem is not None:
            return self._negative(problem)
        self.received_certificates[evaluation_id] = certificate
        return self._positive(TRANSMIT_CERTIFICATE, CERTIFICATE_VERIFIED)

    def _request_challenge(self, payload: bytes) -> bytes:
        self.deauthenticate()
        if len(payload) != 2 + 1 + 16:
            return self._negative(0x13)
        if not self._accepts_configuration(payload[2]):
            return self._negative(0x31)  # Only communicationConfiguration 0 (no session key) without an issuer
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
        info = self._session_key_info(
            'challenge-response', challenge, client_challenge, algorithm_indicator=_algorithm_indicator(acr.indicator)
        )
        return self._positive(task, AUTHENTICATION_COMPLETE, *parameters, _length_prefixed(info))

    def _verify_certificate(self, task: int, payload: bytes) -> bytes:
        self.deauthenticate()
        if len(payload) < 3:
            return self._negative(0x13)
        if not self._accepts_configuration(payload[2]):
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
        self._client_challenge = client_challenge
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
            proof, ephemeral = reader.prefixed('proof'), reader.prefixed('ephemeral public key')
        except UdsAuthenticationError:
            return self._negative(0x13)
        challenge, self._challenge = self._challenge, None  # One proof per challenge
        if not verify(self.client_certificate.public_key(), proof, challenge, self.scheme):
            return self._negative(OWNERSHIP_VERIFICATION_FAILED)
        if self.access_oids:  # Role and white lists from the certificate (SWS_Dcm_01514..01516)
            try:
                self.access = access_rights(self.client_certificate, self.access_oids, self.role_size)
            except UdsAuthenticationError:
                self.client_certificate = None
                return self._negative(INVALID_CONTENT)
        self.authenticated = True
        info = self._session_key_info(
            'certificate',
            challenge,
            getattr(self, '_client_challenge', b''),
            client_certificate=self.client_certificate,
            server_certificate=self.certificate,
            client_ephemeral_public_key=ephemeral,
        )
        return self._positive(PROOF_OF_OWNERSHIP, AUTHENTICATION_COMPLETE, _length_prefixed(info))


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


def certificate_to_transmit(store, source: str) -> x509.Certificate:
    """The certificate for transmitCertificate: of a key store entry (a client certificate, or the first of a
    trusted-certificates entry; readable while the store is locked) or of a PEM/DER file."""
    from pathlib import Path

    from can_explorer.core import keystore

    info = store.entries_by_name().get(source)
    if info is not None:
        if info.purpose not in (keystore.TLS_CLIENT, keystore.TLS_CA):
            raise UdsAuthenticationError(f'The Security Manager entry {source!r} holds no certificate')
        return certificates_from_pem(info.params['certificate'])[0]
    path = Path(source)
    if not path.is_file():
        raise UdsAuthenticationError(f'No certificate entry or file named {source!r}')
    return load_certificate(path.read_bytes())


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
