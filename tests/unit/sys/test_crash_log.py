import datetime
import faulthandler
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from can_explorer.sys.crash_log import (
    CRASH_LOG,
    PREVIOUS_CRASH_LOG,
    REPORT_MARKER,
    CrashLog,
    crash_log_dir,
    report_header,
)


@pytest.fixture(autouse=True)
def restore_faulthandler():
    # CrashLog takes over faulthandler, which pytest enables for itself
    was_enabled = faulthandler.is_enabled()
    hook = sys.excepthook
    yield
    sys.excepthook = hook
    if was_enabled:
        faulthandler.enable(file=sys.__stderr__, all_threads=True)


def test_crash_log_dir_per_platform():
    home = Path('/home/user')
    assert crash_log_dir({}, 'linux', home) == home / '.local' / 'state' / 'CANExplorer'
    assert crash_log_dir({'XDG_STATE_HOME': '/state'}, 'linux', home) == Path('/state/CANExplorer')
    assert crash_log_dir({'LOCALAPPDATA': 'C:/Users/u/AppData/Local'}, 'win32', home) == Path(
        'C:/Users/u/AppData/Local/CANExplorer'
    )
    assert crash_log_dir({}, 'win32', home) == home / 'AppData' / 'Local' / 'CANExplorer'
    assert crash_log_dir({}, 'darwin', home) == home / 'Library' / 'Logs' / 'CANExplorer'


def test_report_header_describes_the_run():
    now = datetime.datetime(2026, 11, 13, 12, 0, tzinfo=datetime.timezone.utc)
    environ = {'QT_QPA_PLATFORMTHEME': 'generic', 'WAYLAND_DISPLAY': 'wayland-0', 'HOME': '/home/user'}
    lines = report_header('1.7.1', ['PyQt 6.9.1, Qt 6.9.2'], environ, now)
    assert lines[0] == 'CANExplorer 1.7.1, started 2026-11-13T12:00:00+00:00'
    assert lines[1].startswith('Python ') and lines[2].startswith('Platform: ')
    assert lines[3:] == ['PyQt 6.9.1, Qt 6.9.2', 'QT_QPA_PLATFORMTHEME=generic', 'WAYLAND_DISPLAY=wayland-0']


def test_a_clean_run_leaves_no_report(tmp_path):
    crash_log = CrashLog(tmp_path / 'dir')
    assert crash_log.start(['CANExplorer test']) is None
    assert crash_log.enabled and faulthandler.is_enabled()
    assert crash_log.path.read_text().splitlines() == ['CANExplorer test', REPORT_MARKER]
    crash_log.stop()
    assert not crash_log.enabled and not faulthandler.is_enabled()
    assert not crash_log.path.exists()
    assert CrashLog(tmp_path / 'dir').start([]) is None  # Nothing from the clean run


def test_a_killed_run_is_not_reported_as_a_crash(tmp_path):
    (tmp_path / CRASH_LOG).write_text(f'CANExplorer test\n{REPORT_MARKER}\n')
    crash_log = CrashLog(tmp_path)
    assert crash_log.start([]) is None
    crash_log.stop()
    assert not (tmp_path / PREVIOUS_CRASH_LOG).exists()


def test_a_crashed_run_is_kept_as_the_previous_report(tmp_path):
    report = f'CANExplorer test\n{REPORT_MARKER}\nFatal Python error: Segmentation fault\n'
    (tmp_path / CRASH_LOG).write_text(report)
    crash_log = CrashLog(tmp_path)
    assert crash_log.start(['next run']) == tmp_path / PREVIOUS_CRASH_LOG
    assert (tmp_path / PREVIOUS_CRASH_LOG).read_text() == report
    assert crash_log.path.read_text().startswith('next run\n')
    crash_log.stop()
    assert (tmp_path / PREVIOUS_CRASH_LOG).exists()  # Kept until a later crash replaces it


def test_an_unwritable_directory_runs_without_a_report(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('')
    crash_log = CrashLog(blocker / 'dir')  # A directory below a file cannot be created
    assert crash_log.start([]) is None
    assert not crash_log.enabled
    crash_log.stop()


def test_uncaught_exceptions_are_recorded(tmp_path):
    crash_log = CrashLog(tmp_path)
    seen = []
    sys.excepthook = lambda *args: seen.append(args[0])
    crash_log.start([])
    try:
        raise RuntimeError('from a slot')
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert seen == [RuntimeError]  # The previous hook still runs
    assert 'RuntimeError: from a slot' in crash_log.path.read_text()
    crash_log.stop()
    assert sys.excepthook is not crash_log._excepthook


CRASHING_RUN = textwrap.dedent(
    """
    import faulthandler, sys
    from pathlib import Path
    from can_explorer.sys.crash_log import CrashLog
    previous = CrashLog(Path(sys.argv[1])).start(['CANExplorer test'])
    print(previous or '', flush=True)
    if sys.argv[2] == 'crash':
        faulthandler._sigsegv()
    """
)


def _run(directory: Path, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-c', CRASHING_RUN, str(directory), mode], capture_output=True, text=True, timeout=60
    )


def test_a_segmentation_fault_writes_a_report_found_at_the_next_start(tmp_path):
    crashed = _run(tmp_path, 'crash')
    assert crashed.returncode != 0
    report = (tmp_path / CRASH_LOG).read_text()
    header, _, traceback = report.partition(REPORT_MARKER)
    assert header == 'CANExplorer test\n'
    assert 'Segmentation fault' in traceback or 'access violation' in traceback  # Unix / Windows wording
    assert 'File "<string>"' in traceback  # The Python stack of the crash
    following = _run(tmp_path, 'exit')
    assert following.stdout.strip() == str(tmp_path / PREVIOUS_CRASH_LOG)
    assert (tmp_path / PREVIOUS_CRASH_LOG).read_text() == report
