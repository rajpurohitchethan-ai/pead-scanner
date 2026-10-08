(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const S = { data: null, tab: 'setups', q: '', liquid: true, sort: 'conviction', bucket: 'ALL', open: new Set() };

  // ---------- storage (per-viewer conveniences only) ----------
  const store = {
    get(k, d) { try { const v = localStorage.getItem('pead.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem('pead.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  // ---------- formatting ----------
  const num = v => (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) ? null : Number(v);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (v, d = 1) => { const n = num(v); return n === null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(d)}%`; };
  const cls = v => { const n = num(v); return n === null ? '' : n > 0 ? 'up' : n < 0 ? 'down' : ''; };
  // 2.5.3: when the filing has no last-year column (common on BSE), show the
  // quarter-on-quarter numbers under an honest heading instead of dashes.
  const resultFact = (it, m) => {
    const yoy = num(it.revenueYoY) !== null || num(it.patYoY) !== null || it.patYoYStatus === 'TURNAROUND';
    const qRev = num(m.revenueQoQ), qPat = num(m.patQoQ ?? it.patQoQ);
    const useQ = !yoy && (qRev !== null || qPat !== null);
    const bps = num(m.changeBps);
    const mBasis = bps === null ? '' : (useQ || m.basis !== 'QoQ') ? '' : ' vs last qtr';
    const margin = bps !== null ? `, margin ${bps > 0 ? '+' : ''}${bps} bps${mBasis}` : '';
    if (useQ) {
      return `<div class="fact"><h3>Result vs last quarter</h3><p><span class="big ${cls(qRev)}">${pct(qRev)}</span>revenue</p>
          <p><span class="big ${cls(qPat)}">${pct(qPat)}</span>profit${margin}</p><p class="note">Year-ago figures not in the filing yet</p></div>`;
    }
    return `<div class="fact"><h3>Result vs last year</h3><p><span class="big ${cls(it.revenueYoY)}">${pct(it.revenueYoY)}</span>revenue</p>
          <p><span class="big ${cls(it.patYoY)}">${it.patYoYStatus === 'TURNAROUND' ? 'Turnaround' : pct(it.patYoY)}</span>profit${margin}</p></div>`;
  };
  const inr = (v, d = 2) => { const n = num(v); return n === null ? '—' : '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }); };
  const cr = v => { const n = num(v); if (n === null) return '—'; return n >= 1000 ? `₹${Math.round(n).toLocaleString('en-IN')} Cr` : `₹${n.toFixed(n >= 10 ? 0 : 1)} Cr`; };
  const x = v => { const n = num(v); return n === null ? '—' : `${n.toFixed(2)}x`; };
  const dt = v => { if (!v) return '—'; const d = new Date(String(v).slice(0, 10) + 'T00:00:00'); return Number.isNaN(+d) ? '—' : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); };
  const daysTo = v => { if (!v) return null; const d = new Date(String(v).slice(0, 10) + 'T00:00:00'); const t = new Date(); t.setHours(0, 0, 0, 0); return Math.round((d - t) / 864e5); };

  // ---------- vocabulary ----------
  const SIGNAL = {
    ENTRY_EARLY: ['Early entry triggered', 'go'], ENTRY_PULLBACK: ['Pullback entry triggered', 'go'],
    ENTRY_BREAKOUT: ['Breakout entry triggered', 'go'], NEAR_ENTRY: ['Near entry', 'go'],
    PULLBACK_ZONE: ['In pullback zone', 'go'], WATCH: ['Watching the base', 'watch'],
    EXTENDED: ['Extended, wait for pullback', 'watch'], RISK_TOO_WIDE: ['Stop too wide', 'watch'],
    WAIT_CONCALL: ['Waiting for concall', 'watch'],
    NO_ENTRY: ['No entry', 'no'], DATA_PENDING: ['Data pending', 'pending'],
    WAIT_REACTION: ['Awaiting reaction session', 'pending'], WAIT_RESULT: ['Awaiting result', 'pending'],
  };
  const sig = it => SIGNAL[it.plus?.plan?.signal || it.entrySignal] || [String(it.entrySignal || '—').replace(/_/g, ' ').toLowerCase(), 'pending'];
  const BUCKET_TONE = { CONFIRMATION: 'go', RE_PEAD: 'go', FRESH_PEAD: 'go', NO_CONFIRMATION: 'bad', WATCH_CONFIRM: 'good', WATCH_REPEAD: 'warn', WATCH_FRESH: '', PENDING: '' };
  const TAIL_TONE = { STRONG: 'good', POSITIVE: 'good', NEUTRAL: '', WEAK: 'bad' };
  const STRENGTH = { STRONG: ['Strong earnings', 'good'], 'AVERAGE+': ['Decent earnings', 'good'], AVERAGE: ['Average earnings', 'warn'], WEAK: ['Weak earnings', 'bad'] };

  // ---------- data access ----------
  const P = it => it.plus || {};
  const px = it => P(it).price || {};
  const liquidOk = it => P(it).liquidity?.pass !== false;
  const declared = () => (S.data?.items || []).filter(it => it.resultsReleased);
  const upcoming = () => (S.data?.items || []).filter(it => !it.resultsReleased);
  const matches = it => {
    if (!S.q) return true;
    const h = [it.symbol, it.name, it.sector, it.industry, P(it).sectorKey].join(' ').toLowerCase();
    return h.includes(S.q.toLowerCase());
  };

  // ---------- chart: price + 21 EMA + reaction marker + plan levels ----------
  function chart(it, h = 120) {
    const c = P(it).chart;
    const closes = (c?.close || []).map(num);
    if (closes.filter(v => v !== null).length < 5) return '<p class="meta">Price chart pending.</p>';
    const plan = P(it).plan || {};
    const W = 340, H = h, pad = 6;
    const ema = (c.ema21 || []).map(num);
    const levels = [num(plan.entry), num(plan.sl)].filter(v => v !== null);
    const all = closes.concat(ema).concat(levels).filter(v => v !== null);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi === lo) { hi += 1; lo -= 1; }
    const span = hi - lo; lo -= span * 0.06; hi += span * 0.06;
    const xs = i => pad + i * (W - 2 * pad) / Math.max(1, closes.length - 1);
    const ys = v => H - pad - (v - lo) / (hi - lo) * (H - 2 * pad);
    const path = arr => arr.map((v, i) => v === null ? null : `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).filter(Boolean).join(' ');
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(it.symbol)} price chart">`;
    for (let k = 1; k < 4; k++) svg += `<line x1="0" x2="${W}" y1="${(H * k / 4).toFixed(1)}" y2="${(H * k / 4).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    if (num(plan.sl) !== null && num(plan.entry) !== null) {
      const y1 = ys(plan.entry), y2 = ys(plan.sl);
      svg += `<rect x="0" y="${Math.min(y1, y2).toFixed(1)}" width="${W}" height="${Math.abs(y2 - y1).toFixed(1)}" fill="var(--loss)" opacity=".07"/>`;
    }
    const ri = num(c.reactionIndex);
    if (ri !== null && ri >= 0 && ri < closes.length) svg += `<line x1="${xs(ri)}" x2="${xs(ri)}" y1="0" y2="${H}" stroke="var(--watch)" stroke-width="1.5" stroke-dasharray="3 3"/>`;
    svg += `<polyline points="${path(ema)}" fill="none" stroke="var(--ema)" stroke-width="1.4" stroke-dasharray="4 3"/>`;
    svg += `<polyline points="${path(closes)}" fill="none" stroke="var(--price)" stroke-width="1.8" stroke-linejoin="round"/>`;
    if (num(plan.entry) !== null) svg += `<line x1="0" x2="${W}" y1="${ys(plan.entry).toFixed(1)}" y2="${ys(plan.entry).toFixed(1)}" stroke="var(--act)" stroke-width="1.6"/>`;
    if (num(plan.sl) !== null) svg += `<line x1="0" x2="${W}" y1="${ys(plan.sl).toFixed(1)}" y2="${ys(plan.sl).toFixed(1)}" stroke="var(--loss)" stroke-width="1.6"/>`;
    const li = closes.length - 1;
    if (closes[li] !== null) svg += `<circle cx="${xs(li)}" cy="${ys(closes[li])}" r="3.2" fill="var(--price)"/>`;
    svg += '</svg>';
    const legend = `<div class="legend"><span><i></i>Close</span><span><i class="lm"></i>21 EMA</span>${ri !== null ? '<span><i style="background:var(--watch)"></i>Result day</span>' : ''}${num(plan.entry) !== null ? '<span><i class="le"></i>Entry</span>' : ''}${num(plan.sl) !== null ? '<span><i class="ls"></i>Stop</span>' : ''}</div>`;
    return svg + legend;
  }

  // ---------- position size ----------
  function sizing(plan) {
    const e = num(plan.entry), s = num(plan.sl);
    const cap = num(store.get('cap', 1000000)), risk = num(store.get('risk', 1));
    if (e === null || s === null || e <= s || !cap || !risk) return '';
    const frac = num(plan.sizeFraction) ?? 1;
    const fullQty = Math.floor(cap * risk / 100 / (e - s));
    const qty = Math.floor(fullQty * frac);
    if (qty < 1) return '<p class="qty">Risk budget is smaller than one share’s stop distance.</p>';
    const atRisk = qty * (e - s), used = qty * e;
    const lead = frac < 1 ? `Starter: ${qty.toLocaleString('en-IN')} of ${fullQty.toLocaleString('en-IN')} shares` : `${qty.toLocaleString('en-IN')} shares`;
    return `<p class="qty">${lead} risks ${inr(atRisk, 0)} and uses ${inr(used, 0)} (${(used / cap * 100).toFixed(0)}% of capital).</p>`;
  }

  // ---------- concall ----------
  function concallLine(c) {
    if (!c) return '';
    const link = (url, label) => url ? ` <a href="${esc(url)}" target="_blank" rel="noopener">${label}</a>` : '';
    if (c.status === 'SCHEDULED') return `<p class="meta call">Concall ${c.callDate ? dt(c.callDate) : 'announced, date in filing'}.${link(c.noticeUrl, 'Notice')}</p>`;
    if (c.status === 'DONE') return `<p class="meta call">Concall held${c.callDate ? ' ' + dt(c.callDate) : ''}.${link(c.transcriptUrl, 'Transcript')}${link(c.audioUrl, 'Audio')}${!c.transcriptUrl && !c.audioUrl ? link(c.noticeUrl, 'Notice') : ''}</p>`;
    return '<p class="meta call">No concall filed yet.</p>';
  }

  // ---------- setup card ----------
  function card(it) {
    const p = P(it), x1 = px(it), plan = p.plan || {};
    const [sLabel, sTone] = sig(it);
    const rail = { go: 's-go', watch: 's-watch', no: 's-no', pending: 's-pending' }[sTone] || 's-pending';
    const b = p.bucket, st = STRENGTH[p.strength], tail = p.sector?.tailwind, liq = p.liquidity || {};
    const tags = [];
    if (p.sectorKey) tags.push(`<span class="tag" title="${esc(it.industry || '')}">${esc(p.sectorKey)}${it.industry ? ` · ${esc(it.industry)}` : ''}</span>`);
    if (b) tags.push(`<span class="tag ${BUCKET_TONE[b.code] || ''}" title="${esc(b.why)}">${esc(b.label)}</span>`);
    if (st) tags.push(`<span class="tag ${st[1]}">${st[0]}</span>`);
    if (tail) tags.push(`<span class="tag ${TAIL_TONE[tail] || ''}">Sector ${tail.toLowerCase()}</span>`);
    if (x1.hv_label) tags.push(`<span class="tag good" title="Highest volume on the result session">${esc(x1.hv_label)}</span>`);
    if (liq.pass === false) tags.push(`<span class="tag bad">${esc(liq.label)}</span>`);
    const m = p.margins || {}, v = p.valuation || {};
    const pc = it.priceContext || {};
    const facts = `
      <div class="facts">
        ${resultFact(it, m)}
        <div class="fact"><h3>Market reaction</h3><p><span class="big ${cls(pc.resultDayPct)}">${pct(pc.resultDayPct)}</span>${num(pc.relativeVolume) !== null ? `on ${x(pc.relativeVolume)} usual volume` : 'result session'}</p>
          <p><span class="big ${cls(x1.return_since_result_pct)}">${pct(x1.return_since_result_pct)}</span>since result${x1.sessions_since_reaction != null ? `, ${x1.sessions_since_reaction} sessions` : ''}</p></div>
        <div class="fact"><h3>Expectations before</h3><p><span class="big ${cls(pc.pre20dPct)}">${pct(pc.pre20dPct)}</span>20-day run-up</p>
          <p><span class="big">${pct(pc.distanceFrom52wHighPct)}</span>from 52-week high</p></div>
        <div class="fact"><h3>Valuation</h3><p><span class="big">${num(v.pe) !== null ? num(v.pe).toFixed(1) + 'x' : '—'}</span>P/E${num(v.sectorPe) !== null && Math.abs(num(v.sectorPe) - (num(v.pe) ?? 0)) >= 0.05 ? ` vs sector ${num(v.sectorPe).toFixed(1)}x` : ''}</p>
          <p><span class="big">${v.tinyBook ? 'n/m' : num(v.roe) !== null ? num(v.roe).toFixed(1) + '%' : '—'}</span>${v.tinyBook ? `ROE (${esc(v.bookNote || 'tiny book value')}, P/B ${num(v.pb) !== null ? num(v.pb).toFixed(0) + 'x' : 'n/a'})` : 'ROE'}${num(it.marketCapCr) !== null ? `, market cap ${cr(it.marketCapCr)}` : ''}</p></div>
      </div>`;
    const lv = (label, val, k = '') => `<div class="lv ${k}"><span>${label}</span><b>${val}</b></div>`;
    const levels = num(plan.entry) !== null ? `<div class="levels">
        ${lv('Entry above', inr(plan.entry), 'e')}${lv('Stop loss', inr(plan.sl), 's')}
        ${lv('Risk', pct(plan.riskPct).replace('+', ''))}${lv('Trail swing / position', `${inr(plan.tslSwing, 0)} / ${inr(plan.tslPosition, 0)}`)}
      </div><p class="qty">Trail stays at the stop until the trade is up 1R, then moves to cost and follows the 21 EMA (swing) or 63 EMA (position).</p>${sizing(plan)}` : '';
    const reasons = (it.reasons || []).slice(0, 6), risks = (it.risks || []).slice(0, 6);
    const links = p.links || {};
    const linkHtml = [['screener', 'Screener'], ['tradingview', 'TradingView'], ['nse', 'NSE'], ['bse', 'BSE']]
      .filter(([k]) => links[k]).map(([k, l]) => `<a href="${esc(links[k])}" target="_blank" rel="noopener">${l}</a>`).join('');
    return `<article class="card ${rail} ${liq.pass === false ? 'dim' : ''}">
      <div class="head"><div><h2 class="sym">${esc(it.symbol)}</h2><div class="co">${esc(it.name)}</div></div>
        <div class="score"><div class="v">${num(it.convictionScore) ?? '—'}</div><div class="l">conviction</div></div></div>
      <div class="tags">${tags.join('')}</div>
      <div class="meta">Result ${dt(it.resultDate)}${it.reactionSession ? `, reaction ${it.reactionWindowStart && it.reactionWindowStart !== it.reactionSession ? dt(it.reactionWindowStart) + '–' : ''}${dt(it.reactionSession)}` : ''}${it.reactionWindowStart && it.reactionWindowStart !== it.reactionSession ? ' (filed in market hours)' : ''}${num(pc.lastClose) !== null ? `, last ${inr(pc.lastClose)}` : ''}</div>
      ${concallLine(p.concall)}
      ${chart(it)}
      ${facts}
      <div class="plan"><div class="plan-top"><span class="plan-sig ${sTone}">${sLabel}${plan.stage === 'STARTER' && num(plan.entry) !== null ? ' <span class="tag warn">Starter, 1/3 size</span>' : plan.stage === 'FULL' && num(plan.entry) !== null ? ' <span class="tag go">Full size</span>' : ''}</span>${num(plan.rNow) !== null ? `<span class="n">${num(plan.rNow).toFixed(1)}R</span>` : ''}</div>
        <p class="plan-why">${esc(plan.why || 'Plan not computed yet.')}</p>${levels}</div>
      ${(reasons.length || risks.length) ? `<details class="why"><summary>Why it scores ${num(it.convictionScore) ?? '—'}</summary><div class="why-cols">
        <div class="pos"><h4>Supporting</h4><ul>${reasons.map(r => `<li>${esc(r)}</li>`).join('') || '<li>Nothing verified yet.</li>'}</ul></div>
        <div class="neg"><h4>Risks and gaps</h4><ul>${risks.map(r => `<li>${esc(r)}</li>`).join('') || '<li>None flagged.</li>'}</ul></div></div></details>` : ''}
      <div class="links">${linkHtml}</div>
    </article>`;
  }

  // ---------- views ----------
  function sorted(list) {
    const key = {
      conviction: it => num(it.convictionScore) ?? -1,
      date: it => -(daysTo(it.resultDate) ?? 999),
      reaction: it => num(it.priceContext?.resultDayPct) ?? -999,
      since: it => num(px(it).return_since_result_pct) ?? -999,
      runup: it => -(num(it.priceContext?.pre20dPct) ?? 999),
      distance: it => -Math.abs(num(P(it).plan?.distancePct) ?? 999),
    }[S.sort];
    return list.slice().sort((a, b) => key(b) - key(a));
  }

  function emptyHidden(total, shown, what) {
    const hidden = total - shown;
    if (hidden > 0 && S.liquid) return `<div class="empty">${hidden} ${what} hidden because they trade under ₹${S.data.thresholds?.liquidityTurnoverCr ?? 1} Cr a day or below ₹${S.data.thresholds?.liquidityMinPrice ?? 20}.<br><button type="button" data-act="show-illiquid">Show them</button></div>`;
    return `<div class="empty">No ${what} match this view yet.</div>`;
  }

  function viewSetups() {
    let list = declared().filter(matches);
    if (S.bucket !== 'ALL') list = list.filter(it => (P(it).bucket?.code || 'NONE') === S.bucket);
    const all = list.length;
    if (S.liquid) list = list.filter(liquidOk);
    if (!list.length) return emptyHidden(all, 0, 'declared results');
    return `<div class="grid">${sorted(list).map(card).join('')}</div>${all > list.length ? `<p class="foot">${all - list.length} illiquid results hidden.</p>` : ''}`;
  }

  function ledgerRow(it) {
    const x1 = px(it), b = P(it).bucket, open = S.open.has(it.eventId), d = daysTo(it.resultDate);
    const row = `<div class="row" role="button" tabindex="0" aria-expanded="${open}" data-id="${esc(it.eventId)}">
      <div><div class="s">${esc(it.symbol)}</div><div class="nm">${esc(P(it).sectorKey || it.name)}</div></div>
      <div class="opt">${b ? `<span class="tag ${BUCKET_TONE[b.code] || ''}" title="${esc(b.why)}">${esc(b.label)}</span>` : '<span class="nm">No Q1 data</span>'}</div>
      <div><div class="k">20-day run-up</div><div class="v ${cls(it.priceContext?.pre20dPct)}">${pct(it.priceContext?.pre20dPct)}</div></div>
      <div class="opt"><div class="k">Q1 reaction</div><div class="v ${cls(x1.q1_reaction_return_pct)}">${pct(x1.q1_reaction_return_pct)}</div></div>
      <div><div class="k">${d !== null && d >= 0 ? 'Result' : '52W high'}</div><div class="v">${d === 0 ? 'Today' : d !== null && d > 0 ? `in ${d}d` : pct(it.priceContext?.distanceFrom52wHighPct)}</div></div>
      <div class="chev" aria-hidden="true">›</div></div>`;
    if (!open) return row;
    const v = P(it).valuation || {}, liq = P(it).liquidity || {};
    return row + `<div class="row-detail">${chart(it, 100)}
      <p class="note">${b ? esc(b.why) + ' ' : ''}${num(x1.q1_reaction_return_pct) !== null ? `Q1 result day ${pct(x1.q1_reaction_return_pct)}${num(x1.q1_reaction_rvol) !== null ? ` on ${x(x1.q1_reaction_rvol)} usual volume` : ''}; ${pct(x1.q1_return_to_q2_pct)} from before the Q1 result to now. ` : 'Q1 result date not found yet. '}${num(v.pe) !== null ? `P/E ${num(v.pe).toFixed(1)}x${num(v.sectorPe) !== null ? ` against sector ${num(v.sectorPe).toFixed(1)}x` : ''}. ` : ''}Liquidity ${esc(liq.label || '—')}${num(it.marketCapCr) !== null ? `, market cap ${cr(it.marketCapCr)}` : ''}.</p>
      <div class="links">${[['screener', 'Screener'], ['tradingview', 'TradingView'], ['nse', 'NSE'], ['bse', 'BSE']].filter(([k]) => P(it).links?.[k]).map(([k, l]) => `<a href="${esc(P(it).links[k])}" target="_blank" rel="noopener">${l}</a>`).join('')}</div></div>`;
  }

  function viewWatch() {
    let list = upcoming().filter(matches);
    if (S.bucket !== 'ALL') list = list.filter(it => (P(it).bucket?.code || 'NONE') === S.bucket);
    const all = list.length;
    if (S.liquid) list = list.filter(liquidOk);
    if (!list.length) return emptyHidden(all, 0, 'upcoming results');
    const groups = new Map();
    list.sort((a, b) => String(a.resultDate || '9').localeCompare(String(b.resultDate || '9')) || (num(b.convictionScore) ?? 0) - (num(a.convictionScore) ?? 0));
    for (const it of list) { const k = it.resultDate || 'Date not announced'; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(it); }
    const hdr = '<div class="hdr" aria-hidden="true"><span>Company</span><span class="opt">Q1 → Q2 bucket</span><span>Run-up</span><span class="opt">Q1 move</span><span>When</span><span></span></div>';
    let html = '';
    for (const [k, rows] of groups) {
      const d = daysTo(k);
      html += `<h2 class="day">${k === 'Date not announced' ? k : new Date(k + 'T00:00:00').toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' })}<small>${rows.length} ${rows.length === 1 ? 'company' : 'companies'}${d === 0 ? ', today' : d === 1 ? ', tomorrow' : d !== null && d > 1 ? `, in ${d} days` : ''}</small></h2>${hdr}<div class="ledger">${rows.map(ledgerRow).join('')}</div>`;
    }
    return html + (all > list.length ? `<p class="foot">${all - list.length} illiquid companies hidden.</p>` : '');
  }

  function viewSectors() {
    const rows = (S.data.sectors || []).filter(r => !S.q || r.sector.toLowerCase().includes(S.q.toLowerCase()));
    if (!rows.length) return '<div class="empty">Sector data appears once prices are tracked.</div>';
    const max = Math.max(10, ...rows.map(r => Math.abs(num(r.relativeToMarket) ?? 0)));
    const bar = v => { const n = num(v); if (n === null) return '—'; const w = Math.abs(n) / max * 50; return `<div class="bar" title="${pct(n)} vs market"><i style="left:${n >= 0 ? 50 : 50 - w}%;width:${w}%;background:${n >= 0 ? 'var(--gain)' : 'var(--loss)'}"></i><i style="left:50%;width:1px;background:var(--ink-3)"></i></div>`; };
    return `<p class="note">Tailwind combines how tracked peers moved over 3 months versus the whole universe, and how many peers that already reported had strong earnings.</p>
      <table><thead><tr><th>Sector</th><th class="hide-sm">3-month vs market</th><th class="r">Relative</th><th class="r hide-sm">Reported</th><th class="r">Strong</th><th>Tailwind</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${esc(r.sector)} <span class="meta">(${r.stocks})</span></td><td class="hide-sm">${bar(r.relativeToMarket)}</td><td class="r n ${cls(r.relativeToMarket)}">${pct(r.relativeToMarket)}</td><td class="r n hide-sm">${r.declared}</td><td class="r n">${r.strongResults}</td><td>${r.tailwind ? `<span class="tag ${TAIL_TONE[r.tailwind] || ''}">${r.tailwind.toLowerCase()}</span>` : '—'}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function viewScore() {
    const sc = S.data.scorecard || { rows: [] };
    return `<p class="note">${esc(sc.metric)}. This checks the core idea behind the buckets: did stocks with a strong Q1 reaction keep drifting? It fills in as more results are tracked; treat small samples as anecdotes.</p>
      <table><thead><tr><th>Group</th><th class="r">Stocks</th><th class="r">Average</th><th class="r">Median</th><th class="r">Positive</th></tr></thead><tbody>
      ${sc.rows.map(r => `<tr><td>${esc(r.group)}</td><td class="r n">${r.n}</td><td class="r n ${cls(r.avg)}">${pct(r.avg)}</td><td class="r n ${cls(r.median)}">${pct(r.median)}</td><td class="r n">${num(r.winRate) === null ? '—' : num(r.winRate).toFixed(0) + '%'}</td></tr>`).join('')}
      </tbody></table>
      <h2 class="day">Entry timing: on the result vs after the concall</h2>
      <p class="note">Every triggered entry is recorded once: the starter taken before the call, and the full entry after it (or when no call is held). Returns run to today, or to the stop if it was hit.</p>
      <table><thead><tr><th>Entry</th><th class="r">Trades</th><th class="r">Average</th><th class="r">Median</th><th class="r">Positive</th><th class="r">Stopped</th></tr></thead><tbody>
      ${(sc.timing || []).map(r => `<tr><td>${esc(r.group)}</td><td class="r n">${r.n}</td><td class="r n ${cls(r.avg)}">${pct(r.avg)}</td><td class="r n ${cls(r.median)}">${pct(r.median)}</td><td class="r n">${num(r.winRate) === null ? '—' : num(r.winRate).toFixed(0) + '%'}</td><td class="r n">${r.stopped ?? 0}</td></tr>`).join('')}
      </tbody></table>`;
  }

  function viewHealth() {
    const h = S.data.health || {}, c = S.data.counts || {};
    const box = (l, v) => `<div class="hbox"><span>${l}</span><b>${v ?? '—'}</b></div>`;
    return `<div class="health">
      ${box('Companies tracked', `${h.activeDashboardEvents ?? '—'} / ${h.eventsTracked ?? '—'}`)}
      ${box('Results declared', h.resultsFiled)}${box('Financials verified', h.financialsVerified)}
      ${box('Financials flagged', h.financialsFlagged)}${box('Reaction measured', h.reactionReady)}
      ${box('Tradeable (liquid)', c.liquid)}${box('Completeness of declared', h.declaredCompletenessPct != null ? Math.round(h.declaredCompletenessPct) + '%' : '—')}
      ${box('Fetch failures', h.fetchesFailed)}${box('Integrity revocations', h.integrity?.revoked)}${box('Duplicates merged', h.integrity?.duplicatesMerged)}${box('Wrong-company links fixed', h.integrity?.identityRepaired)}
      ${box('Price history ready', `${(S.data.items || []).filter(i => P(i).price).length} / ${(S.data.items || []).length}`)}
      ${box('Sector known', `${(S.data.items || []).filter(i => P(i).sectorKey).length} / ${(S.data.items || []).length}`)}
      ${box('Market data as of', esc(S.data.regime?.asOf || '—'))}
    </div><p class="foot">Engine ${esc(S.data.version || '')}. Generated ${S.data.generatedAt ? new Date(S.data.generatedAt).toLocaleString('en-IN') : '—'}.</p>`;
  }

  // ---------- header / chips ----------
  const PAGE_VERSION = '2.6.0';   // must match the engine version (install check)
  function installBanner() {
    const d = S.data || {}, ic = (d.health || {}).installCheck || {};
    const engine = ic.engine || (String(d.version || '').match(/(\d+\.\d+\.\d+)\s*$/) || [])[1];
    const bad = [];
    if (engine && engine !== PAGE_VERSION) bad.push(`page files are ${PAGE_VERSION} but the engine (pead_v2.py) is ${engine}`);
    if (ic.ok === false) bad.push(`pead_plus.py is ${ic.pead_plus} but pead_v2.py is ${ic.engine}`);
    return bad.length ? `<div class="install-bad"><b>Files out of sync.</b> ${esc(bad.join('; '))}. Re-upload all files from the same update.</div>` : '';
  }
  function header() {
    const d = S.data, r = d.regime || {}, c = d.counts || {};
    $('sub').textContent = `${d.liveQuarter || 'Live quarter'} results season, updated ${d.generatedAt ? new Date(d.generatedAt).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'}`;
    const tone = { 'RISK-ON': 'on', MIXED: 'mixed', 'RISK-OFF': 'off' }[r.label] || '';
    $('regime').innerHTML = r.label ? `<span class="dot ${tone}"></span><b>Market ${esc(r.label.toLowerCase())}</b>. ${esc(r.note)}${num(r.ret20dPct) !== null ? ` ${esc(r.index || 'Index')} ${pct(r.ret20dPct)} over 20 days.` : ''}` : `<span class="dot"></span>${esc(r.note && r.note !== 'Index history unavailable' ? r.note : 'Market regime appears after the next data refresh.')}`;
    const decl = declared(), act = decl.filter(it => sig(it)[1] === 'go').length;
    const soon = upcoming().filter(it => { const n = daysTo(it.resultDate); return n !== null && n >= 0 && n <= 7; }).length;
    $('season').innerHTML = installBanner() + `<strong>${decl.length}</strong> results declared, <strong>${act}</strong> with an actionable setup, <strong>${soon}</strong> companies reporting in the next 7 days.`;
    $('c-setups').textContent = decl.length; $('c-watch').textContent = upcoming().length; $('c-sectors').textContent = (d.sectors || []).length;
  }

  function chips() {
    const box = $('chips');
    if (!['setups', 'watch'].includes(S.tab)) { box.innerHTML = ''; return; }
    const list = S.tab === 'setups' ? declared() : upcoming();
    const counts = {};
    list.forEach(it => { const k = P(it).bucket?.code || 'NONE'; counts[k] = (counts[k] || 0) + 1; });
    const labels = { ALL: 'All', CONFIRMATION: 'Confirmation', RE_PEAD: 'Re-PEAD', FRESH_PEAD: 'Fresh PEAD', NO_CONFIRMATION: 'Not confirmed', PENDING: 'Q2 pending',
      WATCH_CONFIRM: 'Q1 held', WATCH_REPEAD: 'Q1 faded', WATCH_FRESH: 'No Q1 setup', NONE: 'No Q1 data' };
    const keys = ['ALL', ...Object.keys(counts).sort((a, b) => counts[b] - counts[a])];
    if (!keys.includes(S.bucket)) S.bucket = 'ALL';
    box.innerHTML = keys.map(k => `<button type="button" class="chip" data-bucket="${k}" aria-pressed="${S.bucket === k}">${labels[k] || k}${k !== 'ALL' ? ` <span class="n">${counts[k]}</span>` : ''}</button>`).join('');
  }

  function render() {
    if (!S.data) return;
    header(); chips();
    $('controls').style.display = ['setups', 'watch', 'sectors'].includes(S.tab) ? '' : 'none';
    $('sizing').style.display = S.tab === 'setups' ? '' : 'none';
    $('liquid').parentElement.style.display = S.tab === 'sectors' ? 'none' : '';
    $('sort').style.display = S.tab === 'setups' ? '' : 'none';
    document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === S.tab)));
    const active = document.querySelector('.tab[aria-selected="true"]');
    if (active && active.scrollIntoView) active.scrollIntoView({ inline: 'center', block: 'nearest' });
    $('view').innerHTML = { setups: viewSetups, watch: viewWatch, sectors: viewSectors, score: viewScore, health: viewHealth }[S.tab]();
  }

  // ---------- events ----------
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => { S.tab = t.dataset.tab; S.bucket = 'ALL'; store.set('tab', S.tab); render(); }));
  $('q').addEventListener('input', e => { S.q = e.target.value.trim(); render(); });
  $('liquid').addEventListener('change', e => { S.liquid = e.target.checked; store.set('liquid', S.liquid); render(); });
  $('sort').addEventListener('change', e => { S.sort = e.target.value; store.set('sort', S.sort); render(); });
  $('chips').addEventListener('click', e => { const b = e.target.closest('[data-bucket]'); if (b) { S.bucket = b.dataset.bucket; render(); } });
  ['cap', 'risk'].forEach(id => $(id).addEventListener('change', e => { store.set(id, num(e.target.value)); render(); }));
  $('view').addEventListener('click', e => {
    if (e.target.closest('[data-act="show-illiquid"]')) { S.liquid = false; $('liquid').checked = false; render(); return; }
    const row = e.target.closest('.row'); if (row && !e.target.closest('a')) { const id = row.dataset.id; S.open.has(id) ? S.open.delete(id) : S.open.add(id); render(); }
  });
  $('view').addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('row')) { e.preventDefault(); e.target.click(); } });
  const applyTheme = t => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; $('theme').textContent = (t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark' ? 'Light mode' : 'Dark mode'; };
  $('theme').addEventListener('click', () => { const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); const next = cur === 'dark' ? 'light' : 'dark'; store.set('theme', next); applyTheme(next); });

  // ---------- boot ----------
  S.tab = store.get('tab', 'setups'); S.liquid = store.get('liquid', true); S.sort = store.get('sort', 'conviction');
  $('liquid').checked = S.liquid; $('sort').value = S.sort;
  $('cap').value = store.get('cap', 1000000); $('risk').value = store.get('risk', 1);
  applyTheme(store.get('theme', null));
  fetch('intelligence.json?t=' + Date.now(), { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(d => { S.data = d; render(); })
    .catch(err => { $('view').innerHTML = `<div class="empty">Could not load intelligence.json (${esc(err.message)}). The page shows data once the engine has published at least once.</div>`; });
})();
