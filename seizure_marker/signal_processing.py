"""
Front-end filter: 60 Hz notch followed by a 1-70 Hz Butterworth band-pass.

Implemented as streaming second-order sections with preserved state so the
same code filters a whole recording at once or one chunk at a time with
identical output.
"""

import numpy as np
from scipy.signal import butter, iirnotch, sosfilt, sosfilt_zi, tf2sos

FS_DEFAULT = 1000.0          # Hz (Open Ephys default)
NOTCH_FREQ = 60.0            # Hz (line noise; use 50.0 outside North America)
NOTCH_Q = 30.0
BAND = (1.0, 70.0)           # Hz band-pass cut-offs
BP_ORDER = 4                 # Butterworth order


class StreamingFilter:
    """Wraps an SOS filter with state preservation for chunk-by-chunk use."""

    def __init__(self, sos: np.ndarray):
        self.sos = sos
        self._zi_template = sosfilt_zi(sos)  # shape (n_sections, 2)
        self._zi: np.ndarray | None = None

    def process(self, chunk: np.ndarray) -> np.ndarray:
        if self._zi is None:
            # Scale initial conditions by the first sample value
            self._zi = self._zi_template * chunk[0]
        filtered, self._zi = sosfilt(self.sos, chunk, zi=self._zi)
        return filtered

    def reset(self) -> None:
        self._zi = None


class SignalProcessor:
    """Composes notch + band-pass streaming filters.

    Parameters
    ----------
    fs : float
        Sampling rate in Hz.
    notch_freq, notch_q : float
        Notch filter centre frequency and quality factor.
    band : tuple[float, float]
        Band-pass (low, high) in Hz.
    bp_order : int
        Butterworth band-pass order.
    """

    def __init__(
        self,
        fs: float = FS_DEFAULT,
        notch_freq: float = NOTCH_FREQ,
        notch_q: float = NOTCH_Q,
        band: tuple[float, float] = BAND,
        bp_order: int = BP_ORDER,
    ):
        self.fs = fs

        # Notch filter (b,a -> SOS)
        b, a = iirnotch(notch_freq, notch_q, fs=fs)
        notch_sos = tf2sos(b, a)
        self._notch = StreamingFilter(notch_sos)

        # Band-pass filter (SOS directly)
        nyq = 0.5 * fs
        low, high = band
        bp_sos = butter(bp_order, [low / nyq, high / nyq], btype="band", output="sos")
        self._bandpass = StreamingFilter(bp_sos)

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """Apply notch then band-pass to a chunk of raw µV data."""
        x = self._notch.process(chunk)
        x = self._bandpass.process(x)
        return x

    def reset(self) -> None:
        self._notch.reset()
        self._bandpass.reset()
