"""Test certificates for benches and simulators: a certificate authority with a tester (client) and an ECU (server)
certificate signed by it, so mutual TLS (DoIP) and UDS Authentication (0x29) can be tried without an OEM PKI.

`create_test_pki` adds them to the key store: the CA as trusted certificates, the tester and the ECU certificate
with their private keys as client certificates. With `directory`, it also writes them as PEM files (for a DoIP
entity or another tool); the private key files are readable by their owner only. These certificates are for
testing: their keys are generated on this computer and the CA key is not kept.
"""

import datetime
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

KEY_TYPES = ('ec-p256', 'ec-p384', 'ed25519', 'rsa-2048', 'rsa-3072')


class TestPkiError(ValueError):
    __test__ = False  # Not a pytest test class


def generate_key(key_type: str):
    if key_type == 'ec-p256':
        return ec.generate_private_key(ec.SECP256R1())
    if key_type == 'ec-p384':
        return ec.generate_private_key(ec.SECP384R1())
    if key_type == 'ed25519':
        return ed25519.Ed25519PrivateKey.generate()
    if key_type in ('rsa-2048', 'rsa-3072'):
        return rsa.generate_private_key(65537, int(key_type[4:]))
    raise TestPkiError(f'Unknown key type {key_type!r}; one of {", ".join(KEY_TYPES)}')


def _name(common_name: str, organisation: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organisation),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _sign_algorithm(key):
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def issue(
    subject: x509.Name,
    key,
    issuer: Optional[x509.Certificate],
    issuer_key,
    days: int,
    ca: bool = False,
    usages: Sequence = (),
    addresses: Sequence[str] = (),
) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
        )
    else:
        builder = builder.add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True
        )
    if usages:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(usages)), critical=False)
    if addresses:
        names = []
        for address in addresses:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(address)))
            except ValueError:
                names.append(x509.DNSName(address))
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    if issuer is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
        )
    return builder.sign(issuer_key, _sign_algorithm(issuer_key))


@dataclass
class TestPki:
    __test__ = False  # Not a pytest test class

    ca: x509.Certificate
    tester: x509.Certificate
    tester_key: object
    ecu: x509.Certificate
    ecu_key: object
    entries: Dict[str, str]  # Role ('ca', 'tester', 'ecu') -> key store entry name
    files: Dict[str, Path]  # Written PEM files, by name


def _pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as file:
        file.write(data)


def create_test_pki(
    store,
    prefix: str = 'Test',
    key_type: str = 'ec-p256',
    days: int = 365,
    ecu_addresses: Sequence[str] = ('127.0.0.1',),
    directory: Optional[Path] = None,
    replace: bool = False,
) -> TestPki:
    """Create a test CA, tester and ECU certificate and add them to `store` (unlocked) as '<prefix> CA',
    '<prefix> tester' and '<prefix> ECU'. `ecu_addresses` go into the ECU certificate's subject alternative names
    (for TLS host name checks)."""
    prefix = prefix.strip()
    if not prefix:
        raise TestPkiError('The certificates need a name prefix')
    if not 1 <= days <= 36500:
        raise TestPkiError('The validity is 1 to 36500 days')
    names = {'ca': f'{prefix} CA', 'tester': f'{prefix} tester', 'ecu': f'{prefix} ECU'}
    existing = set(store.names())
    clashes = [name for name in names.values() if name in existing]
    if clashes and not replace:
        raise TestPkiError(f'The key store has an entry named {clashes[0]!r} already')
    organisation = f'{prefix} (CANExplorer test PKI)'
    ca_key = generate_key(key_type)
    ca = issue(_name(f'{prefix} CA', organisation), ca_key, None, ca_key, days, ca=True)
    tester_key, ecu_key = generate_key(key_type), generate_key(key_type)
    tester = issue(
        _name(f'{prefix} tester', organisation), tester_key, ca, ca_key, days, usages=[ExtendedKeyUsageOID.CLIENT_AUTH]
    )
    ecu = issue(
        _name(f'{prefix} ECU', organisation),
        ecu_key,
        ca,
        ca_key,
        days,
        usages=[ExtendedKeyUsageOID.SERVER_AUTH],
        addresses=ecu_addresses,
    )
    store.add_tls_ca(names['ca'], _pem(ca), replace=True)
    store.add_tls_client(names['tester'], _pem(tester), _key_pem(tester_key), replace=True)
    store.add_tls_client(names['ecu'], _pem(ecu), _key_pem(ecu_key), replace=True)
    files: Dict[str, Path] = {}
    if directory is not None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stem = ''.join(character if character.isalnum() else '_' for character in prefix).lower()
        for name, data, private in (
            (f'{stem}_ca.pem', _pem(ca), False),
            (f'{stem}_tester.pem', _pem(tester), False),
            (f'{stem}_tester.key', _key_pem(tester_key), True),
            (f'{stem}_ecu.pem', _pem(ecu), False),
            (f'{stem}_ecu.key', _key_pem(ecu_key), True),
        ):
            path = directory / name
            if private:
                _write_private(path, data)
            else:
                path.write_bytes(data)
            files[name] = path
    return TestPki(ca, tester, tester_key, ecu, ecu_key, names, files)
