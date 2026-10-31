"""An interactive Python console over a snapshot of the trace, exposing the reverse-engineering primitives.

This gives a power user the same reach the MCP service (mcp/) has over a bus, and the simulation scripts
(scripting/) have over the live connection: run an ad-hoc frame processor or field hunt with the richer
primitives of ``can_explorer.core.reverse`` — ``collect``, ``analyse``, ``BitField``, ``candidates``,
``signal_groups`` — over the frames captured so far.

It is not a sandbox. It runs arbitrary Python in-process, like the two features above; it is meant for the tool's
own user exploring their own capture, not for untrusted input.
"""

import ast
import contextlib
import io
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import can

from can_explorer.core.reverse.analysis import IdAnalysis, IdFrames, analyse, analyse_all, collect
from can_explorer.core.reverse.fields import BitField
from can_explorer.core.reverse.reference import Reference, candidates, correlate, read_reference_csv
from can_explorer.core.reverse.related import signal_groups

MAX_OUTPUT = 20_000  # Characters of captured output or of a value's repr kept per run

HELP = """Interactive reverse-engineering console. Runs Python over the frames captured so far.

Data:
  messages  - the captured frames (list of can.Message)
  frames    - collect(messages): {(id, extended): IdFrames} with .times and .payloads
  analyses  - the fields found by the Automatic analysis tab (list of IdAnalysis)
  fields    - every FoundField of `analyses`, flattened
Helpers:
  ids()                      - the identifier keys, sorted
  frames_of(id, extended=False) - the IdFrames of one identifier (id is int or 'hex')
Primitives (can_explorer.core.reverse):
  collect, analyse, analyse_all, BitField, candidates, signal_groups, correlate, read_reference_csv
The value of the last line is shown, like a Python REPL. print() also works. Type HELP for this text.

Example:
  a = analyse(frames_of('100'))
  [(f.field.key, f.kind) for f in a.fields]
"""


@dataclass
class ConsoleResult:
    output: str = ''  # Captured stdout/stderr
    value: str = ''  # repr() of the last expression's value, when it is not None
    error: str = ''  # Traceback text, when the snippet raised

    @property
    def ok(self) -> bool:
        return not self.error


# The names build_namespace() puts in scope, for the console's syntax highlighting
CONSOLE_NAMES = (
    'messages',
    'frames',
    'analyses',
    'fields',
    'ids',
    'frames_of',
    'collect',
    'analyse',
    'analyse_all',
    'BitField',
    'candidates',
    'signal_groups',
    'correlate',
    'read_reference_csv',
    'Reference',
    'can',
    'HELP',
)


def _key(identifier: Any, extended: bool) -> tuple:
    frame_id = int(identifier, 16) if isinstance(identifier, str) else int(identifier)
    return (frame_id, extended)


def build_namespace(messages: Sequence[can.Message], analyses: Sequence[IdAnalysis] = ()) -> Dict[str, Any]:
    """The names available to a console snippet: the captured frames, the found fields and the primitives."""
    frames = collect(messages)
    analyses = list(analyses)

    def ids() -> List[tuple]:
        return sorted(frames, key=lambda key: (key[1], key[0]))

    def frames_of(identifier: Any, extended: bool = False) -> Optional[IdFrames]:
        return frames.get(_key(identifier, extended))

    return {
        'messages': list(messages),
        'frames': frames,
        'analyses': analyses,
        'fields': [found for analysis in analyses for found in analysis.fields],
        'ids': ids,
        'frames_of': frames_of,
        'collect': collect,
        'analyse': analyse,
        'analyse_all': analyse_all,
        'BitField': BitField,
        'candidates': candidates,
        'signal_groups': signal_groups,
        'correlate': correlate,
        'read_reference_csv': read_reference_csv,
        'Reference': Reference,
        'can': can,
        'HELP': HELP,
    }


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f'\n… ({len(text) - MAX_OUTPUT} more characters)'


def run(source: str, namespace: Dict[str, Any]) -> ConsoleResult:
    """Execute a snippet against `namespace` (kept across calls, so variables persist). The value of a trailing
    expression is returned as its repr, as a REPL would; stdout and stderr are captured."""
    source = source.strip()
    if not source:
        return ConsoleResult()
    try:
        tree = ast.parse(source, '<console>', 'exec')
    except SyntaxError as e:
        return ConsoleResult(error=f'SyntaxError: {e.msg} (line {e.lineno})')
    last_expression: Optional[ast.Expression] = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expression = ast.Expression(tree.body.pop().value)
        ast.copy_location(last_expression, last_expression.body)
    captured = io.StringIO()
    value = ''
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            if tree.body:
                exec(compile(tree, '<console>', 'exec'), namespace)
            if last_expression is not None:
                result = eval(compile(last_expression, '<console>', 'eval'), namespace)
                if result is not None:
                    value = _truncate(repr(result))
    except Exception:
        lines = traceback.format_exc().splitlines()
        # Drop the frames of this module (compile/exec), keeping the snippet's own traceback
        trimmed = [line for line in lines if '<console>' in line or not line.strip().startswith('File "')]
        return ConsoleResult(output=_truncate(captured.getvalue()), error=_truncate('\n'.join(trimmed)))
    return ConsoleResult(output=_truncate(captured.getvalue()), value=value)
