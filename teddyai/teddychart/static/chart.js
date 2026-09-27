// chart.js â€” the renderer.
//
// EIGHT CANVASES, ONE SCHEDULER. Each layer owns a transparent canvas stacked
// in the plot: backdrop, heatmap, tpo, series, levels, chrome, live, cursor.
// A print invalidates only the `live` layer (the forming bar, the last-price
// line and tag, the forming bar's bubbles) and the cursor; a bar close
// repaints the series/tpo layers; only a viewport change repaints everything.
// All invalidations inside one frame coalesce into a single requestAnimationFrame
// at the highest level requested. That is the whole trick behind a chart that
// stays at the display's refresh rate while the tape runs at a few hundred
// prints a second: per tick, the work is a handful of draw calls.
//
// EVERYTHING IS ANCHORED TO PRICE AND LOGICAL BAR INDEX, never to pixels. A
// level, a TPO letter, a bubble and a candle all go through the same X()/Y(),
// recomputed per frame from the view, so they cannot drift apart.

import { TICK, tickOf, priceOf, ptClock, ptParts, ptOffsetMs, LETTERS, sessionOf } from './store.js';

export const LV = { NONE: 0, CURSOR: 1, LIVE: 2, CHROME: 3, STRUCT: 4, FULL: 5 };

const MONO = '"IBM Plex Mono", "Cascadia Mono", Consolas, monospace';
const UI = 'system-ui, -apple-system, "Segoe UI", sans-serif';

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function hexToRgb(h) {
  h = h.replace('#', '');
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
const rgba = (hex, a) => { const [r, g, b] = hexToRgb(hex); return `rgba(${r},${g},${b},${a})`; };
const fmt0 = n => Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
const fmt2 = n => Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = n => Math.abs(n) >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(Math.round(n));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

class Layer {
  constructor(name, threshold) {
    this.name = name; this.threshold = threshold;
    this.cv = document.createElement('canvas');
    this.cv.className = 'layer layer-' + name;
    this.ctx = this.cv.getContext('2d');
  }
  size(W, H, dpr) {
    this.cv.width = Math.round(W * dpr); this.cv.height = Math.round(H * dpr);
    this.cv.style.width = W + 'px'; this.cv.style.height = H + 'px';
  }
  begin(dpr) { const c = this.ctx; c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, this.cv.width, this.cv.height); return c; }
}

export class Chart {
  constructor(root, store, settings) {
    this.root = root; this.store = store; this.S = settings;
    this.layers = {};
    // z-order = this order. `live` sits ABOVE `chrome` (2026-09-07): chrome fills the price gutter with an
    // opaque panel, and the last-price tag is the one thing live draws into that gutter -- below chrome it
    // was painted over and never seen. Everything else live draws is clipped to the plot, so nothing else moves.
    for (const [n, th] of [['backdrop', LV.FULL], ['heatmap', LV.LIVE], ['tpo', LV.STRUCT], ['series', LV.STRUCT],
                           ['levels', LV.FULL], ['chrome', LV.CHROME], ['live', LV.LIVE], ['cursor', LV.CURSOR]]) {
      const l = new Layer(n, th); this.layers[n] = l; root.appendChild(l.cv);
    }
    this.view = { i0: 0, spacing: 9 };          // logical index at the left edge, px per bar
    // FOLLOW = atEnd && !manualY: the right edge pinned to the newest bar AND the price
    // scale auto-fitted to the visible bars. Either half dropping out is what reads as
    // "the chart stopped following the candles" (2026-09-15): a slightly diagonal drag
    // used to hand-hold the price scale for good, so the candles walked off the top of a
    // chart whose time axis was still live. The topbar toggle, `L`, End, double-click
    // and dragging back to the live edge all restore both halves; `onFollow(bool)` is
    // called whenever the state flips so the button can show it.
    this.atEnd = true;
    this.manualY = null;                          // {mid, half, src} when the price axis is hand-held
    this.onFollow = null; this._following = true;
    this.cursor = null;
    this.dirty = LV.NONE; this.raf = 0;
    this.glide = null;                            // {from, to, t0}
    this.fps = { n: 0, t: performance.now(), v: 0 };
    this.heat = { cv: document.createElement('canvas'), key: '', lut: null };
    this.theme = this.readTheme();
    this.hover = null;
    this.tfKey = settings.tf;
    this.builder = store.builder(this.tfKey);
    this.lastVersion = -1;
    this.dpr = window.devicePixelRatio || 1;
    this.bindEvents();
    new ResizeObserver(() => this.resize()).observe(root);
    this.resize();
  }

  // ------------------------------------------------------------ theme ---
  readTheme() {
    const t = {};
    for (const k of ['ground', 'panel', 'panel2', 'ink', 'ink2', 'ink3', 'rule', 'rule2', 'brass', 'brassx',
                     'neg', 'pos', 'posx', 'warn', 'vio']) t[k] = cssVar('--' + k) || '#888';
    t.dark = (() => { const [r, g, b] = hexToRgb(t.panel); return (r * 299 + g * 587 + b * 114) / 1000 < 128; })();
    this.heat.lut = null;
    return t;
  }
  retheme() { this.theme = this.readTheme(); this.heat.key = ''; this.invalidate(LV.FULL); }

  // ----------------------------------------------------------- layout ---
  resize() {
    const W = this.root.clientWidth, H = this.root.clientHeight;
    if (!W || !H) return;
    this.dpr = window.devicePixelRatio || 1;
    this.W = W; this.H = H;
    for (const l of Object.values(this.layers)) l.size(W, H, this.dpr);
    this.layout();
    this.heat.key = '';
    this.invalidate(LV.FULL);
  }
  layout() {
    const S = this.S, W = this.W, H = this.H;
    const axisW = 64, timeH = 22;
    const cvdH = S.cvdPane ? clamp(Math.round(H * S.cvdPaneFrac), 60, 260) : 0;
    const fpFooter = (S.style === 'footprint' && S.fpFooter) ? 26 : 0;
    this.plot = { x: 0, y: 0, w: W - axisW, h: H - timeH };
    this.axis = { x: W - axisW, w: axisW };
    this.price = { y: 0, h: this.plot.h - cvdH - fpFooter, footer: fpFooter };
    this.cvd = cvdH ? { y: this.plot.h - cvdH, h: cvdH } : null;
    this.time = { y: H - timeH, h: timeH };
  }

  // ------------------------------------------------------- scheduling ---
  invalidate(level) {
    if (level > this.dirty) this.dirty = level;
    if (!this.raf) this.raf = requestAnimationFrame(() => this.frame());
  }
  frame() {
    this.raf = 0;
    let lvl = this.dirty; this.dirty = LV.NONE;
    if (lvl === LV.NONE) return;
    if (this.syncBuilder()) lvl = LV.FULL;
    this.prepare();
    const f = this.following();
    if (f !== this._following) { this._following = f; if (this.onFollow) this.onFollow(f); }
    // A tick can move the auto price range (new high) or the CVD range; when
    // it does, the cheap path is no longer honest and everything repaints.
    const rk = this.lo + ':' + this.hi + ':' + this.i0 + ':' + this.i1;
    if (rk !== this.rangeKey) { this.rangeKey = rk; lvl = LV.FULL; }
    if (this.cvd) { const r = this.cvdRange(), ck = r.lo + ':' + r.hi + ':' + r.dmax; if (ck !== this.cvdKey) { this.cvdKey = ck; if (lvl < LV.STRUCT) lvl = LV.STRUCT; } }
    const t0 = performance.now();
    for (const l of Object.values(this.layers)) if (lvl >= l.threshold) this['paint_' + l.name](l.begin(this.dpr));
    const dt = performance.now() - t0;
    this.fps.n++;
    if (t0 - this.fps.t > 1000) { this.fps.v = this.fps.n * 1000 / (t0 - this.fps.t); this.fps.n = 0; this.fps.t = t0; this.fps.ms = dt; }
    if (this.glide && performance.now() - this.glide.t0 < this.S.glideMs) this.invalidate(LV.LIVE);
    else this.glide = null;
    if (this.centering) this.invalidate(LV.LIVE);   // the centre is still easing; the range change makes it a full frame
  }

  syncBuilder() {
    if (this.tfKey !== this.S.tf) {
      this.tfKey = this.S.tf; this.builder = this.store.builder(this.tfKey);
      this.atEnd = true; this.manualY = null; this.heat.key = ''; this.centerMid = this.centerTarget = null;
      return true;
    }
    return false;
  }

  /** Data changed. ev: 'tick' | 'structure' | 'book' | 'levels'. */
  onData(ev) {
    if (ev === 'tick') {
      const b = this.builder.last;
      if (b && this.S.glide) {
        const from = this.glide ? this.glideValue() : (this.prevClose ?? b.c);
        if (from !== b.c) this.glide = { from, to: b.c, t0: performance.now() };
      }
      this.prevClose = b && b.c;
      this.invalidate(LV.LIVE);
    } else if (ev === 'structure') {
      if (this.atEnd) this.invalidate(LV.FULL); else this.invalidate(LV.STRUCT);
    } else if (ev === 'book') {
      this.invalidate(this.S.heatmap ? LV.LIVE : LV.NONE);
    } else this.invalidate(LV.FULL);
  }
  glideValue() {
    const g = this.glide; if (!g) return null;
    const k = clamp((performance.now() - g.t0) / this.S.glideMs, 0, 1);
    const e = 1 - Math.pow(1 - k, 3);
    return g.from + (g.to - g.from) * e;
  }

  // ------------------------------------------------------- coordinates ---
  prepare() {
    const bars = this.builder.bars, N = bars.length, v = this.view, P = this.plot;
    const visN = P.w / v.spacing;
    const pad = Math.min(this.S.rightPadBars, (P.w * 0.3) / v.spacing);
    if (this.atEnd) v.i0 = Math.max(-2, N - visN + pad);
    if (N === 0) v.i0 = 0;
    this.i0 = Math.max(0, Math.floor(v.i0));
    this.i1 = Math.min(N - 1, Math.ceil(v.i0 + visN));
    // price range
    const S = this.S;
    let lo = Infinity, hi = -Infinity;
    for (let i = this.i0; i <= this.i1; i++) { const b = bars[i]; if (b.l < lo) lo = b.l; if (b.h > hi) hi = b.h; }
    if (!isFinite(lo)) { const p = this.store.tape.lastPx() || 20000; lo = p - 50; hi = p + 50; }
    if (S.scaleToLevels && this.store.levels && this.store.levels.ok) {
      const L = this.store.levels, span = Math.max(hi - lo, 1), widen = p => {
        if (p == null) return;
        const nlo = Math.min(lo, p), nhi = Math.max(hi, p);
        if (nhi - nlo <= span * 3) { lo = nlo; hi = nhi; }
      };
      if (S.gReference) widen(L.spot);
      if (S.gRails) { widen(L.rail_hi); widen(L.rail_lo); }
      if (S.setups) for (const s of L.setups || []) { widen(s.entry); widen(s.stop); }
    }
    if (this.manualY) {
      this.lo = this.manualY.mid - this.manualY.half; this.hi = this.manualY.mid + this.manualY.half;
      this.centerMid = this.centerTarget = null; this.centering = false;
    } else {
      const m = (hi - lo) * 0.08 + 1;
      const last = (S.followCenter && this.atEnd && N) ? bars[N - 1].c : null;
      if (last != null && isFinite(last)) {
        // FOLLOW, CENTRED (2026-09-15, Mark: "it should follow but be centered"). The last
        // price is held at the middle of the pane. The centre re-targets only once price has
        // drifted more than followBand% of the half-height from it (0 = every tick), and
        // then eases there over a few frames rather than jumping; the half-height is
        // whatever keeps every visible bar on screen around that centre, so a trend can
        // no longer walk the candles off the top the way it did under the plain auto-fit.
        const band = clamp((S.followBand == null ? 20 : S.followBand) / 100, 0, 0.8);
        if (this.centerMid == null || !isFinite(this.centerMid)) this.centerMid = this.centerTarget = last;
        const h0 = Math.max(hi - this.centerMid, this.centerMid - lo) + m;
        if (Math.abs(last - this.centerMid) > band * h0) this.centerTarget = last;
        const d = this.centerTarget - this.centerMid;
        this.centerMid += Math.abs(d) < 0.05 ? d : d * 0.3;
        this.centering = this.centerMid !== this.centerTarget;
        const mid = this.centerMid, half = Math.max(hi - mid, mid - lo) + m;
        this.lo = mid - half; this.hi = mid + half;
      } else {
        this.centerMid = this.centerTarget = null; this.centering = false;
        this.lo = lo - m; this.hi = hi + m;
      }
    }
    this.autoRange = { lo, hi };
  }
  X(l) { return this.plot.x + (l - this.view.i0 + 0.5) * this.view.spacing; }
  Lof(x) { return (x - this.plot.x) / this.view.spacing + this.view.i0 - 0.5; }
  Y(p) { const pr = this.price; return pr.y + (this.hi - p) / (this.hi - this.lo) * pr.h; }
  Pof(y) { const pr = this.price; return this.hi - (y - pr.y) / pr.h * (this.hi - this.lo); }
  /** timestamp -> x, interpolated inside the bar that contains it. */
  tsToX(ms) {
    const bars = this.builder.bars, i = this.builder.indexAt(ms);
    if (i < 0) return this.X(-1);
    const b = bars[i], tf = this.builder.tf;
    let span = tf.kind === 'time' ? tf.ms : Math.max(1, (bars[i + 1] ? bars[i + 1].t : b.te + 1) - b.t);
    if (tf.kind !== 'time' && i === bars.length - 1) span = Math.max(span, 1);
    return this.X(i + clamp((ms - b.t) / span, 0, 0.999) - 0.5 + 0.5 * (tf.kind === 'time' ? 0 : 0));
  }
  cvdY(v, lo, hi) { const c = this.cvd; return c.y + 4 + (hi - v) / Math.max(hi - lo, 1e-9) * (c.h - 8); }

  // --------------------------------------------------------- backdrop ---
  paint_backdrop(c) {
    const T = this.theme, P = this.plot, S = this.S;
    c.fillStyle = T.panel; c.fillRect(0, 0, this.W, this.H);
    const bars = this.builder.bars;
    // session shading: RTH gets the flat panel, everything else a wash
    if (S.sessionShade && bars.length) {
      c.fillStyle = rgba(T.panel2, T.dark ? 0.55 : 0.7);
      let run = null;
      for (let i = this.i0; i <= this.i1; i++) {
        const q = ptParts(bars[i].t), m = q.h * 60 + q.m, eth = m < 390 || m >= 780;
        if (eth && run === null) run = i;
        if ((!eth || i === this.i1) && run !== null) { const x0 = this.X(run - 0.5), x1 = this.X((eth ? i : i - 1) + 0.5); c.fillRect(x0, P.y, x1 - x0, P.h); run = null; }
      }
    }
    if (S.grid) {
      c.strokeStyle = rgba(T.rule, 0.7); c.lineWidth = 1;
      const step = this.priceStep();
      for (let p = Math.ceil(this.lo / step) * step; p < this.hi; p += step) { const y = Math.round(this.Y(p)) + 0.5; c.beginPath(); c.moveTo(P.x, y); c.lineTo(P.x + P.w, y); c.stroke(); }
      for (const t of this.timeTicks()) { const x = Math.round(t.x) + 0.5; c.beginPath(); c.moveTo(x, P.y); c.lineTo(x, P.y + P.h); c.stroke(); }
    }
    if (this.cvd) { c.strokeStyle = T.rule2; c.beginPath(); c.moveTo(0, this.cvd.y + 0.5); c.lineTo(this.W, this.cvd.y + 0.5); c.stroke(); }
  }
  priceStep() {
    const span = this.hi - this.lo, px = this.price.h;
    const want = span / Math.max(2, px / 44);
    const dec = Math.pow(10, Math.floor(Math.log10(Math.max(want, 1e-6))));
    return dec * ([1, 2, 2.5, 5, 10].find(m => dec * m >= want) || 10);
  }
  timeTicks() {
    const bars = this.builder.bars, out = [], tf = this.builder.tf;
    if (!bars.length || this.i1 < this.i0) return out;
    const span = Math.max(1, this.i1 - this.i0);
    const msPerBar = tf.kind === 'time' ? tf.ms : Math.max(1000, (bars[this.i1].t - bars[this.i0].t) / span);
    const pxPerMs = this.view.spacing / msPerBar;
    const STEPS = [1e3, 5e3, 15e3, 30e3, 6e4, 3e5, 9e5, 18e5, 36e5, 72e5, 144e5, 864e5];
    const step = STEPS.find(st => st * pxPerMs >= 84) || 864e5;
    const secs = step < 6e4;
    let lastDay = null, lastBucket = null;
    for (let i = this.i0; i <= this.i1; i++) {
      const b = bars[i], q = ptParts(b.t);
      const off = ptOffsetMs(b.t), bucket = Math.floor((b.t + off) / step);
      const newDay = lastDay !== null && q.ymd !== lastDay;
      if (lastBucket !== null && (bucket !== lastBucket || newDay)) {
        out.push({ x: this.X(i), label: newDay ? `${q.dow} ${q.ymd.slice(5)}` : ptClock(bucket * step - off, secs), major: newDay });
      }
      lastDay = q.ymd; lastBucket = bucket;
    }
    return out;
  }

  // ---------------------------------------------------------- heatmap ---
  heatLut() {
    if (this.heat.lut) return this.heat.lut;
    const T = this.theme, lut = new Uint8ClampedArray(256 * 4);
    // dark: ink -> violet -> brass -> warm white ; light: panel -> violet -> brass -> neg
    const stops = T.dark
      ? [[0, [0, 0, 0, 0]], [0.12, [...hexToRgb(T.vio), 90]], [0.45, [...hexToRgb(T.brass), 200]], [0.8, [...hexToRgb('#F4D27A'), 240]], [1, [255, 250, 230, 255]]]
      : [[0, [0, 0, 0, 0]], [0.12, [...hexToRgb(T.vio), 70]], [0.45, [...hexToRgb(T.brass), 170]], [0.8, [...hexToRgb(T.neg), 220]], [1, [...hexToRgb('#4A1010'), 255]]];
    for (let i = 0; i < 256; i++) {
      const u = i / 255; let k = 0;
      while (k < stops.length - 2 && u > stops[k + 1][0]) k++;
      const [a0, c0] = stops[k], [a1, c1] = stops[k + 1], f = (u - a0) / (a1 - a0);
      for (let j = 0; j < 4; j++) lut[i * 4 + j] = c0[j] + (c1[j] - c0[j]) * f;
    }
    return this.heat.lut = lut;
  }
  paint_heatmap(c) {
    const S = this.S, B = this.store.book, H = this.heat;
    if (!S.heatmap || !B.n) return;
    const bars = this.builder.bars; if (!bars.length) return;
    const P = this.plot, pr = this.price;
    // rows: one per tick across the visible price range (capped for zoomed-out views)
    const tickLo = Math.floor(this.lo / TICK), tickHi = Math.ceil(this.hi / TICK);
    let rowTicks = 1; while ((tickHi - tickLo) / rowTicks > 2400) rowTicks *= 2;
    const rows = Math.ceil((tickHi - tickLo) / rowTicks);
    const cols = Math.max(1, Math.round(P.w));
    const key = `${this.view.i0.toFixed(3)}:${this.view.spacing}:${tickLo}:${tickHi}:${cols}:${S.heatGain}:${S.heatFloor}:${S.heatRef}:${S.heatRef === 0 ? B.maxSz : 0}:${rowTicks}:${this.tfKey}:${this.theme.dark}:${Math.floor((B.last?.ts || 0)/1000)}:${this.builder.tf.kind === "time" ? 0 : bars[bars.length - 1].te}`;
    const lut = this.heatLut();
    const tsLo = bars[this.i0].t, tsHi = bars[this.i1 + 1]?.t ?? Math.max(bars[this.i1].te, this.store.now());
    const gain = S.heatGain, floor = S.heatFloor;
    const norm = Math.max(1, S.heatRef === 0 ? B.maxSz : S.heatRef);
    const full = key !== H.key;
    if (full) { H.cv.width = cols; H.cv.height = rows; H.key = key; H.img = H.ctx = null; H.ctx = H.cv.getContext('2d'); H.img = H.ctx.createImageData(cols, rows); H.doneSec = -1; }
    const data = H.img.data;
    // fold seconds into pixel columns: max per (col,row)
    let k0 = full ? B.indexAt(Math.floor(tsLo / 1000)) : B.indexAt(H.doneSec);
    let x0 = Infinity, x1 = -Infinity;
    for (let k = k0; k < B.n; k++) {
      const sec = B.sec[k];
      const ms = sec * 1000; if (ms > tsHi) break;
      const x = Math.floor(this.tsToX(ms) - P.x), xe = Math.min(cols - 1, Math.floor(this.tsToX(ms + 1000) - P.x));
      if (xe < 0 || x >= cols) continue;
      const o = k * 20;
      for (let j = 0; j < 20; j++) {
        const sz = B.sz[o + j]; if (!sz) continue;
        const r = rows - 1 - Math.floor((Math.round(B.px[o + j] / TICK) - tickLo) / rowTicks);
        if (r < 0 || r >= rows) continue;
        let u = Math.log1p(sz / norm * gain * 8) / Math.log1p(8);   // log response; walls stay readable
        if (u < floor) continue;
        const v = Math.min(255, Math.round(u * 255));
        for (let xx = Math.max(0, x); xx <= xe; xx++) {
          const p = (r * cols + xx) * 4;
          if (data[p + 3] < lut[v * 4 + 3]) { data[p] = lut[v * 4]; data[p + 1] = lut[v * 4 + 1]; data[p + 2] = lut[v * 4 + 2]; data[p + 3] = lut[v * 4 + 3]; }
          if (xx < x0) x0 = xx; if (xx > x1) x1 = xx;
        }
      }
      H.doneSec = sec;
    }
    if (isFinite(x0)) H.ctx.putImageData(H.img, 0, 0, x0, 0, x1 - x0 + 1, rows);
    else if (full) H.ctx.putImageData(H.img, 0, 0);
    c.save(); c.beginPath(); c.rect(P.x, pr.y, P.w, pr.h); c.clip();
    c.imageSmoothingEnabled = false; c.globalAlpha = S.heatAlpha;
    const yTop = this.Y(tickHi * TICK), yBot = this.Y(tickLo * TICK);
    c.drawImage(H.cv, P.x, yTop, P.w, yBot - yTop);
    c.restore();
  }

  // -------------------------------------------------------------- TPO ---
  paint_tpo(c) {
    const S = this.S; if (!S.tpo) return;
    const T = this.theme, tpo = this.store.tpo, bars = this.builder.bars;
    if (!bars.length || !tpo.sessions.length) return;
    const pr = this.price, rowH = tpo.rowPts / (this.hi - this.lo) * pr.h;
    c.save(); c.beginPath(); c.rect(this.plot.x, pr.y, this.plot.w, pr.h); c.clip();
    const sessions = tpo.sessions.filter(s => S.tpoEth || s.kind === 'RTH').slice(-S.tpoSessions);
    const cellW = clamp(S.tpoCellPx, 4, 24);
    const font = `${clamp(Math.floor(Math.min(rowH * 0.9, cellW * 1.3)), 6, 13)}px ${MONO}`;
    for (const s of sessions) {
      if (s.dirty) tpo.finish(s);
      const xi = this.builder.indexAt(s.start);
      const xs = xi < 0 ? this.X(-1) : this.X(Math.max(xi, 0) - 0.5) + 2;
      const xEndIdx = this.builder.indexAt(Math.min(s.end, bars[bars.length - 1].te)) ;
      const xe = this.X(Math.max(xEndIdx, xi) + 0.5);
      if (xe < this.plot.x || xs > this.plot.x + this.plot.w) continue;
      const rows = s.rowList || [];
      const alpha = s === sessions[sessions.length - 1] ? S.tpoAlpha : S.tpoAlpha * 0.7;
      // value area wash
      if (S.tpoVA && isFinite(s.vaHi)) {
        c.fillStyle = rgba(s.kind === 'RTH' ? T.brass : T.vio, 0.07);
        c.fillRect(xs, this.Y(s.vaHi), Math.max(0, xe - xs), this.Y(s.vaLo) - this.Y(s.vaHi));
      }
      // letters / blocks
      c.font = font; c.textBaseline = 'middle'; c.textAlign = 'center';
      for (const r of rows) {
        const arr = s.rows.get(r), y = this.Y((r + 1) * tpo.rowPts), yb = this.Y(r * tpo.rowPts);
        if (yb < pr.y || y > pr.y + pr.h) continue;
        const h = yb - y;
        for (let k = 0; k < arr.length; k++) {
          const per = arr[k], x = xs + k * cellW;
          if (x > this.plot.x + this.plot.w) break;
          const ib = per < 2, single = S.tpoSingles && arr.length === 1 && s.singles.includes(r);
          const col = single ? T.neg : ib ? T.warn : (s.kind === 'RTH' ? T.ink2 : T.vio);
          if (S.tpoLetters && rowH >= 7 && cellW >= 6) {
            c.fillStyle = rgba(col, alpha); c.fillText(LETTERS[per] || 'Â·', x + cellW / 2, y + h / 2 + 0.5);
          } else {
            c.fillStyle = rgba(col, alpha * 0.55); c.fillRect(x + 0.5, y + 0.5, cellW - 1, Math.max(1, h - 1));
          }
        }
        if (S.tpoPoc && r === s.pocRow) { c.fillStyle = rgba(T.brass, 0.9); c.fillRect(xs - 2, y + h / 2 - 0.75, Math.max(xe - xs + 2, arr.length * cellW + 2), 1.5); }
      }
      // IB bracket
      if (S.tpoIB && isFinite(s.ibHi)) {
        c.strokeStyle = rgba(T.warn, 0.7); c.setLineDash([2, 3]); c.lineWidth = 1;
        for (const p of [s.ibHi, s.ibLo]) { const y = Math.round(this.Y(p)) + 0.5; c.beginPath(); c.moveTo(xs, y); c.lineTo(xe, y); c.stroke(); }
        c.setLineDash([]);
      }
      // label
      c.font = `600 10px ${MONO}`; c.textAlign = 'left'; c.textBaseline = 'top'; c.fillStyle = rgba(T.ink3, 0.9);
      const tag = `${s.kind} ${s.day.slice(5)}  POC ${fmt0(s.poc)}  VA ${fmt0(s.vaLo)}â€“${fmt0(s.vaHi)}`;
      c.fillText(tag, xs + 2, this.Y(s.hi) - 13 < pr.y + 2 ? pr.y + 2 : this.Y(s.hi) - 13);
    }
    c.restore();
  }

  // ------------------------------------------------------------ series ---
  /** Draws one bar (candle or footprint) plus its bubbles. Shared by series and live. */
  drawBar(c, b, i, isLast) {
    const S = this.S, T = this.theme, v = this.view, sp = v.spacing;
    const x = this.X(i), bw = Math.max(1, sp * (S.style === 'footprint' ? 0.92 : 0.66));
    const close = isLast && this.glide ? this.glideValue() : b.c;
    const up = close >= b.o, col = up ? T.pos : T.neg;
    const yH = this.Y(Math.max(b.h, close)), yL = this.Y(Math.min(b.l, close));
    const footprint = S.style === 'footprint' && sp >= S.fpMinPx && b.fp;
    if (footprint) {
      this.drawFootprint(c, b, i, x, bw, close);
    } else {
      c.strokeStyle = col; c.fillStyle = col; c.lineWidth = 1;
      c.beginPath(); c.moveTo(Math.round(x) + 0.5, yH); c.lineTo(Math.round(x) + 0.5, yL); c.stroke();
      const y0 = this.Y(Math.max(b.o, close)), y1 = this.Y(Math.min(b.o, close));
      if (bw < 2) { c.beginPath(); c.moveTo(Math.round(x) + 0.5, y0); c.lineTo(Math.round(x) + 0.5, Math.max(y1, y0 + 1)); c.stroke(); }
      else { if (S.hollowUp && up) { c.fillStyle = T.panel; c.fillRect(x - bw / 2, y0, bw, Math.max(1, y1 - y0)); c.strokeRect(x - bw / 2 + 0.5, y0 + 0.5, bw - 1, Math.max(1, y1 - y0) - 1); }
             else c.fillRect(x - bw / 2, y0, bw, Math.max(1, y1 - y0)); }
      if (S.deltaOnCandles && sp >= 18 && !b.seeded) {
        c.font = `9px ${MONO}`; c.textAlign = 'center'; c.textBaseline = 'bottom';
        c.fillStyle = b.d >= 0 ? T.pos : T.neg; c.fillText((b.d > 0 ? '+' : '') + fmtK(b.d), x, yH - 2);
      }
    }
  }

  drawFootprint(c, b, i, x, bw, close) {
    const S = this.S, T = this.theme, rowT = S.fpRowTicks, pr = this.price;
    const rowH = rowT * TICK / (this.hi - this.lo) * pr.h;
    const x0 = x - bw / 2;
    // aggregate cells to rows
    const rows = new Map();
    for (const [k, cell] of b.fp) { const r = Math.floor(k / rowT); let a = rows.get(r); if (!a) { a = [0, 0]; rows.set(r, a); } a[0] += cell[0]; a[1] += cell[1]; }
    let maxTot = 1; for (const a of rows.values()) maxTot = Math.max(maxTot, a[0] + a[1]);
    const showText = rowH >= 8 && bw >= 40;
    const font = `${clamp(Math.floor(Math.min(rowH * 0.85, 11)), 7, 11)}px ${MONO}`;
    // body outline behind the cells
    const up = close >= b.o;
    const y0 = this.Y(Math.max(b.o, close)), y1 = this.Y(Math.min(b.o, close));
    c.strokeStyle = rgba(up ? T.pos : T.neg, 0.9); c.lineWidth = 1;
    c.beginPath(); c.moveTo(Math.round(x) + 0.5, this.Y(b.h)); c.lineTo(Math.round(x) + 0.5, y0); c.moveTo(Math.round(x) + 0.5, y1); c.lineTo(Math.round(x) + 0.5, this.Y(b.l)); c.stroke();
    c.strokeRect(Math.round(x0) + 0.5, Math.round(y0) + 0.5, Math.round(bw) - 1, Math.max(1, Math.round(y1 - y0) - 1));
    const half = bw / 2;
    const ratio = S.imbRatio, minV = S.imbMinVol;
    for (const [r, a] of rows) {
      const yTop = this.Y((r + 1) * rowT * TICK), yBot = this.Y(r * rowT * TICK), h = yBot - yTop;
      if (yBot < pr.y || yTop > pr.y + pr.h) continue;
      const [bid, ask] = a, tot = bid + ask;
      // cell shading by volume
      if (S.fpShade) {
        const u = tot / maxTot;
        c.fillStyle = rgba(T.neg, 0.06 + 0.35 * u * (bid / (tot || 1))); c.fillRect(x0, yTop, half, h);
        c.fillStyle = rgba(T.pos, 0.06 + 0.35 * u * (ask / (tot || 1))); c.fillRect(x0 + half, yTop, half, h);
      }
      // diagonal imbalances: ask(r) vs bid(r-1); bid(r) vs ask(r+1)
      if (S.imbalance) {
        const below = rows.get(r - 1), above = rows.get(r + 1);
        if (below && ask >= minV && ask >= ratio * Math.max(below[0], 1)) { c.fillStyle = rgba(T.pos, 0.55); c.fillRect(x0 + half, yTop, half, h); }
        if (above && bid >= minV && bid >= ratio * Math.max(above[1], 1)) { c.fillStyle = rgba(T.neg, 0.55); c.fillRect(x0, yTop, half, h); }
      }
      if (S.fpPoc && r === Math.floor(b.poc / rowT)) { c.strokeStyle = rgba(T.brass, 0.95); c.lineWidth = 1.2; c.strokeRect(x0 + 0.5, yTop + 0.5, bw - 1, h - 1); }
      if (showText) {
        c.font = font; c.textBaseline = 'middle'; c.fillStyle = T.ink;
        c.textAlign = 'right'; c.fillText(fmtK(bid), x - 3, yTop + h / 2 + 0.5);
        c.textAlign = 'left'; c.fillText(fmtK(ask), x + 3, yTop + h / 2 + 0.5);
      }
    }
    // footer: delta and volume under the pane
    if (S.fpFooter && pr.footer) {
      const fy = pr.y + pr.h;
      c.font = `600 9.5px ${MONO}`; c.textAlign = 'center'; c.textBaseline = 'top';
      c.fillStyle = b.d >= 0 ? T.pos : T.neg; c.fillText((b.d > 0 ? '+' : '') + fmtK(b.d), x, fy + 2);
      c.fillStyle = T.ink3; c.fillText(fmtK(b.v), x, fy + 13);
    }
  }

  drawBubbles(c, b, barIndex) {
    const S = this.S, T = this.theme, tp = this.store.tape, big = tp.big;
    if (b.i0 < 0 || this.view.spacing < S.bubbleMinPx) return;
    const dense = clamp(this.view.spacing / 24, 0.45, 1);
    // find the first big index >= b.i0 by binary search
    let lo = 0, hi = big.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (big[m] < b.i0) lo = m + 1; else hi = m; }
    const thr = S.bigLot;
    for (let k = lo; k < big.length && big[k] <= b.i1; k++) {
      const i = big[k], sz = tp.sz[i]; if (sz < thr) continue;
      // Tick/range bars may share timestamps. Use the owning bar's trade
      // indices so those prints cannot jump to another bar with the same time.
      const x = this.builder.tf.kind === 'time' ? this.tsToX(tp.ts[i]) :
        this.X(barIndex - 0.5 + (i - b.i0 + 0.5) / (b.i1 - b.i0 + 1));
      const y = this.Y(tp.px[i]);
      const r = clamp(Math.sqrt(sz) * S.bubbleScale * dense, 4, 40);
      const side = tp.side[i], col = side === 1 ? T.pos : side === 2 ? T.neg : T.ink3;
      c.fillStyle = rgba(col, 0.28); c.strokeStyle = rgba(col, 0.95); c.lineWidth = 1.25;
      c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill(); c.stroke();
      if (r >= 9 && S.bubbleLabels) { c.font = `600 ${clamp(Math.round(r * 0.9), 8, 12)}px ${MONO}`; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = T.ink; c.fillText(String(sz), x, y + 0.5); }
    }
  }

  paint_series(c) {
    const bars = this.builder.bars, P = this.plot, pr = this.price;
    if (!bars.length) return;
    c.save(); c.beginPath(); c.rect(P.x, pr.y, P.w, pr.h + pr.footer); c.clip();
    const last = bars.length - 1;
    for (let i = this.i0; i <= this.i1; i++) if (i !== last) this.drawBar(c, bars[i], i, false);
    if (this.S.vwap) this.paintVwap(c);
    if (this.S.wvwap) this.paintWeeklyVwap(c);
    if (this.S.vp) this.paintVolumeProfile(c);
    if (this.S.power) for (const z of this.store.power.zones) this.drawPower(c, z, false);
    // Draw after all candles/profiles so subsequent bars cannot erase bubbles.
    if (this.S.bubbles) for (let i = this.i0; i <= this.i1; i++) {
      if (i !== last && !bars[i].seeded) this.drawBubbles(c, bars[i], i);
    }
    c.restore();
    if (this.cvd) this.paintCvd(c, false);
  }

  /** One power trade: a marker at the burst's price, sized by volume, coloured by delta dominance. */
  drawPower(c, z, forming) {
    const S = this.S, T = this.theme, bars = this.builder.bars;
    if (!bars.length || z.t1 < bars[this.i0].t) return;
    const x = this.tsToX(z.t1);
    if (x < this.plot.x - 40 || x > this.plot.x + this.plot.w + 40) return;
    const yHi = this.Y(z.hi), yLo = this.Y(z.lo), y = (yHi + yLo) / 2;
    // colour = neutral brass blended toward teal (buy) or red (sell) by |delta| dominance
    const dom = Math.min(1, Math.abs(z.pct) / 100);
    const side = z.pct > 0 ? T.pos : T.neg;
    const mix = (a, b, k) => { const A = hexToRgb(a), B = hexToRgb(b); return `rgb(${A.map((v, i) => Math.round(v + (B[i] - v) * k)).join(',')})`; };
    const col = mix(T.brass, side, Math.pow(dom, 0.7));
    const r = clamp(Math.sqrt(z.vol) * S.ptScale, 5, 34);
    c.save();
    c.globalAlpha = forming ? 0.55 : 0.95;
    // price-range bracket behind the marker
    c.strokeStyle = col; c.lineWidth = 1; c.setLineDash([2, 2]);
    c.beginPath(); c.moveTo(x, yHi - 1); c.lineTo(x, yLo + 1); c.stroke(); c.setLineDash([]);
    // the shape
    c.beginPath();
    if (S.ptShape === 'hexagon') { for (let k = 0; k < 6; k++) { const a = Math.PI / 6 + k * Math.PI / 3; const px = x + r * Math.cos(a), py = y + r * Math.sin(a); k ? c.lineTo(px, py) : c.moveTo(px, py); } c.closePath(); }
    else if (S.ptShape === 'box') { c.rect(this.tsToX(z.t0) - 1, yHi - 2, Math.max(4, x - this.tsToX(z.t0) + 2), yLo - yHi + 4); }
    else { c.moveTo(x, y - r); c.lineTo(x + r * 0.72, y); c.lineTo(x, y + r); c.lineTo(x - r * 0.72, y); c.closePath(); }
    c.fillStyle = col; c.globalAlpha *= 0.28; c.fill();
    c.globalAlpha = forming ? 0.7 : 1; c.strokeStyle = col; c.lineWidth = 1.6; c.stroke();
    // halo ring for one-sided bursts
    if (z.kind !== 'n' && S.ptShape !== 'box') { c.globalAlpha = 0.35; c.lineWidth = 1; c.beginPath(); c.arc(x, y, r + 4, 0, Math.PI * 2); c.stroke(); }
    if (S.ptLabels) {
      c.globalAlpha = 1; c.textAlign = 'center';
      const l1 = `${fmtK(z.vol)} in ${z.secs < 10 ? z.secs.toFixed(1) : Math.round(z.secs)}s`;
      const l2 = `B ${fmtK(z.buy)} Â· S ${fmtK(z.sell)} Â· ${z.pct > 0 ? '+' : ''}${z.pct}%`;
      c.font = `700 10px ${MONO}`; c.textBaseline = 'bottom';
      const w = Math.max(c.measureText(l1).width, c.measureText(l2).width) + 8, top = y - r - 30;
      c.fillStyle = rgba(T.panel, 0.8); c.fillRect(x - w / 2, top, w, 26);
      c.fillStyle = col; c.fillText(l1, x, top + 12);
      c.font = `9px ${MONO}`; c.fillStyle = T.ink2; c.fillText(l2, x, top + 24);
    }
    c.restore();
  }

  /** Session-anchored VWAP, one line per session so it resets at 06:30 and 13:00. */
  paintVwap(c) {
    const bars = this.builder.bars, T = this.theme;
    c.strokeStyle = rgba(T.vio, 0.95); c.lineWidth = 1.4; c.beginPath();
    let prev = null;
    for (let i = this.i0; i <= this.i1; i++) {
      const b = bars[i]; if (!isFinite(b.vwap)) { prev = null; continue; }
      const x = this.X(i), y = this.Y(b.vwap);
      if (prev === null || b.sess !== prev) c.moveTo(x, y); else c.lineTo(x, y);
      prev = b.sess;
    }
    c.stroke();
    // DEVIATION BANDS (2026-09-16, Mark: "I typically take longs below weekly vwap near
    // its -1 -2 or -3 deviations and shorts above the +1 +2 +3 deviations. The same
    // applies for session vwap."). One dashed line per band, faded with distance, each
    // labelled at the right edge. The sheet does not carry these -- the chart does.
    if (this.S.vwapBands) {
      const dash = [[3, 3], [2, 4], [1, 5]];
      for (let k = 1; k <= 3; k++) {
        for (const sgn of [1, -1]) {
          c.strokeStyle = rgba(T.vio, 0.42 - 0.08 * (k - 1));
          c.lineWidth = 1; c.setLineDash(dash[k - 1]); c.beginPath();
          let pv = null;
          for (let i = this.i0; i <= this.i1; i++) {
            const b = bars[i];
            if (!isFinite(b.vwap) || !isFinite(b.vsd)) { pv = null; continue; }
            const x = this.X(i), y = this.Y(b.vwap + sgn * k * b.vsd);
            if (pv === null || b.sess !== pv) c.moveTo(x, y); else c.lineTo(x, y);
            pv = b.sess;
          }
          c.stroke();
        }
      }
      c.setLineDash([]);
    }
    const l = bars[Math.min(this.i1, bars.length - 1)];
    if (l && isFinite(l.vwap)) {
      c.font = `600 9.5px ${MONO}`; c.fillStyle = T.vio;
      c.textAlign = 'left'; c.textBaseline = 'bottom';
      c.fillText('VWAP ' + fmt0(l.vwap), this.X(this.i1) + 6, this.Y(l.vwap) - 2);
      if (this.S.vwapBands && isFinite(l.vsd)) {
        for (let k = 1; k <= 3; k++) for (const sgn of [1, -1]) {
          const p = l.vwap + sgn * k * l.vsd, y = this.Y(p);
          if (y < this.price.y || y > this.price.y + this.price.h) continue;
          c.fillStyle = rgba(T.vio, 0.8 - 0.12 * (k - 1));
          c.fillText((sgn > 0 ? '+' : '−') + k + 'σ ' + fmt0(p), this.X(this.i1) + 6, y - 2);
        }
      }
    }
  }

  /** Weekly VWAP and its deviations, anchored at the Sunday 22:00Z reopen.
   *
   * Mark, 2026-09-16: "though I use session vwap the weekly should have more weight if
   * there are points of interest further out." It draws as ONE continuous line across
   * session boundaries -- that is the whole point of it -- in a heavier stroke than the
   * session mean, with its bands dashed behind. */
  paintWeeklyVwap(c) {
    const bars = this.builder.bars, T = this.theme;
    c.strokeStyle = rgba(T.vio, 0.95); c.lineWidth = 2.0; c.beginPath();
    let started = false;
    for (let i = this.i0; i <= this.i1; i++) {
      const b = bars[i]; if (!isFinite(b.wvwap)) { started = false; continue; }
      const x = this.X(i), y = this.Y(b.wvwap);
      if (!started) { c.moveTo(x, y); started = true; } else c.lineTo(x, y);
    }
    c.stroke();
    if (this.S.vwapBands) {
      const dash = [[4, 4], [3, 5], [2, 6]];
      for (let k = 1; k <= 3; k++) for (const sgn of [1, -1]) {
        c.strokeStyle = rgba(T.vio, 0.34 - 0.07 * (k - 1));
        c.lineWidth = 1.2; c.setLineDash(dash[k - 1]); c.beginPath();
        let on = false;
        for (let i = this.i0; i <= this.i1; i++) {
          const b = bars[i];
          if (!isFinite(b.wvwap) || !isFinite(b.wvsd)) { on = false; continue; }
          const x = this.X(i), y = this.Y(b.wvwap + sgn * k * b.wvsd);
          if (!on) { c.moveTo(x, y); on = true; } else c.lineTo(x, y);
        }
        c.stroke();
      }
      c.setLineDash([]);
    }
    const l = bars[Math.min(this.i1, bars.length - 1)];
    if (l && isFinite(l.wvwap)) {
      c.font = `600 9.5px ${MONO}`; c.fillStyle = T.vio;
      c.textAlign = 'left'; c.textBaseline = 'bottom';
      c.fillText('wVWAP ' + fmt0(l.wvwap), this.X(this.i1) + 6, this.Y(l.wvwap) - 2);
      if (this.S.vwapBands && isFinite(l.wvsd)) {
        for (let k = 1; k <= 3; k++) for (const sgn of [1, -1]) {
          const p = l.wvwap + sgn * k * l.wvsd, y = this.Y(p);
          if (y < this.price.y || y > this.price.y + this.price.h) continue;
          c.fillStyle = rgba(T.vio, 0.7 - 0.1 * (k - 1));
          c.fillText('w' + (sgn > 0 ? '+' : '−') + k + 'σ ' + fmt0(p), this.X(this.i1) + 6, y - 2);
        }
      }
    }
  }

  /** Volume-by-price for the visible bars, bid/ask split, on the left edge. */
  paintVolumeProfile(c) {
    const bars = this.builder.bars, T = this.theme, S = this.S, pr = this.price, P = this.plot;
    const rowT = Math.max(1, S.vpRowTicks), rows = new Map();
    let tot = 0;
    for (let i = this.i0; i <= this.i1; i++) {
      const b = bars[i];
      if (b.fp) { for (const [k, cell] of b.fp) { const r = Math.floor(k / rowT); let a = rows.get(r); if (!a) { a = [0, 0]; rows.set(r, a); } a[0] += cell[0]; a[1] += cell[1]; tot += cell[0] + cell[1]; } }
      else if (b.v) {                       // seeded bar: spread volume across its range
        const r0 = Math.floor(tickOf(b.l) / rowT), r1 = Math.floor(tickOf(b.h) / rowT), per = b.v / (r1 - r0 + 1);
        for (let r = r0; r <= r1; r++) { let a = rows.get(r); if (!a) { a = [0, 0]; rows.set(r, a); } a[0] += per / 2; a[1] += per / 2; tot += per; }
      }
    }
    if (!rows.size) return;
    let mx = 1, poc = null; for (const [r, a] of rows) { const s = a[0] + a[1]; if (s > mx) { mx = s; poc = r; } }
    // value area 70%
    const cnt = r => { const a = rows.get(r); return a ? a[0] + a[1] : 0; };
    let lo = poc, hi = poc, acc = mx;
    while (acc < tot * 0.7) { const up = cnt(hi + 1), dn = cnt(lo - 1); if (!up && !dn) break; if (up >= dn) { hi++; acc += up; } else { lo--; acc += dn; } }
    const W = S.vpWidth, x0 = P.x;
    for (const [r, a] of rows) {
      const yT = this.Y((r + 1) * rowT * TICK), yB = this.Y(r * rowT * TICK), h = Math.max(1, yB - yT - 0.5);
      if (yB < pr.y || yT > pr.y + pr.h) continue;
      const wb = a[0] / mx * W, wa = a[1] / mx * W, inVa = r >= lo && r <= hi;
      c.fillStyle = rgba(T.neg, inVa ? 0.45 : 0.22); c.fillRect(x0, yT, wb, h);
      c.fillStyle = rgba(T.pos, inVa ? 0.45 : 0.22); c.fillRect(x0 + wb, yT, wa, h);
      if (r === poc) { c.fillStyle = rgba(T.brass, 0.9); c.fillRect(x0, yT + h / 2 - 0.75, W + 6, 1.5); }
    }
    c.font = `600 9px ${MONO}`; c.fillStyle = T.ink3; c.textAlign = 'left'; c.textBaseline = 'top';
    c.fillText(`VP  POC ${fmt0((poc + 0.5) * rowT * TICK)}  VA ${fmt0(lo * rowT * TICK)}â€“${fmt0((hi + 1) * rowT * TICK)}`, x0 + 4, pr.y + pr.h - 14);
  }

  /** CVD line + delta histogram in the lower pane. `liveOnly` draws just the last bar. */
  cvdRange() {
    const bars = this.builder.bars; let lo = Infinity, hi = -Infinity, dmax = 1;
    for (let i = this.i0; i <= this.i1; i++) { const b = bars[i]; if (b.seeded) continue; lo = Math.min(lo, b.cvd, b.cvdo); hi = Math.max(hi, b.cvd, b.cvdo); dmax = Math.max(dmax, Math.abs(b.d)); }
    if (!isFinite(lo)) { lo = -1; hi = 1; }
    return { lo, hi, dmax };
  }
  paintCvd(c, liveOnly) {
    const S = this.S, T = this.theme, bars = this.builder.bars, cv = this.cvd, sp = this.view.spacing;
    const { lo, hi, dmax } = this.cvdRange();
    const last = bars.length - 1, P = this.plot;
    c.save(); c.beginPath(); c.rect(P.x, cv.y, P.w, cv.h); c.clip();
    const midY = cv.y + cv.h - 4, hh = (cv.h - 8) * 0.45;
    const from = liveOnly ? last : this.i0, to = liveOnly ? last : this.i1;
    if (S.deltaHist) for (let i = from; i <= to; i++) {
      const b = bars[i]; if (b.seeded || (!liveOnly && i === last)) continue;
      const x = this.X(i), h = b.d / dmax * hh, bw = Math.max(1, sp * 0.6);
      c.fillStyle = rgba(b.d >= 0 ? T.pos : T.neg, 0.5);
      c.fillRect(x - bw / 2, midY - Math.max(0, h), bw, Math.abs(h) || 1);
    }
    if (S.cvdLine) {
      c.strokeStyle = T.brass; c.lineWidth = 1.4; c.beginPath();
      let started = false;
      for (let i = liveOnly ? Math.max(this.i0, last - 1) : this.i0; i <= to; i++) {
        const b = bars[i]; if (b.seeded) continue;
        if (!liveOnly && i === last) break;
        const x = this.X(i), y = this.cvdY(b.cvd, lo, hi);
        if (!started) { c.moveTo(x, y); started = true; } else c.lineTo(x, y);
      }
      c.stroke();
    }
    c.restore();
  }

  // ------------------------------------------------------------ levels ---
  paint_levels(c) {
    const S = this.S, T = this.theme, L = this.store.levels, P = this.plot, pr = this.price;
    if (!L || !L.ok) return;
    c.save(); c.beginPath(); c.rect(P.x, pr.y, P.w, pr.h); c.clip();
    const labels = [];       // left-margin labels, collision-avoided later
    const line = (p, col, dash, w = 1, a = 1) => {
      const y = Math.round(this.Y(p)) + 0.5; if (y < pr.y || y > pr.y + pr.h) return false;
      c.globalAlpha = a; c.strokeStyle = col; c.setLineDash(dash); c.lineWidth = w;
      c.beginPath(); c.moveTo(P.x, y); c.lineTo(P.x + P.w, y); c.stroke(); c.setLineDash([]); c.globalAlpha = 1; return true;
    };
    // A book with no nodes draws nothing, so fall through to one that has them rather than
    // leaving the overlay blank: a sheet can carry an empty 0DTE (no_0dte), and a book expires
    // mid-session at its own expiry instant. (2026-09-17)
    const has = b => b && b.nodes && b.nodes.length;
    const book = [L.books[S.gBook], L.books['0DTE'], ...Object.values(L.books)].find(has);
    // gamma profile (right edge) + nodes as bands
    if (S.gamma && book && (S.gProfile || S.gNodes)) {
      const nodes = book.nodes.filter(n => Math.abs(n[1]) >= S.gMinNode);
      const mx = Math.max(1, ...nodes.map(n => Math.abs(n[1])));
      const R = P.x + P.w - 2, PW = S.gProfileWidth;
      for (const [k, g] of nodes) {
        const y = this.Y(k); if (y < pr.y || y > pr.y + pr.h) continue;
        const col = g > 0 ? T.pos : T.neg, w = Math.abs(g) / mx;
        if (S.gNodes) { c.fillStyle = rgba(col, 0.10 + 0.30 * w); c.fillRect(P.x, y - Math.max(1.5, 3 * w), P.w, Math.max(3, 6 * w)); }
        if (S.gProfile) {
          const bw = Math.max(3, w * PW);
          c.fillStyle = rgba(col, 0.45); c.fillRect(R - bw, y - 2.5, bw, 5);
          if (w >= 0.4 && S.gLabels) { c.font = `9.5px ${MONO}`; c.textAlign = 'right'; c.textBaseline = 'middle'; c.fillStyle = T.ink3; c.fillText((g > 0 ? '+' : '') + g, R - bw - 4, y); }
        }
      }
      // walls: largest positive above spot, largest negative below
      if (S.gWalls && (L.live ? L.live_last : L.spot)) {
        const px = L.live ? L.live_last : L.spot;     // repriced levels split at the print they were priced at
        const above = nodes.filter(n => n[0] > px && n[1] > 0).sort((a, b) => b[1] - a[1])[0];
        const below = nodes.filter(n => n[0] < px && n[1] < 0).sort((a, b) => a[1] - b[1])[0];
        if (above && line(above[0], T.pos, [], 1.4, 0.9)) labels.push({ p: above[0], txt: `call wall ${fmt0(above[0])}  +${above[1]}`, col: T.pos });
        if (below && line(below[0], T.neg, [], 1.4, 0.9)) labels.push({ p: below[0], txt: `put wall ${fmt0(below[0])}  ${below[1]}`, col: T.neg });
      }
    }
    // flips per book
    if (S.gamma && S.gFlips) for (const [name, bk] of Object.entries(L.books)) {
      if (!(name === '0DTE' ? S.gFlip0 : name === 'NEAR' ? S.gFlipNear : S.gFlipFull)) continue;
      const col = name === '0DTE' ? T.warn : name === 'NEAR' ? T.vio : T.ink2;
      const dead = L.expired && L.expired.includes(name);
      for (const f of bk.flips) if (line(f, col, [6, 4], 1.2)) labels.push({ p: f, txt: `${name} flip ${fmt0(f)}${dead ? ' (expired)' : ''}`, col });
    }
    if (S.gamma && S.gRails) for (const [p, tag] of [[L.rail_hi, 'rail max'], [L.rail_lo, 'rail min']]) if (p && line(p, T.ink3, [1, 3])) labels.push({ p, txt: `${tag} ${fmt0(p)}`, col: T.ink3 });
    // The reference is the PLAN's anchor -- the settle on a full-day sheet, the print on an
    // overnight or intraday one -- and the rails are drawn around it. The live levels are
    // priced at the current print instead, so when they are live the label says both,
    // rather than leaving a 29,247 line under levels priced at 29,336 (Mark, 2026-09-15).
    if (S.gamma && S.gReference && L.spot && line(L.spot, T.brass, [], 1.2)) {
      const anchor = (!L.rail_label || L.rail_label === '1D') ? 'settle' : 'reference';
      labels.push({ p: L.spot, txt: `${anchor} ${fmt0(L.spot)} · plan anchor${L.live ? `  ·  levels priced @ ${fmt0(L.live_last)}` : ''}`, col: T.brass });
    }
    // setups
    if (S.setups && L.setups) {
      const stateCol = st => !st ? T.ink3 : st === 'in play' ? T.brass : st.startsWith('tp') ? T.pos : st === 'stopped' ? T.neg : st === 'missed' ? T.vio : T.ink3;
      const live = st => ['in play', 'tp1 hit', 'tp2 hit', 'tp3 hit', 'stopped', 'missed'].includes(st);
      for (const s of L.setups) {
        const col = stateCol(s.state), a = (!L.tracked || live(s.state)) ? 0.9 : 0.35;
        line(s.entry, col, [], 1.3, a); line(s.stop, col, [4, 3], 1, a);
        for (const tp of s.tps || []) line(tp, col, [2, 4], 1, a);
        if (S.setupLabels && (!L.tracked || live(s.state))) {
          labels.push({ p: s.entry, txt: `${s.id} entry ${fmt0(s.entry)}`, col, right: true });
          labels.push({ p: s.stop, txt: `${s.id} stop ${fmt0(s.stop)}`, col, right: true });
          (s.tps || []).forEach((tp, k) => labels.push({ p: tp, txt: `${s.id} TP${k + 1} ${fmt0(tp)}`, col, right: true }));
        }
      }
    }
    // labels: the map's labels in the right column beside the setups' (gLabelsRight, the
    // default since 2026-09-15 -- on the left they sat over the volume profile, a screen
    // away from the price axis they describe), or the left column when that is switched
    // off; both columns avoid collisions
    if (S.gLabelsRight) for (const l of labels) l.right = true;
    if (S.gLabels) {
      c.font = `600 10px ${MONO}`; c.textBaseline = 'bottom';
      for (const side of [false, true]) {
        const used = [];
        labels.filter(l => !!l.right === side).sort((a, b) => b.p - a.p).forEach(l => {
          let y = this.Y(l.p) - 2, g = 0;
          while (used.some(u => Math.abs(u - y) < 11) && g++ < 60) y += side ? 11 : -11;
          if (y < pr.y + 10 || y > pr.y + pr.h) return;
          used.push(y);
          c.textAlign = side ? 'right' : 'left'; c.fillStyle = l.col;
          const x = side ? P.x + P.w - (S.gProfile ? S.gProfileWidth + 14 : 6) : P.x + 6;
          const w = c.measureText(l.txt).width;
          c.fillStyle = rgba(T.panel, 0.72); c.fillRect(side ? x - w - 3 : x - 3, y - 10, w + 6, 12);
          c.fillStyle = l.col; c.fillText(l.txt, x, y);
        });
      }
    }
    c.restore();
  }

  // -------------------------------------------------------------- live ---
  paint_live(c) {
    const bars = this.builder.bars, S = this.S, T = this.theme, P = this.plot, pr = this.price;
    if (!bars.length) return;
    const last = bars.length - 1, b = bars[last];
    if (last >= this.i0 - 1 && last <= this.i1 + 1) {
      c.save(); c.beginPath(); c.rect(P.x, pr.y, P.w, pr.h + pr.footer); c.clip();
      this.drawBar(c, b, last, true);
      if (S.power && this.store.power.open) this.drawPower(c, this.store.power.open, true);
      if (S.bubbles && !b.seeded) this.drawBubbles(c, b, last);
      c.restore();
      if (this.cvd) this.paintCvd(c, true);
    }
    // last price line + tag with countdown
    const close = this.glide ? this.glideValue() : b.c;
    const y = Math.round(this.Y(close)) + 0.5;
    if (S.lastLine && y >= pr.y && y <= pr.y + pr.h) {
      c.strokeStyle = rgba(close >= b.o ? T.pos : T.neg, 0.8); c.setLineDash([3, 3]); c.lineWidth = 1;
      c.beginPath(); c.moveTo(P.x, y); c.lineTo(P.x + P.w, y); c.stroke(); c.setLineDash([]);
    }
    if (S.lastTag) {
      const A = this.axis, col = close >= b.o ? T.pos : T.neg;
      let txt = fmt2(close), sub = null;
      if (S.countdown && this.builder.tf.kind === 'time') {
        const left = Math.max(0, b.t + this.builder.tf.ms - (this.store.now()));
        const s = Math.ceil(left / 1000); sub = s >= 3600 ? `${Math.floor(s / 3600)}h${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      }
      const hgt = sub ? 26 : 16, yy = clamp(y, pr.y + hgt / 2, pr.y + pr.h - hgt / 2);
      c.fillStyle = col; c.fillRect(A.x, yy - hgt / 2, A.w, hgt);
      c.fillStyle = T.panel; c.font = `600 10.5px ${MONO}`; c.textAlign = 'left'; c.textBaseline = 'middle';
      c.fillText(txt, A.x + 5, sub ? yy - 5 : yy);
      if (sub) { c.font = `9px ${MONO}`; c.fillText(sub, A.x + 5, yy + 7); }
    }
  }

  // ------------------------------------------------------------ chrome ---
  paint_chrome(c) {
    const T = this.theme, A = this.axis, P = this.plot, pr = this.price;
    c.fillStyle = T.panel; c.fillRect(A.x, 0, A.w, this.H); c.fillRect(0, this.time.y, this.W, this.time.h);
    c.strokeStyle = T.rule2; c.lineWidth = 1;
    c.beginPath(); c.moveTo(A.x + 0.5, 0); c.lineTo(A.x + 0.5, this.time.y); c.moveTo(0, this.time.y + 0.5); c.lineTo(this.W, this.time.y + 0.5); c.stroke();
    c.font = `10px ${MONO}`; c.fillStyle = T.ink3; c.textAlign = 'left'; c.textBaseline = 'middle';
    const step = this.priceStep();
    for (let p = Math.ceil(this.lo / step) * step; p < this.hi; p += step) { const y = this.Y(p); if (y > pr.y + 6 && y < pr.y + pr.h - 6) c.fillText(fmt0(p), A.x + 5, y); }
    if (this.cvd) {
      const { lo, hi } = this.cvdRange();
      c.fillStyle = T.ink3; c.fillText(fmtK(hi), A.x + 5, this.cvdY(hi, lo, hi) + 4); c.fillText(fmtK(lo), A.x + 5, this.cvdY(lo, lo, hi) - 4);
      c.font = `600 9px ${UI}`; c.fillStyle = T.ink3; c.textAlign = 'left'; c.fillText('CVD Â· Î”', P.x + 6, this.cvd.y + 9);
    }
    c.font = `10px ${MONO}`; c.textAlign = 'center'; c.textBaseline = 'middle';
    for (const t of this.timeTicks()) { c.fillStyle = t.major ? T.ink : T.ink3; if (t.x > P.x + 12 && t.x < A.x - 12) c.fillText(t.label, t.x, this.time.y + this.time.h / 2 + 1); }
    if (this.price.footer) { c.strokeStyle = T.rule; c.beginPath(); c.moveTo(0, pr.y + pr.h + 0.5); c.lineTo(A.x, pr.y + pr.h + 0.5); c.stroke(); }
  }

  // ------------------------------------------------------------ cursor ---
  paint_cursor(c) {
    const cur = this.cursor, T = this.theme, P = this.plot, A = this.axis; this.hover = null;
    if (!cur || !this.S.crosshair) return;
    const bars = this.builder.bars;
    let i = Math.round(this.Lof(cur.x)); const inPlot = cur.x >= P.x && cur.x < A.x && cur.y < this.time.y;
    if (!inPlot) return;
    const b = bars[i];
    const x = b ? Math.round(this.X(i)) + 0.5 : Math.round(cur.x) + 0.5;
    let p = this.Pof(cur.y);
    if (this.S.magnet) p = Math.round(p / TICK) * TICK;
    const y = Math.round(this.Y(p)) + 0.5;
    c.strokeStyle = rgba(T.ink2, 0.6); c.setLineDash([3, 3]); c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, 0); c.lineTo(x, this.time.y); c.moveTo(P.x, y); c.lineTo(A.x, y); c.stroke(); c.setLineDash([]);
    if (cur.y >= this.price.y && cur.y <= this.price.y + this.price.h) {
      c.fillStyle = T.ink2; c.fillRect(A.x, y - 8, A.w, 16);
      c.fillStyle = T.panel; c.font = `600 10.5px ${MONO}`; c.textAlign = 'left'; c.textBaseline = 'middle'; c.fillText(fmt2(p), A.x + 5, y);
    }
    if (b) {
      const txt = ptClock(b.t, true) + '  ' + ptParts(b.t).ymd.slice(5);
      c.font = `10px ${MONO}`; const w = c.measureText(txt).width + 10;
      c.fillStyle = T.ink2; c.fillRect(clamp(x - w / 2, 0, A.x - w), this.time.y, w, this.time.h);
      c.fillStyle = T.panel; c.textAlign = 'center'; c.fillText(txt, clamp(x, w / 2, A.x - w / 2), this.time.y + this.time.h / 2 + 1);
      const rowT = this.S.fpRowTicks, r = Math.floor(tickOf(p) / rowT);
      let bid = 0, ask = 0; if (b.fp) for (const [k, cell] of b.fp) if (Math.floor(k / rowT) === r) { bid += cell[0]; ask += cell[1]; }
      this.hover = { bar: b, i, price: p, bid, ask };
    }
  }

  // ------------------------------------------------------------ events ---
  bindEvents() {
    const el = this.root;
    let drag = null;
    el.addEventListener('mousemove', e => {
      const r = el.getBoundingClientRect(); this.cursor = { x: e.clientX - r.left, y: e.clientY - r.top };
      if (drag) {
        const dx = this.cursor.x - drag.x, dy = this.cursor.y - drag.y;
        if (drag.axis) {                       // stretch the price axis
          const mid = drag.mid, half = drag.half * Math.exp(dy / 120);
          this.manualY = { mid, half, src: 'axis' };
        } else {
          this.view.i0 = drag.i0 - dx / this.view.spacing;
          const N = this.builder.bars.length, visN = this.plot.w / this.view.spacing;
          this.atEnd = this.view.i0 >= N - visN + this.S.rightPadBars - 0.01;
          if (this.atEnd) this.view.i0 = Math.max(-2, N - visN + this.S.rightPadBars);
          else drag.left = true;                 // this drag has been away from the live edge
          if (this.manualY || Math.abs(dy) > 2 && !this.S.autoScaleWhilePanning) {
            const span = this.hi - this.lo;
            const mid0 = drag.mid ?? (this.hi + this.lo) / 2;
            // a scale the PAN hand-held (not the axis) is let go again when the drag
            // RETURNS to the live edge, so dragging back resumes following on both axes;
            // a vertical drag that never left the edge keeps the scale it asked for
            this.manualY = { mid: mid0 + dy / this.price.h * span, half: span / 2, src: (this.manualY && this.manualY.src) || 'pan' };
          }
          if (this.atEnd && drag.left && this.manualY && this.manualY.src === 'pan') this.manualY = null;
        }
        this.invalidate(LV.FULL);
      } else this.invalidate(LV.CURSOR);
    });
    el.addEventListener('mouseleave', () => { this.cursor = null; drag = null; this.invalidate(LV.CURSOR); });
    el.addEventListener('mousedown', e => {
      if (e.button !== 0) return;
      const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      const onAxis = x >= this.axis.x;
      drag = { x, y, i0: this.view.i0, axis: onAxis, left: !this.atEnd, mid: this.manualY ? this.manualY.mid : (this.hi + this.lo) / 2, half: this.manualY ? this.manualY.half : (this.hi - this.lo) / 2 };
      el.classList.add('dragging');
    });
    window.addEventListener('mouseup', () => { drag = null; el.classList.remove('dragging'); });
    el.addEventListener('dblclick', () => { this.manualY = null; this.atEnd = true; this.invalidate(LV.FULL); });
    el.addEventListener('wheel', e => {
      e.preventDefault();
      const r = el.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      const k = Math.exp(-e.deltaY * 0.0015);
      if (e.shiftKey || x >= this.axis.x) {
        const mid = this.manualY ? this.manualY.mid : (this.hi + this.lo) / 2, half = (this.manualY ? this.manualY.half : (this.hi - this.lo) / 2) / k;
        // zoom about the price under the cursor
        const pc = this.Pof(y), nm = pc + (mid - pc) / k;
        this.manualY = { mid: nm, half: clamp(half, 2, 20000), src: 'axis' };
      } else {
        const lAt = this.Lof(x);
        this.view.spacing = clamp(this.view.spacing * k, 0.5, 220);
        if (!this.atEnd) this.view.i0 = lAt - (x - this.plot.x) / this.view.spacing + 0.5;
      }
      this.invalidate(LV.FULL);
    }, { passive: false });
  }

  // --------------------------------------------------------- commands ---
  pan(bars) { this.view.i0 += bars; this.atEnd = false; this.invalidate(LV.FULL); }
  zoom(k) { this.view.spacing = clamp(this.view.spacing * k, 0.5, 220); this.invalidate(LV.FULL); }
  toEnd() { this.atEnd = true; this.manualY = null; this.centerMid = this.centerTarget = null; this.invalidate(LV.FULL); }
  /** Both axes tracking the tape: the newest bar at the right edge, the price scale auto-fitted. */
  following() { return this.atEnd && !this.manualY; }
  /** The Follow toggle. Off freezes the view exactly where it is on both axes; on is toEnd(). */
  setFollow(on) {
    if (on) { this.toEnd(); return; }
    this.atEnd = false;
    if (isFinite(this.hi) && isFinite(this.lo)) this.manualY = { mid: (this.hi + this.lo) / 2, half: (this.hi - this.lo) / 2, src: 'axis' };
    this.invalidate(LV.FULL);
  }
  toStart() { this.view.i0 = 0; this.atEnd = false; this.invalidate(LV.FULL); }
  fit() {
    const bars = this.builder.bars, N = bars.length; if (!N) return;
    // fit the CURRENT SESSION (RTH or the overnight), not the whole trading
    // day: 1,300 one-minute bars at one pixel each hides everything
    const sess = bars[N - 1].sess;
    let first = N - 1; while (first > 0 && bars[first - 1].sess === sess && !bars[first - 1].seeded) first--;
    const n = Math.max(60, N - first);
    this.view.spacing = clamp((this.plot.w - 40) / Math.max(n + this.S.rightPadBars, 10), 0.5, 220);
    this.toEnd();
  }
}
