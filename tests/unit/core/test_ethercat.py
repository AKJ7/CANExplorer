import sys
import types

import pytest

from can_explorer.core.canopen_tools import SdoAbort, decode_value
from can_explorer.core.ethercat import (
    SIMULATED,
    STATE_ERROR,
    STATE_INIT,
    STATE_OP,
    STATE_PREOP,
    STATE_SAFEOP,
    EtherCatMaster,
    SimulatedBackend,
    SoemBackend,
    access_text,
    al_status_text,
    state_name,
)


@pytest.fixture
async def master():
    master = EtherCatMaster(SimulatedBackend())
    await master.open(SIMULATED)
    yield master
    await master.close()
    master.shutdown()


def test_texts():
    assert state_name(STATE_PREOP) == 'Pre-Op' and state_name(STATE_SAFEOP | STATE_ERROR) == 'Safe-Op + Error'
    assert al_status_text(0x001B) == 'Sync manager watchdog' and al_status_text(0x0999) == 'AL status 0x0999'
    assert access_text(0x07) == 'ro' and access_text(0x3F) == 'rw' and access_text(0x38) == 'wo'


async def test_scan_and_sdo_transfers(master):
    assert [(slave.position, slave.name, slave.coe) for slave in master.slaves] == [
        (1, 'SIM-Coupler', False),
        (2, 'SIM-AI4', True),
        (3, 'SIM-AO2', True),
    ]
    assert all(slave.state == STATE_PREOP for slave in master.slaves)
    assert await master.sdo_read(2, 0x1008, 0) == b'SIM-AI4'
    assert decode_value(0x07, await master.sdo_read(3, 0x1018, 2)) == 3
    await master.sdo_write(3, 0x7000, 1, (-5).to_bytes(2, 'little', signed=True))
    assert decode_value(0x03, await master.sdo_read(3, 0x7000, 1)) == -5
    with pytest.raises(SdoAbort, match='read only'):
        await master.sdo_write(2, 0x6000, 1, b'\x00\x00')
    with pytest.raises(SdoAbort, match='does not exist'):
        await master.sdo_read(2, 0x2000, 0)
    with pytest.raises(SdoAbort, match='Sub-index'):
        await master.sdo_read(2, 0x1018, 9)
    with pytest.raises(ConnectionError, match='no CoE mailbox'):
        await master.sdo_read(1, 0x1000, 0)


async def test_states_and_emergencies(master):
    await master.request_state(2, STATE_SAFEOP)
    assert [slave.state for slave in master.slaves] == [STATE_PREOP, STATE_SAFEOP, STATE_PREOP]
    await master.request_state(0, STATE_INIT)
    assert all(slave.state == STATE_INIT for slave in master.slaves)
    with pytest.raises(ConnectionError, match='no CoE mailbox, or not in Pre-Op'):
        await master.sdo_read(2, 0x1000, 0)  # No mailbox in Init
    with pytest.raises(ValueError, match='cannot request Op'):
        await master.request_state(0, STATE_OP)
    await master.request_state(0, STATE_PREOP)
    await master.sdo_write(3, 0x8000, 2, (5000).to_bytes(2, 'little'))  # Out of range: an emergency
    assert [(emcy.position, emcy.code) for emcy in master.emergencies] == [(3, 0x6320)]


async def test_object_dictionary_and_pdo_assignment(master):
    od = await master.object_dictionary(3)
    assert od.entry(0x7000, 2).full_name == 'Outputs.Output 2' and od.entry(0x7000, 2).pdo_mappable
    pdos = await master.pdo_assignment(3, od)
    assert [(pdo.name, pdo.bits) for pdo in pdos] == [('RxPDO 0x1600', 32)]
    assert [(v.index, v.subindex, v.bits, v.name) for v in pdos[0].variables] == [
        (0x7000, 1, 16, 'Outputs.Output 1'),
        (0x7000, 2, 16, 'Outputs.Output 2'),
    ]
    pdos = await master.pdo_assignment(2)  # Without a dictionary: indices only
    assert [(pdo.name, [v.name for v in pdo.variables]) for pdo in pdos] == [
        ('TxPDO 0x1A00', ['0x6000 sub 1', '0x6000 sub 2', '0x6000 sub 3', '0x6000 sub 4'])
    ]


# SoemBackend with a stand-in for pysoem: the hardware path without hardware


class FakeSdoError(Exception):
    def __init__(self, slave_pos, index, subindex, abort_code, desc):
        super().__init__(desc)
        self.abort_code = abort_code


class FakeWkcError(Exception):
    pass


class FakeMailboxError(Exception):
    def __init__(self, slave_pos, error_code, desc):
        super().__init__(desc)
        self.desc = desc


class FakeSdoInfoError(Exception):
    pass


class FakeSlave:
    def __init__(self, name, objects=None, fail=None):
        self.name, self.man, self.id, self.rev = name, 0x2, 0x07D83052, 0x00120000
        self.state, self.al_status = STATE_PREOP, 0
        self.objects, self.fail = objects or {}, fail
        self.callbacks = []
        self.written = {}

    def add_emergency_callback(self, callback):
        self.callbacks.append(callback)

    def sdo_read(self, index, subindex):
        if self.fail is not None:
            raise self.fail
        if (index, subindex) not in self.objects:
            raise FakeSdoError(1, index, subindex, 0x06020000, 'Object does not exist')
        return self.objects[(index, subindex)]

    def sdo_write(self, index, subindex, data):
        self.written[(index, subindex)] = data

    def write_state(self):
        if self.state == STATE_SAFEOP and self.name == 'Stubborn':
            self.state, self.al_status = STATE_PREOP | STATE_ERROR, 0x001D

    def state_check(self, state, timeout):
        return self.state

    @property
    def od(self):
        if self.fail is not None:
            raise FakeSdoInfoError('No SDO info')
        entry = types.SimpleNamespace
        return [
            entry(index=0x1008, name='Device name', object_code=7, data_type=0x09, obj_access=0x07, entries=[]),
            entry(
                index=0x7000,
                name='Outputs',
                object_code=9,
                data_type=0,
                obj_access=0,
                entries=[
                    entry(name='Count', data_type=0x05, bit_length=8, obj_access=0x07),
                    entry(name='', data_type=0, bit_length=0, obj_access=0),  # A gap
                    entry(name='Output 2', data_type=0x03, bit_length=16, obj_access=0xBF),
                ],
            ),
        ]


class FakeMaster:
    def __init__(self, slaves):
        self.slaves, self.mapped, self.closed = slaves, 0, False

    def open(self, adapter):
        if adapter != 'eth0':
            raise ConnectionError('could not open interface')

    def close(self):
        self.closed = True

    def config_init(self):
        return len(self.slaves) or -1

    def config_map(self):
        self.mapped += 1

    def read_state(self):
        return min((slave.state for slave in self.slaves), default=0)


@pytest.fixture
def soem(monkeypatch):
    slaves = [
        FakeSlave('EL1008 8Ch. Dig. Input', {(0x1008, 0): b'EL1008\x00'}),
        FakeSlave('Stubborn'),
        FakeSlave('Broken', fail=FakeMailboxError(3, 1, 'Mailbox not supported')),
    ]
    master = FakeMaster(slaves)
    module = types.SimpleNamespace(
        Master=lambda: master,
        find_adapters=lambda: [types.SimpleNamespace(name='eth0', desc=b'Onboard Ethernet')],
        SdoError=FakeSdoError,
        WkcError=FakeWkcError,
        MailboxError=FakeMailboxError,
        PacketError=type('FakePacketError', (Exception,), {}),
        SdoInfoError=FakeSdoInfoError,
    )
    monkeypatch.setitem(sys.modules, 'pysoem', module)
    return master


async def test_soem_backend(soem):
    backend = SoemBackend()
    assert backend.adapters() == [('eth0', 'Onboard Ethernet')]
    master = EtherCatMaster(backend)
    with pytest.raises(ConnectionError, match='raw network access'):
        await master.open('eth9')
    slaves = await master.open('eth0')
    try:
        assert [(slave.position, slave.name, slave.state) for slave in slaves] == [
            (1, 'EL1008 8Ch. Dig. Input', STATE_PREOP),
            (2, 'Stubborn', STATE_PREOP),
            (3, 'Broken', STATE_PREOP),
        ]
        assert await master.sdo_read(1, 0x1008, 0) == b'EL1008\x00'
        with pytest.raises(SdoAbort, match='does not exist'):
            await master.sdo_read(1, 0x2000, 0)
        with pytest.raises(ConnectionError, match='Mailbox error of slave 3: Mailbox not supported'):
            await master.sdo_read(3, 0x1000, 0)
        await master.sdo_write(1, 0x8000, 1, b'\x01')
        assert soem.slaves[0].written == {(0x8000, 1): b'\x01'}

        od = await master.object_dictionary(1)
        assert od.entry(0x1008).access == 'ro' and od.entry(0x1008).data_type == 0x09
        assert sorted(od.objects[0x7000].entries) == [0, 2]  # The gap at sub-index 1 is left out
        output = od.entry(0x7000, 2)
        assert (output.full_name, output.access, output.pdo_mappable) == ('Outputs.Output 2', 'rw', True)
        with pytest.raises(ConnectionError, match='load its EDS file'):
            await master.object_dictionary(3)

        # Emergencies arrive through pysoem's callbacks
        emcy = types.SimpleNamespace(error_code=0x8130, error_reg=0x11, b1=1, w1=0x0302, w2=0x0504)
        soem.slaves[0].callbacks[0](emcy)
        assert [(e.position, e.code, e.register, e.data) for e in master.emergencies] == [
            (1, 0x8130, 0x11, bytes([1, 2, 3, 4, 5]))
        ]

        with pytest.raises(ConnectionError, match='Slave 2 is in Pre-Op \\+ Error: Invalid output configuration'):
            await master.request_state(0, STATE_SAFEOP)
        assert soem.mapped == 1  # Safe-Op configures the process data mapping once
        await master.request_state(1, STATE_SAFEOP)
        assert soem.mapped == 1 and master.slaves[0].state == STATE_SAFEOP
    finally:
        await master.close()
        master.shutdown()
    assert soem.closed
