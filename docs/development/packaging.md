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

## Continuous integration

| Workflow | Runs | Does |
|---|---|---|
| `build.yaml` | Pushes and pull requests to `develop` | ruff and black; tests on Linux, Windows and macOS with Python 3.10 and 3.14 |
| `installer.yaml` | `v*` tags; manually | Builds the bundles for Linux, Windows and macOS (PyInstaller, or Nuitka when started manually) and attaches them to the GitHub release of the tag |
| `docs.yaml` | Pushes and pull requests to `develop` | Builds the documentation strictly; publishes it to GitHub Pages from `develop` |
