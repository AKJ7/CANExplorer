"""End-to-end benchmark on Linux SocketCAN virtual interfaces (vcan): receive throughput and transmit timing.

Unlike tools/benchmark.py, which feeds frames straight into a session, this one goes through the kernel's SocketCAN
stack like a real adapter: frames come from a separate process (can-utils `cangen`), and transmit timing is observed
by a separate process (`candump`, kernel timestamps).

1. Receive: a BusSession on each channel while `cangen` sends at increasing rates. Reports the rate actually sent,
   the frames the session lost, the CPU this process used (100 % = one core) and the longest garbage collection
   pause (a pause longer than the kernel's socket buffer can bridge loses frames).
2. Transmit timing: a 10 ms periodic frame sent by the session, with software timing (the asyncio loop, as the
   Transmit panel does by default) and with the kernel's broadcast manager (hardware=True), on an idle bus and while
   the session receives background traffic. Reports the measured period, its jitter and late frames.

vcan has no bitrate and no arbitration, so this measures CANExplorer's software ceiling, not an adapter's limits.

Set up once (needs root):

    sudo modprobe vcan
    sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0

Usage:

    uv run python tools/benchmark_socketcan.py                       # both benchmarks on vcan0
    uv run python tools/benchmark_socketcan.py --only receive --decoder j1939 --dbc tests/data/example.dbc
    uv run python tools/benchmark_socketcan.py --channels vcan0,vcan1 --json results.json
"""

import argparse
import asyncio
import gc
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import List, Optional

import can

from can_explorer.core.decoding import DbcDecoder, FrameDecoder
from can_explorer.core.protocols import create_protocol_decoder
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource

DEFAULT_RATES = [
    1_000,
    5_000,
    10_000,
    20_000,
    50_000,
    100_000,
    200_000,
    0,
]  # Frames per second per channel; 0 = as fast as possible
FLOOD_FRAMES = 200_000  # Frames of the as-fast-as-possible step
PERIOD = 0.010  # Transmit timing: 10 ms
TX_ID = 0x7E5  # Identifier of the timed frame; background traffic avoids it
BACKGROUND_RATE = 5_000  # Frames per second of the background traffic during the transmit benchmark
CANDUMP_LINE = re.compile(r'^\((\d+\.\d+)\)\s+\S+\s+([0-9A-Fa-f]+)#')


# Set-up checks


def check_environment(channels: List[str]) -> None:
    problems = []
    if platform.system() != 'Linux':
        problems.append('SocketCAN needs Linux.')
    for tool in ('cangen', 'candump'):
        if shutil.which(tool) is None:
            problems.append(f'{tool} not found: install can-utils (e.g. sudo apt install can-utils).')
    for channel in channels:
        state = Path(f'/sys/class/net/{channel}/operstate')
        flags = Path(f'/sys/class/net/{channel}/flags')
        if not state.exists():
            problems.append(
                f'{channel} does not exist: sudo modprobe vcan && sudo ip link add dev {channel} type vcan '
                f'&& sudo ip link set up {channel}'
            )
        elif not int(flags.read_text(), 16) & 0x1:  # IFF_UP
            problems.append(f'{channel} is down: sudo ip link set up {channel}')
    if problems:
        sys.exit('Cannot run the benchmark:\n  ' + '\n  '.join(problems))


def make_decoder(name: str, dbc: Optional[Path]) -> FrameDecoder:
    protocol = None if name == 'raw' else create_protocol_decoder({'j1939': 'J1939', 'uds': 'UDS'}.get(name, name))
    return FrameDecoder(protocol, DbcDecoder(dbc) if dbc else None)


def cangen(channel: str, rate: int, count: int, extended: bool, background: bool = False) -> subprocess.Popen:
    """Start cangen sending `count` frames at `rate` frames/s (0: as fast as possible), 8 random data bytes."""
    command = ['cangen', channel, '-L', '8', '-D', 'r', '-n', str(count), '-p', '100']
    command += ['-e'] if extended else []
    if background:
        command += ['-I', '123']  # One fixed identifier, away from TX_ID
    if rate:
        # Bursts keep the gap at about 0.5 ms or more: cangen sleeps between bursts, and short sleeps are imprecise
        burst = max(1, round(rate / 2000))
        command += ['-a', '-c', str(burst), '-g', f'{burst * 1000 / rate:.4f}']
    else:
        command += ['-g', '0']
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


async def open_session(channel: str, decoder: FrameDecoder) -> BusSession:
    session = BusSession(LiveSource(BusConfig(interface='socketcan', channel=channel)), decoder, buffer_size=100_000)
    session.start()
    deadline = time.monotonic() + 5
    while not session.is_open:
        if time.monotonic() > deadline:
            raise RuntimeError(f'Could not open {channel}')
        await asyncio.sleep(0.01)
    return session


async def settle(sessions: List[BusSession], quiet: float = 0.5, limit: float = 15.0) -> None:
    """Wait until no session has received a frame for `quiet` seconds (or `limit` passed)."""
    last, still_since, started = None, time.monotonic(), time.monotonic()
    while time.monotonic() - started < limit:
        totals = [session.trace.total for session in sessions]
        if totals != last:
            last, still_since = totals, time.monotonic()
        elif time.monotonic() - still_since >= quiet:
            return
        await asyncio.sleep(0.05)


class GcPauses:
    """Records how long each garbage collection stops the process. A full collection walks every frame the trace
    holds, so it can pause receiving long enough for the kernel's socket buffer to overflow."""

    def __init__(self):
        self.pauses_ms: List[float] = []
        self._started = 0.0

    def _callback(self, phase: str, info: dict) -> None:
        if phase == 'start':
            self._started = time.perf_counter()
        else:
            self.pauses_ms.append((time.perf_counter() - self._started) * 1000)

    def __enter__(self) -> 'GcPauses':
        gc.callbacks.append(self._callback)
        return self

    def __exit__(self, *exc) -> None:
        gc.callbacks.remove(self._callback)

    @property
    def longest_ms(self) -> float:
        return max(self.pauses_ms, default=0.0)


# 1. Receive


@dataclass
class ReceiveStep:
    target_fps: Optional[int]  # None: as fast as possible
    sent: int  # Per channel
    sent_fps: float  # Per channel, measured
    received: List[int]
    lost_percent: float  # Over all channels
    cpu_percent: float  # Of this process; 100 % = one core
    longest_gc_ms: float  # Longest garbage collection pause during the step


async def receive_step(channels, decoder_factory, rate, duration, extended) -> ReceiveStep:
    count = FLOOD_FRAMES if rate == 0 else max(1, int(rate * duration))
    sessions = [await open_session(channel, decoder_factory()) for channel in channels]
    try:
        with GcPauses() as gc_pauses:
            cpu_start, wall_start = time.process_time(), time.perf_counter()
            generators = [cangen(channel, rate, count, extended) for channel in channels]
            while any(generator.poll() is None for generator in generators):
                await asyncio.sleep(0.02)
            send_time = time.perf_counter() - wall_start
            for generator in generators:
                if generator.returncode != 0:
                    raise RuntimeError(f'cangen failed: {generator.stderr.read().decode().strip()}')
            await settle(sessions)
            cpu = time.process_time() - cpu_start
            wall = time.perf_counter() - wall_start
            received = [session.trace.total for session in sessions]
    finally:
        for session in sessions:
            session.stop()
    total_sent = count * len(channels)
    return ReceiveStep(
        target_fps=rate or None,
        sent=count,
        sent_fps=count / send_time,
        received=received,
        lost_percent=100 * (total_sent - sum(received)) / total_sent,
        cpu_percent=100 * cpu / wall,
        longest_gc_ms=gc_pauses.longest_ms,
    )


# 2. Transmit timing


@dataclass
class TimingResult:
    mode: str  # software / hardware (kernel broadcast manager)
    background_fps: int
    frames: int
    mean_ms: float
    stdev_ms: float
    min_ms: float
    max_ms: float
    p99_error_ms: float  # 99th percentile of |interval - period|
    late: int  # Intervals over 1.5 periods (a frame came late, or was missed)
    drift_ms: float  # Last frame's time minus where an exact period would have put it


def interval_stats(timestamps: List[float], mode: str, background: int) -> TimingResult:
    intervals = [(b - a) * 1000 for a, b in pairwise(timestamps)]
    period_ms = PERIOD * 1000
    errors = sorted(abs(interval - period_ms) for interval in intervals)
    expected_end = timestamps[0] + PERIOD * (len(timestamps) - 1)
    return TimingResult(
        mode=mode,
        background_fps=background,
        frames=len(timestamps),
        mean_ms=statistics.fmean(intervals),
        stdev_ms=statistics.pstdev(intervals),
        min_ms=min(intervals),
        max_ms=max(intervals),
        p99_error_ms=errors[min(len(errors) - 1, math.ceil(0.99 * len(errors)) - 1)],
        late=sum(1 for interval in intervals if interval > 1.5 * period_ms),
        drift_ms=(timestamps[-1] - expected_end) * 1000,
    )


async def timing_run(channel, decoder_factory, duration, hardware, background) -> TimingResult:
    # candump timestamps every TX_ID frame in the kernel, independently of this process
    observer = subprocess.Popen(
        ['candump', '-L', f'{channel},{TX_ID:03X}:7FF'], stdout=subprocess.PIPE, text=True, bufsize=1
    )
    session = await open_session(channel, decoder_factory())
    load = None
    try:
        await asyncio.sleep(0.3)  # Let candump open its socket
        if background:
            load = cangen(channel, background, int(background * (duration + 2)), False, background=True)
        message = can.Message(arbitration_id=TX_ID, data=bytes(8), is_extended_id=False)
        periodic = session.start_periodic(message, PERIOD, hardware=hardware)
        await asyncio.sleep(duration)
        periodic.stop()
        await asyncio.sleep(0.2)
    finally:
        if load is not None:
            load.terminate()
            load.wait()
        session.stop()
        observer.terminate()
    output, _ = observer.communicate()
    timestamps = [float(m.group(1)) for line in output.splitlines() if (m := CANDUMP_LINE.match(line))]
    if len(timestamps) < 10:
        raise RuntimeError(f'candump saw only {len(timestamps)} frames of 0x{TX_ID:X}')
    return interval_stats(timestamps, 'hardware' if hardware else 'software', background)


# Report


@dataclass
class Results:
    machine: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    receive: List[ReceiveStep] = field(default_factory=list)
    timing: List[TimingResult] = field(default_factory=list)


def machine_info() -> dict:
    cpu = ''
    try:
        cpu = next(
            line.split(':', 1)[1].strip()
            for line in Path('/proc/cpuinfo').read_text().splitlines()
            if line.startswith('model name')
        )
    except (OSError, StopIteration):
        pass
    return {'cpu': cpu, 'cores': os.cpu_count(), 'kernel': platform.release(), 'python': platform.python_version()}


def print_receive(steps: List[ReceiveStep], channels: List[str]) -> None:
    print(f'\n1. Receive ({", ".join(channels)}; per channel)')
    print(f'   {"target fps":>11} {"sent fps":>10} {"frames":>9} {"lost":>8} {"CPU":>6} {"longest GC":>11}')
    for step in steps:
        target = f'{step.target_fps:,}' if step.target_fps else 'max'
        print(
            f'   {target:>11} {step.sent_fps:>10,.0f} {step.sent:>9,} {step.lost_percent:>7.2f}% '
            f'{step.cpu_percent:>5.0f}% {step.longest_gc_ms:>8.1f} ms'
        )


def print_timing(results: List[TimingResult]) -> None:
    print(f'\n2. Transmit timing ({PERIOD * 1000:.0f} ms period, measured by candump)')
    print(
        f'   {"timing":<9} {"background":>10} {"frames":>7} {"mean":>8} {"jitter σ":>9} {"min":>7} {"max":>7} '
        f'{"p99 err":>8} {"late":>5} {"drift":>8}'
    )
    for r in results:
        background = f'{r.background_fps:,} fps' if r.background_fps else 'idle'
        print(
            f'   {r.mode:<9} {background:>10} {r.frames:>7} {r.mean_ms:>6.3f}ms {r.stdev_ms:>7.3f}ms '
            f'{r.min_ms:>5.2f}ms {r.max_ms:>5.2f}ms {r.p99_error_ms:>6.3f}ms {r.late:>5} {r.drift_ms:>6.1f}ms'
        )


async def run(args) -> Results:
    channels = [channel.strip() for channel in args.channels.split(',') if channel.strip()]
    check_environment(channels)
    dbc = Path(args.dbc) if args.dbc else None
    results = Results(
        machine=machine_info(),
        settings={'channels': channels, 'decoder': args.decoder, 'dbc': str(dbc) if dbc else None},
    )
    print(f'Machine: {results.machine["cpu"]} ({results.machine["cores"]} cores), kernel {results.machine["kernel"]}')
    print(f'Decoder: {args.decoder}' + (f' + DBC {dbc.name}' if dbc else ''))

    def decoder_factory():
        return make_decoder(args.decoder, dbc)

    if args.only in (None, 'receive'):
        rates = [int(rate) for rate in args.rates.split(',')] if args.rates else DEFAULT_RATES
        for rate in rates:
            step = await receive_step(channels, decoder_factory, rate, args.duration, args.extended)
            results.receive.append(step)
            print(f'   ... {step.target_fps or "max"}: {step.lost_percent:.2f}% lost', flush=True)
        print_receive(results.receive, channels)
    if args.only in (None, 'timing'):
        for hardware in (False, True):
            for background in (0, BACKGROUND_RATE):
                result = await timing_run(channels[0], decoder_factory, args.timing_duration, hardware, background)
                results.timing.append(result)
        print_timing(results.timing)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--channels', default='vcan0', help='Comma-separated vcan interfaces (default vcan0)')
    parser.add_argument('--only', choices=('receive', 'timing'), help='Run one benchmark only')
    parser.add_argument('--rates', help='Receive rates in frames/s per channel, 0 = max (default 1k..200k,max)')
    parser.add_argument('--duration', type=float, default=3.0, help='Seconds per receive step (default 3)')
    parser.add_argument('--timing-duration', type=float, default=10.0, help='Seconds per timing run (default 10)')
    parser.add_argument('--decoder', default='raw', help='raw (default), j1939, uds or another protocol name')
    parser.add_argument('--dbc', help='Also decode with this database')
    parser.add_argument('--extended', action='store_true', help='Send 29-bit identifiers (e.g. for J1939)')
    parser.add_argument('--json', help='Also write the results to this JSON file')
    args = parser.parse_args()
    results = asyncio.run(run(args))
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(results), indent=2))
        print(f'\nWrote {args.json}')


if __name__ == '__main__':
    main()
