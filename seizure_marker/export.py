"""
Excel export and PNG saving for the marker GUI: one sheet per mouse (Dataset,
Mouse, Channel, Date, onset/offset sample numbers and seconds, trace image,
Source, Note) plus a Parameters sheet.
"""

from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Callable

import matplotlib
matplotlib.use("Agg")
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .plots import DPI, IMG_H_IN, PAD_S, print_setup, trace_png
from .session import (COMMITTED, FILTER_TEXT, VERSION, Mark, Recording, Session, effective_threshold, load_signal,
                      methods_text, parameter_rows)

HEADERS = ["Dataset", "Mouse", "Channel", "Date", "ONSET Sample Number", "OFFSET Sample Number",
           "ONSET Time (s)", "OFFSET Time (s)", "Trace Image", "Source", "Note"]
WIDTHS = [18, 9, 9, 12, 21, 22, 16, 16, 166, 10, 40]
SOURCE_LABEL = {"auto": "detector", "edited": "edited", "added": "added by hand", "rejected": "rejected"}


def _date_cell(d: str):
    return date(int(d[:4]), int(d[4:6]), int(d[6:])) if len(d) == 8 and d.isdigit() else d


def event_title(rec: Recording, mi: int, m: Mark) -> str:
    n = len(rec.marks)
    flags = {"auto": "detector placement accepted", "edited": "cursors adjusted by reviewer",
             "added": "added by reviewer", "rejected": "REJECTED (not a seizure)", "pending": "not yet reviewed"}[m.status]
    sess = f"session {rec.session}, " if rec.session else ""
    return (f"Mouse {rec.mouse}  |  {rec.dataset}  |  {sess}{rec.date_pretty()}  |  Event {mi + 1} of {n}\n"
            f"onset {m.onset:.3f} s  →  offset {m.offset:.3f} s  (duration {m.offset - m.onset:.1f} s)   ·   {flags}"
            f"{'   ·   ' + m.note if m.note else ''}")


def event_png(rec: Recording, mi: int, m: Mark, pad_s: float = PAD_S) -> bytes:
    win = (m.onset - pad_s, m.offset + pad_s)
    return trace_png(rec.filt, rec.fs, None, win, event_title(rec, mi, m), (m.onset, m.offset), None)


def overview_png(rec: Recording) -> bytes:
    evs = [(m.onset, m.offset) for m in rec.marks if m.status in COMMITTED]
    title = (f"Mouse {rec.mouse}  |  {rec.dataset}  |  {rec.date_pretty()}\n"
             f"whole recording, {len(evs)} committed event(s)")
    return trace_png(rec.filt, rec.fs, None, (0, rec.n_seconds), title, None, None, events=evs)


def safe_stem(rec: Recording) -> str:
    return re.sub(r"[^\w.-]+", "_", f"{rec.dataset}_{rec.mouse}_{rec.name}")


def save_images(rec: Recording, out_dir: Path, marks: list[int] | None = None,
                overview: bool = True) -> list[Path]:
    """PNG of the overview and of each listed mark (default: every committed one)."""
    if not rec.loaded:
        load_signal(rec)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(rec)
    written = []
    if overview:
        p = out_dir / f"{stem}_overview.png"
        p.write_bytes(overview_png(rec))
        written.append(p)
    idx = marks if marks is not None else [i for i, m in enumerate(rec.marks) if m.status in COMMITTED]
    for mi in idx:
        p = out_dir / f"{stem}_event{mi + 1}.png"
        p.write_bytes(event_png(rec, mi, rec.marks[mi]))
        written.append(p)
    return written


def export_workbook(session: Session, path: Path, include_rejected: bool = True,
                    progress: Callable[[str], None] | None = None) -> int:
    """One sheet per mouse. Recordings are (re)loaded one at a time for the
    images. Returns the number of rows written."""
    rows: list[dict] = []
    for rec in session.recordings:
        marks = [(i, m) for i, m in enumerate(rec.marks)
                 if m.status in COMMITTED or (include_rejected and m.status == "rejected")]
        if not marks:
            continue
        if progress:
            progress(f"rendering {rec.label()} …")
        if not rec.loaded:
            try:
                load_signal(rec)
            except Exception as e:                       # keep the numbers even if the signal is gone
                for i, m in marks:
                    rows.append(dict(rec=rec, mi=i, m=m, png=None, err=str(e)))
                continue
        for i, m in marks:
            rows.append(dict(rec=rec, mi=i, m=m, png=event_png(rec, i, m), err=""))
        if session.recordings and rec is not _current(session):
            rec.unload()

    wb = Workbook()
    wb.remove(wb.active)
    mice = sorted({r["rec"].mouse or "unknown" for r in rows}, key=lambda s: (not s.isdigit(), int(s) if s.isdigit() else s))
    for mouse in mice:
        _write_sheet(wb, mouse, [r for r in rows if (r["rec"].mouse or "unknown") == mouse])
    write_parameters_sheet(wb, [r["rec"] for r in rows])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return len(rows)


def write_parameters_sheet(wb: Workbook, recs: list[Recording]) -> None:
    """'Parameters' tab: which settings each exported recording used, every value
    with a plain-language definition, and a ready-to-edit methods paragraph."""
    ws = wb.create_sheet(title="Parameters")
    navy = PatternFill("solid", fgColor="1B3A66")
    hdr_font = Font(bold=True, color="FFFFFF", size=12)
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    # distinct parameter sets, in order of first use
    import json
    uniq = list({id(r): r for r in recs}.values())
    sets: list[dict] = []
    set_of: dict[int, int] = {}
    seen = {}
    for rec in uniq:
        key = (rec.profile_name, json.dumps(rec.profile, sort_keys=True, default=str))
        if key not in seen:
            seen[key] = len(sets)
            sets.append(dict(name=rec.profile_name, profile=rec.profile, label=f"Set {chr(65 + len(sets))}", recs=[]))
        sets[seen[key]]["recs"].append(rec)
        set_of[id(rec)] = seen[key]

    r = 1
    ws.cell(row=r, column=1, value="Seizure On/Off Marker — settings used for this export").font = Font(bold=True, size=14)
    r += 1
    ws.cell(row=r, column=1, value=f"Software version {VERSION}.  Front-end filter: {FILTER_TEXT}. "
            f"Times are seconds from the first sample of each recording; sample numbers = time × sampling rate.")
    r += 2
    heads = ["Mouse", "Date", "Recording", "Channel", "Sampling rate (Hz)", "Preset", "Sensitivity", "Effective onset threshold", "Parameter set"]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font, cell.fill = hdr_font, navy
    r += 1
    from .session import SENSITIVITY
    for rec in uniq:
        vals = [rec.mouse, _date_cell(rec.date), rec.name, rec.channel, rec.fs or None, rec.profile_name,
                SENSITIVITY[int(rec.profile.get("sensitivity", 0))][0], effective_threshold(rec.profile),
                sets[set_of[id(rec)]]["label"]]
        for c, v in enumerate(vals, 1):
            ws.cell(row=r, column=c, value=v)
        ws.cell(row=r, column=2).number_format = "m/d/yyyy"
        r += 1
    r += 1

    # parameter values per set, with definitions
    heads = ["Parameter", "Key"] + [f"{st['label']} ({st['name']})" for st in sets] + ["What it means"]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font, cell.fill = hdr_font, navy
    r += 1
    tables = [parameter_rows(st["profile"]) for st in sets]
    for i, (key, label, _, desc) in enumerate(tables[0]):
        ws.cell(row=r, column=1, value=label).font = bold
        ws.cell(row=r, column=2, value=key)
        for j, t in enumerate(tables):
            ws.cell(row=r, column=3 + j, value=t[i][2])
        ws.cell(row=r, column=3 + len(sets), value=desc).alignment = wrap
        r += 1
    r += 1

    # methods paragraph(s)
    for st in sets:
        ws.cell(row=r, column=1, value=f"Methods text for {st['label']} ({st['name']}; "
                f"{len(st['recs'])} recording(s))").font = bold
        r += 1
        fs_vals = {x.fs for x in st["recs"] if x.fs}
        cell = ws.cell(row=r, column=1, value=methods_text(st["profile"], fs_vals.pop() if len(fs_vals) == 1 else None))
        cell.alignment = wrap
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3 + len(sets))
        ws.row_dimensions[r].height = 190
        r += 2
    for col, w in zip("ABCDEFGHIJ", [34, 24, 40, 14, 18, 14, 14, 22, 16, 60]):
        ws.column_dimensions[col].width = w
    ws.column_dimensions[chr(65 + 2 + len(sets))].width = 90


def _current(session: Session) -> Recording | None:
    for r in session.recordings:
        if r.loaded:
            return r
    return None


def _write_sheet(wb: Workbook, mouse: str, rows: list[dict]) -> None:
    ws = wb.create_sheet(title=str(mouse)[:31])
    navy = PatternFill("solid", fgColor="1B3A66")
    thin = Side(style="thin", color="D9D9D9")
    for c, (h, w) in enumerate(zip(HEADERS, WIDTHS), 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF", size=13)
        cell.fill = navy
        cell.alignment = Alignment(horizontal="center" if h == "Trace Image" else "left", vertical="center")
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    img_h_px = int(IMG_H_IN * DPI)
    for r, row in enumerate(rows, 2):
        rec, m = row["rec"], row["m"]
        rejected = m.status == "rejected"
        vals = [rec.dataset, rec.mouse, rec.channel, _date_cell(rec.date),
                None if rejected else int(round(m.onset * rec.fs)) if rec.fs else None,
                None if rejected else int(round(m.offset * rec.fs)) if rec.fs else None,
                None if rejected else round(m.onset, 3), None if rejected else round(m.offset, 3),
                None, SOURCE_LABEL.get(m.status, m.status), m.note]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.alignment = Alignment(vertical="bottom")
            cell.border = Border(bottom=thin)
            if rejected:
                cell.font = Font(color="999999")
        ws.cell(row=r, column=4).number_format = "m/d/yyyy"
        ws.cell(row=r, column=7).number_format = "0.000"
        ws.cell(row=r, column=8).number_format = "0.000"
        if row["png"]:
            ws.row_dimensions[r].height = img_h_px * 0.75 + 6
            ws.add_image(XLImage(io.BytesIO(row["png"])), f"I{r}")
        elif row["err"]:
            ws.cell(row=r, column=9, value=f"no image: {row['err']}").font = Font(italic=True, color="C00000")
    ws.auto_filter.ref = f"A1:K{len(rows) + 1}"
    print_setup(ws, len(rows) + 1)
    ws.print_area = f"A1:K{len(rows) + 1}"
