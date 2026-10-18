import can
from PyQt6.QtCore import Qt

from can_explorer.core.filters import MessageFilter
from can_explorer.core.frames import Frame
from can_explorer.core.store import IdStatistics
from can_explorer.gui.models import RICH_TEXT_ROLE, OverviewModel, TraceModel


def frame(can_id, data=b'', timestamp=10.0, info=''):
    return Frame(can.Message(arbitration_id=can_id, is_extended_id=False, data=data, timestamp=timestamp), info)


def cell(model, row, column, role=Qt.ItemDataRole.DisplayRole):
    return model.data(model.index(row, column), role)


def test_trace_model_is_bounded(qapp):
    model = TraceModel(lambda: 10.0, max_rows=5)
    model.append_frames([frame(i, timestamp=10.0 + i) for i in range(4)])
    model.append_frames([frame(i) for i in range(4, 8)])
    assert model.rowCount() == 5
    assert [cell(model, row, 3) for row in range(5)] == ['003', '004', '005', '006', '007']
    assert cell(model, 0, 0) == '3.000000'


def test_trace_model_filter(qapp):
    model = TraceModel(lambda: None, max_rows=100)
    frames = [frame(0x100, info='a'), frame(0x200, info='b')]
    model.append_frames(frames)
    model.set_filter(MessageFilter.parse('200'), frames)
    assert model.rowCount() == 1 and cell(model, 0, 6) == 'b'
    model.append_frames([frame(0x100), frame(0x200)])
    assert model.rowCount() == 2


def test_overview_model_highlights_changes(qapp):
    statistics = IdStatistics()
    model = OverviewModel(statistics)
    statistics.update(frame(0x200, b'\x01\x02', timestamp=1.0))
    statistics.update(frame(0x100, b'\x01', timestamp=1.0))
    model.refresh(*statistics.drain_changes())
    assert [cell(model, row, 0) for row in range(2)] == ['100', '200']
    statistics.update(frame(0x200, b'\x01\x03', timestamp=1.1))
    model.refresh(*statistics.drain_changes())
    assert cell(model, 1, 3) == '2'
    assert cell(model, 1, 4) == '100.0'
    assert cell(model, 1, 6) == '01 <b>03</b>'
    assert cell(model, 1, 6, RICH_TEXT_ROLE) is True
    assert cell(model, 1, 7, RICH_TEXT_ROLE) is False
