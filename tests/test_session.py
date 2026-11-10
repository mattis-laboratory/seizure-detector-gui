from pathlib import Path

from seizure_marker.session import Session, discover_recordings, make_recording, run_detector


def test_discover_from_parent_folder(synthetic_rec):
    found = discover_recordings([synthetic_rec.parent])
    assert found == [synthetic_rec.resolve()]


def test_metadata_from_folder_name(synthetic_rec):
    rec = make_recording(synthetic_rec)
    assert rec.mouse == "0001"
    assert rec.date == "20260115"
    assert rec.channel == "CH2"          # no lab log -> default channel


def test_detector_marks_and_autosave_roundtrip(synthetic_rec, tmp_path, expected_seizures):
    s = Session(tmp_path)
    s.add_paths([synthetic_rec])
    rec = s.recordings[0]
    marks = run_detector(rec)
    assert len(marks) == len(expected_seizures)
    marks[0].status = "auto"
    marks[1].onset -= 1.0
    marks[1].status = "edited"
    s.autosave()
    assert (tmp_path / "marker_session.json").exists()

    s2 = Session(tmp_path)
    s2.add_paths([synthetic_rec])          # restore() runs inside add_paths
    r2 = s2.recordings[0]
    assert [m.status for m in r2.marks] == ["auto", "edited"]
    assert r2.marks[1].onset == marks[1].onset
    assert s2.progress() == (1, 1)
