import json
import math
from datetime import datetime, timezone

import pytest

from can_explorer.core import media_sync
from can_explorer.core.media_sync import GpsTrack, MediaError, MediaManifest, TrackPoint


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def nmea(body: str) -> str:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f'${body}*{checksum:02X}'


# Manifest


def test_manifest_and_video_paths(tmp_path):
    assert media_sync.manifest_path(tmp_path / 'drive.blf') == tmp_path / 'drive.blf.media.json'
    assert media_sync.video_path_for(tmp_path / 'drive.asc.gz') == tmp_path / 'drive.mp4'
    assert media_sync.base_log_path(tmp_path / 'drive_003.blf') == tmp_path / 'drive.blf'
    assert media_sync.base_log_path(tmp_path / 'drive_003.asc.gz') == tmp_path / 'drive.asc.gz'
    assert media_sync.base_log_path(tmp_path / 'drive.blf') is None


def test_find_manifest_of_numbered_files(tmp_path):
    assert media_sync.find_manifest(tmp_path / 'drive_002.blf') is None
    MediaManifest(media_sync.manifest_path(tmp_path / 'drive.blf')).save()
    assert media_sync.find_manifest(tmp_path / 'drive_002.blf') == tmp_path / 'drive.blf.media.json'
    MediaManifest(media_sync.manifest_path(tmp_path / 'drive_002.blf')).save()
    assert media_sync.find_manifest(tmp_path / 'drive_002.blf') == tmp_path / 'drive_002.blf.media.json'


def test_manifest_round_trip(tmp_path):
    path = tmp_path / 'drive.blf.media.json'
    manifest = MediaManifest(path, 0.25, {'drive_001.blf': 100.0}, 'drive.mp4', 1000.0, -0.5, 'track.gpx', 2.0)
    manifest.save()
    assert json.loads(path.read_text())['version'] == 1
    assert MediaManifest.load(path) == manifest


def test_invalid_manifests(tmp_path):
    path = tmp_path / 'bad.media.json'
    path.write_text('[1, 2]')
    with pytest.raises(MediaError):
        MediaManifest.load(path)
    path.write_text('{"clock_offset": "x", "log_starts": {"a.blf": "nope", "b.blf": 3}, "video_offset": null}')
    manifest = MediaManifest.load(path)
    assert manifest.clock_offset is None and manifest.log_starts == {'b.blf': 3.0} and manifest.video_offset == 0.0
    with pytest.raises(MediaError):
        MediaManifest.load(tmp_path / 'missing.json')


def test_video_position(tmp_path):
    # The interface clock runs 1000 s behind the wall clock; the video started 2 s before the second file
    manifest = MediaManifest(
        tmp_path / 'm.json', clock_offset=1000.0, log_starts={'a.blf': 50.0, 'b.blf': 60.0}, video='v.mp4'
    )
    assert manifest.video_position('a.blf', 1.0) is None  # No video start yet
    manifest.video_start = 1058.0
    assert manifest.video_position('b.blf', 0.0) == pytest.approx(2.0)
    assert manifest.video_position('a.blf', 3.0) == pytest.approx(-5.0)  # Before the video
    manifest.video_offset = 0.5
    assert manifest.video_position('b.blf', 1.0) == pytest.approx(3.5)
    assert manifest.video_position('unknown.blf', 1.0) is None


def test_relative_name(tmp_path):
    manifest = MediaManifest(tmp_path / 'm.json')
    assert manifest.relative_name(tmp_path / 'sub' / 'track.gpx') == 'sub/track.gpx'
    assert manifest.resolve('sub/track.gpx') == tmp_path / 'sub' / 'track.gpx'
    outside = tmp_path.parent / 'elsewhere.gpx'
    assert manifest.relative_name(outside) == str(outside)


# GPX

GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="48.0" lon="11.0"><ele>500</ele><time>2026-11-13T10:00:00Z</time></trkpt>
    <trkpt lat="48.001" lon="11.0"><ele>510</ele><time>2026-11-13T10:00:10Z</time></trkpt>
    <trkpt lat="48.002" lon="11.0"><time>2026-11-13T10:00:20+00:00</time></trkpt>
    <trkpt lat="48.5" lon="11.5"></trkpt>
  </trkseg></trk>
</gpx>"""


def test_parse_gpx():
    track = media_sync.parse_gpx(GPX)
    assert track.absolute and len(track.points) == 3  # The point without a time is skipped
    assert track.start == utc(2026, 11, 13, 10, 0, 0) and track.duration == 20
    first = track.points[0]
    assert first.altitude == 500 and first.course == pytest.approx(0.0, abs=1e-6)
    assert first.speed == pytest.approx(111.2 / 10, rel=0.01)  # About 111 m per 0.001° of latitude
    assert track.length() == pytest.approx(222.4, rel=0.01)


def test_parse_gpx_route_points_and_errors():
    route = '<gpx><rte><rtept lat="1" lon="2"><time>2026-01-01T00:00:00Z</time></rtept></rte></gpx>'
    assert media_sync.parse_gpx(route).points[0].lat == 1.0
    with pytest.raises(MediaError):
        media_sync.parse_gpx('<gpx></gpx>')
    with pytest.raises(MediaError):
        media_sync.parse_gpx('not xml')


# NMEA


def test_parse_nmea_merges_rmc_and_gga():
    lines = [
        'garbage',
        nmea('GPGGA,235959.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,'),  # Before any date
        nmea('GNRMC,235959.00,A,4807.038,N,01131.000,E,10.0,90.0,131126,,,A'),
        nmea('GNGGA,235959.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,'),
        '2026-11-13 12:00 ' + nmea('GPGGA,000001.00,4807.040,N,01131.010,E,1,08,0.9,546.0,M,46.9,M,,'),
        nmea('GPRMC,000001.00,A,4807.040,N,01131.010,E,10.0,92.0,141126,,,A'),
        nmea('GPRMC,000002.00,V,,,,,,,141126,,,N'),  # No fix
        '$GPRMC,000003.00,A,4807.042,N,01131.020,E,10.0,92.0,141126,,,A*00',  # Bad checksum
        nmea('GPGGA,000004.00,4807.044,S,01131.030,W,0,00,,,M,,M,,'),  # Fix quality 0
    ]
    track = media_sync.parse_nmea('\n'.join(lines))
    assert track.absolute and len(track.points) == 2
    first, second = track.points
    assert first.time == utc(2026, 11, 13, 23, 59, 59) and second.time == utc(2026, 11, 14, 0, 0, 1)
    assert first.lat == pytest.approx(48 + 7.038 / 60) and first.lon == pytest.approx(11 + 31 / 60)
    assert first.speed == pytest.approx(10 * 1852 / 3600) and first.course == 90.0 and first.altitude == 545.4


def test_parse_nmea_without_dates_starts_at_zero():
    lines = [
        nmea('GPGGA,235958.0,0100.000,S,00200.000,W,1,08,0.9,10,M,0,M,,'),
        nmea('GPGGA,000002.0,0100.060,S,00200.000,W,1,08,0.9,10,M,0,M,,'),  # Past midnight
    ]
    track = media_sync.parse_nmea('\n'.join(lines))
    assert not track.absolute and [point.time for point in track.points] == [0.0, 4.0]
    assert track.points[0].lat == -1.0 and track.points[0].lon == -2.0
    assert track.points[0].course == pytest.approx(180.0)  # Moving south


def test_parse_nmea_without_fixes():
    with pytest.raises(MediaError):
        media_sync.parse_nmea('hello\n' + nmea('GPRMC,000002.00,V,,,,,,,141126,,,N'))


def test_load_track(tmp_path):
    (tmp_path / 'a.gpx').write_text(GPX)
    (tmp_path / 'b.log').write_text(nmea('GPRMC,120000.00,A,4807.038,N,01131.000,E,0.0,0.0,131126,,,A'))
    assert media_sync.load_track(tmp_path / 'a.gpx').absolute
    assert len(media_sync.load_track(tmp_path / 'b.log').points) == 1
    with pytest.raises(MediaError):
        media_sync.load_track(tmp_path / 'missing.gpx')


# Positions and alignment


def test_track_interpolation():
    track = GpsTrack(
        [TrackPoint(10.0, 1.0, 2.0, 10.0, 350.0, 100.0), TrackPoint(0.0, 0.0, 0.0, 0.0, 10.0, None)], absolute=False
    )
    assert track.start == 0.0  # Sorted
    middle = track.at(5.0)
    assert (middle.lat, middle.lon, middle.speed) == (0.5, 1.0, 5.0)
    assert middle.course == pytest.approx(0.0)  # Turning the short way, through north
    assert middle.altitude == 100.0
    assert track.at(10.0).lat == 1.0 and track.at(-0.1) is None and track.at(10.1) is None
    assert track.bounds() == (0.0, 0.0, 1.0, 2.0)


def test_track_time_alignment():
    dated = GpsTrack([TrackPoint(utc(2026, 1, 1), 0, 0), TrackPoint(utc(2026, 1, 1, 0, 10), 0, 0)], absolute=True)
    wall = utc(2026, 1, 1, 0, 1)
    assert media_sync.track_time(dated, 5.0, log_start_wall=wall) == wall + 5
    assert media_sync.track_time(dated, 5.0, first_timestamp=wall, offset=1.0) == wall + 6
    # A log with relative timestamps and no manifest: the track starts with the log
    assert media_sync.track_time(dated, 5.0, first_timestamp=0.0) == dated.start + 5
    undated = GpsTrack([TrackPoint(0.0, 0, 0), TrackPoint(9.0, 0, 0)], absolute=False)
    assert media_sync.track_time(undated, 3.0, log_start_wall=wall, offset=-1.0) == 2.0


def test_thin():
    points = [TrackPoint(float(i), 0, 0) for i in range(100)]
    thinned = media_sync.thin(points, 10)
    assert len(thinned) == 10 and thinned[0] is points[0] and thinned[-1] is points[-1]
    assert media_sync.thin(points[:5], 10) == points[:5]


# Web Mercator


def test_projection():
    assert media_sync.project(0, 0) == pytest.approx((0.5, 0.5))
    assert media_sync.project(0, 180) == pytest.approx((1.0, 0.5))
    x, y = media_sync.project(48.137, 11.575)
    assert media_sync.unproject(x, y) == pytest.approx((48.137, 11.575))
    assert media_sync.project(90, 0)[1] == pytest.approx(0.0, abs=1e-6)  # Clamped to the map's edge
    assert media_sync.track_coordinates([TrackPoint(0, 0, 0)]) == pytest.approx([0.5, 0.5])


def test_fit_zoom():
    # One tile's worth of world (zoom 0) fits a 256-pixel view without margin
    assert media_sync.fit_zoom((-85.05112878, -180, 85.05112878, 180), 256, 256, margin=0) == pytest.approx(0.0)
    small = media_sync.fit_zoom((48.0, 11.0, 48.01, 11.01), 800, 600)
    assert 13 < small < 16
    assert media_sync.fit_zoom((48.0, 11.0, 48.0, 11.0), 800, 600) == media_sync.MAX_ZOOM  # A single point


def test_visible_tiles():
    tiles = media_sync.visible_tiles(0.5, 0.5, 1.0, 512, 512)
    assert sorted((z, x, y) for z, x, y, *_ in tiles) == [(1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)]
    assert all(size == 256 and left in (0, 256) and top in (0, 256) for *_, left, top, size in tiles)
    # Fractional zoom scales the tiles; x wraps around the antimeridian; y stops at the poles
    tiles = media_sync.visible_tiles(0.0, 0.5, 1.5, 300, 300)
    assert all(size == pytest.approx(256 * math.sqrt(2)) for *_, size in tiles)
    assert {x for _, x, *_ in tiles} == {0, 1}
    assert all(0 <= y <= 1 for _, _, y, *_ in tiles)
    assert media_sync.visible_tiles(0.5, 0.0, 0.0, 256, 256)[0][2] == 0


def test_tile_url():
    url = media_sync.tile_url('https://tile.openstreetmap.org/{z}/{x}/{y}.png', 3, 4, 5)
    assert url == 'https://tile.openstreetmap.org/3/4/5.png'
