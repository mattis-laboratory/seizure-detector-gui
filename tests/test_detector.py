from pathlib import Path

from seizure_marker import detector as oo


def test_load_openephys(synthetic_rec):
    sig, fs, info = oo.load(synthetic_rec, "CH2", None)
    assert fs == 1000.0
    assert info["channel"] == "CH2"
    assert len(sig) == 20 * 60 * 1000


def test_channel_lookup_accepts_numbers(synthetic_rec):
    _, _, info = oo.load(synthetic_rec, 2, None)
    assert info["channel"] == "CH2"


def test_detects_both_synthetic_seizures(synthetic_rec, expected_seizures):
    profile = dict(oo.PROFILES["chol"])
    sig, fs, _ = oo.load(synthetic_rec, "CH2", None)
    _, std, runs, events = oo.analyze(sig, fs, profile, oo.rule_for(profile))
    assert len(events) == len(expected_seizures), [(e.onset_s, e.offset_s, e.tier) for e in events]
    for ev, (onset, dur) in zip(events, expected_seizures):
        assert abs(ev.onset_s - onset) <= 3, (ev.onset_s, onset)
        assert abs(ev.offset_s - (onset + dur)) <= 10, (ev.offset_s, onset + dur)
        assert ev.tier == "1"


def test_quiet_channel_has_no_events(synthetic_rec):
    profile = dict(oo.PROFILES["chol"])
    sig, fs, _ = oo.load(synthetic_rec, "CH1", None)
    _, _, _, events = oo.analyze(sig, fs, profile, oo.rule_for(profile))
    assert events == []


def test_cli_writes_csv_and_png(synthetic_rec, tmp_path):
    import sys
    argv = sys.argv
    sys.argv = ["detector", "run", str(synthetic_rec), "--channel", "CH2", "--out", str(tmp_path)]
    try:
        oo.main()
    finally:
        sys.argv = argv
    out = next(Path(tmp_path).iterdir())
    assert (out / "events.csv").exists()
    assert len(list(out.glob("event_*.png"))) == 2
