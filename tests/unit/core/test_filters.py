import can
import pytest

from can_explorer.core.filters import MessageFilter


def message(can_id=0x100, extended=False, rx=True, fd=False):
    return can.Message(arbitration_id=can_id, is_extended_id=extended, is_rx=rx, is_fd=fd)


def test_empty_filter_matches_everything():
    message_filter = MessageFilter.parse('  ')
    assert message_filter.is_empty
    assert message_filter.matches(message())


def test_ids_and_ranges():
    message_filter = MessageFilter.parse('0x100, 200-2FF')
    assert message_filter.matches(message(0x100))
    assert message_filter.matches(message(0x250))
    assert not message_filter.matches(message(0x101))


def test_exclusion():
    message_filter = MessageFilter.parse('!100-1FF')
    assert not message_filter.matches(message(0x150))
    assert message_filter.matches(message(0x200))


def test_keywords():
    assert not MessageFilter.parse('ext').matches(message(extended=False))
    assert MessageFilter.parse('std ext').matches(message(extended=True))
    assert not MessageFilter.parse('tx').matches(message(rx=True))
    assert MessageFilter.parse('fd').matches(message(fd=True))
    assert not MessageFilter.parse('fd').matches(message(fd=False))


@pytest.mark.parametrize('text', ['xyz', '200-100', '!', '1-2-3'])
def test_invalid_terms(text):
    with pytest.raises(ValueError):
        MessageFilter.parse(text)
