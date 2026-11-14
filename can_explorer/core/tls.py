"""TLS contexts for DoIP (ISO 13400-2:2019) from the key store: trusted certificates and client certificates.

- **Verifying the entity.** With trusted certificates (a `tls-ca` entry) the entity's certificate chain is verified
  against them. Its host name is not checked by default: DoIP entities are reached by IP address and their
  certificates rarely name it; `check_hostname=True` checks it. Without trusted certificates the entity is not
  verified (encryption only), as before.
- **Mutual TLS.** A `tls-client` entry presents a client certificate. Python's `ssl` loads a private key only from a
  file, so it is written to a temporary file readable by the owner only and deleted as soon as it is loaded.
- **Key log.** `keylog` (by default the file named by the `SSLKEYLOGFILE` environment variable, as in browsers)
  receives the session secrets in the NSS key log format, so a capture of the session can be decrypted, e.g. in
  Wireshark. The file lets anyone holding it read the sessions.
"""

import os
import ssl
import tempfile
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from can_explorer.core import audit, keystore
from can_explorer.core.keystore import KeyStore, KeyStoreError


def client_context(
    store: Optional[KeyStore] = None,
    trusted: str = '',
    client: str = '',
    check_hostname: bool = False,
    keylog: Optional[str | Path] = None,
    minimum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
) -> ssl.SSLContext:
    """A client SSLContext. `trusted` and `client` name key store entries ('' for none); `keylog` '' for none."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = minimum_version
    if trusted:
        info = _entry(store, trusted, keystore.TLS_CA)
        context.load_verify_locations(cadata=info.params['certificate'])
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = check_hostname
        _load_revocation_lists(context, store, info)
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if client:
        info = _entry(store, client, keystore.TLS_CLIENT)
        if store is None or not store.unlocked:
            raise KeyStoreError('The key store is locked; unlock it to use the client certificate')
        private_key = store.secrets(client)['private_key']
        audit.record('tls client certificate used', entry=client)
        _load_client_certificate(context, info.params['certificate'].encode('ascii'), private_key)
    if keylog is None:
        keylog = os.environ.get('SSLKEYLOGFILE', '')
    if keylog:
        context.keylog_filename = str(keylog)
    return context


def _load_revocation_lists(context: ssl.SSLContext, store: KeyStore, trusted: keystore.KeyInfo) -> None:
    """Check the entity's certificate against the store's revocation lists of the trusted issuers. OpenSSL then
    needs a list for the issuer of the entity's certificate, so this applies only when there is one."""
    issuers = {
        certificate.subject for certificate in x509.load_pem_x509_certificates(trusted.params['certificate'].encode())
    }
    crls = [crl for crl in store.revocation_lists() if crl.issuer in issuers]
    if not crls:
        return
    pem = b''.join(crl.public_bytes(serialization.Encoding.PEM) for crl in crls)
    with tempfile.TemporaryDirectory(prefix='canexplorer-crl-') as directory:
        path = Path(directory) / 'crls.pem'
        path.write_bytes(pem)  # Revocation lists are public
        context.load_verify_locations(cafile=str(path))
    context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    audit.record('tls revocation lists used', issuers=[crl.issuer.rfc4514_string() for crl in crls])


def _entry(store: Optional[KeyStore], name: str, purpose: str) -> keystore.KeyInfo:
    if store is None:
        raise KeyStoreError('No key store')
    info = store.entries_by_name().get(name)
    if info is None:
        raise KeyStoreError(f'No key store entry named {name!r}')
    if info.purpose != purpose:
        raise KeyStoreError(f'The key store entry {name!r} is a {info.purpose}, not a {purpose}')
    return info


def _load_client_certificate(context: ssl.SSLContext, certificate: bytes, private_key: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix='canexplorer-tls-') as directory:
        certificate_file = Path(directory) / 'certificate.pem'
        key_file = Path(directory) / 'key.pem'
        certificate_file.write_bytes(certificate)
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as file:
            file.write(private_key)
        try:
            context.load_cert_chain(certificate_file, key_file)
        except ssl.SSLError as e:
            raise KeyStoreError(f'Cannot use the client certificate: {e}') from e
