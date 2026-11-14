"""Remembering the key store password in the operating system's keychain (Windows Credential Manager, macOS
Keychain, the Secret Service on Linux) through the `keyring` package, so the key store unlocks when CANExplorer
starts. Off unless asked for; where no keychain is available (e.g. a Linux session without a Secret Service),
`available()` is False and nothing is stored.
"""

from pathlib import Path
from typing import Optional

SERVICE = 'CANExplorer key store'


class KeychainError(Exception):
    pass


def _keyring():
    import keyring

    return keyring


def available() -> bool:
    """Whether a usable keychain backend exists (the fail/null backends do not count)."""
    try:
        backend = _keyring().get_keyring()
    except Exception:  # noqa: BLE001 - a broken backend setup means no keychain
        return False
    module = type(backend).__module__
    return (
        not module.startswith(('keyring.backends.fail', 'keyring.backends.null'))
        and getattr(backend, 'priority', 1) > 0
    )


def _account(path: Path) -> str:
    return str(Path(path).resolve())


def remember(path: Path, password: str) -> None:
    try:
        _keyring().set_password(SERVICE, _account(path), password)
    except Exception as e:  # noqa: BLE001 - keyring raises backend-specific errors
        raise KeychainError(f'Cannot store the password in the keychain: {e}') from e


def recall(path: Path) -> Optional[str]:
    """The remembered password of the key store at `path`, or None."""
    if not available():
        return None
    try:
        return _keyring().get_password(SERVICE, _account(path))
    except Exception:  # noqa: BLE001 - a locked or refusing keychain: as if nothing were remembered
        return None


def forget(path: Path) -> None:
    try:
        _keyring().delete_password(SERVICE, _account(path))
    except Exception:  # noqa: BLE001 - nothing remembered, or the keychain refuses: nothing to forget
        pass
