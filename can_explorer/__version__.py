from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('can-explorer')
except PackageNotFoundError:
    try:
        # Written by packaging/build.py for builds that do not carry the package metadata (Nuitka)
        from can_explorer._build_version import VERSION as __version__
    except ImportError:  # Running from a source tree that was never installed
        __version__ = '0.0.0'
