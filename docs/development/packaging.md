# Packaging

`packaging/build.py` builds a standalone bundle of the application for the current platform in `dist/`:

```bash
uv run python packaging/build.py --archive                   # PyInstaller (default)
uv run python packaging/build.py --backend nuitka --archive  # Compiled with Nuitka
uv run python packaging/build.py --backend nuitka --onefile  # Single executable (Linux, Windows)
```

| | PyInstaller | Nuitka |
|---|---|---|
| Method | Bundles the Python bytecode with an interpreter | Compiles the Python code to C and native code |
| Build time | About a minute | Tens of minutes (cached between builds) |
| Requirements | – | A C compiler: gcc/clang (Linux, with `patchelf`), Xcode command line tools (macOS), MSVC or a downloaded MinGW (Windows) |
| Output | `dist/CANExplorer/` (`.app` on macOS) | Same, or a single executable with `--onefile` |

`--archive` also zips the bundle to `dist/CANExplorer-<version>-<platform>-<arch>[-nuitka].zip`.

Both builds include the QML files, the Qt QML modules, the grammar files of ldfparser and lark, and all python-can
interface modules (which python-can imports by name at run time). Nuitka cannot relate the package metadata of an
editable install to the package, so the build writes the version into a generated module instead.

MF4 log files are only supported when asammdf is installed in the build environment. The release bundles are
built without it to keep them small (it adds numpy and pandas); to include it, sync with `--extra mf4` before
building.

## Installers

`packaging/installer.py` turns the bundle into the installers of the platform, in `dist/installers`:

```bash
uv run python packaging/build.py
uv run python packaging/installer.py                  # All formats of the platform
uv run python packaging/installer.py --formats deb    # Only some
```

| Platform | Format | Tool |
|---|---|---|
| Windows | `CANExplorer-<version>-windows-x86_64-setup.exe`: Inno Setup installer for the current user or all users, with start menu entry, optional desktop icon and uninstaller, in English or German | [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`choco install innosetup`) |
| Linux | `….AppImage`: runs on any distribution with glibc 2.35 or newer | `appimagetool`, downloaded to `build/` when not installed |
| Linux | `….deb`: installs to `/opt/canexplorer` with `/usr/bin/canexplorer`, a menu entry and icons | `dpkg-deb` |
| macOS | `….dmg`: the app and a link to *Applications* to drag it to | `hdiutil` |

The Windows installer keeps a fixed `AppId`, so a new version installs over the previous one. The installers are
not code signed; signing needs certificates (an Authenticode certificate for Windows, an Apple Developer ID with
notarization for macOS), which can be added to the release workflow as secrets.

## Icons

The application icon (`.ico` on Windows, `.icns` on macOS, `.png` on Linux) is set on the bundle and on the
window; on Linux, `--install-desktop-entry` installs a desktop menu entry with the icon (see
[Installation](../getting-started/installation.md#desktop-menu-entry-linux)). The icons, the logo in the
application and the logo and favicon of this documentation are generated from the logo artwork
`docs/assets/logo.png`, drawn on black: the tool first makes the background transparent
(`docs/assets/logo-transparent.png`), keeping the glow as a soft edge. After changing the logo, run

```bash
QT_QPA_PLATFORM=offscreen uv run python tools/make_icons.py
```

It writes `packaging/icons/`, `can_explorer/gui/qt/CanExplorer/images/logo.png`, the desktop entry icons in
`can_explorer/resources/icons/` and `docs/assets/logo-transparent.png`, `icon.png`, `favicon.png` and
`logo-banner.png`.

## Continuous integration

| Workflow | Runs | Does |
|---|---|---|
| `build.yaml` | Pushes and pull requests to `develop` | ruff and black; tests on Linux, Windows and macOS with Python 3.10 and 3.14 |
| `release.yaml` | `v*` tags; manually; pull requests changing the packaging | Builds the bundles and installers for Windows, Linux and macOS (PyInstaller, or Nuitka when started manually), installs each one to check that it starts, and stores them as workflow artifacts for 90 days. For tags it also publishes a GitHub release with the installers, the zip archives and `SHA256SUMS.txt` (a pre-release for tags such as `v1.0.0-rc1`) |
| `docs.yaml` | Pushes and pull requests to `develop` | Builds the documentation strictly; publishes it to GitHub Pages from `develop` |

### Releasing a version

1. Set the version in `pyproject.toml` (`uv version 1.2.0`), move the *Unreleased* entries of `CHANGELOG.md` under a
   heading for it and commit.
2. Tag the commit and push the tag: `git tag v1.2.0 && git push origin v1.2.0`.
3. The release workflow checks that the tag matches the version, builds and tests the installers on the three
   platforms and publishes the release with them.

Manual runs (*Actions → release → Run workflow*) build the installers of any branch without a release; download
them from the run's artifacts.
