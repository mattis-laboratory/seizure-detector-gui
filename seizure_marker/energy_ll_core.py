"""
Shared energy + line-length decision logic — single source of truth for both
the live streaming detector (`EnergyLineLengthDetector`) and the offline
parameter sweep (`sweep.py`).

The per-second pipeline is:

  1. baseline      = trailing median of the prior ``W`` feature values
  2. foreground    = trailing median of the last ``L`` feature values   (NEW #6)
  3. ratio         = foreground / baseline   (energy and line-length separately)
  4. hot           = (E_ratio > T) and (LL_ratio > T)   (AND — see step())
  5. active        = (# hot in last ``M`` seconds) >= ``K``               (persistence)
  6. onset         = rising edge of ``active`` after a gap > ``gap_tol_s``

``L = median_filter_len = 1`` disables the foreground median filter and exactly
reproduces the original detector.

Two entry points, kept bit-for-bit consistent:
  * ``EnergyLLDecision`` — streaming, one second at a time (used by the live detector).
  * ``replay()``         — vectorised, whole feature arrays at once (used by the sweep).
"""

import collections

import numpy as np

EPS = 1e-12


# ── Streaming decision (live detector) ───────────────────────────────

class EnergyLLDecision:
    """Per-second energy/LL decision with a rolling-median baseline, optional
    foreground median filter, K-of-M persistence and gap-merged run tracking.

    Feed calibration windows via :meth:`add_calibration` until
    :attr:`n_calibration` reaches ``baseline_window``; then call :meth:`step`
    for every detection second.  Onset edge logic is delegated to the caller's
    ``RunTracker`` (passed in) so the live detector keeps its event objects.
    """

    def __init__(
        self,
        run_tracker,
        threshold: float,
        persistence_k: int,
        persistence_m: int,
        baseline_window: int,
        median_filter_len: int = 1,
    ):
        self.run_tracker = run_tracker
        self.threshold = threshold
        self.persistence_k = persistence_k
        self.baseline_window = baseline_window

        self._e_hist: collections.deque[float] = collections.deque(maxlen=baseline_window)
        self._ll_hist: collections.deque[float] = collections.deque(maxlen=baseline_window)
        self._e_fg: collections.deque[float] = collections.deque(maxlen=median_filter_len)
        self._ll_fg: collections.deque[float] = collections.deque(maxlen=median_filter_len)
        self._hot_hist: collections.deque[bool] = collections.deque(maxlen=persistence_m)

    @property
    def n_calibration(self) -> int:
        return len(self._e_hist)

    def calibration_medians(self) -> tuple[float, float]:
        """(median energy, median line-length) over the current baseline window;
        snapshotted by the live detector at the end of calibration."""
        return float(np.median(self._e_hist)), float(np.median(self._ll_hist))

    def add_calibration(self, energy: float, ll: float) -> None:
        self._e_hist.append(energy)
        self._ll_hist.append(ll)
        # Warm the foreground median window so it is defined over the whole
        # feature stream (matches the vectorised replay; identity for L=1).
        self._e_fg.append(energy)
        self._ll_fg.append(ll)

    def step(self, second_idx: int, energy: float, ll: float):
        # Baseline from prior data (deque BEFORE adding the current value).
        e_base = float(np.median(self._e_hist))
        ll_base = float(np.median(self._ll_hist))
        self._e_hist.append(energy)
        self._ll_hist.append(ll)

        # Foreground median filter (L=1 → identity).
        self._e_fg.append(energy)
        self._ll_fg.append(ll)
        e_fg = float(np.median(self._e_fg))
        ll_fg = float(np.median(self._ll_fg))

        e_ratio = e_fg / max(e_base, EPS)
        ll_ratio = ll_fg / max(ll_base, EPS)

        # AND combination: both energy and line-length must exceed T. On the
        # chronic-mouse (.mat) data this cuts false positives ~30-75x vs the old
        # OR rule at equal sensitivity on the lab's chronic PPN data (2026-07).
        hot = e_ratio > self.threshold and ll_ratio > self.threshold
        self._hot_hist.append(hot)
        active = sum(self._hot_hist) >= self.persistence_k

        return self.run_tracker.update(second_idx, active)


# ── Vectorised primitives (used by replay + sweep) ───────────────────

def trailing_median(arr: np.ndarray, win: int) -> np.ndarray:
    """Causal trailing median: out[i] = median(arr[max(0, i-win+1) : i+1]).

    Truncated at the start of ``arr`` (window grows from 1 up to ``win``).
    """
    n = len(arr)
    if win <= 1 or n == 0:
        return arr.astype(np.float64, copy=True)
    out = np.empty(n, dtype=np.float64)
    # Edge region (window not yet full): expanding median.
    for i in range(min(win - 1, n)):
        out[i] = np.median(arr[: i + 1])
    if n >= win:
        swv = np.lib.stride_tricks.sliding_window_view(arr, win)  # (n-win+1, win)
        out[win - 1:] = np.median(swv, axis=1)
    return out


def prior_median(arr: np.ndarray, win: int) -> np.ndarray:
    """Baseline median of the *prior* ``win`` values:
    out[k] = median(arr[k-win : k]) for k >= win, NaN for k < win.
    """
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= win:
        return out
    swv = np.lib.stride_tricks.sliding_window_view(arr, win)  # rows j cover arr[j:j+win]
    med = np.median(swv, axis=1)                              # len n-win+1
    # out[k] = median(arr[k-win:k]) = med[k-win]; k in [win, n-1] → med[0 : n-win]
    out[win:] = med[: n - win]
    return out


def trailing_sum(arr: np.ndarray, win: int) -> np.ndarray:
    """Causal trailing sum: out[i] = sum(arr[max(0, i-win+1) : i+1]) (truncated)."""
    c = np.cumsum(arr, dtype=np.int64)
    out = c.copy()
    out[win:] = c[win:] - c[:-win]
    return out


def onsets_from_active(active: np.ndarray, gap_tol_s: int) -> list[int]:
    """Indices where an active run begins, merging runs separated by
    <= ``gap_tol_s`` inactive seconds.  Mirrors ``RunTracker`` with min_dur_s=1.
    """
    if gap_tol_s <= 0:
        # Rising edges of `active`.
        idx = np.flatnonzero(active & ~np.concatenate(([False], active[:-1])))
        return idx.tolist()

    onsets: list[int] = []
    in_run = False
    gap = 0
    for i, a in enumerate(active):
        if not in_run:
            if a:
                in_run = True
                gap = 0
                onsets.append(i)
        else:
            if a:
                gap = 0
            else:
                gap += 1
                if gap > gap_tol_s:
                    in_run = False
    return onsets


# ── Vectorised replay (whole arrays) ─────────────────────────────────

def replay(
    energy: np.ndarray,
    ll: np.ndarray,
    *,
    threshold: float,
    persistence_k: int,
    persistence_m: int,
    baseline_window: int,
    median_filter_len: int = 1,
    gap_tol_s: int = 0,
) -> list[int]:
    """Replay the energy/LL detector over feature arrays; return onset *seconds*.

    ``energy``/``ll`` are per-window features where array index ``i`` corresponds
    to detector ``second_idx = i + 1`` (the first chunk is buffer-only and
    produces no feature, exactly as the streaming detector does).  Returned
    onsets are those ``second_idx`` values (seconds into the recording).
    """
    e_base = prior_median(energy, baseline_window)
    ll_base = prior_median(ll, baseline_window)
    e_fg = trailing_median(energy, median_filter_len)
    ll_fg = trailing_median(ll, median_filter_len)

    return _onsets_from_precomputed(
        e_base, ll_base, e_fg, ll_fg,
        threshold=threshold,
        persistence_k=persistence_k,
        persistence_m=persistence_m,
        baseline_window=baseline_window,
        gap_tol_s=gap_tol_s,
    )


def _onsets_from_precomputed(
    e_base: np.ndarray,
    ll_base: np.ndarray,
    e_fg: np.ndarray,
    ll_fg: np.ndarray,
    *,
    threshold: float,
    persistence_k: int,
    persistence_m: int,
    baseline_window: int,
    gap_tol_s: int,
) -> list[int]:
    """Shared tail of :func:`replay` — also called by the sweep, which caches the
    four precomputed arrays per (stream, baseline, L) and only varies T/M/K here.

    The detection region is indices ``k >= baseline_window`` (where ``e_base`` is
    defined).  Foreground median accumulates only within this region, matching
    the streaming detector (its foreground deque is fed only during detection).
    """
    n = len(e_base)
    W = baseline_window
    if n <= W:
        return []

    k = np.arange(W, n)
    e_ratio = e_fg[k] / np.maximum(e_base[k], EPS)
    ll_ratio = ll_fg[k] / np.maximum(ll_base[k], EPS)
    hot = (e_ratio > threshold) & (ll_ratio > threshold)  # AND (see step())

    active = trailing_sum(hot.astype(np.int64), persistence_m) >= persistence_k
    det_onsets = onsets_from_active(active, gap_tol_s)

    # detection index d → array index k = W + d → second_idx = k + 1
    return [W + d + 1 for d in det_onsets]
