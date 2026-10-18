# Reference

- [Scripting API](scripting-api.md): the Python API of simulation scripts.
- [Test API](test-api.md): the Python API of test scripts.
- [QML panel API](panel-api.md): the `panel` object of QML panels.
- [Supported interfaces](interfaces.md): every CAN interface with its hardware, platforms, driver and CAN FD support.
- [Command line](command-line.md): options and settings of `canexplorer`, `can-test`, `can-server` and the
  `can-db-convert` converter.
- [File formats](file-formats.md): supported log files and databases and their details.
- [Keyboard shortcuts](keyboard-shortcuts.md)
- [Links](links.md): related documentation and specifications.

## Stability and versioning

CANExplorer follows [semantic versioning](https://semver.org/) from version 1.0.0 on. Within a major version (1.x),
the following stays compatible: scripts, files and commands written for 1.0 keep working with every later 1.x
release. New features may be added; changes that break them only come with 2.0, after being deprecated (with a
warning) in a 1.x release.

| Public interface | Covered |
|---|---|
| Simulation scripts | The names exported by `can_explorer.scripting` (its `__all__`), as documented in the [scripting API](scripting-api.md) |
| Test scripts | The names exported by `can_explorer.testing` (its `__all__`), as documented in the [test API](test-api.md) |
| QML panels | The `panel` object, as documented in the [panel API](panel-api.md) |
| Command line | The commands `canexplorer`, `can-test`, `can-server` and `can-db-convert` with their documented options and exit codes |
| Files | Workspaces (`.cxw`): files of 1.0 open in every 1.x version (they carry a format version; a newer file is refused with a message rather than misread). Test reports in JUnit XML |
| Network | The `can-server` protocol: clients and servers of the same major version work together |
| python-can | The interface name `canexplorer_remote` (class `RemoteBus`) and its channel syntax |

Everything else is internal and may change in any release, in particular the other modules of `can_explorer`
(`can_explorer.core`, `can_explorer.gui`, …) even where they are importable, the layout of the HTML test report,
the application's settings storage and the user interface itself.
