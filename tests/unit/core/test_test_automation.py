import asyncio
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import can
import pytest

from can_explorer.core.decoding import open_signal_database
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.scripting import ScriptRuntime
from can_explorer.testing import TestRunner, Verdict, load_test_script, to_html, to_junit
from can_explorer.testing.cli import main as can_test

EXAMPLES = Path(__file__).resolve().parents[3] / 'examples'

SCRIPT = '''
from can_explorer.testing import TestModule, testcase


class Basics(TestModule):
    """Verdicts of the different outcomes."""

    @testcase
    async def passes(self, t):
        t.check(True, 'true is true')
        t.check_equal(1.04, 1, 'close enough', tolerance=0.05)
        t.check_range(5, 0, 10)
        t.log('done')

    @testcase(title='Soft check fails, test goes on')
    async def soft_failure(self, t):
        t.check(False, 'first check fails')
        t.log('still running')

    @testcase
    async def required(self, t):
        t.require(1 == 2, 'one is two')
        t.log('not reached')

    @testcase
    async def explicit_failure(self, t):
        t.fail('broken on purpose')

    @testcase
    async def skipped(self, t):
        t.skip('feature not present')

    @testcase
    async def raises(self, t):
        raise RuntimeError('boom')

    @testcase(timeout=0.2)
    async def too_slow(self, t):
        await t.sleep(5)

    @testcase
    def synchronous(self, t):
        t.check(True, 'sync test cases work too')


class BrokenSetup(TestModule):
    async def setup(self, t):
        raise ValueError('no device')

    @testcase
    async def never_runs(self, t):
        pass


class BrokenTeardown(TestModule):
    async def teardown(self, t):
        raise ValueError('cleanup failed')

    @testcase
    async def passes_but_teardown_fails(self, t):
        t.check(True, 'ok')


class Bus(TestModule):
    uds_request_id = 0x7E0
    uds_response_id = 0x7E8

    @testcase
    async def own_frames_are_not_received(self, t):
        t.send(0x321, b'\\x01')
        await t.expect_no_message(0x321, 0.2)

    @testcase
    async def raw_frames(self, t):
        msg = await t.expect_message(0x123, timeout=2)
        t.check_equal(msg.data, b'\\xAA', 'payload')
        frames = await t.collect(0x123, 0.35)
        t.check(len(frames) >= 2, f'{len(frames)} frames collected')
        times = await t.measure_cycle_time(0x123, 0.5)
        t.check_range(times.mean, 70, 130, 'cycle time of 0x123')

    @testcase
    async def uds_without_ecu(self, t):
        result = await t.uds.request(bytes([0x3E, 0x00]))
        t.check(result.response is None and result.error, 'no response: ' + str(result.error))
'''


@pytest.fixture
def script(tmp_path):
    path = tmp_path / 'checks.py'
    path.write_text(SCRIPT)
    return path


async def peer_sending(channel, stop):
    bus = can.Bus(interface='virtual', channel=channel)
    try:
        while not stop.is_set():
            bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b'\xaa'))
            await asyncio.sleep(0.1)
    finally:
        bus.shutdown()


def test_loading(script, tmp_path):
    loaded = load_test_script(script)
    assert loaded.error == ''
    assert [module.name for module in loaded.modules] == ['Basics', 'BrokenSetup', 'BrokenTeardown', 'Bus']
    basics = loaded.modules[0]
    assert basics.description == 'Verdicts of the different outcomes.'
    assert [case.name for case in basics.cases][:3] == ['passes', 'soft_failure', 'required']  # Definition order
    assert basics.cases[1].title == 'Soft check fails, test goes on' and basics.cases[0].title == 'Passes'
    broken = tmp_path / 'broken.py'
    broken.write_text('def broken(:')
    assert 'SyntaxError' in load_test_script(broken).error
    empty = tmp_path / 'empty.py'
    empty.write_text('x = 1')
    assert 'defines no TestModule' in load_test_script(empty).error


async def test_verdicts(script):
    channel = f'tests-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    stop = asyncio.Event()
    peer = asyncio.ensure_future(peer_sending(channel, stop))
    runner = TestRunner(session, lambda: None)
    runner.load(script)
    report = await runner.run()
    stop.set()
    await peer
    session.stop()

    verdicts = {result.key: result.verdict for result in report.results}
    assert verdicts == {
        'Basics.passes': Verdict.PASSED,
        'Basics.soft_failure': Verdict.FAILED,
        'Basics.required': Verdict.FAILED,
        'Basics.explicit_failure': Verdict.FAILED,
        'Basics.skipped': Verdict.SKIPPED,
        'Basics.raises': Verdict.ERROR,
        'Basics.too_slow': Verdict.ERROR,
        'Basics.synchronous': Verdict.PASSED,
        'BrokenSetup.never_runs': Verdict.ERROR,
        'BrokenTeardown.passes_but_teardown_fails': Verdict.ERROR,
        'Bus.own_frames_are_not_received': Verdict.PASSED,
        'Bus.raw_frames': Verdict.PASSED,
        'Bus.uds_without_ecu': Verdict.PASSED,  # request() reports the missing response in its result
    }
    results = {result.key: result for result in report.results}
    assert [step.text for step in results['Basics.soft_failure'].steps] == ['first check fails', 'still running']
    assert 'not reached' not in [step.text for step in results['Basics.required'].steps]
    assert 'RuntimeError: boom' in results['Basics.raises'].error
    assert 'Timed out after 0.2 s' in results['Basics.too_slow'].error
    assert 'Setup failed: ValueError: no device' in results['BrokenSetup.never_runs'].error
    assert results['Basics.passes'].checks == 3 and results['Basics.soft_failure'].failed_checks == 1
    assert report.verdict == Verdict.ERROR and report.count(Verdict.PASSED) == 5
    assert results['Bus.uds_without_ecu'].steps[0].text.startswith('no response: ')

    html = to_html(report)
    assert '<title>Test run</title>' in html and 'boom' in html and 'Soft check fails' in html
    junit = ET.fromstring(to_junit(report))
    assert junit.get('tests') == '13' and junit.get('failures') == '3' and junit.get('errors') == '4'
    assert junit.find("testsuite[@name='Basics']/testcase[@name='skipped']/skipped") is not None


async def test_example_against_the_simulation():
    channel = f'tests-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.decoder.database = open_signal_database(EXAMPLES / 'simulation' / 'vehicle.dbc')
    session.start()
    simulation = ScriptRuntime(session, lambda: session.decoder.database)
    simulation.load(EXAMPLES / 'simulation' / 'engine_simulation.py')
    simulation.start()
    runner = TestRunner(session, lambda: session.decoder.database, simulation.variables)
    assert runner.load(EXAMPLES / 'testing' / 'vehicle_tests.py').error == ''
    selection = {case.key for case in runner.cases if not case.key.startswith('EngineTests.engine_data')}
    report = await runner.run(selection)
    simulation.stop()
    session.stop()
    verdicts = {result.key: result.verdict for result in report.results}
    assert verdicts.pop('DiagnosticsTests.read_vin') == Verdict.SKIPPED  # The simulation has no UDS server
    assert set(verdicts.values()) == {Verdict.PASSED}, [r for r in report.results if r.verdict != Verdict.PASSED]


async def test_stopping(script):
    session = BusSession(LiveSource(BusConfig('virtual', f'tests-{uuid.uuid4().hex}')))
    session.start()
    runner = TestRunner(session, lambda: None)
    runner.load(script)
    task = runner.start({'Basics.too_slow', 'Basics.passes'})
    await asyncio.sleep(0.05)
    assert runner.running
    runner.stop()
    report = await task
    session.stop()
    assert report.stopped and report.verdict == Verdict.ERROR
    assert {result.key: result.verdict for result in report.results} == {
        'Basics.passes': Verdict.PASSED,
        'Basics.too_slow': Verdict.ERROR,
    }


def test_command_line(tmp_path, capsys):
    args = [
        str(EXAMPLES / 'testing' / 'vehicle_tests.py'),
        '--channel',
        f'cli-{uuid.uuid4().hex}',
        '--database',
        str(EXAMPLES / 'simulation' / 'vehicle.dbc'),
        '--simulation',
        str(EXAMPLES / 'simulation' / 'engine_simulation.py'),
    ]
    assert can_test(args + ['--list']) == 0
    assert 'GearboxTests.obd_vehicle_speed_request' in capsys.readouterr().out
    report, junit = tmp_path / 'report.html', tmp_path / 'results.xml'
    assert can_test(args + ['-k', 'Gearbox', '--report', str(report), '--junit', str(junit)]) == 0
    output = capsys.readouterr().out
    assert 'PASS  GearboxTests.obd_vehicle_speed_request' in output and '2 test cases: 2 passed' in output
    assert (
        report.read_text(encoding='utf-8').startswith('<!doctype html>')
        and ET.parse(junit).getroot().get('failures') == '0'
    )
    assert can_test(args + ['-k', 'nothing matches']) == 2
    assert can_test([str(tmp_path / 'missing.py')]) == 2
    assert can_test(args[:1] + ['--report', str(tmp_path / 'report.txt')]) == 2


PARAMETRISED = '''
from can_explorer.testing import TestModule, testcase


class Limits(TestModule):
    requirements = 'REQ-SYS-001'

    @testcase(params=[1, 2.5, 7], requirements='REQ-LIM-001')
    async def below_five(self, t, value):
        t.check(value < 5, f'{value} < 5')

    @testcase(params=[{'gear': 1, 'limit': 50}, {'gear': 5, 'limit': 180}], ids=['first', 'fifth'])
    async def keywords(self, t, gear, limit):
        t.check_equal(limit, {1: 50, 5: 180}[gear], f'limit of gear {gear}')

    @testcase(params=[(1, 2), (3, 4)], requirements=['REQ-LIM-001', 'REQ-LIM-002'])
    def positional(self, t, low, high):
        t.check(low < high, 'ordered')

    @testcase
    async def plain(self, t):
        t.skip('not today')
'''


async def test_parameters_and_requirements(tmp_path):
    path = tmp_path / 'limits.py'
    path.write_text(PARAMETRISED)
    loaded = load_test_script(path)
    cases = loaded.modules[0].cases
    assert [case.name for case in cases] == [
        'below_five[1]',
        'below_five[2.5]',
        'below_five[7]',
        'keywords[first]',
        'keywords[fifth]',
        'positional[1,2]',
        'positional[3,4]',
        'plain',
    ]
    assert cases[3].title == 'Keywords [first]' and cases[3].kwargs == {'gear': 1, 'limit': 50}
    assert cases[0].requirements == ('REQ-SYS-001', 'REQ-LIM-001') and cases[-1].requirements == ('REQ-SYS-001',)

    session = BusSession(LiveSource(BusConfig('virtual', f'tests-{uuid.uuid4().hex}')))
    session.start()
    runner = TestRunner(session, lambda: None)
    runner.load(path)
    report = await runner.run()
    session.stop()
    verdicts = {result.name: result.verdict for result in report.results}
    assert verdicts['below_five[7]'] == Verdict.FAILED and verdicts['below_five[2.5]'] == Verdict.PASSED
    assert verdicts['keywords[fifth]'] == Verdict.PASSED and verdicts['positional[3,4]'] == Verdict.PASSED
    requirements = {requirement: (verdict, len(results)) for requirement, verdict, results in report.requirements()}
    assert requirements == {
        'REQ-SYS-001': (Verdict.FAILED, 8),
        'REQ-LIM-001': (Verdict.FAILED, 5),
        'REQ-LIM-002': (Verdict.PASSED, 2),
    }
    junit = ET.fromstring(to_junit(report).split('\n', 1)[1])
    case = junit.find(".//testcase[@name='positional[1,2]']")
    assert [prop.get('value') for prop in case.iter('property')] == ['REQ-SYS-001', 'REQ-LIM-001', 'REQ-LIM-002']
    page = to_html(report)
    assert '<h2>Requirements</h2>' in page and '<span class="req">REQ-LIM-002</span>' in page

    with pytest.raises(ValueError, match='one name per parameter set'):
        from can_explorer.testing import testcase

        testcase(params=[1, 2], ids=['one'])


def test_command_line_requirements(tmp_path, capsys):
    path = tmp_path / 'limits.py'
    path.write_text(PARAMETRISED)
    channel = ['--channel', f'cli-{uuid.uuid4().hex}']
    assert can_test([str(path), *channel, '--list', '-r', 'REQ-LIM-002']) == 0
    assert capsys.readouterr().out.splitlines() == [
        'Limits.positional[1,2]  Positional [1,2]  [REQ-SYS-001, REQ-LIM-001, REQ-LIM-002]',
        'Limits.positional[3,4]  Positional [3,4]  [REQ-SYS-001, REQ-LIM-001, REQ-LIM-002]',
    ]
    assert can_test([str(path), *channel, '-r', 'REQ-LIM-002']) == 0
    output = capsys.readouterr().out
    assert '2 requirements' not in output and 'PASS  REQ-LIM-002 (2 test cases)' in output
    assert can_test([str(path), *channel]) == 1
    assert 'FAIL  REQ-LIM-001 (5 test cases)' in capsys.readouterr().out


def test_markdown_summary(tmp_path, capsys):
    path = tmp_path / 'limits.py'
    path.write_text(PARAMETRISED)
    summary = tmp_path / 'step_summary'  # Like $GITHUB_STEP_SUMMARY: no extension, appended to
    summary.write_text('Earlier step\n')
    assert can_test([str(path), '--channel', f'cli-{uuid.uuid4().hex}', '--markdown', str(summary)]) == 1
    text = summary.read_text(encoding='utf-8')
    assert text.startswith('Earlier step\n## ❌ CANExplorer test run')
    assert '| REQ-LIM-002 | ✅ passed | Limits.positional[1,2], Limits.positional[3,4] |' in text
    assert '| Limits.below_five[7] | ❌ failed |' in text and '7 < 5' in text
    assert '| Limits.plain | ⏭️ skipped |' in text and 'not today' in text
