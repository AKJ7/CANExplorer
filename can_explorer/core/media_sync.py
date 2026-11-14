"""Camera video and GPS tracks synchronised with CAN log files.

A recording made with a camera gets a sidecar manifest next to the log, ``<log>.media.json``. It ties the
recording's frame clock to the wall clock, so a replay position (seconds since the first frame of a log file) can
be turned into a position in the video, or a time in a GPS track:

- ``clock_offset``: wall-clock time minus frame timestamp, measured while recording (interfaces stamp frames
  with their own clock, which need not be the wall clock).
- ``log_starts``: per log file (a split recording has several), the frame timestamp of its first frame.
- ``video`` and ``video_start``: the video file, and the wall-clock time of its first frame.
- ``gps``: a GPX or NMEA 0183 file attached to the recording afterwards.
- ``video_offset`` and ``gps_offset``: manual corrections in seconds, added to the media time.

GPS tracks with dates (GPX, NMEA with RMC sentences) are aligned by their UTC times; tracks without (NMEA with
only GGA sentences) start with the log.
"""

import json
import math
import re
import xml.etree.ElementTree as ElementTree
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from itertools import pairwise
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from can_explorer.core.log_formats import log_suffix

MANIFEST_SUFFIX = '.media.json'
MANIFEST_VERSION = 1
# Frame timestamps after 2001-09-09 are taken as wall-clock (Unix) times
WALL_CLOCK_THRESHOLD = 1e9
TILE_SIZE = 256
MAX_ZOOM = 19
MAX_TRACK_POINTS = 5000  # Points handed to the map; longer tracks are thinned out
KNOTS = 1852 / 3600  # m/s
EARTH_RADIUS = 6_371_000.0  # m


class MediaError(Exception):
    pass


# Manifest


def manifest_path(log_path: str | Path) -> Path:
    """'drive.blf' -> 'drive.blf.media.json'."""
    log_path = Path(log_path)
    return log_path.with_name(log_path.name + MANIFEST_SUFFIX)


def base_log_path(log_path: str | Path) -> Optional[Path]:
    """The path a numbered file of a split or repeated recording was named after ('drive_003.blf' ->
    'drive.blf'), or None for a file without a number."""
    log_path = Path(log_path)
    suffix = log_suffix(log_path) or log_path.suffix
    stem = log_path.name[: len(log_path.name) - len(suffix)] if suffix else log_path.name
    match = re.fullmatch(r'(.+)_\d{3,}', stem)
    return log_path.with_name(match.group(1) + suffix) if match else None


def find_manifest(log_path: str | Path) -> Optional[Path]:
    """The manifest of a log file: its own, or that of the recording a numbered file belongs to."""
    own = manifest_path(log_path)
    if own.is_file():
        return own
    base = base_log_path(log_path)
    if base is not None and manifest_path(base).is_file():
        return manifest_path(base)
    return None


def video_path_for(log_path: str | Path, suffix: str = '.mp4') -> Path:
    """'drive.asc.gz' -> 'drive.mp4'."""
    log_path = Path(log_path)
    log = log_suffix(log_path) or log_path.suffix
    stem = log_path.name[: len(log_path.name) - len(log)] if log else log_path.name
    return log_path.with_name(stem + suffix)


def _float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class MediaManifest:
    path: Path
    clock_offset: Optional[float] = None
    log_starts: Dict[str, float] = field(default_factory=dict)
    video: str = ''
    video_start: Optional[float] = None
    video_offset: float = 0.0
    gps: str = ''
    gps_offset: float = 0.0

    @classmethod
    def load(cls, path: str | Path) -> 'MediaManifest':
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise MediaError(f'Could not read {path.name}: {e}') from e
        if not isinstance(data, dict):
            raise MediaError(f'{path.name} is not a media manifest')
        starts = data.get('log_starts')
        return cls(
            path=path,
            clock_offset=_float(data.get('clock_offset')),
            log_starts={
                str(name): start
                for name, value in (starts.items() if isinstance(starts, dict) else ())
                if (start := _float(value)) is not None
            },
            video=str(data.get('video') or ''),
            video_start=_float(data.get('video_start')),
            video_offset=_float(data.get('video_offset')) or 0.0,
            gps=str(data.get('gps') or ''),
            gps_offset=_float(data.get('gps_offset')) or 0.0,
        )

    def to_dict(self) -> dict:
        return {
            'version': MANIFEST_VERSION,
            'clock_offset': self.clock_offset,
            'log_starts': dict(self.log_starts),
            'video': self.video,
            'video_start': self.video_start,
            'video_offset': self.video_offset,
            'gps': self.gps,
            'gps_offset': self.gps_offset,
        }

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.to_dict(), indent=2) + '\n', encoding='utf-8')
        except OSError as e:
            raise MediaError(f'Could not write {self.path.name}: {e}') from e

    def resolve(self, name: str) -> Optional[Path]:
        """A file named in the manifest, relative to the manifest's directory."""
        return (self.path.parent / name) if name else None

    def relative_name(self, path: str | Path) -> str:
        """How the manifest refers to a file: relative to its directory when possible."""
        path = Path(path)
        try:
            return path.resolve().relative_to(self.path.parent.resolve()).as_posix()
        except ValueError:
            return str(path)

    def log_start_wall(self, log_name: str) -> Optional[float]:
        """Wall-clock time of the first frame of a log file of the recording."""
        start = self.log_starts.get(log_name)
        if start is None or self.clock_offset is None:
            return None
        return start + self.clock_offset

    def video_position(self, log_name: str, position: float) -> Optional[float]:
        """Seconds into the video at a replay `position` of a log file; None when it cannot be synchronised."""
        start = self.log_start_wall(log_name)
        if start is None or self.video_start is None or not self.video:
            return None
        return start + position - self.video_start + self.video_offset


# GPS tracks


@dataclass(frozen=True)
class TrackPoint:
    time: float  # UTC Unix time, or seconds from the first point for tracks without dates
    lat: float
    lon: float
    speed: Optional[float] = None  # m/s
    course: Optional[float] = None  # Degrees from north
    altitude: Optional[float] = None  # m


@dataclass
class GpsTrack:
    points: List[TrackPoint]
    absolute: bool  # Times are UTC Unix times (else seconds from the first point)

    def __post_init__(self):
        self.points.sort(key=lambda point: point.time)
        self._times = [point.time for point in self.points]

    @property
    def start(self) -> float:
        return self._times[0]

    @property
    def end(self) -> float:
        return self._times[-1]

    @property
    def duration(self) -> float:
        return self.end - self.start

    def at(self, time: float) -> Optional[TrackPoint]:
        """The position at `time`, interpolated between fixes; None outside the track."""
        if not self.points or time < self.start or time > self.end:
            return None
        index = bisect_right(self._times, time)
        if index >= len(self.points):
            return self.points[-1]
        before, after = self.points[index - 1], self.points[index]
        span = after.time - before.time
        fraction = (time - before.time) / span if span > 0 else 0.0

        def mix(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is None or b is None:
                return a if b is None else b
            return a + (b - a) * fraction

        course = before.course
        if before.course is not None and after.course is not None:
            turn = (after.course - before.course + 180) % 360 - 180  # The short way round
            course = (before.course + turn * fraction) % 360
        return TrackPoint(
            time,
            before.lat + (after.lat - before.lat) * fraction,
            before.lon + (after.lon - before.lon) * fraction,
            mix(before.speed, after.speed),
            course,
            mix(before.altitude, after.altitude),
        )

    def bounds(self) -> Tuple[float, float, float, float]:
        """(south, west, north, east)."""
        lats = [point.lat for point in self.points]
        lons = [point.lon for point in self.points]
        return min(lats), min(lons), max(lats), max(lons)

    def length(self) -> float:
        """Distance along the track in metres."""
        return sum(distance(a, b) for a, b in pairwise(self.points))


def distance(a: TrackPoint, b: TrackPoint) -> float:
    """Great-circle distance in metres."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: TrackPoint, b: TrackPoint) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360


def _with_motion(points: List[TrackPoint]) -> List[TrackPoint]:
    """Fill in missing speeds and courses from the neighbouring fixes."""
    result = []
    for index, point in enumerate(points):
        before = points[index - 1] if index > 0 else point
        after = points[index + 1] if index + 1 < len(points) else point
        span = after.time - before.time
        speed, course = point.speed, point.course
        if speed is None and span > 0:
            speed = distance(before, after) / span
        if course is None and (before.lat, before.lon) != (after.lat, after.lon):
            course = bearing(before, after)
        result.append(replace(point, speed=speed, course=course))
    return result


def _gpx_time(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    text = text.strip().replace('Z', '+00:00')
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_gpx(text: str) -> GpsTrack:
    """Track points (trkpt), or else route points (rtept), of a GPX file. Points without a time are skipped."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as e:
        raise MediaError(f'Not a GPX file: {e}') from e

    def local(tag: str) -> str:
        return tag.rsplit('}', 1)[-1]

    def child(element, name: str) -> Optional[str]:
        for item in element:
            if local(item.tag) == name:
                return item.text
        return None

    def points_of(kind: str) -> List[TrackPoint]:
        points = []
        for element in root.iter():
            if local(element.tag) != kind:
                continue
            time = _gpx_time(child(element, 'time'))
            lat, lon = _float(element.get('lat')), _float(element.get('lon'))
            if time is None or lat is None or lon is None:
                continue
            # <speed> and <course> are GPX 1.0 elements (and common extensions)
            speed = _float(next((item.text for item in element.iter() if local(item.tag) == 'speed'), None))
            course = _float(next((item.text for item in element.iter() if local(item.tag) == 'course'), None))
            points.append(TrackPoint(time, lat, lon, speed, course, _float(child(element, 'ele'))))
        return points

    points = points_of('trkpt') or points_of('rtept')
    if not points:
        raise MediaError('The GPX file has no track points with times')
    points.sort(key=lambda point: point.time)
    return GpsTrack(_with_motion(points), absolute=True)


def _nmea_checksum_ok(sentence: str) -> bool:
    if '*' not in sentence:
        return True  # Some loggers drop checksums
    body, checksum = sentence[1:].split('*', 1)
    expected = 0
    for char in body:
        expected ^= ord(char)
    try:
        return int(checksum[:2], 16) == expected
    except ValueError:
        return False


def _nmea_coordinate(value: str, hemisphere: str) -> Optional[float]:
    """'4807.038', 'N' -> 48.1173."""
    if not value or '.' not in value:
        return None
    try:
        degrees_digits = value.index('.') - 2
        result = int(value[:degrees_digits] or 0) + float(value[degrees_digits:]) / 60
    except ValueError:
        return None
    return -result if hemisphere in ('S', 'W') else result


def _nmea_seconds(value: str) -> Optional[float]:
    """'123519.50' -> seconds of the day."""
    if len(value) < 6:
        return None
    try:
        return int(value[0:2]) * 3600 + int(value[2:4]) * 60 + float(value[4:])
    except ValueError:
        return None


def parse_nmea(text: str) -> GpsTrack:
    """Fixes of an NMEA 0183 log: RMC sentences (time, date, position, speed, course) and GGA sentences (time,
    position, altitude), of any talker (GP, GN, GL, ...). Sentences with bad checksums or without a fix are
    skipped. Without any RMC date, times count from the first fix."""
    fixes: Dict[Tuple[int, float], dict] = {}  # (day, seconds of day) -> fields
    date: Optional[datetime] = None
    day = 0
    previous_seconds: Optional[float] = None
    any_date = False
    for line in text.splitlines():
        start = line.find('$')
        if start < 0:
            continue
        sentence = line[start:].strip()
        if not _nmea_checksum_ok(sentence):
            continue
        fields = sentence.split('*', 1)[0].split(',')
        kind = fields[0][3:] if len(fields[0]) == 6 else ''
        if kind == 'RMC' and len(fields) >= 10:
            seconds = _nmea_seconds(fields[1])
            if seconds is None or fields[2] != 'A':
                continue
            stamp = fields[9]
            if len(stamp) == 6:
                try:
                    date = datetime(2000 + int(stamp[4:6]), int(stamp[2:4]), int(stamp[0:2]), tzinfo=timezone.utc)
                except ValueError:
                    pass
                else:
                    any_date = True
                    day, previous_seconds = 0, None  # The date counts from here; no midnight to detect
            values = {
                'lat': _nmea_coordinate(fields[3], fields[4]),
                'lon': _nmea_coordinate(fields[5], fields[6]),
                'speed': (_float(fields[7]) * KNOTS) if _float(fields[7]) is not None else None,
                'course': _float(fields[8]),
            }
        elif kind == 'GGA' and len(fields) >= 10:
            seconds = _nmea_seconds(fields[1])
            if seconds is None or fields[6] in ('', '0'):
                continue
            values = {
                'lat': _nmea_coordinate(fields[2], fields[3]),
                'lon': _nmea_coordinate(fields[4], fields[5]),
                'altitude': _float(fields[9]),
            }
        else:
            continue
        if values.get('lat') is None or values.get('lon') is None:
            continue
        if previous_seconds is not None and seconds < previous_seconds - 12 * 3600:
            day += 1  # Past midnight
        previous_seconds = seconds
        key = ((date.toordinal() if date else 0) + day, seconds)  # The same moment, before or after a new date
        entry = fixes.setdefault(key, {'date': date, 'day': day, 'seconds': seconds})
        entry.update({name: value for name, value in values.items() if value is not None})
    if not fixes:
        raise MediaError('The NMEA log has no position fixes (RMC or GGA)')
    points = []
    for entry in fixes.values():
        if any_date and entry['date'] is None:
            continue  # Before the first date: cannot be placed in time
        if any_date:
            time = (entry['date'] + timedelta(days=entry['day'], seconds=entry['seconds'])).timestamp()
        else:
            time = entry['day'] * 86400 + entry['seconds']
        points.append(
            TrackPoint(time, entry['lat'], entry['lon'], entry.get('speed'), entry.get('course'), entry.get('altitude'))
        )
    points.sort(key=lambda point: point.time)
    if not any_date:
        first = points[0].time
        points = [replace(point, time=point.time - first) for point in points]
    return GpsTrack(_with_motion(points), absolute=any_date)


def load_track(path: str | Path) -> GpsTrack:
    """A GPX file (.gpx) or an NMEA 0183 log (any other suffix)."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        raise MediaError(f'Could not read {path.name}: {e}') from e
    if path.suffix.lower() == '.gpx' or text.lstrip().startswith('<'):
        return parse_gpx(text)
    return parse_nmea(text)


def track_time(
    track: GpsTrack,
    position: float,
    log_start_wall: Optional[float] = None,
    first_timestamp: Optional[float] = None,
    offset: float = 0.0,
) -> float:
    """The track time at a replay `position`.

    A dated track is aligned by wall-clock time: from the manifest (`log_start_wall`), else from the log's first
    frame timestamp when it is a wall-clock time. Otherwise the track starts with the log.
    """
    if track.absolute:
        start = log_start_wall
        if start is None and first_timestamp is not None and first_timestamp >= WALL_CLOCK_THRESHOLD:
            start = first_timestamp
        if start is not None:
            return start + position + offset
    return track.start + position + offset


def thin(points: List[TrackPoint], limit: int = MAX_TRACK_POINTS) -> List[TrackPoint]:
    """At most `limit` points, evenly picked, always keeping the last."""
    if len(points) <= limit:
        return list(points)
    step = len(points) / (limit - 1)
    picked = [points[int(index * step)] for index in range(limit - 1)]
    return picked + [points[-1]]


# Web Mercator (the projection of OpenStreetMap tiles)


def project(lat: float, lon: float) -> Tuple[float, float]:
    """World coordinates in 0..1 (x east, y south) of a latitude and longitude."""
    lat = max(-85.05112878, min(85.05112878, lat))
    x = (lon + 180.0) / 360.0
    sin = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)
    return x, y


def unproject(x: float, y: float) -> Tuple[float, float]:
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y))))
    return lat, lon


def fit_zoom(bounds: Tuple[float, float, float, float], width: float, height: float, margin: float = 0.1) -> float:
    """The largest zoom (fractional) at which the bounds fit into a view of `width` x `height` pixels."""
    south, west, north, east = bounds
    x1, y1 = project(north, west)
    x2, y2 = project(south, east)
    span_x, span_y = max(abs(x2 - x1), 1e-12), max(abs(y2 - y1), 1e-12)
    usable = 1 - 2 * margin
    zoom_x = math.log2(max(width, 1) * usable / (span_x * TILE_SIZE))
    zoom_y = math.log2(max(height, 1) * usable / (span_y * TILE_SIZE))
    return max(0.0, min(float(MAX_ZOOM), zoom_x, zoom_y))


def visible_tiles(
    center_x: float, center_y: float, zoom: float, width: float, height: float
) -> List[Tuple[int, int, int, float, float, float]]:
    """Tiles covering a view centred on world coordinates (`center_x`, `center_y`) at a fractional `zoom`:
    (z, x, y, left, top, size) with the tile's position and size in view pixels. x wraps around the world."""
    z = max(0, min(MAX_ZOOM, int(math.floor(zoom))))
    count = 2**z
    size = TILE_SIZE * 2 ** (zoom - z)  # Tiles are scaled between integer zooms
    world = size * count
    left_world = center_x * world - width / 2
    top_world = center_y * world - height / 2
    # The last tile is the one containing the view's last pixel, not the one starting at its edge
    first_x, last_x = math.floor(left_world / size), math.ceil((left_world + width) / size) - 1
    first_y = max(0, math.floor(top_world / size))
    last_y = min(count - 1, math.ceil((top_world + height) / size) - 1)
    tiles = []
    for ty in range(first_y, last_y + 1):
        for tx in range(first_x, last_x + 1):
            tiles.append((z, tx % count, ty, tx * size - left_world, ty * size - top_world, size))
    return tiles


def tile_url(template: str, z: int, x: int, y: int) -> str:
    """'https://tile.openstreetmap.org/{z}/{x}/{y}.png' -> the URL of one tile."""
    return template.replace('{z}', str(z)).replace('{x}', str(x)).replace('{y}', str(y))


def track_coordinates(points: Iterable[TrackPoint]) -> List[float]:
    """Flat [x0, y0, x1, y1, ...] world coordinates of track points, for drawing."""
    flat: List[float] = []
    for point in points:
        flat.extend(project(point.lat, point.lon))
    return flat
