# Building and testing

## Set-up

```bash
git clone https://github.com/AKJ7/CANExplorer.git
cd CANExplorer
uv sync            # Runtime, dev, test and packaging dependencies in .venv
uv run canexplorer        # Start the application
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. The dependency groups are `dev` (linters),
`test`, `packaging` (PyInstaller, Nuitka) and `docs` (MkDocs).

## Docker

`docker-compose.yml` runs the project in a container with Python, the Qt libraries for headless tests and the
Linux CAN tools. The source is mounted; the virtual environment lives in a volume of its own.

```bash
docker compose run --rm test        # The test suite
docker compose run --rm dev         # A shell, e.g. for `uv sync && uv run pytest` or virtual CAN buses:
                                    #   sudo ip link add vcan0 type vcan && sudo ip link set vcan0 up
CAN_SERVER_TOKEN=secret docker compose up -d can-server   # Share the host's can0, see Remote buses
```

The `can-server` service uses the host's network, so it sees the host's CAN interfaces and listens on port 8765;
`CAN_SERVER_BUS` (default `vehicle=socketcan:can0:500000`) and `CAN_SERVER_PORT` change the bus and the port, and
it does not start without `CAN_SERVER_TOKEN`. Set `UID` to your user id (`id -u`) when it is not 1000, so files
created in the source belong to you.

## Tests

```bash
uv run pytest
```

The tests run headless (Qt's `offscreen` platform) and use python-can's `virtual` interface, simulated ECUs and LIN
slaves, so no hardware is needed. They cover the core, the decoders and file formats, the scripting runtime, the Qt
models and controllers, and loading the QML user interface without warnings.

To test other Python versions without changing `.venv`:

```bash
UV_PROJECT_ENVIRONMENT=.venv-3.10 uv run --python 3.10 --no-default-groups --group test pytest
```

## Linting and formatting

```bash
uv run ruff check .
uv run black .
```

CI fails on lint errors and on code that black would reformat.

## Benchmark

```bash
QT_QPA_PLATFORM=offscreen uv run python tools/benchmark.py
```

Measures the receive pipeline (frames per second, with and without decoding) and the table models. The frames are
handed straight to the session, so this is the processing speed alone.

### End to end on SocketCAN

`tools/benchmark_socketcan.py` measures the whole path through the Linux SocketCAN stack, like a real adapter, on
virtual `vcan` interfaces. It needs Linux and can-utils (`cangen`, `candump`), and a `vcan` interface, created once
with root:

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0

uv run python tools/benchmark_socketcan.py                     # both benchmarks on vcan0
uv run python tools/benchmark_socketcan.py --only receive --decoder j1939 --dbc tests/data/example.dbc --extended
uv run python tools/benchmark_socketcan.py --channels vcan0,vcan1 --json results.json
```

- **Receive**: `cangen`, a separate process, sends at increasing rates (1 000 to 200 000 frames/s, then as fast as
  possible). For each rate the tool reports the frames the session lost, the CPU it used, and the longest garbage
  collection pause. A pause longer than the kernel's socket buffer can bridge (a few hundred frames) loses frames.
- **Transmit timing**: a 10 ms periodic frame with software timing and with the kernel's broadcast manager
  (`hardware=True`), on an idle bus and while receiving 5 000 frames/s. `candump` timestamps the frames in the
  kernel, independently of CANExplorer, and the tool reports the period, jitter, 99th percentile error, late frames
  and drift.

`vcan` has no bitrate and no arbitration, so the results are CANExplorer's software limits, not an adapter's. A
fully loaded 1 Mbit/s classic CAN bus carries about 8 000 to 9 000 frames/s.

#### Results

One run on a laptop (Intel Core Ultra 5 226V, Linux 7.0, Python 3.12), one channel, no decoder, 3 s per step:

| Frames/s | Lost   | CPU   | Longest GC |
|---------:|-------:|------:|-----------:|
| 1 000    | 0 %    | 14 %  | 0.9 ms     |
| 5 000    | 0 %    | 37 %  | 1.4 ms     |
| 10 000   | 0 %    | 51 %  | 1.4 ms     |
| 20 000   | 0 %    | 68 %  | 1.8 ms     |
| 50 000   | 3.1 %  | 88 %  | 1.6 ms     |
| 100 000  | 19 %   | 96 %  | 0.9 ms     |
| 200 000  | 31 %   | 105 % | 0.6 ms     |

Up to 20 000 frames/s, more than two fully loaded 1 Mbit/s buses, no frames are lost. Above that the session needs
a whole CPU core and loses frames because it cannot keep up. Garbage collection is no longer the cause: its longest
pause stays below 2 ms.

| 10 ms periodic transmit | Background | Jitter σ | Longest interval | 99th percentile error |
|-------------------------|-----------:|---------:|-----------------:|----------------------:|
| Software                | idle       | 0.79 ms  | 13.4 ms          | 1.86 ms               |
| Software                | 5 000 fps  | 0.21 ms  | 12.9 ms          | 0.81 ms               |
| Kernel (`hardware=True`)| idle       | 0.03 ms  | 10.2 ms          | 0.16 ms               |
| Kernel (`hardware=True`)| 5 000 fps  | 0.01 ms  | 10.1 ms          | 0.04 ms               |

The kernel-timed transmission is far more regular, but its period runs slightly long, by up to 0.04 ms per period
(37 ms over 10 s), more on an idle CPU. This drift is the kernel's, not CANExplorer's.

## Documentation

The documentation is built with [MkDocs](https://www.mkdocs.org/) and [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/);
the scripting API reference is generated from the docstrings with [mkdocstrings](https://mkdocstrings.github.io/).

```bash
uv run --group docs mkdocs serve           # Live preview on http://127.0.0.1:8000
uv run --group docs mkdocs build --strict  # Build into site/, failing on warnings such as broken links
```

The screenshots in `docs/assets/screenshots` are generated by driving the application with simulated buses:

```bash
QT_QPA_PLATFORM=offscreen uv run python tools/docs_screenshots.py
```
