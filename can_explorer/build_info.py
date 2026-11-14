"""Which build of CANExplorer is running: its version, commit, build date and packaging backend.

Bundles record the commit, the build date and the backend in `can_explorer/_build_version.py`, written by
packaging/build.py while it builds them. Run from a source tree, the commit is read from git (with a "-dirty" suffix
when tracked files have changes); a source tree without git, or an installed wheel, only has the version.
"""

import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from can_explorer.__version__ import __version__

ROOT = Path(__file__).resolve().parent.parent
GIT_TIMEOUT = 2.0


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str = ''  # Short commit hash
    date: str = ''  # When the bundle was built (UTC); empty when running from source
    backend: str = ''  # PyInstaller or Nuitka for a bundle; empty when running from source

    def details(self) -> str:
        """'commit 0c63aeb, built 2026-11-13 10:02 UTC with PyInstaller' or 'commit 0c63aeb-dirty, from source'."""
        parts = [f'commit {self.commit}'] if self.commit else []
        if self.backend:
            parts.append(f'built {self.date} with {self.backend}' if self.date else f'built with {self.backend}')
        else:
            parts.append('from source')
        return ', '.join(parts)

    def describe(self) -> str:
        return f'{self.version} ({self.details()})'


def source_commit(root: Path = ROOT, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The short commit of a git source tree, '-dirty' when tracked files changed; '' without git."""
    if not (root / '.git').exists():
        return ''

    def git(*arguments: str) -> str:
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0  # No console flash
        result = run(
            ['git', *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=True,
            creationflags=flags,
        )
        return result.stdout.strip()

    try:
        commit = git('rev-parse', '--short', 'HEAD')
        dirty = git('status', '--porcelain', '--untracked-files=no')
    except (OSError, subprocess.SubprocessError):
        return ''
    return f'{commit}-dirty' if commit and dirty else commit


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    try:
        from can_explorer import _build_version as built
    except ImportError:
        return BuildInfo(str(__version__), source_commit())
    return BuildInfo(
        str(__version__),
        getattr(built, 'COMMIT', ''),
        getattr(built, 'BUILD_DATE', ''),
        getattr(built, 'BACKEND', ''),
    )
