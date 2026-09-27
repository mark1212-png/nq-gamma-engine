"""
The regression wall.

Every number the sheet prints, for four cached sessions, frozen at the state of
the engine on 2026-09-09 before the market-state work began. If a change to any
new module moves one of these, the test names WHICH number moved.

This is the test that lets the upgrade proceed one subsystem at a time. The new
layers (topology, node quality, regime, scenario) are all ADDITIVE: they read
the exposure engine's output and must not perturb it. That claim is only worth
anything if it is checked, and this checks it.

Runtime is ~25s for the four sessions because trinity_db.hvl() sweeps 1,001
repriced levels per panel. That is production behaviour and is deliberately not
shortened here; the fast unit tests live in the other files.
"""
import json
import hashlib

import pytest

import trinity_db
from conftest import have_chain, chain_path

PANELS = ("0DTE", "NEAR", "FULL")


def _gamma_hash(gmap):
    return hashlib.sha256(json.dumps(gmap, sort_keys=True).encode("utf-8")).hexdigest()


def _sessions(baseline):
    return sorted(baseline["sessions"])


@pytest.fixture(scope="module")
def computed(baseline):
    """Run the engine once per session for the whole module."""
    out = {}
    for s, rec in baseline["sessions"].items():
        if not have_chain(s):
            continue
        res, books, live = trinity_db.analyse(str(chain_path(s)), rec["report_date"],
                                              hours=6.5, quiet=True)
        out[s] = res
    return out


def _pairs(baseline, computed):
    for s, rec in sorted(baseline["sessions"].items()):
        if s not in computed:
            pytest.skip(f"no cached chain for {s}")
        yield s, rec, computed[s]


# --------------------------------------------------------------- scalars ---

@pytest.mark.parametrize("field", ["spot", "ref_symbol", "expiries", "no_0dte",
                                   "front_root", "front_expiry_hours"])
def test_scalar_fields_unchanged(baseline, computed, field):
    for s, rec, res in _pairs(baseline, computed):
        assert res[field] == rec[field], f"{s}: {field} moved {rec[field]!r} -> {res[field]!r}"


def test_iv_and_expected_move_unchanged(baseline, computed):
    for s, rec, res in _pairs(baseline, computed):
        # exact: these come from a deterministic solve, not a fit with tolerance
        assert res["iv_atm"] == rec["iv_atm"], f"{s}: ATM IV moved"
        assert res["em"] == rec["em"], f"{s}: expected move moved"


def test_vex_total_unchanged(baseline, computed):
    for s, rec, res in _pairs(baseline, computed):
        assert round(res["vex_total"], 6) == rec["vex_total"], f"{s}: net VEX moved"


# ---------------------------------------------------------------- panels ---

@pytest.mark.parametrize("panel", PANELS)
def test_panel_flip_unchanged(baseline, computed, panel):
    for s, rec, res in _pairs(baseline, computed):
        want = rec["panels"][panel]["hvl"]
        got = res["panels"][panel]["hvl"]
        assert got == want, f"{s} {panel}: flips moved {want} -> {got}"


@pytest.mark.parametrize("panel", PANELS)
def test_panel_net_total_unchanged(baseline, computed, panel):
    for s, rec, res in _pairs(baseline, computed):
        want = rec["panels"][panel]["net_total"]
        got = res["panels"][panel]["net_total"]
        assert got == want, f"{s} {panel}: column total moved {want} -> {got}"


@pytest.mark.parametrize("panel", PANELS)
def test_panel_gamma_map_unchanged(baseline, computed, panel):
    """Hash first; on failure, name the strikes that actually differ."""
    for s, rec, res in _pairs(baseline, computed):
        want_sha = rec["panels"][panel]["gamma_sha256"]
        gmap = res["panels"][panel]["gamma"]
        if _gamma_hash(gmap) == want_sha:
            continue
        assert len(gmap) == rec["panels"][panel]["n_strikes"], (
            f"{s} {panel}: strike COUNT moved {rec['panels'][panel]['n_strikes']} -> {len(gmap)}")
        pytest.fail(f"{s} {panel}: gamma map changed but strike count did not "
                    f"({len(gmap)} strikes) -- values moved. Run "
                    f"`python tests/make_baseline.py --check` for the full diff.")


# ------------------------------------------------------------- aggregates ---

def test_by_expiry_unchanged(baseline, computed):
    for s, rec, res in _pairs(baseline, computed):
        be = res["by_expiry"]
        assert len(be) == rec["by_expiry_n"], f"{s}: expiry count moved"
        assert sum(b["gamma"] for b in be) == rec["by_expiry_gamma_sum"], f"{s}: signed expiry gamma sum moved"
        assert sum(abs(b["gamma"]) for b in be) == rec["by_expiry_abs_gamma_sum"], f"{s}: gross expiry gamma sum moved"


def test_confluence_count_unchanged(baseline, computed):
    for s, rec, res in _pairs(baseline, computed):
        assert len(res["confluence"]) == rec["confluence_n"], f"{s}: confluence strike count moved"


# ---------------------------------------------------- invariants, not snapshots ---

def test_panel_books_are_nested_even_though_strike_keys_are_not(computed):
    """The BOOKS nest (dte==0 subset of dte<=9 subset of all). The published
    STRIKE KEYS do not, and assuming they did is a trap this test exists to
    document.

    trinity_db writes `gamma` as {strike: int(round(v))} filtered by `if
    round(val)`, so any strike whose NET across that panel rounds to zero is
    dropped. A strike can therefore be present in 0DTE and absent from NEAR by
    cancelling against a later expiry at the same strike. Measured on the
    2024-09-10 book: 18,590 is -1.27 on 0DTE and +0.11 on NEAR, so NEAR drops it.

    CONSEQUENCE FOR ANYTHING READING trinity.json: those maps are a DISPLAY
    product -- integer-rounded and zero-suppressed. A topology layer that reads
    them will see holes that are rounding artefacts and not pockets in the book.
    Read strike_metrics() or chain_live.json for structural work.
    """
    for s, res in computed.items():
        assert res["panels"]["0DTE"]["expiries"] <= res["panels"]["NEAR"]["expiries"]
        assert res["panels"]["NEAR"]["expiries"] <= res["panels"]["FULL"]["expiries"]


def test_published_gamma_maps_are_rounded_and_zero_suppressed(computed):
    """Locks the display convention in place: every published value is a nonzero
    integer. If this ever fails, something started publishing raw floats and the
    downstream consumers that assume integers need revisiting."""
    for s, res in computed.items():
        for p in PANELS:
            for k, v in res["panels"][p]["gamma"].items():
                assert isinstance(v, int), f"{s} {p} {k}: published gamma is not an int ({v!r})"
                assert v != 0, f"{s} {p} {k}: a zero was published; the filter should have dropped it"


def test_flip_is_nearest_crossing_first(computed):
    """Everything downstream reads hvl[0] as 'the flip'. It must be the crossing
    nearest the reference, which is what trinity_db sorts it to be."""
    for s, res in computed.items():
        spot = res["spot"]
        for p in PANELS:
            h = res["panels"][p]["hvl"]
            if len(h) > 1:
                assert abs(h[0] - spot) <= abs(h[1] - spot), f"{s} {p}: hvl not nearest-first"
