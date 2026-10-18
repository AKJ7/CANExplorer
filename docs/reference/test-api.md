# Test API

Test scripts import their API from `can_explorer.testing`:

```python
from can_explorer.testing import TestModule, testcase
```

See [Test automation](../user-guide/test-automation.md) for how test scripts are written and run.

## Test modules and test cases

::: can_explorer.testing.api.TestModule
    options:
      members:
        - setup
        - teardown

::: can_explorer.testing.api.testcase

## Test context

::: can_explorer.testing.api.TestContext
    options:
      members:
        - elapsed
        - log
        - check
        - check_equal
        - check_range
        - require
        - fail
        - skip
        - sleep
        - variables
        - message
        - send
        - wait_for_message
        - expect_message
        - expect_no_message
        - wait_for_signal
        - expect_signal
        - collect
        - measure_cycle_time
        - expect_cycle_time
        - uds

## Results

::: can_explorer.testing.api.CycleTimes

::: can_explorer.testing.api.Verdict

Received frames are [`ReceivedMessage`](scripting-api.md#messages) objects, as in simulation scripts.
