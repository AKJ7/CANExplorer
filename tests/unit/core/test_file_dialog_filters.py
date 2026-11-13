import pytest

from can_explorer.core.file_dialog_filters import default_suffix, filter_extensions, with_default_suffix
from can_explorer.core.log_formats import file_filters


@pytest.mark.parametrize(
    'name_filter, expected',
    [
        ('CSV files (*.csv)', ['csv']),
        ('CAN log files (*.asc *.blf *.asc.gz)', ['asc', 'blf', 'asc.gz']),
        ('All files (*)', []),
        ('Logs (*.asc *)', ['asc']),
        ('Python files (*.py)', ['py']),
        ('No patterns', []),
        ('', []),
    ],
)
def test_filter_extensions(name_filter, expected):
    assert filter_extensions(name_filter) == expected


def test_every_log_format_filter_has_its_suffix():
    for name_filter in file_filters()[1:]:
        assert len(filter_extensions(name_filter)) == 1


def test_default_suffix_prefers_the_filter():
    assert default_suffix('Compressed ASC (*.asc.gz)', 'blf') == 'asc.gz'
    assert default_suffix('All files (*)', 'blf') == 'blf'
    assert default_suffix('', '.csv') == 'csv'
    assert default_suffix('All files (*)') == ''


@pytest.mark.parametrize(
    'path, suffix, expected',
    [
        ('/tmp/trace', 'asc', '/tmp/trace.asc'),
        ('/tmp/trace', '.asc', '/tmp/trace.asc'),
        ('/tmp/trace.blf', 'asc', '/tmp/trace.blf'),
        ('/tmp/trace.asc.gz', 'asc', '/tmp/trace.asc.gz'),
        ('/tmp/v1.2/trace', 'asc', '/tmp/v1.2/trace.asc'),
        ('C:\\logs\\trace', 'asc', 'C:\\logs\\trace.asc'),
        ('/tmp/.hidden', 'asc', '/tmp/.hidden.asc'),
        ('/tmp/trace', '', '/tmp/trace'),
        ('', 'asc', ''),
    ],
)
def test_with_default_suffix(path, suffix, expected):
    assert with_default_suffix(path, suffix) == expected
