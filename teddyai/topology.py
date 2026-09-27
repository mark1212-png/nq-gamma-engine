#!/usr/bin/env python3
"""
topology.py -- the SHAPE of dealer exposure across price.

    import topology
    t = topology.strike_topology(gmap, spot=29538.75, em=243.0)
    p = topology.spot_profile(books, F_ref, S)

The exposure engine (trinity_db.strike_metrics) says how much gamma sits at each
strike. This module says what SHAPE that makes: where the walls are, where the
shelves are, where the book is empty, and where it changes sign.

WHAT THIS MODULE DOES NOT DO. It never says which way price goes. Every output
here is a description of structure. The 63-session study in
`Claude outputs/RESULTS-1.md` tested the one directional reading that this kind
of layer invites -- that a break into thin structure runs further -- and found it
an artefact of node density: of 4,354 breaks only 15 had a gap wider than half an
expected move, so "air" did not exist to break into. A vacuum here therefore
carries a width and a confidence and no direction at all. There is deliberately
no field in the output schema that a caller could mistake for a bias.

--------------------------------------------------------------------------------
THE TWO AXES, WHICH ARE DIFFERENT OBJECTS AND MUST NOT BE MERGED
--------------------------------------------------------------------------------

STRIKE AXIS -- `strike_topology()`
    Exposure attributed to each strike, at today's spot. This is where walls,
    shelves, clusters and pockets live, because it is where the open interest
    actually sits. It is a POINT MEASURE on an irregular grid, not a function.

SPOT AXIS -- `spot_profile()`
    Total book gamma when the underlying is repriced to a hypothetical level.
    This is what trinity_db.hvl() already sweeps, and its zero crossing is the
    flip. It is a smooth function of price, and it is the ONLY axis on which
    dG/dS and d2G/dS2 are well posed.

Measured on the 2026-09-08 near book, the two look nothing alike. The spot axis
runs monotonically from -1,130 at 29,239 to +3,998 at 29,839 with no local
extremum. The strike axis over the same range alternates sign between adjacent
strikes: +32 at 29,480, -14 at 29,490, -7 at 29,500, +3 at 29,510.

--------------------------------------------------------------------------------
WHY THE STRIKE AXIS IS BINNED BEFORE IT IS READ
--------------------------------------------------------------------------------

NQ lists strikes 5 and 10 points apart near the money and 50 or 100 points apart
in the wings; one near book carried spacings of 5, 10, 20, 25, 50 and 100. A
finite difference across that grid measures the listing convention, not the book.

Exposure is a point measure, so the correct aggregation is to SUM into fixed
price bins, which preserves total mass exactly. Structure is then read off a
regular grid where a difference means something. The bin width defaults to the
chain's own strike grid (25 on NQ, 5 on SPX, 1 on the ETFs) and is configurable.

--------------------------------------------------------------------------------
UNITS
--------------------------------------------------------------------------------

Input `gmap`      {strike: exposure}. The engine's native unit is CONTRACTS OF
                  DELTA PER 1% MOVE (the QuikStrike Gamma(1 Pct) convention).
                  Nothing here assumes that; it is unit-agnostic and carries the
                  caller's label through to the output as `unit`.
mass, peak        the same unit as the input, summed over an interval
width, lower,     price points of the reference contract
  upper, center
strength          dimensionless, share of the window's GROSS exposure in [0,1]
concentration     dimensionless, peak bin over interval mass in [0,1]
gradient          input unit per POINT of the underlying   (spot axis only)
curvature         input unit per POINT SQUARED             (spot axis only)

--------------------------------------------------------------------------------
READ RAW FLOATS, NOT trinity.json
--------------------------------------------------------------------------------

`trinity.json` publishes {strike: int(round(v))} filtered by `if round(val)`, so
every strike whose net rounds to zero is DROPPED. On the 2024-09-10 book 18,590
is -1.27 on the 0DTE panel and +0.11 on NEAR, so NEAR omits it entirely. Reading
those maps here would turn rounding into pockets. Use
trinity_db.strike_metrics() or chain_live.json, which carry the raw floats.
`from_books()` below does this for you.

Written 2026-09-09, Phase B of the market-state upgrade.
"""

import math
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- config --

@dataclass
class Config:
    """Every threshold in this module. Nothing is hardcoded at a call site.

    The two magnitude thresholds are RELATIVE to the window's own distribution,
    so a quiet book and a violent one are read on the same footing. `material`
    reuses the 0.25 that command_center._material has used since 2026-09-05 for
    the structural stop rule, deliberately: two different definitions of "this
    strike matters" on one sheet is how a stop and a label come to disagree.
    """
    bin_width: float = 25.0          # price points per bin; default overridden by the chain's grid
    window_ems: float = 2.0          # analyse +/- this many expected moves around price
    window_min: float = 400.0        # ...but never a narrower window than this, in points

    material: float = 0.25           # |bin| >= this * scale is structure
    quiet: float = 0.08              # |bin| <= this * scale is empty
    wall_peak: float = 0.50          # a structural run whose peak bin reaches this * scale is a wall
    isolated_gap: float = 50.0       # points of empty either side for a wall to be called isolated
    vacuum_width: float = 100.0      # a pocket at least this wide is labelled a vacuum
    min_run_bins: int = 1            # bins a run needs before it is reported

    flip_band: float = 0.10          # spot axis: |G| <= this * scale_spot is "at the flip"
    profile_step: float = 5.0        # spot axis: price step of the repricing sweep, in points
    profile_span: float = 400.0      # spot axis: +/- points swept around the reference

    scale_pct: float = 0.95          # the robust scale is this quantile of |bin| in the window

    def merged(self, **kw):
        d = asdict(self)
        d.update({k: v for k, v in kw.items() if v is not None and k in d})
        return Config(**d)


# --------------------------------------------------------------- helpers --

def _quantile(xs, q):
    """Linear-interpolated quantile of a sorted-able sequence. No numpy needed,
    and it must behave on a 1- or 2-element window without raising."""
    v = sorted(x for x in xs if x is not None)
    if not v:
        return 0.0
    if len(v) == 1:
        return v[0]
    i = q * (len(v) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def _sign(x, eps=0.0):
    return 1 if x > eps else -1 if x < -eps else 0


def bin_exposure(gmap, lo, hi, width):
    """
    Sum a point measure into fixed-width price bins. MASS PRESERVING: the total
    of the returned values equals the total of every input strike inside [lo, hi].

    Returns (centers, values, counts) where counts is how many STRIKES landed in
    each bin -- a bin fed by one strike and a bin fed by six are different kinds
    of evidence and the caller is told which it has.
    """
    if width <= 0:
        raise ValueError("bin width must be positive")
    n = max(1, int(math.ceil((hi - lo) / width)))
    vals = [0.0] * n
    cnts = [0] * n
    for k, v in gmap.items():
        k = float(k)
        if k < lo or k > hi:
            continue
        i = min(n - 1, int((k - lo) // width))
        vals[i] += float(v)
        cnts[i] += 1
    centers = [lo + (i + 0.5) * width for i in range(n)]
    return centers, vals, cnts


# -------------------------------------------------------------- features --

@dataclass
class Feature:
    """One structural formation on the strike axis.

    `kind` is one of four NON-OVERLAPPING types. The trader vocabulary -- cluster,
    isolated node, vacuum -- is carried in `labels` rather than as extra types,
    because a cluster IS a wall made of several strikes and giving it its own
    type would put two correlated names on one object and invite double-counting
    in anything that scores them later.
    """
    kind: str                     # wall | shelf | pocket | transition
    lower: float
    upper: float
    center: float
    sign: int                     # +1, -1, or 0 for a pocket
    mass: float                   # summed exposure over the interval, input unit
    peak: float                   # largest single bin, signed, input unit
    peak_price: float
    width: float                  # points
    n_bins: int
    n_strikes: int                # how many listed strikes fed it
    strength: float               # share of the window's gross exposure, [0,1]
    concentration: float          # |peak| / |mass|, [0,1]; 1 = all in one bin
    distance: float               # signed points from the reference price
    edge: bool                    # touches the analysis window boundary
    labels: list = field(default_factory=list)
    evidence: list = field(default_factory=list)

    def as_dict(self):
        d = asdict(self)
        for k in ("lower", "upper", "center", "peak_price", "width", "distance"):
            d[k] = round(d[k], 2)
        for k in ("mass", "peak"):
            d[k] = round(d[k], 3)
        for k in ("strength", "concentration"):
            d[k] = round(d[k], 4)
        return d


def _runs(vals, pred):
    """Maximal contiguous index ranges where pred(i) holds."""
    out, start = [], None
    for i in range(len(vals)):
        if pred(i):
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i - 1)); start = None
    if start is not None:
        out.append((start, len(vals) - 1))
    return out


def strike_topology(gmap, spot, em=None, unit="contracts of delta per 1% move",
                    cfg=None, coverage=None, **kw):
    """
    Structural formations on the STRIKE axis.

    gmap      {strike: exposure} with RAW float values. See the module docstring
              on why the rounded maps in trinity.json must not be used.
    spot      the reference price distances are measured from -- the live print
              on an intraday plan, the settle otherwise.
    em        expected move, used only to size the analysis window.
    coverage  optional (lo, hi) beyond which the ENGINE has no data, e.g. the
              moneyness cap in trinity_db. A feature touching it is flagged
              `edge`, so a truncation is never reported as an empty book.

    Returns a dict with `features`, the scale and window it used, and the counts
    a caller needs to judge how much evidence there is.
    """
    cfg = (cfg or Config()).merged(**kw)
    gmap = {float(k): float(v) for k, v in gmap.items()}

    half = max(cfg.window_ems * (em or 0.0), cfg.window_min)
    lo, hi = spot - half, spot + half
    if coverage:
        lo, hi = max(lo, coverage[0]), min(hi, coverage[1])

    centers, vals, cnts = bin_exposure(gmap, lo, hi, cfg.bin_width)
    absv = [abs(v) for v in vals]
    gross = sum(absv)

    # THE SCALE IS TAKEN OVER POPULATED BINS ONLY.
    #
    # An empty bin says nothing about what counts as a big number in this book,
    # and including empties makes the scale depend on how WIDE a window was
    # analysed rather than on the book. Measured: three strikes of 20 swept over
    # a 6,000-point window give 240 bins of which 3 are populated, so p95 over
    # every bin is 0.0 and every threshold collapses. Over populated bins it is
    # 20.0. The same defect quietly changed a broad even shelf into a row of
    # walls, because p95 over mostly-empty bins landed at the shelf's own height.
    #
    # WHY A QUANTILE RATHER THAN THE MAX. command_center._material has used
    # `max(abs(v))` since 2026-09-05. The max is fragile: one absurd print pushes
    # every genuine strike under the material threshold. p95 keeps that from
    # happening while agreeing with the max on any small book, because with
    # fewer than ~20 populated bins the interpolation lands on the top value
    # anyway. Setting scale_pct = 1.0 reproduces the older convention exactly.
    populated = [abs(v) for v, c in zip(vals, cnts) if c]
    scale = _quantile(populated, cfg.scale_pct)
    if scale <= 0:
        return {"features": [], "scale": 0.0, "gross": 0.0, "unit": unit,
                "window": [round(lo, 2), round(hi, 2)], "bin_width": cfg.bin_width,
                "n_bins": len(vals), "n_strikes": sum(cnts), "spot": spot,
                "note": "no exposure in the analysis window", "config": asdict(cfg)}

    mat = cfg.material * scale
    qui = cfg.quiet * scale
    wall_lvl = cfg.wall_peak * scale

    feats = []

    def build(kind, i0, i1, sign):
        seg = vals[i0:i1 + 1]
        mass = sum(seg)
        pk_i = i0 + max(range(len(seg)), key=lambda j: abs(seg[j]))
        lower = centers[i0] - cfg.bin_width / 2
        upper = centers[i1] + cfg.bin_width / 2
        width = upper - lower
        nstk = sum(cnts[i0:i1 + 1])
        f = Feature(
            kind=kind, lower=lower, upper=upper, center=(lower + upper) / 2,
            sign=sign, mass=mass, peak=vals[pk_i], peak_price=centers[pk_i],
            width=width, n_bins=i1 - i0 + 1, n_strikes=nstk,
            strength=(abs(mass) / gross) if gross else 0.0,
            concentration=(abs(vals[pk_i]) / abs(mass)) if mass else 1.0,
            distance=(lower + upper) / 2 - spot,
            edge=(i0 == 0 or i1 == len(vals) - 1),
        )
        return f

    # --- structural runs: same-signed, at or above the material threshold ----
    struct = []
    for s in (1, -1):
        for i0, i1 in _runs(vals, lambda i, s=s: _sign(vals[i]) == s and abs(vals[i]) >= mat):
            if i1 - i0 + 1 < cfg.min_run_bins:
                continue
            seg = vals[i0:i1 + 1]
            pk = max(abs(v) for v in seg)
            kind = "wall" if pk >= wall_lvl else "shelf"
            f = build(kind, i0, i1, s)
            f.evidence.append(f"{f.n_strikes} listed strike(s) in {f.n_bins} bin(s) of {cfg.bin_width:g} pts")
            f.evidence.append(f"peak {f.peak:+.1f} = {pk / scale:.2f}x the window scale ({scale:.1f})")
            if kind == "wall" and f.n_bins > 1:
                f.labels.append("cluster")
            if f.edge:
                f.evidence.append("touches the analysis window edge; mass beyond it is not counted")
            struct.append((i0, i1, f))
            feats.append(f)

    # isolation: a wall with nothing structural within isolated_gap either side
    occupied = [(a, b) for a, b, _ in struct]
    for i0, i1, f in struct:
        if f.kind != "wall":
            continue
        gap_lo = min([f.lower - (centers[b] + cfg.bin_width / 2)
                      for a, b in occupied if b < i0] or [math.inf])
        gap_hi = min([(centers[a] - cfg.bin_width / 2) - f.upper
                      for a, b in occupied if a > i1] or [math.inf])
        if min(gap_lo, gap_hi) >= cfg.isolated_gap:
            f.labels.append("isolated_node")
            f.evidence.append(f"nearest other structure {min(gap_lo, gap_hi):.0f} pts away")

    # --- pockets: quiet runs ------------------------------------------------
    for i0, i1 in _runs(vals, lambda i: abs(vals[i]) <= qui):
        if i1 - i0 + 1 < cfg.min_run_bins:
            continue
        f = build("pocket", i0, i1, 0)
        bounded = i0 > 0 and i1 < len(vals) - 1
        f.evidence.append(f"every bin under {cfg.quiet:.2f}x the window scale")
        if f.width >= cfg.vacuum_width:
            f.labels.append("vacuum")
            f.evidence.append(f"{f.width:.0f} pts wide, at or over the {cfg.vacuum_width:g} pt vacuum threshold")
        if not bounded:
            f.evidence.append("open at one end of the window: this is the edge of what was analysed, "
                              "not necessarily the edge of the book")
        feats.append(f)

    # --- transitions: where the structural sign changes ----------------------
    ordered = sorted(struct, key=lambda t: t[0])
    for (a0, a1, fa), (b0, b1, fb) in zip(ordered, ordered[1:]):
        if fa.sign == fb.sign:
            continue
        lower, upper = fa.upper, fb.lower
        if upper < lower:
            lower, upper = upper, lower
        t = Feature(
            kind="transition", lower=lower, upper=upper, center=(lower + upper) / 2,
            sign=0, mass=sum(vals[a1 + 1:b0]) if b0 > a1 + 1 else 0.0,
            peak=0.0, peak_price=(lower + upper) / 2, width=upper - lower,
            n_bins=max(0, b0 - a1 - 1), n_strikes=sum(cnts[a1 + 1:b0]),
            strength=min(fa.strength, fb.strength),
            concentration=0.0, distance=(lower + upper) / 2 - spot, edge=False,
        )
        t.evidence.append(f"{fa.kind} {fa.sign:+d} below at {fa.center:,.0f}, "
                          f"{fb.kind} {fb.sign:+d} above at {fb.center:,.0f}")
        t.evidence.append("a sign change in the strike map is not the gamma flip; "
                          "the flip is a spot-axis crossing, see spot_profile()")
        feats.append(t)

    feats.sort(key=lambda f: f.lower)
    return {
        "features": [f.as_dict() for f in feats],
        # FEATURES MAY OVERLAP, AND THAT IS NOT A BUG. A transition spans the
        # interval between two opposite-signed structures, and a pocket often
        # sits inside it -- on the 2026-09-08 near book the transition runs
        # 29,453-29,578 and a pocket sits at 29,478-29,528. They are two true
        # statements about one stretch of price: the sign changes across it, and
        # the book is empty in the middle of it. Anything that SCORES these must
        # therefore deduplicate by price rather than summing over features, or a
        # region gets counted twice.
        "features_may_overlap": True,
        "unit": unit,
        "scale": round(scale, 4),
        "scale_basis": f"p{cfg.scale_pct * 100:.0f} of |binned exposure| over POPULATED bins",
        "n_populated_bins": len(populated),
        "gross": round(gross, 3),
        "spot": spot,
        "window": [round(lo, 2), round(hi, 2)],
        "window_truncated_by_coverage": bool(coverage) and (lo <= (coverage[0] if coverage else lo)
                                                            or hi >= (coverage[1] if coverage else hi)),
        "bin_width": cfg.bin_width,
        "n_bins": len(vals),
        "n_strikes": sum(cnts),
        "thresholds": {"material": round(mat, 4), "quiet": round(qui, 4), "wall": round(wall_lvl, 4)},
        "config": asdict(cfg),
    }


# ------------------------------------------------------------- spot axis --

def spot_profile(books, F_ref, spot, cfg=None, strike_metrics=None,
                 unit="contracts of delta per 1% move", **kw):
    """
    The G(F) curve: total book exposure when the underlying is repriced.

    This is the ONLY axis where a derivative is well posed, because the sweep is
    evaluated on a regular grid the caller chooses. trinity_db.hvl() already
    computes this curve at 1-point steps to find its zero crossings and then
    discards it; this returns the curve, the crossings AND the local shape.

    Derivatives are central differences on a uniform step, so their error is
    O(step^2). At the default 5-point step on NQ that is negligible against the
    curve's own smoothness.

    `strike_metrics` is injected so this can be unit tested against an analytic
    curve without building a chain.
    """
    cfg = (cfg or Config()).merged(**kw)
    if strike_metrics is None:
        import trinity_db
        strike_metrics = trinity_db.strike_metrics

    step = cfg.profile_step
    n = int(cfg.profile_span / step)
    offsets = [i * step for i in range(-n, n + 1)]
    prices, gs = [], []
    for x in offsets:
        g, _ = strike_metrics(books, S_shift=float(x), on_axis=False)
        prices.append(F_ref + x)
        gs.append(sum(g.values()))

    def d1(i):
        if i == 0 or i == len(gs) - 1:
            return None
        return (gs[i + 1] - gs[i - 1]) / (2 * step)

    def d2(i):
        if i == 0 or i == len(gs) - 1:
            return None
        return (gs[i + 1] - 2 * gs[i] + gs[i - 1]) / (step * step)

    # zero crossings, linearly interpolated, nearest the reference first
    cross = []
    for i in range(len(gs) - 1):
        a, b = gs[i], gs[i + 1]
        if (a < 0 <= b) or (a >= 0 > b):
            x = prices[i] + (0 - a) * (prices[i + 1] - prices[i]) / (b - a)
            cross.append(x)
    cross.sort(key=lambda x: abs(x - spot))

    scale = _quantile([abs(g) for g in gs], cfg.scale_pct)
    band = cfg.flip_band * scale

    # the flip ZONE: how far price must travel from a crossing before the book
    # carries a decisive sign. A narrow zone is a sharp regime boundary; a wide
    # one means the sign near the flip is not meaningful.
    zones = []
    for x in cross:
        i = min(range(len(prices)), key=lambda j: abs(prices[j] - x))
        lo_i = i
        while lo_i > 0 and abs(gs[lo_i]) <= band:
            lo_i -= 1
        hi_i = i
        while hi_i < len(gs) - 1 and abs(gs[hi_i]) <= band:
            hi_i += 1
        zones.append({
            "crossing": round(x, 2),
            "lower": round(prices[lo_i], 2),
            "upper": round(prices[hi_i], 2),
            "width": round(prices[hi_i] - prices[lo_i], 2),
            "distance": round(x - spot, 2),
            "gradient": (round(d1(i), 5) if d1(i) is not None else None),
            "evidence": [f"|G| stays under {band:.1f} ({cfg.flip_band:.2f}x the sweep scale) "
                         f"across {round(prices[hi_i] - prices[lo_i], 0):.0f} pts"],
        })

    i_spot = min(range(len(prices)), key=lambda j: abs(prices[j] - spot))
    return {
        "unit": unit,
        "gradient_unit": f"{unit} per point of underlying",
        "curvature_unit": f"{unit} per point squared",
        "reference": F_ref,
        "spot": spot,
        "step": step,
        "span": cfg.profile_span,
        "curve": [[round(p, 2), round(g, 3)] for p, g in zip(prices, gs)],
        "at_spot": {
            "G": round(gs[i_spot], 3),
            "gradient": (round(d1(i_spot), 5) if d1(i_spot) is not None else None),
            "curvature": (round(d2(i_spot), 8) if d2(i_spot) is not None else None),
        },
        "crossings": [round(x, 2) for x in cross],
        "flip_zones": zones,
        "scale": round(scale, 4),
        "monotone": all(b >= a for a, b in zip(gs, gs[1:])) or all(b <= a for a, b in zip(gs, gs[1:])),
        "note": ("Descriptive only. A gamma sign does not imply a range: the 60-session "
                 "backtest found no support for it (p=0.46)."),
        "config": asdict(cfg),
    }


# ------------------------------------------------------------ convenience --

def from_books(books, spot, F_ref, em=None, panel=None, cfg=None, **kw):
    """
    strike_topology() straight off the exposure engine, using RAW floats.

    `books` is trinity_db's book list; `panel` optionally filters it, e.g.
    [b for b in books if b['dte'] == 0]. The moneyness cap each book carries is
    turned into a coverage window so a truncation is never read as an empty book.
    """
    import trinity_db
    bs = panel if panel is not None else books
    if not bs:
        return {"features": [], "scale": 0.0, "gross": 0.0, "spot": spot,
                "note": "empty book", "n_strikes": 0, "n_bins": 0}
    g, _ = trinity_db.strike_metrics(bs, S_shift=spot - F_ref)
    cap = min(b.get("cap", trinity_db.MONEYNESS_CAP) for b in bs)
    fmin = min(b["F"] for b in bs)
    fmax = max(b["F"] for b in bs)
    coverage = (fmin * math.exp(-cap), fmax * math.exp(cap))
    grid = bs[0].get("grid", trinity_db.GRID)
    cfg = (cfg or Config()).merged(bin_width=kw.pop("bin_width", grid), **kw)
    return strike_topology(g, spot, em=em, cfg=cfg, coverage=coverage)
