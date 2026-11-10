"""
Trace images for the Excel export / Save image(s), and the worksheet print
layout. Colours are Okabe-Ito (colour-blind safe).
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from . import detector as oo

PAD_S = 30                     # context either side of the event in the trace image
IMG_W_IN, IMG_H_IN, DPI = 11.5, 2.7, 100

# distinct, colour-blind-safe (Okabe-Ito) — trace stays neutral so lines read clearly
C_TRACE = "#4C72B0"
C_SHADE = "#009E73"             # detected seizure span (very light fill)
C_DET_ON = "#009E73"            # bluish green
C_DET_OFF = "#D55E00"           # vermillion
C_LOG_ON = "#CC79A7"            # reddish purple
C_LOG_OFF = "#000000"           # black (dotted) — never confused with the vermillion detected offset
LINE_ALPHA = 0.45               # "very faint"
SHADE_ALPHA = 0.10



def recording_start(rec_dir: Path) -> datetime | None:
    """Local wall-clock time of the first sample of the amplifier stream."""
    oebin = oo._find_oebin(rec_dir)
    sync = oebin.parent / "sync_messages.txt"
    if not sync.exists():
        return None
    meta = json.loads(oebin.read_text())
    stream = max(meta["continuous"], key=lambda s: s["num_channels"])
    fs = float(stream["sample_rate"])
    key = stream["folder_name"].split(".")[-1].rstrip("/")      # "Rhythm Data" / "acquisition_board"
    txt = sync.read_text()
    sw = re.search(r"Software Time[^:]*:\s*(\d+)", txt)
    st = re.search(rf"{re.escape(key)} @ [\d.]+ Hz:\s*(\d+)", txt)
    if not (sw and st):
        return None
    first = int(np.load(oebin.parent / "continuous" / stream["folder_name"] / "sample_numbers.npy",
                        mmap_mode="r")[0])
    t0 = datetime.fromtimestamp(int(sw.group(1)) / 1000)          # local time zone
    return t0 + timedelta(seconds=(first - int(st.group(1))) / fs)


def trace_png(filt: np.ndarray, fs: float, t_start: datetime | None, win: tuple[int, int],
              title: str, det: tuple[int, int] | None, log: tuple[float, float] | None,
              ylim: tuple[float, float] | None = None,
              events: list[tuple[int, int]] | None = None) -> bytes:
    a, b = max(0, int(win[0] * fs)), min(len(filt), int(win[1] * fs))
    secs = np.arange(a, b) / fs
    y = filt[a:b]
    step = max(1, len(y) // 20000)                                 # keep images light
    secs, y = secs[::step], y[::step]
    to_x = (lambda s: mdates.date2num(t_start + timedelta(seconds=float(s)))) if t_start else (lambda s: s)
    x = np.array([to_x(s) for s in secs]) if t_start else secs

    fig, ax = plt.subplots(figsize=(IMG_W_IN, IMG_H_IN), dpi=DPI)
    ax.plot(x, y, color=C_TRACE, lw=0.6)
    lim = 1.1 * np.percentile(np.abs(y), 99.8) if len(y) else 1
    ax.set_ylim(*(ylim or (-lim, lim)))
    handles = []
    inside = lambda s: win[0] <= s <= win[1]        # zoom windows can cut through an event
    if det:
        if det[0] < win[1] and det[1] > win[0]:
            ax.axvspan(to_x(max(det[0], win[0])), to_x(min(det[1], win[1])),
                       color=C_SHADE, alpha=SHADE_ALPHA, lw=0)
        if inside(det[0]):
            handles.append(ax.axvline(to_x(det[0]), color=C_DET_ON, ls="--", lw=1.1, alpha=LINE_ALPHA,
                                      label="detected onset"))
        if inside(det[1]):
            handles.append(ax.axvline(to_x(det[1]), color=C_DET_OFF, ls="--", lw=1.1, alpha=LINE_ALPHA,
                                      label="detected offset"))
    for a_, b_ in (events or []):                    # overview: every event, lines only
        if a_ < win[1] and b_ > win[0]:
            ax.axvspan(to_x(max(a_, win[0])), to_x(min(b_, win[1])), color=C_SHADE, alpha=SHADE_ALPHA, lw=0)
        if inside(a_):
            ax.axvline(to_x(a_), color=C_DET_ON, ls="--", lw=1.1, alpha=LINE_ALPHA)
        if inside(b_):
            ax.axvline(to_x(b_), color=C_DET_OFF, ls="--", lw=1.1, alpha=LINE_ALPHA)
    if log:
        if inside(log[0]):
            handles.append(ax.axvline(to_x(log[0]), color=C_LOG_ON, ls=":", lw=1.4, alpha=LINE_ALPHA,
                                      label="logged onset"))
        if inside(log[1]):
            handles.append(ax.axvline(to_x(log[1]), color=C_LOG_OFF, ls=":", lw=1.4, alpha=LINE_ALPHA,
                                      label="logged offset"))
    ax.set_xlim(to_x(max(win[0], 0)), to_x(min(win[1], len(filt) / fs)))
    if handles:
        leg = ax.legend(handles=handles, loc="upper right", fontsize=7, framealpha=0.85, ncol=len(handles))
        for h in leg.get_lines():
            h.set_alpha(1.0)                                       # legend swatches readable
    ax.set_title(title, fontsize=11 if "\n" not in title else 9.5)
    ax.set_ylabel("µV")
    if t_start:
        span = (win[1] - win[0])
        step = next((s for s in (5, 10, 15, 20, 30, 60, 120, 300, 600) if span / s <= 10), 900)
        loc = (mdates.SecondLocator(bysecond=range(0, 60, step)) if step < 60
               else mdates.MinuteLocator(byminute=range(0, 60, step // 60)))
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        ax.set_xlabel("Time (HH:MM:SS)")
    else:
        ax.set_xlabel("s from recording start")
    ax.grid(True, color="#BBBBBB", lw=0.6)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()


def print_setup(ws, last_row: int) -> None:
    """Landscape, table scaled to the LEFT half of each page; right half left blank for
    handwritten annotations . Header row repeats on every page."""
    ws.print_area = f"A1:I{last_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0                      # as many pages tall as needed
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left, ws.page_margins.right = 0.4, 5.6     # 11 in wide: content in the left 5 in
    ws.page_margins.top, ws.page_margins.bottom = 0.5, 0.5
    ws.print_title_rows = "1:1"
    ws.print_options.gridLines = True
