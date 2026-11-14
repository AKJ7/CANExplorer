import datetime

import can

from can_explorer.build_info import build_info
from can_explorer.core.channel_detection import ERROR, FOUND, BackendProbe
from can_explorer.sys.crash_log import PREVIOUS_CRASH_LOG
from can_explorer.sys.system_report import application_header, system_report

PROBES = [
    BackendProbe('etas', ERROR, detail="OSError('CSI_CreateProtocolTree failed')", seconds=0.3, alone=True),
    BackendProbe('virtual', FOUND, [{'interface': 'virtual', 'channel': 'bench'}], seconds=0.01),
]


def test_application_header():
    header = application_header()
    assert header[0].startswith(f'CANExplorer {build_info().describe()}, started ')
    assert f'python-can {can.__version__}' in header
    assert any(line.startswith('PyQt ') for line in header)


def test_report_with_detection_and_crash_report(tmp_path):
    (tmp_path / PREVIOUS_CRASH_LOG).write_text('Fatal Python error: Segmentation fault\n')
    when = datetime.datetime(2026, 11, 13, 12, 0, tzinfo=datetime.timezone.utc)
    text = system_report(['CANExplorer 1.7.1 (from source)', 'Python 3.14.0'], PROBES, when, tmp_path)
    assert text.splitlines() == [
        'CANExplorer system report',
        '',
        '```',
        'CANExplorer 1.7.1 (from source)',
        'Python 3.14.0',
        '```',
        '',
        'Channel detection (2026-11-13T12:00:00+00:00):',
        '```',
        "etas: error after 0.30 s: OSError('CSI_CreateProtocolTree failed') [probed alone]",
        'virtual: 1 channel(s) in 0.01 s: bench',
        '```',
        '',
        f'The last crash left a crash report: {tmp_path / PREVIOUS_CRASH_LOG} (please attach it too)',
    ]


def test_report_before_detection_without_crash(tmp_path):
    text = system_report(['CANExplorer 1.7.1'], [], crash_dir=tmp_path)
    assert 'Channel detection: not run' in text and text.endswith('No crash report\n')
