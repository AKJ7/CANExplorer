"""Loading test scripts and running their test cases on a BusSession."""

import asyncio
import inspect
import sys
import time
import traceback
import types
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import can

from can_explorer.core.isotp_client import IsoTpClient
from can_explorer.core.session import BusSession, SessionError
from can_explorer.core.uds import UdsClient
from can_explorer.scripting.api import ReceivedMessage, ScriptError
from can_explorer.scripting.runtime import ScriptRuntime, Variables
from can_explorer.sys.log_handler import get_logger
from can_explorer.testing.api import (
    CheckFailed,
    Step,
    TestContext,
    TestModule,
    TestSkipped,
    Verdict,
    parameter_id,
)

logger = get_logger(__name__)

OWN_ECHO_WINDOW = 0.5  # Seconds in which an echoed frame is recognised as one the test sent


@dataclass
class TestCaseInfo:
    __test__ = False
    module: str
    name: str
    title: str
    description: str
    timeout: float
    method: str = ''  # Method to call; differs from `name` for parametrised test cases ('case[id]')
    args: tuple = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    requirements: Tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f'{self.module}.{self.name}'


@dataclass
class TestModuleInfo:
    __test__ = False
    name: str
    description: str
    path: Path
    cls: type
    cases: List[TestCaseInfo] = field(default_factory=list)


@dataclass
class LoadedTestScript:
    path: Path
    modules: List[TestModuleInfo] = field(default_factory=list)
    error: str = ''


@dataclass
class TestCaseResult:
    __test__ = False
    module: str
    name: str
    title: str
    description: str = ''
    requirements: Tuple[str, ...] = ()
    verdict: str = Verdict.PENDING
    duration: float = 0.0
    steps: List[Step] = field(default_factory=list)
    error: str = ''

    @property
    def key(self) -> str:
        return f'{self.module}.{self.name}'

    @property
    def checks(self) -> int:
        return sum(1 for step in self.steps if step.kind == 'check')

    @property
    def failed_checks(self) -> int:
        return sum(1 for step in self.steps if step.kind == 'check' and step.passed is False)


@dataclass
class TestReport:
    __test__ = False
    title: str
    started: datetime
    duration: float = 0.0
    environment: Dict[str, str] = field(default_factory=dict)
    results: List[TestCaseResult] = field(default_factory=list)
    stopped: bool = False

    def count(self, verdict: str) -> int:
        return sum(1 for result in self.results if result.verdict == verdict)

    def requirements(self) -> List[Tuple[str, str, List[TestCaseResult]]]:
        """(requirement, verdict, test cases) for every requirement, in the order they appear.

        A requirement fails when any of its test cases failed or had an error, and passed when all of them
        that ran passed.
        """
        cases: Dict[str, List[TestCaseResult]] = {}
        for result in self.results:
            for requirement in result.requirements:
                cases.setdefault(requirement, []).append(result)
        return [(requirement, _combined_verdict(results), results) for requirement, results in cases.items()]

    @property
    def verdict(self) -> str:
        """Overall verdict: error, failed, passed (skipped tests do not fail a run), or pending."""
        verdicts = {result.verdict for result in self.results}
        for verdict in (Verdict.ERROR, Verdict.FAILED):
            if verdict in verdicts:
                return verdict
        if self.stopped or verdicts & {Verdict.PENDING, Verdict.RUNNING}:
            return Verdict.PENDING
        return Verdict.PASSED if Verdict.PASSED in verdicts else Verdict.SKIPPED


def _combined_verdict(results: List[TestCaseResult]) -> str:
    verdicts = {result.verdict for result in results}
    for verdict in (Verdict.ERROR, Verdict.FAILED, Verdict.RUNNING, Verdict.PENDING, Verdict.PASSED):
        if verdict in verdicts:
            return verdict
    return Verdict.SKIPPED


def _as_tuple(value) -> Tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value or ())


def _first_line(text: Optional[str]) -> str:
    return inspect.cleandoc(text or '').split('\n\n')[0].replace('\n', ' ')


def load_test_script(path: str | Path) -> LoadedTestScript:
    """Load a test script and find its test modules and test cases (in definition order)."""
    path = Path(path).resolve()
    script = LoadedTestScript(path)
    module_name = f'can_explorer_test_{abs(hash(path))}'
    try:
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        sys.modules[module_name] = module
        exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
    except Exception as e:
        script.error = f'{type(e).__name__}: {e}'
        return script
    for value in vars(module).values():
        if not (isinstance(value, type) and issubclass(value, TestModule) and value.__module__ == module_name):
            continue
        info = TestModuleInfo(value.__name__, _first_line(value.__doc__), path, value)
        module_requirements = _as_tuple(getattr(value, 'requirements', ()))
        cases = []
        for name, member in inspect.getmembers(value, callable):
            marker = getattr(member, '_testcase', None)
            if marker is None:
                continue
            title = marker['title'] or name.replace('_', ' ').capitalize()
            description = _first_line(member.__doc__)
            requirements = tuple(dict.fromkeys(module_requirements + marker.get('requirements', ())))
            params = marker.get('params')
            if params is None:
                case = TestCaseInfo(info.name, name, title, description, marker['timeout'], name, (), {}, requirements)
                cases.append((marker['order'], 0, case))
                continue
            ids = marker.get('ids') or [parameter_id(param) for param in params]
            for index, (param, param_id) in enumerate(zip(params, ids, strict=True)):
                args, kwargs = (
                    ((), dict(param))
                    if isinstance(param, dict)
                    else (tuple(param), {}) if isinstance(param, tuple) else ((param,), {})
                )
                case = TestCaseInfo(
                    info.name,
                    f'{name}[{param_id}]',
                    f'{title} [{param_id}]',
                    description,
                    marker['timeout'],
                    name,
                    args,
                    kwargs,
                    requirements,
                )
                cases.append((marker['order'], index, case))
        info.cases = [case for _, _, case in sorted(cases, key=lambda item: item[:2])]
        script.modules.append(info)
    if not script.modules:
        script.error = f'{path.name} defines no TestModule subclass'
    return script


class TestRunner:
    """Runs test cases one after the other on a session.

    The session must be open. `database` returns the signal database of the connection; `variables` are the
    shared variables of its simulation, if any. `on_change` is called whenever results change.
    """

    __test__ = False

    def __init__(
        self,
        session: BusSession,
        database: Callable[[], Any],
        variables: Optional[Variables] = None,
        on_change: Optional[Callable[[], None]] = None,
    ):
        self.session = session
        self.runtime = ScriptRuntime(session, database)  # Database helpers: message buffers, decoding
        self.variables = variables if variables is not None else Variables(lambda name, value: None)
        self.scripts: List[LoadedTestScript] = []
        self.report: Optional[TestReport] = None
        self._on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._waiters: List[tuple] = []
        self._collectors: List[tuple] = []
        self._own: Set[int] = set()
        self._recent_sent: deque = deque(maxlen=64)
        self._uds_clients: Dict[int, tuple] = {}
        # Frames received since the test last sent one, until the next wait: responses can arrive before the
        # test starts waiting for them (e.g. from simulated nodes, which answer while the frame is being sent)
        self._since_send: Optional[List[can.Message]] = None
        self.current: str = ''

    # Scripts

    def load(self, path: str | Path) -> LoadedTestScript:
        path = Path(path).resolve()
        script = load_test_script(path)
        self.scripts = [existing for existing in self.scripts if existing.path != path] + [script]
        self.scripts.sort(key=lambda candidate: candidate.path.name)
        self._changed()
        return script

    def unload(self, path: str | Path) -> None:
        path = Path(path).resolve()
        self.scripts = [script for script in self.scripts if script.path != path]
        self._changed()

    @property
    def modules(self) -> List[TestModuleInfo]:
        return [module for script in self.scripts for module in script.modules]

    @property
    def cases(self) -> List[TestCaseInfo]:
        return [case for module in self.modules for case in module.cases]

    # Running

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, selection: Optional[Set[str]] = None, title: str = 'Test run') -> 'asyncio.Task':
        """Run the selected test cases ('Module.case' keys; all when None) in the background."""
        if self.running:
            raise ScriptError('Tests are running already')
        plan = self._prepare(selection, title)  # The report exists at once, also if the run is stopped right away
        self._task = asyncio.ensure_future(self._execute(*plan))
        return self._task

    def stop(self) -> None:
        if self.running:
            self._task.cancel()
            if self.report is not None:
                self.report.stopped = True
            self._changed()

    async def run(self, selection: Optional[Set[str]] = None, title: str = 'Test run') -> TestReport:
        return await self._execute(*self._prepare(selection, title))

    def _prepare(self, selection: Optional[Set[str]], title: str) -> tuple:
        if not self.session.is_open:
            raise ScriptError('Connect before running tests')
        modules = [
            (module, [case for case in module.cases if selection is None or case.key in selection])
            for module in self.modules
        ]
        modules = [(module, cases) for module, cases in modules if cases]
        report = TestReport(title, datetime.now(), environment=self._environment())
        report.results = [
            TestCaseResult(case.module, case.name, case.title, case.description, case.requirements)
            for _, cases in modules
            for case in cases
        ]
        self.report = report
        return report, modules

    async def _execute(self, report: TestReport, modules: List) -> TestReport:
        results = {result.key: result for result in report.results}
        started = time.monotonic()
        self.session.add_message_listener(self._on_message)
        self._changed()
        try:
            for module, cases in modules:
                await self._run_module(module, cases, results)
        except asyncio.CancelledError:
            report.stopped = True
            for result in report.results:
                if result.verdict == Verdict.RUNNING:
                    result.verdict, result.error = Verdict.ERROR, 'Stopped'
        finally:
            self.session.remove_message_listener(self._on_message)
            self._close_uds()
            for _, future in self._waiters:
                if not future.done():
                    future.cancel()
            self._waiters.clear()
            self._collectors.clear()
            report.duration = time.monotonic() - started
            self.current = ''
            self._changed()
        return report

    def _environment(self) -> Dict[str, str]:
        database = self.runtime.database
        return {
            'Bus': self.session.source.description,
            'Database': database.path.name if database is not None else '–',
            'Scripts': ', '.join(script.path.name for script in self.scripts),
        }

    async def _run_module(self, module: TestModuleInfo, cases: List, results: Dict[str, TestCaseResult]) -> None:
        instance = module.cls()
        setup = TestContext(self, instance)
        self.current = f'{module.name} (setup)'
        setup_error = await self._call(instance.setup, setup, timeout=60.0)
        for case in cases:
            result = results[case.key]
            if setup_error:
                result.verdict, result.error = Verdict.ERROR, f'Setup failed: {setup_error}'
                result.steps = list(setup.steps)
                self._changed()
                continue
            await self._run_case(instance, case, result)
        teardown = TestContext(self, instance)
        self.current = f'{module.name} (teardown)'
        teardown_error = await self._call(instance.teardown, teardown, timeout=60.0)
        if teardown_error and cases:
            last = results[cases[-1].key]
            last.steps += teardown.steps
            last.steps.append(Step(0.0, 'error', f'Teardown failed: {teardown_error}', False))
            if last.verdict in (Verdict.PASSED, Verdict.SKIPPED):
                last.verdict, last.error = Verdict.ERROR, f'Teardown failed: {teardown_error}'
        uds = self._uds_clients.pop(id(instance), None)
        if uds is not None:
            uds[0].close()

    async def _call(self, function, t: TestContext, timeout: float) -> str:
        """Run setup or teardown; returns an error text, or ''."""
        try:
            outcome = function(t)
            if inspect.isawaitable(outcome):
                await asyncio.wait_for(outcome, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return self._error_text(e)
        return ''

    async def _run_case(self, instance: TestModule, case: TestCaseInfo, result: TestCaseResult) -> None:
        t = TestContext(self, instance)
        result.steps = t.steps
        result.verdict = Verdict.RUNNING
        self.current = case.key
        self._changed()
        started = time.monotonic()
        try:
            outcome = getattr(instance, case.method or case.name)(t, *case.args, **case.kwargs)
            if inspect.isawaitable(outcome):
                await asyncio.wait_for(outcome, case.timeout)
        except CheckFailed:
            result.verdict = Verdict.FAILED
        except TestSkipped as e:
            result.verdict, result.error = Verdict.SKIPPED, str(e)
        except asyncio.TimeoutError:
            result.verdict, result.error = Verdict.ERROR, f'Timed out after {case.timeout:g} s'
            t._step('error', result.error, False)
        except asyncio.CancelledError:
            result.duration = time.monotonic() - started
            raise
        except Exception as e:
            result.verdict, result.error = Verdict.ERROR, self._error_text(e)
            t._step('error', result.error, False)
        else:
            result.verdict = Verdict.FAILED if t.failed_checks else Verdict.PASSED
        result.duration = time.monotonic() - started
        self._changed()

    @staticmethod
    def _error_text(error: BaseException) -> str:
        details = ''.join(traceback.format_exception(type(error), error, error.__traceback__)[-2:]).strip()
        return f'{type(error).__name__}: {error}\n{details}'

    # Bus access for the test context

    def _send(self, msg: can.Message) -> None:
        if self._since_send is None:
            self._since_send = []
        self._own.add(id(msg))
        self._recent_sent.append((msg.arbitration_id, bytes(msg.data), time.monotonic()))
        try:
            self.session.send(msg)
        except SessionError as e:
            raise ScriptError(str(e)) from e
        finally:
            self._own.discard(id(msg))

    def _is_own(self, msg: can.Message) -> bool:
        if id(msg) in self._own:
            return True
        if msg.is_rx:
            return False
        now, data = time.monotonic(), bytes(msg.data)
        for frame_id, sent_data, sent in self._recent_sent:
            if frame_id == msg.arbitration_id and sent_data == data and now - sent < OWN_ECHO_WINDOW:
                return True
        return False

    def _on_message(self, msg: can.Message) -> None:
        if msg.is_error_frame or (not self._waiters and not self._collectors and self._since_send is None):
            return
        if self._is_own(msg):
            return
        if self._since_send is not None:
            self._since_send.append(msg)
        received = ReceivedMessage(msg, self.runtime)
        for matches, future in list(self._waiters):
            if not future.done() and matches(received):
                future.set_result(received)
        for matches, frames in self._collectors:
            if matches(received):
                frames.append(received)

    def _take_since_send(self) -> List[ReceivedMessage]:
        """Frames received since the last send; only the first wait or collection after a send sees them."""
        frames, self._since_send = self._since_send or [], None
        return [ReceivedMessage(msg, self.runtime) for msg in frames]

    async def _wait(self, matches, timeout: float) -> Optional[ReceivedMessage]:
        for received in self._take_since_send():
            if matches(received):
                return received
        entry = (matches, asyncio.get_running_loop().create_future())
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(entry[1], timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.remove(entry)

    async def _collect(self, matches, duration: float) -> List[ReceivedMessage]:
        entry = (matches, [received for received in self._take_since_send() if matches(received)])
        self._collectors.append(entry)
        try:
            await asyncio.sleep(duration)
        finally:
            self._collectors.remove(entry)
        return entry[1]

    def _uds(self, module: TestModule) -> UdsClient:
        if module.uds_request_id is None or module.uds_response_id is None:
            raise ScriptError('Set uds_request_id and uds_response_id of the test module to use t.uds')
        if id(module) not in self._uds_clients:
            transport = IsoTpClient(
                self.session, module.uds_request_id, module.uds_response_id, extended=module.uds_extended
            )
            self._uds_clients[id(module)] = (transport, UdsClient(transport))
        return self._uds_clients[id(module)][1]

    def _close_uds(self) -> None:
        for transport, _ in self._uds_clients.values():
            transport.close()
        self._uds_clients.clear()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
