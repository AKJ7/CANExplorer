import can
from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtGui import QGuiApplication

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


def test_copy_and_select_all(qapp):
    model = TraceModel(lambda: 10.0, max_rows=100, describe=lambda frame: f'info {frame.message.arbitration_id:X}')
    model.append_frames([frame(i, bytes([i])) for i in range(3)])
    selection = QItemSelectionModel(model)
    assert model.copy_selection(selection) == 0
    model.select_all(selection)
    assert model.selected_rows(selection) == [0, 1, 2]
    assert model.copy_selection(selection) == 3
    lines = QGuiApplication.clipboard().text().splitlines()
    assert lines[0].startswith('Time [s]\tDir')
    assert lines[2].split('\t')[3:] == ['001', '1', '01', 'info 1']
    selection.clearSelection()
    selection.select(
        model.index(1, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    )
    assert [f.message.arbitration_id for f in model.selected_frames(selection)] == [1]
    assert len(model.selected_frames(None)) == 3


def test_overview_refreshes_only_changed_rows(qapp):
    statistics = IdStatistics()
    model = OverviewModel(statistics)
    for can_id in range(10):
        statistics.update(frame(can_id, b'\x00'))
    model.refresh(*statistics.drain_changes())
    changes = []
    model.dataChanged.connect(lambda top, bottom: changes.append((top.row(), bottom.row())))
    for can_id in (2, 3, 4, 7):
        statistics.update(frame(can_id, b'\x01'))
    model.refresh(*statistics.drain_changes())
    assert changes == [(2, 4), (7, 7)]
    selection = QItemSelectionModel(model)
    model.select_all(selection)
    model.copy_selection(selection)
    assert '<b>' not in QGuiApplication.clipboard().text()


def test_overview_highlight_color(qapp):
    statistics = IdStatistics()
    model = OverviewModel(statistics)
    statistics.update(frame(0x100, b'\x01\x02', timestamp=1.0))
    statistics.update(frame(0x100, b'\x01\x03', timestamp=1.1))
    model.refresh(*statistics.drain_changes())
    changes = []
    model.dataChanged.connect(lambda top, bottom: changes.append((top.column(), bottom.column())))
    model.highlight_color = '#f39c12'
    assert changes == [(6, 6)]
    assert cell(model, 0, 6) == '01 <font color="#f39c12"><b>03</b></font>'


def test_overview_sorting(qapp):
    statistics = IdStatistics()
    model = OverviewModel(statistics)
    for can_id, count in ((0x300, 1), (0x100, 3), (0x200, 2)):
        for i in range(count):
            statistics.update(frame(can_id, bytes([can_id >> 8]), timestamp=1.0 + i))
    model.refresh(*statistics.drain_changes())

    def ids():
        return [cell(model, row, 0) for row in range(model.rowCount())]

    assert ids() == ['100', '200', '300']
    model.sort_by(3)  # Count, ascending
    assert ids() == ['300', '200', '100'] and model.headerData(3, Qt.Orientation.Horizontal) == 'Count ▲'
    model.sort_by(3)
    assert ids() == ['100', '200', '300'] and model.headerData(3, Qt.Orientation.Horizontal) == 'Count ▼'

    # Live updates keep the order, and selections stay on their identifiers
    selection = QItemSelectionModel(model)
    selection.select(
        model.index(2, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    )
    for i in range(5):
        statistics.update(frame(0x300, b'\x03', timestamp=5.0 + i))
    model.refresh(*statistics.drain_changes())
    assert ids() == ['300', '100', '200']
    assert model.selected_rows(selection) == [0]

    model.sort_by(4)  # Cycle time: 0x300 has none
    model.sort_by(4)
    model.sort_by(4)  # Third click: back to the identifiers
    assert ids() == ['100', '200', '300'] and model.headerData(4, Qt.Orientation.Horizontal) == 'Cycle [ms]'

    # New identifiers are inserted in the sort order
    model.sort_by(6)
    statistics.update(frame(0x050, b'\x02', timestamp=9.0))
    model.refresh(*statistics.drain_changes())
    assert ids() == ['100', '050', '200', '300']
