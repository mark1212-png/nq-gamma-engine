#!/usr/bin/env python3
"""
quikstrike_oi.py -- Friday's open interest on a Saturday.

    python quikstrike_oi.py --date 2026-09-04
    python quikstrike_oi.py --date 2026-09-04 --oi-pdf data/2026-09-04-oi.pdf \\
                            --settle data/2026-09-04-settle.json --carry data/prelim/2026-09-03-chain.json

WHY THIS EXISTS. Open interest reaches Databento as a statistics message on
CME's MDP3 feed, and that feed is dark from Friday 16:00 CT to Sunday 17:00 CT.
Friday's OI is final at CME by Saturday morning -- QuikStrike shows it -- but
nothing carries it over the feed until the Sunday reopen at the earliest, and
possibly not until the Tuesday clearing day. A plan for the Sunday/Monday
Globex session therefore cannot get Friday's book through Databento in time.

QuikStrike's OI Matrix (Greek: None), printed to PDF by hand, has the figure.
Manual export is fine under their terms; automating their site is not, and
this file does not. It PARSES the export and MERGES it with the settlement
file the 16:00 job already captured:

    open interest      <- the PDF (Friday's final, per strike, per expiry)
    settlement prices  <- data/<date>-settle.json (Friday's, every listed option)
    futures settles    <- the same file (NQU6 29,565.25, NQZ6 29,860.25 ...)
    expiries the PDF does not list (QuikStrike's default list stops ~30 DTE)
                       <- carried from the previous chain, stamped as such

and writes data/<date>-chain.json in exactly the shape databento_pull writes,
stamped `oi_source: quikstrike`, so nothing downstream needs a special case.
Implied vol is solved from the settlement prices by nqvol, as always.

THE PARSER is positional, lifted unchanged from the retired nqgamma.py: it bins
every number on a row to the nearest C/P header column by x-coordinate. Pasting
or text-dumping the matrix collapses empty cells and shifts values one column
left, which reads puts as calls (see METHOD.md). Zero warnings = zero
ambiguous cells; anything else, look at the file.

Checks it makes before writing, because a wrong book here is worse than none:
  * the PDF's "as of" date must equal --date
  * the PDF's futures reference must equal the settle file's NQU6 settle
  * the front expiry in the PDF must be the first expiry after --date
  * total OI must be within 40% of the carried chain's
"""

import re, sys, json, argparse, datetime, pathlib

import nqvol

ROOT = pathlib.Path(__file__).parent.resolve()
DATA = ROOT / "data"

# QuikStrike's code for the end-of-month root is NEU6; Databento's is QNEU6.
ALIAS = {"NE": "QNE"}


def parse_matrix(pdf_path):
    """
    Positional extraction of a QuikStrike OI Matrix PDF.
    Returns (rows, expiries, dtes, underlyings, asof_date, warnings) where rows
    maps strike -> {column_index: value}, 2*j = expiry j calls, 2*j+1 = puts.
    """
    import pdfplumber
    pdf = pdfplumber.open(pdf_path)
    rows, expiries, dtes, unders, asof = {}, [], [], [], None
    warnings = []

    for pi_, page in enumerate(pdf.pages):
        words = page.extract_words()
        if pi_ == 0:
            text = page.extract_text() or ""
            m = re.search(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", text)
            if m:
                asof = datetime.datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%b %d %Y").date()

        cp = [w for w in words if w["text"] in ("C", "P") and 90 < w["x0"] < 620]
        if not cp:
            continue
        tops = sorted({round(w["top"]) for w in cp})
        best = max(tops, key=lambda t: sum(1 for w in cp if abs(w["top"] - t) < 3))
        hdr = sorted([w for w in cp if abs(w["top"] - best) < 3], key=lambda w: w["x0"])
        centers = [(w["x0"] + w["x1"]) / 2 for w in hdr]

        if pi_ == 0:
            band = [w for w in words if best - 22 < w["top"] < best - 14 and w["x0"] > 110]
            expiries = [w["text"] for w in sorted(band, key=lambda w: w["x0"])]
            dband = [w for w in words if best - 14 < w["top"] < best - 6 and w["x0"] > 110]
            dtes = [int(t["text"]) for t in sorted(dband, key=lambda w: w["x0"]) if t["text"].isdigit()]
            uband = [w for w in words if best - 45 < w["top"] < best - 24 and w["x0"] > 110]
            unders = sorted({w["text"] for w in uband if re.fullmatch(r"\d+(\.\d+)?", w["text"])}, key=float)

        buckets = {}
        for w in words:
            if w["top"] <= best + 5 or w["x0"] < 90 or w["x1"] > 620:
                continue
            if not re.fullmatch(r"[\d,]+(\.\d+)?", w["text"]):
                continue
            buckets.setdefault(round(w["top"] / 3), []).append(w)

        for _, wl in sorted(buckets.items()):
            wl = sorted(wl, key=lambda w: w["x0"])
            strike, vals = None, {}
            for w in wl:
                c = (w["x0"] + w["x1"]) / 2
                v = w["text"].replace(",", "")
                if c < 118:
                    strike = float(v)
                else:
                    i = min(range(len(centers)), key=lambda j: abs(centers[j] - c))
                    if abs(centers[i] - c) > 9:
                        warnings.append(f"p{pi_} strike {strike} token {v} far from any column")
                    vals[i] = vals.get(i, 0) + int(float(v))
            if strike is None:
                continue
            cur = rows.setdefault(strike, {})
            for i, v in vals.items():
                if i in cur and cur[i] != v:
                    warnings.append(f"conflict at {strike} col {i}: {cur[i]} vs {v}")
                cur[i] = v
    pdf.close()
    return rows, expiries, dtes, unders, asof, warnings


def db_root(code):
    """QuikStrike expiry code -> Databento root (NEU6 -> QNEU6; the rest match)."""
    for q, d in ALIAS.items():
        if code.startswith(q) and not code.startswith(d):
            return d + code[len(q):]
    return code


def merge(date, oi_pdf, settle_path, carry_path=None, out=None):
    d = datetime.date.fromisoformat(date)
    rows, exps, dtes, unders, asof, warns = parse_matrix(oi_pdf)
    if warns:
        raise SystemExit(f"  {len(warns)} ambiguous cells in {oi_pdf} -- not merging:\n  " + "\n  ".join(warns[:8]))
    if asof != d:
        raise SystemExit(f"  the PDF is as of {asof}, not {d} -- wrong export")
    settle = json.load(open(settle_path, encoding="utf-8"))
    if settle.get("date") != date:
        raise SystemExit(f"  {settle_path} is for {settle.get('date')}, not {date}")
    fut = settle["futures_settle"]
    ref_pdf = float(unders[0]) if unders else None
    ref_db = fut.get("NQU6")
    if ref_pdf and ref_db and abs(ref_pdf - ref_db) > 0.01:
        raise SystemExit(f"  PDF futures reference {ref_pdf:,.2f} != settle file NQU6 {ref_db:,.2f}")
    front_expected = nqvol.next_session(d)
    print(f"  PDF: as of {asof}, {len(rows)} strikes x {len(exps)} expiries, reference {ref_pdf:,.2f}, "
          f"front {exps[0]} (DTE {dtes[0] if dtes else '?'}), no ambiguous cells")

    chain = settle["chain"]                       # settle prices, oi = 0 everywhere
    roots_in_chain = {k.split("|")[1]: k for k in chain}
    seen_roots, missing = set(), []
    filled = 0
    for j, code in enumerate(exps):
        root = db_root(code)
        key = roots_in_chain.get(root)
        if key is None:
            missing.append(code); continue
        seen_roots.add(root)
        if j == 0 and key.split("|")[0] != front_expected.isoformat():
            raise SystemExit(f"  front expiry in the PDF is {code} = {key.split('|')[0]}, "
                             f"expected the first session after {d}: {front_expected}")
        strikes = chain[key]
        for ks, cols in rows.items():
            k = f"{ks:.0f}"
            for cls, idx in (("C", 2 * j), ("P", 2 * j + 1)):
                n = cols.get(str(idx), cols.get(idx, 0))
                rec = strikes.get(k)
                if rec is None or cls not in rec:
                    if n:
                        # listed in the PDF, not priced in the settle file: keep the OI, no vol
                        strikes.setdefault(k, {})[cls] = {"oi": int(n), "settle": None, "underlying": None,
                                                          "expiration": None, "root": root,
                                                          "symbol": f"{root} {cls}{k}", "iv": None,
                                                          "iv_note": "no settlement price"}
                        filled += 1
                    continue
                rec[cls]["oi"] = int(n)
                if n:
                    filled += 1
        # fix underlying/expiration on any row we invented, from a sibling
        sib = next((r[c] for r in strikes.values() for c in r if r[c].get("expiration")), None)
        if sib:
            for r in strikes.values():
                for c in r:
                    if r[c].get("expiration") is None:
                        r[c]["expiration"], r[c]["underlying"] = sib["expiration"], sib["underlying"]
    if missing:
        print(f"  PDF expiries with no settle-file book (skipped): {missing}")

    # expiries the PDF does not list: carry the previous chain's OI, and say so
    carried = []
    if carry_path:
        prev = json.load(open(carry_path, encoding="utf-8"))
        for key, strikes in chain.items():
            day_, root = key.split("|")
            if root in seen_roots or day_ <= date:      # never carry an expiry that has already gone
                continue
            pk = next((k for k in prev["chain"] if k.split("|")[1] == root), None)
            if pk is None:
                continue
            n_ = 0
            for ks, rec in prev["chain"][pk].items():
                for cls in ("C", "P"):
                    if cls in rec and rec[cls].get("oi"):
                        tgt = strikes.setdefault(ks, {}).setdefault(cls, dict(rec[cls]))
                        tgt["oi"] = int(rec[cls]["oi"]); n_ += tgt["oi"]
                        tgt["oi_carried_from"] = prev.get("date")
            carried.append((root, n_))
    # drop zero-OI legs, and books with nothing open
    for key in list(chain):
        strikes = chain[key]
        for ks in list(strikes):
            for cls in list(strikes[ks]):
                if not strikes[ks][cls].get("oi"):
                    del strikes[ks][cls]
            if not strikes[ks]:
                del strikes[ks]
        if not strikes:
            del chain[key]

    tot = sum((r[c].get("oi") or 0) for st in chain.values() for r in st.values() for c in r)
    pdf_tot = sum(v for cols in rows.values() for v in cols.values())
    print(f"  merged: {filled:,} legs from the PDF ({pdf_tot:,} contracts across {len(exps)} expiries)"
          + (f"; carried {sum(n for _, n in carried):,} from {carry_path.split('/')[-1]} on "
             f"{len(carried)} far expiries ({', '.join(r for r, _ in carried)})" if carried else "")
          + f"; total {tot:,}")
    if carry_path:
        pt = sum((r[c].get("oi") or 0) for st in prev["chain"].values() for r in st.values() for c in r)
        if pt and abs(tot - pt) / pt > 0.40:
            raise SystemExit(f"  total OI {tot:,} is {100*(tot-pt)/pt:+.0f}% vs the carried chain's {pt:,} -- not writing")

    raw = {"date": date, "settle_only": False, "oi_source": "quikstrike",
           "oi_pdf": pathlib.Path(oi_pdf).name, "oi_carried_from": carry_path and prev.get("date"),
           "futures_settle": fut, "chain": chain}
    nqvol.solve_chain(raw, settle_date=date, force=True)
    out = out or (DATA / f"{date}-chain.json")
    json.dump(raw, open(out, "w", encoding="utf-8"), indent=1)
    print(f"  wrote {out} ({pathlib.Path(out).stat().st_size/1e6:.2f} MB, {len(chain)} expiries, iv on {raw['iv_basis']} basis)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="the OI / settlement date, YYYY-MM-DD")
    ap.add_argument("--oi-pdf", default=None, help="QuikStrike OI Matrix, Greek: None (default data/<date>-oi.pdf)")
    ap.add_argument("--settle", default=None, help="settle-only chain from the 16:00 job (default data/<date>-settle.json)")
    ap.add_argument("--carry", default=None, help="previous chain for expiries the PDF does not list (default: newest before <date>)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = datetime.date.fromisoformat(a.date)
    oi_pdf = a.oi_pdf or (DATA / f"{a.date}-oi.pdf")
    settle = a.settle or (DATA / f"{a.date}-settle.json")
    carry = a.carry
    if carry is None:
        p = nqvol.prev_session(d)
        for _ in range(4):
            for cand in (DATA / f"{p}-chain.json", DATA / "prelim" / f"{p}-chain.json"):
                if cand.exists() and cand.stat().st_size > 500_000:
                    carry = str(cand); break
            if carry: break
            p = nqvol.prev_session(p)
    for f in (oi_pdf, settle):
        if not pathlib.Path(f).exists():
            raise SystemExit(f"  missing {f}")
    merge(a.date, str(oi_pdf), str(settle), carry, a.out)


if __name__ == "__main__":
    main()
