import asyncio
import json
import uuid

import can
import pytest
from PyQt6.QtCore import QObject, QSettings, QUrl, pyqtSignal

from can_explorer.core.media_sync import MediaManifest, manifest_path
from can_explorer.core.session import SessionState
from can_explorer.gui import media as media_module
from can_explorer.gui.app_state import DEFAULT_TILE_URL, AppState
from can_explorer.gui.connection_manager import ConnectionManager


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'Timed out waiting for condition'
        await asyncio.sleep(0.01)


@pytest.fixture
def manager(qapp, tmp_path):
    state = AppState(QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    manager = ConnectionManager(state)
    yield manager
    manager.close_all()


def write_log(path, count=30, start=10.0, step=0.1):
    with can.Logger(path) as logger:
        for i in range(count):
            logger.on_message_received(can.Message(arbitration_id=i, timestamp=start + i * step))


GPX = """<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="48.0" lon="11.0"><time>1970-01-01T00:16:50Z</time></trkpt>
<trkpt lat="48.01" lon="11.0"><time>1970-01-01T00:17:00Z</time></trkpt>
</trkseg></trk></gpx>"""


async def replayed(manager, path):
    manager.open_log_file(QUrl.fromLocalFile(str(path)), 'Raw', 0.0)
    connection = manager.connections[-1]
    await wait_until(lambda: connection.session.state is SessionState.FINISHED)
    await wait_until(lambda: connection.session.source.duration is not None)
    connection.replay.refresh()
    connection.media.refresh()
    return connection


async def test_replay_video_and_gps_follow_the_position(manager, tmp_path):
    log = tmp_path / 'drive.asc'
    write_log(log)  # 2.9 s
    (tmp_path / 'drive.mp4').write_bytes(b'')
    # Recorded with an interface clock 1000 s behind the wall clock; the video started 2 s after the first frame
    MediaManifest(manifest_path(log), 1000.0, {'drive.asc': 10.0}, 'drive.mp4', video_start=1012.0).save()
    connection = await replayed(manager, log)
    media = connection.media
    assert media.is_replay and media.video_name == 'drive.mp4' and media.video_problem == ''
    assert media.video_url == QUrl.fromLocalFile(str(tmp_path / 'drive.mp4')).toString()
    assert connection.replay.position == pytest.approx(2.9)
    assert media.video_target == pytest.approx(0.9)

    assert media.set_video_offset(0.25) == ''
    assert media.video_target == pytest.approx(1.15)
    assert json.loads(manifest_path(log).read_text())['video_offset'] == 0.25

    # A GPX track, aligned by UTC time: 1010 s + 2.9 s is 29 % of the way between its fixes
    assert not media.has_track and media.current == {'valid': False}
    (tmp_path / 'tracks').mkdir()
    (tmp_path / 'tracks' / 'drive.gpx').write_text(GPX)
    track_changes = []
    media.track_changed.connect(lambda: track_changes.append(True))
    assert media.attach_gps(QUrl.fromLocalFile(str(tmp_path / 'tracks' / 'drive.gpx'))) == ''
    assert media.has_track and media.track_name == 'drive.gpx' and track_changes == [True]
    assert len(media.track_coordinates) == 4 and '2 fixes' in media.track_summary
    assert media.current['valid'] and media.current['lat'] == pytest.approx(48.0029)
    assert media.current['speed_kmh'] == pytest.approx(400.3, rel=0.01)
    assert json.loads(manifest_path(log).read_text())['gps'] == 'tracks/drive.gpx'
    media.set_gps_offset(-5.0)
    assert not media.current['valid']  # Before the track
    media.detach_gps()
    assert not media.has_track and json.loads(manifest_path(log).read_text())['gps'] == ''


async def test_replay_without_manifest(manager, tmp_path):
    log = tmp_path / 'plain.asc'
    write_log(log, count=5)
    connection = await replayed(manager, log)
    media = connection.media
    assert media.video_url == '' and media.video_target == -1.0 and media.video_problem == ''
    assert 'Could not read' in media.attach_gps(QUrl.fromLocalFile(str(tmp_path / 'missing.gpx')))
    (tmp_path / 'bad.gpx').write_text('<gpx/>')
    assert media.attach_gps(QUrl.fromLocalFile(str(tmp_path / 'bad.gpx'))) != ''
    assert not manifest_path(log).exists()  # Nothing attached, nothing written
    (tmp_path / 'track.gpx').write_text(GPX)
    assert media.attach_gps(QUrl.fromLocalFile(str(tmp_path / 'track.gpx'))) == ''
    # No wall-clock times in the log (ASC times count from 0 here): the track starts with the log
    assert media.current['valid'] and media.current['lat'] == pytest.approx(48.0004)
    assert manifest_path(log).exists()


async def test_missing_video_is_reported(manager, tmp_path):
    log = tmp_path / 'drive.asc'
    write_log(log, count=3)
    MediaManifest(manifest_path(log), 0.0, {'drive.asc': 10.0}, 'gone.mp4', 10.0).save()
    connection = await replayed(manager, log)
    assert connection.media.video_url == '' and connection.media.video_problem == 'gone.mp4 not found'


def test_map_tiles(manager):
    state = manager.app_state
    assert not state.map_tiles and state.map_tile_url == DEFAULT_TILE_URL
    state.set_map_tiles(True, 'https://tiles.example/{z}/{x}/{y}.png')
    assert state.map_tiles and state.map_tile_url == 'https://tiles.example/{z}/{x}/{y}.png'
    state.set_map_tiles(True, 'ftp://no-placeholders')
    assert state.map_tile_url == DEFAULT_TILE_URL  # Invalid templates fall back to the default
    controller = media_module.MediaController(connection=None)
    tiles = controller.tiles(0.5, 0.5, 1.0, 512, 512, DEFAULT_TILE_URL)
    assert len(tiles) == 4 and tiles[0]['url'].startswith('https://tile.openstreetmap.org/1/')


class FakeCamera(QObject):
    changed = pyqtSignal()
    instances = []

    def __init__(self, camera_id, path, parent=None):
        super().__init__(parent)
        if camera_id != 'cam0':
            raise media_module.MediaError('The camera is no longer available')
        self.path, self.start, self.error, self.codec = path, None, '', 'H.264'
        self.previews = []
        FakeCamera.instances.append(self)

    def begin(self):
        import time

        self.start = time.time()

    def stop(self):
        self.stopped = True

    def set_preview(self, output):
        self.previews.append(output)


async def test_camera_recording_writes_the_manifest(manager, tmp_path, monkeypatch):
    monkeypatch.setattr(media_module, 'CameraRecording', FakeCamera)
    channel = f'media-{uuid.uuid4().hex}'
    manager.add_connection({'name': 'Bus', 'interface': 'VirtualBus', 'channel': channel})
    connection = manager.connections[0]
    connection.open()
    errors = []
    connection.error_occurred.connect(errors.append)

    # An unknown camera is reported; the CAN recording runs anyway
    assert connection.record(QUrl.fromLocalFile(str(tmp_path / 'a.asc')), {'camera': 'gone'}) == ''
    assert connection.recording and errors and 'no longer available' in errors[0]
    connection.stop_recording()

    log = tmp_path / 'drive.blf'
    assert connection.record(QUrl.fromLocalFile(str(log)), {'camera': 'cam0'}) == ''
    camera = FakeCamera.instances[-1]
    assert connection.media.camera_recording and camera.path == tmp_path / 'drive.mp4'
    peer = can.Bus(interface='virtual', channel=channel)
    peer.send(can.Message(arbitration_id=0x100, is_extended_id=False))
    await wait_until(lambda: connection.session.trace.total == 1)
    peer.shutdown()
    connection.stop_recording()
    assert not connection.media.camera_recording and camera.stopped
    manifest = MediaManifest.load(manifest_path(log))
    assert manifest.video == 'drive.mp4' and manifest.video_start == camera.start
    assert list(manifest.log_starts) == ['drive.blf'] and manifest.clock_offset == pytest.approx(0.0, abs=1.0)
    assert 'Video saved' in connection.media.camera_status

    # Closing the connection while recording also finishes the video
    assert connection.record(QUrl.fromLocalFile(str(tmp_path / 'other.blf')), {'camera': 'cam0'}) == ''
    connection.close()
    assert not connection.media.camera_recording and manifest_path(tmp_path / 'other.blf').exists()


def frame_source(parent):
    """A QVideoFrameInput fed with small coloured frames at 30 fps, like a camera (frames sent while the recorder
    is not ready are dropped)."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtMultimedia import QVideoFrame, QVideoFrameInput

    frames = QVideoFrameInput(parent)
    sent = [0]

    def send():
        image = QImage(160, 120, QImage.Format.Format_RGBA8888)
        image.fill(QColor(sent[0] % 256, 80, 160))
        frame = QVideoFrame(image)
        frame.setStartTime(sent[0] * 33_333)
        if frames.sendVideoFrame(frame):
            sent[0] += 1

    timer = QTimer(parent)
    timer.setInterval(33)
    timer.timeout.connect(send)
    timer.start()
    return frames


def test_video_encoding_falls_back_to_a_working_codec(qtbot, tmp_path, monkeypatch):
    """Records real frames: H.264 is encoded in hardware only by Qt's FFmpeg, which fails on many machines; the
    recording must then go on with MPEG-4 Part 2 instead of producing an empty file."""
    monkeypatch.setattr(media_module, '_failed_codecs', set())
    owner = QObject()
    frames = frame_source(owner)
    path = tmp_path / 'video.mp4'
    recording = media_module.CameraRecording('', path, frame_input=frames)
    recording.begin()
    qtbot.waitUntil(lambda: recording.start is not None or bool(recording.error), timeout=10_000)
    qtbot.wait(300)
    recording.stop()
    qtbot.wait(500)
    assert recording.error == '' and recording.start is not None
    assert recording.codec in ('H.264', 'MPEG-4 Part 2') and path.stat().st_size > 0


def test_codec_fallback_order(qapp, tmp_path, monkeypatch):
    from PyQt6.QtMultimedia import QMediaRecorder

    monkeypatch.setattr(media_module, '_failed_codecs', set())
    owner = QObject()
    recording = media_module.CameraRecording('', tmp_path / 'v.mp4', frame_input=frame_source(owner))
    recording._codecs = list(media_module.VIDEO_CODECS)  # As if both were supported
    recording._set_format()
    assert recording.codec == 'H.264'
    # The recorder is stopped already, so the next codec is tried at once
    recording._on_recorder_error(QMediaRecorder.Error.ResourceError, 'Could not initialize encoder')
    assert recording.codec == 'MPEG-4 Part 2' and recording.error == ''
    assert media_module._failed_codecs == {'H264'}  # Not tried again by later recordings
    recording._on_recorder_error(QMediaRecorder.Error.ResourceError, 'Could not initialize encoder')
    assert recording.error == 'Could not initialize encoder'  # Nothing left to try
    # Errors after frames were recorded are reported, not retried
    recording._codecs = [('MPEG4', 'MPEG-4 Part 2')]
    recording.error, recording.start = '', 1.0
    recording._on_recorder_error(QMediaRecorder.Error.ResourceError, 'disk full')
    assert recording.error == 'disk full'
    recording.stop()
