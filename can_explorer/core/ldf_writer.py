"""Reading and writing LIN description files (LDF) as dictionaries.

The dictionary is ldfparser's representation of the file (`ldfparser.parse_ldf_to_dict`): the database editor
changes it, `ldf_text` writes it back. Comments of the original file are not kept.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List

import ldfparser
from lark import Lark
from ldfparser.grammar import LdfTransformer

INDENT = '  '


@lru_cache(maxsize=1)
def _parser() -> Lark:
    grammar = (Path(ldfparser.__file__).parent / 'grammars' / 'ldf.lark').read_text(encoding='utf-8')
    return Lark(grammar=grammar, parser='lalr')


def parse_ldf_text(text: str) -> Dict[str, Any]:
    """Parse LDF text into ldfparser's dictionary; raises lark errors (with line and column) on bad syntax."""
    return LdfTransformer().transform(_parser().parse(text))


def _number(value: Any) -> str:
    """Integers as such, other numbers without float noise (0.1 rather than 0.10000000000000001)."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f'{round(value, 9):.9g}'
    return str(value)


def _ms(seconds: float) -> str:
    return _number(round(seconds * 1000, 9))


def _string(text: str) -> str:
    return '"' + str(text).replace('"', "'") + '"'


def _hex(value: int) -> str:
    return f'0x{value:X}'


def _init_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return '{' + ', '.join(_number(item) for item in value) + '}'
    return _number(value)


def _schedule_command(command: Dict[str, Any]) -> str:
    kind = command['type']
    if kind == 'frame':
        return command['frame']
    if kind == 'master_request':
        return 'MasterReq'
    if kind == 'slave_response':
        return 'SlaveResp'
    if kind == 'assign_nad':
        return f'AssignNAD {{ {command["node"]} }}'
    if kind == 'conditional_change_nad':
        values = ', '.join(_hex(command[key]) for key in ('nad', 'id', 'byte', 'mask', 'inv', 'new_nad'))
        return f'ConditionalChangeNAD {{ {values} }}'
    if kind == 'data_dump':
        return f'DataDump {{ {command["node"]}, {", ".join(_hex(value) for value in command["data"])} }}'
    if kind == 'save_configuration':
        return f'SaveConfiguration {{ {command["node"]} }}'
    if kind == 'assign_frame_id_range':
        values = [command['node'], _number(command['frame_index']), *(_hex(pid) for pid in command.get('pids', []))]
        return f'AssignFrameIdRange {{ {", ".join(values)} }}'
    if kind == 'assign_frame_id':
        return f'AssignFrameId {{ {command["node"]}, {command["frame"]} }}'
    if kind == 'unassign_frame_id':
        return f'UnassignFrameId {{ {command["node"]}, {command["frame"]} }}'
    if kind == 'free_format':
        return f'FreeFormat {{ {", ".join(_hex(value) for value in command["data"])} }}'
    raise ValueError(f'Unknown schedule command {kind!r}')


def schedule_entry_text(entry: Dict[str, Any]) -> str:
    return f'{_schedule_command(entry["command"])} delay {_ms(entry["delay"])} ms;'


def encoding_value_text(value: Dict[str, Any]) -> str:
    kind = value['type']
    if kind == 'logical':
        text = f', {_string(value["text"])}' if value.get('text') is not None else ''
        return f'logical_value, {_number(value["value"])}{text};'
    if kind == 'physical':
        unit = f', {_string(value["unit"])}' if value.get('unit') else ''
        numbers = ', '.join(_number(value[key]) for key in ('min', 'max', 'scale', 'offset'))
        return f'physical_value, {numbers}{unit};'
    if kind == 'bcd':
        return 'bcd_value;'
    if kind == 'ascii':
        return 'ascii_value;'
    raise ValueError(f'Unknown encoding value {kind!r}')


def _block(name: str, lines: Iterable[str], level: int = 0) -> List[str]:
    lines = list(lines)
    pad = INDENT * level
    return [f'{pad}{name} {{', *(f'{pad}{INDENT}{line}' for line in lines), f'{pad}}}']


def _frame_signals(signals: List[Dict[str, Any]]) -> List[str]:
    return [f'{signal["signal"]}, {_number(signal["offset"])};' for signal in signals]


def _node_attributes(node: Dict[str, Any]) -> List[str]:
    lines = []
    if 'lin_protocol' in node:
        lines.append(f'LIN_protocol = {_string(node["lin_protocol"])};')
    for key, keyword in (('configured_nad', 'configured_NAD'), ('initial_nad', 'initial_NAD')):
        if node.get(key) is not None:
            lines.append(f'{keyword} = {_hex(node[key])};')
    if node.get('product_id'):
        product = node['product_id']
        values = [_hex(product['supplier_id']), _hex(product['function_id'])]
        if product.get('variant'):
            values.append(_number(product['variant']))
        lines.append(f'product_id = {", ".join(values)};')
    if node.get('response_error'):
        lines.append(f'response_error = {node["response_error"]};')
    if node.get('fault_state_signals'):
        lines.append(f'fault_state_signals = {", ".join(node["fault_state_signals"])};')
    for key in ('P2_min', 'ST_min', 'N_As_timeout', 'N_Cr_timeout'):
        if node.get(key) is not None:
            lines.append(f'{key} = {_ms(node[key])} ms;')
    frames = node.get('configurable_frames')
    if isinstance(frames, dict):  # LIN 2.0: frames with their message identifiers
        lines += _block('configurable_frames', (f'{frame} = {_hex(msg_id)};' for frame, msg_id in frames.items()))
    elif frames:
        lines += _block('configurable_frames', (f'{frame};' for frame in frames))
    if node.get('response_tolerance') is not None:
        lines.append(f'response_tolerance = {_number(round(node["response_tolerance"] * 100, 9))} %;')
    for key in ('wakeup_time', 'poweron_time'):
        if node.get(key) is not None:
            lines.append(f'{key} = {_ms(node[key])} ms;')
    return lines


def ldf_text(data: Dict[str, Any]) -> str:  # noqa: C901 (one section after the other)
    """The LDF file of a dictionary in ldfparser's format."""
    out = [
        'LIN_description_file;',
        f'LIN_protocol_version = {_string(data.get("protocol_version", "2.1"))};',
        f'LIN_language_version = {_string(data.get("language_version", "2.1"))};',
    ]
    if data.get('file_revision'):
        out.append(f'LDF_file_revision = {_string(data["file_revision"])};')
    out.append(f'LIN_speed = {_number(round(data.get("speed", 19200) / 1000, 9))} kbps;')
    if data.get('channel_name'):
        out.append(f'Channel_name = {_string(data["channel_name"])};')
    if data.get('endianness'):
        out.append(f'LIN_sig_byte_order_{"big" if data["endianness"] == "big" else "little"}_endian;')
    out.append('')

    nodes = data.get('nodes', {})
    node_lines = []
    master = nodes.get('master')
    if master:
        parts = [master['name'], f'{_ms(master["timebase"])} ms', f'{_ms(master["jitter"])} ms']
        # SAE J2602: header length and response tolerance, only together
        if master.get('max_header_length') is not None and master.get('response_tolerance') is not None:
            parts.append(f'{_number(master["max_header_length"])} bits')
            parts.append(f'{_number(round(master["response_tolerance"] * 100, 9))} %')
        node_lines.append(f'Master: {", ".join(parts)};')
    if nodes.get('slaves'):
        node_lines.append(f'Slaves: {", ".join(nodes["slaves"])};')
    out += _block('Nodes', node_lines) + ['']

    if data.get('node_compositions'):
        lines = []
        for configuration in data['node_compositions']:
            lines += _block(
                f'configuration {configuration["name"]}',
                (
                    f'{composite["name"]} {{ {", ".join(composite["nodes"])} }}'
                    for composite in configuration['compositions']
                ),
            )
        out += _block('composite', lines) + ['']

    signal_lines = []
    for signal in data.get('signals', []):
        parts = [
            _number(signal['width']),
            _init_value(signal['init_value']),
            signal['publisher'],
            *signal.get('subscribers', []),
        ]
        signal_lines.append(f'{signal["name"]}: {", ".join(parts)};')
    out += _block('Signals', signal_lines) + ['']

    if data.get('diagnostic_signals'):
        lines = [
            f'{signal["name"]}: {_number(signal["width"])}, {_init_value(signal["init_value"])};'
            for signal in data['diagnostic_signals']
        ]
        out += _block('Diagnostic_signals', lines) + ['']

    frame_lines = []
    for frame in data.get('frames', []):
        header = f'{frame["name"]}: {_hex(frame["frame_id"])}, {frame["publisher"]}'
        if frame.get('length') is not None:
            header += f', {_number(frame["length"])}'
        frame_lines += _block(header, _frame_signals(frame.get('signals', [])))
    out += _block('Frames', frame_lines) + ['']

    if data.get('sporadic_frames'):
        lines = [f'{frame["name"]}: {", ".join(frame["frames"])};' for frame in data['sporadic_frames']]
        out += _block('Sporadic_frames', lines) + ['']

    if data.get('event_triggered_frames'):
        lines = []
        for frame in data['event_triggered_frames']:
            table = frame.get('collision_resolving_schedule_table')
            prefix = f'{table}, ' if table else ''  # Optional (LIN 2.0 has none)
            lines.append(f'{frame["name"]}: {prefix}{_hex(frame["frame_id"])}, {", ".join(frame["frames"])};')
        out += _block('Event_triggered_frames', lines) + ['']

    if data.get('diagnostic_frames'):
        lines = []
        for frame in data['diagnostic_frames']:
            lines += _block(f'{frame["name"]}: {_hex(frame["frame_id"])}', _frame_signals(frame.get('signals', [])))
        out += _block('Diagnostic_frames', lines) + ['']

    if data.get('diagnostic_addresses'):
        lines = [f'{node}: {_hex(address)};' for node, address in data['diagnostic_addresses'].items()]
        out += _block('Diagnostic_addresses', lines) + ['']

    if data.get('node_attributes') is not None:  # Required in LIN 2.x files, also when empty
        lines = []
        for node in data['node_attributes']:
            lines += _block(node['name'], _node_attributes(node))
        out += _block('Node_attributes', lines) + ['']

    if data.get('schedule_tables') is not None:
        lines = []
        for table in data['schedule_tables']:
            lines += _block(table['name'], (schedule_entry_text(entry) for entry in table['schedule']))
        out += _block('Schedule_tables', lines) + ['']

    if data.get('signal_groups'):
        lines = []
        for group in data['signal_groups']:
            lines += _block(
                f'{group["name"]}: {_number(group["size"])}',
                (f'{signal}, {_number(offset)};' for signal, offset in group['signals'].items()),
            )
        out += _block('Signal_groups', lines) + ['']

    if data.get('signal_encoding_types'):
        lines = []
        for encoding in data['signal_encoding_types']:
            lines += _block(encoding['name'], (encoding_value_text(value) for value in encoding['values']))
        out += _block('Signal_encoding_types', lines) + ['']

    representations = [entry for entry in data.get('signal_representations', []) if entry['signals']]
    if representations:
        lines = [f'{entry["encoding"]}: {", ".join(entry["signals"])};' for entry in representations]
        out += _block('Signal_representation', lines) + ['']

    return '\n'.join(out).rstrip() + '\n'
