"""
Write a synthetic Open Ephys recording with a couple of fake seizures, so the
app can be tried (and tested) without any lab data.

    python -m seizure_marker.synthetic                 # -> ./synthetic_demo/<recording folder>
    python -m seizure_marker.synthetic OUT_DIR --minutes 20 --mouse 0001

The folder layout, structure.oebin and continuous.dat match what Open Ephys
writes (Record Node / experiment / recording), with two channels: CH1 is quiet
background, CH2 carries the seizures. The folder name follows the lab's
convention (YYYY-MM-DD_HH-MM-SS_<mouse>...), so mouse number and date are picked
up automatically.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

BIT_VOLTS = 0.195                      # µV per int16 step (Intan / Open Ephys default)
# (onset s, duration s): where the fake seizures go
DEFAULT_SEIZURES = ((600.0, 45.0), (900.0, 30.0))


def _background(n: int, fs: float, rng: np.random.Generator, std_uv: float = 40.0) -> np.ndarray:
    """Band-limited noise (1-40 Hz) that looks roughly like resting ECoG."""
    sos = butter(2, [1.0 / (fs / 2), 40.0 / (fs / 2)], btype="band", output="sos")
    x = sosfilt(sos, rng.standard_normal(n))
    return x / x.std() * std_uv


def synthesize(minutes: float = 20.0, fs: float = 1000.0, seizures=DEFAULT_SEIZURES,
               seed: int = 0) -> np.ndarray:
    """(n_samples, 2) float µV: column 0 = quiet CH1, column 1 = CH2 with seizures."""
    rng = np.random.default_rng(seed)
    n = int(minutes * 60 * fs)
    ch1 = _background(n, fs, rng, 30.0)
    ch2 = _background(n, fs, rng, 40.0)
    t = np.arange(n) / fs
    for onset, dur in seizures:
        a, b = int(onset * fs), int((onset + dur) * fs)
        tt = t[a:b] - onset
        # spike-wave train at 8 Hz: sharp spikes riding on a larger slow wave,
        # amplitude ramping up over the first 3 s and tapering in the last 3 s
        env = np.clip(tt / 3.0, 0, 1) * np.clip((dur - tt) / 3.0, 0, 1)
        phase = (tt * 8.0) % 1.0
        spikes = 350.0 * np.exp(-((phase - 0.5) / 0.05) ** 2)
        slow = 150.0 * np.sin(2 * np.pi * 8.0 * tt)
        ch2[a:b] = env * (spikes + slow) + 3.0 * ch2[a:b]
        # postictal suppression: 60 s of much quieter background
        c = min(n, int((onset + dur + 60) * fs))
        ch2[b:c] *= 0.35
    return np.column_stack([ch1, ch2])


def write_recording(out_dir: Path, mouse: str = "0001", minutes: float = 20.0, fs: float = 1000.0,
                    seizures=DEFAULT_SEIZURES, seed: int = 0, when: datetime | None = None) -> Path:
    """Write <out_dir>/<YYYY-MM-DD_HH-MM-SS_<mouse>_synthetic>/Record Node 101/experiment1/recording1/…
    and return the recording folder (the one named after the date)."""
    when = when or datetime(2026, 1, 15, 10, 30, 0)
    rec = Path(out_dir) / f"{when:%Y-%m-%d_%H-%M-%S}_{mouse}_synthetic"
    node = rec / "Record Node 101" / "experiment1" / "recording1"
    stream_dir = node / "continuous" / "Synthetic-100.Rhythm Data"
    stream_dir.mkdir(parents=True, exist_ok=True)
    sig = synthesize(minutes, fs, seizures, seed)
    raw = np.clip(np.round(sig / BIT_VOLTS), -32768, 32767).astype("<i2")
    raw.tofile(stream_dir / "continuous.dat")
    np.save(stream_dir / "sample_numbers.npy", np.arange(len(raw), dtype=np.int64))
    meta = {
        "GUI version": "0.6.7",
        "continuous": [{
            "folder_name": "Synthetic-100.Rhythm Data",
            "sample_rate": float(fs),
            "stream_name": "Rhythm Data",
            "source_processor_name": "Synthetic",
            "num_channels": 2,
            "channels": [
                {"channel_name": "CH1", "bit_volts": BIT_VOLTS, "units": "uV"},
                {"channel_name": "CH2", "bit_volts": BIT_VOLTS, "units": "uV"},
            ],
        }],
        "events": [],
        "spikes": [],
    }
    (node / "structure.oebin").write_text(json.dumps(meta, indent=2))
    (rec / "README_synthetic.txt").write_text(
        "Synthetic recording written by seizure_marker.synthetic. CH2 contains fake seizures at "
        + ", ".join(f"{on:.0f}-{on + d:.0f} s" for on, d in seizures) + f" ({fs:g} Hz, {minutes:g} min).\n")
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="write a synthetic Open Ephys recording for trying the app")
    ap.add_argument("out_dir", nargs="?", default="synthetic_demo")
    ap.add_argument("--mouse", default="0001")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--fs", type=float, default=1000.0)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    rec = write_recording(Path(a.out_dir), a.mouse, a.minutes, a.fs, seed=a.seed)
    print(f"wrote {rec}\nOpen that folder (or {a.out_dir}) in the app; the seizures are on CH2 at "
          + ", ".join(f"{on:.0f}-{on + d:.0f} s" for on, d in DEFAULT_SEIZURES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
