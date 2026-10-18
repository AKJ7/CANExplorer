"""Command line test runner, e.g. for continuous integration.

Usage: can-test SCRIPT [SCRIPT ...] [--interface virtual] [--channel test] [--database vehicle.dbc]
                [--simulation nodes.py] [-k FILTER] [--report report.html] [--junit results.xml]

Exit code 0 when all selected test cases passed (or were skipped), 1 when any failed or had an error, 2 when the
scripts or the bus could not be loaded.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.session import BusSession, SessionState
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.scripting import ScriptRuntime
from can_explorer.testing.api import Verdict
from can_explorer.testing.report import write_report
from can_explorer.testing.runner import TestReport, TestRunner

SYMBOLS = {Verdict.PASSED: 'PASS', Verdict.FAILED: 'FAIL', Verdict.ERROR: 'ERROR', Verdict.SKIPPED: 'SKIP'}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='can-test', description='Run CANExplorer test scripts on a CAN bus.')
    parser.add_argument('scripts', nargs='+', type=Path, help='test scripts (Python files with TestModule classes)')
    parser.add_argument('--interface', default='virtual', help='python-can interface (default: virtual)')
    parser.add_argument('--channel', default='can-test', help='channel, e.g. can0 or PCAN_USBBUS1')
    parser.add_argument('--bitrate', type=int, default=500_000, help='bitrate (default: 500000)')
    parser.add_argument('--fd', action='store_true', help='CAN FD')
    parser.add_argument('--data-bitrate', type=int, help='CAN FD data bitrate')
    parser.add_argument('--database', type=Path, help='signal database (DBC, DBF, KCD, SYM, ARXML, LDF)')
    parser.add_argument(
        '--simulation', type=Path, action='append', default=[], help='simulation script to run alongside (repeatable)'
    )
    parser.add_argument('-k', dest='filter', help='run only test cases whose "Module.case" contains this text')
    parser.add_argument('--report', type=Path, help='write an HTML report')
    parser.add_argument('--junit', type=Path, help='write JUnit XML results')
    parser.add_argument('--list', action='store_true', help='list the test cases and exit')
    parser.add_argument('--title', default='CANExplorer test run', help='title of the reports')
    return parser


def _print_result(result) -> None:
    line = f'{SYMBOLS.get(result.verdict, result.verdict):5} {result.key} ({result.duration:.2f} s)'
    print(line, flush=True)
    if result.verdict in (Verdict.FAILED, Verdict.ERROR):
        for step in result.steps:
            if step.passed is False:
                print(f'        ✘ {step.text}', flush=True)
        if result.verdict == Verdict.ERROR and result.error:
            print('        ' + result.error.replace('\n', '\n        '), flush=True)


async def run(args: argparse.Namespace) -> int:
    session = BusSession(
        LiveSource(
            BusConfig(
                args.interface,
                args.channel,
                bitrate=args.bitrate,
                fd=args.fd or args.data_bitrate is not None,
                data_bitrate=args.data_bitrate,
            )
        )
    )
    if args.database:
        try:
            session.decoder.database = open_signal_database(args.database)
        except Exception as e:
            print(f'error: could not load {args.database}: {e}', file=sys.stderr)
            return 2

    simulation = ScriptRuntime(
        session,
        lambda: session.decoder.database,
        lambda level, source, text: print(f'  [{source}] {text}', file=sys.stderr, flush=True),
    )
    for path in args.simulation:
        if simulation.load(path).error:
            return 2
    printed = set()

    def progress() -> None:
        if runner.report is None:
            return
        for result in runner.report.results:
            if result.key not in printed and result.verdict not in (Verdict.PENDING, Verdict.RUNNING):
                printed.add(result.key)
                _print_result(result)

    runner = TestRunner(session, lambda: session.decoder.database, simulation.variables, on_change=progress)
    failed_loading = False
    for path in args.scripts:
        script = runner.load(path)
        if script.error:
            print(f'error: {script.error}', file=sys.stderr)
            failed_loading = True
    if failed_loading:
        return 2
    selection = {case.key for case in runner.cases if not args.filter or args.filter in case.key}
    if args.list:
        for case in runner.cases:
            if case.key in selection:
                print(f'{case.key}  {case.title}')
        return 0
    if not selection:
        print('error: no test cases selected', file=sys.stderr)
        return 2

    session.start()
    if session.state is not SessionState.OPEN:
        print(f'error: could not open {args.interface}:{args.channel}: {session.error}', file=sys.stderr)
        return 2
    try:
        if args.simulation:
            simulation.start()
        report: TestReport = await runner.run(selection, args.title)
    finally:
        simulation.stop()
        session.stop()

    print(
        f'\n{len(report.results)} test cases: {report.count(Verdict.PASSED)} passed, {report.count(Verdict.FAILED)} '
        f'failed, {report.count(Verdict.ERROR)} errors, {report.count(Verdict.SKIPPED)} skipped '
        f'in {report.duration:.1f} s'
    )
    for path in filter(None, (args.report, args.junit and args.junit.with_suffix('.xml'))):
        print(f'Wrote {write_report(report, path)}')
    return 0 if report.verdict in (Verdict.PASSED, Verdict.SKIPPED) else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.report and args.report.suffix.lower() not in ('.html', '.htm'):
        print('error: --report writes HTML; use a .html file name (--junit writes XML)', file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == '__main__':
    sys.exit(main())
