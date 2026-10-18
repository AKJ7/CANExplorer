"""Build a standalone CANExplorer bundle for the current platform.

Usage: uv run python packaging/build.py [--archive]

The bundle is written to dist/CANExplorer (dist/CANExplorer.app on macOS). With --archive it is also
zipped to dist/CANExplorer-<version>-<platform>-<arch>.zip.
"""

import argparse
import os
import platform
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
BUILD = ROOT / 'build'
NAME = 'CANExplorer'


def build() -> Path:
    PyInstaller.__main__.run(
        [
            str(ROOT / 'packaging' / 'launcher.py'),
            '--name',
            NAME,
            '--windowed',
            '--noconfirm',
            '--clean',
            '--add-data',
            f'{ROOT / "can_explorer" / "gui" / "qt"}{os.pathsep}can_explorer/gui/qt',
            # The application version is read from the package metadata
            '--copy-metadata',
            'can-explorer',
            # python-can imports its interface backends by name at runtime
            '--collect-submodules',
            'can.interfaces',
            '--distpath',
            str(DIST),
            '--workpath',
            str(BUILD),
            '--specpath',
            str(BUILD),
        ]
    )
    bundle = DIST / (f'{NAME}.app' if sys.platform == 'darwin' else NAME)
    if not bundle.exists():
        raise SystemExit(f'Expected bundle not found: {bundle}')
    return bundle


def archive(bundle: Path) -> Path:
    system = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, sys.platform)
    base = DIST / f'{NAME}-{version("can-explorer")}-{system}-{platform.machine().lower()}'
    return Path(shutil.make_archive(str(base), 'zip', root_dir=bundle.parent, base_dir=bundle.name))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--archive', action='store_true', help='also create a zip archive of the bundle')
    args = parser.parse_args()
    bundle = build()
    print(f'Bundle: {bundle}')
    if args.archive:
        print(f'Archive: {archive(bundle)}')


if __name__ == '__main__':
    main()
