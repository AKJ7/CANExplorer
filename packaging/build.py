"""Build a standalone CANExplorer bundle for the current platform.

Usage: uv run python packaging/build.py [--backend pyinstaller|nuitka] [--onefile] [--archive] [--j2534-bridge PYTHON]

Backends:
- pyinstaller (default): bundles the Python bytecode with an interpreter. Fast to build.
- nuitka: compiles the Python code to C and then to native code. Slower to build (several minutes,
  needs a C compiler: gcc/clang on Linux, Xcode command line tools on macOS, MSVC or MinGW on Windows,
  which Nuitka offers to download), but starts faster and does not ship the Python sources.

The bundle is written to dist/CANExplorer (dist/CANExplorer.app on macOS). With --onefile (Nuitka only)
it is a single executable instead. With --archive it is also zipped to
dist/CANExplorer-<version>-<platform>-<arch>[-<backend>].zip.

With --j2534-bridge (Windows), the 32-bit J2534 bridge (can_explorer/core/j2534_bridge.py) is built with the given
32-bit Python, which needs PyInstaller, and added to the bundle as j2534-bridge32/j2534-bridge32.exe. It runs the
32-bit J2534 drivers that the 64-bit application cannot load.
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
BUILD = ROOT / 'build'
NAME = 'CANExplorer'
LAUNCHER = ROOT / 'packaging' / 'launcher.py'
ICONS = ROOT / 'packaging' / 'icons'  # Generated from docs/assets/logo.png by tools/make_icons.py
QML_DIR = ROOT / 'can_explorer' / 'gui' / 'qt'
RESOURCES_DIR = ROOT / 'can_explorer' / 'resources'  # Desktop entry icons
DISTRIBUTION = 'can-explorer'
VERSION_MODULE = ROOT / 'can_explorer' / '_build_version.py'
# Packages whose data files are read at runtime: the LDF grammar and the lark grammars it imports
DATA_PACKAGES = ('ldfparser', 'lark')
# python-can imports its interface backends by name at runtime, and pyserial its URL handlers (socket:// etc.), so
# they are not found by import analysis
DYNAMIC_PACKAGES = ('can.interfaces', 'serial.urlhandler')
# python-can backends that load their vendor driver when imported, so --collect-submodules skips them on a build machine
# without that driver; named explicitly, they are bundled and load the driver on the user's machine
DRIVER_MODULES = (
    'can.interfaces.etas',
    'can.interfaces.etas.boa',
    'can.interfaces.ixxat',
    'can.interfaces.usb2can',
)
# CANExplorer's own python-can interfaces, registered as entry points (pyproject.toml) and imported by name only
# The J2534 bridge, a 32-bit program for 32-bit J2534 drivers (standard library only)
J2534_BRIDGE = ROOT / 'can_explorer' / 'core' / 'j2534_bridge.py'
J2534_BRIDGE_NAME = 'j2534-bridge32'
INTERFACE_MODULES = (
    'can_explorer.core.remote',
    'can_explorer.core.gvret',
    'can_explorer.core.elm327',
    'can_explorer.core.j2534',
)


def _clean(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def build_pyinstaller() -> Path:
    import PyInstaller.__main__

    arguments = [
        str(LAUNCHER),
        '--name',
        NAME,
        '--windowed',
        '--noconfirm',
        '--icon',
        str(ICONS / ('canexplorer.icns' if sys.platform == 'darwin' else 'canexplorer.ico')),
        '--clean',
        '--add-data',
        f'{QML_DIR}{os.pathsep}can_explorer/gui/qt',
        '--add-data',
        f'{RESOURCES_DIR}{os.pathsep}can_explorer/resources',
        # The application version is read from the package metadata
        '--copy-metadata',
        DISTRIBUTION,
        '--distpath',
        str(DIST),
        '--workpath',
        str(BUILD / 'pyinstaller'),
        '--specpath',
        str(BUILD / 'pyinstaller'),
    ]
    for package in DYNAMIC_PACKAGES:
        arguments += ['--collect-submodules', package]
    for module in INTERFACE_MODULES + DRIVER_MODULES:
        arguments += ['--hidden-import', module]
    for package in DATA_PACKAGES:
        arguments += ['--collect-data', package]
    PyInstaller.__main__.run(arguments)
    bundle = DIST / (f'{NAME}.app' if sys.platform == 'darwin' else NAME)
    if not bundle.exists():
        raise SystemExit(f'Expected bundle not found: {bundle}')
    return bundle


def build_j2534_bridge(python: str, bundle: Path) -> Path:
    """Build the J2534 bridge with a 32-bit Python and copy it into the bundle."""
    bits = subprocess.run(
        [python, '-c', 'import struct; print(struct.calcsize("P") * 8)'], check=True, capture_output=True, text=True
    ).stdout.strip()
    if bits != '32':
        raise SystemExit(f'The J2534 bridge needs a 32-bit Python; {python} is {bits}-bit')
    work = BUILD / 'j2534-bridge'
    subprocess.run(
        [
            python,
            '-m',
            'PyInstaller',
            str(J2534_BRIDGE),
            '--name',
            J2534_BRIDGE_NAME,
            '--onedir',
            '--console',  # Talks over stdin and stdout; CANExplorer starts it without a window
            # The bridge imports j2534_driver from its own folder, which import analysis does not follow
            '--paths',
            str(J2534_BRIDGE.parent),
            '--hidden-import',
            'j2534_driver',
            '--noconfirm',
            '--clean',
            '--distpath',
            str(work / 'dist'),
            '--workpath',
            str(work / 'build'),
            '--specpath',
            str(work),
        ],
        check=True,
        cwd=ROOT,
    )
    if bundle.suffix == '.exe':  # Nuitka onefile: the bridge goes next to the executable
        target = bundle.parent / J2534_BRIDGE_NAME
    else:
        target = bundle / J2534_BRIDGE_NAME
    _clean(target)
    shutil.copytree(work / 'dist' / J2534_BRIDGE_NAME, target)
    return target


def _numeric_version() -> str:
    """Windows version resources need a purely numeric version such as 1.2.3.0."""
    numbers = [int(part) for part in re.findall(r'\d+', version(DISTRIBUTION))[:4]]
    return '.'.join(str(number) for number in (numbers + [0, 0, 0, 0])[:4])


def build_nuitka(onefile: bool) -> Path:
    output = BUILD / 'nuitka'
    if sys.platform == 'darwin':
        mode = 'app'  # An .app bundle (onefile is not used for macOS GUI applications)
    else:
        mode = 'onefile' if onefile else 'standalone'
    arguments = [
        sys.executable,
        '-m',
        'nuitka',
        str(LAUNCHER),
        f'--mode={mode}',
        f'--output-dir={output}',
        f'--output-filename={NAME}',
        f'--output-folder-name={NAME}',
        '--enable-plugin=pyqt6',
        # QML modules (QtQuick, Controls, Dialogs, Layouts) on top of the default Qt plugins
        '--include-qt-plugins=qml',
        f'--include-data-dir={QML_DIR}=can_explorer/gui/qt',
        f'--include-data-dir={RESOURCES_DIR}=can_explorer/resources',
        '--include-module=can_explorer._build_version',
        *(f'--include-package={package}' for package in DYNAMIC_PACKAGES),
        *(f'--include-module={module}' for module in INTERFACE_MODULES),
        # Nuitka's own package configuration already includes the lark grammars
        *(f'--include-package-data={package}' for package in DATA_PACKAGES if package != 'lark'),
        '--assume-yes-for-downloads',
        f'--report={output / "compilation-report.xml"}',
        f'--product-name={NAME}',
        f'--product-version={_numeric_version()}',
        f'--file-version={_numeric_version()}',
        '--company-name=AKJ7',
    ]
    if sys.platform == 'win32':
        arguments += ['--windows-console-mode=disable', f'--windows-icon-from-ico={ICONS / "canexplorer.ico"}']
    elif sys.platform == 'darwin':
        arguments += [f'--macos-app-name={NAME}', f'--macos-app-icon={ICONS / "canexplorer.icns"}']
    else:
        arguments.append(f'--linux-icon={ICONS / "canexplorer.png"}')
    # Nuitka cannot relate the package metadata of an editable install (uv sync) to the package, so the
    # version is compiled in instead (see can_explorer/__version__.py)
    VERSION_MODULE.write_text(f'VERSION = {version(DISTRIBUTION)!r}\n')
    try:
        subprocess.run(arguments, check=True, cwd=ROOT)
    finally:
        VERSION_MODULE.unlink(missing_ok=True)

    if sys.platform == 'darwin':
        built, bundle = output / f'{NAME}.app', DIST / f'{NAME}.app'
    elif onefile:
        executable = NAME + ('.exe' if sys.platform == 'win32' else '')
        built, bundle = output / executable, DIST / executable
    else:
        built, bundle = output / NAME, DIST / NAME
        if not built.exists():  # Older Nuitka versions keep the '.dist' suffix
            built = output / f'{NAME}.dist'
    if not built.exists():
        raise SystemExit(f'Expected Nuitka output not found: {built}')
    DIST.mkdir(exist_ok=True)
    _clean(bundle)
    shutil.move(str(built), str(bundle))
    return bundle


def archive(bundle: Path, backend: str) -> Path:
    system = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, sys.platform)
    suffix = '' if backend == 'pyinstaller' else f'-{backend}'
    base = DIST / f'{NAME}-{version(DISTRIBUTION)}-{system}-{platform.machine().lower()}{suffix}'
    return Path(shutil.make_archive(str(base), 'zip', root_dir=bundle.parent, base_dir=bundle.name))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', choices=['pyinstaller', 'nuitka'], default='pyinstaller')
    parser.add_argument('--onefile', action='store_true', help='Nuitka: build a single executable (not on macOS)')
    parser.add_argument('--archive', action='store_true', help='also create a zip archive of the bundle')
    parser.add_argument(
        '--j2534-bridge', metavar='PYTHON', help='Windows: build the J2534 bridge with this 32-bit Python'
    )
    args = parser.parse_args()
    if args.onefile and args.backend != 'nuitka':
        parser.error('--onefile is only supported with --backend nuitka')
    bundle = build_nuitka(args.onefile) if args.backend == 'nuitka' else build_pyinstaller()
    print(f'Bundle: {bundle}')
    if args.j2534_bridge:
        print(f'J2534 bridge: {build_j2534_bridge(args.j2534_bridge, bundle)}')
    if args.archive:
        print(f'Archive: {archive(bundle, args.backend)}')


if __name__ == '__main__':
    main()
