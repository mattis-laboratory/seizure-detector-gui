"""
Seizure On/Off Marker — interactive review of detector-placed onset/offset
cursors (PyQt6 + pyqtgraph). Entry point: `python -m seizure_marker` (see
__main__.py); user guide: docs/USER_GUIDE.md.

Layout:
  row 1     Open recording(s)… | recording queue | channel | Save image(s)… | Export to Excel
  row 2     Preset ▾ | Save preset… | Sensitivity slider | Advanced ▾ (raw numbers, sampling-rate override)
  top       zoomed-out whole recording, every event marked, draggable yellow zoom box
  middle    zoomed-in view with draggable ONSET (green) / OFFSET (orange) cursors
  buttons   Prev / Next event · Accept/Save (one adaptive button) · Reset to detector · Not a seizure · Add a missed seizure
  bottom    table of committed timestamps (autosaved to <out_dir>/marker_session.json)

Keys: ←/→ nudge the active cursor 0.1 s (Shift: 1 s), Tab switches cursor,
Enter accepts/saves, Delete rejects, R resets the cursors, N / P next / previous event.
"""

from __future__ import annotations

import copy
import traceback
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMainWindow, QMessageBox, QProgressDialog,
                             QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import detector as oo
from . import export as ex
from .session import (COMMITTED, DEFAULT_PROFILE, PARAM_DOC, SENSITIVITY, USER_PRESETS_PATH, Mark, Recording, Session,
                      delete_preset, effective_threshold, envelope, load_presets, load_signal, profile_value,
                      run_detector, save_preset)

pg.setConfigOptions(antialias=False, background="w", foreground="k")

C_TRACE, C_ON, C_OFF = "#4C72B0", "#009E73", "#D55E00"
STATUS_COLOR = {"pending": (76, 114, 176, 60), "auto": (0, 158, 115, 70), "edited": (46, 111, 191, 70),
                "added": (142, 68, 173, 70), "rejected": (160, 160, 160, 60)}
STATUS_TEXT = {"pending": "not reviewed", "auto": "detector", "edited": "edited", "added": "added", "rejected": "rejected"}
PAD_S = 30.0          # zoom window: event ± PAD_S
NUDGE_S = 0.1


# ── background loading ───────────────────────────────────────────────

class LoadWorker(QThread):
    done = pyqtSignal(object, str)          # recording, error message ("" = ok)

    def __init__(self, rec: Recording, detect: bool):
        super().__init__()
        self.rec, self.detect = rec, detect

    def run(self) -> None:
        try:
            load_signal(self.rec)
            if self.detect or not self.rec.detected:
                run_detector(self.rec)
            self.done.emit(self.rec, "")
        except Exception as e:              # noqa: BLE001 — surfaced in the UI
            self.rec.error = str(e)
            self.done.emit(self.rec, f"{e}\n{traceback.format_exc(limit=2)}")


# ── advanced panel (raw numbers) ─────────────────────────────────────

class AdvancedPanel(QFrame):
    """Collapsible 'under the hood' editor: every detector number, the
    sampling-rate override, and an Apply button that re-runs the detector."""
    # (key, kind, min, max, step) — labels/descriptions come from PARAM_DOC
    SPEC = {
        ("threshold",): (float, 0.5, 10.0, 0.1), ("persistence_k",): (int, 1, 10, 1), ("persistence_m",): (int, 1, 20, 1),
        ("baseline_window",): (int, 10, 900, 10), ("onset_refine",): (bool, 0, 1, 1),
        ("offset", "frac"): (float, 0.1, 1.5, 0.05), ("offset", "k"): (int, 1, 30, 1), ("offset", "min_s"): (int, 1, 120, 1),
        ("offset", "tier2_frac"): (float, 0.1, 2.0, 0.05), ("offset", "tier2_k"): (int, 1, 60, 1),
        ("offset", "merge_s"): (int, 0, 600, 5), ("offset", "confirm_s"): (int, 0, 60, 1),
        ("offset", "resume_frac"): (float, 0.5, 5.0, 0.1), ("offset", "resume_k"): (int, 1, 10, 1),
        ("offset", "inherit_s"): (int, 0, 1800, 30), ("offset", "backstop_s"): (int, 60, 7200, 60),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame{background:#F4F5F7;border:1px solid #C9CED6;border-radius:4px}")
        g = QGridLayout(self)
        g.setContentsMargins(10, 6, 10, 6)
        g.setHorizontalSpacing(14)
        self.widgets: dict[tuple, QWidget] = {}
        doc = {k: (lab, d) for k, lab, d in PARAM_DOC}
        groups = [("ONSET", [("threshold",), ("persistence_k",), ("persistence_m",), ("baseline_window",), ("onset_refine",)]),
                  ("OFFSET", [("offset", "frac"), ("offset", "k"), ("offset", "min_s"), ("offset", "tier2_frac"), ("offset", "tier2_k")]),
                  ("MERGING & LOOKAHEAD", [("offset", "merge_s"), ("offset", "confirm_s"), ("offset", "resume_frac"),
                                           ("offset", "resume_k"), ("offset", "inherit_s"), ("offset", "backstop_s")])]
        for col, (title, keys) in enumerate(groups):
            head = QLabel(title)
            head.setStyleSheet("border:none;color:#1B3A66;font-weight:bold")
            g.addWidget(head, 0, col * 2, 1, 2)
            for i, key in enumerate(keys, 1):
                kind, lo, hi, step = self.SPEC[key]
                label, desc = doc[key]
                if kind is bool:
                    w = QCheckBox()
                else:
                    w = QDoubleSpinBox() if kind is float else QSpinBox()
                    w.setRange(lo, hi)
                    w.setSingleStep(step)
                    if kind is float:
                        w.setDecimals(2)
                    w.setMaximumWidth(90)
                w.setToolTip(desc)
                lab = QLabel(label)
                lab.setToolTip(desc)
                lab.setStyleSheet("border:none")
                g.addWidget(lab, i, col * 2)
                g.addWidget(w, i, col * 2 + 1)
                self.widgets[key] = w
        row = 1 + max(len(k) for _, k in groups)
        fs_lab = QLabel("Sampling rate override (Hz)")
        fs_lab.setStyleSheet("border:none")
        fs_lab.setToolTip("Leave at 'auto' to use the sampling rate stored in the recording's metadata. Set it only for data "
                          "whose metadata lacks it or is wrong.")
        self.fs_box = QDoubleSpinBox()
        self.fs_box.setRange(0, 100000)
        self.fs_box.setDecimals(1)
        self.fs_box.setSpecialValueText("auto (from file)")
        self.fs_box.setMaximumWidth(140)
        g.addWidget(fs_lab, row, 0)
        g.addWidget(self.fs_box, row, 1)
        self.b_apply = QPushButton("Apply && re-run on this recording")
        self.b_apply.setStyleSheet("QPushButton{background:#1B3A66;color:white;font-weight:bold;padding:4px 12px}")
        g.addWidget(self.b_apply, row, 4, 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        hint = QLabel("Hover a label for what it does. The sensitivity slider multiplies the onset threshold; "
                      "the effective value is shown next to the slider and in the export's Parameters tab.")
        hint.setStyleSheet("color:#666;border:none")
        g.addWidget(hint, row + 1, 0, 1, 6)

    def load(self, profile: dict, fs_override: float) -> None:
        for key, w in self.widgets.items():
            v = profile_value(profile, key)
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif v is not None:
                w.setValue(v)
            w.blockSignals(False)
        self.fs_box.setValue(fs_override or 0)

    def read(self, profile: dict) -> tuple[dict, float]:
        p = copy.deepcopy(profile)
        p.setdefault("offset", {})
        for key, w in self.widgets.items():
            kind = self.SPEC[key][0]
            v = w.isChecked() if isinstance(w, QCheckBox) else (float(w.value()) if kind is float else int(w.value()))
            if len(key) == 1:
                p[key[0]] = v
            else:
                p["offset"][key[1]] = v
        return p, float(self.fs_box.value())


# ── main window ──────────────────────────────────────────────────────

class MarkerWindow(QMainWindow):
    def __init__(self, session: Session, paths: list[Path] | None = None, channel: str | None = None,
                 profile_name: str = DEFAULT_PROFILE):
        super().__init__()
        self.session = session
        self.default_channel = channel
        self.presets = load_presets()
        self.default_profile_name = profile_name if profile_name in self.presets else DEFAULT_PROFILE
        self.default_profile = copy.deepcopy(self.presets[self.default_profile_name])
        self.ri = -1                    # current recording index
        self.mi = -1                    # current mark index
        self.active = "onset"           # cursor the arrow keys move
        self._syncing = False
        self._worker: LoadWorker | None = None
        self._pending_mark = 0
        self._sens_timer = QTimer(self)
        self._sens_timer.setSingleShot(True)
        self._sens_timer.setInterval(350)
        self._sens_timer.timeout.connect(self._apply_sensitivity)
        self.setWindowTitle("Seizure On/Off Marker")
        self.resize(1500, 1040)
        self.setAcceptDrops(True)
        self._build_ui()
        if paths:
            self.open_paths(paths)

    # ── UI construction ──
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(8, 6, 8, 6)

        # row 1: files
        tb = QHBoxLayout()
        self.b_open = QPushButton("Open recording(s)…")
        self.b_open.clicked.connect(self.open_dialog)
        tb.addWidget(self.b_open)
        tb.addWidget(QLabel("Recording:"))
        self.rec_combo = QComboBox()
        self.rec_combo.setMinimumWidth(320)
        self.rec_combo.currentIndexChanged.connect(self._rec_combo_changed)
        tb.addWidget(self.rec_combo, 2)
        tb.addWidget(QLabel("Channel:"))
        self.ch_combo = QComboBox()
        self.ch_combo.addItems([f"CH{i}" for i in range(1, 17)])
        self.ch_combo.currentTextChanged.connect(self._channel_changed)
        tb.addWidget(self.ch_combo)
        tb.addStretch(1)
        self.queue_label = QLabel("Queue: –")
        tb.addWidget(self.queue_label)
        self.b_images = QPushButton("Save image(s)…")
        self.b_images.clicked.connect(self.save_images_dialog)
        tb.addWidget(self.b_images)
        self.b_export = QPushButton("Export to Excel")
        self.b_export.setStyleSheet("QPushButton{background:#1B3A66;color:white;font-weight:bold;padding:4px 12px}")
        self.b_export.clicked.connect(self.export_dialog)
        tb.addWidget(self.b_export)
        v.addLayout(tb)

        # row 2: detector settings
        sb = QHBoxLayout()
        sb.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(150)
        self._fill_presets()
        self.preset_combo.currentTextChanged.connect(self._preset_changed)
        sb.addWidget(self.preset_combo)
        self.b_save_preset = QPushButton("Save preset…")
        self.b_save_preset.setToolTip("Save the current sensitivity and advanced numbers under a name you can pick again later")
        self.b_save_preset.clicked.connect(self.save_preset_dialog)
        sb.addWidget(self.b_save_preset)
        self.b_del_preset = QPushButton("Delete preset")
        self.b_del_preset.clicked.connect(self.delete_preset_clicked)
        sb.addWidget(self.b_del_preset)
        sb.addSpacing(24)
        sb.addWidget(QLabel("Sensitivity:"))
        sb.addWidget(QLabel("less"))
        self.sens = QSlider(Qt.Orientation.Horizontal)
        self.sens.setRange(-2, 2)
        self.sens.setValue(0)
        self.sens.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sens.setTickInterval(1)
        self.sens.setFixedWidth(160)
        self.sens.setToolTip("How readily the detector flags a candidate seizure. Everything is still reviewed by you; "
                             "higher = more candidates (fewer misses, more to reject).")
        self.sens.valueChanged.connect(self._sensitivity_moved)
        sb.addWidget(self.sens)
        sb.addWidget(QLabel("more"))
        self.sens_label = QLabel("Normal")
        self.sens_label.setMinimumWidth(150)
        self.sens_label.setStyleSheet("font-weight:bold")
        sb.addWidget(self.sens_label)
        sb.addStretch(1)
        self.b_adv = QPushButton("Advanced ▾")
        self.b_adv.setCheckable(True)
        self.b_adv.setToolTip("Show the individual detector numbers and the sampling-rate override")
        self.b_adv.toggled.connect(self._toggle_advanced)
        sb.addWidget(self.b_adv)
        v.addLayout(sb)
        self.adv = AdvancedPanel()
        self.adv.b_apply.clicked.connect(self.apply_advanced)
        self.adv.setVisible(False)
        v.addWidget(self.adv)

        # overview
        self.ov_title = QLabel("ZOOMED-OUT: whole recording")
        self.ov_title.setStyleSheet("color:#333")
        v.addWidget(self.ov_title)
        self.ov = pg.PlotWidget()
        self.ov.setMaximumHeight(230)
        self.ov.setMouseEnabled(x=False, y=False)
        self.ov.setMenuEnabled(False)
        self.ov.hideButtons()
        self.ov.setLabel("bottom", "s from recording start")
        self.ov.setLabel("left", "µV")
        self.ov.showGrid(x=True, y=True, alpha=0.2)
        self.ov.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ov_curve = self.ov.plot(pen=pg.mkPen(C_TRACE, width=1))
        self.ov_region = pg.LinearRegionItem(values=(0, 60), movable=True, brush=pg.mkBrush(240, 196, 25, 70),
                                             pen=pg.mkPen("#B8860B", width=1.5))
        self.ov_region.setZValue(10)
        self.ov.addItem(self.ov_region)
        self.ov_region.sigRegionChanged.connect(self._region_changed)
        self.ov.scene().sigMouseClicked.connect(self._ov_clicked)
        self.ov_event_items: list[pg.LinearRegionItem] = []
        v.addWidget(self.ov)

        # zoom
        self.zm_title = QLabel("ZOOMED-IN: drag a cursor to adjust · scroll to zoom · ←/→ nudge 0.1 s (Shift: 1 s) · Tab switches cursor · Enter accept · Delete reject · R reset")
        self.zm_title.setStyleSheet("color:#333")
        v.addWidget(self.zm_title)
        self.zm = pg.PlotWidget()
        self.zm.setMouseEnabled(x=True, y=False)
        self.zm.setMenuEnabled(False)
        self.zm.hideButtons()
        self.zm.setLabel("bottom", "s from recording start")
        self.zm.setLabel("left", "µV")
        self.zm.showGrid(x=True, y=True, alpha=0.2)
        self.zm.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.zm_curve = self.zm.plot(pen=pg.mkPen(C_TRACE, width=1))
        self.zm_curve.setDownsampling(auto=True, method="peak")
        self.zm_curve.setClipToView(True)
        self.span = pg.LinearRegionItem(values=(0, 0), movable=False, brush=pg.mkBrush(0, 158, 115, 22), pen=pg.mkPen(None))
        self.span.setZValue(-5)
        self.zm.addItem(self.span)
        lo = dict(position=0.96, fill=(255, 255, 255, 230), movable=False)
        self.on_line = pg.InfiniteLine(pos=0, angle=90, movable=True, pen=pg.mkPen(C_ON, width=2.5),
                                       hoverPen=pg.mkPen(C_ON, width=4), label="ONSET  {value:.3f} s",
                                       labelOpts=dict(lo, color=C_ON))
        self.off_line = pg.InfiniteLine(pos=0, angle=90, movable=True, pen=pg.mkPen(C_OFF, width=2.5),
                                        hoverPen=pg.mkPen(C_OFF, width=4), label="OFFSET  {value:.3f} s",
                                        labelOpts=dict(lo, color=C_OFF, position=0.90))
        for ln in (self.on_line, self.off_line):
            ln.setZValue(20)
            self.zm.addItem(ln)
        self.on_line.sigPositionChanged.connect(lambda: self._cursor_moved("onset"))
        self.off_line.sigPositionChanged.connect(lambda: self._cursor_moved("offset"))
        self.zm.plotItem.vb.sigXRangeChanged.connect(self._zoom_range_changed)
        v.addWidget(self.zm, 3)

        # buttons
        br = QHBoxLayout()
        self.b_prev = QPushButton("◀ Prev event")
        self.b_prev.clicked.connect(self.prev_event)
        self.b_next = QPushButton("Next event ▶")
        self.b_next.clicked.connect(self.next_event)
        # ONE accept button: its label says what pressing it will save (detector's marks /
        # my adjusted marks / this hand-placed seizure). Enter does the same thing.
        self.b_accept = QPushButton("✓ Accept detector's marks")
        self.b_accept.setStyleSheet("QPushButton{background:#1E8E5A;color:white;font-weight:bold;padding:6px 14px}"
                                    "QPushButton:disabled{background:#A8D5BF}")
        self.b_accept.clicked.connect(self.accept_event)
        self.b_reset = QPushButton("↺ Reset to detector")
        self.b_reset.setToolTip("Put the cursors back where the detector placed them (undo your dragging). "
                                "Nothing is saved until you press Accept.")
        self.b_reset.clicked.connect(self.reset_cursors)
        self.b_reject = QPushButton("✗ Not a seizure")
        self.b_reject.setToolTip("This event is not a seizure: drop it (it stays in the table greyed out as "
                                 "'rejected' so you can see it was reviewed). Shortcut: Delete")
        self.b_reject.clicked.connect(self.reject_event)
        self.b_add = QPushButton("+ Add a missed seizure")
        self.b_add.setToolTip("The detector missed one: scroll the view onto it, press this, drag the new "
                              "cursors onto the seizure, then Accept.")
        self.b_add.clicked.connect(self.add_seizure)
        for b in (self.b_prev, self.b_next):
            br.addWidget(b)
        br.addSpacing(18)
        br.addWidget(self._vsep())
        br.addSpacing(18)
        for b in (self.b_accept, self.b_reset, self.b_reject):
            br.addWidget(b)
        br.addSpacing(18)
        br.addWidget(self._vsep())
        br.addSpacing(18)
        br.addWidget(self.b_add)
        br.addStretch(1)
        # one-line instruction that always says what to do next for the event on screen
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:#1B3A66;font-weight:bold;padding:2px 0")
        v.addWidget(self.hint)
        v.addLayout(br)
        self.status = QLabel("Open a recording folder (or drop one here) to start.")
        self.status.setStyleSheet("color:#555")
        v.addWidget(self.status)

        # table
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["Mouse", "Date", "Channel", "Event", "ONSET (s)", "OFFSET (s)", "Source", "Note"])
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(220)
        self.table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked)
        self.table.cellClicked.connect(self._table_clicked)
        self.table.itemChanged.connect(self._table_edited)
        self.table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        v.addWidget(self.table, 1)
        self._set_enabled(False)
        self._show_settings(self.default_profile_name, self.default_profile, 0.0)

    # ── queue / loading ──
    def open_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a recording folder (or a folder containing many)")
        if d:
            self.open_paths([Path(d)])

    def dragEnterEvent(self, e) -> None:                   # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                        # noqa: N802
        self.open_paths([Path(u.toLocalFile()) for u in e.mimeData().urls()])

    def open_paths(self, paths: list[Path]) -> None:
        new = self.session.add_paths(paths, self.default_channel, self.default_profile_name, self.default_profile)
        self._refresh_rec_combo()
        if not self.session.recordings:
            QMessageBox.information(self, "No recordings", "No Open Ephys recordings (structure.oebin) found under the chosen folder(s).")
            return
        self.session.autosave()
        if self.ri < 0:
            self.goto_recording(0)
        elif new:
            self.status.setText(f"Added {len(new)} recording(s) to the queue.")
        self._refresh_table()

    def _refresh_rec_combo(self) -> None:
        self.rec_combo.blockSignals(True)
        self.rec_combo.clear()
        for i, r in enumerate(self.session.recordings):
            done = r.detected and all(m.status != "pending" for m in r.marks)
            flag = "✓ " if done else ("⚠ " if r.error else "")
            self.rec_combo.addItem(f"{flag}{i + 1}. {r.label()}  —  {r.name}")
        if self.ri >= 0:
            self.rec_combo.setCurrentIndex(self.ri)
        self.rec_combo.blockSignals(False)
        done, total = self.session.progress()
        self.queue_label.setText(f"Queue: {done} of {total} done")

    def _rec_combo_changed(self, i: int) -> None:
        if i >= 0 and i != self.ri:
            self.goto_recording(i)

    def goto_recording(self, i: int, mark_index: int = 0, detect: bool = False) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if 0 <= self.ri < len(self.session.recordings) and self.ri != i:
            self.session.recordings[self.ri].unload()
        self.ri = i
        rec = self.session.recordings[i]
        self._pending_mark = mark_index
        self.rec_combo.blockSignals(True)
        self.rec_combo.setCurrentIndex(i)
        self.rec_combo.blockSignals(False)
        self.status.setText(f"Loading {rec.label()} ({rec.channel}) …")
        self._set_enabled(False)
        QApplication.processEvents()
        self._worker = LoadWorker(rec, detect)
        self._worker.done.connect(self._loaded)
        self._worker.start()

    def load_sync(self, i: int, mark_index: int = 0, detect: bool = False) -> None:
        """Synchronous variant of goto_recording (tests / scripting)."""
        if 0 <= self.ri < len(self.session.recordings) and self.ri != i:
            self.session.recordings[self.ri].unload()
        self.ri = i
        self._pending_mark = mark_index
        rec = self.session.recordings[i]
        try:
            load_signal(rec)
            if detect or not rec.detected:
                run_detector(rec)
            self._loaded(rec, "")
        except Exception as e:            # noqa: BLE001
            rec.error = str(e)
            self._loaded(rec, str(e))

    def _loaded(self, rec: Recording, err: str) -> None:
        self._show_settings(rec.profile_name, rec.profile, rec.fs_override)
        if err:
            self.status.setText(f"⚠ {rec.label()}: {err.splitlines()[0]}")
            self.session.autosave()
            self._refresh_rec_combo()
            self._set_enabled(False)
            self.b_next.setEnabled(True)
            self.b_prev.setEnabled(True)
            return
        self.ch_combo.blockSignals(True)
        self.ch_combo.setCurrentText(rec.channel)
        self.ch_combo.blockSignals(False)
        self._draw_recording(rec)
        self.session.autosave()
        self._refresh_rec_combo()
        self._refresh_table()
        self._set_enabled(True)
        if rec.marks:
            self.show_mark(min(self._pending_mark, len(rec.marks) - 1))
        else:
            self.mi = -1
            self._set_zoom(0, min(120, rec.n_seconds))
            self.status.setText(f"{rec.label()}: no seizure detected. Use “+ Add seizure” if you see one, "
                                f"raise the sensitivity, or press Next to move on.")
            self._update_buttons()

    def _draw_recording(self, rec: Recording) -> None:
        x, y = envelope(rec.filt, rec.fs)
        self.ov_curve.setData(x, y)
        lim = 1.1 * float(np.percentile(np.abs(y), 99.8)) if len(y) else 1.0
        self.ov.setYRange(-lim, lim, padding=0)
        self.ov.setXRange(0, rec.n_seconds, padding=0)
        self.ov_region.setBounds([0, rec.n_seconds])
        t = np.arange(len(rec.filt)) / rec.fs
        self.zm_curve.setData(t, rec.filt)
        # Re-bounding can clamp cursors still sitting at the PREVIOUS recording's event; that
        # clamp must not be written into this recording's mark (bug seen 2026-11-10: a 755-765 s
        # event became 964.9-965.0 s after switching from a longer recording).
        self._syncing = True
        self.on_line.setBounds([0, rec.n_seconds])
        self.off_line.setBounds([0, rec.n_seconds])
        self._syncing = False
        self._draw_event_markers()
        fs_note = " (override)" if rec.fs_override else ""
        self.ov_title.setText(f"ZOOMED-OUT: {rec.label()}  —  {rec.name}  ({rec.channel}, {rec.fs:g} Hz{fs_note}, "
                              f"{rec.n_seconds / 60:.1f} min)  —  click to move the zoom window")

    def _draw_event_markers(self) -> None:
        for it in self.ov_event_items:
            self.ov.removeItem(it)
        self.ov_event_items = []
        rec = self.current
        if rec is None:
            return
        for mi, m in enumerate(rec.marks):
            col = STATUS_COLOR[m.status]
            it = pg.LinearRegionItem(values=(m.onset, max(m.offset, m.onset + 0.5)), movable=False,
                                     brush=pg.mkBrush(*col), pen=pg.mkPen(QColor(*col[:3]), width=1.5 if mi == self.mi else 0.8))
            it.setZValue(5)
            self.ov.addItem(it)
            self.ov_event_items.append(it)

    # ── current mark ──
    @property
    def current(self) -> Recording | None:
        return self.session.recordings[self.ri] if 0 <= self.ri < len(self.session.recordings) else None

    @property
    def mark(self) -> Mark | None:
        rec = self.current
        return rec.marks[self.mi] if rec and 0 <= self.mi < len(rec.marks) else None

    def show_mark(self, mi: int) -> None:
        rec = self.current
        if rec is None or not rec.marks:
            return
        self.mi = max(0, min(mi, len(rec.marks) - 1))
        m = rec.marks[self.mi]
        self._set_cursors(m.onset, m.offset)
        self._set_zoom(m.onset - PAD_S, m.offset + PAD_S)
        self._draw_event_markers()
        self._update_buttons()
        self._update_status()

    def _set_cursors(self, on: float, off: float) -> None:
        self._syncing = True
        self.on_line.setValue(on)
        self.off_line.setValue(off)
        self._syncing = False
        self._after_cursor_change()

    def _set_zoom(self, a: float, b: float) -> None:
        rec = self.current
        a, b = max(0.0, a), min(float(rec.n_seconds), b) if rec else b
        self._syncing = True
        self.zm.setXRange(a, b, padding=0)
        self.ov_region.setRegion((a, b))
        self._syncing = False
        self._autoscale_y(a, b)

    def _autoscale_y(self, a: float, b: float) -> None:
        rec = self.current
        if rec is None or rec.filt is None:              # range changes also fire before/while loading
            return
        i0, i1 = max(0, int(a * rec.fs)), min(len(rec.filt), int(b * rec.fs))
        if i1 - i0 < 10:
            return
        seg = rec.filt[i0:i1]
        lim = 1.1 * float(np.percentile(np.abs(seg), 99.8)) or 1.0
        self.zm.setYRange(-lim, lim, padding=0)

    def _after_cursor_change(self) -> None:
        on, off = self.on_line.value(), self.off_line.value()
        self.span.setRegion((on, off))
        self.on_line.setBounds([0, off - NUDGE_S])
        rec = self.current
        self.off_line.setBounds([on + NUDGE_S, rec.n_seconds if rec else off + 1e9])

    def _cursor_moved(self, which: str) -> None:
        if self._syncing:
            return
        self.active = which
        self._after_cursor_change()
        m = self.mark
        if m is not None:
            m.onset, m.offset = float(self.on_line.value()), float(self.off_line.value())
        self._update_buttons()
        self._update_status()

    # ── sync overview <-> zoom ──
    def _region_changed(self) -> None:
        if self._syncing:
            return
        a, b = self.ov_region.getRegion()
        self._syncing = True
        self.zm.setXRange(a, b, padding=0)
        self._syncing = False
        self._autoscale_y(a, b)

    def _zoom_range_changed(self) -> None:
        if self._syncing:
            return
        a, b = self.zm.plotItem.vb.viewRange()[0]
        self._syncing = True
        self.ov_region.setRegion((a, b))
        self._syncing = False
        self._autoscale_y(a, b)

    def _ov_clicked(self, ev) -> None:
        if self.current is None:
            return
        x = self.ov.plotItem.vb.mapSceneToView(ev.scenePos()).x()
        a, b = self.ov_region.getRegion()
        if a <= x <= b:
            return                                     # click inside the box = start of a drag
        w = b - a
        self._set_zoom(x - w / 2, x + w / 2)

    @staticmethod
    def _vsep() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet("color:#BBBBBB")
        return f

    # ── decisions ──
    def accept_event(self) -> None:
        """Save the cursors as they are on screen. Source becomes 'detector' when they
        are still where the detector put them, 'edited' when dragged, 'added' when hand-placed."""
        m = self.mark
        if m is None:
            return
        m.onset, m.offset = float(self.on_line.value()), float(self.off_line.value())
        m.status = "added" if m.auto_onset is None else ("edited" if m.moved else "auto")
        self._after_commit()

    def reset_cursors(self) -> None:
        """Undo dragging: cursors back to the detector's placement (not saved yet)."""
        m = self.mark
        if m is None or m.auto_onset is None:
            return
        m.onset, m.offset = m.auto_onset, m.auto_offset
        self._set_cursors(m.onset, m.offset)
        self._update_buttons()
        self._update_status()

    def reject_event(self) -> None:
        m = self.mark
        if m is None:
            return
        m.status = "rejected"
        self._after_commit()

    def add_seizure(self) -> None:
        rec = self.current
        if rec is None:
            return
        a, b = self.zm.plotItem.vb.viewRange()[0]
        c = (a + b) / 2
        half = min(5.0, (b - a) / 6)
        m = Mark(onset=max(0.0, c - half), offset=min(float(rec.n_seconds), c + half), status="pending")
        rec.marks.append(m)
        rec.marks.sort(key=lambda k: k.onset)
        self.mi = rec.marks.index(m)
        self._set_cursors(m.onset, m.offset)
        self._draw_event_markers()
        self._update_buttons()
        self.status.setText("New event placed at the centre of the view — drag the cursors onto the seizure, then press “✓ Save this seizure”.")
        self.session.autosave()
        self._refresh_rec_combo()

    def _after_commit(self) -> None:
        self.session.autosave()
        self._refresh_table()
        self._refresh_rec_combo()
        self._draw_event_markers()
        self._update_buttons()
        self._update_status()
        self._advance_to_pending()

    def _advance_to_pending(self) -> None:
        rec = self.current
        for mi in range(self.mi + 1, len(rec.marks)):
            if rec.marks[mi].status == "pending":
                self.show_mark(mi)
                return
        for mi in range(0, self.mi):
            if rec.marks[mi].status == "pending":
                self.show_mark(mi)
                return
        nxt = next((i for i in range(self.ri + 1, len(self.session.recordings))
                    if not self.session.recordings[i].error), None)
        if nxt is None:
            done, total = self.session.progress()
            self.status.setText("All events in this recording decided. "
                                + ("Queue complete — Export to Excel when ready." if done == total
                                   else "Pick another recording from the queue."))
        else:
            self.status.setText("All events in this recording decided — press Next ▶ for the next recording.")

    # ── navigation ──
    def next_event(self) -> None:
        self._step(+1)

    def prev_event(self) -> None:
        self._step(-1)

    def _step(self, d: int) -> None:
        rec = self.current
        if rec is None:
            return
        if rec.marks and 0 <= self.mi + d < len(rec.marks):
            self.show_mark(self.mi + d)
            return
        j = self.ri + d
        if 0 <= j < len(self.session.recordings):
            r = self.session.recordings[j]
            self.goto_recording(j, 0 if d > 0 else max(0, len(r.marks) - 1))
            return
        self.status.setText("Start of queue." if d < 0 else "End of queue.")

    # ── channel / presets / sensitivity / advanced ──
    def _confirm_rerun(self, what: str) -> bool:
        rec = self.current
        if rec is None or not any(m.status != "pending" for m in rec.marks):
            return True
        return QMessageBox.question(self, "Re-run detector", f"{what} re-runs the detector on this recording and discards "
                                    "the decisions already made on it. Continue?") == QMessageBox.StandardButton.Yes

    def _rerun(self) -> None:
        rec = self.current
        if rec is None:
            return
        rec.marks, rec.detected = [], False
        rec.unload()
        self.goto_recording(self.ri, 0, detect=True)

    def _channel_changed(self, ch: str) -> None:
        rec = self.current
        if rec is None or ch == rec.channel:
            return
        if not self._confirm_rerun(f"Changing the channel to {ch}"):
            self.ch_combo.blockSignals(True)
            self.ch_combo.setCurrentText(rec.channel)
            self.ch_combo.blockSignals(False)
            return
        rec.channel = ch
        self._rerun()

    def _fill_presets(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.presets = load_presets()
        for name in self.presets:
            tag = "" if name in oo.PROFILES else " (saved)"
            self.preset_combo.addItem(f"{name}{tag}", name)
        self.preset_combo.blockSignals(False)

    def _show_settings(self, name: str, profile: dict, fs_override: float) -> None:
        """Reflect a recording's settings in the preset combo, slider and advanced panel without triggering re-runs."""
        self.preset_combo.blockSignals(True)
        idx = self.preset_combo.findData(name)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.preset_combo.blockSignals(False)
        self.b_del_preset.setEnabled(name not in oo.PROFILES)
        self.sens.blockSignals(True)
        self.sens.setValue(int(profile.get("sensitivity", 0)))
        self.sens.blockSignals(False)
        self._sens_text(profile)
        self.adv.load(profile, fs_override)

    def _sens_text(self, profile: dict) -> None:
        lvl = int(profile.get("sensitivity", 0))
        self.sens_label.setText(f"{SENSITIVITY[lvl][0]}  (threshold {effective_threshold(profile):g}×)")

    def _preset_changed(self, _text: str) -> None:
        name = self.preset_combo.currentData()
        if name is None:
            return
        prof = copy.deepcopy(self.presets[name])
        self.default_profile_name, self.default_profile = name, copy.deepcopy(prof)
        for r in self.session.recordings:                # not-yet-detected recordings follow the new default
            if not r.detected:
                r.profile_name, r.profile = name, copy.deepcopy(prof)
        rec = self.current
        if rec is None:
            self._show_settings(name, prof, 0.0)
            return
        if not self._confirm_rerun(f"Switching to preset “{name}”"):
            self._show_settings(rec.profile_name, rec.profile, rec.fs_override)
            return
        rec.profile_name, rec.profile = name, prof
        self._show_settings(name, prof, rec.fs_override)
        self._rerun()

    def save_preset_dialog(self) -> None:
        rec = self.current
        prof = rec.profile if rec else self.default_profile
        name, ok = QInputDialog.getText(self, "Save preset", "Name for these settings (sensitivity + advanced numbers):")
        name = name.strip()
        if not ok or not name:
            return
        if name in oo.PROFILES:
            QMessageBox.warning(self, "Reserved name", f"“{name}” is a built-in preset; choose another name.")
            return
        self.save_preset_as(name)

    def save_preset_as(self, name: str) -> None:
        rec = self.current
        prof = rec.profile if rec else self.default_profile
        save_preset(name, prof)
        self._fill_presets()
        if rec:
            rec.profile_name = name
        self.default_profile_name, self.default_profile = name, copy.deepcopy(prof)
        self._show_settings(name, prof, rec.fs_override if rec else 0.0)
        self.session.autosave()
        self.status.setText(f"Preset “{name}” saved to {USER_PRESETS_PATH}")

    def delete_preset_clicked(self) -> None:
        name = self.preset_combo.currentData()
        if name is None or name in oo.PROFILES:
            return
        if QMessageBox.question(self, "Delete preset", f"Delete the saved preset “{name}”?") != QMessageBox.StandardButton.Yes:
            return
        delete_preset(name)
        self._fill_presets()
        rec = self.current
        self._show_settings(rec.profile_name if rec else DEFAULT_PROFILE, rec.profile if rec else self.default_profile,
                            rec.fs_override if rec else 0.0)

    def _sensitivity_moved(self, v: int) -> None:
        prof = self.current.profile if self.current else self.default_profile
        self._sens_text(dict(prof, sensitivity=v))
        self._sens_timer.start()                           # debounce: re-run only once the slider settles

    def _apply_sensitivity(self) -> None:
        v = self.sens.value()
        self.default_profile["sensitivity"] = v
        for r in self.session.recordings:
            if not r.detected:
                r.profile["sensitivity"] = v
        rec = self.current
        if rec is None or int(rec.profile.get("sensitivity", 0)) == v:
            return
        if not self._confirm_rerun("Changing the sensitivity"):
            self.sens.blockSignals(True)
            self.sens.setValue(int(rec.profile.get("sensitivity", 0)))
            self.sens.blockSignals(False)
            self._sens_text(rec.profile)
            return
        rec.profile["sensitivity"] = v
        self.adv.load(rec.profile, rec.fs_override)
        self._rerun()

    def _toggle_advanced(self, on: bool) -> None:
        self.adv.setVisible(on)
        self.b_adv.setText("Advanced ▴" if on else "Advanced ▾")

    def apply_advanced(self) -> None:
        rec = self.current
        base = rec.profile if rec else self.default_profile
        prof, fs_override = self.adv.read(base)
        prof["sensitivity"] = self.sens.value()
        self.default_profile = copy.deepcopy(prof)
        for r in self.session.recordings:
            if not r.detected:
                r.profile = copy.deepcopy(prof)
                r.fs_override = fs_override
        if rec is None:
            self._sens_text(prof)
            return
        if not self._confirm_rerun("Applying these numbers"):
            return
        rec.profile, rec.fs_override = prof, fs_override
        self._sens_text(prof)
        self._rerun()

    # ── images / export ──
    def save_images_dialog(self) -> None:
        rec = self.current
        if rec is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Save image(s)")
        box.setText("Save PNG images of the trace with the cursors as currently placed.")
        b_cur = box.addButton("This event", QMessageBox.ButtonRole.AcceptRole)
        b_rec = box.addButton("This recording (overview + committed events)", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() not in (b_cur, b_rec):
            return
        d = QFileDialog.getExistingDirectory(self, "Folder for the images", str(self.session.out_dir / "images"))
        if not d:
            return
        marks = [self.mi] if box.clickedButton() is b_cur else None
        files = ex.save_images(rec, Path(d), marks, overview=box.clickedButton() is b_rec)
        self.status.setText(f"Saved {len(files)} image(s) to {d}")

    def export_dialog(self) -> None:
        if not self.session.committed_rows():
            QMessageBox.information(self, "Nothing to export", "No events have been committed yet.")
            return
        default = self.session.out_dir / "seizure_timestamps.xlsx"
        f, _ = QFileDialog.getSaveFileName(self, "Export workbook", str(default), "Excel workbook (*.xlsx)")
        if not f:
            return
        self.export_to(Path(f))

    def export_to(self, path: Path, include_rejected: bool = True) -> int:
        prog = QProgressDialog("Exporting…", None, 0, 0, self)
        prog.setWindowTitle("Export to Excel")
        prog.setMinimumDuration(0)
        prog.show()
        try:
            n = ex.export_workbook(self.session, path, include_rejected,
                                   progress=lambda s: (prog.setLabelText(s), QApplication.processEvents()))
        finally:
            prog.close()
        self.status.setText(f"Exported {n} rows (+ Parameters tab) to {path}")
        return n

    # ── table ──
    def _refresh_table(self) -> None:
        self.table.blockSignals(True)
        rows = self.session.committed_rows()
        self.table.setRowCount(len(rows))
        for r, (rec, mi, m) in enumerate(rows):
            vals = [rec.mouse, rec.date_pretty(), rec.channel, str(mi + 1),
                    "—" if m.status == "rejected" else f"{m.onset:.3f}",
                    "—" if m.status == "rejected" else f"{m.offset:.3f}", STATUS_TEXT[m.status], m.note]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.ItemDataRole.UserRole, (self.session.index_of(rec), mi))
                if c != 7:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if m.status == "rejected":
                    it.setForeground(QColor("#999999"))
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)

    def _table_clicked(self, row: int, col: int) -> None:
        it = self.table.item(row, 0)
        if it is None:
            return
        ri, mi = it.data(Qt.ItemDataRole.UserRole)
        if ri == self.ri:
            self.show_mark(mi)
        else:
            self.goto_recording(ri, mi)

    def _table_edited(self, it: QTableWidgetItem) -> None:
        if it.column() != 7:
            return
        ri, mi = it.data(Qt.ItemDataRole.UserRole)
        self.session.recordings[ri].marks[mi].note = it.text()
        self.session.autosave()

    # ── state / status ──
    def _set_enabled(self, on: bool) -> None:
        for b in (self.b_prev, self.b_next, self.b_accept, self.b_reset, self.b_reject, self.b_add,
                  self.b_images, self.ch_combo):
            b.setEnabled(on)
        if not on:
            self.hint.setText("Open a recording folder to start.")

    def _update_buttons(self) -> None:
        m = self.mark
        has = m is not None
        self.b_accept.setEnabled(has)
        self.b_reject.setEnabled(has)
        self.b_reset.setEnabled(has and m.auto_onset is not None and m.moved)
        if not has:
            self.hint.setText("No events in this recording. Use “+ Add a missed seizure” if you see one.")
            return
        hand = m.auto_onset is None
        if hand:
            self.b_accept.setText("✓ Save this seizure  (Enter)")
            self.b_accept.setToolTip("Save the hand-placed cursors as a seizure. Source = 'added'.")
            self.hint.setText("Hand-placed event: drag the ONSET and OFFSET cursors onto the seizure, then press ✓ Save this seizure.")
        elif m.moved:
            self.b_accept.setText("✓ Save my adjusted marks  (Enter)")
            self.b_accept.setToolTip("Save the cursors where you dragged them. Source = 'edited'.")
            self.hint.setText("You moved the cursors. ✓ Save keeps your placement; ↺ Reset puts them back where the detector had them.")
        else:
            self.b_accept.setText("✓ Accept detector's marks  (Enter)")
            self.b_accept.setToolTip("The cursors are where the detector put them. Save them as-is. Source = 'detector'.")
            self.hint.setText("Check the ONSET and OFFSET cursors. Right? press ✓ Accept. Wrong? drag them, then Save. "
                              "Not a seizure at all? press ✗.")
        if m.status != "pending":
            self.hint.setText(f"Already decided ({STATUS_TEXT[m.status]}). "
                              "You can still change it: drag + Save, or ✗ Not a seizure.")

    def _update_status(self) -> None:
        rec, m = self.current, self.mark
        if rec is None or m is None:
            return
        moved = ("cursors moved" if m.moved and m.auto_onset is not None
                 else ("hand-placed" if m.auto_onset is None else "cursors unchanged from detector"))
        extra = (f" · detector: {m.onset:.0f}–{m.offset:.0f} s, {m.runs} run(s), offset tier {m.tier}"
                 if m.auto_onset is not None else "")
        self.status.setText(f"Recording {self.ri + 1} of {len(self.session.recordings)} · event {self.mi + 1} of "
                            f"{len(rec.marks)} · {STATUS_TEXT[m.status]} · {moved}{extra} · active cursor: {self.active}")

    # ── keyboard ──
    def keyPressEvent(self, e) -> None:                    # noqa: N802
        k = e.key()
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        step = 1.0 if shift else NUDGE_S
        if k in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.mark is not None:
            ln = self.on_line if self.active == "onset" else self.off_line
            ln.setValue(ln.value() + (step if k == Qt.Key.Key_Right else -step))
        elif k == Qt.Key.Key_Tab:
            self.active = "offset" if self.active == "onset" else "onset"
            self._update_status()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.mark is not None:
            self.accept_event()
        elif k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.reject_event()
        elif k == Qt.Key.Key_R:
            self.reset_cursors()
        elif k == Qt.Key.Key_N:
            self.next_event()
        elif k == Qt.Key.Key_P:
            self.prev_event()
        else:
            super().keyPressEvent(e)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Interactive seizure onset/offset marker")
    ap.add_argument("paths", nargs="*", help="recording folders (or folders containing them)")
    ap.add_argument("--channel", help="override the ECoG channel for every recording (default: from the lab log, else CH2)")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="preset to start with (built-in or saved)")
    ap.add_argument("--out", help="output folder for autosave / exports (default: offset_detector/output/marker)")
    args = ap.parse_args(argv)
    app = QApplication.instance() or QApplication([])
    session = Session(Path(args.out) if args.out else None)
    win = MarkerWindow(session, [Path(p) for p in args.paths], args.channel, args.profile)
    win.show()
    return app.exec()
