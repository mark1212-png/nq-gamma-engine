// panels.js — the DOM ladder, the large-lot tape, the legend and the status
// line. All DOM, all throttled to one update per animation frame so a busy
// book (thousands of updates a second upstream, twenty a second on the wire)
// never touches layout more than the display can show.

import { ptClock } from './store.js';

const fmt0 = n => Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
const fmt2 = n => Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtK = n => Math.abs(n) >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(Math.round(n));
const sgn = n => (n > 0 ? '+' : '') + fmtK(n);

export class Panels {
  constructor(store, chart, S) {
    this.store = store; this.chart = chart; this.S = S;
    this.$ = id => document.getElementById(id);
    this.domRows = this.$('domrows');
    this.tapeEl = this.$('tape');
    this.legend = this.$('legend');
    this.tapeCursor = 0;
    this.raf = 0; this.pending = new Set();
    this.lastPx = NaN;
    this.buildDom();
  }

  schedule(what) { this.pending.add(what); if (!this.raf) this.raf = requestAnimationFrame(() => this.flush()); }
  flush() {
    this.raf = 0; const p = this.pending; this.pending = new Set();
    if (p.has('dom')) this.paintDom();
    if (p.has('tape')) this.paintTape();
    if (p.has('legend')) this.paintLegend();
  }

  // ------------------------------------------------------------ DOM ---
  buildDom() {
    const tb = this.domRows; tb.innerHTML = '';
    this.rows = [];
    for (let i = 9; i >= 0; i--) tb.appendChild(this.mkRow('ask', i));
    const sp = document.createElement('tr'); sp.className = 'spread'; sp.innerHTML = '<td colspan="3" id="domspread">—</td>'; tb.appendChild(sp);
    for (let i = 0; i < 10; i++) tb.appendChild(this.mkRow('bid', i));
  }
  mkRow(side, i) {
    const tr = document.createElement('tr'); tr.className = side;
    tr.innerHTML = `<td class="b"><div class="bar"></div><span></span></td><td class="p"><span></span></td><td class="a"><div class="bar"></div><span></span></td>`;
    this.rows.push({ tr, side, i, b: tr.children[0], p: tr.children[1], a: tr.children[2] });
    return tr;
  }
  paintDom() {
    const snap = this.store.book.last; if (!snap || !this.S.dom) return;
    let mx = 1; for (let k = 0; k < 20; k++) mx = Math.max(mx, snap.sz[k]);
    const last = this.store.tape.lastPx(), wall = this.S.wallSize;
    for (const r of this.rows) {
      const k = r.side === 'bid' ? r.i : 10 + r.i;
      const p = snap.px[k], z = snap.sz[k];
      r.p.firstChild.textContent = p ? fmt2(p) : '';
      const cell = r.side === 'bid' ? r.b : r.a, other = r.side === 'bid' ? r.a : r.b;
      cell.lastChild.textContent = p ? String(z) : '';
      cell.firstChild.style.width = p ? (z / mx * 100).toFixed(0) + '%' : '0';
      cell.lastChild.className = z >= wall ? 'wall' : '';
      other.lastChild.textContent = ''; other.firstChild.style.width = '0';
      r.tr.classList.toggle('last', !!p && Math.abs(p - last) < 1e-6);
    }
    const bb = snap.px[0], ba = snap.px[10];
    const sp = document.getElementById('domspread');
    if (bb && ba) {
      let bidTot = 0, askTot = 0; for (let k = 0; k < 10; k++) { bidTot += snap.sz[k]; askTot += snap.sz[10 + k]; }
      const imb = (bidTot - askTot) / Math.max(1, bidTot + askTot);
      sp.textContent = `${fmt2(bb)} / ${fmt2(ba)}  ·  ${Math.round((ba - bb) / 0.25)}t  ·  book ${(imb * 100).toFixed(0) > 0 ? '+' : ''}${(imb * 100).toFixed(0)}%`;
    }
  }

  // ----------------------------------------------------------- tape ---
  paintTape() {
    if (!this.S.tape) return;
    const tp = this.store.tape, big = tp.big, min = this.S.tapeMin, max = this.S.tapeRows;
    const frag = document.createDocumentFragment();
    let added = 0;
    for (let k = this.tapeCursor; k < big.length; k++) {
      const i = big[k]; if (tp.sz[i] < min) continue;
      const row = document.createElement('div');
      const side = tp.side[i];
      row.className = 'row ' + (side === 1 ? 'buy' : side === 2 ? 'sell' : '') + (tp.sz[i] >= min * 4 ? ' huge' : '');
      row.innerHTML = `<span class="t">${ptClock(tp.ts[i], true)}</span><span>${fmt2(tp.px[i])} ${side === 1 ? '▲' : side === 2 ? '▼' : '·'}</span><span class="s">${tp.sz[i]}</span>`;
      frag.prepend(row); added++;
    }
    this.tapeCursor = big.length;
    if (added) {
      this.tapeEl.prepend(frag);
      while (this.tapeEl.childElementCount > max) this.tapeEl.lastElementChild.remove();
    }
  }
  resetTape() { this.tapeEl.innerHTML = ''; this.tapeCursor = 0; this.paintTape(); }

  // --------------------------------------------------------- legend ---
  paintLegend() {
    const ch = this.chart, b = ch.hover ? ch.hover.bar : ch.builder.last;
    const L = this.store.levels, S = this.S;
    if (!b) { this.legend.innerHTML = `<b>NQ</b> ${S.tf} <span class="dim">waiting for the tape…</span>`; return; }
    const up = b.c >= b.o;
    let html = `<b>NQ</b> ${S.tf} <span class="dim">${ptClock(b.t, true)}</span>  ` +
      `O <b>${fmt2(b.o)}</b> H <b>${fmt2(b.h)}</b> L <b>${fmt2(b.l)}</b> C <b class="${up ? 'pos' : 'neg'}">${fmt2(b.c)}</b>  ` +
      (b.seeded ? `V <b>${fmtK(b.v)}</b>` : `Δ <b class="${b.d >= 0 ? 'pos' : 'neg'}">${sgn(b.d)}</b> V <b>${fmtK(b.v)}</b> <span class="dim">(${fmtK(b.bv)}×${fmtK(b.av)})</span> CVD <b class="${b.cvd >= 0 ? 'pos' : 'neg'}">${sgn(b.cvd)}</b>`);
    if (S.power) { const P = this.store.power, z = P.zones, last = z[z.length - 1];
      html += ` <span class="dim">· power</span> <b>${z.length + (P.open ? 1 : 0)}</b> <span class="dim">(max ${fmtK(P.best)} in ${S.ptWindow}s, ${fmtK(P.bestTight)} within ${S.ptZoneTicks}t)</span>` + (last ? ` <span class="dim">last</span> <b class="${last.kind === 'buy' ? 'pos' : last.kind === 'sell' ? 'neg' : 'brass'}">${fmtK(last.vol)} in ${last.secs < 10 ? last.secs.toFixed(1) : Math.round(last.secs)}s · B ${fmtK(last.buy)}/S ${fmtK(last.sell)} ${last.pct > 0 ? '+' : ''}${last.pct}% @ ${ptClock(last.t1, true)}</b>` : ''); }
    if (ch.hover && b.fp) html += `<br><span class="dim">at ${fmt2(ch.hover.price)}:</span> bid <b class="neg">${fmtK(ch.hover.bid)}</b> × ask <b class="pos">${fmtK(ch.hover.ask)}</b>`;
    if (S.gamma && L && L.ok) {
      const bk = L.books[S.gBook] || L.books['0DTE'];
      const flips = Object.entries(L.books).map(([n, v]) => `${n} ${v.flips.map(fmt0).join('/')}`).join(' · ');
      html += `<br><span class="brass">TeddyAI</span> <span class="dim">${L.report_date || ''} · ${L.oi_vintage || ''} OI · ${L.hours ? L.hours.toFixed(1) + 'h' : ''}</span> ref <b>${fmt0(L.spot)}</b> rails <b>${fmt0(L.rail_lo)}–${fmt0(L.rail_hi)}</b> <span class="dim">(${L.rail_label || ''} ${L.rail_scale ? '×' + L.rail_scale : ''})</span>`;
      html += `<br><span class="dim">flips</span> ${flips}` + (bk && bk.net_total != null ? ` <span class="dim">· ${S.gBook} net</span> <b class="${bk.net_total >= 0 ? 'pos' : 'neg'}">${sgn(bk.net_total)}</b>` : '');
      if (L.warn) html += `<br><span class="neg">${L.warn}</span>`;
    }
    this.legend.innerHTML = html;
  }

  // --------------------------------------------------------- status ---
  paintFlow() {
    const f = this.store.flow, el = this.$('flow-summary');
    if (!f || !el) return;
    const age = f.last_trade_ms == null ? Infinity : this.store.now()-f.last_trade_ms;
    const paused = f.replaying || age > 30000 || this.disconnected;
    const lines = [`${String(f.mode).toUpperCase()} · ${f.contract || 'unmapped'} · ${paused ? 'WAITING / STALE' : 'observed tape'}`];
    for (const seconds of [10,60,300]) {
      const w = f.windows[String(seconds)];
      lines.push(`${seconds}s Δ ${sgn(w.delta)} · ${fmt0(w.count)} prints · coverage ${w.coverage == null ? '—' : Math.round(w.coverage*100)+'%'}`);
      if (seconds === 60) lines.push(`Buy ${fmt0(w.buy)} / sell ${fmt0(w.sell)} / unknown ${fmt0(w.unknown)} · move ${w.displacement == null ? '—' : fmt2(w.displacement)+'pt'}`);
    }
    lines.push(`CVD ${sgn(f.cvd)} · first observed ${f.first_trade_ms ? ptClock(f.first_trade_ms, true) : 'unavailable'} PT`);
    if (f.out_of_order_dropped) lines.push(`${fmt0(f.out_of_order_dropped)} out-of-order records excluded`);
    lines.push('Completed seconds · descriptive, uncalibrated');
    el.textContent = lines.join('\n');
  }
  status(st, lag) {
    const $ = this.$;
    const state = st.state || '—';
    this.disconnected = ['disconnected','connected','reconnecting'].includes(state);
    const d = st.depth;
    if (d) {
      const topOnly=d.schema==='mbp-1';
      this.$('depth-title').textContent=topOnly?'Best bid / ask · MBP-1':'Book liquidity · '+d.schema;
      this.$('depth-status').textContent = `${d.schema}: ${d.state}. ${d.error || (d.state === 'waiting' ? 'Waiting for book data.' : topOnly?'Heatmap shows only displayed size at the best bid and ask; deeper levels are unavailable.':'Heatmap samples displayed depth each second.')} History starts when this chart receives book data.`;
      for(const row of this.rows) row.tr.hidden=topOnly&&row.i>0;
    }
    const el = $('st_state');
    el.textContent = st.replay ? `REPLAY ${st.day} ×${st.speed}${st.replay_from ? ` · ${st.replay_from}–${st.replay_to}` : ''} · ${state}` : st.sim ? `SIM · ${state}` : state;
    el.className = (state === 'streaming' || state === 'replay') ? 'live' : (state === 'replaying' || state === 'replay finished' || state === 'paused') ? 'warn' : 'bad';
    if (st.day) $('st_day').textContent = st.day;
    $('st_prints').textContent = fmt0(this.store.tape.n);
    if (st.msgs_s != null) $('st_rate').textContent = st.msgs_s;
    if (lag != null) $('st_lag').textContent = lag + 'ms';
    if ('stack' in st) this.stack = st.stack;
    const c = $('contract'); if (st.contract) c.textContent = st.contract;
  }
  tick() {
    const $ = this.$;
    this.paintFlow();
    $('st_clock').textContent = ptClock(this.store.now(), true);
    $('st_fps').textContent = this.chart.fps.v ? Math.round(this.chart.fps.v) + (this.chart.fps.ms != null ? ` · ${this.chart.fps.ms.toFixed(1)}ms` : '') : '—';
    const b = this.chart.builder.last; $('st_cvd').textContent = b ? sgn(b.cvd) : '0';
    const L = this.store.levels;
    // The stack token is always printed, even when it is healthy. On 2026-09-16 the
    // scheduler was stopped for 22 hours and this bar read exactly as it does on a good
    // day — map, setups, levels live at the last print — because every one of those is
    // true of a map that has aged two sessions. A line that only speaks up when something
    // is wrong cannot be told from a line that has stopped speaking.
    const S = this.stack;
    const el = $('st_levels');
    el.textContent = (L && L.ok
      ? `map ${L.report_date} · ${L.setups ? L.setups.length : 0} setups${L.tracked ? ' · tracked' : ''}`
        + (L.live ? ` · levels live ${String(L.live_asof || '').slice(11, 16)}Z @ ${L.live_last}` : ' · levels at plan')
        + (L.expired && L.expired.length ? ` · ${L.expired.join('/')} expired — rebuild` : '')
      : (L && L.note) || '')
      + (S && S.line ? ` · ${S.line}` : '');
    el.className = !S || S.ok ? '' : S.state === 'down' ? 'bad' : 'warn';
  }
}
