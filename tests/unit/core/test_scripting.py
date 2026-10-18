import asyncio
import textwrap
import uuid

import can
import pytest

from can_explorer.core.decoding import DbcDecoder
from can_explorer.core.session import BusSession
from can_explorer.core.sources import BusConfig, LiveSource
from can_explorer.scripting import ScriptError, ScriptRuntime

ENGINE_SCRIPT = '''
from can_explorer.scripting import Node, on_message, on_signal, on_start, on_stop, on_variable


class Engine(Node):
    @on_start
    def start(self):
        self.status = self.message('EngineData')
        self.speed = 800
        self.every(0.02, self.send_status)
        self.log('engine started')

    def send_status(self):
        self.status.EngineSpeed = self.speed
        self.status['GearPosition'] = 'First'
        self.send(self.status)

    @on_variable('target_speed')
    def target(self, value):
        self.speed = value

    @on_message(0x7DF)
    def diagnostic(self, msg):
        self.send(0x7E8, [0x02, 0x41, msg.data[2]])

    @on_stop
    def stop(self):
        self.log('engine stopped')


class Dashboard(Node):
    name = 'Cluster'

    @on_start
    def start(self):
        self.received = []
        self.speeds = []

    @on_message('EngineData')
    def engine(self, msg):
        self.received.append((msg.name, msg.signals['EngineSpeed'], msg.labels['GearPosition']))

    @on_signal('EngineData', 'EngineSpeed')
    def speed(self, value, msg):
        self.speeds.append(value)
        self.variables['displayed_speed'] = value
'''


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


async def receive(bus, predicate=lambda msg: True, timeout=3.0):
    """Receive from a python-can bus without blocking the event loop the simulation runs on."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        msg = bus.recv(timeout=0)
        if msg is None:
            await asyncio.sleep(0.005)
        elif predicate(msg):
            return msg
    raise AssertionError('Timed out waiting for a frame')


@pytest.fixture
async def simulation(data_dir, tmp_path):
    channel = f'sim-{uuid.uuid4().hex}'
    session = BusSession(LiveSource(BusConfig('virtual', channel)))
    session.start()
    database = DbcDecoder(data_dir / 'example.dbc')
    logs = []
    runtime = ScriptRuntime(session, lambda: database, lambda level, source, text: logs.append((level, source, text)))
    peer = can.Bus(interface='virtual', channel=channel)
    yield runtime, session, peer, logs, tmp_path
    runtime.stop()
    peer.shutdown()
    session.stop()


def write(tmp_path, name, code):
    path = tmp_path / name
    path.write_text(textwrap.dedent(code))
    return path


async def test_nodes_send_receive_and_share_variables(simulation):
    runtime, session, peer, logs, tmp_path = simulation
    script = runtime.load(write(tmp_path, 'engine.py', ENGINE_SCRIPT))
    assert script.error == '' and [node.name for node in script.nodes] == ['Engine', 'Cluster']
    runtime.start()
    engine, cluster = script.nodes
    await wait_until(lambda: len(cluster.received) >= 3)
    assert cluster.received[0] == ('EngineData', 800.0, 'First')
    msg = await receive(peer, lambda msg: msg.arbitration_id == 0x100)
    assert msg.data[:2] == bytes([0x80, 0x0C])  # 800 rpm / 0.25 = 3200
    runtime.variables['target_speed'] = 1500
    await wait_until(lambda: 1500.0 in cluster.speeds)
    assert cluster.speeds.count(800.0) == 1  # on_signal only fires on changes
    assert runtime.variables['displayed_speed'] == 1500.0
    # Frames from other bus participants, by identifier
    peer.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=bytes([0x02, 0x01, 0x0D])))
    response = await receive(peer, lambda msg: msg.arbitration_id == 0x7E8)
    assert bytes(response.data) == bytes([0x02, 0x41, 0x0D])
    runtime.stop()
    sent = len([f for f in session.trace if f.message.arbitration_id == 0x100])
    await asyncio.sleep(0.1)
    assert len([f for f in session.trace if f.message.arbitration_id == 0x100]) == sent  # Timers stopped
    assert ('info', 'Engine', 'engine started') in logs and ('info', 'Engine', 'engine stopped') in logs


async def test_nodes_do_not_receive_their_own_frames(simulation):
    runtime, session, peer, logs, tmp_path = simulation
    script = runtime.load(
        write(
            tmp_path,
            'echo.py',
            '''
            from can_explorer.scripting import Node, on_message, on_start

            class Echo(Node):
                @on_start
                def start(self):
                    self.seen = []
                    self.after(0.01, self.send, 0x123, b'\\x01')

                @on_message()
                def any_frame(self, msg):
                    self.seen.append(msg.id)
            ''',
        )
    )
    runtime.start()
    await asyncio.sleep(0.1)
    assert script.nodes[0].seen == []
    peer.send(can.Message(arbitration_id=0x55, is_extended_id=False))
    await wait_until(lambda: script.nodes[0].seen == [0x55])


async def test_errors_are_contained(simulation):
    runtime, session, peer, logs, tmp_path = simulation
    script = runtime.load(
        write(
            tmp_path,
            'faulty.py',
            '''
            from can_explorer.scripting import Node, on_message, on_start

            class Faulty(Node):
                @on_start
                def start(self):
                    self.calls = 0
                    self.every(0.02, self.tick)

                def tick(self):
                    self.calls += 1
                    raise RuntimeError('boom')

                @on_message('NoSuchMessage')
                def never(self, msg):
                    pass

                def send_unknown(self):
                    self.send('NoSuchMessage', Value=1)
            ''',
        )
    )
    runtime.start()
    node = script.nodes[0]
    await wait_until(lambda: node.calls >= 3)  # The timer keeps running after errors
    assert runtime.errors >= 3
    assert any(level == 'error' and 'RuntimeError: boom' in text for level, _, text in logs)
    with pytest.raises(ScriptError, match='Unknown message'):
        node.send_unknown()
    with pytest.raises(ScriptError, match='no signal'):
        node.message('EngineData')['Nope'] = 1


async def test_script_loading_errors_and_reload(simulation):
    runtime, session, peer, logs, tmp_path = simulation
    broken = runtime.load(write(tmp_path, 'broken.py', 'def oops(:\n'))
    assert broken.nodes == [] and 'SyntaxError' in broken.error
    empty = runtime.load(write(tmp_path, 'empty.py', 'x = 1\n'))
    assert 'defines no Node subclass' in empty.error
    path = write(tmp_path, 'node.py', 'from can_explorer.scripting import Node\nclass A(Node):\n    pass\n')
    runtime.load(path)
    assert [script.path.name for script in runtime.scripts] == ['broken.py', 'empty.py', 'node.py']
    path.write_text('from can_explorer.scripting import Node\nclass B(Node):\n    pass\n')
    reloaded = runtime.load(path)
    assert [node.name for node in reloaded.nodes] == ['B']
    assert [script.path.name for script in runtime.scripts] == ['broken.py', 'empty.py', 'node.py']
    runtime.unload(path)
    assert len(runtime.scripts) == 2


async def test_raw_send_options_and_requirements(simulation, data_dir):
    runtime, session, peer, logs, tmp_path = simulation
    script = runtime.load(
        write(tmp_path, 'raw.py', 'from can_explorer.scripting import Node\nclass Raw(Node):\n    pass\n')
    )
    node = script.nodes[0]
    with pytest.raises(ScriptError, match='not running'):
        node.send(0x1, b'')
    runtime.start()
    node.send(0x18FEF100, [1, 2, 3])
    node.send(0x10, remote=True)
    node.send('VehicleSpeed', WheelSpeed=48)
    frames = [await receive(peer) for _ in range(3)]
    assert frames[0].is_extended_id and bytes(frames[0].data) == b'\x01\x02\x03'
    assert frames[1].is_remote_frame and not frames[1].is_extended_id
    assert frames[2].arbitration_id == 0x18FEF1FE and frames[2].data[1:3] == bytes([0x00, 0x30])
    with pytest.raises(ScriptError, match='database message name'):
        node.send(0x1, b'', Value=1)


async def test_start_requires_open_session(data_dir):
    session = BusSession(LiveSource(BusConfig('virtual', 'closed')))
    runtime = ScriptRuntime(session, lambda: None)
    with pytest.raises(ScriptError, match='Connect'):
        runtime.start()
