"""
Offline seizure onset + offset detection for already-recorded EEG (one channel).

Input formats:
  * Open Ephys binary recordings (a folder containing ``structure.oebin``)
  * ``.mat`` files holding one Nx1 variable (sampling rate given explicitly)

Pipeline:
  1. Load -> 60 Hz notch + 1-70 Hz band-pass (``signal_processing``).
  2. Onset: energy AND line-length ratios to a rolling-median baseline, K-of-M
     persistence (``energy_ll_core``), with a per-dataset parameter profile
     (``PROFILES``) -> active runs; optional look-back to the first hot second.
  3. Offset: postictal-suppression rule — per-second std < frac x the pre-onset
     baseline std for k consecutive seconds; burst-train lookahead; tier-2
     fallback; backstop as a flagged safety net. Re-fires are merged into one
     event and an event starting soon after another reuses its baseline.

Command line (also ``seizure-marker-cli`` after ``pip install``):
    python -m seizure_marker.detector PATH --channel CH2 --profile chol --out output
    python -m seizure_marker.detector recording.mat --fs 500 --profile ppn

Writes <out>/<name>/events.csv and one PNG per event.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib.pyplot as plt

from . import energy_ll_core as core
from .signal_processing import SignalProcessor


# Onset-detector parameters are per epilepsy type (PI, 2026-08-23).
PROFILES = {
    # PPN chronic: the balanced operating point, tuned on mouse 3728.
    "ppn": dict(threshold=3.0, persistence_k=3, persistence_m=5, baseline_window=60, fs=500.0,
                # no burst-train lookahead: 3728 keeps flapping after a seizure and
                # the lookahead reads that as continuation (#2: 41 s -> 175 s).
                offset=dict(confirm_s=0, inherit_s=0)),
    # Scn1a hyperthermia (LC / DG GRAB-NE heating sessions). Starts from the PPN point.
    "lc": dict(threshold=3.0, persistence_k=3, persistence_m=5, baseline_window=60, fs=None,
               onset_refine=True, offset=dict(confirm_s=15)),
    # Cholinergic sessions (MS / DG grabACh; hyperthermia and PTZ). Same as
    # ``lc`` but the rolling baseline is 180 s instead of 60 s: PTZ seizures ramp up
    # over 1-3 min of growing spikes, and a 60 s median adapts into the ramp so the
    # burst is never 3x louder than its own run-up (5818 PTZ: 2/5 bursts found with
    # W=60, 5/5 with W=180; the 14 logged LC+DG seizures go 12/14 -> 13/14 with no
    # extra events; lowering T instead added events without finding the bursts).
    "chol": dict(threshold=3.0, persistence_k=3, persistence_m=5, baseline_window=180, fs=None,
                 onset_refine=True, offset=dict(confirm_s=15)),
}
# ``onset_refine`` (offline only, 2026-10-17): the K-of-M persistence rule can only
# declare a run on its K-th hot second, so every causal onset is ~K-1 s late. Once a
# run exists we may look back: the onset is moved to the first hot second of the
# cluster that triggered it (the earliest hot second in the last M seconds, then
# back through any directly preceding hot seconds). The run is the same run; only
# its reported start moves. ``ppn`` keeps it off so the 3728 regression is unchanged.


@dataclass
class OffsetRule:
    frac: float = 0.8            # tier 1: std below this x baseline ...
    k: int = 3                   # ... for this many consecutive seconds
    min_s: int = 5               # never earlier than this after onset
    baseline_span: tuple[int, int] = (-75, -15)
    tier2_frac: float = 1.0      # fallback when there is no postictal suppression
    tier2_k: int = 10
    merge_s: int = 60            # runs starting before offset + merge_s are the same event
    backstop_s: int = 1800       # safety net only
    # Lookahead confirmation (offline only): a quiet stretch is not the offset if
    # a burst (std > resume_frac x baseline for resume_k s) follows within
    # confirm_s — the seizure is still going (Scn1a hyperthermia seizures end as
    # a train of bursts separated by 3-7 s pauses). confirm_s=0 disables it.
    confirm_s: int = 15
    resume_frac: float = 1.5
    resume_k: int = 2
    # Baseline inheritance (offline only, 2026-10-17): an event that starts within
    # inherit_s of the previous event's offset reuses that event's pre-ictal
    # baseline. Otherwise its own [-75, -15) window sits in the previous
    # seizure's postictal quiet, the baseline collapses, and std can never fall
    # below 0.8x of it -> backstop / end_of_recording. 0 disables it.
    inherit_s: int = 300


@dataclass
class Event:
    onset_s: int
    offset_s: int
    tier: str                    # "1", "2", "backstop" or "end_of_recording"
    baseline_std: float
    n_runs: int
    run_starts: list[int] = field(default_factory=list)
    # quiet stretches the burst-train lookahead overruled (empty = lookahead never fired)
    lookahead_rejected: list[int] = field(default_factory=list)
    baseline_inherited: bool = False     # baseline taken from the previous event (see OffsetRule.inherit_s)

    @property
    def duration_s(self) -> int:
        return self.offset_s - self.onset_s


# ── Loading ──────────────────────────────────────────────────────────

def _find_oebin(path: Path) -> Path:
    """structure.oebin for an Open Ephys path; prefers the lowest-numbered
    Record Node (later nodes can sit after an OE bandpass filter)."""
    if path.is_file() and path.name == "structure.oebin":
        return path
    found = sorted(path.rglob("structure.oebin"))
    if not found:
        raise FileNotFoundError(f"no structure.oebin under {path}")

    def node_num(p: Path) -> int:
        m = re.search(r"Record Node (\d+)", str(p))
        return int(m.group(1)) if m else 0
    return min(found, key=lambda p: (node_num(p), str(p)))


def _channel_index(names: list[str], channel: str | int) -> int:
    """'CH8', '8' or 8 -> index of 'CH8' (1-based numbering, as in the lab log)."""
    s = str(channel).strip()
    if s in names:
        return names.index(s)
    if s.isdigit() and f"CH{int(s)}" in names:
        return names.index(f"CH{int(s)}")
    raise ValueError(f"channel {channel!r} not in {names}")


def load_openephys(path: Path, channel: str | int) -> tuple[np.ndarray, float, dict]:
    oebin = _find_oebin(path)
    meta = json.loads(oebin.read_text())
    # the amplifier stream = the continuous stream with the most channels
    stream = max(meta["continuous"], key=lambda s: s["num_channels"])
    names = [c["channel_name"] for c in stream["channels"]]
    ci = _channel_index(names, channel)
    nch = stream["num_channels"]
    dat = oebin.parent / "continuous" / stream["folder_name"] / "continuous.dat"
    raw = np.memmap(dat, dtype="<i2", mode="r").reshape(-1, nch)
    sig = np.asarray(raw[:, ci], dtype=np.float64) * stream["channels"][ci]["bit_volts"]
    info = dict(source=str(oebin), channel=names[ci])
    # temperature probe (ADC1 in the LC rig), per-second mean, raw volts — context only
    if "ADC1" in names:
        a = names.index("ADC1")
        fs = float(stream["sample_rate"])
        n = raw.shape[0] // int(fs)
        adc = np.asarray(raw[: n * int(fs), a], dtype=np.float64) * stream["channels"][a]["bit_volts"]
        info["adc1"] = adc.reshape(n, int(fs)).mean(axis=1)
    return sig, float(stream["sample_rate"]), info


def load_mat(path: Path, fs: float) -> tuple[np.ndarray, float, dict]:
    import scipy.io as sio
    mat = sio.loadmat(str(path))
    key = [k for k in mat if not k.startswith("__")][0]
    return mat[key].astype(np.float64).ravel(), fs, dict(source=str(path), channel=key)


def load(path: Path, channel: str | int | None, fs: float | None) -> tuple[np.ndarray, float, dict]:
    if path.suffix.lower() == ".mat":
        return load_mat(path, fs or 500.0)
    if channel is None:
        raise ValueError("Open Ephys recordings need --channel (e.g. CH8)")
    return load_openephys(path, channel)


# ── Features ─────────────────────────────────────────────────────────

def per_second_std(sig: np.ndarray, fs: float) -> np.ndarray:
    """std of each 1 s chunk of the filtered signal; index s == detector second s."""
    c = int(round(fs))
    n = len(sig) // c
    return sig[: n * c].reshape(n, c).std(axis=1)


def energy_ll(sig: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Per-second energy + line-length over 2 s windows (index i -> second i+1),
    vectorised version of extract_features_3728.features_from_signal."""
    c = int(round(fs))
    n = len(sig) // c
    x = sig[: n * c].reshape(n, c)
    w = np.concatenate([x[:-1], x[1:]], axis=1)                  # (n-1, 2c)
    energy = np.mean(w ** 2, axis=1)
    ll = np.mean(np.abs(np.diff(w, axis=1)), axis=1)
    return energy, ll


def detector_runs(energy: np.ndarray, ll: np.ndarray, p: dict) -> list[tuple[int, int]]:
    """(start_second, end_second_inclusive) of each active run, same decision
    rule and second indexing as core.replay()."""
    W = p["baseline_window"]
    n = len(energy)
    if n <= W:
        return []
    e_base = core.prior_median(energy, W)
    ll_base = core.prior_median(ll, W)
    k = np.arange(W, n)
    hot = (energy[k] / np.maximum(e_base[k], core.EPS) > p["threshold"]) & \
          (ll[k] / np.maximum(ll_base[k], core.EPS) > p["threshold"])
    M = p["persistence_m"]
    active = core.trailing_sum(hot.astype(np.int64), M) >= p["persistence_k"]
    edges = np.diff(np.concatenate(([0], active.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
    if p.get("onset_refine"):
        starts = [refine_onset(hot, s, M, prev_end) for s, prev_end in
                  zip(starts, np.concatenate(([-1], ends[:-1])))]
    return [(W + s + 1, W + e + 1) for s, e in zip(starts, ends)]


def refine_onset(hot: np.ndarray, start: int, m: int, prev_end: int) -> int:
    """Look-back onset (offline only): the first hot second of the hot cluster
    that made ``start`` active -- earliest hot second within the last ``m``
    seconds, extended back through directly preceding hot seconds. Never moves
    into the previous run."""
    lo = max(0, start - m + 1, prev_end + 1)
    hot_idx = np.flatnonzero(hot[lo: start + 1])
    s = lo + int(hot_idx[0]) if len(hot_idx) else start
    while s - 1 > prev_end and hot[s - 1]:
        s -= 1
    return s


# ── Offset rule ──────────────────────────────────────────────────────

def baseline_std(std: np.ndarray, onset: int, rule: OffsetRule) -> float:
    a, b = onset + rule.baseline_span[0], onset + rule.baseline_span[1]
    base = std[max(0, a): max(0, b)]
    return float(np.median(base)) if len(base) >= 10 else float("nan")


def _first_quiet(std: np.ndarray, start: int, stop: int, thresh: float, k: int) -> int | None:
    run = 0
    for s in range(start, stop):
        run = run + 1 if std[s] < thresh else 0
        if run >= k:
            return s - k + 1
    return None


def _burst_after(std: np.ndarray, start: int, stop: int, thresh: float, k: int) -> int | None:
    """First second after the end of a burst (>= k s above thresh) that begins
    in [start, stop), or None if there is no such burst."""
    run = 0
    for s in range(start, min(stop, len(std))):
        run = run + 1 if std[s] > thresh else 0
        if run >= k:
            e = s + 1
            while e < len(std) and std[e] > thresh:
                e += 1
            return e
    return None


def find_offset_detail(std: np.ndarray, onset: int, search_from: int, base: float,
                       rule: OffsetRule) -> tuple[int, str, list[int]]:
    """(offset second, tier, quiet stretches the lookahead rejected). A rejected
    stretch is a candidate offset that was overruled because a burst followed it."""
    stop = min(onset + rule.backstop_s, len(std))
    if not np.isfinite(base):
        return stop, "no_baseline", []
    for frac, k, tier in ((rule.frac, rule.k, "1"), (rule.tier2_frac, rule.tier2_k, "2")):
        start = search_from
        rejected: list[int] = []
        while True:
            s = _first_quiet(std, start, stop, frac * base, k)
            if s is None or rule.confirm_s <= 0:
                break
            resume = _burst_after(std, s + k, s + k + rule.confirm_s,
                                  rule.resume_frac * base, rule.resume_k)
            if resume is None:
                break
            rejected.append(s)
            start = resume          # seizure continued: look for quiet after this burst
        if s is not None:
            return s, tier, rejected
    return stop, ("end_of_recording" if stop == len(std) else "backstop"), []


def find_offset(std: np.ndarray, onset: int, search_from: int, base: float,
                rule: OffsetRule) -> tuple[int, str]:
    s, tier, _ = find_offset_detail(std, onset, search_from, base, rule)
    return s, tier


def build_events(std: np.ndarray, runs: list[tuple[int, int]], rule: OffsetRule) -> list[Event]:
    """Group detector runs into events and give each one an offset."""
    events: list[Event] = []
    i = 0
    while i < len(runs):
        onset, end = runs[i]
        inherited = bool(events) and rule.inherit_s > 0 and onset - events[-1].offset_s <= rule.inherit_s
        base = events[-1].baseline_std if inherited else baseline_std(std, onset, rule)
        search_from = max(onset + rule.min_s, end + 1)
        off, tier, rejected = find_offset_detail(std, onset, search_from, base, rule)
        starts = [onset]
        j = i + 1
        # absorb re-fires: runs that start before offset + merge window
        while j < len(runs) and runs[j][0] <= off + rule.merge_s:
            starts.append(runs[j][0])
            search_from = max(search_from, runs[j][1] + 1)
            off, tier, rejected = find_offset_detail(std, onset, search_from, base, rule)
            j += 1
        events.append(Event(onset, off, tier, base, len(starts), starts, rejected, inherited))
        i = j
    return events


def offset_from_onset(std: np.ndarray, onset: int, rule: OffsetRule) -> tuple[int, str, float]:
    """Rule applied from a given (e.g. hand-marked) onset, no run merging."""
    base = baseline_std(std, onset, rule)
    off, tier = find_offset(std, onset, onset + rule.min_s, base, rule)
    return off, tier, base


# ── Plotting ─────────────────────────────────────────────────────────

def plot_event(path: Path, sig: np.ndarray, std: np.ndarray, fs: float, ev: Event,
               rule: OffsetRule, title: str, truth: tuple[float, float] | None = None,
               adc: np.ndarray | None = None) -> None:
    t0 = max(0, ev.onset_s - 90)
    t1 = min(len(std), ev.offset_s + 90)
    a, b = int(t0 * fs), int(t1 * fs)
    t = np.arange(a, b) / fs
    y = sig[a:b]
    rows = 3 if adc is not None else 2
    fig, axs = plt.subplots(rows, 1, figsize=(14, 2.3 * rows + 1.5), sharex=True,
                            gridspec_kw=dict(height_ratios=[2, 1, 0.7][:rows]))
    lim = 1.1 * np.percentile(np.abs(y), 99.9) if len(y) else 1
    axs[0].plot(t, y, lw=0.4, color="k")
    axs[0].set_ylim(-lim, lim)
    axs[0].set_ylabel("filtered EEG")
    axs[0].set_title(title, fontsize=10)
    secs = np.arange(t0, t1)
    axs[1].plot(secs, std[t0:t1], color="tab:blue", lw=1)
    axs[1].axhline(ev.baseline_std, color="gray", ls=":", label="pre-onset baseline std")
    axs[1].axhline(rule.frac * ev.baseline_std, color="tab:orange", ls="--",
                   label=f"{rule.frac}x baseline")
    axs[1].set_ylabel("per-second std")
    if adc is not None:
        axs[2].plot(secs, adc[t0:t1], color="tab:red", lw=1)
        axs[2].set_ylabel("ADC1 (temp, V)")
    axs[-1].set_xlabel("s from recording start")
    for ax in axs:
        ax.axvspan(ev.onset_s + rule.baseline_span[0], ev.onset_s + rule.baseline_span[1],
                   color="gray", alpha=0.08)
        ax.axvline(ev.onset_s, color="g", lw=1.2)
        ax.axvline(ev.offset_s, color="r", ls="--", lw=1.2)
        for s in ev.run_starts[1:]:
            ax.axvline(s, color="purple", lw=0.6, alpha=0.6)
        if truth is not None:
            ax.axvline(truth[0], color="g", ls=":", lw=1.5)
            ax.axvline(truth[1], color="r", ls=":", lw=1.5)
    axs[0].plot([], [], color="g", label="detected onset")
    axs[0].plot([], [], color="r", ls="--", label=f"offset (tier {ev.tier})")
    if len(ev.run_starts) > 1:
        axs[0].plot([], [], color="purple", lw=0.6, label="merged re-fire")
    if truth is not None:
        axs[0].plot([], [], color="g", ls=":", label="logged onset")
        axs[0].plot([], [], color="r", ls=":", label="logged offset")
    axs[0].legend(loc="upper right", fontsize=8)
    axs[1].legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# ── Drivers ──────────────────────────────────────────────────────────

def analyze(sig: np.ndarray, fs: float, profile: dict, rule: OffsetRule):
    filt = SignalProcessor(fs=fs).process(sig)
    std = per_second_std(filt, fs)
    energy, ll = energy_ll(filt, fs)
    runs = detector_runs(energy, ll, profile)
    return filt, std, runs, build_events(std, runs, rule)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def rule_for(profile: dict) -> OffsetRule:
    return OffsetRule(**profile.get("offset", {}))


def cmd_run(args: argparse.Namespace) -> None:
    profile = dict(PROFILES[args.profile])
    rule = rule_for(profile)
    path = Path(args.path)
    sig, fs, info = load(path, args.channel, args.fs or profile["fs"])
    filt, std, runs, events = analyze(sig, fs, profile, rule)
    name = args.name or re.sub(r"[^\w.-]+", "_", path.stem if path.is_file() else path.name)
    out = Path(args.out) / name
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, ev in enumerate(events, 1):
        rows.append(dict(event=n, onset_s=ev.onset_s, offset_s=ev.offset_s,
                         duration_s=ev.duration_s, offset_tier=ev.tier,
                         merged_runs=ev.n_runs, baseline_std=round(ev.baseline_std, 3)))
        plot_event(out / f"event_{n:03d}.png", filt, std, fs, ev, rule,
                   f"{name} {info['channel']}  event {n}: {ev.onset_s}-{ev.offset_s} s "
                   f"({ev.duration_s} s, tier {ev.tier}, {ev.n_runs} runs)",
                   adc=info.get("adc1"))
    write_csv(out / "events.csv", rows)
    print(f"{name} [{info['channel']}, {fs:g} Hz, {len(std) / 60:.1f} min]: "
          f"{len(runs)} detector runs -> {len(events)} events -> {out}/")
    for r in rows:
        print(f"  #{r['event']:<3} {r['onset_s']:>7}-{r['offset_s']:<7} {r['duration_s']:>5} s  "
              f"tier {r['offset_tier']:<3} runs {r['merged_runs']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one recording (.mat or Open Ephys folder)")
    r.add_argument("path")
    r.add_argument("--channel", help="Open Ephys channel, e.g. CH8 or 8 (1-based)")
    r.add_argument("--profile", choices=sorted(PROFILES), default="chol")
    r.add_argument("--fs", type=float, help="sample rate for .mat files (default: profile)")
    r.add_argument("--name", help="output folder name")
    r.add_argument("--out", default="output", help="where to write <name>/events.csv and PNGs (default: ./output)")
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
