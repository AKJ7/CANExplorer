import asyncio

import pytest

from can_explorer.core.physical_io import SIMULATED, open_device
from can_explorer.gui.physical_io import PhysicalIoController


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
async def io(qapp):
    controller = PhysicalIoController()
    assert controller.devices[-1]['id'] == SIMULATED
    assert controller.open_device(SIMULATED) == ''
    await wait_until(lambda: controller.is_open and not controller.busy)
    await controller._poll_once(controller._io)
    yield controller
    controller.shutdown()


async def done(controller, error):
    assert error == ''
    count = len(controller.log)
    await wait_until(lambda: len(controller.log) > count)
    await asyncio.sleep(0.01)  # The poll after the operation
    return controller.log[-1]


async def test_lines_outputs_and_inputs(io):
    assert io.device_name == 'Simulated I/O'
    assert io.device_details == '8 lines, 4 analogue inputs (12 bit), firmware 1.0.0, hardware 1'
    lines = io.lines
    assert len(lines) == 8 and not any(line['output'] for line in lines)
    assert [line['can_output'] for line in lines] == [True] * 4 + [False] * 4

    assert (await done(io, io.configure_line(0, True, 'none', False)))['text'] == 'Line 0: output'
    assert io.lines[0]['output'] and not io.lines[4]['level']
    assert (await done(io, io.set_output(0, True)))['text'] == 'Line 0 → high'
    assert io.lines[0]['level'] and io.lines[0]['requested'] and io.lines[4]['level']  # Wired to line 0

    entry = await done(io, io.configure_line(5, False, 'up', True))
    assert entry['text'] == 'Line 5: input, pull-up, active low' and not io.lines[5]['level']
    assert (io.lines[5]['pull'], io.lines[5]['active_low']) == ('up', True)

    entry = await done(io, io.configure_line(6, True, 'none', False))
    assert entry['error'] and entry['text'] == 'Line 6 cannot be an output'
    entry = await done(io, io.set_output(3, True))
    assert entry['error'] and 'not an output' in entry['text']
    assert io.configure_line(1, False, 'sideways', False) == "Unknown pull 'sideways'"

    await done(io, io.configure_line(1, True, 'none', False))
    assert (await done(io, io.set_all_outputs(True)))['text'] == 'All outputs → high'
    assert io.lines[1]['level'] and io.lines[5]['level'] is False  # Line 5 is active low
    await done(io, io.set_all_outputs(False))
    assert not io.lines[0]['level'] and not io.lines[1]['level']


async def test_analogue_values_and_settings(io):
    for _ in range(3):
        await io._poll_once(io._io)
    analog = io.analog
    assert [channel['channel'] for channel in analog] == [0, 1, 2, 3]
    first = analog[0]
    assert first['volts'] == pytest.approx(first['raw'] * 3.3 / 4095) and len(first['history']) == 4
    io.set_reference_voltage(5.0)
    assert io.analog[0]['volts'] == pytest.approx(io.analog[0]['raw'] * 5.0 / 4095)
    io.set_poll_interval(5)
    assert io.poll_interval == 20  # Clamped
    assert (await done(io, io.identify()))['text'] == 'Identify sent to Simulated I/O'


async def test_open_errors_and_unplugging(qapp):
    def opener(device_id):
        if device_id == 'aio:missing':
            raise ConnectionError('aio:missing is not connected')
        return open_device(device_id)

    io = PhysicalIoController(opener=opener)
    try:
        assert io.set_output(0, True) == 'Open a device first'
        assert io.open_device('aio:missing') == ''
        await wait_until(lambda: not io.busy)
        assert not io.is_open and io.log[-1] == {'text': 'aio:missing is not connected', 'error': True}

        io.open_device(SIMULATED)
        await wait_until(lambda: io.is_open)

        def unplugged():
            raise ConnectionError('No such device')

        io._io.backend.read_status = unplugged
        for _ in range(3):
            await io._poll_once(io._io)
        assert not io.is_open and io.log[-2]['text'] == 'Simulated I/O stopped answering: No such device'
        assert io.log[-1]['text'] == 'Closed Simulated I/O'
    finally:
        io.shutdown()
