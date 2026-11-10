> Background document from the Mattis Lab analysis repository, kept with the app for reference.
> File paths and dataset names in it (e.g. `offset_detector/…`, LC / DG / PPN recordings) refer to that
> internal repository and its data, which are not part of this package. The algorithm and parameter
> values described here are exactly what `seizure_marker/detector.py` implements.

# Detector overview

How the offline seizure detector finds when a seizure **starts** (onset) and when it
**ends** (offset) in an EEG/ECoG recording that has already been made.

_Last updated: 2026-10-17. Code: `offset_detector/offline_offsets.py`._

---

## In one paragraph

This is **not a trained model**. It is a fixed chain of simple signal-processing rules,
and every decision is a threshold you can read in the code. It takes one EEG channel,
cleans it, and summarises each second with a few numbers. A seizure **starts** when the
signal becomes much larger and much busier than it was over the previous minute. It
**ends** when the signal becomes quieter than it was just before the seizure began
(post-seizure, or *postictal*, suppression). Because it runs on finished recordings, it
may look ahead in time. That lets it merge repeated detections of the same seizure and
avoid stopping at a brief pause in the middle of one.

## The pipeline at a glance

```
recording ─► 1. load one channel ─► 2. filter ─► 3. per-second numbers ─┬─► 4. onset detector ─► "runs"
                                                                         │                          │
                                                                         └─► std ─► 5–6. offset rule ◄┘
                                                                                          │
                                                                              7. events table + plots
```

---

## Step by step

### 0. Start-up: choose a profile

You run one command (see *How to run*) and name a **profile**. A profile is the set of
parameters for one type of epilepsy or recording setup, because settings that work for
one type do not necessarily transfer to another. There are two today:

| profile | intended for | differs from the other by |
|---|---|---|
| `ppn` | long chronic recordings (PPN mice) | burst lookahead **off**, onset look-back **off** |
| `lc`  | short heat-induced seizure sessions (Scn1a, LC and DG rigs) | burst lookahead **on** (15 s), onset look-back **on**, baseline inheritance **on** |
| `chol` | Limei's cholinergic sessions (MS PV / MS ChAT / DG ACh; heating, HupA and PTZ) | as `lc` but rolling baseline **180 s** instead of 60 s |

`chol` exists because PTZ seizures ramp up over 1–3 min of growing spikes; a 60 s median
adapts into the ramp and the burst never looks 3× louder than its own run-up (5818 PTZ:
2 of 5 bursts found with 60 s, 5 of 5 with 180 s). On the 14 logged LC+DG seizures the
longer window finds one more (DG 4566, +25 s lag) with no extra events; lowering the
threshold instead added events without finding the PTZ bursts. Note that nothing can be
detected in the first 180 s of a `chol` recording.

Everything else (onset settings, offset thresholds, safety limit) is currently the same
in both. A new dataset gets a new profile rather than a change to an existing one.

### 1. Load one channel

Everything after this step works on a single 1-D signal: one number per sample.

- **`.mat` files:** takes the first variable in the file. Assumes 500 Hz unless `--fs`
  is given.
- **Open Ephys recordings:** reads `structure.oebin`, uses the lowest-numbered Record Node
  (later nodes can be filtered copies, not raw data), picks the amplifier stream, extracts
  the named channel (e.g. `CH8`) and converts it to µV. If the recording has an `ADC1`
  channel (the temperature probe on the heating rigs), its per-second average is kept for
  the plots only. It plays no part in detection.

You must choose the right channel. The detector cannot tell a good ECoG channel from a
noisy or disconnected one.

### 2. Filter

A 60 Hz notch (removes mains electrical noise) followed by a 1–70 Hz bandpass (removes slow
drift and high-frequency noise). These are the same filters the live detector uses, applied
to the whole recording at once.

### 3. Summarise each second

The filtered signal is cut into 1-second pieces. For each second the detector computes:

| number | what it measures | used by |
|---|---|---|
| **std** | how spread out the signal is in that second (≈ amplitude) | offset rule |
| **energy** | mean squared amplitude, over a 2 s window | onset detector |
| **line-length** | mean absolute change between neighbouring samples, over a 2 s window (≈ amplitude × frequency, i.e. "busyness") | onset detector |

The 2 s windows move forward 1 s at a time.

### 4. Onset detector: find candidate seizures

For every second from 60 s onward:

1. Divide energy and line-length by **their own median over the previous 60 s**. That
   median is a rolling "normal level", so the detector adapts to each recording and
   absolute signal size does not matter.
2. The second is **hot** if **both** ratios are above **3**. Requiring both cuts false
   alarms: a spike or a slow wave usually raises only one of them.
3. The second is **active** if at least **3 of the last 5** seconds were hot. This
   ignores one-off spikes.
4. A continuous stretch of active seconds is a **run** (start second, end second).
5. **Onset look-back** (`lc` profile, offline only, added 2026-10-17). Step 3 can only
   declare a run on its 3rd hot second, so a causal onset is always ~2 s late. Because
   the recording is finished, the run's start is moved back to the **first hot second of
   the cluster that triggered it**: the earliest hot second within the last 5 s, then
   further back through any directly preceding hot seconds. It never crosses into the
   previous run. Nothing else changes (the run itself, the merge rule and the offset
   search are the same); only the reported onset moves earlier. On the logged LC and DG
   seizures this took the onset lag from +2.3…+3.4 s to −2.2…+1.4 s (LC: +0.3…+1.0 s)
   with every offset unchanged.

Consequences worth knowing:
- **Nothing can be detected in the first 60 s**; the rolling median needs a full minute.
- **Without the look-back, detected onsets are ~2–3 s late**, because 3 hot seconds are
  needed. With it (`lc`), they sit within about ±1 s of the logged UEO; an onset a second
  or two *before* the logged UEO means the EEG was already hot before the reviewer's mark.
- **A seizure that builds slowly out of an already-busy background can be missed**,
  because the previous minute is then already "loud".

If the true onset is already known (e.g. hand-marked), the offset rule below can be run
from it directly, skipping this step.

### 5. Offset rule: find where each seizure ends

Take the first run not yet assigned to an event:

1. **Onset** = the run's start second.
2. **Pre-seizure baseline** = the median std from **75 s to 15 s before the onset**. The
   15 s gap keeps early seizure activity out of it. If fewer than 10 s of history exist,
   the event is flagged `no_baseline`.
3. **Search** from the later of (onset + 5 s) and (end of the run) for the first
   **3 consecutive seconds with std below 0.8 × baseline**: the brain has gone quieter
   than it was before the seizure. This is **tier 1**, and its first second is the offset.

### 6. Offline-only refinements

These use information *after* the moment being judged, which is why they only work on
recorded data.

- **Burst lookahead** (`lc` profile only). Some seizures end as a train of bursts with
  short pauses between them, and a pause can look like the end. So when a tier-1 quiet
  stretch is found, the rule checks the **next 15 s**. If the std rises above
  **1.5 × baseline for 2 s or more**, the seizure is still going: that quiet stretch is
  rejected and the search restarts after the burst. This repeats until a quiet stretch has
  no burst after it. The rejected stretches are recorded (the *Lookahead?* column in the
  review workbook).
- **Tier 2 fallback.** Some seizures are never followed by a quiet period. If tier 1
  never succeeds before the safety limit, the rule accepts **std below 1.0 × baseline for
  10 s** (back to normal rather than below normal).
- **Safety limit.** If neither tier ever succeeds, the offset is set **30 minutes after
  onset** (flagged `backstop`) or at the **end of the recording** (flagged
  `end_of_recording`). Treat these offsets as "unknown", not as real seizure ends.
- **Merging repeat detections.** The onset detector often fires several times during one
  seizure. Any later run that starts **before offset + 60 s** is treated as the same
  seizure: the search moves past that run and the offset is recomputed. The baseline stays
  anchored to the first onset. This repeats until no run falls inside the window.
- **Baseline inheritance** (`lc` profile, added 2026-10-17). A *separate* event that starts
  **within 300 s of the previous event's offset** reuses the previous event's pre-seizure
  baseline instead of measuring its own. Its own −75…−15 s window would lie in the previous
  seizure's postictal quiet, giving a baseline so low that the std can never fall below
  0.8× of it; on Limei's cholinergic sessions every backstop / end-of-recording offset had
  exactly this cause (postictal spikes re-triggering the onset detector 60–130 s after a
  seizure). Events that inherit are flagged (`baseline_inherited`, "baseline from previous
  event" in the workbook image title). `ppn` keeps this off (`inherit_s=0`).

Then the next unassigned run starts a new event, and steps 5–6 repeat until every run is used.

### 7. Output

| output | contents |
|---|---|
| `events.csv` | one row per event: onset, offset, duration, which rule ended it (`1`, `2`, `backstop`, `end_of_recording`), number of runs merged, baseline std |
| one PNG per event | filtered trace; per-second std with the baseline and 0.8× lines; temperature if available |
| review workbook (`export_detections_xlsx.py`) | one row per event with trace image, onset lag, offset error, Lookahead?, Tier? |

When a lab log is available, the detector is also scored against it:
**onset lag = detected onset − logged onset** and
**offset error = detected offset − logged offset**, in seconds. A positive value means
late, a negative value means early.

---

## All parameters in one place

| parameter | value | step | where it came from |
|---|---|---|---|
| filter | 60 Hz notch, 1–70 Hz bandpass | 2 | shared with the live detector |
| rolling baseline W | 60 s (`chol`: 180 s) | 4 | tuned on PPN mouse 3728 (11 seizures); 180 s chosen on 5818 PTZ + the 14 logged LC/DG seizures |
| threshold T | 3× | 4 | tuned on PPN mouse 3728 |
| persistence K of M | 3 of 5 s | 4 | tuned on PPN mouse 3728 |
| onset look-back | first hot second of the triggering cluster (`lc` only) | 4 | offline-only; verified on the 14 logged LC+DG seizures |
| pre-seizure baseline window | −75 to −15 s | 5 | design choice (not swept) |
| minimum duration | 5 s | 5 | design choice (not swept) |
| tier 1 | < 0.8× baseline for 3 s | 5 | swept on FCD (train/test split), checked against 30 hand-marked FCD offsets |
| tier 2 | < 1.0× baseline for 10 s | 6 | chosen from an FCD observation; not tuned |
| burst lookahead | 15 s, > 1.5× for 2 s | 6 | chosen after viewing 3 LC seizures; **not tested on held-out data** |
| merge window | 60 s | 6 | judgment call; not tuned |
| baseline inheritance window | 300 s (`lc` only) | 6 | judgment call (postictal quiet lasts a few minutes); not tuned |
| safety limit | 30 min | 6 | safety net only |

---

## How well it works (2026-09-28)

Scored against the lab's logged onset and offset (`lc` profile, unchanged settings):

| dataset | seizures | onsets found | onset lag (typical) | offsets within 2 s |
|---|---|---|---|---|
| LC (Scn1a heating) | 6 | 6/6 | +0.3 to +1.0 s (was +2 to +3 s before the look-back) | 5/6 (one −9 s) |
| DG (Scn1a heating) | 8 | 7/8 overlap the logged seizure | −2.2 to +1.4 s (6 sessions; was +1.2 to +3.4 s) | 4/6 of the cleanly detected |

- The LC offset numbers are optimistic: the burst lookahead was chosen by looking at 3 of
  those same 6 seizures.
- DG failures: one mouse (4566) was missed. One (3154) was detected, but merged into an
  event that began at earlier spikes. Two offsets came out 10–15 s late because of spikes
  after the real end.
- On PPN mouse 3728 there are no offset marks, so offsets there are unverified.

## Known ways it goes wrong

| situation | what happens |
|---|---|
| spikes or artifacts before the seizure | an early false onset chains into the real seizure → onset far too early |
| seizure builds out of an already-busy minute (e.g. during heating) | ratios never reach 3 → missed |
| only amplitude rises, not "busyness" (large slow waves) | line-length stays low → missed (both features are required) |
| isolated spikes after the real end | offset pushed late, or the lookahead thinks the seizure continued |
| pauses inside a seizure longer than 15 s | offset too early |
| quiet (e.g. sleep) before the seizure | 0.8× baseline is hard to reach → offset late or backstop |
| earlier seizure > 300 s before the onset, postictal quiet still in the baseline window | same as above (inheritance only reaches back 300 s) |
| artifact burst (handling, cable) with no quiet afterwards | false onset whose offset comes minutes later (tier 1 keeps searching; e.g. MS PV 3771 at 400 s) |
| no quiet period after the seizure | tier 2 or backstop |
| two seizures < 60 s apart | reported as one event (count lost) |
| seizure in the first minute / running past the end of the file | not detectable / offset = end of recording |
| wrong, flat, disconnected or clipped channel | false detections or none |

## How to run

Run from the repository root:

```bash
# one recording (.mat or Open Ephys folder)
.venv/bin/python offset_detector/offline_offsets.py run PATH --channel CH8 --profile lc

# all LC sessions in the lab log, scored against it
.venv/bin/python offset_detector/offline_offsets.py lc

# review workbook for LC + DG (trace images, lag/error, Lookahead?, Tier?)
.venv/bin/python offset_detector/export_detections_xlsx.py

# Limei's cholinergic recordings (folders 1/2/6 Scn1a-seizure only, 7/8 all) -> one sheet per mouse, profile chol
.venv/bin/python offset_detector/export_cholinergic_xlsx.py

# interactive review: detector places the cursors, the user accepts / drags / rejects / adds, then exports
.venv/bin/python offset_detector/marker_gui.py [FOLDER]        # see offset_detector/marker/README.md
```

## Using it on a new dataset

1. **Pick the channel.** Look at a whole-recording overview and use the channel where
   seizures are clearest. A lab log's ECoG channel is the starting point.
2. **Start from the closest existing profile.** Chronic long recordings → `ppn`; short
   induced-seizure sessions → `lc`.
3. **Check a handful of seizures against hand marks** before trusting any numbers. Look at
   the plots, not just the table.
4. **If it consistently fails, add a new profile**; don't edit an existing one, which
   would change results already reported for another dataset. Change the fewest
   parameters possible and re-check on seizures that were not used to choose them.
