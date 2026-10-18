"""ECU test automation.

Test scripts are Python files defining `TestModule` subclasses with `@testcase` methods::

    from can_explorer.testing import TestModule, testcase

    class EngineTests(TestModule):
        \"\"\"Engine control unit on the vehicle bus.\"\"\"

        @testcase
        async def engine_data_is_cyclic(self, t):
            await t.expect_cycle_time('EngineData', 100, tolerance=0.1)

        @testcase
        async def speed_follows_the_target(self, t):
            t.variables['target_speed'] = 3000
            await t.expect_signal('EngineData.EngineSpeed', lambda rpm: rpm > 2500, timeout=3)

They run in the Tests view of a connection, or headless with `can-test` (see docs/user-guide/test-automation.md).
"""

from can_explorer.testing.api import (
    CheckFailed,
    CycleTimes,
    Step,
    TestContext,
    TestModule,
    TestSkipped,
    Verdict,
    testcase,
)
from can_explorer.testing.report import to_html, to_junit, write_report
from can_explorer.testing.runner import (
    LoadedTestScript,
    TestCaseInfo,
    TestCaseResult,
    TestModuleInfo,
    TestReport,
    TestRunner,
    load_test_script,
)

__all__ = [
    'CheckFailed',
    'CycleTimes',
    'LoadedTestScript',
    'Step',
    'TestCaseInfo',
    'TestCaseResult',
    'TestContext',
    'TestModule',
    'TestModuleInfo',
    'TestReport',
    'TestRunner',
    'TestSkipped',
    'Verdict',
    'load_test_script',
    'testcase',
    'to_html',
    'to_junit',
    'write_report',
]
