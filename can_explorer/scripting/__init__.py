"""Node simulation and scripting.

Simulation scripts are Python files defining `Node` subclasses::

    from can_explorer.scripting import Node, on_message, on_start, on_variable

    class Engine(Node):
        @on_start
        def start(self):
            self.status = self.message('EngineData')   # Signal values, encoded with the signal database
            self.every(0.1, self.send_status)

        def send_status(self):
            self.status['EngineSpeed'] = self.variables.get('target_speed', 800)
            self.send(self.status)

        @on_message('EngineControl')                    # By database name, or by identifier: 0x200
        def control(self, msg):
            self.log('target', msg.signals['TargetSpeed'])

See docs/scripting.md for the complete API.
"""

from can_explorer.scripting.api import (
    MessageBuffer,
    Node,
    ReceivedMessage,
    ScriptError,
    Timer,
    on_message,
    on_signal,
    on_start,
    on_stop,
    on_variable,
)
from can_explorer.scripting.runtime import LoadedScript, ScriptRuntime, Variables

__all__ = [
    'LoadedScript',
    'MessageBuffer',
    'Node',
    'ReceivedMessage',
    'ScriptError',
    'ScriptRuntime',
    'Timer',
    'Variables',
    'on_message',
    'on_signal',
    'on_start',
    'on_stop',
    'on_variable',
]
