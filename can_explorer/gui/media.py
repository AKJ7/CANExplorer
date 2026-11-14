"""Camera video and GPS tracks of a connection: recording a camera alongside the CAN log, and showing the video
and a map in step with a replay. The synchronisation logic is in core/media_sync.py."""

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot

from can_explorer.core import media_sync
from can_explorer.core.media_sync import GpsTrack, MediaError, MediaManifest, TrackPoint
from can_explorer.core.sources import FileSource
from can_explorer.sys.log_handler import get_logger

logger = get_logger(__name__)

OSM_ATTRIBUTION = '© OpenStreetMap contributors'


def _local_path(url: QUrl | str) -> Path:
    if isinstance(url, QUrl):
        return Path(url.toLocalFile() if url.isLocalFile() else url.toString())
    return Path(url)


def camera_devices() -> List[Dict[str, str]]:
    """The cameras of the system: {'id', 'name'}. Empty when Qt Multimedia has no backend."""
    try:
        from PyQt6.QtMultimedia import QMediaDevices
    except ImportError:
        return []
    return [
        {'id': bytes(device.id()).decode(errors='replace'), 'name': device.description()}
        for device in QMediaDevices.videoInputs()
    ]


# Video codecs to try, best first. The FFmpeg build bundled with Qt is the LGPL one: it encodes H.264 only in
# hardware (VA-API, NVENC), which fails on many GPUs ("No usable encoding profile found"); MPEG-4 Part 2 has a
# software encoder and works everywhere.
VIDEO_CODECS = (('H264', 'H.264'), ('MPEG4', 'MPEG-4 Part 2'))
_failed_codecs: set = set()  # Codecs whose encoder could not be opened in this process; not tried again


class CameraRecording(QObject):
    """Records one camera to a video file. `start` is the wall-clock time of the video's first frame.

    When the encoder of a codec cannot be opened, the recording starts again with the next codec of VIDEO_CODECS.
    """

    changed = pyqtSignal()

    def __init__(self, camera_id: str, path: Path, parent: Optional[QObject] = None, frame_input=None):
        """Records the camera `camera_id`, or with `frame_input` (a QVideoFrameInput) the frames sent to it."""
        super().__init__(parent)
        from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QMediaFormat, QMediaRecorder

        device = None
        if frame_input is None:
            device = next(
                (d for d in QMediaDevices.videoInputs() if bytes(d.id()).decode(errors='replace') == camera_id), None
            )
            if device is None:
                raise MediaError('The camera is no longer available')
        self.path = path
        self.start: Optional[float] = None
        self.error = ''
        self._requested_at = 0.0
        self._stopping = False
        self._retry_pending = False
        supported = QMediaFormat(QMediaFormat.FileFormat.MPEG4).supportedVideoCodecs(QMediaFormat.ConversionMode.Encode)
        self._codecs = [
            (name, label)
            for name, label in VIDEO_CODECS
            if getattr(QMediaFormat.VideoCodec, name) in supported and name not in _failed_codecs
        ]
        self._codec: Optional[tuple] = None  # (name, label) being recorded with; None: Qt's default
        self._camera = QCamera(device, self) if device is not None else None
        self._session = QMediaCaptureSession(self)
        if self._camera is not None:
            self._session.setCamera(self._camera)
            self._camera.errorOccurred.connect(self._on_error)
        else:
            self._session.setVideoFrameInput(frame_input)
        self._recorder = QMediaRecorder(self)
        self._session.setRecorder(self._recorder)
        self._recorder.setQuality(QMediaRecorder.Quality.HighQuality)
        self._recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
        self._recorder.actualLocationChanged.connect(self._location_changed)
        self._recorder.durationChanged.connect(self._duration_changed)
        self._recorder.errorOccurred.connect(self._on_recorder_error)
        self._recorder.recorderStateChanged.connect(self._state_changed)

    @property
    def codec(self) -> str:
        """The codec recorded with, e.g. 'H.264'; '' for Qt's default."""
        return self._codec[1] if self._codec else ''

    def _set_format(self) -> None:
        from PyQt6.QtMultimedia import QMediaFormat

        media_format = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
        self._codec = self._codecs.pop(0) if self._codecs else None
        if self._codec is not None:
            media_format.setVideoCodec(getattr(QMediaFormat.VideoCodec, self._codec[0]))
        self._recorder.setMediaFormat(media_format)

    def begin(self) -> None:
        self._set_format()
        if self._camera is not None:
            self._camera.start()
        self._requested_at = time.time()
        self._recorder.record()

    def stop(self) -> None:
        self._stopping = True
        self._recorder.stop()
        if self._camera is not None:
            self._camera.stop()

    def set_preview(self, output: Optional[QObject]) -> None:
        self._session.setVideoOutput(output)

    def _location_changed(self, url: QUrl) -> None:
        if url.isLocalFile():
            self.path = Path(url.toLocalFile())
            self.changed.emit()

    def _duration_changed(self, duration_ms: int) -> None:
        # The first frame was `duration` before now; the smallest estimate has the least delivery delay
        if duration_ms > 0:
            start = time.time() - duration_ms / 1000
            if self.start is None or start < self.start:
                self.start = max(start, self._requested_at)
                self.changed.emit()

    def _on_recorder_error(self, error, message: str) -> None:
        from PyQt6.QtMultimedia import QMediaRecorder

        encoder_failed = error in (QMediaRecorder.Error.ResourceError, QMediaRecorder.Error.FormatError)
        if encoder_failed and self.start is None and not self._stopping and self._codecs:
            # Nothing was recorded yet: try the next codec (the failed attempt left an empty file, overwritten)
            failed = self._codec
            if failed is not None:
                _failed_codecs.add(failed[0])
            logger.warning(
                f'Camera recording: {failed[1] if failed else "the default codec"} failed ({message}); '
                f'trying {self._codecs[0][1]}'
            )
            # Qt reports the failure while still in RecordingState and stops afterwards; record() in between is ignored
            self._retry_pending = True
            if self._recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState:
                self._retry()
            return
        self._on_error(error, message)

    def _state_changed(self, state) -> None:
        from PyQt6.QtMultimedia import QMediaRecorder

        if self._retry_pending and state == QMediaRecorder.RecorderState.StoppedState:
            self._retry()

    def _retry(self) -> None:
        self._retry_pending = False
        if self._stopping:
            return
        self._set_format()
        self._recorder.record()
        self.changed.emit()

    def _on_error(self, *args) -> None:
        message = next((arg for arg in args if isinstance(arg, str) and arg), 'camera error')
        self.error = message
        logger.error(f'Camera recording: {message}')
        self.changed.emit()


class MediaController(QObject):
    """Video and GPS of one connection: the camera of a live recording, and the media of a replayed log file."""

    changed = pyqtSignal()  # Media, track, offsets, camera state
    position_changed = pyqtSignal()  # Video target and current GPS position
    cameras_changed = pyqtSignal()
    track_changed = pyqtSignal()  # A track was loaded or removed

    def __init__(self, connection, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.connection = connection
        self._cameras: List[Dict[str, str]] = []
        self._camera: Optional[CameraRecording] = None
        self._camera_log: Optional[Path] = None  # The log path the camera records alongside
        self._preview: Optional[QObject] = None
        self._status = ''
        self._manifest: Optional[MediaManifest] = None
        self._log_path: Optional[Path] = None  # Of the replay the manifest was loaded for
        self._track: Optional[GpsTrack] = None
        self._track_error = ''
        self._video_target = -1.0
        self._current: Optional[TrackPoint] = None

    # Cameras

    @pyqtProperty('QVariantList', notify=cameras_changed)
    def cameras(self) -> List[Dict[str, str]]:
        return self._cameras

    @pyqtSlot()
    def refresh_cameras(self):
        self._cameras = camera_devices()
        self.cameras_changed.emit()

    @pyqtProperty(bool, notify=changed)
    def camera_recording(self) -> bool:
        return self._camera is not None

    @pyqtProperty(str, notify=changed)
    def camera_status(self) -> str:
        return self._status

    def start_camera(self, camera_id: str, log_path: Path) -> str:
        """Record the camera to a video next to the log. Returns an error, or ''."""
        self.stop_camera()
        if not camera_id:
            return ''
        path = media_sync.video_path_for(log_path)
        try:
            self._camera = CameraRecording(camera_id, path, parent=self)
        except (ImportError, MediaError) as e:
            self._status = f'Camera: {e}'
            self.changed.emit()
            return self._status
        self._camera.changed.connect(self._camera_changed)
        self._camera_log = log_path
        if self._preview is not None:
            self._camera.set_preview(self._preview)
        self._camera.begin()
        self._camera_changed()  # 'Recording video to drive.mp4 (H.264)'
        return ''

    def _camera_changed(self):
        camera = self._camera
        if camera is None:
            return
        if camera.error:
            self._status = f'Camera: {camera.error}'
        else:
            codec = f' ({camera.codec})' if camera.codec else ''
            self._status = f'Recording video to {camera.path.name}{codec}'
        self.changed.emit()

    def stop_camera(self) -> None:
        """Stop the camera and write the manifest that synchronises the video with the recorded log files."""
        camera, log_path = self._camera, self._camera_log
        self._camera, self._camera_log = None, None
        if camera is None or log_path is None:
            return
        camera.stop()
        session = self.connection.session
        recorder = session.recorder or session.last_recorder
        starts = dict(recorder.file_starts) if recorder is not None else {}
        manifest = MediaManifest(media_sync.manifest_path(log_path))
        manifest.clock_offset = session.recording_clock_offset
        manifest.log_starts = starts
        manifest.video = manifest.relative_name(camera.path)
        manifest.video_start = camera.start
        if camera.start is None or manifest.clock_offset is None or not starts:
            self._status = 'The video could not be synchronised: no frames were recorded with it'
        else:
            self._status = f'Video saved to {camera.path.name}'
        try:
            manifest.save()
        except MediaError as e:
            self._status = str(e)
        camera.deleteLater()
        self.changed.emit()

    @pyqtSlot(QObject)
    def set_preview(self, output: Optional[QObject]):
        """The QML VideoOutput showing the camera while recording."""
        self._preview = output
        if self._camera is not None:
            self._camera.set_preview(output)

    # Following the connection

    def refresh(self) -> None:
        session = self.connection.session
        if self._camera is not None and not session.recording:
            self.stop_camera()  # The recording stopped (or the connection closed)
        self._follow_replay()

    def _source(self) -> Optional[FileSource]:
        source = self.connection.session.source
        return source if isinstance(source, FileSource) else None

    def _follow_replay(self) -> None:
        source = self._source()
        if source is None:
            return
        if source.path != self._log_path:
            self._load(source.path)
        position = self.connection.replay.position
        target = -1.0
        if self._manifest is not None:
            video = self._manifest.video_position(source.path.name, position)
            target = video if video is not None else -1.0
        current = None
        if self._track is not None:
            current = self._track.at(
                media_sync.track_time(
                    self._track,
                    position,
                    self._manifest.log_start_wall(source.path.name) if self._manifest else None,
                    source.first_timestamp,
                    self._manifest.gps_offset if self._manifest else 0.0,
                )
            )
        if target != self._video_target or current != self._current:
            self._video_target, self._current = target, current
            self.position_changed.emit()

    def _load(self, log_path: Path) -> None:
        self._log_path = log_path
        self._manifest, self._track, self._track_error = None, None, ''
        found = media_sync.find_manifest(log_path)
        if found is not None:
            try:
                self._manifest = MediaManifest.load(found)
            except MediaError as e:
                self._track_error = str(e)
        if self._manifest is not None and self._manifest.gps:
            self._load_track(self._manifest.resolve(self._manifest.gps))
        self.changed.emit()
        self.track_changed.emit()

    def _load_track(self, path: Optional[Path]) -> None:
        try:
            self._track = media_sync.load_track(path) if path else None
            self._track_error = ''
        except MediaError as e:
            self._track, self._track_error = None, str(e)

    def _ensure_manifest(self) -> Optional[MediaManifest]:
        if self._manifest is None and self._log_path is not None:
            self._manifest = MediaManifest(media_sync.manifest_path(self._log_path))
        return self._manifest

    def _save(self) -> str:
        try:
            if self._manifest is not None:
                self._manifest.save()
        except MediaError as e:
            return str(e)
        return ''

    # Video of a replay

    @pyqtProperty(bool, notify=changed)
    def is_replay(self) -> bool:
        return self._source() is not None

    @pyqtProperty(str, notify=changed)
    def video_url(self) -> str:
        path = self._manifest.resolve(self._manifest.video) if self._manifest else None
        return QUrl.fromLocalFile(str(path)).toString() if path is not None and path.is_file() else ''

    @pyqtProperty(str, notify=changed)
    def video_name(self) -> str:
        return Path(self._manifest.video).name if self._manifest and self._manifest.video else ''

    @pyqtProperty(str, notify=changed)
    def video_problem(self) -> str:
        """Why a video named in the manifest cannot be shown in step, or ''."""
        manifest = self._manifest
        if manifest is None or not manifest.video:
            return ''
        path = manifest.resolve(manifest.video)
        if path is None or not path.is_file():
            return f'{manifest.video} not found'
        if self._log_path is not None and manifest.log_start_wall(self._log_path.name) is None:
            return f'The manifest has no timing for {self._log_path.name}'
        if manifest.video_start is None:
            return 'The video start time was not recorded'
        return ''

    @pyqtProperty(float, notify=position_changed)
    def video_target(self) -> float:
        """Seconds into the video at the replay position; negative before the video starts or when unknown."""
        return self._video_target

    @pyqtProperty(float, notify=changed)
    def video_offset(self) -> float:
        return self._manifest.video_offset if self._manifest else 0.0

    @pyqtSlot(float, result=str)
    def set_video_offset(self, offset: float) -> str:
        manifest = self._ensure_manifest()
        if manifest is None:
            return ''
        manifest.video_offset = round(float(offset), 3)
        error = self._save()
        self.changed.emit()
        self._follow_replay()
        return error

    # GPS track of a replay

    @pyqtSlot(QUrl, result=str)
    def attach_gps(self, url: QUrl) -> str:
        """Attach a GPX or NMEA file to the replayed log. Returns an error, or ''."""
        path = _local_path(url)
        try:
            track = media_sync.load_track(path)
        except MediaError as e:
            return str(e)
        manifest = self._ensure_manifest()
        if manifest is None:
            return 'Only a log file replay can have a GPS track'
        manifest.gps = manifest.relative_name(path)
        self._track, self._track_error = track, ''
        error = self._save()
        self.changed.emit()
        self.track_changed.emit()
        self._follow_replay()
        return error

    @pyqtSlot()
    def detach_gps(self):
        if self._manifest is not None:
            self._manifest.gps = ''
            self._save()
        self._track, self._current = None, None
        self.changed.emit()
        self.track_changed.emit()
        self.position_changed.emit()

    @pyqtProperty(bool, notify=changed)
    def has_track(self) -> bool:
        return self._track is not None

    @pyqtProperty(str, notify=changed)
    def track_name(self) -> str:
        return Path(self._manifest.gps).name if self._manifest and self._manifest.gps else ''

    @pyqtProperty(str, notify=changed)
    def track_error(self) -> str:
        return self._track_error

    @pyqtProperty(str, notify=changed)
    def track_summary(self) -> str:
        track = self._track
        if track is None:
            return ''
        timing = 'UTC times' if track.absolute else 'no dates: starts with the log'
        return f'{len(track.points)} fixes, {track.length() / 1000:.2f} km, {track.duration:.0f} s ({timing})'

    @pyqtProperty('QVariantList', notify=track_changed)
    def track_coordinates(self) -> List[float]:
        """Flat world coordinates [x0, y0, x1, y1, ...] (0..1, Web Mercator) of the track."""
        return media_sync.track_coordinates(media_sync.thin(self._track.points)) if self._track else []

    @pyqtProperty(float, notify=changed)
    def gps_offset(self) -> float:
        return self._manifest.gps_offset if self._manifest else 0.0

    @pyqtSlot(float, result=str)
    def set_gps_offset(self, offset: float) -> str:
        manifest = self._ensure_manifest()
        if manifest is None:
            return ''
        manifest.gps_offset = round(float(offset), 3)
        error = self._save()
        self.changed.emit()
        self._follow_replay()
        return error

    @pyqtProperty('QVariantMap', notify=position_changed)
    def current(self) -> Dict[str, Any]:
        """The GPS position at the replay position: valid, x, y (world), lat, lon, speed_kmh, course, altitude."""
        point = self._current
        if point is None:
            return {'valid': False}
        x, y = media_sync.project(point.lat, point.lon)
        return {
            'valid': True,
            'x': x,
            'y': y,
            'lat': point.lat,
            'lon': point.lon,
            'speed_kmh': point.speed * 3.6 if point.speed is not None else -1.0,
            'course': point.course if point.course is not None else -1.0,
            'altitude': point.altitude if point.altitude is not None else float('nan'),
        }

    # Map

    @pyqtSlot(float, float, result=float)
    def fit_zoom(self, width: float, height: float) -> float:
        return media_sync.fit_zoom(self._track.bounds(), width, height) if self._track else 1.0

    @pyqtSlot(result='QVariantList')
    def track_center(self) -> List[float]:
        """World coordinates of the middle of the track's bounds."""
        if self._track is None:
            return [0.5, 0.5]
        south, west, north, east = self._track.bounds()
        x1, y1 = media_sync.project(north, west)
        x2, y2 = media_sync.project(south, east)
        return [(x1 + x2) / 2, (y1 + y2) / 2]

    @pyqtSlot(float, float, float, float, float, str, result='QVariantList')
    def tiles(self, center_x: float, center_y: float, zoom: float, width: float, height: float, template: str):
        """Map tiles of a view: {url, left, top, size}."""
        return [
            {'url': media_sync.tile_url(template, z, x, y), 'left': left, 'top': top, 'size': size}
            for z, x, y, left, top, size in media_sync.visible_tiles(center_x, center_y, zoom, width, height)
        ]

    @pyqtSlot(float, float, result='QVariantList')
    def to_lat_lon(self, x: float, y: float) -> List[float]:
        return list(media_sync.unproject(x, y))
