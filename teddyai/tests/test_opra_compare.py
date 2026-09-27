"""
opra_compare.nq_reference -- which NQ contract the compare (and so the QQQ-per-NQ
factor every derived equity price is multiplied by) is struck on.

2026-09-16: left to trinity_db's default the NQ book referenced the front option
book's underlying, NQU6 in roll week, while the tape, the bars and the sheet were
on NQZ6. The factor has to follow the bar feed's contract when the chain can
price it, and fall back honestly when it cannot.
"""
import pytest

import opra_compare

FUT = {"NQU6": 28955.0, "NQZ6": 29246.75, "NQH7": 29588.5, "NQU6-NQZ6": 291.75}
RAW = {"futures_settle": FUT}


def test_pinned_contract_wins_when_the_chain_settles_it():
    ref, how = opra_compare.nq_reference(RAW, None, pinned="NQZ6")
    assert ref == "NQZ6"
    assert "bar feed" in how


def test_continuous_symbol_falls_back_to_the_front_book():
    ref, how = opra_compare.nq_reference(RAW, None, pinned="NQ.c.0")
    assert ref is None
    assert "front book" in how


def test_a_pin_the_chain_cannot_price_falls_back():
    # a backtest chain from another year does not settle this quarter's contract
    ref, how = opra_compare.nq_reference({"futures_settle": {"NQU4": 19000.0}}, None, pinned="NQZ6")
    assert ref is None
    assert "front book" in how


def test_no_pin_at_all_falls_back():
    ref, how = opra_compare.nq_reference(RAW, None, pinned=None)
    assert ref is None


def test_explicit_ref_is_honoured_and_named():
    ref, how = opra_compare.nq_reference(RAW, "NQH7", pinned="NQZ6")
    assert (ref, how) == ("NQH7", "--ref")


def test_explicit_ref_the_chain_cannot_price_is_refused():
    # trinity_db.load would keep the symbol and quietly substitute the front forward,
    # and the mapping would then be labelled with a contract it was not struck on
    with pytest.raises(SystemExit) as ex:
        opra_compare.nq_reference(RAW, "NQZ9", pinned=None)
    assert "NQZ9" in str(ex.value)
    assert "NQZ6" in str(ex.value)          # the outrights it does settle are listed
    assert "NQU6-NQZ6" not in str(ex.value)  # spreads are not offered as references


def test_default_pin_is_the_bar_feed_symbol():
    # the one place the roll is decided: live_feed.SYMBOL, imported at module load
    assert opra_compare.BAR_SYMBOL is None or isinstance(opra_compare.BAR_SYMBOL, str)


# ---------------------------------------------------------------- the compare's freshness

def _autopull(tmp_path, monkeypatch):
    import autopull
    monkeypatch.setattr(autopull, "DATA", tmp_path)
    (tmp_path / "opra").mkdir(parents=True, exist_ok=True)
    return autopull


def _chains(tmp_path, date="2026-09-15"):
    for r in ("SPX", "SPXW", "QQQ", "SPY"):
        (tmp_path / "opra" / f"{date}-{r}-chain.json").write_text("{}", encoding="utf-8")


def test_no_nq_chain_is_a_reason_not_silence(tmp_path, monkeypatch):
    """The 16:10 PT case: four equity chains, no NQ book until 22:15. Reporting this as
    'current' is how a settle spent a whole night with no compare on disk."""
    ap = _autopull(tmp_path, monkeypatch)
    _chains(tmp_path)
    why = ap._opra_compare_needed("2026-09-15")
    assert why and "NQ chain" in why


def test_missing_compare_with_an_nq_chain_asks_for_one(tmp_path, monkeypatch):
    ap = _autopull(tmp_path, monkeypatch)
    _chains(tmp_path)
    (tmp_path / "2026-09-15-chain.json").write_bytes(b"x" * 600_000)
    assert ap._opra_compare_needed("2026-09-15") == "no compare on disk"


def test_an_equity_chain_refetched_after_the_compare_reopens_it(tmp_path, monkeypatch):
    """opra-live refetches a root for its open interest; a compare older than that
    root was built on the previous vintage."""
    import os, time
    ap = _autopull(tmp_path, monkeypatch)
    _chains(tmp_path)
    (tmp_path / "2026-09-15-chain.json").write_bytes(b"x" * 600_000)
    cmp_ = tmp_path / "opra" / "2026-09-15-compare.json"
    cmp_.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    for f in list((tmp_path / "opra").glob("2026-09-15-*-chain.json")) + [cmp_, tmp_path / "2026-09-15-chain.json"]:
        os.utime(f, (old, old))
    assert ap._opra_compare_needed("2026-09-15") is None
    qqq = tmp_path / "opra" / "2026-09-15-QQQ-chain.json"
    os.utime(qqq, None)                                   # the opra-live refetch
    why = ap._opra_compare_needed("2026-09-15")
    assert why and "QQQ" in why


def test_a_settle_only_nq_file_does_not_count_as_a_chain(tmp_path, monkeypatch):
    ap = _autopull(tmp_path, monkeypatch)
    _chains(tmp_path)
    (tmp_path / "2026-09-15-chain.json").write_bytes(b"x" * 1000)   # a settle-only stub
    assert ap._opra_nq_chain("2026-09-15") is None
