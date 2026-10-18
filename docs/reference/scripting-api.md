# Scripting API

Simulation scripts import their API from `can_explorer.scripting`:

```python
from can_explorer.scripting import Node, on_message, on_signal, on_start, on_stop, on_variable
```

See [Simulation and scripting](../user-guide/simulation.md) for how scripts are loaded and run.

## Nodes

::: can_explorer.scripting.api.Node
    options:
      members:
        - name
        - variables
        - time
        - message
        - send
        - every
        - after
        - log

::: can_explorer.scripting.api.Gateway
    options:
      members:
        - source
        - target
        - ids
        - both_directions
        - transform

## Handler decorators

::: can_explorer.scripting.api.on_start

::: can_explorer.scripting.api.on_stop

::: can_explorer.scripting.api.on_message

::: can_explorer.scripting.api.on_signal

::: can_explorer.scripting.api.on_variable

## Messages

::: can_explorer.scripting.api.ReceivedMessage

::: can_explorer.scripting.api.MessageBuffer
    options:
      members:
        - signal_names
        - update
        - encode

## Timers, variables and errors

::: can_explorer.scripting.api.Timer
    options:
      members:
        - running
        - cancel

::: can_explorer.scripting.runtime.Variables
    options:
      members:
        - get
        - set

::: can_explorer.scripting.api.ScriptError
