"""Headless GUI tests (QT_QPA_PLATFORM=offscreen is set in conftest.py)."""

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from seizure_marker.app import MarkerWindow  # noqa: E402
from seizure_marker.export import export_workbook  # noqa: E402
from seizure_marker.session import Session  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_review_flow_and_export(qapp, synthetic_rec, tmp_path):
    w = MarkerWindow(Session(tmp_path), [synthetic_rec])
    w.load_sync(0)
    rec = w.current
    assert len(rec.marks) == 2

    # event 1: untouched -> Accept keeps the detector's placement
    assert "Accept detector" in w.b_accept.text()
    assert not w.b_reset.isEnabled()
    w.accept_event()
    assert rec.marks[0].status == "auto"

    # auto-advanced to event 2: drag the offset, reset, drag again, save
    assert w.mi == 1
    m = rec.marks[1]
    w.off_line.setValue(m.auto_offset - 3)
    assert "Save my adjusted" in w.b_accept.text() and w.b_reset.isEnabled()
    w.reset_cursors()
    assert w.off_line.value() == m.auto_offset
    w.off_line.setValue(m.auto_offset - 3)
    w.accept_event()
    assert m.status == "edited" and m.offset == m.auto_offset - 3

    # add a seizure by hand, then reject it
    w.add_seizure()
    assert "Save this seizure" in w.b_accept.text()
    w.accept_event()
    assert [k.status for k in rec.marks].count("added") == 1
    w.reject_event()                       # the view moved on; reject whatever is shown
    assert any(k.status == "rejected" for k in rec.marks) or w.mark is None

    # export: one sheet for the mouse + Parameters
    out = tmp_path / "marks.xlsx"
    n = export_workbook(w.session, out)
    assert n >= 3 and out.exists()
    import openpyxl
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["0001", "Parameters"]
    ws = wb["0001"]
    assert [c.value for c in ws[1]][:4] == ["Dataset", "Mouse", "Channel", "Date"]
