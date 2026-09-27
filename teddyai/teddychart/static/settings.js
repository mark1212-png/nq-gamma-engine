// settings.js — every knob on the chart, declared once. The drawer renders from
// this schema, the chart reads the flat `S` object, and localStorage keeps it.
// Adding a setting is one line here; nothing else needs to know.

export const SCHEMA = [
  ['Chart', [
    ['tf', 'Timeframe', 'select', ['1s', '5s', '15s', '30s', '1m', '2m', '3m', '5m', '15m', '30m', '1h', '250t', '500t', '1000t', '2000t', '5r', '10r', '20r']],
    ['style', 'Bar style', 'select', ['candles', 'footprint']],
    ['theme', 'Theme', 'select', ['auto', 'light', 'dark']],
    ['grid', 'Grid', 'bool'],
    ['sessionShade', 'Shade overnight session', 'bool'],
    ['crosshair', 'Crosshair', 'bool'],
    ['magnet', 'Crosshair snaps to tick', 'bool'],
    ['lastLine', 'Last-price line', 'bool'],
    ['lastTag', 'Last-price tag on axis', 'bool'],
    ['countdown', 'Bar countdown in tag', 'bool'],
    ['glide', 'Glide the forming bar', 'bool'],
    ['glideMs', 'Glide duration (ms)', 'num', 40, 400, 10],
    ['rightPadBars', 'Empty bars right of live', 'num', 0, 60, 1],
    ['scaleToLevels', 'Auto-scale includes plan levels', 'bool'],
    ['autoScaleWhilePanning', 'Keep auto price scale while panning', 'bool'],
    ['followCenter', 'Follow keeps the last price centred', 'bool'],
    ['followBand', 'Re-centre once price drifts (% of half-height, 0 = every tick)', 'num', 0, 80, 5],
    ['hollowUp', 'Hollow up-candles', 'bool'],
    ['deltaOnCandles', 'Delta above candles', 'bool'],
  ]],
  ['Footprint', [
    ['fpRowTicks', 'Row size (ticks)', 'num', 1, 40, 1],
    ['fpMinPx', 'Min bar width for cells (px)', 'num', 10, 120, 2],
    ['fpShade', 'Shade cells by volume', 'bool'],
    ['imbalance', 'Diagonal imbalances', 'bool'],
    ['imbRatio', 'Imbalance ratio (x)', 'num', 1.5, 10, 0.5],
    ['imbMinVol', 'Imbalance min volume', 'num', 1, 500, 1],
    ['fpPoc', 'Outline bar POC', 'bool'],
    ['fpFooter', 'Delta / volume footer', 'bool'],
  ]],
  ['Order flow', [
    ['vwap', 'Session VWAP', 'bool'],
    ['vwapBands', 'VWAP ±1 ±2 ±3σ', 'bool'],
    ['wvwap', 'Weekly VWAP', 'bool'],
    ['vp', 'Volume profile (visible range)', 'bool'],
    ['vpRowTicks', 'Volume-profile row (ticks)', 'num', 1, 40, 1],
    ['vpWidth', 'Volume-profile width (px)', 'num', 30, 300, 10],
    ['bubbles', 'Large trade-print bubbles', 'bool'],
    ['bigLot', 'Minimum contracts per individual print', 'num', 5, 2000, 5],
    ['bubbleScale', 'Bubble size', 'num', 0.4, 5, 0.2],
    ['bubbleLabels', 'Size labels in bubbles', 'bool'],
    ['bubbleMinPx', 'Hide bubbles below bar width (px)', 'num', 0, 60, 1],
    ['cvdPane', 'CVD / delta pane', 'bool'],
    ['cvdPaneFrac', 'Pane height (fraction)', 'num', 0.1, 0.5, 0.02],
    ['cvdLine', 'CVD line', 'bool'],
    ['deltaHist', 'Per-bar delta histogram', 'bool'],
    ['dom', 'Depth ladder', 'bool'],
    ['tape', 'Large-lot tape', 'bool'],
    ['tapeMin', 'Tape threshold (contracts)', 'num', 5, 2000, 5],
    ['tapeRows', 'Tape rows kept', 'num', 10, 300, 10],
    ['wallSize', 'Highlight resting size ≥', 'num', 50, 5000, 25],
  ]],
  ['Power trades', [
    ['power', 'Power trades', 'bool'],
    ['ptVolume', 'Total volume (contracts)', 'num', 20, 5000, 10],
    ['ptWindow', 'Time window (seconds)', 'num', 0.5, 60, 0.5],
    ['ptZoneTicks', 'Max zone height (ticks, 0 = no limit)', 'num', 0, 200, 1],
    ['ptDelta', 'One-sided if |delta| ≥ (%)', 'num', 0, 100, 5],
    ['ptShape', 'Marker shape', 'select', ['diamond', 'hexagon', 'box']],
    ['ptScale', 'Marker size', 'num', 0.3, 4, 0.1],
    ['ptMerge', 'Merge overlapping bursts into one', 'bool'],
    ['ptLabels', '"N in Ns · buy/sell · delta" labels', 'bool'],
    ['ptSound', 'Sound on a new zone', 'bool'],
  ]],
  ['Liquidity heatmap', [
    ['heatmap', 'Liquidity heatmap (available book)', 'bool'],
    ['heatAlpha', 'Opacity', 'num', 0.1, 1, 0.05],
    ['heatGain', 'Gain', 'num', 0.2, 6, 0.1],
    ['heatFloor', 'Hide below (0–1)', 'num', 0, 0.6, 0.02],
    ['heatRef', 'Reference size (0 = auto max)', 'num', 0, 5000, 50],
  ]],
  ['TPO · market profile', [
    ['tpo', 'TPO profiles', 'bool'],
    ['tpoRow', 'Row size (points)', 'select', [0.25, 0.5, 1, 2.5, 5, 10]],
    ['tpoSessions', 'Sessions shown', 'num', 1, 12, 1],
    ['tpoEth', 'Include overnight (ETH) profiles', 'bool'],
    ['tpoLetters', 'Letters (else blocks)', 'bool'],
    ['tpoCellPx', 'Letter cell width (px)', 'num', 4, 24, 1],
    ['tpoAlpha', 'Opacity', 'num', 0.2, 1, 0.05],
    ['tpoPoc', 'POC line', 'bool'],
    ['tpoVA', 'Value-area wash', 'bool'],
    ['tpoIB', 'Initial-balance bracket', 'bool'],
    ['tpoSingles', 'Mark single prints', 'bool'],
  ]],
  ['Gamma map', [
    ['gamma', 'Gamma map', 'bool'],
    ['gBook', 'Profile book', 'select', ['0DTE', 'NEAR', 'FULL']],
    ['gProfile', 'Gamma profile (right edge)', 'bool'],
    ['gProfileWidth', 'Profile width (px)', 'num', 40, 300, 10],
    ['gNodes', 'Gamma nodes as bands', 'bool'],
    ['gMinNode', 'Min |gamma| to draw', 'num', 1, 100, 1],
    ['gWalls', 'Call / put walls', 'bool'],
    ['gFlips', 'Flips (HVL)', 'bool'],
    ['gFlip0', '· 0DTE flip', 'bool'],
    ['gFlipNear', '· NEAR flip', 'bool'],
    ['gFlipFull', '· FULL flip', 'bool'],
    ['gRails', 'Expected-move rails', 'bool'],
    ['gReference', 'Reference price', 'bool'],
    ['gLabels', 'Level labels', 'bool'],
    ['gLabelsRight', 'Level labels on the right', 'bool'],
    ['setups', 'Gameplan setups', 'bool'],
    ['setupLabels', 'Setup labels', 'bool'],
  ]],
];

export const DEFAULTS = {
  tf: '1m', style: 'candles', theme: 'auto', grid: true, sessionShade: true, crosshair: true, magnet: true,
  lastLine: true, lastTag: true, countdown: true, glide: true, glideMs: 120, rightPadBars: 8,
  scaleToLevels: true, autoScaleWhilePanning: true, followCenter: true, followBand: 20, hollowUp: false, deltaOnCandles: false,
  fpRowTicks: 4, fpMinPx: 28, fpShade: true, imbalance: true, imbRatio: 3, imbMinVol: 10, fpPoc: true, fpFooter: true,
  vwap: true, vwapBands: true, wvwap: true, vp: true, vpRowTicks: 4, vpWidth: 90,
  bubbles: true, bigLot: 25, bubbleScale: 1.6, bubbleLabels: true, bubbleMinPx: 2,
  power: true, ptVolume: 200, ptWindow: 5, ptZoneTicks: 0, ptDelta: 60, ptShape: 'diamond', ptScale: 1, ptMerge: false, ptLabels: true, ptSound: false,
  cvdPane: true, cvdPaneFrac: 0.2, cvdLine: true, deltaHist: true,
  dom: true, tape: true, tapeMin: 20, tapeRows: 60, wallSize: 300,
  heatmap: true, heatAlpha: 0.85, heatGain: 1, heatFloor: 0.06, heatRef: 0,
  tpo: true, tpoRow: 2.5, tpoSessions: 3, tpoEth: true, tpoLetters: true, tpoCellPx: 9, tpoAlpha: 0.85,
  tpoPoc: true, tpoVA: true, tpoIB: true, tpoSingles: true,
  gamma: true, gBook: '0DTE', gProfile: true, gProfileWidth: 110, gNodes: true, gMinNode: 8, gWalls: true,
  gFlips: true, gFlip0: true, gFlipNear: true, gFlipFull: true, gRails: true, gReference: true, gLabels: true, gLabelsRight: true,
  setups: true, setupLabels: true,
};

const KEY = 'teddychart.settings.v2';

export function loadSettings() {
  const S = { ...DEFAULTS };
  try { Object.assign(S, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch { /* fresh */ }
  return S;
}
export function saveSettings(S) {
  try { localStorage.setItem(KEY, JSON.stringify(S)); } catch { /* private mode */ }
}

/** Render the drawer. onChange(key, value) fires for every edit. */
export function renderDrawer(el, S, onChange) {
  el.innerHTML = '';
  for (const [group, items] of SCHEMA) {
    const h = document.createElement('h3'); h.textContent = group; el.appendChild(h);
    for (const [key, label, type, ...rest] of items) {
      const row = document.createElement('div'); row.className = 'set';
      const lab = document.createElement('label'); lab.textContent = label; row.appendChild(lab);
      let ctl;
      if (type === 'bool') {
        ctl = document.createElement('div'); ctl.className = 'sw' + (S[key] ? ' on' : ''); ctl.dataset.key = key;
        const flip = () => { S[key] = !S[key]; ctl.classList.toggle('on', S[key]); onChange(key, S[key]); };
        ctl.onclick = flip; lab.onclick = flip;
      } else if (type === 'num') {
        ctl = document.createElement('input'); ctl.type = 'number'; ctl.min = rest[0]; ctl.max = rest[1]; ctl.step = rest[2]; ctl.value = S[key]; ctl.dataset.key = key;
        ctl.onchange = () => { const v = +ctl.value; if (isFinite(v)) { S[key] = Math.max(rest[0], Math.min(rest[1], v)); ctl.value = S[key]; onChange(key, S[key]); } };
      } else {
        ctl = document.createElement('select'); ctl.dataset.key = key;
        for (const o of rest[0]) { const op = document.createElement('option'); op.value = o; op.textContent = o; if (String(S[key]) === String(o)) op.selected = true; ctl.appendChild(op); }
        ctl.onchange = () => { const v = rest[0].find(o => String(o) === ctl.value); S[key] = v; onChange(key, v); };
      }
      row.appendChild(ctl); el.appendChild(row);
    }
  }
  const btns = document.createElement('div'); btns.className = 'btnrow';
  const reset = document.createElement('button'); reset.textContent = 'Reset to defaults';
  reset.onclick = () => { Object.assign(S, DEFAULTS); saveSettings(S); renderDrawer(el, S, onChange); onChange('*', null); };
  btns.appendChild(reset); el.appendChild(btns);
  const hint = document.createElement('div'); hint.className = 'hint'; hint.textContent = 'Settings are kept in this browser and survive restarts.';
  el.appendChild(hint);
}

/** Reflect one changed value into an already-rendered drawer (for keyboard toggles). */
export function syncDrawer(el, key, value) {
  const ctl = el.querySelector(`[data-key="${key}"]`); if (!ctl) return;
  if (ctl.classList.contains('sw')) ctl.classList.toggle('on', !!value);
  else ctl.value = value;
}
