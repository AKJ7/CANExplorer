import asyncio
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core.ethercat import SIMULATED
from can_explorer.gui.ethercat import EtherCatController

EDS = Path(__file__).parents[3] / 'examples' / 'canopen' / 'io_module.eds'


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
async def ethercat(qapp):
    controller = EtherCatController()
    assert controller.open_network(SIMULATED) == ''
    await wait_until(lambda: not controller.busy)
    yield controller
    controller.shutdown()


async def run(controller, error):
    assert error == ''
    await wait_until(lambda: not controller.busy)
    return controller.log[-1]


def test_adapters_include_the_simulated_network(qapp):
    controller = EtherCatController()
    assert controller.adapters[-1] == {'name': SIMULATED, 'description': 'Simulated network (no hardware)'}
    assert controller.sdo_read('1000', '0') == 'Select a slave'
    assert controller.scan() == 'Open a network first'
    controller.shutdown()


async def test_network_slaves_and_states(ethercat):
    assert ethercat.is_open and ethercat.adapter == SIMULATED
    assert ethercat.adapter_description == 'Simulated network (no hardware)'
    assert ethercat.log[-1]['text'] == 'simulated: 3 slaves'
    assert [(slave['position'], slave['name'], slave['state'], slave['coe']) for slave in ethercat.slaves] == [
        (1, 'SIM-Coupler', 'Pre-Op', False),
        (2, 'SIM-AI4', 'Pre-Op', True),
        (3, 'SIM-AO2', 'Pre-Op', True),
    ]
    assert ethercat.selected == 2  # The first slave with a CoE mailbox
    assert [state['name'] for state in ethercat.states] == ['Init', 'Pre-Op', 'Safe-Op']
    assert (await run(ethercat, ethercat.request_state(4, False)))['text'] == 'Slave 2 → Safe-Op'
    assert [slave['state'] for slave in ethercat.slaves] == ['Pre-Op', 'Safe-Op', 'Pre-Op']
    assert (await run(ethercat, ethercat.request_state(2, True)))['text'] == 'All slaves → Pre-Op'
    assert (await run(ethercat, ethercat.scan()))['text'] == 'Found 3 slaves'
    ethercat.close_network()
    await wait_until(lambda: not ethercat.is_open)
    assert ethercat.slaves == [] and ethercat.log[-1]['text'] == 'simulated closed'


async def test_object_dictionary_and_sdo(ethercat):
    ethercat.select(3)
    assert ethercat.objects == [] and ethercat.dictionary_source == ''
    entry = await run(ethercat, ethercat.sdo_read('1008', '0'))
    assert entry['text'] == 'Slave 3 0x1008:00 Manufacturer device name = 53 49 4D 2D 41 4F 32 "SIM-AO2"'
    assert (await run(ethercat, ethercat.read_dictionary()))[
        'text'
    ] == 'Read 21 entries of the object dictionary of slave 3'
    assert ethercat.dictionary_source == 'SDO information'
    assert {'index': '7000', 'subindex': '02', 'name': 'Outputs.Output 2'}.items() <= next(
        o for o in ethercat.objects if (o['index'], o['subindex']) == ('7000', '02')
    ).items()
    entry = await run(ethercat, ethercat.sdo_write('7000', '02', '-1200'))
    assert entry['text'] == 'Slave 3 0x7000:02 Outputs.Output 2 ← -1200'
    assert (await run(ethercat, ethercat.sdo_read('7000', '2')))['text'] == 'Slave 3 0x7000:02 Outputs.Output 2 = -1200'
    entry = await run(ethercat, ethercat.sdo_write('1008', '0', 'Renamed'))
    assert entry['error'] and 'read only' in entry['text']
    assert ethercat.sdo_write('7000', '02', '99999').startswith('INTEGER16 values')
    assert ethercat.sdo_read('zz', '0') == "Invalid hex index: 'zz'"

    # An out of range setting makes the terminal send an emergency
    await run(ethercat, ethercat.sdo_write('8000', '02', '5000'))
    await ethercat._poll_states(ethercat._master)
    assert [(e['code'], e['text']) for e in ethercat.emergencies] == [('0x6320', 'Data set')]
    assert ethercat.slaves[2]['emcy_count'] == 1

    # The dictionary belongs to the slave it was read for; an EDS file can stand in for another
    ethercat.select(2)
    assert ethercat.objects == [] and ethercat.emergencies == []
    assert ethercat.load_dictionary(QUrl.fromLocalFile(str(EDS))) == ''
    assert ethercat.dictionary_source == 'io_module.eds' and ethercat.objects
    ethercat.unload_dictionary()
    assert ethercat.objects == []

    ethercat.select(1)
    entry = await run(ethercat, ethercat.read_dictionary())
    assert entry['error'] and 'no CoE mailbox' in entry['text']


async def test_pdo_configuration(ethercat):
    await run(ethercat, ethercat.read_dictionary())
    assert (await run(ethercat, ethercat.read_pdo_configuration()))['text'] == 'Read 1 assigned PDOs of slave 2'
    [pdo] = ethercat.pdos
    assert (pdo['name'], pdo['kind'], pdo['bits']) == ('TxPDO 0x1A00', 'TxPDO', 64)
    assert [(v['object'], v['name'], v['value']) for v in pdo['variables']][:2] == [
        ('6000:01', 'Inputs.Input 1', '1000'),
        ('6000:02', 'Inputs.Input 2', '2000'),
    ]
    ethercat.select(3)
    assert ethercat.pdos == []


async def test_closing_reports_closed_only_when_closed(ethercat, monkeypatch):
    from can_explorer.core.ethercat import EtherCatMaster

    real_close = EtherCatMaster.close
    progress = {'started': False, 'done': False}

    async def slow_close(self):  # Closing takes a while, as on slow machines, before and after it starts
        await asyncio.sleep(0.1)
        closing = asyncio.ensure_future(real_close(self))
        progress['started'] = True  # The master now reports itself closed
        await asyncio.sleep(0.1)
        await closing
        progress['done'] = True

    monkeypatch.setattr(EtherCatMaster, 'close', slow_close)
    ethercat.close_network()
    ethercat.close_network()  # A second click while closing does nothing
    checked_after_start = False
    # Open throughout closing, also after the master started closing (follows the close, as sleeps are coarse on
    # Windows: 15.6 ms), and cannot be opened again meanwhile
    while not progress['done']:
        assert ethercat.is_open
        if progress['started'] and not checked_after_start:
            assert ethercat.open_network(SIMULATED) == 'The network is still closing'
            checked_after_start = True
        await asyncio.sleep(0.01)
    assert checked_after_start
    await wait_until(lambda: not ethercat.is_open)
    assert ethercat.slaves == [] and ethercat.log[-1]['text'] == 'simulated closed'
    assert [entry['text'] for entry in ethercat.log].count('simulated closed') == 1
    assert ethercat.open_network(SIMULATED) == ''  # Can be opened again
    await wait_until(lambda: ethercat.is_open and not ethercat.busy)
