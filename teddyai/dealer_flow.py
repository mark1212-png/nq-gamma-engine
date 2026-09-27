#!/usr/bin/env python3
"""
dealer_flow.py -- which side of each contract dealers hold, measured from the tape
instead of assumed.

    import dealer_flow
    net, unplaced = dealer_flow.classify_prints(prints)       # customer contracts, signed, per contract
    positions = dealer_flow.reconcile(oi_prev, oi_now, net, inventory, asof, is_call)
    dealer_flow.apply(books, positions)                         # stamps rec["pos"] on the loaded books
    python dealer_flow.py --selftest

WHY (2026-09-12). METHOD.md section 9: "Dealer sign convention (long calls / short
puts) is an assumption about positioning, not an observation. Every sign on the
sheet inverts if it is wrong." Until today nothing in this project measured it.
Nicky's engine (Nicky24B/nq-pma-nicky, db_snapshot.py --flow, since 2026-09-07)
does, and this is that method written again here, with one change, below.

THE METHOD
  1. Every print is classified by aggressor. Lifted the ask: the customer bought,
     the dealer sold. Hit the bid: the customer sold, the dealer bought. A `side`
     field (B / A) is used when the feed carries one; otherwise the print is
     placed against the quote at that instant; a print INSIDE the spread takes
     the side of the last price change in that contract (Lee-Ready's tie-break);
     the first print of a contract, with no tick before it, stays unplaced and
     is counted rather than guessed.
     "Aggressor = customer" is itself an assumption -- dealers hedging into a
     move are aggressive too -- and every record carries it as `assumption`.
  2. The session's net customer flow is reconciled against the CHANGE in open
     interest: the direction comes from the flow, the size from the OI change,
     so classification noise cannot accumulate into a position that never
     existed.
  3. A running inventory per contract. A contract first seen with open interest
     already on it starts from the convention (`convention-init`) and is
     corrected by flow from then on; one that lists inside our own history
     starts from zero and is flow all the way.

THE ONE CHANGE FROM THE SOURCE. Nicky's rule assigns the ENTIRE OI change to the
flow's side whenever the net flow is non-zero: one net contract of customer
buying moves 500 new contracts to dealer-short. Here the share assigned by flow
is min(1, |net flow| / |delta OI|), the remainder follows the convention, and
the record carries `flow_share` -- the fraction of the contract's open interest
whose sign was measured rather than assumed -- so a reader can tell the two
apart. With every |net flow| >= |delta OI| the two rules agree exactly.

WHAT THIS IS NOT. It is not wired into the pull. Trade prints for NQ options are
a separate Databento leg (GLBX.MDP3 `tbbo` on the option roots) this project has
never fetched, and a fetch that can bill is a decision, not a default. Until a
caller has run this and stamped the books, engine.py reports
`sign_convention.source = "convention"` and carries the sensitivity block that
says which conclusions survive if the convention is simply wrong.

FOR THE RECORD, what the source method had produced by 2026-09-10 on Nicky's
inventory (3,899 NQ contracts, three sessions of flow on a convention seed):
19.4% of contracts carried a flow sign opposite to the convention, concentrated
in tiny lines (23% under 10 contracts, 8% at 100-1,000 contracts), and 4 of the
31 lines over 1,000 contracts -- one of them the second-largest call on the
board, QN2U6 30,250C, 2,666 open, read as dealers SHORT 2,504. Three sessions on
a seeded book is not a measurement of the convention; it is the reason to build
one.

UNITS. Contracts. Positive = dealers LONG the contract, negative = dealers SHORT:
the sign trinity_db.net_position() consumes.
"""

import json
import argparse

ASSUMPTION = ("aggressor = customer: the passive side of every print is taken to be the dealer, "
              "so a lifted ask makes dealers shorter and a hit bid makes them longer")


# ------------------------------------------------------------ classify ---

def classify_prints(prints):
    """
    prints  time-ordered iterable of dicts: `sym`, `price`, `size`, and optionally
            `side` ('B' = buy aggressor, 'A' = sell aggressor), `bid`, `ask`.
    Returns ({sym: net CUSTOMER contracts, buy +, sell -}, n_unplaced).
    """
    net, last_px, last_tick, unplaced = {}, {}, {}, 0
    for p in prints:
        s = p["sym"]
        px = float(p["price"])
        sz = float(p.get("size") or 0)
        side = str(p.get("side") or "").upper()
        sgn = 1.0 if side == "B" else -1.0 if side == "A" else 0.0
        if sgn == 0.0 and p.get("bid") is not None and p.get("ask") is not None:
            if px >= float(p["ask"]):
                sgn = 1.0
            elif px <= float(p["bid"]):
                sgn = -1.0
        prev = last_px.get(s)
        if sgn == 0.0:
            # THE TICK RULE, for a print the quote cannot place. Measured by the
            # source on 2026-09-03: a quarter of QQQ and SPY prints and three in
            # four SPXW prints sit inside the consolidated spread.
            if prev is None:
                sgn = 0.0
            elif px > prev:
                sgn = 1.0
            elif px < prev:
                sgn = -1.0
            else:
                sgn = last_tick.get(s, 0.0)
        if prev is not None and px != prev:
            last_tick[s] = 1.0 if px > prev else -1.0
        last_px[s] = px
        if sgn == 0.0 or sz == 0.0:
            unplaced += 1
            continue
        net[s] = net.get(s, 0.0) + sgn * sz
    return net, unplaced


# ----------------------------------------------------------- reconcile ---

def _default_is_call(sym):
    """'QN2U6 C30250' -> True. The GLBX raw_symbol shape: root, space, type, strike."""
    parts = str(sym).split()
    return bool(parts) and parts[-1][:1].upper() == "C"


def reconcile(oi_prev, oi_now, net_customer, inventory=None, asof=None, is_call=None,
              dealer_long_calls=True):
    """
    oi_prev, oi_now   {sym: contracts} at two consecutive open-interest publications
    net_customer      {sym: signed customer contracts} from classify_prints, for the
                      session between them
    inventory         the previous call's result, or None the first time
    asof              the OI date being folded in (ISO); a record already at or past
                      it is not applied twice -- a re-run or a holiday sees the same
                      pair again
    is_call           sym -> bool for the convention seed; default parses the symbol

    Returns {sym: record}. Every record carries the assumption in words.
    """
    is_call = is_call or _default_is_call
    inv = inventory or {}
    asof = str(asof) if asof is not None else None
    out = {}
    for s, o1 in oi_now.items():
        o1 = int(o1)
        if o1 <= 0:
            continue
        o0 = int(oi_prev.get(s, 0) or 0)
        d = o1 - o0
        f = float(net_customer.get(s, 0.0) or 0.0)
        conv = (1.0 if is_call(s) else -1.0) * (1.0 if dealer_long_calls else -1.0)
        rec = inv.get(s)
        if rec is None:
            pos, meas = conv * o0, 0.0
            src = "convention-init" if o0 else "flow"
        else:
            pos, meas = float(rec.get("pos", 0.0)), float(rec.get("measured_contracts", 0.0))
            src = str(rec.get("source", "flow"))
        already = rec is not None and asof is not None and str(rec.get("asof", "")) >= asof
        if d != 0 and not already:
            if f != 0.0:
                share = min(1.0, abs(f) / abs(d))
                # customers bought -> dealers shorter; the measured share of the
                # change goes the flow's way, the rest follows the convention
                pos += -(1.0 if f > 0 else -1.0) * share * abs(d) + conv * (1.0 - share) * d
                meas += share * abs(d)
                src = "flow"
            else:
                pos += conv * d
                src = "flow+fill" if src.startswith("flow") else "convention-fill"
        pos = max(-o1, min(o1, pos))               # never more than the contracts that exist
        meas = max(0.0, min(float(o1), meas))
        out[s] = {"pos": int(round(pos)), "oi": o1, "oi_prev": o0, "delta_oi": d,
                  "net_customer": int(round(f)),
                  "measured_contracts": int(round(meas)),
                  "flow_share": round(meas / o1, 3) if o1 else 0.0,
                  "source": src, "asof": asof, "assumption": ASSUMPTION}
    return out


# --------------------------------------------------------------- apply ---

def apply(books, positions, symbol=None):
    """
    Stamp `rec["pos"] = {"dealer": signed contracts, ...}` onto the LOADED books,
    in memory only. trinity_db.net_position() reads it in preference to the
    convention; chain files on disk are never written.

    `symbol(book, strike, cp)` names a contract the way `positions` keys it;
    the default is the GLBX raw_symbol shape, e.g. "QN2U6 C30250". A strike
    with only one leg measured keeps the convention on the other leg.
    Returns (strikes stamped, open interest covered).
    """
    symbol = symbol or (lambda b, K, cp: f"{b['root']} {cp}{K:g}")
    n, covered = 0, 0
    for b in books:
        for ks, rec in b["strikes"].items():
            K = float(ks)
            legs, tot, src, meas, oi = {}, 0.0, set(), 0.0, 0
            for cp in ("C", "P"):
                r = positions.get(symbol(b, K, cp))
                if r is None:
                    continue
                legs[cp] = r["pos"]
                tot += r["pos"]
                src.add(r["source"])
                meas += r.get("measured_contracts", 0)
                oi += r.get("oi", 0)
            if not legs:
                continue
            for cp in ("C", "P"):
                if cp not in legs:
                    o = (rec.get(cp) or {}).get("oi", 0) or 0
                    tot += o if cp == "C" else -o
                    oi += o
            rec["pos"] = {"dealer": tot, "legs": legs, "source": sorted(src),
                          "flow_share": round(meas / oi, 3) if oi else 0.0}
            n += 1
            covered += oi
    return n, covered


def summary(positions, is_call=None):
    """What a reader needs to know about a stamped inventory, in one block."""
    is_call = is_call or _default_is_call
    by_src, oi_by_src, opp, n, meas, tot_oi = {}, {}, 0, 0, 0.0, 0
    for s, r in positions.items():
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        oi_by_src[r["source"]] = oi_by_src.get(r["source"], 0) + r["oi"]
        conv = r["oi"] if is_call(s) else -r["oi"]
        n += 1
        tot_oi += r["oi"]
        meas += r.get("measured_contracts", 0)
        if r["pos"] * conv < 0:
            opp += 1
    return {"contracts": n, "by_source": by_src, "oi_by_source": oi_by_src,
            "sign_opposite_to_convention": opp,
            "share_opposite": round(opp / n, 4) if n else None,
            "oi_share_measured": round(meas / tot_oi, 4) if tot_oi else None,
            "assumption": ASSUMPTION}


# ------------------------------------------------------------- selftest ---

def selftest():
    # 1. classification: side field, quote placement, tick rule, unplaced first print
    prints = [
        {"sym": "X C100", "price": 5.0, "size": 10, "side": "B"},
        {"sym": "X C100", "price": 4.9, "size": 3, "bid": 4.8, "ask": 5.0},      # inside: tick down -> sell
        {"sym": "X C100", "price": 5.0, "size": 2, "bid": 4.8, "ask": 5.0},      # at the ask -> buy
        {"sym": "X C100", "price": 5.0, "size": 4, "bid": 4.9, "ask": 5.1},      # inside, unchanged -> last tick (up)
        {"sym": "X P90", "price": 2.0, "size": 7},                                # first print, no quote: unplaced
        {"sym": "X P90", "price": 1.9, "size": 5},                                # tick down -> sell
    ]
    net, unplaced = classify_prints(prints)
    assert net["X C100"] == 10 - 3 + 2 + 4, net
    assert net["X P90"] == -5, net
    assert unplaced == 1, unplaced

    # 2. reconcile: seed, flow direction, the share rule, fill, clamp, idempotence
    oi0 = {"X C100": 100, "X P90": 0}
    oi1 = {"X C100": 130, "X P90": 40, "X C110": 20}
    pos = reconcile(oi0, oi1, {"X C100": 5.0, "X P90": -40.0}, None, "2026-09-11")
    c = pos["X C100"]
    assert c["source"] == "flow" and c["oi_prev"] == 100 and c["delta_oi"] == 30
    # +100 convention seed; 30 new: 5 measured (customers bought -> dealers shorter), 25 by convention (+)
    assert c["pos"] == 100 - 5 + 25, c
    assert c["measured_contracts"] == 5 and abs(c["flow_share"] - 5 / 130) < 1e-3, c
    p = pos["X P90"]
    assert p["source"] == "flow" and p["pos"] == 40, p    # customers sold 40 puts -> dealers long 40
    assert p["flow_share"] == 1.0
    f = pos["X C110"]
    # listed inside our history (seed "flow", from zero), no prints: the convention fills it and the label says so
    assert f["source"] == "flow+fill" and f["pos"] == 20 and f["flow_share"] == 0.0, f
    again = reconcile(oi0, oi1, {"X C100": 5.0}, pos, "2026-09-11")
    assert again["X C100"]["pos"] == c["pos"], "the same session was applied twice"
    nick = reconcile(oi0, oi1, {"X C100": 500.0}, None, "2026-09-11")
    assert nick["X C100"]["pos"] == 100 - 30, "with |flow| >= |delta OI| the source rule is recovered"

    # 3. apply onto a book, and the summary
    books = [{"root": "X", "strikes": {"100.0": {"C": {"oi": 130}, "P": {"oi": 10}},
                                       "90.0": {"C": {"oi": 0}, "P": {"oi": 40}},
                                       "120.0": {"C": {"oi": 3}, "P": {"oi": 0}}}}]
    n, covered = apply(books, pos)
    assert n == 2 and covered == 130 + 10 + 40, (n, covered)
    assert books[0]["strikes"]["100.0"]["pos"]["dealer"] == 120 - 10       # measured call leg, convention put leg
    assert books[0]["strikes"]["90.0"]["pos"]["dealer"] == 40
    assert "pos" not in books[0]["strikes"]["120.0"]
    sm = summary(pos)
    assert sm["contracts"] == 3 and sm["sign_opposite_to_convention"] == 1, sm   # the puts read dealers LONG
    print("dealer_flow selftest passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--summary", help="an inventory JSON ({sym: record}) to summarise")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.summary:
        print(json.dumps(summary(json.load(open(a.summary, encoding="utf-8"))), indent=1))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
