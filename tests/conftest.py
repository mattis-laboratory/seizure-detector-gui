import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")      # GUI tests run headless
os.environ.setdefault("MPLBACKEND", "Agg")

from seizure_marker.synthetic import DEFAULT_SEIZURES, write_recording  # noqa: E402


@pytest.fixture(scope="session")
def synthetic_rec(tmp_path_factory):
    """One synthetic Open Ephys recording (20 min, two fake seizures on CH2)."""
    out = tmp_path_factory.mktemp("synthetic")
    return write_recording(out, mouse="0001", minutes=20)


@pytest.fixture(scope="session")
def expected_seizures():
    return DEFAULT_SEIZURES
