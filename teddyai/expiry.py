#!/usr/bin/env python3
"""
expiry.py -- how much of the book's exposure comes from which expiry.

    import expiry
    d = expiry.dominance(books, spot, F_ref)

The engine already reports every expiry's gamma, vanna and open interest in
`trinity_db`'s `by_expiry`. What it does not say is the SHARE: whether tonight's
expiring line is carrying the board or is a rounding error against the September
quarterly. That is what this answers.

--------------------------------------------------------------------------------
THERE ARE THREE LEVELS OF CANCELLATION AND THE CHOICE BETWEEN THEM CHANGES THE ANSWER
--------------------------------------------------------------------------------

Net exposure cancels three separate times on its way to a headline number:

  1. WITHIN A STRIKE   n = OI_call - OI_put. A strike with 500 calls and 500 puts
                       nets to zero and vanishes.
  2. ACROSS STRIKES    an expiry with +800 at one strike and -800 at another nets
     WITHIN AN EXPIRY  to zero, though it plainly contains structure.
  3. ACROSS EXPIRIES   a +900 expiry and a -900 expiry net to zero.

Mark's brief warned about level 3. Levels 1 and 2 are just as real, and a
dominance ratio that silently picks one is not auditable. So BOTH are reported
for every bucket:

  net     the signed sum over strikes. What a dealer's net delta hedge responds
          to. Cancels at levels 1 and 2.
  gross   the sum of |per-strike exposure|. What structure exists. Cancels only
          at level 1, which is unavoidable -- an offsetting strike genuinely has
          no gamma.

and TWO dominance measures accordingly:

  share       |net_bucket| / sum over buckets of |net|      -- level 3 protected
  gross_share gross_bucket / sum over buckets of gross      -- levels 2 and 3 protected

`share` is the figure Mark's brief describes. `gross_share` is reported beside it
because the two disagree exactly when an expiry is internally offsetting, and
that disagreement is information rather than noise.

--------------------------------------------------------------------------------
UNITS
--------------------------------------------------------------------------------

gamma        contracts of delta per 1% move   (the engine's native unit)
vanna        contracts of delta per vol point
oi           contracts
share        dimensionless, [0, 1]
dte          SESSIONS to expiry, not calendar days -- trinity_db counts business
             sessions, so a Friday book seen on a Thursday is 1, not 1 or 3
             depending on the weekend.

--------------------------------------------------------------------------------
CHARM
--------------------------------------------------------------------------------

The brief asks for a charm dominance measure alongside gamma, vanna and open
interest. Charm is not implemented in `nqvol`, and `Claude outputs/RESULTS-1.md`
tested it over 63 sessions: net dealer charm called the day's direction 43% of the
time, worse than chance, and the charm "pin" sat farther from the close than a
random material strike. Its dominance would also be near-tautological -- that
study found the charm figures identical across the 0DTE, sub-9 and full books
because the 0DTE book dominates charm by construction, so the ratio would print
"0DTE dominant" every single day and carry no information. It is not built.

Written 2026-09-09, Phase D of the market-state upgrade.
"""

from dataclasses import dataclass, asdict, field

# Buckets are in SESSIONS to expiry. The first three exist because they behave
# differently intraday; the last three because the brief asked for weekly and
# monthly separation. The 0 / 1-9 / 10+ partition used by trinity_db's confluence
# score is reported separately as `partition`, so the two conventions sit side by
# side rather than one quietly replacing the other.
DEFAULT_BUCKETS = [
    ("0DTE", 0, 0),
    ("1DTE", 1, 1),
    ("2-5", 2, 5),
    ("6-9", 6, 9),
    ("10-21", 10, 21),
    ("22+", 22, 10_000),
]

PARTITION = [("0DTE", 0, 0), ("1-9", 1, 9), ("10+", 10, 10_000)]


@dataclass
class Config:
    buckets: list = field(default_factory=lambda: list(DEFAULT_BUCKETS))
    include_oi: bool = True

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


def _bucket_of(dte, buckets):
    for name, lo, hi in buckets:
        if lo <= dte <= hi:
            return name
    return None


def per_expiry(books, spot, F_ref):
    """
    Signed and gross exposure for every live book, plus its open interest.

    Computed per book with `on_axis=False`, matching trinity_db.by_expiry: a
    single book needs no basis shift because every strike in it shares one
    underlying, and shifting would only bucket it onto a grid for no purpose.
    """
    import trinity_db
    rows = []
    for b in books:
        g, v = trinity_db.strike_metrics([b], S_shift=spot - F_ref, on_axis=False)
        oi = sum((r.get(c, {}).get("oi") or 0)
                 for r in b["strikes"].values() for c in ("C", "P"))
        rows.append({
            "expiry": b["day"],
            "root": b["root"],
            "dte": b["dte"],
            "underlying": b["und"],
            "basis": b.get("basis", 0.0),
            "T_sessions": round(b["T"] * 252, 4),
            "atm_iv": round(b["atm"], 6),
            "gamma_net": round(sum(g.values()), 4),
            "gamma_gross": round(sum(abs(x) for x in g.values()), 4),
            "vanna_net": round(sum(v.values()), 4),
            "vanna_gross": round(sum(abs(x) for x in v.values()), 4),
            "oi": int(oi),
            "n_strikes": len(g),
        })
    rows.sort(key=lambda r: (r["dte"], r["expiry"], r["root"]))
    return rows


def _aggregate(rows, buckets, keys):
    """Fold per-expiry rows into buckets and compute both dominance measures."""
    out = {}
    for name, lo, hi in buckets:
        out[name] = {"expiries": 0, "dte_range": [lo, hi if hi < 10_000 else None], "roots": []}
        for k in keys:
            out[name][f"{k}_net"] = 0.0
            out[name][f"{k}_gross"] = 0.0
        out[name]["oi"] = 0

    for r in rows:
        name = _bucket_of(r["dte"], buckets)
        if name is None:
            continue
        b = out[name]
        b["expiries"] += 1
        b["roots"].append(r["root"])
        b["oi"] += r["oi"]
        for k in keys:
            b[f"{k}_net"] += r[f"{k}_net"]
            b[f"{k}_gross"] += r[f"{k}_gross"]

    for k in keys:
        tot_absnet = sum(abs(b[f"{k}_net"]) for b in out.values()) or 0.0
        tot_gross = sum(b[f"{k}_gross"] for b in out.values()) or 0.0
        for b in out.values():
            b[f"{k}_share"] = round(abs(b[f"{k}_net"]) / tot_absnet, 4) if tot_absnet else None
            b[f"{k}_gross_share"] = round(b[f"{k}_gross"] / tot_gross, 4) if tot_gross else None
            b[f"{k}_net"] = round(b[f"{k}_net"], 4)
            b[f"{k}_gross"] = round(b[f"{k}_gross"], 4)

    tot_oi = sum(b["oi"] for b in out.values()) or 0
    for b in out.values():
        b["oi_share"] = round(b["oi"] / tot_oi, 4) if tot_oi else None
    return out


def dominance(books, spot, F_ref, cfg=None, **kw):
    """
    Exposure share by expiry bucket, on both cancellation conventions.

    Returns per-expiry rows, bucket aggregates, the 0/1-9/10+ partition the
    confluence score uses, and headline 0DTE figures.
    """
    cfg = (cfg or Config()).merged(**kw)
    if not books:
        return {"buckets": {}, "by_expiry": [], "partition": {},
                "zero_dte": None, "note": "empty book"}

    rows = per_expiry(books, spot, F_ref)
    keys = ("gamma", "vanna")
    buckets = _aggregate(rows, cfg.buckets, keys)
    partition = _aggregate(rows, PARTITION, keys)

    z = buckets.get("0DTE", {})
    has_zero = z.get("expiries", 0) > 0
    signed_total = round(sum(r["gamma_net"] for r in rows), 4)
    abs_total = round(sum(abs(r["gamma_net"]) for r in rows), 4)
    gross_total = round(sum(r["gamma_gross"] for r in rows), 4)

    return {
        "by_expiry": rows,
        "buckets": buckets,
        "partition": partition,
        "totals": {
            "gamma_signed": signed_total,
            "gamma_sum_of_abs_expiry_nets": abs_total,
            "gamma_gross": gross_total,
            # How much the signed headline hides. 1.0 means every expiry pulls the
            # same way; near 0 means the board is internally offsetting and the
            # column sum is not describing the book.
            "signed_over_abs": round(abs(signed_total) / abs_total, 4) if abs_total else None,
            "abs_over_gross": round(abs_total / gross_total, 4) if gross_total else None,
        },
        "zero_dte": ({
            "present": True,
            "expiries": z["expiries"],
            "roots": z["roots"],
            "gamma_net": z["gamma_net"],
            "gamma_gross": z["gamma_gross"],
            "share": z["gamma_share"],
            "gross_share": z["gamma_gross_share"],
            "vanna_share": z["vanna_share"],
            "oi_share": z["oi_share"],
        } if has_zero else {
            "present": False,
            "note": ("Nothing expires on the report date. The 0DTE bucket is EMPTY, "
                     "not flat -- dealers are not neutral, there is simply no book."),
        }),
        "share_basis": "|bucket net| / sum over buckets of |net|; opposite-signed expiries cannot cancel",
        "gross_share_basis": "bucket gross / total gross; strikes within an expiry cannot cancel either",
        "charm": ("not computed: not implemented in nqvol, measured worse than chance over 63 "
                  "sessions, and its dominance is near-tautological because the 0DTE book "
                  "dominates charm by construction"),
        "config": {"buckets": cfg.buckets},
    }
