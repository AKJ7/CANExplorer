"""Promote reverse-engineering results to a signal database: turn the fields the analysis found into signal
definitions and write them, with their messages, into a DatabaseEditor.

Qt-free, so the GUI's "Promote to database" and the MCP interface can share the mapping and the writing. A found
field becomes a signal at its bit position, with its kind, details and (for multiplexed fields) condition in the
comment; multiplexing itself is not written, since the editor cannot express it.
"""

from typing import Any, Dict, List, Sequence

from can_explorer.core.database_editor import CAN_FD_LENGTHS, CAN_LENGTHS, DatabaseEditor, EditError
from can_explorer.core.reverse.analysis import IdAnalysis

KIND_NAMES = {
    'checksum': 'Checksum',
    'counter': 'Counter',
    'multiplexer': 'Multiplexer',
    'physical': 'Value',
    'enum': 'State',
    'flag': 'Flag',
    'constant': 'Constant',
    'unknown': 'Unknown',
}


def signal_definitions(
    analyses: Sequence[IdAnalysis], include_constant: bool = False, min_confidence: float = 0.0
) -> List[Dict[str, Any]]:
    """The fields found across `analyses` as signal definitions, strongest first wins any bit overlap (a database
    signal may not share bits with another, unlike the multiplexed candidates of the analysis)."""
    found = [
        (analysis, field)
        for analysis in analyses
        for field in analysis.fields
        if (include_constant or field.kind != 'constant') and field.confidence >= min_confidence
    ]
    found.sort(key=lambda item: -item[1].confidence)
    taken: Dict[tuple, set] = {}
    definitions = []
    for analysis, result in found:
        field = result.field
        key = (field.frame_id, field.extended)
        bits = set(field.bits)
        used = taken.setdefault(key, set())
        if bits & used:
            continue
        used |= bits
        comment = [KIND_NAMES.get(result.kind, result.kind)]
        if result.details:
            comment.append(result.details)
        if result.multiplexer is not None:
            comment.append(f'only in frames with {result.multiplexer.key} = {result.mux_value}')
        definitions.append(
            {
                'frame_id': field.frame_id,
                'extended': field.extended,
                'frame_length': max(analysis.length, field.min_length),
                'name': f'{KIND_NAMES.get(result.kind, "Signal")}_{field.id_text}_{field.start}',
                'start': field.start,
                'length': field.length,
                'byte_order': field.byte_order,
                'is_signed': field.signed,
                'scale': field.scale,
                'offset': field.offset,
                'unit': '',
                'comment': '; '.join(comment),
            }
        )
    definitions.sort(key=lambda definition: (definition['extended'], definition['frame_id'], definition['start']))
    return definitions


def _add_one(editor: DatabaseEditor, definition: Dict[str, Any]) -> str:
    """Add one signal (and its message, resizing the message when needed) to the editor; returns the message name."""
    frame_id, extended = int(definition['frame_id']), bool(definition.get('extended', False))
    needed = int(definition.get('frame_length', 8))
    message = next((m for m in editor.messages if m.frame_id == frame_id and m.is_extended_frame == extended), None)
    if message is None:
        id_text = f'{frame_id:08X}' if extended else f'{frame_id:03X}'
        names = {m.name for m in editor.messages}
        name, index = f'Message_{id_text}', 2
        while name in names:
            name, index = f'Message_{id_text}_{index}', index + 1
        name = editor.add_message(name, frame_id, extended)
        current_length = 0
    else:
        name, current_length = message.name, message.length
    if needed > current_length:
        fd = needed > 8 or (message is not None and message.is_fd)
        length = min(value for value in (CAN_FD_LENGTHS if fd else CAN_LENGTHS) if value >= min(needed, 64))
        name = editor.update_message(name, length=length, is_fd=fd)
    fields = {
        key: definition[key]
        for key in ('start', 'length', 'byte_order', 'is_signed', 'scale', 'offset', 'unit', 'comment')
        if key in definition
    }
    signal = None
    try:
        # add_signal places the signal at a default position; update_signal moves it to its real one. Defer the
        # validation so only the final position is checked, not the (possibly overlapping) default one.
        with editor.deferred():
            signal = editor.add_signal(name, definition.get('name') or None)
            editor.update_signal(name, signal, **fields)
    except Exception as e:  # Invalid or overlapping signal: undo and report
        if signal is not None:
            editor.remove_signal(name, signal)
        raise EditError(str(e)) from e
    return name


def write_definitions(editor: DatabaseEditor, definitions: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Add every definition to the editor; returns counts of signals added, messages touched and skipped ones."""
    signals = 0
    skipped = 0
    messages: set = set()
    for definition in definitions:
        try:
            int(definition['frame_id'])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        try:
            messages.add(_add_one(editor, dict(definition)))
            signals += 1
        except (EditError, ValueError):
            skipped += 1
    return {'signals': signals, 'messages': len(messages), 'skipped': skipped}
