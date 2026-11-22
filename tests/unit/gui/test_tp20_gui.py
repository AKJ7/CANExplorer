import json
import uuid

import pytest
from PyQt6.QtCore import QUrl

from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.gui.connection_manager import ConnectionManager
from tests.unit.core.test_tp20 import GROUP_1, FakeModule
from tests.unit.gui.test_main_window import window  # noqa: F401 (fixture)
from tests.unit.waiting import wait_until

# Illustrative labels for the tests only
LABELS = {
    'modules': {
        '01': {
            'name': 'Engine',
            'formulas': {'0x1A': {'expression': 'a + b', 'unit': 'u'}},
            'groups': {'1': ['First']},
            'coding': {
                'Coding': {
                    'protocol': 'kwp',
                    'identifier': '0x9A',
                    'length': 3,
                    'fields': [
                        {'name': 'Option A', 'byte': 0, 'bit': 0, 'values': {'0': 'off', '1': 'on'}},
                        {'name': 'Variant', 'byte': 1, 'bit': 4, 'width': 4},
                    ],
                },
                'Unified': {'protocol': 'uds', 'identifier': '0x0600'},
            },
        }
    }
}


class Module:
    """A module answering a session, group 1, a coding record at 0x9A, security access and TesterPresent."""

    def __init__(self):
        self.coding = bytearray(b'\x01\x30\x07')
        self.unlocked = False
        self.tester_present = 0

    def __call__(self, request):
        request = bytes(request)
        if request == b'\x10\x89':
            return b'\x50\x89'
        if request == b'\x21\x01':
            return GROUP_1
        if request == b'\x21\x9a':
            return b'\x61\x9a' + bytes(self.coding)
        if request[:2] == b'\x3b\x9a':
            if not self.unlocked:
                return b'\x7f\x3b\x33'  # securityAccessDenied
            self.coding[:] = request[2:]
            return b'\x7b\x9a'
        if request == b'\x27\x01':
            return b'\x67\x01\x12\x34'
        if request[:2] == b'\x27\x02':
            self.unlocked = request[2:] == b'\xed\xcb'
            return b'\x67\x02' if self.unlocked else b'\x7f\x27\x35'
        if request == b'\x3e':
            self.tester_present += 1
            return b'\x7e'
        return b'\x7f' + request[:1] + b'\x11'


@pytest.fixture
def channel():
    return f'tp20-gui-{uuid.uuid4().hex}'


@pytest.fixture
def manager(qapp):
    manager = ConnectionManager()
    yield manager
    manager.close_all()


@pytest.fixture
async def ecu(channel):
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    yield session
    session.stop()


def open_connection(manager, channel):
    manager.add_connection({'name': 'VW', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[-1]
    connection.open()
    return connection


@pytest.fixture
def labels(tmp_path):
    path = tmp_path / 'labels.json'
    path.write_text(json.dumps(LABELS), encoding='utf-8')
    return path


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / 'key.py'
    path.write_text('def compute_key(level, seed):\n    return bytes(b ^ 0xFF for b in seed)\n', encoding='utf-8')
    return path


async def idle(tp20):
    assert await wait_until(lambda: not tp20.busy)


async def test_channel_blocks_coding_and_requests(manager, channel, ecu, labels, key_file):
    handler = Module()
    module = FakeModule(ecu, 0x01, handler=handler)
    connection = open_connection(manager, channel)
    tp20 = connection.tp20
    try:
        assert tp20.read_group('1') == 'Open a channel to the module first'
        assert tp20.load_labels(QUrl.fromLocalFile(str(labels))) == ''
        assert tp20.labels_name == 'labels.json'
        assert tp20.open_channel('01', '89') == ''
        await idle(tp20)
        assert tp20.channel_open and tp20.module_name == 'Engine' and not tp20.error
        assert tp20.channel_text == 'module 0x01: tester on 0x740, module on 0x300'
        assert tp20.open_channel('01', '89') == 'A channel is already open; close it first'
        assert tp20.scan('01-FF', '', '') == 'Close the channel first: the scan opens channels of its own'

        # Measuring blocks, scaled by the label file
        assert tp20.read_group('1') == ''
        await idle(tp20)
        block = tp20.block
        assert block['group'] == 1 and len(block['values']) == 8
        assert block['values'][0]['name'] == 'First' and block['values'][3]['text'] == f'{0x32 + 0x4B} u'
        assert tp20.read_group('300') == 'The group is a number, 0-255'
        assert tp20.set_live(True, '1') == '' and tp20.live
        assert await wait_until(lambda: len(module.requests) >= 5)
        tp20.set_live(False, '1')
        assert not tp20.live

        # Coding: read, edit, write (denied, then after security access)
        assert tp20.coding_records == ['Coding', 'Unified']
        assert 'UDS record' in tp20.read_coding('Unified', '')
        assert tp20.read_coding('Coding', '') == ''
        await idle(tp20)
        assert tp20.coding_hex == '01 30 07' and not tp20.coding_modified
        fields = {field['name']: field for field in tp20.coding_fields}
        assert fields['Option A']['options'] == [{'value': 0, 'text': '0 — off'}, {'value': 1, 'text': '1 — on'}]
        assert fields['Variant']['value'] == 3 and fields['Variant']['maximum'] == 15
        assert tp20.write_coding() == 'Nothing changed'
        assert tp20.set_field('Option A', 0) == '' and tp20.set_field('Variant', 9) == ''
        assert tp20.set_field('Variant', 16).startswith('Field')
        assert tp20.coding_modified and tp20.coding_hex == '00 90 07'
        assert tp20.coding_changes == ['Option A: 1 (on) → 0 (off)', 'Variant: 3 → 9']
        assert [f['changed'] for f in tp20.coding_fields] == [True, True]
        assert tp20.write_coding() == ''
        await idle(tp20)
        assert 'securityAccessDenied' in tp20.error and bytes(handler.coding) == b'\x01\x30\x07'

        assert tp20.security_access('02', str(key_file)) == 'The level is odd, 01-7F'
        assert tp20.security_access('01', QUrl.fromLocalFile(str(key_file)).toString()) == ''
        await idle(tp20)
        assert handler.unlocked and tp20.history[-1]['description'] == 'Level 1 unlocked'
        assert tp20.write_coding() == ''
        await idle(tp20)
        assert bytes(handler.coding) == b'\x00\x90\x07'
        assert tp20.coding_backup_hex == '01 30 07' and 'kept the new coding' in tp20.coding_status
        assert not tp20.coding_modified and tp20.coding_original_hex == '00 90 07'

        # Raw bytes editing and revert
        assert tp20.set_coding_bytes('01 02') == 'The record is 3 bytes, got 2'
        assert tp20.set_coding_bytes('01 30 07') == '' and tp20.coding_modified
        tp20.revert_coding()
        assert not tp20.coding_modified
        assert tp20.read_coding('', '9A') == ''
        await idle(tp20)
        assert tp20.coding_record == 'local identifier 0x9A (KWP2000 local identifier 0x9A)'
        assert tp20.coding_fields == []
        assert 'use MCP' in tp20.read_coding('', 'uds:0600')

        # Raw request and history
        assert tp20.raw_request('21 02') == ''
        await idle(tp20)
        assert tp20.history[-1]['response'] == '7F 21 11' and not tp20.history[-1]['positive']
        assert tp20.raw_request('') == 'Enter the request bytes, e.g. 21 01'
        tp20.clear_history()
        assert tp20.history == []

        tp20.close_channel()
        assert not tp20.channel_open
        assert await wait_until(lambda: module.channel is not None and not module.channel.open)
    finally:
        module.close()


async def test_session_keep_alive(manager, channel, ecu, monkeypatch):
    from can_explorer.gui import tp20 as tp20_module

    monkeypatch.setattr(tp20_module, 'SESSION_KEEP_ALIVE', 0.05)
    handler = Module()
    module = FakeModule(ecu, 0x01, handler=handler)
    connection = open_connection(manager, channel)
    tp20 = connection.tp20
    try:
        assert tp20.open_channel('01', '89') == ''
        await idle(tp20)
        assert await wait_until(lambda: handler.tester_present >= 2)
        tp20.close_channel()
    finally:
        module.close()


async def test_refused_session_keeps_the_channel(manager, channel, ecu):
    module = FakeModule(ecu, 0x01, handler=lambda request: b'\x7f\x10\x12')
    connection = open_connection(manager, channel)
    tp20 = connection.tp20
    try:
        assert tp20.open_channel('01', '89') == ''
        await idle(tp20)
        assert tp20.channel_open and 'session was not started' in tp20.error
        tp20.close_channel()
    finally:
        module.close()


async def test_scan(manager, channel, ecu, labels):
    modules = [FakeModule(ecu, 0x01, handler=Module()), FakeModule(ecu, 0x17)]
    connection = open_connection(manager, channel)
    tp20 = connection.tp20
    try:
        assert tp20.load_labels(QUrl.fromLocalFile(str(labels))) == ''
        assert tp20.scan('zz', '', '').startswith('Invalid address range')
        assert tp20.scan('01-03, 17', '', '') == ''
        assert tp20.scanning and tp20.scan('01', '', '') == 'A scan is already running'
        assert tp20.open_channel('01', '') == 'Wait for the scan to finish'
        assert await wait_until(lambda: not tp20.scanning)
        assert [(r['address'], r['name']) for r in tp20.scan_results] == [('01', 'Engine'), ('17', '')]
        assert tp20.scan_progress == '4 of 4 addresses, 2 module(s) found'
        assert tp20.scan('01-FF', '', '') == ''
        tp20.stop_scan()
        assert await wait_until(lambda: not tp20.scanning)
    finally:
        for module in modules:
            module.close()


async def test_needs_a_bus(manager, channel):
    manager.add_connection({'name': 'VW', 'interface': 'VirtualBus', 'channel': channel})
    tp20 = manager.connections[-1].tp20
    assert tp20.open_channel('01', '89') == 'Connect first'
    assert tp20.scan('01', '', '') == 'Connect first'
    assert tp20.open_channel('', '') in ('Connect first', 'Enter the module address, e.g. 01')
    assert tp20.load_labels(QUrl.fromLocalFile('/nonexistent/labels.json')).startswith('Cannot read')


async def test_page_renders_with_data_without_warnings(window, qapp, qtbot, channel, ecu, labels):  # noqa: F811
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickWindow

    engine, manager, warnings = window
    handler = Module()
    module = FakeModule(ecu, 0x01, handler=handler)
    try:
        connection = open_connection(manager, channel)
        await wait_until(lambda: connection.connected, pump=qapp.processEvents)
        stack = [sip.cast(engine.rootObjects()[0], QQuickWindow).contentItem()]
        while not stack[-1].metaObject().className().startswith('ConnectionView'):
            item = stack.pop()
            stack.extend(item.childItems())
        view = stack[-1]
        view.setProperty('page', 'tp20')
        tp20 = connection.tp20
        assert tp20.load_labels(QUrl.fromLocalFile(str(labels))) == ''
        assert tp20.scan('01-02', '', '') == ''
        await wait_until(lambda: not tp20.scanning, pump=qapp.processEvents)
        assert tp20.open_channel('01', '89') == ''
        await wait_until(lambda: tp20.channel_open and not tp20.busy, pump=qapp.processEvents)
        assert tp20.read_group('1') == ''
        await wait_until(lambda: not tp20.busy and tp20.block, pump=qapp.processEvents)
        assert tp20.read_coding('Coding', '') == ''
        await wait_until(lambda: not tp20.busy and tp20.coding_fields, pump=qapp.processEvents)
        tp20.set_field('Variant', 9)
        assert tp20.raw_request('21 02') == ''
        await wait_until(lambda: not tp20.busy and tp20.history, pump=qapp.processEvents)

        tab_bar = None
        stack = [view]
        while stack and tab_bar is None:
            item = stack.pop()
            if item.objectName() == 'tp20Tabs':
                tab_bar = item
            stack.extend(item.childItems())
        assert tab_bar is not None and tab_bar.property('count') == 4
        for index in range(4):
            tab_bar.setProperty('currentIndex', index)
            qtbot.wait(50)
        tp20.close_channel()
        qtbot.wait(50)
        assert warnings == []
    finally:
        module.close()


async def test_close_channel_leaves_no_background_task(manager, channel, ecu):
    """A hang at event-loop teardown came from close_channel firing an untracked disconnect task; closing must be
    synchronous and leave no TP 2.0 task (keep-alive, live or disconnect) pending to outlive the page."""
    import asyncio

    module = FakeModule(ecu, 0x01, handler=Module())
    connection = open_connection(manager, channel)
    tp20 = connection.tp20
    try:
        assert tp20.open_channel('01', '89') == ''
        await idle(tp20)
        assert tp20.set_live(True, '1') == '' and tp20.live
        tp20.close_channel()
        assert not tp20.live and tp20._live_task is None
        await asyncio.sleep(0)  # Let any cancellation settle
        lingering = [
            task
            for task in asyncio.all_tasks()
            if not task.done() and (task.get_name() == 'TP 2.0' or 'disconnect' in repr(task.get_coro()))
        ]
        assert lingering == []
        assert await wait_until(lambda: module.channel is not None and not module.channel.open)
    finally:
        module.close()
