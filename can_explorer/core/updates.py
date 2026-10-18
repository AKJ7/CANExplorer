"""Updates from the GitHub releases of CANExplorer: finding a newer release, downloading and verifying its installer
for the way this copy was installed, and installing it.

| Installation | Detected by | Update |
|---|---|---|
| Windows setup | Frozen build next to an Inno Setup uninstaller (`unins000.exe`) | Runs the new setup silently over it; the setup restarts the application |
| Linux AppImage | `$APPIMAGE` | Replaces the AppImage file and restarts |
| Linux package | Frozen build in `/opt/canexplorer` | `pkexec apt-get install` of the new `.deb` (asks for the administrator password) |
| macOS app | Frozen build inside `CANExplorer.app` | Replaces the app with the one of the new disk image and restarts |
| Zip archive, source | Anything else | Not installed automatically: the release page is opened |

Every download is checked against the `SHA256SUMS.txt` of the release before it is used.
"""

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPOSITORY = 'AKJ7/CANExplorer'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases'
CHECKSUMS = 'SHA256SUMS.txt'
TIMEOUT = 15.0
CHUNK = 256 * 1024

WINDOWS_SETUP, APPIMAGE, DEB, MACOS_APP, PORTABLE, SOURCE = (
    'windows-setup',
    'appimage',
    'deb',
    'macos-app',
    'portable',
    'source',
)
INSTALL_DESCRIPTIONS = {
    WINDOWS_SETUP: 'Windows installation',
    APPIMAGE: 'AppImage',
    DEB: 'Debian package',
    MACOS_APP: 'macOS app',
    PORTABLE: 'portable archive',
    SOURCE: 'source installation',
}


class UpdateError(Exception):
    pass


# Versions


_VERSION = re.compile(r'^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-.]?([a-z]+)\.?(\d*))?', re.IGNORECASE)
_PRE_ORDER = {'dev': 0, 'a': 1, 'alpha': 1, 'b': 2, 'beta': 2, 'rc': 3, 'c': 3, 'pre': 3}


def version_key(text: str) -> Tuple:
    """A sortable key: '1.2.0' > '1.2.0-rc2' > '1.2.0-rc1' > '1.1.9'. Unknown texts sort lowest."""
    match = _VERSION.match(text.strip())
    if match is None:
        return (-1,)
    major, minor, patch, pre, number = match.groups()
    release = (int(major), int(minor or 0), int(patch or 0))
    if pre is None:
        return (*release, 1, 0, 0)  # Final releases after their pre-releases
    return (*release, 0, _PRE_ORDER.get(pre.lower(), 0), int(number or 0))


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


# Releases


@dataclass
class Release:
    version: str
    tag: str
    name: str
    notes: str
    url: str  # The release page
    prerelease: bool
    published: str
    assets: Dict[str, str] = field(default_factory=dict)  # File name -> download URL


def _request(url: str, accept: str = 'application/vnd.github+json'):
    # Downloads only come from GitHub (release assets redirect to its object storage), except for a test server
    host = urllib.parse.urlparse(url).hostname or ''
    if not host.endswith(('github.com', 'githubusercontent.com')) and not os.environ.get('CANEXPLORER_UPDATE_URL'):
        raise UpdateError(f'Refusing to download from {host}')
    request = urllib.request.Request(url, headers={'Accept': accept, 'User-Agent': 'CANExplorer-updater'})
    return urllib.request.urlopen(request, timeout=TIMEOUT)  # noqa: S310 (only GitHub URLs)


def parse_releases(data: list) -> List[Release]:
    releases = []
    for item in data:
        if item.get('draft'):
            continue
        tag = item.get('tag_name', '')
        releases.append(
            Release(
                version=tag.removeprefix('v'),
                tag=tag,
                name=item.get('name') or tag,
                notes=item.get('body') or '',
                url=item.get('html_url', ''),
                prerelease=bool(item.get('prerelease')),
                published=(item.get('published_at') or '')[:10],
                assets={asset['name']: asset['browser_download_url'] for asset in item.get('assets', [])},
            )
        )
    return releases


def fetch_releases(url: Optional[str] = None) -> List[Release]:
    """The releases on GitHub, newest first (CANEXPLORER_UPDATE_URL replaces the API URL, e.g. for tests)."""
    url = url or os.environ.get('CANEXPLORER_UPDATE_URL') or API_URL
    try:
        with _request(url) as response:
            data = json.load(response)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f'Could not check for updates: {e}') from e
    if not isinstance(data, list):
        raise UpdateError('Unexpected answer from GitHub')
    return parse_releases(data)


def newest_release(releases: List[Release], current: str, include_prereleases: bool = False) -> Optional[Release]:
    """The newest release newer than `current`, or None."""
    candidates = [release for release in releases if include_prereleases or not release.prerelease]
    candidates = [release for release in candidates if is_newer(release.version, current)]
    return max(candidates, key=lambda release: version_key(release.version), default=None)


# Installation of this copy


def machine() -> str:
    return {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'arm64'}.get(
        platform.machine().lower(), platform.machine().lower()
    )


def frozen() -> bool:
    return bool(getattr(sys, 'frozen', False)) or '__compiled__' in globals()


def executable() -> Path:
    return Path(sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]).resolve()


def installation_kind(environ: Optional[Dict[str, str]] = None, platform_name: Optional[str] = None) -> str:
    environ = os.environ if environ is None else environ
    platform_name = platform_name or sys.platform
    if platform_name.startswith('linux') and environ.get('APPIMAGE'):
        return APPIMAGE
    if not frozen():
        return SOURCE
    path = executable()
    if platform_name == 'win32' and (path.parent / 'unins000.exe').exists():
        return WINDOWS_SETUP
    if platform_name == 'darwin' and app_bundle(path) is not None:
        return MACOS_APP
    if platform_name.startswith('linux') and path.as_posix().startswith('/opt/canexplorer/'):
        return DEB
    return PORTABLE


def app_bundle(path: Path) -> Optional[Path]:
    """The .app bundle containing an executable, if any."""
    for parent in path.parents:
        if parent.suffix == '.app':
            return parent
    return None


def asset_name(release: Release, kind: str) -> Optional[str]:
    """The release file for an installation kind on this machine."""
    arch = machine()
    patterns = {
        WINDOWS_SETUP: rf'-windows-{arch}-setup\.exe$',
        APPIMAGE: rf'-linux-{arch}\.AppImage$',
        DEB: rf'-linux-{arch}\.deb$',
        MACOS_APP: rf'-macos-{arch}\.dmg$',
    }
    pattern = patterns.get(kind)
    if pattern is None:
        return None
    return next((name for name in release.assets if re.search(pattern, name)), None)


# Downloading


def download(
    url: str,
    target: Path,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download to a file; progress(done, total) is called from the downloading thread (total 0 when unknown)."""
    partial = target.with_name(target.name + '.part')
    try:
        with _request(url, accept='application/octet-stream') as response, partial.open('wb') as file:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            while chunk := response.read(CHUNK):
                if cancelled():
                    raise UpdateError('Cancelled')
                file.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as e:
        partial.unlink(missing_ok=True)
        raise UpdateError(f'Download failed: {e}') from e
    partial.replace(target)
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, checksums: str) -> None:
    """Check a file against the lines '<sha256>  <name>' of SHA256SUMS.txt."""
    expected = None
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('*') == path.name:
            expected = parts[0].lower()
    if expected is None:
        raise UpdateError(f'{path.name} is not listed in {CHECKSUMS}')
    if sha256(path) != expected:
        raise UpdateError(f'{path.name} is damaged: its checksum does not match {CHECKSUMS}')


def download_update(
    release: Release,
    kind: str,
    directory: Path,
    progress: Optional[Callable[[int, int], None]] = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download and verify the installer of a release for this installation."""
    name = asset_name(release, kind)
    if name is None:
        raise UpdateError(f'Release {release.version} has no update for this {INSTALL_DESCRIPTIONS.get(kind, kind)}')
    if CHECKSUMS not in release.assets:
        raise UpdateError(f'Release {release.version} has no {CHECKSUMS} to verify the download')
    directory.mkdir(parents=True, exist_ok=True)
    sums = download(release.assets[CHECKSUMS], directory / CHECKSUMS, cancelled=cancelled).read_text(encoding='utf-8')
    path = download(release.assets[name], directory / name, progress, cancelled)
    verify(path, sums)
    return path


# Installing


def _detach(command: List[str]) -> None:
    """Start a process that outlives this one."""
    options: Dict = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    subprocess.Popen(command, **options)  # noqa: S603 (our own installer and paths)


def install_windows(setup: Path) -> None:
    """Run the setup silently over this installation; it closes and restarts the application."""
    directory = executable().parent
    program_files = [os.environ.get(name, '') for name in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432')]
    for_all_users = any(base and str(directory).lower().startswith(base.lower()) for base in program_files)
    _detach(
        [
            str(setup),
            '/SILENT',
            '/SUPPRESSMSGBOXES',
            '/NORESTART',
            '/CLOSEAPPLICATIONS',
            '/ALLUSERS' if for_all_users else '/CURRENTUSER',
            f'/DIR={directory}',
            '/UPDATE=1',  # The setup starts the new version when done (see packaging/installer.py)
        ]
    )


def install_appimage(new: Path, environ: Optional[Dict[str, str]] = None) -> Path:
    """Replace the running AppImage (Linux keeps the running file open) and return its path."""
    current = Path((environ or os.environ)['APPIMAGE'])
    staged = current.with_name(current.name + '.update')
    shutil.copy2(new, staged)
    staged.chmod(0o755)
    staged.replace(current)
    return current


def _waiting_script(lines: List[str]) -> Path:
    """A shell script that waits for this process to end, then runs the lines (and deletes itself)."""
    script = Path(tempfile.mkdtemp(prefix='canexplorer-update-')) / 'update.sh'
    script.write_text(
        '#!/bin/sh\n'
        f'while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done\n'
        + '\n'.join(lines)
        + '\nrm -rf "$(dirname "$0")"\n'
    )
    script.chmod(0o755)
    return script


def install_macos(dmg: Path) -> None:
    """After the application quit: mount the disk image, replace the app with its copy and start it again."""
    bundle = app_bundle(executable())
    if bundle is None:
        raise UpdateError('This copy is not a macOS app')
    if not os.access(bundle.parent, os.W_OK):
        raise UpdateError(f'No permission to replace {bundle}; drag the new version to {bundle.parent} instead')
    mount = Path(tempfile.mkdtemp(prefix='canexplorer-dmg-'))
    q = shlex.quote
    script = _waiting_script(
        [
            f'hdiutil attach {q(str(dmg))} -mountpoint {q(str(mount))} -nobrowse -quiet || exit 1',
            f'rm -rf {q(str(bundle))}.old && mv {q(str(bundle))} {q(str(bundle))}.old',
            f'ditto {q(str(mount / bundle.name))} {q(str(bundle))} && rm -rf {q(str(bundle))}.old '
            f'|| mv {q(str(bundle))}.old {q(str(bundle))}',
            f'hdiutil detach {q(str(mount))} -quiet',
            f'open {q(str(bundle))}',
        ]
    )
    _detach(['/bin/sh', str(script)])


def install_deb(package: Path) -> None:
    """Install the package with administrator rights (pkexec asks for the password), then start the new version."""
    pkexec = shutil.which('pkexec')
    if pkexec is None:
        raise UpdateError(f'Install {package} with: sudo apt install {package}')
    q = shlex.quote
    script = _waiting_script([f'{q(pkexec)} apt-get install -y {q(str(package))} && exec /usr/bin/canexplorer'])
    _detach(['/bin/sh', str(script)])


def restart_command(kind: str, environ: Optional[Dict[str, str]] = None) -> Optional[List[str]]:
    """How to start the updated application, for the kinds that are not restarted by their installer."""
    if kind == APPIMAGE:
        return [(environ or os.environ)['APPIMAGE']]
    return None


def install(kind: str, path: Path) -> Optional[List[str]]:
    """Install a downloaded update. The application must quit afterwards; the returned command (if any) starts the
    new version and should be run just before quitting."""
    if kind == WINDOWS_SETUP:
        install_windows(path)
    elif kind == APPIMAGE:
        install_appimage(path)
    elif kind == MACOS_APP:
        install_macos(path)
    elif kind == DEB:
        install_deb(path)
    else:
        raise UpdateError(f'Updates cannot be installed automatically for a {INSTALL_DESCRIPTIONS.get(kind, kind)}')
    return restart_command(kind)


def start_detached(command: List[str]) -> None:
    _detach(command)
