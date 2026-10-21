"""Write a log file with synthetic traffic from a signal database, for trying out the reverse engineering tools.

Usage: uv run python tools/synthetic_traffic.py examples/simulation/vehicle.dbc traffic.asc --duration 30 \
    --behaviour EngineData.GearPosition=step:10

Behaviours: sine, ramp, random_walk (with an optional period in seconds, e.g. sine:4), constant, counter, toggle,
noise, checksum (a CRC-8 of the other bytes, for a byte of its own) and step (with the time of the step, e.g.
step:10). Signals without one are smooth, or toggle when they have at most 4 bits. Replay the file, open the Reverse
engineering page, notch before the step and watch its bits, or run the automatic analysis.
"""

import argparse
from pathlib import Path

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.reverse.synthetic import Behaviour, generate


def parse_behaviour(text: str):
    name, _, spec = text.partition('=')
    kind, _, parameter = spec.partition(':')
    if not name or not kind:
        raise argparse.ArgumentTypeError(f'Expected Message.Signal=kind[:value], got {text!r}')
    try:
        if not parameter:
            return name, Behaviour(kind)
        return name, (
            Behaviour(kind, at=float(parameter)) if kind == 'step' else Behaviour(kind, period=float(parameter))
        )
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('database', type=Path, help='DBC, ARXML, KCD, SYM or DBF file')
    parser.add_argument('output', type=Path, help='Log file to write (.asc, .blf, .log, .trc, .csv, …)')
    parser.add_argument('--duration', type=float, default=30.0, help='Seconds of traffic (default 30)')
    parser.add_argument('--seed', type=int, default=0, help='Random seed (default 0)')
    parser.add_argument('--behaviour', type=parse_behaviour, action='append', default=[], metavar='SIGNAL=KIND')
    arguments = parser.parse_args()
    traffic = generate(
        open_signal_database(arguments.database),
        duration=arguments.duration,
        behaviours=dict(arguments.behaviour),
        seed=arguments.seed,
    )
    traffic.write(arguments.output)
    print(f'Wrote {len(traffic.messages):,} frames to {arguments.output}')
    for name, behaviour in sorted(traffic.behaviours.items()):
        field = traffic.fields.get(name)
        print(f'  {name:40} {field.key if field else "(floating point)":24} {behaviour.kind}')


if __name__ == '__main__':
    main()
