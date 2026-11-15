"""PKCS#11 tokens — smart cards, USB tokens and hardware security modules — as security profiles
(core/security_profiles.py): the tester certificate is read from the token, and proofs of ownership are signed on
it, so the private key never leaves the token.

Used for UDS Authentication: by certificate exchange (`certificate` and `sign`) and by challenge-response with a
signature (`sign`). The token is reached through its vendor's PKCS#11 module (a shared library), with the
python-pkcs11 package (the optional `pkcs11` extra). A session is opened for each operation and closed after it, so
a removed and re-inserted token keeps working.

The mechanisms are those of the PKCS#11 specification (v2.40, OASIS): CKM_ECDSA_SHA256 (whose signature is r ‖ s,
§2.3.1; DER-encoded here for the 'ecdsa-sha256' scheme), CKM_SHA256_RSA_PKCS, CKM_SHA256_RSA_PKCS_PSS with SHA-256,
MGF1-SHA-256 and a 32-byte salt (as `uds_auth.sign`), and CKM_EDDSA for Ed25519 (PKCS#11 v3.0).
"""

from typing import Any, Callable, Optional

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from can_explorer.core import audit
from can_explorer.core.security_profiles import SecurityProfileError


def _library(module: str):
    try:
        import pkcs11
    except ImportError as e:
        raise SecurityProfileError(
            'PKCS#11 tokens need the python-pkcs11 package (from source: uv sync --extra pkcs11)'
        ) from e
    try:
        return pkcs11.lib(module)
    except Exception as e:  # noqa: BLE001 - a missing or broken module, in whatever form python-pkcs11 reports it
        raise SecurityProfileError(f'Cannot load the PKCS#11 module {module}: {e}') from e


class Pkcs11Profile:
    """A key on a PKCS#11 token: its certificate and signatures. `library` opens the module (for tests)."""

    description = 'Certificate and signatures from a PKCS#11 token'

    def __init__(
        self,
        name: str,
        module: str,
        token: str,
        key: str,
        pin: str = '',
        library: Optional[Callable[[str], Any]] = None,
    ):
        self.name = name
        self.module, self.token_label, self.key_label = module, token, key
        self._pin = pin
        self._open_library = library or _library
        self._lib = None
        self._certificate: Optional[x509.Certificate] = None

    def __repr__(self) -> str:  # Never show the PIN
        return f'Pkcs11Profile({self.name!r}, token={self.token_label!r}, key={self.key_label!r})'

    def _session(self):
        import pkcs11

        if self._lib is None:
            self._lib = self._open_library(self.module)
        try:
            token = self._lib.get_token(token_label=self.token_label)
        except pkcs11.exceptions.NoSuchToken as e:
            raise SecurityProfileError(f'No PKCS#11 token labelled {self.token_label!r} (inserted?)') from e
        try:
            return token.open(user_pin=self._pin or None)
        except pkcs11.exceptions.PinIncorrect as e:
            raise SecurityProfileError(f'Wrong PIN for the PKCS#11 token {self.token_label!r}') from e
        except pkcs11.exceptions.PinLocked as e:
            raise SecurityProfileError(f'The PIN of the PKCS#11 token {self.token_label!r} is locked') from e

    def certificate(self) -> Optional[x509.Certificate]:
        from pkcs11 import Attribute, ObjectClass

        if self._certificate is None:
            with self._session() as session:
                objects = list(
                    session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE, Attribute.LABEL: self.key_label})
                )
                if not objects:
                    return None
                self._certificate = x509.load_der_x509_certificate(bytes(objects[0][Attribute.VALUE]))
        return self._certificate

    def sign(self, data: bytes, scheme: str) -> bytes:
        import pkcs11
        from pkcs11 import MGF, Mechanism, ObjectClass

        mechanisms = {
            'ecdsa-sha256': (Mechanism.ECDSA_SHA256, None),
            'ecdsa-sha256-raw': (Mechanism.ECDSA_SHA256, None),
            'rsa-pkcs1-sha256': (Mechanism.SHA256_RSA_PKCS, None),
            'rsa-pss-sha256': (Mechanism.SHA256_RSA_PKCS_PSS, (Mechanism.SHA256, MGF.SHA256, 32)),
            'ed25519': (Mechanism.EDDSA, None),
        }
        if scheme not in mechanisms:
            raise SecurityProfileError(f'A PKCS#11 token cannot sign with the scheme {scheme!r}')
        mechanism, parameter = mechanisms[scheme]
        with self._session() as session:
            try:
                key = session.get_key(object_class=ObjectClass.PRIVATE_KEY, label=self.key_label)
            except (pkcs11.exceptions.NoSuchKey, pkcs11.exceptions.MultipleObjectsReturned) as e:
                raise SecurityProfileError(
                    f'No single private key labelled {self.key_label!r} on the token {self.token_label!r}'
                ) from e
            try:
                signature = bytes(key.sign(bytes(data), mechanism=mechanism, mechanism_param=parameter))
            except pkcs11.exceptions.PKCS11Error as e:
                raise SecurityProfileError(f'The PKCS#11 token could not sign ({scheme}): {e!r}') from e
        audit.record('proof of ownership signed on a pkcs11 token', profile=self.name, scheme=scheme)
        if scheme == 'ecdsa-sha256':  # r ‖ s to the DER encoding the scheme sends
            half = len(signature) // 2
            return encode_dss_signature(
                int.from_bytes(signature[:half], 'big'), int.from_bytes(signature[half:], 'big')
            )
        return signature
