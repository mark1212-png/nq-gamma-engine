/* Live ladder explanations. Pure comparison/tracker exports are tested with node:test.
 * Consumes the command center's existing repricing; never fetches, prices options,
 * calls a model, writes a report, or changes a trade. See LIVE-LADDER.md.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LiveLadderExplain = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const POLICY = Object.freeze({gammaAbs: 10, gammaFrac: 0.20, flipPts: 5,
    flipEM: 0.02, percentFloor: 10, holdMs: 30000, staleMs: 90000, maxEvents: 12});
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const number = x => finite(x) ? x.toLocaleString('en-US', {maximumFractionDigits: 2}) : 'unavailable';
  const signed = x => finite(x) ? (Math.abs(x)<0.005 ? '0' : (x > 0 ? '+' : '') + number(x)) : 'unavailable';
  const stamp = ms => new Date(ms).toLocaleTimeString('en-US', {timeZone: 'America/Los_Angeles', hour12: false});
  function measure(before, now, threshold) {
    if (![before, now, threshold].every(finite)) return {before, now, delta: null, percent: null, material: false, threshold};
    const delta = now - before;
    return {before, now, delta, percent: Math.abs(before) >= POLICY.percentFloor ? 100 * delta / Math.abs(before) : null,
      threshold, material: Math.abs(delta) >= threshold};
  }
  const gammaThreshold = value => Math.max(POLICY.gammaAbs, Math.abs(value) * POLICY.gammaFrac);
  function windowMap(g, center, radius) {
    return Object.fromEntries(Object.entries(g).filter(([k]) => Math.abs(Number(k) - center) <= radius));
  }
  const sum = g => Object.values(g).reduce((s, v) => s + v, 0);
  function compare(s) {
    if (!finite(s.center) || !finite(s.radius) || s.radius <= 0 || !s.base || !s.current) throw Error('Comparison inputs unavailable');
    for (const map of [s.base.g, s.current.g]) {
      if (!map || !Object.entries(map).every(([k,v]) => Number.isFinite(Number(k)) && finite(v))) throw Error('Invalid gamma map');
    }
    // A fixed price window prevents strikes entering/leaving a moving window from
    // masquerading as an exposure change. Missing strikes in a valid map mean zero.
    const old = windowMap(s.base.g, s.center, s.radius), cur = windowMap(s.current.g, s.center, s.radius);
    const oldNet = sum(old), net = sum(cur), facts = [];
    facts.push({id: 'net', kind: 'gamma', title: 'Net gamma in the fixed comparison window',
      ...measure(oldNet, net, gammaThreshold(oldNet)), unit: 'contracts / 1% move'});
    facts.push({id: 'flip', kind: 'flip', title: 'Nearest modeled flip',
      ...measure(s.base.flip, s.current.flip, Math.max(POLICY.flipPts, POLICY.flipEM * s.radius)), unit: 'NQ points'});
    const keys = [...new Set([...Object.keys(old), ...Object.keys(cur)])];
    const changes = keys.map(k => ({id: 'node:' + k, kind: 'gamma', title: number(+k) + ' strike gamma',
      ...measure(old[k] || 0, cur[k] || 0, gammaThreshold(old[k] || 0)), unit: 'contracts / 1% move'}));
    changes.sort((a,b) => Math.abs(b.delta) - Math.abs(a.delta) || a.id.localeCompare(b.id));
    if (changes.length) facts.push(changes[0]);
    const strongest = Object.entries(cur).sort((a,b) => Math.abs(b[1])-Math.abs(a[1]) || +a[0]- +b[0])[0];
    if (strongest && (!changes[0] || changes[0].id !== 'node:' + strongest[0])) {
      const row = changes.find(x => x.id === 'node:' + strongest[0]);
      facts.push({...row, title: row.title + ' (strongest now)'});
    } else if (strongest && facts[2]) facts[2].title += ' (strongest now)';
    const gross = Object.values(cur).reduce((a,v) => a + Math.abs(v), 0);
    const share = strongest && gross ? Math.abs(strongest[1]) / gross * 100 : null;
    return {facts, strongest: strongest ? +strongest[0] : null, share,
      spot: measure(s.center, s.current.last, 0),
      distance: finite(s.current.flip) ? s.current.last - s.current.flip : null,
      oldDistance: finite(s.base.flip) ? s.center - s.base.flip : null};
  }
  function reason(s) {
    if (!s || s.error) return s && s.error || 'Waiting for a saved options book and live trades.';
    if (!s.connected) return 'Trade stream disconnected.';
    if (s.feedState !== 'live') return 'Trade stream is catching up or reconnecting.';
    if (!['real','replay','sim'].includes(s.mode)) return 'Live trade feed is unavailable.';
    if (!finite(s.now) || !finite(s.lastMs)) return 'Waiting for timestamped trades.';
    if (s.now - s.lastMs > POLICY.staleMs) return 'Last trade is over 90 seconds old.';
    if (s.lastMs > s.now + 2000) return 'Trade timestamps are ahead of the interpretation clock.';
    if (s.now < s.baseAt) return 'The saved ladder is later than the current clock.';
    return null;
  }
  function createMinuteBars() {
    let cursor = 0, session = null, lastTime = null, bars = [], firstMinute = null;
    return {consume(ts, prices, sessionId) {
      if (session !== sessionId || ts.length < cursor || (ts.length && lastTime !== null && ts[ts.length-1] < lastTime)) {
        cursor = 0; session = sessionId; bars = []; lastTime = null; firstMinute = null;
      }
      for (; cursor < ts.length; cursor++) {
        const t = ts[cursor], p = prices[cursor];
        if (!finite(t) || !finite(p) || (lastTime !== null && t < lastTime)) continue;
        const minute = Math.floor(t / 60000) * 60000;
        if (firstMinute === null) firstMinute = minute;
        const last = bars[bars.length-1];
        if (last && last.t === minute) { last.h = Math.max(last.h,p); last.l = Math.min(last.l,p); last.c = p; }
        else bars.push({t:minute,o:p,h:p,l:p,c:p});
        lastTime = t;
      }
      bars = bars.slice(-181);
      // The first observed minute may start mid-bar. Never use it to confirm.
      return bars.filter(b => b.t !== firstMinute).map(b => ({...b}));
    }};
  }
  function priceConfirmations(bars, levels, now, knownAt, radius) {
    const buffer = Math.ceil(Math.max(1, radius * 0.0025) * 4) / 4;
    const cutoff = Math.ceil(knownAt / 60000) * 60000;
    const done = (bars || []).filter(b => b.t >= cutoff && b.t >= now - 33*60000 && b.t+60000 <= now &&
      ['t','o','h','l','c'].every(k => finite(b[k])));
    // No confirmations from a cache whose last complete minute is missing.
    if (!done.length || done[done.length-1].t !== Math.floor(now/60000)*60000-60000)
      return {buffer, signals:[], ready:false, reason:'Waiting for contiguous completed one-minute bars.'};
    const signals = [];
    for (const level of levels) {
      const k = level.price;
      if (!finite(k)) continue;
      const side = b => b.c > k+buffer ? 1 : b.c < k-buffer ? -1 : 0;
      for (let i=2;i<done.length;i++) {
        const [p,a,b] = done.slice(i-2,i+1), dir = side(a);
        if (a.t-p.t!==60000 || b.t-a.t!==60000 || !dir || side(b)!==dir) continue;
        let kind = null;
        if (side(p) === -dir) kind = dir>0 ? 'Reclaim confirmed' : 'Loss confirmed';
        else if (side(p)===dir && (dir<0 ? a.h>=k+buffer : a.l<=k-buffer))
          kind = dir<0 ? 'Failed reclaim from below' : 'Retest held from above';
        else if (side(p)===0) kind = dir>0 ? 'Two closes above' : 'Two closes below';
        if (!kind) continue;
        signals.push({at:b.t+60000,price:k,label:level.label,kind,side:dir,
          closes:[a.c,b.c],buffer,detail:`${number(a.c)} and ${number(b.c)} closed ${dir>0?'above':'below'} ${number(k)} by more than ${number(buffer)} points.`});
      }
    }
    signals.sort((a,b)=>b.at-a.at||a.price-b.price);
    return {buffer, signals:signals.slice(0,6), ready:done.length>=3,
      reason:done.length>=3?'':'Waiting for three completed bars after the snapshot.'};
  }
  function createTracker() {
    let context = null, lastNow = null, pending = new Map(), anchors = new Map(), events = [], paused = false, confirmationFloor = 0;
    return {update(s) {
      const stopped = reason(s);
      const rewind = s && finite(s.now) && lastNow !== null && s.now < lastNow;
      if (s && s.id && (s.id !== context || rewind)) {
        context = s.id; pending.clear(); anchors.clear(); events = []; lastNow = null; confirmationFloor = 0;
      }
      if (stopped) {
        confirmationFloor = Math.max(confirmationFloor, s && finite(s.now) ? s.now : lastNow || 0);
        pending.clear(); paused = true;
        return {paused: true, reason: stopped, events: [...events], snapshot: s};
      }
      if (paused) { pending.clear(); anchors.clear(); events = []; paused = false; }
      const analysis = compare(s); lastNow = s.now;
      const visible = new Set();
      for (const f of analysis.facts) {
        visible.add(f.id);
        if (!finite(f.delta)) { pending.delete(f.id); continue; }
        const previous = anchors.has(f.id) ? anchors.get(f.id) : f.before;
        const step = measure(previous, f.now, f.kind === 'gamma' ? gammaThreshold(previous) : f.threshold);
        const direction = Math.sign(step.delta);
        if (!step.material) { pending.delete(f.id); continue; }
        let p = pending.get(f.id);
        if (!p || p.direction !== direction) { p = {since: s.now, direction}; pending.set(f.id, p); }
        if (s.now - p.since >= POLICY.holdMs) {
          events.unshift({at: s.now, title: f.title, before: previous, now: f.now,
            delta: step.delta, unit: f.unit, threshold: step.threshold,
            basis: anchors.has(f.id) ? 'since its previous logged change' : 'since saved snapshot'});
          anchors.set(f.id, f.now); pending.delete(f.id);
        }
      }
      for (const key of pending.keys()) if (!visible.has(key)) pending.delete(key);
      events = events.slice(0, POLICY.maxEvents);
      return {paused: false, ...analysis, events: [...events], snapshot: s,
        confirmations:priceConfirmations(s.bars,s.levels||[],s.now,Math.max(s.baseAt,confirmationFloor),s.radius)};
    }};
  }
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function explanation(r) {
    const net = r.facts[0], flip = r.facts[1], parts = [];
    const polarityChanged = Math.abs(net.before) >= POLICY.gammaAbs && Math.abs(net.now) >= POLICY.gammaAbs && Math.sign(net.before) !== Math.sign(net.now);
    if (polarityChanged && net.material)
      parts.push(`Estimated net gamma in the fixed nearby window turned ${net.now < 0 ? 'negative' : 'positive'}: ${signed(net.before)} → ${signed(net.now)} (Δ ${signed(net.delta)} contracts per 1% move).`);
    else if (Math.abs(net.now) < POLICY.gammaAbs)
      parts.push(`Net gamma is near zero (${signed(net.now)}); a small sign change here is not treated as a meaningful regime change.`);
    else parts.push(`Estimated net gamma is ${net.now < 0 ? 'negative' : 'positive'} at ${signed(net.now)}, ${net.material ? 'a material' : 'a small'} change of ${signed(net.delta)} since the saved snapshot.`);
    if (finite(r.distance) && finite(r.oldDistance)) {
      const crossed = Math.abs(r.distance) >= 5 && Math.abs(r.oldDistance) >= 5 && Math.sign(r.distance) !== Math.sign(r.oldDistance);
      if (crossed) parts.push(`Price is now ${r.distance < 0 ? 'below' : 'above'} the nearest flip after being ${r.oldDistance < 0 ? 'below' : 'above'} it at the snapshot. ` +
        (flip.material ? `The nearest modeled flip also shifted ${signed(flip.delta)} points; both locations changed.` : `The nearest flip changed only ${signed(flip.delta)} points, below its ${number(flip.threshold)}-point materiality threshold.`));
    }
    const node = r.facts.slice(2).find(f => f.material);
    if (node) parts.push(`${node.title.replace(' (strongest now)', '')}: ${signed(node.before)} → ${signed(node.now)} (Δ ${signed(node.delta)}). Its magnitude ${Math.abs(node.now) > Math.abs(node.before) ? 'increased' : 'decreased'} under the same saved book.`);
    parts.push('OI and the volatility smile are held at the snapshot; these changes do not show new positions being added.');
    return parts.join(' ');
  }
  function render(r) {
    const s = r.snapshot || {}, mode = s.mode === 'real' ? 'LIVE' : s.mode === 'replay' ? 'REPLAY' : s.mode === 'sim' ? 'SIMULATION' : 'WAITING';
    const header = `<div class="lle-meta"><b>${mode}${r.paused ? ' · PAUSED' : ''}</b> · ${escape(s.label || 'NQ')} ${finite(s.lastMs) ? '· latest trade ' + stamp(s.lastMs) + ' PT' : ''}</div>`;
    if (r.paused) return header + `<p class="lle-warning">${escape(r.reason)}</p>`;
    const rows = r.facts.map(f => {
      const pct = f.kind === 'gamma' && f.percent !== null ? ` (${signed(f.percent)}% of |snapshot|)` : '';
      const missing = !finite(f.delta);
      const detail = missing ? 'No comparable flip inside the ±500-point model search; this does not establish that no flip exists.' :
        `Material at ≥${number(f.threshold)} ${f.unit}.`;
      return `<div class="lle-row"><div><b>${escape(f.title)}</b><small>${escape(f.unit)}</small></div>` +
        `<div class="mono">${number(f.before)} → ${number(f.now)}<small>Δ ${signed(f.delta)}${pct}</small></div>` +
        `<div><b class="${f.material ? 'lle-material' : 'dim'}">${missing ? 'Unavailable' : f.material ? 'Material size' : 'Small change'}</b><small>${escape(detail)}</small></div></div>`;
    }).join('');
    const flipText = r.distance === null ? 'No comparable current flip in the model search.' :
      `Price is ${number(Math.abs(r.distance))} points ${r.distance >= 0 ? 'above' : 'below'} the current modeled flip` +
      (r.oldDistance === null ? '.' : `; at the snapshot it was ${number(Math.abs(r.oldDistance))} points ${r.oldDistance >= 0 ? 'above' : 'below'}.`);
    const top = r.strongest === null ? 'No nonzero nearby strike exposure.' :
      `Strongest now: ${number(r.strongest)}, ${number(r.share)}% of summed absolute net-strike gamma in this window.`;
    const log = r.events.length ? '<ol class="lle-log">' + r.events.map(e => `<li><time>${stamp(e.at)} PT</time> <b>${escape(e.title)}</b>: ${number(e.before)} → ${number(e.now)} (Δ ${signed(e.delta)} ${escape(e.unit)}, ${escape(e.basis)}). The ${number(Math.abs(e.delta))} ${escape(e.unit)} change met the ${number(e.threshold)} threshold and persisted for 30 seconds. ${e.unit==='NQ points'?'The modeled crossing moved; price acceptance around it requires separate confirmation.':'This is a change in modeled exposure under the same OI snapshot, not evidence of new option trades.'}</li>`).join('') + '</ol>' :
      '<p class="dim">No material changes have persisted for 30 seconds during this observation period.</p>';
    const conf = r.confirmations || {signals:[],ready:false,reason:'Waiting for bars.'};
    const confirmations = '<details><summary>Price confirmations at saved levels (' + conf.signals.length + ')</summary>' +
      (conf.signals.length ? '<ol class="lle-log">'+conf.signals.map(c=>`<li><time>${stamp(c.at)} PT</time> <b>${escape(c.kind)} · ${escape(c.label)} ${number(c.price)}</b>. ${escape(c.detail)} Price now ${s.current.last>=c.price?'above':'below'} this level.</li>`).join('')+'</ol>' :
        `<p class="dim">${escape(conf.ready?'No qualifying price confirmations in the last 30 minutes.':conf.reason)}</p>`) +
      `<p class="dim">Price confirmation only, not an entry signal. Uses the saved snapshot’s strikes and flip (not today’s moving flip applied to earlier prices). Two consecutive completed one-minute closes beyond a ${number(conf.buffer)}-point buffer are required, with a prior completed bar to establish the approach. Buffer = max(1 point, 0.25% of full-session EM), rounded up to a tick; provisional. Reclaims start on the opposite side; failed reclaims require an intrabar retest and two closes back below. Events are reconstructed from recorded bars after the snapshot; a feed gap resets confirmation history and a missing minute or partial first bar cannot confirm.</p></details>`;
    const recent = conf.signals[0];
    const priceRead = recent ? `<p><b>Latest price confirmation · ${stamp(recent.at)} PT:</b> ${escape(recent.kind)} at ${number(recent.price)}. ${escape(recent.detail)} Price now ${s.current.last >= recent.price ? 'above' : 'below'} the saved level; this event does not assert a current entry.</p>` :
      `<p class="dim">Price confirmation: ${escape(conf.ready ? 'none under the two-close rule in the last 30 minutes.' : conf.reason)}</p>`;
    const fw=s.flow&&s.flow.windows&&s.flow.windows['60'];
    const flowRead=fw?`<p><b>Current futures pressure · 60s:</b> buy ${number(fw.buy)}, sell ${number(fw.sell)}, unknown ${number(fw.unknown)}; Δ ${signed(fw.delta)} contracts, price displacement ${fw.displacement==null?'unavailable':signed(fw.displacement)+' points'}, classified coverage ${fw.coverage==null?'unavailable':number(fw.coverage*100)+'%'} (${number(fw.count)} prints). Completed seconds ending ${stamp(s.flow.end_ms)} PT. This describes current tape; it does not confirm an earlier ladder event or establish dealer positioning.</p>`:'<p class="dim">Futures pressure unavailable: waiting for a fresh, matching chart feed.</p>';
    const net=r.facts[0];
    const quickGamma=`<p><b>Gamma:</b> ${signed(net.now)} · Δ ${signed(net.delta)}${net.percent===null?'':` (${signed(net.percent)}%)`} · ${net.material?'material size':'small change'}.</p>`;
    const quickFlip=`<p><b>Flip:</b> ${r.distance===null?'unavailable':`${number(s.current.flip)} · price ${number(Math.abs(r.distance))} pt ${r.distance>=0?'above':'below'}`}.</p>`;
    const quickFlow=fw?`<p><b>Flow · 60s:</b> Δ ${signed(fw.delta)} contracts · price ${signed(fw.displacement)} pt · ${number(fw.count)} prints · ${fw.coverage==null?'coverage unavailable':number(fw.coverage*100)+'% classified'}.</p>`:'<p class="dim"><b>Flow:</b> waiting for a fresh, matching feed.</p>';
    const quickConfirmation=recent?`<p><b>Price:</b> ${escape(recent.kind)} at ${number(recent.price)} · ${stamp(recent.at)} PT.</p>`:`<p class="dim"><b>Price:</b> ${conf.ready?'no confirmed level break/reclaim.':'waiting for completed bars.'}</p>`;
    const latest=r.events[0];
    const quickEvent=latest?`<p><b>Ladder · ${stamp(latest.at)}:</b> ${escape(latest.title)} Δ ${signed(latest.delta)} ${escape(latest.unit)} · held 30s.</p>`:'<p class="dim"><b>Ladder:</b> no material change held for 30s.</p>';
    return header + quickGamma + quickFlip + quickFlow + quickConfirmation + quickEvent +
      '<p class="dim">Gamma uses saved OI/IV · materiality is provisional.</p>' +
      `<details><summary>Full explanation and measurements</summary><p>${escape(explanation(r))}</p>` + detailedRead(r).map(p=>`<p>${escape(p)}</p>`).join('') + `<p>Snapshot price ${number(s.center)} → ${number(s.current.last)} (Δ ${signed(r.spot.delta)} points). ${escape(flipText)}</p>` + rows + flowRead + `<p>${escape(top)}</p>${priceRead}</details>` +
      `${confirmations}<details><summary>Recent confirmed ladder changes (${r.events.length})</summary>${log}</details>` +
      `<details class="lle-method"><summary>Comparison basis and significance</summary><p>Compared with ${escape(s.asof)} · OI ${escape(s.settle)} (${escape(s.vintage)}). Fixed window ${number(s.center-s.radius)}–${number(s.center+s.radius)}: snapshot price ± one full-session expected move. Book/expiry changes or a new snapshot start a new comparison.</p>` +
      '<p>Gamma materiality: at least 10 contracts per 1% move AND 20% of the absolute comparison value. Flip materiality: at least 5 points AND 2% of the full-session expected move. Percentages are suppressed for gamma baselines below 10. These are provisional display thresholds, not statistical significance, probabilities, or trading signals. Log thresholds apply since the prior logged change; rows always compare with the saved snapshot.</p>' +
      '<p>Uses the existing ladder model, saved OI and saved volatility smile, repriced with price and time. It does not observe new positioning or current option quotes. The nearest flip may switch between crossings. Changes do not establish buying, selling, support, or a trade entry. The log is local to this page and resets after a feed gap; it is not a persisted trading journal.</p></details>';
  }
  function detailedRead(r) {
    const s=r.snapshot, net=r.facts[0], flip=r.facts[1], paragraphs=[];
    paragraphs.push(`Size and significance: Net exposure moved from ${signed(net.before)} to ${signed(net.now)} contracts per 1% move. `+
      (net.percent===null?'The starting exposure was too small for a useful percentage comparison. ':`The signed change is ${signed(net.percent)}% of the absolute snapshot exposure. `)+
      `Its absolute change of ${number(Math.abs(net.delta))} ${net.material?'meets':'does not meet'} the ${number(net.threshold)}-contract display threshold. `+
      (net.material?'That flags a material-sized observation; only changes sustained for 30 seconds enter the event log. ':'The current observation stays below the materiality filter even if the displayed number changes on every update. ')+
      'These thresholds are provisional filters, not a measured probability of a price move.');
    paragraphs.push(`Model drivers: Price is ${signed(r.spot.delta)} points from the snapshot reference, and the clock has advanced. `+
      'The ladder reprices the same open interest using the snapshot volatility smile. Price, remaining time, and movement along that smile can change the exposure; this update does not separately attribute how much came from each. '+
      'An increase in negative exposure therefore does not establish that traders added shorts, and a positive change does not establish new buying.');
    if(finite(flip.delta)) paragraphs.push(`Flip interpretation: The nearest modeled crossing moved from ${number(flip.before)} to ${number(flip.now)}, a ${signed(flip.delta)}-point shift. `+
      (flip.material?'The crossing itself moved enough to pass its size filter, so both the level and price location need to be tracked. ':'The crossing stayed below its movement threshold; a change in which side price occupies should be distinguished from a material shift in the crossing. ')+
      'The nearby net-exposure window and the flip calculation summarize different parts of the book, so their signs need not change together. Neither gamma sign nor a flip touch alone establishes direction or acceptance.');
    else paragraphs.push('Flip interpretation: There is no comparable crossing available in the model search. That is missing evidence, not proof that the market has no flip. Use the available strike levels without inventing a crossing.');
    const conf=r.confirmations||{},latest=conf.signals&&conf.signals[0];
    if(latest) paragraphs.push(`Confirmation and invalidation: The latest recorded confirmation is ${latest.kind.toLowerCase()} at ${number(latest.price)}. `+
      `Price currently sits ${s.current.last>=latest.price?'at or above':'below'} that level. Further completed bars holding the same side would support continued acceptance; a buffered two-close move back through it would challenge that reading. `+
      'A current tick on the other side is an early change to watch, not by itself a new confirmed event or an executable entry.');
    else paragraphs.push(`Evidence still needed: ${conf.ready?'No qualifying price confirmation has been recorded under the current rule.':conf.reason||'Completed bars are not yet available.'} `+
      'Watch the selected snapshot strikes and flip for a defined reclaim, loss, or retest followed by the required completed closes. Until then, the ladder describes changing exposure rather than confirmed acceptance.');
    const w=s.flow&&s.flow.windows&&s.flow.windows['60'];
    if(w&&finite(w.displacement)&&w.count>0){
      const delta=w.delta, move=w.displacement;
      const read=delta>0?(move>0?'Buyer-initiated volume exceeded seller-initiated volume and price advanced over the same window.':move<0?'Buyer-initiated volume exceeded seller-initiated volume, but price still fell over the same window.':'Buyer-initiated volume exceeded seller-initiated volume without net upward displacement.'):
        delta<0?(move<0?'Seller-initiated volume exceeded buyer-initiated volume and price declined over the same window.':move>0?'Seller-initiated volume exceeded buyer-initiated volume, but price still rose over the same window.':'Seller-initiated volume exceeded buyer-initiated volume without net downward displacement.'):'Classified buying and selling were balanced over the current window.';
      paragraphs.push('Order-flow context: '+read+' '+(delta*move<0?'Aggressor pressure and net price response diverged; this alone cannot identify absorption or a reversal. ':'This is a simultaneous observation, not a forecast. ')+
        'Read it alongside the print count and classification coverage below. The window is not matched retrospectively to earlier confirmations, and activity has not been normalized for this session.');
    }else paragraphs.push('Order-flow context: A usable current order-flow window is unavailable. The price and gamma observations remain separate; no flow confirmation is assumed.');
    return paragraphs;
  }
  function mount(getSnapshot, doc = document) {
    const tracker = createTracker();
    function refresh() {
      let result;
      try { result = tracker.update(getSnapshot()); }
      catch (e) { result = tracker.update({error: 'Live ladder calculation unavailable: ' + e.message}); }
      const html = render(result);
      for (const el of doc.querySelectorAll('[data-live-ladder-explanation]')) {
        if (el._ladderHtml === html) continue;
        const open = [...el.querySelectorAll('details')].map(d => d.open);
        const scrollTop = el.scrollTop;
        el.innerHTML = html; el._ladderHtml = html;
        [...el.querySelectorAll('details')].forEach((d,i) => { d.open = !!open[i]; });
        el.scrollTop = scrollTop;
      }
    }
    refresh(); const timer = setInterval(refresh, 2000);
    return {refresh, stop: () => clearInterval(timer)};
  }
  return {POLICY, measure, compare, reason, createMinuteBars, priceConfirmations, createTracker, explanation, detailedRead, render, mount};
});
