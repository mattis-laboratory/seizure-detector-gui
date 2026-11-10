# Changelog

## 1.0.0 — 2026-11-10

First stand-alone release, split out of the Mattis Lab analysis repository.

- Detector: energy + line-length onset detector (AND rule, K-of-M persistence,
  rolling-median baseline, onset look-back) and the postictal-suppression offset
  rule with burst-train lookahead, tier-2 fallback, re-fire merging, baseline
  inheritance and a flagged backstop. Presets `chol`, `lc`, `ppn`.
- GUI: zoomed-out overview with draggable zoom box, zoomed-in view with
  draggable ONSET / OFFSET cursors, one adaptive Accept/Save button, Reset to
  detector, Not a seizure, Add a missed seizure, presets and a sensitivity
  slider with an Advanced panel (raw numbers, sampling-rate override), autosave
  and resume, per-mouse Excel export with trace images and a Parameters sheet
  containing a generated methods paragraph.
- Fixed: switching to a shorter recording could clamp the previous event's
  cursors to the new file's end and write that into the new recording's first
  mark.
- Added: synthetic recording generator (`python -m seizure_marker.synthetic`),
  tests, double-click launchers for macOS and Windows.
