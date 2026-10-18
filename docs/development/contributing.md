# Contributing

- Work on a branch and open a pull request against `develop`.
- Keep the core (`can_explorer/core`, `can_explorer/scripting`) free of Qt; Qt specific code belongs in
  `can_explorer/gui`, the user interface in QML.
- Add tests for new behaviour. Use the `virtual` interface and the simulators in `can_explorer/core/simulation.py`
  instead of hardware, and prefer asserting decoded results over implementation details.
- Run `uv run pytest`, `uv run ruff check .` and `uv run black --check .` before pushing; CI runs them on all
  platforms.
- Update the documentation in `docs/` with user-visible changes, and `CHANGELOG.md` under *Unreleased*.
- Code style: black with a line length of 120 and single quotes, type hints for public functions, docstrings for
  public classes and functions.
