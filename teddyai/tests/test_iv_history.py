"""
Tests for build_iv_history.py.

The loader's job is to hand regime.vol_state a comparable distribution or hand
it nothing. Handing it a distribution measured on a superseded convention would
be worse than handing it none, because the percentile would look authoritative
and be ranked against the wrong ruler.
"""
import json

import pytest

import nqvol
import build_iv_history


def _doc(tmp_path, basis, vals):
    p = tmp_path / "iv_history.json"
    p.write_text(json.dumps({
        "generated": "2026-09-09T00:00:00+00:00",
        "iv_basis": basis,
        "hours": 6.5,
        "n": len(vals),
        "sessions": {f"2026-01-{i + 1:02d}": {"iv_atm": v} for i, v in enumerate(vals)},
    }), encoding="utf-8")
    return p


def test_a_missing_file_returns_an_empty_series():
    """vol_state must degrade to 'level only', not fail, when no history exists."""
    assert build_iv_history.load("does-not-exist.json") == []


def test_a_history_on_the_current_basis_loads():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = _doc(pathlib.Path(d), nqvol.IV_BASIS, [0.10, 0.20, 0.15])
        assert build_iv_history.load(p) == [0.10, 0.20, 0.15]


def test_a_history_on_a_superseded_basis_is_refused():
    """IV_BASIS is bumped whenever the calendar or the clock changes. A history
    solved on the old one is not comparable and must not be ranked against."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = _doc(pathlib.Path(d), "calendar", [0.10, 0.20, 0.15])
        assert build_iv_history.load(p) == []


def test_the_series_is_ordered_oldest_first():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "h.json"
        p.write_text(json.dumps({
            "iv_basis": nqvol.IV_BASIS,
            "sessions": {"2026-03-01": {"iv_atm": 0.30},
                         "2026-01-01": {"iv_atm": 0.10},
                         "2026-02-01": {"iv_atm": 0.20}},
        }), encoding="utf-8")
        assert build_iv_history.load(p) == [0.10, 0.20, 0.30]


@pytest.mark.parametrize("path", ["data/iv_history.json"])
def test_the_real_history_is_usable_if_it_has_been_built(root, path):
    """Not a requirement -- the file is generated, not committed logic -- but if
    it is present it must be on the current basis and long enough to rank."""
    p = root / path
    if not p.exists():
        pytest.skip("iv_history.json not built")
    series = build_iv_history.load(p)
    assert series, "history exists but was refused; check iv_basis"
    assert len(series) >= 20
    assert all(0.0 < v < 5.0 for v in series), "an implausible vol got into the history"
