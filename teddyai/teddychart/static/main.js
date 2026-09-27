// main.js — wires the socket to the store, the store to the chart and panels,
// the keyboard and the topbar to the settings. Reconnects forever.

import { decode, TRADES, BOOK } from './wire.js';
import { Store } from './store.js';
import { Chart, LV } from './chart.js';
import { Panels } from './panels.js';
import { loadSettings, saveSettings, renderDrawer, syncDrawer, SCHEMA } from './settings.js';

const $ = id => document.getElementById(id);
const S = loadSettings();
const store = new Store();
store.clockOffset = 0;
store.now = () => Date.now() + store.clockOffset;

store.setPower({ windowSec: S.ptWindow, volume: S.ptVolume, zoneTicks: S.ptZoneTicks, deltaPct: S.ptDelta, merge: S.ptMerge });
applyTheme();
const chart = new Chart($('chart'), store, S);
const panels = new Panels(store, chart, S);

// ------------------------------------------------------------- store → UI
let powerSeen = 0;
function beep() {
  try { const ac = beep.ac || (beep.ac = new AudioContext()); const o = ac.createOscillator(), g = ac.createGain();
    o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.12); } catch { /* no audio */ }
}
store.on((ev, arg) => {
  if (ev === 'tick' || ev === 'structure') {
    chart.onData(ev); panels.schedule('legend'); panels.schedule('tape');
    const n = store.power.zones.length;
    if (n > powerSeen) { if (S.power && S.ptSound && store.replayDone) beep(); powerSeen = n; }
  }
  else if (ev === 'book') { chart.onData('book'); panels.schedule('dom'); }
  else if (ev === 'levels') { chart.onData('levels'); panels.schedule('legend'); }
});
// hover changes need the legend too
$('chart').addEventListener('mousemove', () => panels.schedule('legend'));
$('chart').addEventListener('mouseleave', () => panels.schedule('legend'));
setInterval(() => panels.tick(), 500);

// ----------------------------------------------------------------- socket
let ws, lag = null, pingAt = 0, replayShown = false;
function connect() {
  const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  ws = new WebSocket(url); ws.binaryType = 'arraybuffer';
  ws.onopen = () => { panels.status({ state: 'connected' }); $('replay').classList.add('on'); replayShown = true; };
  ws.onmessage = e => {
    if (typeof e.data === 'string') { onText(JSON.parse(e.data)); return; }
    const f = decode(e.data);
    if (f.type === TRADES) store.addTrades(f);
    else if (f.type === BOOK) store.addBook(f);
  };
  ws.onclose = () => { panels.status({ state: 'disconnected' }); setTimeout(connect, 1500); };
  ws.onerror = () => { try { ws.close(); } catch { /* */ } };
}
function onText(m) {
  switch (m.t) {
    case 'hello':
      store.hello = m; store.tape.dayOpen = m.day_open_ms;
      if (m.server_ms) store.clockOffset = m.server_ms - Date.now();
      if (m.contract) $('contract').textContent = m.contract;
      break;
    case 'bars': store.addSeed(m); break;
    case 'levels': store.levels = m; store.emit('levels'); banner(m.ok ? '' : ('gamma map: ' + (m.note || 'unavailable'))); break;
    case 'status': store.status = m; if (m.server_ms) store.clockOffset = m.server_ms - Date.now(); panels.status(m, lag); paintSpeed(m); break;
    case 'flow': store.flow = m; panels.paintFlow();
      if (window.parent !== window) for (const origin of CC_ORIGINS) window.parent.postMessage(m, origin);
      break;
    case 'replay_done':
      $('replay').classList.remove('on'); replayShown = false; store.replayDone = true; powerSeen = store.power.zones.length;
      if (!chart.fitted) { chart.fit(); chart.fitted = true; }
      panels.resetTape();
      break;
    case 'pong': lag = Math.round(performance.now() - pingAt); break;
  }
}
setInterval(() => { if (ws && ws.readyState === 1) { pingAt = performance.now(); ws.send(JSON.stringify({ t: 'ping', c: pingAt })); } }, 5000);
// levels are pushed on change; poll gently too, so a rebuild shows within seconds
setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'levels' })); }, 20000);
connect();

// ---------------------------------------------------------- replay speed
const SPEEDS = [0, 1, 2, 5, 10, 30, 100];
let curSpeed = 1, lastPlaySpeed = 1;
function paintSpeed(st) {
  const el = $('speed'); el.hidden = !st.replay;
  if (!st.replay) return;
  if (st.speed != null) { curSpeed = +st.speed; if (curSpeed > 0) lastPlaySpeed = curSpeed; }
  for (const b of el.children) b.classList.toggle('on', +b.dataset.speed === curSpeed);
}
function setSpeed(v) { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ t: 'speed', v })); curSpeed = v; if (v > 0) lastPlaySpeed = v; paintSpeed({ replay: true, speed: v }); }
for (const b of $('speed').children) b.onclick = () => setSpeed(+b.dataset.speed);

function banner(txt) { const b = $('banner'); b.textContent = txt; b.classList.toggle('on', !!txt); }

// --------------------------------------------------------------- settings
const drawer = $('drawer');
const QUICK = [['heatmap', 'Heat', 'h'], ['tpo', 'TPO', 't'], ['gamma', 'Gamma', 'g'], ['setups', 'Setups', 's'],
               ['bubbles', 'Bubbles', 'b'], ['power', 'Power', 'x'], ['vwap', 'VWAP', 'w'], ['vp', 'VP', null], ['cvdPane', 'CVD', 'v'], ['dom', 'Depth', 'd'], ['tape', 'Tape', null]];

function apply(key, value) {
  saveSettings(S);
  if (key === 'theme') applyTheme();
  if (key === 'tpoRow') store.setTpoRow(+S.tpoRow);
  if (key === 'ptVolume' || key === 'ptWindow' || key === 'ptZoneTicks' || key === 'ptDelta' || key === 'ptMerge' || key === '*') store.setPower({ windowSec: S.ptWindow, volume: S.ptVolume, zoneTicks: S.ptZoneTicks, deltaPct: S.ptDelta, merge: S.ptMerge });
  if (key === 'tf') { chart.fitted = false; }
  if (key === 'style' && S.style === 'footprint' && chart.view.spacing < S.fpMinPx + 20) chart.view.spacing = S.fpMinPx + 28;
  if (key === 'cvdPane' || key === 'cvdPaneFrac' || key === 'style' || key === 'fpFooter' || key === 'dom' || key === 'tape') { layoutSide(); chart.resize(); }
  if (key === 'tapeMin' || key === 'tapeRows') panels.resetTape();
  if (key === 'heatmap' || key === 'heatGain' || key === 'heatFloor' || key === 'heatRef' || key === 'heatAlpha') chart.heat.key = '';
  if (key === 'dom') panels.schedule('dom');
  if (key === 'tape') panels.schedule('tape');
  chart.invalidate(LV.FULL);
  paintTop();
  panels.schedule('legend');
}
renderDrawer(drawer, S, apply);
$('gear').onclick = () => drawer.classList.toggle('open');

function set(key, value) { S[key] = value; syncDrawer(drawer, key, value); apply(key, value); }

function applyTheme() {
  const r = document.documentElement;
  if (S.theme === 'auto') r.removeAttribute('data-theme'); else r.setAttribute('data-theme', S.theme);
  if (window.chartReady) chart.retheme();
}
window.chartReady = true;
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => chart.retheme());

function layoutSide() {
  const side = $('side'); side.classList.toggle('hidden', !S.dom && !S.tape);
  $('dom').style.display = S.dom ? '' : 'none'; $('dom').previousElementSibling.style.display = S.dom ? '' : 'none';
  $('tape').style.display = S.tape ? '' : 'none'; $('tape').previousElementSibling.style.display = S.tape ? '' : 'none';
}

// ----------------------------------------------------------------- topbar
const TFS = SCHEMA[0][1][0][3];
function paintTop() {
  const tfs = $('tfs');
  if (!tfs.childElementCount) for (const k of TFS) { const b = document.createElement('button'); b.className = 'pill'; b.textContent = k; b.dataset.tf = k; b.onclick = () => set('tf', k); tfs.appendChild(b); }
  for (const b of tfs.children) b.classList.toggle('on', b.dataset.tf === S.tf);
  for (const b of $('styles').children) { b.classList.toggle('on', b.dataset.style === S.style); b.onclick = () => set('style', b.dataset.style); }
  const q = $('quick');
  if (!q.childElementCount) for (const [key, label] of QUICK) { const b = document.createElement('button'); b.className = 'tog'; b.textContent = label; b.dataset.key = key; b.onclick = () => set(key, !S[key]); q.appendChild(b); }
  for (const b of q.children) b.classList.toggle('on', !!S[b.dataset.key]);
}
paintTop(); layoutSide();
$('fit').onclick = () => chart.fit();
$('end').onclick = () => chart.toEnd();
// Follow: the chart owns the state (both axes tracking the tape); the button only shows and flips it
const paintFollow = f => $('follow').classList.toggle('on', !!f);
chart.onFollow = f => { paintFollow(f); if (window.parent !== window) for (const o of CC_ORIGINS) { try { window.parent.postMessage(ccState(), o); } catch { /* not that parent */ } } };
paintFollow(chart.following());
$('follow').onclick = () => chart.setFollow(!chart.following());
$('helpbtn').onclick = () => $('help').classList.toggle('on');

// --------------------------------------------------------------- keyboard
window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  const k = e.key;
  if (k === 'Escape') { drawer.classList.remove('open'); $('help').classList.remove('on'); return; }
  if (!$('speed').hidden) {
    if (k === ' ') { setSpeed(curSpeed === 0 ? lastPlaySpeed : 0); e.preventDefault(); return; }
    if (k === ']') { const i = SPEEDS.indexOf(curSpeed); setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, i + 1)]); return; }
    if (k === '[') { const i = SPEEDS.indexOf(curSpeed); setSpeed(SPEEDS[Math.max(1, i - 1)]); return; }
  }
  if (k === '?') { $('help').classList.toggle('on'); return; }
  if (k === ',') { drawer.classList.toggle('open'); return; }
  if (k === 'ArrowLeft') { chart.pan(-(e.shiftKey ? 50 : 10)); e.preventDefault(); return; }
  if (k === 'ArrowRight') { chart.pan(e.shiftKey ? 50 : 10); e.preventDefault(); return; }
  if (k === '+' || k === '=') { chart.zoom(1.25); return; }
  if (k === '-' || k === '_') { chart.zoom(0.8); return; }
  if (k === 'End') { chart.toEnd(); return; }
  if (k === 'Home') { chart.toStart(); return; }
  const low = k.toLowerCase();
  if (low === 'f') { chart.fit(); return; }
  if (low === 'l') { chart.setFollow(!chart.following()); return; }
  if (low === 'c') { set('style', 'candles'); return; }
  if (low === 'p') { set('style', 'footprint'); return; }
  for (const [key, , hot] of QUICK) if (hot && low === hot) { set(key, !S[key]); return; }
  if (/^[1-9]$/.test(k)) { const i = +k - 1; if (TFS[i]) set('tf', TFS[i]); }
});

// ------------------------------------------------- embedded in the command center
// teddy.py's /cc page shows this chart in an iframe and drives the quick toggles from
// its own toolbar. Only the dashboard's origin is honoured; messages are the same
// {key, value} pairs the drawer sets, so nothing here can do what a click cannot.
const CC_ORIGINS = new Set(['http://127.0.0.1:5137', 'http://localhost:5137']);
function ccState() { return { t: 'state', settings: { heatmap: !!S.heatmap, tpo: !!S.tpo, vp: !!S.vp, cvdPane: !!S.cvdPane, bubbles: !!S.bubbles, power: !!S.power, gamma: !!S.gamma, setups: !!S.setups, dom: !!S.dom, tape: !!S.tape, tf: S.tf, style: S.style, follow: chart.following() } }; }
window.addEventListener('message', e => {
  if (!CC_ORIGINS.has(e.origin) || !e.data || typeof e.data !== 'object') return;
  const m = e.data;
  if (m.t === 'toggle' && m.key in S && typeof S[m.key] === 'boolean') set(m.key, !S[m.key]);
  else if (m.t === 'set' && m.key in S) set(m.key, m.value);
  else if (m.t === 'fit') chart.fit();
  else if (m.t === 'end') chart.toEnd();
  else if (m.t === 'follow') chart.setFollow(typeof m.value === 'boolean' ? m.value : !chart.following());
  e.source.postMessage(ccState(), e.origin);
});
// tell an embedding page the initial state once it is listening
if (window.parent !== window) setTimeout(() => { for (const o of CC_ORIGINS) { try { window.parent.postMessage(ccState(), o); } catch { /* not that parent */ } } }, 800);
