"""The API of test scripts: test modules, test cases and the test context `t` passed to them."""

import asyncio
import itertools
import math
import statistics
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, List, Optional, Union

import can

from can_explorer.scripting.api import MessageBuffer, ReceivedMessage, ScriptError

if TYPE_CHECKING:
    from can_explorer.testing.runner import TestRunner

#: Selects frames: an identifier, a database message name, a function of the ReceivedMessage, or None for any.
Selector = Union[int, str, Callable[[ReceivedMessage], bool], None]

_order = itertools.count()


class Verdict:
    PASSED = 'passed'
    FAILED = 'failed'
    ERROR = 'error'
    SKIPPED = 'skipped'
    RUNNING = 'running'
    PENDING = 'pending'


class CheckFailed(Exception):
    """Raised by `t.require` and `t.fail`: ends the test case as failed."""


class TestSkipped(Exception):
    """Raised by `t.skip`: ends the test case as skipped."""

    __test__ = False


@dataclass
class Step:
    """One entry of a test case's log."""

    time: float  # Seconds since the test case started
    kind: str  # 'check', 'log', 'send', 'info' or 'error'
    text: str
    passed: Optional[bool] = None  # For checks


@dataclass
class CycleTimes:
    """Cycle time statistics of a message in milliseconds; NaN with fewer than two frames."""

    count: int
    mean: float = math.nan
    minimum: float = math.nan
    maximum: float = math.nan
    deviation: float = math.nan

    def __str__(self) -> str:
        if self.count < 2:
            return f'{self.count} frames'
        return (
            f'{self.count} frames, mean {self.mean:.1f} ms, min {self.minimum:.1f} ms, max {self.maximum:.1f} ms, '
            f'deviation {self.deviation:.1f} ms'
        )


def testcase(
    function: Optional[Callable] = None,
    *,
    title: Optional[str] = None,
    timeout: float = 30.0,
    params: Optional[Iterable[Any]] = None,
    ids: Optional[Iterable[str]] = None,
    requirements: Union[str, Iterable[str]] = (),
):
    """Mark a method of a TestModule as test case. Test cases run in the order they are defined.

    Examples:
        ```python
        @testcase
        async def engine_speed_is_sent(self, t): ...

        @testcase(title='Brake lights follow the pedal', timeout=10, requirements='REQ-BODY-012')
        async def brake_lights(self, t): ...

        @testcase(params=[800, 2000, 4000])                      # One test case per value
        async def idle_and_cruise(self, t, speed): ...

        @testcase(params=[{'gear': 1, 'limit': 50}, {'gear': 5, 'limit': 180}], ids=['first', 'fifth'])
        async def speed_limit(self, t, gear, limit): ...
        ```

    Args:
        title: Title in the results; defaults to the method name with spaces.
        timeout: The test case ends with an error when it runs longer (seconds).
        params: Run the test case once per item: a dict is passed as keyword arguments, a tuple as positional
            arguments, anything else as one argument after `t`. Each run is a test case of its own, named
            `method[id]`.
        ids: Names of the parameter sets in the results; by default the values, e.g. `gear=1,limit=50`.
        requirements: Requirement identifiers the test case verifies, e.g. 'REQ-ENG-001' or a list of them.
            The reports list the verdict per requirement.
    """
    params_list = list(params) if params is not None else None
    ids_list = list(ids) if ids is not None else None
    if params_list is not None and ids_list is not None and len(ids_list) != len(params_list):
        raise ValueError('ids needs one name per parameter set')
    requirement_list = (requirements,) if isinstance(requirements, str) else tuple(requirements)

    def mark(function: Callable) -> Callable:
        function._testcase = {
            'order': next(_order),
            'title': title,
            'timeout': timeout,
            'params': params_list,
            'ids': ids_list,
            'requirements': requirement_list,
        }
        return function

    return mark(function) if function is not None else mark


def parameter_id(value: Any) -> str:
    """The name of a parameter set in test case names, e.g. '800' or 'gear=1,limit=50'."""
    if isinstance(value, dict):
        return ','.join(f'{key}={parameter_id(item)}' for key, item in value.items())
    if isinstance(value, tuple):
        return ','.join(parameter_id(item) for item in value)
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


class TestModule:
    """Base class of test modules. Subclass it in a test script and mark its test methods with `@testcase`.

    The docstring describes the module in the results. `setup` runs before the first test case of the module and
    `teardown` after the last one; a failing setup marks all test cases of the module as errors.

    Attributes:
        requirements: Requirement identifiers all test cases of the module verify (besides their own).
        uds_request_id: CAN identifier of the UDS requests (`t.uds`), e.g. 0x7E0.
        uds_response_id: CAN identifier of the UDS responses, e.g. 0x7E8.
        uds_extended: Whether the UDS identifiers are extended (29 bit).
    """

    __test__ = False  # Not a pytest test class
    requirements: Union[str, Iterable[str]] = ()
    uds_request_id: Optional[int] = None
    uds_response_id: Optional[int] = None
    uds_extended: bool = False

    async def setup(self, t: 'TestContext') -> None:
        """Runs before the first test case of the module."""

    async def teardown(self, t: 'TestContext') -> None:
        """Runs after the last test case of the module, also when test cases failed."""


def _describe_selector(selector: Selector) -> str:
    if selector is None:
        return 'any frame'
    if isinstance(selector, int):
        return f'0x{selector:X}'
    if isinstance(selector, str):
        return selector
    return getattr(selector, '__name__', 'matching frame')


def _matcher(selector: Selector, condition: Optional[Callable[[ReceivedMessage], bool]]):
    def matches(msg: ReceivedMessage) -> bool:
        if isinstance(selector, int) and msg.id != selector:
            return False
        if isinstance(selector, str) and msg.name != selector:
            return False
        if callable(selector) and not selector(msg):
            return False
        return condition is None or bool(condition(msg))

    return matches


class TestContext:
    """The `t` of a test case: checks, sending, waiting for frames and signals, timing and diagnostics.

    Checks record a step and continue the test case; `require` and `fail` end it. Waits see the frames that arrive
    after they start, and, for the first wait after `send`, also those that arrived since the send (responses can
    come before the wait starts). Frames the test sent itself are never seen.
    """

    __test__ = False

    def __init__(self, runner: 'TestRunner', module: TestModule):
        self._runner = runner
        self._module = module
        self.steps: List[Step] = []
        self._started = time.monotonic()
        self.failed_checks = 0

    # Logging and checks

    @property
    def elapsed(self) -> float:
        """Seconds since the test case (or setup/teardown) started."""
        return time.monotonic() - self._started

    def _step(self, kind: str, text: str, passed: Optional[bool] = None) -> None:
        self.steps.append(Step(round(self.elapsed, 4), kind, text, passed))
        self._runner._changed()

    def log(self, *values: Any) -> None:
        """Add a line to the test case's log."""
        self._step('log', ' '.join(str(value) for value in values))

    def check(self, condition: Any, description: str) -> bool:
        """Record a check. A failed check fails the test case but lets it continue. Returns the result."""
        passed = bool(condition)
        if not passed:
            self.failed_checks += 1
        self._step('check', description, passed)
        return passed

    def check_equal(self, actual: Any, expected: Any, description: str = '', tolerance: float = 0.0) -> bool:
        """Check that `actual` equals `expected` (numbers within `tolerance`)."""
        if tolerance and isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
            passed = abs(actual - expected) <= tolerance
            detail = f'{actual!r} = {expected!r} ± {tolerance:g}'
        else:
            passed = actual == expected
            detail = f'{actual!r} == {expected!r}'
        return self.check(passed, f'{description}: {detail}' if description else detail)

    def check_range(self, value: Any, minimum: float, maximum: float, description: str = '') -> bool:
        """Check that `minimum <= value <= maximum`."""
        passed = value is not None and minimum <= value <= maximum
        detail = f'{minimum:g} ≤ {value!r} ≤ {maximum:g}'
        return self.check(passed, f'{description}: {detail}' if description else detail)

    def require(self, condition: Any, description: str) -> None:
        """Like `check`, but a failure ends the test case."""
        if not self.check(condition, description):
            raise CheckFailed(description)

    def fail(self, description: str) -> None:
        """End the test case as failed."""
        self.check(False, description)
        raise CheckFailed(description)

    def skip(self, reason: str) -> None:
        """End the test case as skipped, e.g. when the device under test lacks a feature."""
        self._step('info', f'Skipped: {reason}')
        raise TestSkipped(reason)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    # Shared variables of the simulation

    @property
    def variables(self):
        """Shared variables of the simulation of the connection (e.g. `t.variables['ignition'] = True`)."""
        return self._runner.variables

    # Sending

    def message(self, name: str) -> MessageBuffer:
        """A buffer with the signals of a database message, to fill and send."""
        return self._runner.runtime.message(name)

    def send(
        self,
        message: Union[MessageBuffer, str, int],
        data: Union[bytes, Iterable[int], None] = None,
        *,
        extended: Optional[bool] = None,
        fd: bool = False,
        remote: bool = False,
        **signals: Any,
    ) -> None:
        """Send a frame: `t.send('BrakeCommand', Pressure=50)`, `t.send(buffer)` or `t.send(0x123, b'\\x01')`."""
        if isinstance(message, str):
            message = self.message(message).update(signals)
        elif signals:
            raise ScriptError('Signal values can only be given with a database message name')
        if isinstance(message, MessageBuffer):
            msg = can.Message(
                arbitration_id=message.id,
                is_extended_id=message.is_extended,
                is_fd=message.is_fd,
                bitrate_switch=message.is_fd,
                data=message.encode(),
            )
            name = message.name
        else:
            payload = bytes(data or b'')
            msg = can.Message(
                arbitration_id=int(message),
                is_extended_id=int(message) > 0x7FF if extended is None else extended,
                is_fd=fd,
                bitrate_switch=fd,
                is_remote_frame=remote,
                dlc=8 if remote and not payload else None,
                data=payload,
            )
            name = f'0x{msg.arbitration_id:X}'
        self._runner._send(msg)
        self._step('send', f'{name} [{bytes(msg.data).hex(" ").upper()}]')

    # Waiting

    async def wait_for_message(
        self,
        selector: Selector = None,
        timeout: float = 1.0,
        condition: Optional[Callable[[ReceivedMessage], bool]] = None,
    ) -> Optional[ReceivedMessage]:
        """The next frame matching `selector` (and `condition`), or None after `timeout` seconds.

        Examples:
            ```python
            msg = await t.wait_for_message('EngineData')
            msg = await t.wait_for_message(0x7E8, timeout=0.5)
            msg = await t.wait_for_message('EngineData', condition=lambda m: m.signals['EngineSpeed'] > 2000)
            ```
        """
        return await self._runner._wait(_matcher(selector, condition), timeout)

    async def expect_message(
        self,
        selector: Selector,
        timeout: float = 1.0,
        condition: Optional[Callable[[ReceivedMessage], bool]] = None,
        description: str = '',
    ) -> Optional[ReceivedMessage]:
        """Wait for a frame and record whether it came in time."""
        msg = await self.wait_for_message(selector, timeout, condition)
        text = description or f'{_describe_selector(selector)} received within {timeout:g} s'
        self.check(msg is not None, text)
        return msg

    async def expect_no_message(self, selector: Selector, duration: float = 1.0, description: str = '') -> bool:
        """Check that no matching frame arrives for `duration` seconds."""
        msg = await self.wait_for_message(selector, duration)
        text = description or f'no {_describe_selector(selector)} for {duration:g} s'
        return self.check(msg is None, text if msg is None else f'{text} (got {msg!r})')

    @staticmethod
    def _split_key(key: str) -> tuple:
        message, _, signal = key.partition('.')
        if not signal:
            raise ScriptError(f'Signals are given as "Message.Signal", not {key!r}')
        return message, signal

    async def wait_for_signal(self, key: str, expected: Any = None, timeout: float = 1.0) -> Any:
        """Wait until signal `key` ("Message.Signal") has a value: any value, `expected`, or one for which the
        function `expected` returns True. Returns the value, or None after `timeout` seconds.

        Value table texts can be compared too: `await t.wait_for_signal('EngineData.GearPosition', 'First')`.
        """
        message, signal = self._split_key(key)

        def accepted(msg: ReceivedMessage) -> bool:
            if signal not in msg.signals:
                return False
            if expected is None:
                return True
            if callable(expected):
                return bool(expected(msg.signals[signal]))
            return msg.signals[signal] == expected or msg.labels.get(signal) == expected

        msg = await self.wait_for_message(message, timeout, accepted)
        if msg is None:
            return None
        value = msg.signals[signal]
        return msg.labels.get(signal, value) if isinstance(expected, str) else value

    async def expect_signal(self, key: str, expected: Any = None, timeout: float = 1.0, description: str = '') -> Any:
        """Wait for a signal value (see `wait_for_signal`) and record whether it came in time."""
        value = await self.wait_for_signal(key, expected, timeout)
        if description:
            text = description
        elif expected is None:
            text = f'{key} received within {timeout:g} s'
        elif callable(expected):
            name = getattr(expected, '__name__', '<lambda>')
            text = f'{key} meets {"the condition" if name == "<lambda>" else name} within {timeout:g} s'
        else:
            text = f'{key} = {expected!r} within {timeout:g} s'
        self.check(value is not None, f'{text}' + (f' (value {value!r})' if value is not None else ''))
        return value

    async def collect(self, selector: Selector = None, duration: float = 1.0) -> List[ReceivedMessage]:
        """All matching frames that arrive within `duration` seconds."""
        return await self._runner._collect(_matcher(selector, None), duration)

    async def measure_cycle_time(self, selector: Selector, duration: float = 1.0) -> CycleTimes:
        """Measure the cycle time of a message for `duration` seconds."""
        frames = await self.collect(selector, duration)
        times = [frame.timestamp for frame in frames]
        intervals = [(later - earlier) * 1000 for earlier, later in zip(times, times[1:], strict=False)]
        if not intervals:
            return CycleTimes(len(frames))
        return CycleTimes(
            len(frames),
            statistics.fmean(intervals),
            min(intervals),
            max(intervals),
            statistics.pstdev(intervals) if len(intervals) > 1 else 0.0,
        )

    async def expect_cycle_time(
        self, selector: Selector, expected_ms: float, tolerance: float = 0.1, duration: float = 1.0
    ) -> CycleTimes:
        """Check that the mean cycle time is within `tolerance` (a fraction, 0.1 = ±10 %) of `expected_ms`."""
        times = await self.measure_cycle_time(selector, duration)
        name = _describe_selector(selector)
        passed = times.count >= 2 and abs(times.mean - expected_ms) <= expected_ms * tolerance
        self.check(passed, f'cycle time of {name} {expected_ms:g} ms ± {tolerance * 100:g} %: {times}')
        return times

    # Diagnostics

    @property
    def uds(self):
        """UDS client (`can_explorer.core.uds.UdsClient`) on the module's `uds_request_id`/`uds_response_id`.

        Examples:
            ```python
            vin = await t.uds.read_data_by_identifier(0xF190)
            await t.uds.diagnostic_session_control(3)
            result = await t.uds.request(bytes([0x22, 0xF1, 0x90]))   # Negative responses as result
            ```
        """
        return self._runner._uds(self._module)
