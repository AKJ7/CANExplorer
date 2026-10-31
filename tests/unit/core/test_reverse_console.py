import can

from can_explorer.core.reverse.console import build_namespace, run


def frame(can_id, data, timestamp, extended=False):
    return can.Message(arbitration_id=can_id, is_extended_id=extended, data=bytes(data), timestamp=timestamp)


def sample_messages():
    messages = []
    for i in range(60):
        value = i * 400  # A rising 16-bit value in bytes 2-3
        messages.append(frame(0x100, [i & 0xFF, 0x55, value & 0xFF, (value >> 8) & 0xFF], i * 0.01))
        messages.append(frame(0x200, [0x55, 0xAA], i * 0.01))
    return messages


def test_expression_value_and_print_output():
    namespace = build_namespace(sample_messages())
    result = run('print("hi"); 1 + 2', namespace)
    assert result.ok and result.value == '3' and result.output.strip() == 'hi'


def test_variables_persist_across_runs():
    namespace = build_namespace(sample_messages())
    assert run('x = 40', namespace).value == ''  # An assignment has no value
    assert run('x + 2', namespace).value == '42'


def test_primitives_and_data_are_available():
    namespace = build_namespace(sample_messages())
    assert run('sorted(ids())', namespace).value == '[(256, False), (512, False)]'
    assert run('len(frames_of("100").payloads)', namespace).value == '60'
    # analyse finds byte 0 as a counter of 0x100
    result = run('a = analyse(frames_of("100")); [f.kind for f in a.fields]', namespace)
    assert result.ok and 'counter' in result.value
    assert run('BitField(0x100, False, 0, 8).key', namespace).value == "'100[0|8@1+]'"


def test_errors_are_captured_not_raised():
    namespace = build_namespace(sample_messages())
    syntax = run('1 +', namespace)
    assert not syntax.ok and 'SyntaxError' in syntax.error
    runtime = run('frames_of("999").payloads', namespace)  # None has no .payloads
    assert not runtime.ok and 'AttributeError' in runtime.error


def test_output_is_truncated():
    namespace = build_namespace([])
    result = run('"x" * 1_000_000', namespace)
    assert result.ok and len(result.value) < 50_000 and 'more characters' in result.value
