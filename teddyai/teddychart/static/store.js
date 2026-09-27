// store.js — the chart's data core. Everything the renderer draws is derived
// here, incrementally, from two raw inputs: the day's prints (the tape) and the
// prior sessions' 1-minute bars. Nothing here knows about pixels.
//
// THE TAPE IS THE TRUTH FOR TODAY. Time, tick and range bars, footprints, delta,
// CVD, the TPO letters and the big-lot list are all folded from it, so a
// timeframe switch never asks the server for anything and every view agrees
// with every other view to the print.
//
// EVERYTHING IS INCREMENTAL. Each builder keeps a cursor into the tape and only
// ever looks at prints it has not seen. A tick costs O(1) per builder; a replay
// of a million prints costs one pass. The forming bar is a real bar object that
// is mutated in place — the renderer treats "last bar changed" as the cheap
// path, so nothing is rebuilt on a tick.
//
// PRICES ARE HELD AS TICK INDICES inside footprints and profiles (price / 0.25
// rounded), so Map keys are exact integers and there is no float-key drift.

export const TICK = 0.25;
export const tickOf = p => Math.round(p / TICK);
export const priceOf = t => t * TICK;

const PT = 'America/Los_Angeles';
const _fmt = new Intl.DateTimeFormat('en-US', {
  timeZone: PT, hour12: false, weekday: 'short',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
// ms -> {h, m, s, dow, ymd} in Pacific time. One Intl call per minute-bar is
// cheap; it is never called per print.
export function ptParts(ms) {
  const p = {};
  for (const { type, value } of _fmt.formatToParts(ms)) p[type] = value;
  return { h: +p.hour % 24, m: +p.minute, s: +p.second, dow: p.weekday,
           ymd: `${p.year}-${p.month}-${p.day}` };
}
export function ptClock(ms, secs = false) {
  const q = ptParts(ms);
  const hh = String(q.h).padStart(2, '0'), mm = String(q.m).padStart(2, '0');
  return secs ? `${hh}:${mm}:${String(q.s).padStart(2, '0')}` : `${hh}:${mm}`;
}
// Minutes since midnight PT, and the PT offset — used to align bars to the
// session clock rather than to UTC, so a 30m bar starts at 06:30 not 06:00.
export function ptOffsetMs(ms) {
  const q = ptParts(ms);
  const asUtc = Date.UTC(+q.ymd.slice(0, 4), +q.ymd.slice(5, 7) - 1, +q.ymd.slice(8, 10), q.h, q.m, q.s);
  return asUtc - Math.floor(ms / 1000) * 1000;
}

// ---------------------------------------------------------------- tape ---

export class Tape {
  constructor() {
    this.cap = 1 << 18;
    this.ts = new Float64Array(this.cap);
    this.px = new Float32Array(this.cap);
    this.sz = new Uint32Array(this.cap);
    this.side = new Uint8Array(this.cap);
    this.cv = new Float64Array(this.cap);   // cumulative volume through print i
    this.cd = new Float64Array(this.cap);   // cumulative delta (ask - bid) through print i
    this.n = 0;
    this.seq = 0;               // next expected day-sequence number
    this.dayOpen = null;
    this.big = [];              // indices of prints >= BIG_MIN, for the bubbles
    this.dupes = 0;
  }
  static BIG_MIN = 5;

  grow(need) {
    let cap = this.cap;
    while (cap < need) cap *= 2;
    const g = (old, C) => { const a = new C(cap); a.set(old.subarray(0, this.n)); return a; };
    this.ts = g(this.ts, Float64Array); this.px = g(this.px, Float32Array);
    this.sz = g(this.sz, Uint32Array); this.side = g(this.side, Uint8Array);
    this.cv = g(this.cv, Float64Array); this.cd = g(this.cd, Float64Array);
    this.cap = cap;
  }

  /** Append a decoded TRADES frame. Overlap with what we hold is dropped by seq. */
  push(f) {
    let skip = 0;
    if (f.seq0 < this.seq) skip = Math.min(f.n, this.seq - f.seq0);
    if (f.seq0 > this.seq) this.gap = (this.gap || 0) + (f.seq0 - this.seq);
    const k = f.n - skip;
    if (k <= 0) { this.dupes += f.n; return 0; }
    if (this.n + k > this.cap) this.grow(this.n + k);
    this.ts.set(f.ts.subarray(skip), this.n);
    this.px.set(f.px.subarray(skip), this.n);
    this.sz.set(f.sz.subarray(skip), this.n);
    this.side.set(f.side.subarray(skip), this.n);
    let cv = this.n ? this.cv[this.n - 1] : 0, cd = this.n ? this.cd[this.n - 1] : 0;
    for (let i = this.n; i < this.n + k; i++) {
      const z = this.sz[i]; cv += z; cd += this.side[i] === 1 ? z : this.side[i] === 2 ? -z : 0;
      this.cv[i] = cv; this.cd[i] = cd;
      if (z >= Tape.BIG_MIN) this.big.push(i);
    }
    this.n += k;
    this.seq = f.seq0 + f.n;
    return k;
  }
  get last() { return this.n ? this.n - 1 : -1; }
  lastPx() { return this.n ? this.px[this.n - 1] : NaN; }
  lastTs() { return this.n ? this.ts[this.n - 1] : NaN; }
}

// ---------------------------------------------------------------- bars ---

/** One bar. `fp` maps tickIdx -> [bidVol, askVol] (sell-aggressor, buy-aggressor). */
export function newBar(t, price, i) {
  return { t, te: t, o: price, h: price, l: price, c: price,
           v: 0, bv: 0, av: 0, d: 0, n: 0, cvd: 0, cvdo: 0, vwap: NaN, vsd: NaN,
           wvwap: NaN, wvsd: NaN,
           fp: new Map(), poc: 0, maxCell: 0, i0: i, i1: i, seeded: false };
}

export function parseTf(key) {
  const m = /^(\d+)(s|m|h|t|r)$/.exec(key);
  if (!m) return { kind: 'time', ms: 60000, key: '1m' };
  const n = +m[1];
  if (m[2] === 't') return { kind: 'tick', n, key };
  if (m[2] === 'r') return { kind: 'range', pts: n, key };
  return { kind: 'time', ms: n * { s: 1000, m: 60000, h: 3600000 }[m[2]], key };
}

export class BarBuilder {
  constructor(tape, tfKey) {
    this.tape = tape;
    this.tf = parseTf(tfKey);
    this.key = tfKey;
    this.bars = [];
    this.cursor = 0;
    this.cvd = 0;
    this.version = 0;          // bumps when a bar CLOSES (structure changed)
    this.tick = 0;             // bumps on every print (last bar changed)
    this.seedEnd = -Infinity;
    // pv2 carries sum(v * tp^2) so the standard deviation comes out of the same pass;
    // Mark trades the +/-1, 2 and 3 deviation bands off VWAP (2026-09-16).
    this.vw = { start: NaN, end: NaN, pv: 0, v: 0, pv2: 0 };   // session VWAP accumulator
    // WEEKLY (2026-09-16, Mark: "adding weekly vwap onto the chart"). Anchored at the
    // Sunday 22:00 UTC reopen and never reset by a session boundary, so it is the mean the
    // whole week has traded around. He weights it ABOVE the session one when the points of
    // interest are further out, which is why it gets its own line rather than a toggle.
    this.wk = { start: NaN, pv: 0, v: 0, pv2: 0 };
  }
  /** Reset the VWAP anchor when a bar opens in a new session (RTH / ETH). */
  anchor(t) {
    const w = this.vw;
    if (!(t >= w.start && t < w.end)) { const s = sessionOf(t); w.start = s.start; w.end = s.end; w.pv = 0; w.v = 0; w.pv2 = 0; }
    const k = this.wk;
    const ws = weekStart(t);
    if (!(k.start === ws)) { k.start = ws; k.pv = 0; k.v = 0; k.pv2 = 0; }
  }

  /** Align a timestamp to this timeframe on the Pacific clock. */
  align(ms) {
    const ms_ = this.tf.ms;
    if (ms_ >= 3600000 || ms_ === 1800000 || ms_ === 900000 || ms_ === 300000 || ms_ === 120000 || ms_ === 180000) {
      const off = ptOffsetMs(ms);
      return Math.floor((ms + off) / ms_) * ms_ - off;
    }
    return Math.floor(ms / ms_) * ms_;
  }

  /** Prior-session 1-minute bars [ts,o,h,l,c,v]. Time bars >= 1m fold them in. */
  seed(rows) {
    if (this.tf.kind !== 'time' || this.tf.ms < 60000 || !rows.length) return;
    if (this.bars.length && !this.bars[this.bars.length - 1].seeded) return; // tape already started
    for (const r of rows) {
      const t = this.align(r[0]);
      let b = this.bars[this.bars.length - 1];
      if (!b || b.t !== t) {
        if (b && t < b.t) continue;
        b = newBar(t, r[1], -1); b.seeded = true; b.fp = null;
        this.bars.push(b);
        this.anchor(t); b.sess = this.vw.start;
      }
      b.h = Math.max(b.h, r[2]); b.l = Math.min(b.l, r[3]); b.c = r[4]; b.v += r[5]; b.te = r[0];
      { const tp = (r[2] + r[3] + r[4]) / 3;
        this.vw.pv += tp * r[5]; this.vw.pv2 += tp * tp * r[5]; this.vw.v += r[5];
        b.vwap = this.vw.v ? this.vw.pv / this.vw.v : NaN;
        b.vsd = this.vw.v ? Math.sqrt(Math.max(0, this.vw.pv2 / this.vw.v - b.vwap * b.vwap)) : NaN;
        this.wk.pv += tp * r[5]; this.wk.pv2 += tp * tp * r[5]; this.wk.v += r[5];
        b.wvwap = this.wk.v ? this.wk.pv / this.wk.v : NaN;
        b.wvsd = this.wk.v ? Math.sqrt(Math.max(0, this.wk.pv2 / this.wk.v - b.wvwap * b.wvwap)) : NaN; }
    }
    this.seedEnd = rows[rows.length - 1][0];
    this.version++;
  }

  /** Fold every unseen print. Returns true if the structure changed. */
  update() {
    const tp = this.tape, n = tp.n;
    if (this.cursor >= n) return false;
    const { ts, px, sz, side } = tp;
    let b = this.bars[this.bars.length - 1];
    const kind = this.tf.kind;
    let structural = false;
    for (let i = this.cursor; i < n; i++) {
      const t = ts[i], p = px[i], s = sz[i], sd = side[i];
      let open = false;
      if (!b || b.seeded) open = true;
      else if (kind === 'time') open = t >= b.t + this.tf.ms;
      else if (kind === 'tick') open = b.n >= this.tf.n;
      else open = (Math.max(b.h, p) - Math.min(b.l, p)) > this.tf.pts + 1e-9;
      if (open) {
        if (b && b.seeded && kind === 'time') {
          // the tape starts inside a seeded minute: continue that bar live
          const t0 = this.align(t);
          if (b.t === t0) { b.seeded = false; b.fp = new Map(); b.i0 = i; open = false; }
        }
        if (open) {
          const t0 = kind === 'time' ? this.align(t) : t;
          b = newBar(t0, p, i);
          b.cvdo = this.cvd; b.cvd = this.cvd;
          this.bars.push(b);
          this.anchor(t0); b.sess = this.vw.start;
          structural = true;
        }
      }
      // fold the print
      if (p > b.h) b.h = p; if (p < b.l) b.l = p;
      b.c = p; b.te = t; b.v += s; b.n++; b.i1 = i;
      this.vw.pv += p * s; this.vw.pv2 += p * p * s; this.vw.v += s;
      b.vwap = this.vw.pv / this.vw.v;
      b.vsd = Math.sqrt(Math.max(0, this.vw.pv2 / this.vw.v - b.vwap * b.vwap));
      this.wk.pv += p * s; this.wk.pv2 += p * p * s; this.wk.v += s;
      b.wvwap = this.wk.pv / this.wk.v;
      b.wvsd = Math.sqrt(Math.max(0, this.wk.pv2 / this.wk.v - b.wvwap * b.wvwap));
      if (sd === 1) { b.av += s; b.d += s; this.cvd += s; }
      else if (sd === 2) { b.bv += s; b.d -= s; this.cvd -= s; }
      b.cvd = this.cvd;
      if (b.fp) {
        const k = tickOf(p);
        let cell = b.fp.get(k);
        if (!cell) { cell = [0, 0]; b.fp.set(k, cell); }
        if (sd === 2) cell[0] += s; else if (sd === 1) cell[1] += s; else { cell[0] += s / 2; cell[1] += s / 2; }
        const tot = cell[0] + cell[1];
        if (tot > b.maxCell) { b.maxCell = tot; b.poc = k; }
      }
    }
    this.cursor = n;
    this.tick++;
    if (structural) this.version++;
    return structural;
  }

  get last() { return this.bars[this.bars.length - 1]; }

  /** Index of the bar containing timestamp ms (binary search on start times). */
  indexAt(ms) {
    const a = this.bars;
    let lo = 0, hi = a.length - 1;
    if (!a.length) return -1;
    if (ms < a[0].t) return -1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (a[mid].t <= ms) lo = mid; else hi = mid - 1; }
    return lo;
  }
}

// ----------------------------------------------------------------- TPO ---

export const LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

/**
 * Sessions on the Pacific clock. RTH is 06:30-13:00; everything else is the
 * Globex session ("ETH"), keyed by the trading day it belongs to, which runs
 * from the 13:00 cash close through the 14:00-15:00 halt to the next 06:30.
 */
export function sessionOf(ms) {
  const q = ptParts(ms);
  const mins = q.h * 60 + q.m;
  const dayMs = Date.UTC(+q.ymd.slice(0, 4), +q.ymd.slice(5, 7) - 1, +q.ymd.slice(8, 10));
  const off = ptOffsetMs(ms);
  const at = m => dayMs + m * 60000 - off;          // PT clock minutes -> ms
  if (mins >= 390 && mins < 780) return { kind: 'RTH', day: q.ymd, start: at(390), end: at(780) };
  if (mins >= 780) {
    // evening: belongs to the NEXT trading day; ends at 06:30 tomorrow
    return { kind: 'ETH', day: nextDay(q.ymd), start: at(780), end: at(780 + 1050) };
  }
  return { kind: 'ETH', day: q.ymd, start: at(780 - 1440), end: at(390) };
}
/** The Sunday 22:00 UTC reopen the week runs from, as an epoch ms. */
export function weekStart(ms) {
  const d = new Date(ms);
  const t = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 22, 0, 0);
  let anchor = ms >= t ? t : t - 86400000;
  while (new Date(anchor).getUTCDay() !== 0) anchor -= 86400000;   // 0 = Sunday
  return anchor;
}

function nextDay(ymd) {
  const d = new Date(Date.UTC(+ymd.slice(0, 4), +ymd.slice(5, 7) - 1, +ymd.slice(8, 10) + 1));
  return d.toISOString().slice(0, 10);
}

export class TpoBuilder {
  /** Consumes a 1-minute BarBuilder. rowPts: points per TPO row. */
  constructor(minutes, rowPts = 2.5, periodMin = 30) {
    this.src = minutes;
    this.rowPts = rowPts;
    this.periodMs = periodMin * 60000;
    this.sessions = [];
    this.cursor = 0;
    this.version = 0;
    this.lastSig = '';
  }
  rowOf(p) { return Math.floor(p / this.rowPts + 1e-9); }

  update() {
    const bars = this.src.bars;
    if (!bars.length) return false;
    // re-apply the forming minute every time; sets are idempotent
    const from = Math.max(0, Math.min(this.cursor, bars.length - 1));
    let changed = false;
    for (let i = from; i < bars.length; i++) {
      const b = bars[i];
      const s = this.sessionFor(b.t);
      const per = Math.floor((b.t - s.start) / this.periodMs);
      const r0 = this.rowOf(b.l), r1 = this.rowOf(b.h);
      for (let r = r0; r <= r1; r++) {
        let arr = s.rows.get(r);
        if (!arr) { arr = []; s.rows.set(r, arr); changed = true; }
        if (arr[arr.length - 1] !== per) { arr.push(per); changed = true; s.total++; }
      }
      if (per >= s.periods) { s.periods = per + 1; changed = true; }
      if (b.h > s.hi) { s.hi = b.h; changed = true; }
      if (b.l < s.lo) { s.lo = b.l; changed = true; }
      if (per < 2) { s.ibHi = Math.max(s.ibHi, b.h); s.ibLo = Math.min(s.ibLo, b.l); }
      s.close = b.c;
      if (!s.opened) { s.open = b.o; s.opened = true; }
    }
    this.cursor = bars.length - 1;
    if (changed) { for (const s of this.sessions) if (s.dirty) this.finish(s); this.version++; }
    return changed;
  }

  sessionFor(ms) {
    let s = this.sessions[this.sessions.length - 1];
    if (s && ms >= s.start && ms < s.end) { s.dirty = true; return s; }
    const d = sessionOf(ms);
    s = this.sessions.find(x => x.start === d.start);
    if (!s) {
      s = { ...d, rows: new Map(), periods: 0, hi: -Infinity, lo: Infinity, ibHi: -Infinity, ibLo: Infinity,
            total: 0, poc: NaN, vaHi: NaN, vaLo: NaN, singles: [], opened: false, dirty: true,
            id: `${d.day} ${d.kind}` };
      this.sessions.push(s);
      this.sessions.sort((a, b) => a.start - b.start);
    }
    s.dirty = true;
    return s;
  }

  /** POC, value area (70% of TPOs), single prints. */
  finish(s, vaPct = 0.70) {
    s.dirty = false;
    let best = -1, poc = NaN;
    const rows = [...s.rows.keys()].sort((a, b) => a - b);
    const cnt = r => (s.rows.get(r) || []).length;
    for (const r of rows) { const c = cnt(r); if (c > best || (c === best && Math.abs(r * this.rowPts - s.close) < Math.abs(poc * this.rowPts - s.close))) { best = c; poc = r; } }
    s.pocRow = poc; s.poc = (poc + 0.5) * this.rowPts;
    // value area: grow from POC absorbing the larger adjacent pair
    const target = s.total * vaPct;
    let lo = poc, hi = poc, acc = best;
    while (acc < target) {
      const up = cnt(hi + 1) + cnt(hi + 2), dn = cnt(lo - 1) + cnt(lo - 2);
      if (up === 0 && dn === 0) break;
      if (up >= dn) { hi += 2; acc += up; } else { lo -= 2; acc += dn; }
    }
    s.vaLo = lo * this.rowPts; s.vaHi = (hi + 1) * this.rowPts;
    s.singles = rows.filter(r => cnt(r) === 1 && r !== rows[0] && r !== rows[rows.length - 1]);
    s.rowList = rows;
  }
}

// --------------------------------------------------------------- book ---

/**
 * Ten-level depth sampled once per second (latest snapshot in that second).
 * Slots move with the touch: never combine a size with a different price.
 * This is sampled displayed liquidity, not a reconstruction of all book events.
 */
export class BookHistory {
  constructor() {
    this.cap = 4096;
    this.sec = new Float64Array(this.cap);
    this.px = new Float32Array(this.cap * 20);
    this.sz = new Uint32Array(this.cap * 20);
    this.n = 0;
    this.last = null;            // latest raw snapshot for the DOM ladder
    this.maxSz = 1;
    this.version = 0;
    this.stamp = 0;
  }
  grow() {
    const cap = this.cap * 2;
    const g = (old, C) => { const a = new C(cap * (old.length / this.cap)); a.set(old); return a; };
    this.sec = g(this.sec, Float64Array); this.px = g(this.px, Float32Array); this.sz = g(this.sz, Uint32Array);
    this.cap = cap;
  }
  push(snap) {
    if (!Number.isFinite(snap.ts) || (this.last && snap.ts < this.last.ts)) return;
    this.last = snap; this.stamp++;
    const s = Math.floor(snap.ts / 1000);
    let i = this.n - 1;
    if (i < 0 || this.sec[i] !== s) {
      if (i >= 0 && this.sec[i] > s) return;
      else {
        if (this.n >= 86400) {
          this.sec.copyWithin(0,3600,this.n);
          this.px.copyWithin(0,3600*20,this.n*20); this.sz.copyWithin(0,3600*20,this.n*20);
          this.n -= 3600; this.maxSz = 1;
          for (let k=0;k<this.n*20;k++) this.maxSz=Math.max(this.maxSz,this.sz[k]);
        }
        if (this.n >= this.cap) this.grow();
        i = this.n++; this.sec[i] = s;
        this.px.fill(0, i * 20, i * 20 + 20); this.sz.fill(0, i * 20, i * 20 + 20);
        this.version++;
      }
    }
    const o = i * 20;
    for (let k = 0; k < 20; k++) {
      const p = snap.px[k], z = snap.sz[k];
      const valid = Number.isFinite(p) && p > 0 && Number.isFinite(z) && z > 0;
      this.px[o + k] = valid ? p : 0; this.sz[o + k] = valid ? z : 0;
      if (z > this.maxSz) this.maxSz = z;
    }
  }
  /** First column index with sec >= s (binary search). */
  indexAt(s) {
    let lo = 0, hi = this.n;
    while (lo < hi) { const m = (lo + hi) >> 1; if (this.sec[m] < s) lo = m + 1; else hi = m; }
    return lo;
  }
}


// -------------------------------------------------------------- power ---

/**
 * Power trades: a burst of executed volume inside a short time window and a
 * narrow price range — the Bookmap / Quantower definition. Detected on a
 * sliding window over the tape with prefix sums, so every print costs O(1)
 * amortised. Consecutive qualifying windows merge into one zone.
 */
export class PowerTrades {
  constructor(tape, opts) { this.tape = tape; this.set(opts); }
  set({ windowSec, volume, zoneTicks, deltaPct, merge }) {
    this.W = windowSec * 1000; this.V = volume; this.Z = zoneTicks * TICK; this.D = deltaPct; this.merge = !!merge;
    this.zones = []; this.open = null; this.cursor = 0; this.j = 0;
    this.maxQ = []; this.minQ = [];        // monotonic deques of indices
    this.version = 0;
    this.best = 0; this.bestTight = 0;     // largest window volume seen: any height / within Z
  }
  update() {
    const tp = this.tape, n = tp.n, { ts, px, cv, cd } = tp;
    if (this.cursor >= n) return false;
    let changed = false;
    for (let i = this.cursor; i < n; i++) {
      const t = ts[i];
      while (this.j < i && t - ts[this.j] > this.W) this.j++;
      const j = this.j;
      // sliding max / min of price
      while (this.maxQ.length && this.maxQ[0] < j) this.maxQ.shift();
      while (this.minQ.length && this.minQ[0] < j) this.minQ.shift();
      while (this.maxQ.length && px[this.maxQ[this.maxQ.length - 1]] <= px[i]) this.maxQ.pop();
      while (this.minQ.length && px[this.minQ[this.minQ.length - 1]] >= px[i]) this.minQ.pop();
      this.maxQ.push(i); this.minQ.push(i);
      const hi = px[this.maxQ[0]], lo = px[this.minQ[0]];
      const vol = cv[i] - (j ? cv[j - 1] : 0);
      const tight = hi - lo <= this.Z + 1e-9;
      if (vol > this.best) this.best = vol;
      if (tight && vol > this.bestTight) this.bestTight = vol;
      const ok = vol >= this.V && tight;
      if (ok) {
        const z = this.open;
        if (z && ts[j] <= z.t1) {          // overlaps the open zone
          if (this.merge) { z.i1 = i; z.t1 = t; z.hi = Math.max(z.hi, hi); z.lo = Math.min(z.lo, lo); }
          else continue;                    // discrete events: one per burst, no aggregation
        } else {
          if (z) { this.finish(z); this.zones.push(z); changed = true; }
          this.open = { i0: j, i1: i, t0: ts[j], t1: t, hi, lo, vol: 0, delta: 0, pct: 0, kind: 'n' };
        }
        this.finish(this.open);
      } else if (this.open && t - this.open.t1 > this.W) {
        this.zones.push(this.open); this.open = null; changed = true;
      }
    }
    this.cursor = n;
    if (changed) this.version++;
    return changed;
  }
  finish(z) {
    const tp = this.tape;
    z.vol = tp.cv[z.i1] - (z.i0 ? tp.cv[z.i0 - 1] : 0);
    z.delta = tp.cd[z.i1] - (z.i0 ? tp.cd[z.i0 - 1] : 0);
    z.pct = z.vol ? Math.round(z.delta / z.vol * 100) : 0;
    z.buy = Math.round((z.vol + z.delta) / 2); z.sell = Math.round((z.vol - z.delta) / 2);
    z.secs = Math.max(0.1, (z.t1 - z.t0) / 1000);
    z.kind = z.pct >= this.D ? 'buy' : z.pct <= -this.D ? 'sell' : 'n';
  }
}

// -------------------------------------------------------------- store ---

export class Store {
  constructor() {
    this.tape = new Tape();
    this.book = new BookHistory();
    this.builders = new Map();
    this.seedRows = [];          // prior sessions' 1m bars, oldest first
    this.levels = null;
    this.status = {};
    this.hello = null;
    this.minutes = this.builder('1m');
    this.tpo = new TpoBuilder(this.minutes, 2.5);
    this.power = new PowerTrades(this.tape, { windowSec: 3, volume: 250, zoneTicks: 8, deltaPct: 60 });
    this.listeners = new Set();
    this.replayDone = false;
  }
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(ev, arg) { for (const fn of this.listeners) fn(ev, arg); }

  builder(key) {
    let b = this.builders.get(key);
    if (!b) {
      b = new BarBuilder(this.tape, key);
      b.seed(this.seedRows);
      b.update();
      this.builders.set(key, b);
      // keep memory honest: at most six builders alive besides the minutes
      if (this.builders.size > 7) {
        for (const [k, v] of this.builders) { if (k !== '1m' && k !== key && v !== this.active) { this.builders.delete(k); break; } }
      }
    }
    return b;
  }

  addSeed(doc) {
    // rows arrive per session, oldest first, and a session can arrive TWICE:
    // the server reloads its history when the trading day rolls (Sunday 17:00
    // Chicago turns Friday from "today" into a prior session) and resends every
    // day it holds. Merge by minute, the later copy winning, so a repeated day
    // does not double its volume; then re-seed every builder with no live bars.
    const by = new Map();
    for (const r of this.seedRows) by.set(r[0], r);
    for (const r of doc.rows) by.set(r[0], r);
    this.seedRows = [...by.values()].sort((a, b) => a[0] - b[0]);
    for (const b of this.builders.values()) {
      if (b.bars.length && !b.bars[b.bars.length - 1].seeded) continue; // live already — skip
      b.bars = []; b.seed(this.seedRows); b.update();
    }
    this.tpo.cursor = 0; this.tpo.sessions = []; this.tpo.update();
    this.emit('structure');
  }

  addTrades(frame) {
    const k = this.tape.push(frame);
    if (!k) return;
    let structural = false;
    for (const b of this.builders.values()) structural = b.update() || structural;
    if (this.tpo.update()) structural = true;
    if (this.power.update()) structural = true;
    this.emit(structural ? 'structure' : 'tick', frame);
  }

  addBook(snap) { this.book.push(snap); this.emit('book', snap); }

  setPower(opts) { this.power.set(opts); this.power.update(); this.emit('structure'); }

  setTpoRow(pts) {
    this.tpo = new TpoBuilder(this.minutes, pts);
    this.tpo.update();
    this.emit('structure');
  }
}
