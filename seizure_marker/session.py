"""
Data model for the interactive seizure on/off marker (no Qt in this module).

A Session is an ordered queue of Recordings. Each Recording knows where its Open
Ephys folder is, which channel / detector profile to use, and holds a list of
Marks (one per candidate seizure). Marks start life as the detector's events
("pending"), and the user turns each into "auto" (accepted as placed),
"edited" (dragged), "rejected" (not a seizure) or "added" (placed by hand).

Signals are loaded on demand and cached for the current recording only, so a
queue of 30+ one-hour recordings does not fill memory. Decisions are autosaved
to <out_dir>/marker_session.json after every change.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import detector as oo

DEFAULT_OUT_DIR = Path.home() / "SeizureMarker"      # autosave + default export location
DEFAULT_CHANNEL = "CH2"
DEFAULT_PROFILE = "chol"
STATUSES = ("pending", "auto", "edited", "rejected", "added")
COMMITTED = ("auto", "edited", "added")
USER_PRESETS_PATH = Path.home() / ".seizure_marker_presets.json"
from . import __version__
VERSION = __version__

# Sensitivity scale shown to users: one knob that scales the onset threshold.
# Lower threshold = more sensitive (more candidate events for the reviewer).
SENSITIVITY = {-2: ("Least", 1.6), -1: ("Low", 1.3), 0: ("Normal", 1.0), 1: ("High", 0.8), 2: ("Most", 0.65)}

# Front-end filter (signal_processing.py) — reported in exports
FILTER_TEXT = "60 Hz notch (Q = 30) followed by a 1–70 Hz 4th-order Butterworth band-pass"

# (key path, short label, plain-language description) — drives the Advanced panel,
# the Parameters tab of the export and the README.
PARAM_DOC = [
    (("threshold",), "Onset threshold (×)",
     "Both energy and line-length must exceed this multiple of their own rolling baseline for a second to count as 'hot'. "
     "Lower = more sensitive. The sensitivity slider multiplies this value."),
    (("persistence_k",), "Hot seconds needed (K)", "A candidate onset is declared once K of the last M seconds are hot."),
    (("persistence_m",), "Window for K (M, s)", "Length of the window the K hot seconds must fall in."),
    (("baseline_window",), "Rolling baseline (s)",
     "Energy and line-length are divided by their median over this many preceding seconds. Longer windows do not adapt into a "
     "slow pre-seizure build-up (PTZ); nothing can be detected in the first window of a recording."),
    (("onset_refine",), "Onset look-back",
     "Offline only: move the onset from the K-th hot second back to the first hot second of the cluster that triggered it."),
    (("offset", "frac"), "Offset: below × baseline",
     "The seizure ends when the per-second standard deviation drops below this multiple of the pre-seizure baseline "
     "(median std over the baseline span before onset)."),
    (("offset", "k"), "… for (s)", "Consecutive seconds below the offset level needed to call the offset."),
    (("offset", "min_s"), "Minimum duration (s)", "The offset search never starts earlier than this after onset."),
    (("offset", "baseline_span"), "Baseline span (s)", "Pre-seizure window, relative to onset, over which the baseline std is measured."),
    (("offset", "tier2_frac"), "Fallback: below × baseline", "If no postictal suppression is found, accept the std returning to this multiple of baseline …"),
    (("offset", "tier2_k"), "… for (s)", "… for this many consecutive seconds."),
    (("offset", "merge_s"), "Merge re-detections within (s)",
     "A new detection starting within this many seconds of an offset is treated as the same seizure (offset search restarts after it)."),
    (("offset", "confirm_s"), "Burst lookahead (s)",
     "A quiet stretch is not the end if a burst follows within this many seconds (0 = off). Handles seizures that end as a train of bursts."),
    (("offset", "resume_frac"), "… burst above × baseline", "Std level that counts as a burst for the lookahead."),
    (("offset", "resume_k"), "… burst length (s)", "Consecutive seconds above the burst level that count as a burst."),
    (("offset", "inherit_s"), "Reuse previous baseline within (s)",
     "An event starting within this many seconds of the previous event's offset reuses that event's pre-seizure baseline, "
     "because its own baseline window would lie in the postictal quiet (0 = off)."),
    (("offset", "backstop_s"), "Give up after (s)", "If no offset is found, the event is closed this long after onset and flagged."),
    (("sensitivity",), "Sensitivity", "Slider position (−2 … +2); scales the onset threshold by " +
     ", ".join(f"{v[1]}× ({v[0]})" for v in SENSITIVITY.values()) + "."),
]


def profile_value(profile: dict, key: tuple) -> object:
    """Value of a (possibly nested) profile key, falling back to OffsetRule defaults."""
    if len(key) == 1:
        if key[0] == "sensitivity":
            return int(profile.get("sensitivity", 0))
        if key[0] == "onset_refine":
            return bool(profile.get("onset_refine", False))
        return profile.get(key[0])
    return profile.get("offset", {}).get(key[1], getattr(oo.OffsetRule(), key[1]))


def effective_threshold(profile: dict) -> float:
    return round(float(profile["threshold"]) * SENSITIVITY[int(profile.get("sensitivity", 0))][1], 3)


def effective_params(profile: dict) -> dict:
    """Profile with the sensitivity slider folded into the threshold."""
    p = copy.deepcopy(profile)
    p["threshold"] = effective_threshold(profile)
    return p


def parameter_rows(profile: dict) -> list[tuple[str, str, object, str]]:
    """(key, label, value, description) for every documented parameter, plus the effective threshold."""
    rows = []
    for key, label, desc in PARAM_DOC:
        v = profile_value(profile, key)
        if isinstance(v, (tuple, list)):
            v = f"{v[0]} … {v[1]}"
        rows.append((".".join(key), label, v, desc))
    rows.insert(1, ("threshold_effective", "Effective onset threshold (×)", effective_threshold(profile),
                    "Onset threshold × sensitivity factor — the value actually used."))
    return rows


def load_presets() -> dict[str, dict]:
    """Built-in profiles plus the user's saved presets (user presets override on name clash)."""
    presets = {k: copy.deepcopy(v) for k, v in oo.PROFILES.items()}
    if USER_PRESETS_PATH.exists():
        try:
            presets.update(json.loads(USER_PRESETS_PATH.read_text()))
        except Exception:
            pass
    return presets


def save_preset(name: str, profile: dict) -> None:
    user = {}
    if USER_PRESETS_PATH.exists():
        try:
            user = json.loads(USER_PRESETS_PATH.read_text())
        except Exception:
            user = {}
    p = copy.deepcopy(profile)
    p.pop("fs", None)
    user[name] = p
    USER_PRESETS_PATH.write_text(json.dumps(user, indent=1))


def delete_preset(name: str) -> None:
    if name in oo.PROFILES or not USER_PRESETS_PATH.exists():
        return
    user = json.loads(USER_PRESETS_PATH.read_text())
    user.pop(name, None)
    USER_PRESETS_PATH.write_text(json.dumps(user, indent=1))


def methods_text(profile: dict, fs: float | None = None, channel: str | None = None) -> str:
    """A methods-section paragraph describing exactly these settings."""
    p = profile
    o = {**{k: getattr(oo.OffsetRule(), k) for k in ("frac", "k", "min_s", "baseline_span", "tier2_frac", "tier2_k",
                                                      "merge_s", "confirm_s", "resume_frac", "resume_k", "inherit_s", "backstop_s")},
         **p.get("offset", {})}
    span = o["baseline_span"]
    t_eff = effective_threshold(p)
    sens = SENSITIVITY[int(p.get("sensitivity", 0))][0].lower()
    ch = f" ({channel})" if channel else ""
    fs_txt = f", sampled at {fs:g} Hz," if fs else ""
    s = (f"Seizure onset and offset times were determined with a semi-automated detector followed by manual review. "
         f"The ECoG channel{ch}{fs_txt} was {FILTER_TEXT}. Signal energy (mean squared amplitude) and line length "
         f"(mean absolute sample-to-sample difference) were computed in 2 s windows advanced in 1 s steps and expressed "
         f"as ratios to their medians over the preceding {p['baseline_window']} s. A second was flagged when both ratios "
         f"exceeded {t_eff:g} (sensitivity setting: {sens}), and a candidate onset was declared when at least "
         f"{p['persistence_k']} of the last {p['persistence_m']} s were flagged")
    s += ("; the onset was then moved back to the first flagged second of that cluster. " if p.get("onset_refine")
          else ". ")
    s += (f"The offset was defined as the first of {o['k']} consecutive seconds in which the per-second standard "
          f"deviation fell below {o['frac']:g}× the pre-ictal baseline (median per-second standard deviation from "
          f"{-span[0]} to {-span[1]} s before onset), searched from {o['min_s']} s after onset")
    if o["confirm_s"] > 0:
        s += (f"; a quiet stretch followed within {o['confirm_s']} s by at least {o['resume_k']} s above "
              f"{o['resume_frac']:g}× baseline was treated as a pause within the seizure")
    s += (f". When no such suppression occurred, {o['tier2_k']} consecutive seconds below {o['tier2_frac']:g}× baseline "
          f"were accepted instead, and events with no identifiable offset were closed {o['backstop_s'] // 60} min after "
          f"onset and flagged. Detections re-starting within {o['merge_s']} s of an offset were merged into a single event")
    if o["inherit_s"] > 0:
        s += (f", and an event beginning within {o['inherit_s']} s of a previous event's offset reused that event's "
              f"pre-ictal baseline")
    s += (". Candidate onsets and offsets were then displayed with the filtered trace in an interactive viewer in which "
          "a reviewer accepted, adjusted, rejected or added events; the reviewer's final times were used for all "
          "subsequent analyses.")
    return s


@dataclass
class Mark:
    onset: float
    offset: float
    auto_onset: float | None = None      # detector's placement (None for hand-added)
    auto_offset: float | None = None
    status: str = "pending"
    note: str = ""
    tier: str = ""
    runs: int = 1

    @property
    def committed(self) -> bool:
        return self.status in COMMITTED

    @property
    def moved(self) -> bool:
        """Cursors differ from the detector's placement."""
        if self.auto_onset is None:
            return True
        return abs(self.onset - self.auto_onset) > 1e-3 or abs(self.offset - self.auto_offset) > 1e-3


@dataclass
class Recording:
    path: Path
    channel: str = DEFAULT_CHANNEL
    profile_name: str = DEFAULT_PROFILE
    profile: dict = field(default_factory=dict)      # editable copy of oo.PROFILES[profile_name]
    dataset: str = ""
    mouse: str = ""
    date: str = ""                                     # YYYYMMDD
    session: str = ""                                  # heating session # from the log, if known
    marks: list[Mark] = field(default_factory=list)
    detected: bool = False                             # detector has run (marks populated)
    error: str = ""
    fs_override: float = 0.0                           # 0 = use the sampling rate from the file's metadata
    # transient, current recording only
    fs: float = 0.0
    filt: np.ndarray | None = None
    n_seconds: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def loaded(self) -> bool:
        return self.filt is not None

    def unload(self) -> None:
        self.filt = None

    def date_pretty(self) -> str:
        d = self.date
        return f"{int(d[4:6])}/{int(d[6:])}/{d[:4]}" if len(d) == 8 and d.isdigit() else d

    def label(self) -> str:
        parts = [p for p in (self.dataset, self.mouse, self.date_pretty()) if p]
        return "  |  ".join(parts) if parts else self.name


# ── metadata inference ───────────────────────────────────────────────

_REC_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_\d{2}-\d{2}-\d{2}_?(.*)$")


def infer_from_name(rec: Path) -> tuple[str, str]:
    """(YYYYMMDD, mouse) from an Open Ephys folder name like
    2025-12-03_10-47-07_4826het_seizure1; empty strings if not recognisable."""
    m = _REC_RE.match(rec.name)
    if not m:
        return "", ""
    date = m.group(1) + m.group(2) + m.group(3)
    mm = re.search(r"(\d{3,5})", m.group(4))
    return date, (mm.group(1) if mm else "")


def find_lab_log(rec: Path, max_up: int = 5) -> Path | None:
    p = rec
    for _ in range(max_up):
        p = p.parent
        hits = [x for x in p.glob("*.xlsx") if "log" in x.name.lower() and not x.name.startswith("~$")]
        if hits:
            return hits[0]
        if p == p.parent:
            break
    return None


def log_rows(path: Path) -> list[dict]:
    """Rows of a lab log laid out like the LC / DG / cholinergic logs (header on row 2)."""
    import openpyxl
    try:
        ws = openpyxl.load_workbook(path, data_only=True, read_only=True).active
        rows = list(ws.iter_rows(values_only=True))
    except Exception:
        return []
    if len(rows) < 2:
        return []
    hdr = [str(h).strip() if h is not None else "" for h in rows[1]]
    out = []
    for r in rows[2:]:
        d = dict(zip(hdr, r))
        if d.get("mouse ID #") is None:
            continue
        out.append(d)
    return out


def dataset_label(folder: Path) -> str:
    """'6 DG ACH hot seizure' -> 'DG ACH hot seizure'; '2_MS ChAT hyperthermia' -> 'MS ChAT hyperthermia'."""
    return re.sub(r"^\d+[_\s-]*", "", folder.name).strip()


def make_recording(rec: Path, channel: str | None = None, profile_name: str = DEFAULT_PROFILE,
                   profile: dict | None = None) -> Recording:
    """Recording with metadata filled from the folder name and, if one is found
    above it, the lab log (ECoG channel, session #, dataset label)."""
    rec = Path(rec)
    date, mouse = infer_from_name(rec)
    r = Recording(path=rec, date=date, mouse=mouse, profile_name=profile_name,
                  profile=copy.deepcopy(profile if profile is not None else load_presets()[profile_name]))
    log = find_lab_log(rec)
    if log is not None:
        r.dataset = dataset_label(log.parent)
        for d in log_rows(log):
            if str(d.get("mouse ID #")) == mouse and str(d.get("date")) == date:
                ch = d.get("channel(s): ECoG")
                if ch is not None and channel is None:
                    try:
                        r.channel = f"CH{int(ch)}"
                    except (TypeError, ValueError):
                        r.channel = str(ch)
                if d.get("heating session #") is not None:
                    r.session = str(d["heating session #"])
                break
    else:
        # two levels up is usually the cohort / experiment folder
        r.dataset = dataset_label(rec.parent.parent) if rec.parent.parent != rec.parent else ""
    if channel:
        r.channel = channel
    return r


def discover_recordings(paths: list[Path]) -> list[Path]:
    """Every Open Ephys session folder (one containing structure.oebin under a
    Record Node) at or below the given paths, deduplicated and sorted."""
    found: set[Path] = set()
    for p in paths:
        p = Path(p)
        if p.is_file():
            p = p.parent
        # recurse_symlinks: a folder of shortcuts (e.g. a demo folder) works; resolve() so the
        # lab-log lookup and dataset label come from the real location, not the shortcut
        # os.walk(followlinks=True): a folder of shortcuts/symlinks works on every Python
        # version; resolve() so the lab-log lookup and dataset label use the real location
        for root, _dirs, files in os.walk(p, followlinks=True):
            if "structure.oebin" not in files:
                continue
            oebin = Path(root) / "structure.oebin"
            node = next((a for a in oebin.parents if a.name.startswith("Record Node")), None)
            found.add((node.parent if node is not None else oebin.parent).resolve())
    return sorted(found)


# ── signal loading / detection ───────────────────────────────────────

def rule_for(profile: dict) -> oo.OffsetRule:
    return oo.OffsetRule(**profile.get("offset", {}))


def load_signal(rec: Recording) -> None:
    """Load + filter the channel into rec.filt (raises on missing data)."""
    try:
        sig, fs, info = oo.load(rec.path, rec.channel, None)
    except (FileNotFoundError, ValueError, OSError) as e:
        if "mmap" in str(e) or "No such file" in str(e) or "continuous.dat" in str(e):
            raise ValueError("no EEG data in this recording (continuous.dat is missing or empty)") from e
        raise
    if rec.fs_override and rec.fs_override > 0:
        fs = float(rec.fs_override)
    if len(sig) < fs * 120:
        raise ValueError(f"recording too short ({len(sig) / fs:.0f} s) — no usable EEG")
    rec.fs = fs
    rec.filt = oo.SignalProcessor(fs=fs).process(sig)
    rec.n_seconds = int(len(rec.filt) // fs)
    rec.channel = info["channel"]


def run_detector(rec: Recording) -> list[Mark]:
    """Run the onset+offset detector on the loaded signal; returns fresh marks."""
    if not rec.loaded:
        load_signal(rec)
    std = oo.per_second_std(rec.filt, rec.fs)
    energy, ll = oo.energy_ll(rec.filt, rec.fs)
    params = effective_params(rec.profile)
    runs = oo.detector_runs(energy, ll, params)
    events = oo.build_events(std, runs, rule_for(params))
    marks = [Mark(onset=float(e.onset_s), offset=float(e.offset_s), auto_onset=float(e.onset_s),
                  auto_offset=float(e.offset_s), status="pending", tier=e.tier, runs=e.n_runs)
             for e in events]
    rec.marks = marks
    rec.detected = True
    rec.error = ""
    return marks


def envelope(y: np.ndarray, fs: float, n_bins: int = 4000) -> tuple[np.ndarray, np.ndarray]:
    """Min/max envelope for the whole-recording overview: (x in s, y) with two
    points per bin so a line plot shows the full amplitude range."""
    m = max(1, len(y) // n_bins)
    nb = len(y) // m
    blk = y[: nb * m].reshape(nb, m)
    mn, mx = blk.min(axis=1), blk.max(axis=1)
    x = (np.arange(nb) * m + m / 2) / fs
    return np.repeat(x, 2), np.column_stack([mn, mx]).ravel()


# ── session ──────────────────────────────────────────────────────────

class Session:
    def __init__(self, out_dir: Path | None = None):
        self.out_dir = Path(out_dir or DEFAULT_OUT_DIR)
        self.recordings: list[Recording] = []
        self.autosave_path = self.out_dir / "marker_session.json"

    # queue management
    def add_paths(self, paths: list[Path], channel: str | None = None,
                  profile_name: str = DEFAULT_PROFILE, profile: dict | None = None) -> list[Recording]:
        have = {r.path.resolve() for r in self.recordings}
        new = []
        for p in discover_recordings(paths):
            if p.resolve() in have:
                continue
            r = make_recording(p, channel, profile_name, profile)
            self.recordings.append(r)
            new.append(r)
        self.restore(new)
        return new

    def index_of(self, rec: Recording) -> int:
        return self.recordings.index(rec)

    # flattened event navigation
    def event_list(self) -> list[tuple[int, int]]:
        return [(ri, mi) for ri, r in enumerate(self.recordings) for mi in range(len(r.marks))]

    def committed_rows(self) -> list[tuple[Recording, int, Mark]]:
        return [(r, mi, m) for r in self.recordings for mi, m in enumerate(r.marks)
                if m.status != "pending"]

    def progress(self) -> tuple[int, int]:
        """(recordings with every mark decided, total recordings)."""
        done = sum(1 for r in self.recordings if r.detected and all(m.status != "pending" for m in r.marks))
        return done, len(self.recordings)

    # persistence
    def autosave(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        data = dict(saved=datetime.now().isoformat(timespec="seconds"), recordings=[
            dict(path=str(r.path), channel=r.channel, profile_name=r.profile_name, profile=r.profile,
                 dataset=r.dataset, mouse=r.mouse, date=r.date, session=r.session, detected=r.detected,
                 error=r.error, fs_override=r.fs_override, marks=[asdict(m) for m in r.marks])
            for r in self.recordings])
        tmp = self.autosave_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.autosave_path)

    def restore(self, recs: list[Recording] | None = None) -> int:
        """Restore marks/decisions for the given (or all) recordings from the
        autosave file. Returns the number of recordings restored."""
        if not self.autosave_path.exists():
            return 0
        try:
            saved = {d["path"]: d for d in json.loads(self.autosave_path.read_text())["recordings"]}
        except Exception:
            return 0
        n = 0
        for r in (recs if recs is not None else self.recordings):
            d = saved.get(str(r.path))
            if not d:
                continue
            r.channel, r.profile_name = d["channel"], d.get("profile_name", r.profile_name)
            r.profile = d.get("profile") or r.profile
            r.dataset, r.mouse, r.date, r.session = d["dataset"], d["mouse"], d["date"], d.get("session", "")
            r.detected, r.error = d.get("detected", False), d.get("error", "")
            r.fs_override = float(d.get("fs_override", 0.0))
            r.marks = [Mark(**m) for m in d["marks"]]
            n += 1
        return n
