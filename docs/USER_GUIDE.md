# Seizure On/Off Marker — user guide

Interactive review of automatically detected seizure onset and offset times in
Open Ephys EEG recordings. The detector places two cursors; you accept them,
drag them, reject the event, or add one it missed; the approved times go to an
Excel workbook (one sheet per mouse, plus a Parameters tab) ready for the
analysis pipeline.

## Install and run

See the top-level [README](../README.md): double-click `launchers/setup_mac.command`
or `launchers\setup_windows.bat` once, then `Seizure Marker.command` / `.bat`
(or `python -m seizure_marker`). Command-line options:

```bash
python -m seizure_marker                                  # then Open recording(s)… or drag a folder in
python -m seizure_marker "D:/EEG/6 DG ACH hot seizure"    # whole experiment folder: every recording is queued
python -m seizure_marker PATH --channel CH9 --profile lc   # force a channel / preset
python -m seizure_marker PATH --out ~/Desktop/marks        # where the autosave and exports go
```

The ECoG channel for each recording is read from the lab log spreadsheet
(`*log*.xlsx`) found in a folder above the recording, matched by mouse number and
date (see *Data layout* in the README). Without a log it defaults to CH2; change it
in the toolbar at any time. The sampling rate is read from the recording's
metadata (`structure.oebin`).

## The window, top to bottom

| element | what it does |
|---|---|
| **Open recording(s)… / Recording / Channel** | queue of recordings (✓ = every event decided, ⚠ = no usable EEG); the channel being analysed |
| **Preset** | a named set of detector settings. Built-in: `chol` (cholinergic MS/DG hyperthermia + PTZ, default), `lc` (LC / DG GRAB-NE hyperthermia), `ppn` (chronic PPN). **Save preset…** stores the current sensitivity and advanced numbers under your own name, on your computer (`~/.seizure_marker_presets.json`), so you can pick it again next time. |
| **Sensitivity** | one slider from *less* to *more*. It scales the onset threshold (Least = 1.6×, Low = 1.3×, Normal = 1.0×, High = 0.8×, Most = 0.65×). More sensitive = more candidate events for you to look at (fewer misses, more to reject). Everything is still reviewed by you. |
| **Advanced ▾** | the individual numbers behind the detector (hover a label for its meaning) and a sampling-rate override for recordings whose metadata lacks it. **Apply & re-run** re-detects the current recording. |
| **Zoomed-out plot** | the whole recording with every event shaded (blue = not yet reviewed, green = accepted, blue-grey = edited, purple = added, grey = rejected). The yellow box is the zoom window: drag it, or click anywhere to move it. |
| **Zoomed-in plot** | drag the green ONSET or orange OFFSET cursor. Mouse wheel zooms, drag the background to pan. ← / → nudge the active cursor by 0.1 s (Shift: 1 s); Tab switches the active cursor. |
| **✓ Accept / Save** (Enter) | the one button that saves the event. Its label tells you what it will do: *Accept detector's marks* when you have not touched the cursors (Source = *detector*), *Save my adjusted marks* after you dragged them (Source = *edited*), *Save this seizure* for an event you added by hand (Source = *added*). |
| **↺ Reset to detector** (R) | undo your dragging: cursors go back to where the detector put them. Nothing is saved until you press Accept. |
| **✗ Not a seizure** (Delete) | the event is not a seizure. It stays in the table greyed out as *rejected* so you can see it was reviewed. |
| **+ Add a missed seizure** | the detector missed one: scroll the view onto it, press this, drag the new cursors onto the seizure, then Save. |
| **Prev / Next event** | step through events, then through recordings. The bold line above the buttons always says what to do for the event on screen. After every decision the next undecided event is shown automatically. Enter = accept/save, Delete = reject, R = reset, N / P = next / previous. |
| **Table** | every decision so far. Click a row to jump to it; double-click **Note** to annotate (e.g. "two seizures merged"). |
| **Save image(s)…** | PNGs of the current event, or of the whole recording and all its committed events. |
| **Export to Excel** | per-mouse sheets: Dataset, Mouse, Channel, Date, ONSET/OFFSET sample number, ONSET/OFFSET time (s), trace image, Source, Note; plus a **Parameters** sheet (below). |

Times are seconds from the start of the recording (recording = 0 s); sample
numbers are time × sampling rate. Every decision is saved immediately to
`<out>/marker_session.json` (default `~/SeizureMarker/`), so
closing the window loses nothing; re-opening the same folders restores progress.

Changing the preset, the sensitivity, the channel or an advanced number re-runs
the detector on the current recording; if you had already decided events there,
it asks first, because those decisions are discarded.

## What the Parameters sheet contains

For every exported recording: mouse, date, recording name, channel, sampling
rate, preset, sensitivity, effective onset threshold and which parameter set it
used. Then every parameter of each set with a one-line definition (the table
below), and a methods-section paragraph generated from those exact values.
Keep that sheet with the data: it answers "what did I use?" later.

## How the detector works

1. **Filter.** 60 Hz notch (Q = 30) followed by a 1–70 Hz 4th-order Butterworth band-pass. You are looking at the filtered trace.
2. **Per-second features.** Energy (mean squared amplitude) and line length
   (mean absolute sample-to-sample difference, ≈ amplitude × frequency) over 2 s
   windows stepped 1 s; per-second standard deviation for the offset rule.
3. **Onset.** Each second's energy and line length are divided by their medians
   over the preceding rolling-baseline window. A second is *hot* when **both**
   ratios exceed the threshold; an onset is declared when K of the last M
   seconds are hot, then moved back to the first hot second of that cluster.
4. **Offset.** The first run of K consecutive seconds whose standard deviation
   is below *frac* × the pre-seizure baseline (median std over the baseline span
   before onset). A quiet stretch followed quickly by another burst is treated as
   a pause inside the seizure. If the signal never goes quiet, a looser fallback
   is used; if that fails too, the event is closed at the backstop and flagged.
5. **Merging.** Re-detections within the merge window belong to the same event;
   an event starting soon after a previous one reuses that event's baseline (its
   own baseline window would sit in the postictal quiet).

### Every parameter (default preset `chol`)

| parameter | key | value | meaning |
|---|---|---|---|
| Onset threshold (×) | `threshold` | 3.0 | Both energy and line-length must exceed this multiple of their own rolling baseline for a second to count as 'hot'. Lower = more sensitive. The sensitivity slider multiplies this value. |
| Effective onset threshold (×) | `threshold_effective` | 3.0 | Onset threshold × sensitivity factor — the value actually used. |
| Hot seconds needed (K) | `persistence_k` | 3 | A candidate onset is declared once K of the last M seconds are hot. |
| Window for K (M, s) | `persistence_m` | 5 | Length of the window the K hot seconds must fall in. |
| Rolling baseline (s) | `baseline_window` | 180 | Energy and line-length are divided by their median over this many preceding seconds. Longer windows do not adapt into a slow pre-seizure build-up (PTZ); nothing can be detected in the first window of a recording. |
| Onset look-back | `onset_refine` | True | Offline only: move the onset from the K-th hot second back to the first hot second of the cluster that triggered it. |
| Offset: below × baseline | `offset.frac` | 0.8 | The seizure ends when the per-second standard deviation drops below this multiple of the pre-seizure baseline (median std over the baseline span before onset). |
| … for (s) | `offset.k` | 3 | Consecutive seconds below the offset level needed to call the offset. |
| Minimum duration (s) | `offset.min_s` | 5 | The offset search never starts earlier than this after onset. |
| Baseline span (s) | `offset.baseline_span` | -75 … -15 | Pre-seizure window, relative to onset, over which the baseline std is measured. |
| Fallback: below × baseline | `offset.tier2_frac` | 1.0 | If no postictal suppression is found, accept the std returning to this multiple of baseline … |
| … for (s) | `offset.tier2_k` | 10 | … for this many consecutive seconds. |
| Merge re-detections within (s) | `offset.merge_s` | 60 | A new detection starting within this many seconds of an offset is treated as the same seizure (offset search restarts after it). |
| Burst lookahead (s) | `offset.confirm_s` | 15 | A quiet stretch is not the end if a burst follows within this many seconds (0 = off). Handles seizures that end as a train of bursts. |
| … burst above × baseline | `offset.resume_frac` | 1.5 | Std level that counts as a burst for the lookahead. |
| … burst length (s) | `offset.resume_k` | 2 | Consecutive seconds above the burst level that count as a burst. |
| Reuse previous baseline within (s) | `offset.inherit_s` | 300 | An event starting within this many seconds of the previous event's offset reuses that event's pre-seizure baseline, because its own baseline window would lie in the postictal quiet (0 = off). |
| Give up after (s) | `offset.backstop_s` | 1800 | If no offset is found, the event is closed this long after onset and flagged. |
| Sensitivity | `sensitivity` | 0 | Slider position (−2 … +2); scales the onset threshold by 1.6× (Least), 1.3× (Low), 1.0× (Normal), 0.8× (High), 0.65× (Most). |

Longer explanation, with how each value was chosen and the known failure modes:
[DETECTOR_DETAILS.md](DETECTOR_DETAILS.md).

## Sample methods text (default preset `chol`, 1000 Hz)

> Seizure onset and offset times were determined with a semi-automated detector followed by manual review. The ECoG channel, sampled at 1000 Hz, was 60 Hz notch (Q = 30) followed by a 1–70 Hz 4th-order Butterworth band-pass. Signal energy (mean squared amplitude) and line length (mean absolute sample-to-sample difference) were computed in 2 s windows advanced in 1 s steps and expressed as ratios to their medians over the preceding 180 s. A second was flagged when both ratios exceeded 3 (sensitivity setting: normal), and a candidate onset was declared when at least 3 of the last 5 s were flagged; the onset was then moved back to the first flagged second of that cluster. The offset was defined as the first of 3 consecutive seconds in which the per-second standard deviation fell below 0.8× the pre-ictal baseline (median per-second standard deviation from 75 to 15 s before onset), searched from 5 s after onset; a quiet stretch followed within 15 s by at least 2 s above 1.5× baseline was treated as a pause within the seizure. When no such suppression occurred, 10 consecutive seconds below 1× baseline were accepted instead, and events with no identifiable offset were closed 30 min after onset and flagged. Detections re-starting within 60 s of an offset were merged into a single event, and an event beginning within 300 s of a previous event's offset reused that event's pre-ictal baseline. Candidate onsets and offsets were then displayed with the filtered trace in an interactive viewer in which a reviewer accepted, adjusted, rejected or added events; the reviewer's final times were used for all subsequent analyses.

The Parameters sheet of every export contains this paragraph regenerated from
the settings actually used, so copy it from there rather than from here.
