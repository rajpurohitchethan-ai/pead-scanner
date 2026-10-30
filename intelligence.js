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
  // 2.9.4: a loss on either side has no ordinary % change; say what happened.
  const PAT_STATE = { TURNAROUND: ['Turnaround', 'up'], DETERIORATION: ['Profit → loss', 'down'],
    LOSS_NARROWING: ['Loss narrowed', 'up'], LOSS_WIDENING: ['Loss widened', 'down'], LOSS_FLAT: ['Loss, flat', ''] };
  const patCell = (v, status) => {
    const st = PAT_STATE[status];
    return st ? `<span class="big ${st[1]}">${st[0]}</span>` : `<span class="big ${cls(v)}">${pct(v)}</span>profit`;
  };
  const resultFact = (it, m) => {
    if (it.halfYearOnly && num(it.revenueYoY) === null && num(m.revenueQoQ) === null) {
      return `<div class="fact"><h3>Result</h3><p class="note">Half-year figures only: this filing has no quarter-only numbers, so there is nothing to compare quarter by quarter.</p></div>`;
    }
    const yoy = num(it.revenueYoY) !== null || num(it.patYoY) !== null || !!PAT_STATE[it.patYoYStatus];
    const qRev = num(m.revenueQoQ), qPat = num(m.patQoQ ?? it.patQoQ);
    const useQ = !yoy && (qRev !== null || qPat !== null || !!PAT_STATE[m.patQoQStatus]);
    const bps = num(m.changeBps);
    const mBasis = bps === null ? '' : (useQ || m.basis !== 'QoQ') ? '' : ' vs last qtr';
    const margin = bps !== null ? `, margin ${bps > 0 ? '+' : ''}${bps} bps${mBasis}` : '';
    if (useQ) {
      return `<div class="fact"><h3>Result vs last quarter</h3><p><span class="big ${cls(qRev)}">${pct(qRev)}</span>revenue</p>
          <p>${patCell(qPat, m.patQoQStatus)}${margin}</p><p class="note">Year-ago figures not in the filing yet</p></div>`;
    }
    return `<div class="fact"><h3>Result vs last year</h3><p><span class="big ${cls(it.revenueYoY)}">${pct(it.revenueYoY)}</span>revenue</p>
          <p>${patCell(it.patYoY, it.patYoYStatus)}${margin}</p></div>`;
  };
  // 2.9.5: TradingView's embeddable chart, loaded only when opened. NSE
  // symbols are blocked in embeds, so it shows the BSE listing (end of day).
  const tvChart = it => {
    const sym = P(it).links?.tvEmbed;
    return sym ? `<details class="tvd" data-tv="${esc(sym)}"><summary>Interactive chart <span class="nm">TradingView · BSE, end of day</span></summary><div class="tvbox"></div></details>` : '';
  };
  const loadTv = d => {
    const box = d.querySelector('.tvbox');
    if (!box || box.dataset.loaded) return;
    box.dataset.loaded = '1';
    const dark = (document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
    const c = document.createElement('div'); c.className = 'tradingview-widget-container'; c.style.height = '100%';
    const w = document.createElement('div'); w.className = 'tradingview-widget-container__widget'; w.style.height = 'calc(100% - 22px)';
    const cp = document.createElement('div'); cp.className = 'tradingview-widget-copyright';
    cp.innerHTML = '<a href="https://www.tradingview.com/" rel="noopener nofollow" target="_blank">Chart by TradingView</a>';
    const sc = document.createElement('script');
    sc.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js'; sc.async = true;
    sc.text = JSON.stringify({ symbol: d.dataset.tv, interval: 'D', timezone: 'Asia/Kolkata', theme: dark ? 'dark' : 'light',
      style: '1', locale: 'en', autosize: true, hide_side_toolbar: true, allow_symbol_change: false, save_image: false,
      calendar: false, support_host: 'https://www.tradingview.com' });
    c.append(w, cp, sc); box.appendChild(c);
  };
  document.addEventListener('toggle', e => { if (e.target.classList?.contains('tvd') && e.target.open) loadTv(e.target); }, true);
  const inr = (v, d = 2) => { const n = num(v); return n === null ? '—' : '₹' + n.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d }); };
  const cr = v => { const n = num(v); if (n === null) return '—'; return n >= 1000 ? `₹${Math.round(n).toLocaleString('en-IN')} Cr` : `₹${n.toFixed(n >= 10 ? 0 : 1)} Cr`; };
  const x = v => { const n = num(v); return n === null ? '—' : `${n.toFixed(2)}x`; };
  const dt = v => { if (!v) return '—'; const d = new Date(String(v).slice(0, 10) + 'T00:00:00'); return Number.isNaN(+d) ? '—' : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); };
  const daysTo = v => { if (!v) return null; const d = new Date(String(v).slice(0, 10) + 'T00:00:00'); const t = new Date(); t.setHours(0, 0, 0, 0); return Math.round((d - t) / 864e5); };

  // ---------- vocabulary ----------
  const SIGNAL = {
    ENTRY_EARLY: ['Early entry triggered', 'go'], ENTRY_PULLBACK: ['Pullback entry triggered', 'go'],
    ENTRY_BREAKOUT: ['Breakout entry triggered', 'go'], ENTRY_TRIGGERED: ['In trade: entry triggered', 'go'], NEAR_ENTRY: ['Near entry', 'go'],
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
    const h = [it.symbol, it.name, it.sector, it.industry, P(it).sectorKey, ...(P(it).themes || [])].join(' ').toLowerCase();
    return h.includes(S.q.toLowerCase());
  };

  // ---------- chart: price + 21 EMA + reaction marker + plan levels ----------
  function chart(it, h = 120) {
    const c = P(it).chart;
    const closes = (c?.close || []).map(num);
    if (closes.filter(v => v !== null).length < 5) return '<p class="meta">Price chart pending.</p>';
    const plan = P(it).plan || {};
    const vols = (c.volume || []).map(num);
    const VB = vols.filter(v => v).length >= 5 ? 22 : 0;     // 2.9.23: volume strip under the price
    const W = 340, H = h + VB, pad = 6;
    const ema = (c.ema21 || []).map(num);
    const levels = [num(plan.entry), num(plan.sl)].filter(v => v !== null);
    const all = closes.concat(ema).concat(levels).filter(v => v !== null);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (hi === lo) { hi += 1; lo -= 1; }
    const span = hi - lo; lo -= span * 0.06; hi += span * 0.06;
    const xs = i => pad + i * (W - 2 * pad) / Math.max(1, closes.length - 1);
    const ys = v => h - pad - (v - lo) / (hi - lo) * (h - 2 * pad);
    const path = arr => arr.map((v, i) => v === null ? null : `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).filter(Boolean).join(' ');
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(it.symbol)} price chart">`;
    for (let k = 1; k < 4; k++) svg += `<line x1="0" x2="${W}" y1="${(h * k / 4).toFixed(1)}" y2="${(h * k / 4).toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    if (VB) {
      const vmax = Math.max(...vols.filter(v => v)), bw = Math.max(1.5, (W - 2 * pad) / closes.length - 1);
      vols.forEach((v, i) => {
        if (!v) return;
        const prior = vols.slice(Math.max(0, i - 20), i).filter(x => x);
        const avg = prior.length >= 5 ? prior.reduce((a, b) => a + b, 0) / prior.length : null;
        const hot = avg && v >= 2 * avg;
        const vh = Math.max(1, v / vmax * (VB - 3));
        svg += `<rect x="${(xs(i) - bw / 2).toFixed(1)}" y="${(H - vh).toFixed(1)}" width="${bw.toFixed(1)}" height="${vh.toFixed(1)}" fill="${hot ? 'var(--act)' : 'var(--ink-3)'}" opacity="${hot ? '.85' : '.35'}"/>`;
      });
    }
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
    const legend = `<div class="legend"><span><i></i>Close</span><span><i class="lm"></i>21 EMA</span>${ri !== null ? '<span><i style="background:var(--watch)"></i>Result day</span>' : ''}${num(plan.entry) !== null ? '<span><i class="le"></i>Entry</span>' : ''}${num(plan.sl) !== null ? '<span><i class="ls"></i>Stop</span>' : ''}${VB ? '<span><i style="background:var(--act)"></i>Volume ≥2x avg</span>' : ''}</div>`;
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
    const capPct = num(store.get('pcap', 15)) ?? 15, budget = cap * capPct / 100, share = used / cap * 100;
    const over = used > budget;
    return `<p class="qty">${lead} risks ${inr(atRisk, 0)} and uses ${inr(used, 0)} (${share.toFixed(0)}% of capital).</p>
      <p class="qty ${over ? 'warn-t' : ''}">PEAD is the tactical part of the portfolio: keep all open PEAD trades within ${capPct}% of capital (${inr(budget, 0)}). ${over ? `This one trade alone is over that limit; take fewer shares (max ${Math.floor(budget / e).toLocaleString('en-IN')}).` : `This trade uses ${(used / budget * 100).toFixed(0)}% of that budget.`}</p>`;
  }

  // ---------- concall ----------
  const TD_ICON = { ok: '✓', warn: '!', bad: '✗', na: '–' };
  // 2.9.8: QoQ detects the change, YoY confirms it, YTD validates execution.
  function lensBlock(it) {
    const L = P(it).lens;
    if (!L) return '';
    const cell = v => `<td class="r n ${cls(v)}">${pct(v)}</td>`;
    const st = L.profitStatus ? (PAT_STATE[L.profitStatus] || [])[0] : null;
    const prow = st ? `<tr><th>Profit</th>${cell(L.profit.qoq)}<td class="r" colspan="2">${esc(st)}</td></tr>`
      : `<tr><th>Profit</th>${cell(L.profit.qoq)}${cell(L.profit.yoy)}${cell(L.profit.ytd)}</tr>`;
    return `<div class="lens"><table><thead><tr><th></th><th class="r" title="vs last quarter: is it improving now?">QoQ</th><th class="r" title="vs same quarter last year: is it real growth?">YoY</th><th class="r" title="April to date vs last year: is execution consistent?">YTD</th></tr></thead>
      <tbody><tr><th>Revenue</th>${cell(L.revenue.qoq)}${cell(L.revenue.yoy)}${cell(L.revenue.ytd)}</tr>${prow}</tbody></table>
      ${(L.notes || []).length ? `<ul class="lens-notes">${L.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}</div>`;
  }
  const checklistChips = cl => `<div class="td-row">${cl.checks.map(c => `<span class="tdc ${c.status}" title="${esc(c.note)}"><i aria-hidden="true">${TD_ICON[c.status] || '–'}</i>${esc(c.label)}</span>`).join('')}</div>
      <ul class="td-notes">${cl.checks.map(c => `<li><b>${esc(c.label)}:</b> ${esc(c.note)}</li>`).join('')}</ul>`;
  // 2.9.20: is the sector losing favour? (after @SureshKBN)
  const FAV = { IN_FAVOUR: ['In favour', 'go'], NEUTRAL: ['Neutral', ''], OUT_OF_FAVOUR: ['Out of favour', 'watch'],
    DETERIORATING: ['Deteriorating', 'bad'], NOT_ENOUGH: ['Not enough data', 'pending'] };
  const favTag = f => f ? `<span class="tag ${(FAV[f.status] || ['', ''])[1]}" title="${esc(f.note || '')}">${esc((FAV[f.status] || [f.status])[0])}</span>` : '—';
  function favourBlock(f, title = 'Sector favour') {
    if (!f || !(f.signals || []).length) return '';
    return `<div class="td"><div class="td-h"><b>${esc(title)}</b> ${favTag(f)} <span class="meta">${esc(f.note)}</span></div>
      ${checklistChips({ checks: f.signals })}<p class="meta"><b>Fundamentals:</b> ${esc(f.fundamentalsNote || '—')} Out of favour is not the same as fundamentals deteriorating.</p></div>`;
  }
  function sectorFavourLine(it) {
    const f = P(it).favour;
    if (!f || f.status === 'NOT_ENOUGH') return '';
    const sec = (S.data.sectors || []).find(r => r.sector === f.sector);
    return favourBlock(sec?.favour, `Sector favour: ${f.sector}`).replace('<ul class="td-notes">', '<details class="td-more"><summary>What each signal found</summary><ul class="td-notes">').replace(/<\/ul><p class="meta">/, '</ul></details><p class="meta">');
  }
  // 2.9.23: one folded row for the sector-level blocks (they repeat across every
  // stock of a sector); the company's own execution line stays visible.
  function sectorCompact(it) {
    const name = P(it).sectorKey, sec = (S.data.sectors || []).find(r => r.sector === name);
    const cl = sec?.checklist, fav = P(it).favour, ex = P(it).execution, rs = P(it).rsSector;
    if (!cl && !fav && !rs) return '';
    const EX = { AHEAD: ['Ahead of sector', 'go'], BEHIND: ['Behind sector', 'bad'], MIXED: ['In line / mixed', 'watch'] };
    const e = ex?.label ? EX[ex.label] : null;
    const pills = [cl && cl.measured ? `<span class="tag">Checklist ${cl.score}/${cl.measured}</span>` : '',
      fav && fav.status !== 'NOT_ENOUGH' ? favTag(fav) : '', e ? `<span class="tag ${e[1]}">${e[0]}</span>` : ''].filter(Boolean).join(' ');
    return `${rsLine(it)}<details class="td sect-c"><summary><b>Sector</b> <span class="meta">${esc(name || '—')}</span> ${pills}</summary>
      ${sectorFavourLine(it)}${sectorChecklist(it)}</details>`;
  }
  // 2.9.23: relative strength against the stock's own sector index (1 week / 1 month).
  function rsLine(it, r = P(it).rsSector) {
    if (!r) return '';
    const cell = (st, ix, rs) => `<b class="${cls(rs)}">${rs === null || rs === undefined ? '—' : (num(rs) > 0 ? '+' : '') + num(rs).toFixed(1) + ' pp'}</b> <span class="meta">(stock ${pct(st)}, index ${pct(ix)})</span>`;
    return `<p class="rsline"><b>Strength vs ${esc(r.index)}</b><br>1 week ${cell(r.stock1w, r.index1w, r.rs1w)} · 1 month ${cell(r.stock1m, r.index1m, r.rs1m)}</p>`;
  }
  function sectorChecklist(it) {
    const sec = (S.data.sectors || []).find(r => r.sector === P(it).sectorKey), cl = sec?.checklist, ex = P(it).execution;
    if (!cl) return '';
    const EX = { AHEAD: ['Ahead of sector', 'go'], BEHIND: ['Behind sector', 'bad'], MIXED: ['In line / mixed', 'watch'] };
    const e = ex?.label ? EX[ex.label] : null;
    return `<div class="td"><div class="td-h"><b>Sector checklist</b> <span class="meta">${esc(P(it).sectorKey)}: ${cl.score} of ${cl.measured} measured pass (5 questions; ${cl.reported} liquid ${cl.reported === 1 ? 'company has' : 'companies have'} reported)</span></div>
      ${checklistChips(cl).replace('<ul class="td-notes">', '<details class="td-more"><summary>What each question found</summary><ul class="td-notes">').replace(/<\/ul>$/, '</ul></details>')}
      ${ex ? `<p class="note"><b>Company execution:</b> ${e ? `<span class="tag ${e[1]}">${e[0]}</span> ` : ''}${esc(ex.note)} A strong sector does not make every stock a winner.</p>` : ''}</div>`;
  }
  function topDownStrip(it) {
    const td = P(it).topDown;
    if (!td || !(td.checks || []).length) return '';
    return `<div class="td" aria-label="Top-down check: ${td.passed} of ${td.checks.length} pass">
      <div class="td-h"><b>Top-down check</b> <span class="meta">${td.passed} of ${td.checks.length} pass</span></div>
      <div class="td-row">${td.checks.map(c => `<span class="tdc ${c.status}" title="${esc(c.note)}"><i aria-hidden="true">${TD_ICON[c.status] || '–'}</i>${esc(c.label)}</span>`).join('')}</div>
      <details class="td-more"><summary>What each check says</summary><ul class="td-notes">${td.checks.map(c => `<li><b>${esc(c.label)}:</b> ${esc(c.note)}</li>`).join('')}</ul></details></div>`;
  }
  function peersLine(it) {
    const pe = P(it).peers;
    if (!pe || !pe.count) return '';
    const role = { LEADER: ['Sector leader', 'warn'], LAGGARD: ['Sector laggard', 'good'], MIDDLE: ['Mid-pack', ''] }[pe.role];
    const rep = (pe.reported || []).map(r => `${esc(r.symbol)} ${pct(r.reaction)}`).join(', ');
    const lead = (pe.leaders || []).map(r => `${esc(r.symbol)} ${pct(r.ret63, 0)}`).join(', ');
    const pf = P(it).peerFundamentals || {};
    const ord = n => n + (['th', 'st', 'nd', 'rd'][(n % 100 > 10 && n % 100 < 14) ? 0 : Math.min(n % 10, 4) % 4] || 'th');
    const fx = (k, label, unit = '%') => { const b = pf[k]; if (!b) return ''; return `<span class="pf"><b>${label}</b> ${k.endsWith('YoY') ? pct(b.value) : num(b.value).toFixed(1) + unit} <span class="meta">(${ord(b.rank)} of ${b.of}, median ${k.endsWith('YoY') ? pct(b.median) : num(b.median).toFixed(1) + unit})</span></span>`; };
    const mnote = P(it).margins?.opmNote;   // 2.9.23: margin beyond 100% is not ranked
    const fund = [fx('revenueYoY', 'Revenue'), fx('patYoY', 'Profit'), pf.margin ? fx('margin', 'Margin') : (mnote ? `<span class="pf"><b>Margin</b> n/m <span class="meta">(${esc(mnote.replace(/^n\/m /, ''))})</span></span>` : ''), fx('roe', 'ROE')].filter(Boolean).join('');
    return `<div class="peers"><b>Peers · ${esc(pe.sector)}</b>${role ? ` <span class="tag ${role[1]}">${role[0]}</span>` : ''}
      <span class="meta">${pe.rank ? `#${pe.rank} of ${pe.count} on 3-month move (${pct(pe.ret63)}; sector median ${pct(pe.sectorMedian63)}).` : ''}
      ${lead ? ` Leaders: ${lead}.` : ''}${rep ? ` Already reported: ${rep}${pe.reportedCount > (pe.reported || []).length ? '…' : ''} (avg reaction ${pct(pe.reportedAvgReaction)}).` : ' No peer has reported yet.'}</span>${fund ? `<div class="pfs"><b class="meta">Against sector peers</b>${fund}</div>` : ''}</div>`;
  }
  function homework(it) {
    const l = P(it).links || {};
    return `<p class="homework"><b>Your homework</b> (the screener can't judge these): capex and rerating, next growth triggers, FY27/FY28 EPS, fair value.${l.screener ? ` <a href="${esc(l.screener)}" target="_blank" rel="noopener">Screener</a>` : ''}${P(it).concall?.transcriptUrl ? ` · <a href="${esc(P(it).concall.transcriptUrl)}" target="_blank" rel="noopener">Concall transcript</a>` : ''}</p>`;
  }

  // 2.9.19: one price vocabulary on every card. A live quote shows a LIVE pill
  // and the close next to it is the previous session's ("prev close"); with
  // no live quote the price is the last close (CLOSED).
  const dayPct = (a, b) => num(a) !== null && num(b) ? (num(a) / num(b) - 1) * 100 : null;
  function priceState(live, at) {
    const t = at ? new Date(at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : '';
    return live ? `<span class="tag good">LIVE${t ? ' ' + t : ''}</span>` : `<span class="tag">CLOSED</span> Last close`;
  }
  function liveLine(l, prevClose) {
    if (!l || num(l.price) === null) return '';
    const st = { ABOVE_TRIGGER: ['above entry', 'good'], NEAR_TRIGGER: ['near entry', 'warn'], BELOW_TRIGGER: ['below entry', ''] }[l.state];
    const d = dayPct(l.price, prevClose);
    return `<div class="meta">${priceState(true, l.at)} ${inr(l.price)}${d !== null ? ` · <span class="${cls(d)}">day ${pct(d)}</span>` : ''}${st ? ` · <span class="tag ${st[1]}">${pct(l.distancePct)} ${st[0]} ${inr(l.entry)}</span>` : ''}</div>`;
  }

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
    const GROWTH = { ACCELERATING: ['Growth accelerating', 'good'], DECELERATING: ['Growth slowing', 'warn'], STEADY: ['Growth steady', ''] };
    const g = p.growth && GROWTH[p.growth.label];
    if (g) tags.push(`<span class="tag ${g[1]}" title="YoY growth vs last quarter: revenue ${p.growth.revenueDeltaPp ?? '—'} pp, profit ${p.growth.profitDeltaPp ?? '—'} pp">${g[0]}</span>`);
    if (x1.hv_label) tags.push(`<span class="tag good" title="Highest volume on the result session">${esc(x1.hv_label)}</span>`);
    if (liq.pass === false) tags.push(`<span class="tag bad">${esc(liq.label)}</span>`);
    const m = p.margins || {}, v = p.valuation || {};
    const pc = it.priceContext || {};
    const facts = `
      <div class="facts">
        ${resultFact(it, m)}
        <div class="fact"><h3>Market reaction</h3><p><span class="big ${cls(pc.resultDayPct)}">${pct(pc.resultDayPct)}</span>${num(pc.relativeVolume) !== null ? `on ${x(pc.relativeVolume)} usual volume` : 'result session'}</p>
          ${num(x1.sessions_since_reaction) > 0 ? `<p><span class="big ${cls(x1.return_since_result_pct)}">${pct(x1.return_since_result_pct)}</span>since result, ${x1.sessions_since_reaction} session${x1.sessions_since_reaction > 1 ? 's' : ''}</p>` : ''}${num(p.relativeStrength?.relativePct) !== null ? `<p><span class="big ${cls(p.relativeStrength.relativePct)}">${pct(p.relativeStrength.relativePct)}</span>vs NIFTY 500 (index ${pct(p.relativeStrength.indexReturnPct)})</p>` : ''}</div>
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
      <div class="tags">${(P(it).themes || []).map(t => `<span class="tag theme">${esc(t)}</span>`).join('')}${tags.join('')}</div>
      <div class="meta">Result ${dt(it.resultDate)}${it.reactionSession ? `, reaction ${it.reactionWindowStart && it.reactionWindowStart !== it.reactionSession ? dt(it.reactionWindowStart) + '–' : ''}${dt(it.reactionSession)}` : ''}${it.reactionWindowStart && it.reactionWindowStart !== it.reactionSession ? ' (filed in market hours)' : ''}${num(pc.lastClose) !== null ? `, ${p.live && num(p.live.price) !== null ? 'prev close' : 'last close'} ${inr(pc.lastClose)}` : ''}</div>
      ${concallLine(p.concall)}
      ${liveLine(p.live, pc.lastClose)}
      ${chart(it)}
      ${tvChart(it)}
      ${facts}
      ${lensBlock(it)}
      ${topDownStrip(it)}
      ${sectorCompact(it)}
      ${peersLine(it)}
      <div class="plan"><div class="plan-top"><span class="plan-sig ${sTone}">${sLabel}${plan.stage === 'STARTER' && num(plan.entry) !== null ? ' <span class="tag warn">Starter, 1/3 size</span>' : plan.stage === 'FULL' && num(plan.entry) !== null ? ' <span class="tag go">Full size</span>' : ''}</span>${num(plan.rNow) !== null ? `<span class="n">${num(plan.rNow).toFixed(1)}R</span>` : ''}</div>
        <p class="plan-why">${esc(plan.why || 'Plan not computed yet.')}</p>${levels}</div>
      ${(reasons.length || risks.length) ? `<details class="why"><summary>Why it scores ${num(it.convictionScore) ?? '—'}</summary><div class="why-cols">
        <div class="pos"><h4>Supporting</h4><ul>${reasons.map(r => `<li>${esc(r)}</li>`).join('') || '<li>Nothing verified yet.</li>'}</ul></div>
        <div class="neg"><h4>Risks and gaps</h4><ul>${risks.map(r => `<li>${esc(r)}</li>`).join('') || '<li>None flagged.</li>'}</ul></div></div></details>` : ''}
      ${homework(it)}
      <div class="links">${linkHtml}</div>
    </article>`;
  }

  // ---------- views ----------
  const SORT_KEYS = {
    conviction: it => num(it.convictionScore),
    date: it => { const d = daysTo(it.resultDate); return d === null ? null : -d; },
    reaction: it => num(it.priceContext?.resultDayPct),
    rvol: it => num(it.priceContext?.relativeVolume),
    since: it => num(px(it).return_since_result_pct),
    revyoy: it => num(it.revenueYoY),
    patyoy: it => num(it.patYoY),
    price: it => num(it.priceContext?.lastClose),
    completeness: it => num(it.dataCompletenessPct),
    runup: it => num(it.priceContext?.pre20dPct),
    distance: it => { const d = num(P(it).plan?.distancePct); return d === null ? null : -Math.abs(d); },
    volume: it => { const r = { HVE: 3, HVY: 2, HVQ: 1 }[px(it).hv_label] || 0; return r * 1000 + (num(it.priceContext?.relativeVolume) ?? 0); },
    rs: it => num(P(it).relativeStrength?.relativePct),
    growth: it => { const g = { ACCELERATING: 2, STEADY: 1, DECELERATING: 0 }[P(it).growth?.label]; return g === undefined ? null : g * 1000 + (num(P(it).growth?.revenueDeltaPp) ?? 0); },
    topdown: it => { const t = P(it).topDown; return t ? t.passed * 10 + t.known : null; },
  };
  function sorted(list) {
    const key = SORT_KEYS[S.sort] || SORT_KEYS.conviction, dir = S.dir === 'asc' ? 1 : -1;
    // Missing values always sort last, whichever direction.
    return list.slice().sort((a, b) => {
      const ka = key(a), kb = key(b);
      if (ka === null && kb === null) return 0;
      if (ka === null) return 1;
      if (kb === null) return -1;
      return (ka - kb) * dir;
    });
  }

  function emptyHidden(total, shown, what) {
    const hidden = total - shown;
    if (hidden > 0 && S.liquid) return `<div class="empty">${hidden} ${what} hidden because they trade under ₹${S.data.thresholds?.liquidityTurnoverCr ?? 1} Cr a day or below ₹${S.data.thresholds?.liquidityMinPrice ?? 20}.<br><button type="button" data-act="show-illiquid">Show them</button></div>`;
    return `<div class="empty">No ${what} match this view yet.</div>`;
  }

  // 2.9.9: Radar = reaction done, setup still alive, screener watching for an entry.
  const RADAR = { ENTRY_EARLY: 0, ENTRY_PULLBACK: 0, ENTRY_BREAKOUT: 0, ENTRY_TRIGGERED: 1, NEAR_ENTRY: 1, PULLBACK_ZONE: 2, WATCH: 3,
    RISK_TOO_WIDE: 4, EXTENDED: 5, WAIT_CONCALL: 6 };
  // 2.9.14: setups below the ₹1,000 Cr minimum (or with unknown market cap) are
  // listed separately, labelled, so a live setup is never silently hidden; they
  // still do not qualify as trade candidates.
  const MIN_MCAP = () => num(S.data.thresholds?.minMarketCapCr) ?? 1000;
  const radarAll = () => declared().filter(it => {
    const s = P(it).plan?.signal;
    return s in RADAR && liquidOk(it) && !px(it).base_broken;
  }).sort((a, b) => RADAR[P(a).plan.signal] - RADAR[P(b).plan.signal] || (num(b.convictionScore) ?? 0) - (num(a.convictionScore) ?? 0));
  // 2.9.23: a stock you hold is never moved to the "not trade candidates" table.
  const heldPos = it => (S.data.positions || []).find(p => String(p.symbol).toUpperCase() === String(it.symbol).toUpperCase());
  const bigEnough = it => !!heldPos(it) || (num(it.marketCapCr) !== null && num(it.marketCapCr) >= MIN_MCAP());
  const radar = () => radarAll().filter(bigEnough);
  const radarSmall = () => radarAll().filter(it => !bigEnough(it));
  // 2.9.25: best to worst. Grade from the engine (pead_plus.radar_grade); stocks you
  // hold sit in their own section at the end.
  const TIER = { A: ['Best: take on the trigger', 'go'], B: ['Good: wait for the trigger', 'act'],
    C: ['Watch only', 'watch'], D: ['Weak: skip for now', 'pending'] };
  const grade = it => P(it).grade || { score: 0, tier: 'D', pros: [], cons: [] };
  function viewRadar() {
    const all = radarAll().filter(matches);
    const held = all.filter(it => heldPos(it)), rest = all.filter(it => !heldPos(it));
    if (!all.length) return `<div class="empty">Nothing on the radar yet. A stock lands here once its reaction session has traded and the setup is still alive (base intact, earnings good enough, liquid).</div>`;
    const byTier = t => rest.filter(it => grade(it).tier === t).sort((x, y) => grade(y).score - grade(x).score);
    const sect = t => { const l = byTier(t); return l.length ? `<h3 class="radar-sub"><span class="tag ${TIER[t][1]}">${t}</span> ${esc(TIER[t][0])} <span class="meta">(${l.length})</span></h3><div class="ledger">${l.map(radarRow).join('')}</div>` : ''; };
    const nA = byTier('A').length;
    return copyBar(rest, 'radar') + `<p class="note">Ranked best to worst for taking only a few trades: how ready the setup is, earnings quality, risk from today's price to the stop, the reaction, volume since, strength against its sector index, sector favour and red flags. ${nA ? `<b>${nA} in the top group.</b>` : '<b>Nothing in the top group right now: waiting is fine.</b>'} Tap a row for the full card. A ranking to help you choose, not a rule.</p>
      ${['A', 'B', 'C', 'D'].map(sect).join('')}
      ${held.length ? `<h3 class="radar-sub">Already in your trades <span class="meta">(${held.length})</span></h3><div class="ledger">${held.map(radarRow).join('')}</div>` : ''}`;
  }
  function radarRow(it) {
        const pl = P(it).plan || {}, s = sig(it), pc = it.priceContext || {}, live = P(it).live;
        const last = num(live?.price) ?? num(pc.lastClose), low = num(it.resultDayLow);
        const toBase = last !== null && low ? (last / low - 1) * 100 : null;
        const hp = heldPos(it);
        // 2.9.23: in a stock you hold, the levels are your fill's, not the theoretical trigger's.
        const trig = hp ? num(hp.oneR) : num(pl.entry), toTrig = last !== null && trig ? (trig / last - 1) * 100 : null;
        const open = S.open.has(it.eventId);
        return `<div class="row radar-row" role="button" tabindex="0" aria-expanded="${open}" data-id="${esc(it.eventId)}">
          <div><div class="s">${esc(it.symbol)} <span class="gpill g${grade(it).tier}" title="Radar grade">${grade(it).tier} ${grade(it).score}</span></div><div class="nm"><span class="tag ${s[1]}">${esc(s[0])}</span>${(P(it).themes || []).map(t => ` <span class="tag theme">${esc(t)}</span>`).join('')}${hp ? ' <span class="tag go">You hold</span>' : ''}${pl.stage === 'STARTER' ? ' <span class="tag watch">Starter 1/3</span>' : ''}${bigEnough(it) ? '' : ` <span class="tag pending">${num(it.marketCapCr) !== null ? `₹${Math.round(num(it.marketCapCr)).toLocaleString('en-IN')} Cr` : 'Mcap unknown'}</span>`}</div></div>
          <div><div class="k">Reaction</div><div class="v ${cls(pc.resultDayPct)}">${pct(pc.resultDayPct)}${num(pc.relativeVolume) !== null ? ` <span class="meta">${num(pc.relativeVolume).toFixed(1)}x vol</span>` : ''}</div></div>
          <div><div class="k">${live?.price ? 'Live' : 'Last close'}</div><div class="v">${inr(last)}</div></div>
          <div class="opt"><div class="k">Base (result-day low)</div><div class="v">${inr(low)}${toBase !== null ? ` <span class="meta">${pct(toBase)}</span>` : ''}</div></div>
          <div><div class="k">${hp ? 'Your +1R' : trig ? 'Trigger' : 'Risk'}</div><div class="v">${trig ? `${inr(trig)}${toTrig !== null ? ` <span class="meta">${toTrig > 0 ? pct(toTrig) + ' away' : 'above'}</span>` : ''}` : (num(pl.riskPct) !== null ? `${num(pl.riskPct).toFixed(1)}%` : '—')}</div></div>
          <div class="chev" aria-hidden="true">›</div></div>
          ${(grade(it).pros.length || grade(it).cons.length) && !hp ? `<div class="radar-why grade-why">${grade(it).pros.length ? `<span class="up">+ ${esc(grade(it).pros.join(', '))}</span>` : ''}${grade(it).cons.length ? `${grade(it).pros.length ? '<br>' : ''}<span class="down">− ${esc(grade(it).cons.join(', '))}</span>` : ''}</div>` : ''}
          <div class="radar-why">${P(it).rsSector && num(P(it).rsSector.rs1m) !== null ? `<span class="${cls(P(it).rsSector.rs1m)}">RS 1M ${num(P(it).rsSector.rs1m) > 0 ? '+' : ''}${num(P(it).rsSector.rs1m).toFixed(1)} pp vs ${esc(P(it).rsSector.index)}</span>. ` : ''}${hp ? `<b>You hold</b> since ${dt(hp.entryDate)} at ${inr(hp.entryPrice)}: stop ${inr(num(hp.swingStop) ?? num(hp.initialStop))}, +1R ${inr(hp.oneR)}${num(hp.rNow) !== null ? ` (now ${num(hp.rNow).toFixed(2)}R)` : ''}.` : ''}${hp && pl.signal === 'ENTRY_TRIGGERED' ? '' : (hp ? ' Screener: ' : '') + esc(pl.why || '')}</div>${open ? `<div class="row-detail">${card(it)}</div>` : ''}`;
  }
  // 2.9.12: My trades. Entry/stop come from master/positions.json (public repo:
  // symbol, date, price only); quantity is kept in this browser only.
  const POS = { HOLD: ['Hold', 'go'], TRAILING: ['Trailing (past +1R)', 'go'], BELOW_ENTRY: ['Below entry', 'watch'],
    EXIT_SWING: ['Exit swing part', 'bad'], EXIT: ['Exit: closed below stop', 'bad'], NO_DATA: ['Waiting for data', 'pending'] };
  function viewTrades() {
    const list = S.data.positions || [];
    if (!list.length) return closedBlock() + `<div class="empty">No open trades. Tell Claude when you buy, and the trade is tracked here against the screener's plan.</div>`;
    return `<p class="note">Your open trades, checked every hour against the screener's plan: stop 1% under the result-day low until +1R, then stop to cost and trail the 21 EMA (swing) or 63 EMA (position). Stops are judged on closing prices and never move down. Quantity stays on this device only.</p>
      ${tradesRisk(list)}<div class="grid">${list.map(p => {
        const st = POS[p.status] || [p.status, ''];
        let qty = null; try { qty = num(localStorage.getItem('qty:' + p.symbol)); } catch (e) { /* storage blocked */ }
        const money = v => v === null ? '—' : `${v >= 0 ? '+' : '−'}₹${Math.abs(Math.round(v)).toLocaleString('en-IN')}`;
        const pnl = qty && num(p.last) !== null ? (p.last - p.entryPrice) * qty : null;
        const atRisk = qty && num(p.swingStop) !== null ? (p.swingStop - p.entryPrice) * qty : null;
        const lv = (l, v, k = '') => `<div class="lv ${k}"><span>${l}</span><b>${v}</b></div>`;
        return `<article class="card"><div class="head"><div><h2 class="sym">${esc(p.symbol)}</h2><div class="co">Bought ${dt(p.entryDate)} at ${inr(p.entryPrice)}</div></div>
          <div class="score"><div class="v ${cls(p.pnlPct)}">${pct(p.pnlPct)}</div><div class="l">${num(p.rNow) !== null ? num(p.rNow).toFixed(2) + 'R' : ''}</div></div></div>
          <div class="tags"><span class="tag ${st[1]}">${esc(st[0])}</span></div>
          <div class="meta">${priceState(!!p.live, p.at)} ${inr(p.last)}${p.live && num(p.lastClose) !== null ? ` · prev close ${inr(p.lastClose)} · <span class="${cls(dayPct(p.last, p.lastClose))}">day ${pct(dayPct(p.last, p.lastClose))}</span>` : ''}</div>
          <p class="plan-why">${esc(p.note || '')}</p>
          <div class="levels">${lv('Stop now (swing)', inr(p.swingStop), 's')}${lv('Position stop', inr(p.positionStop), 's')}
            ${lv('+1R (stop to cost)', inr(p.oneR), 'e')}${lv('+2R', inr(p.twoR))}</div>
          <p class="qty">Risk at entry ${num(p.riskPct) !== null ? num(p.riskPct).toFixed(1) + '%' : '—'} (${inr(p.riskPerShare)} a share). Result-day low ${inr(p.resultDayLow)}. 21 EMA ${inr(p.ema21)}, 63 EMA ${inr(p.ema63)}.</p>
          <div class="sizing"><label>Quantity <input type="number" min="0" step="1" inputmode="numeric" class="pos-qty" data-sym="${esc(p.symbol)}" value="${qty ?? ''}"></label>
            ${qty ? `<span class="meta">P&amp;L ${money(pnl)} · at the stop ${money(atRisk)}</span>` : '<span class="meta">enter quantity for rupee P&amp;L</span>'}</div>
          ${p.setup ? `<p class="note">${esc(p.setup)}</p>` : ''}${p.rsSector ? rsLine(null, p.rsSector) : ''}${p.sectorFavour && p.sectorFavour.status !== 'NOT_ENOUGH' ? `<p class="meta"><b>Sector:</b> ${esc(p.sectorFavour.sector)} ${favTag(p.sectorFavour)} ${esc(p.sectorFavour.note)}</p>` : ''}${rsiBlock(p.watch && p.watch.rsi)}${noteBlock(p.notes)}${watchBlock(p.watch)}</article>`;
      }).join('')}</div>
      <details class="td qtx"><summary>Move quantities to another device</summary>
        <p class="meta">Quantities stay in this browser only (the repo is public). Copy them here and paste them on your other phone or laptop.</p>
        <button type="button" data-act="qty-copy">Copy my quantities</button>
        <p><input id="qty-in" class="n" placeholder="HCLTECH=114;GROWW=750" autocomplete="off"> <button type="button" data-act="qty-apply">Apply</button></p>
        <p class="meta" id="qty-msg"></p></details>${closedBlock()}${globalBlock()}`;
  }
  // 2.9.19: total rupee risk to the current stops over the trades with a quantity entered.
  function tradesRisk(list) {
    let risk = 0, used = 0, n = 0;
    for (const p of list) {
      let q = null; try { q = num(localStorage.getItem('qty:' + p.symbol)); } catch (e) { /* storage blocked */ }
      const stop = num(p.swingStop) ?? num(p.initialStop);
      if (!q || stop === null || num(p.last) === null) continue;
      n += 1; used += q * num(p.last); risk += Math.max(0, num(p.last) - stop) * q;
    }
    if (!n) return '';
    const cap = num(store.get('cap', 1000000));
    const of = v => cap ? ` (${(v / cap * 100).toFixed(1)}% of ₹${Math.round(cap).toLocaleString('en-IN')} capital)` : '';
    const rOff = (S.data.regime || {}).label === 'RISK-OFF';
    const capPct = num(store.get('pcap', 15)) ?? 15, budget = cap * capPct / 100, usedPct = budget ? used / budget * 100 : 0;
    const tone = usedPct > 100 ? 'bad' : usedPct > 80 ? 'watch' : 'go';
    const bar = budget ? `<div class="capbar" role="img" aria-label="PEAD cap used ${usedPct.toFixed(0)}%"><i class="${tone}" style="width:${Math.min(100, usedPct).toFixed(1)}%"></i></div>
      <p class="note ${usedPct > 100 ? 'warn-t' : ''}"><b>PEAD cap</b> ${usedPct.toFixed(0)}% used: ₹${Math.round(used).toLocaleString('en-IN')} deployed of ₹${Math.round(budget).toLocaleString('en-IN')} (${capPct}% of ₹${Math.round(cap).toLocaleString('en-IN')}).${usedPct > 100 ? ' Over the cap: no new PEAD trades until something exits, or raise the cap on purpose.' : ''} <span class="meta">Capital and cap are the Setups tab figures; change them there.</span></p>` : '';
    return bar + `<p class="note"><b>Open risk to stops</b> ₹${Math.round(risk).toLocaleString('en-IN')}${of(risk)} · <b>deployed</b> ₹${Math.round(used).toLocaleString('en-IN')}${of(used)} over ${n} trade${n > 1 ? 's' : ''} with a quantity entered.${rOff ? ' <span class="tag warn">Market risk-off</span>' : ''} <span class="meta">From the current price to each stop; capital is the Setups tab figure.</span></p>`;
  }
  // 2.9.15: filings, deals, selling pressure, global peers and headlines for
  // open trades. Display only; the exit rules above do not read any of it.
  const href = u => /^https?:\/\//i.test(String(u || '')) ? esc(u) : null;
  const ext = (u, label) => href(u) ? `<a href="${href(u)}" target="_blank" rel="noopener">${label}</a>` : label;
  const KIND = { ORDER: 'Order', DEAL: 'Deal', INSIDER: 'Insider / SAST', PLEDGE: 'Pledge', RATING: 'Rating', LEGAL: 'Legal / tax',
    MGMT: 'Management', CORP: 'Corporate', RESULT: 'Results / call', OTHER: 'Filing',
    CORRECTION: 'Correction', QUERY: 'Exchange query' };   // 2.9.19
  const PRESS = { HEAVY_SELLING: ['Heavy selling', 'bad'], SELLING: ['Selling pressure', 'watch'], NEUTRAL: ['No unusual pressure', ''],
    ACCUMULATION: ['Accumulation', 'go'] };
  const when = v => { const d = new Date(v); return Number.isNaN(+d) ? '' : d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }); };
  const moveCell = r => `<span class="${cls(r.chg1dPct)}">${pct(r.chg1dPct)}</span> <span class="meta">5D ${pct(r.chg5dPct)}</span>`;
  const headlines = (list, empty) => list && list.length ? `<ul class="wl">${list.map(n => `<li>${ext(n.url, esc(n.title))} <span class="meta">${esc(n.publisher || '')}${n.at ? ' · ' + when(n.at) : ''}</span></li>`).join('')}</ul>` : `<p class="meta">${empty}</p>`;
  function watchBlock(w) {
    if (!w) return '';
    const pr = w.pressure, pl = pr ? (PRESS[pr.label] || [pr.label, '']) : null;
    const fl = (w.flags || []).length;
    const filings = (w.filings || []).length ? `<ul class="wl">${w.filings.map(f => `<li><span class="tag ${f.tone === 'neg' ? 'bad' : f.tone === 'pos' ? 'go' : f.tone === 'warn' ? 'warn' : ''}">${esc(KIND[f.kind] || f.kind)}</span> ${ext(f.url, esc(f.text))} <span class="meta">${when(f.at)}</span></li>`).join('')}</ul>`
      : `<p class="meta">${w.filingsAt ? 'No filings in the last 30 days besides routine ones.' : 'Not checked yet.'}</p>`;
    const deals = (w.deals || []).length ? `<ul class="wl">${w.deals.map(d => `<li><span class="tag ${d.side === 'SELL' || d.kind === 'SHORT' ? 'bad' : 'go'}">${d.kind === 'SHORT' ? 'Short sale' : esc(d.kind.charAt(0) + d.kind.slice(1).toLowerCase()) + ' ' + esc((d.side || '').toLowerCase())}</span> ${dt(d.date)} · ${num(d.qty) !== null ? num(d.qty).toLocaleString('en-IN') + ' shares' : ''}${num(d.price) !== null ? ' @ ' + inr(d.price) : ''}${d.client ? ' · ' + esc(d.client) : ''}</li>`).join('')}</ul>`
      : '<p class="meta">No bulk, block or short-selling reports in the last 10 days.</p>';
    const peers = (w.peers || []).length ? `<div class="wpeers">${w.peers.map(r => `<div><span>${esc(r.name)}</span><b>${moveCell(r)}</b></div>`).join('')}</div>` : `<p class="meta">${w.peerGroup ? 'Peer prices load with the next global-markets refresh (about every 2 hours).' : 'No global peer list for this sector.'}</p>`;
    return `<details class="tvd watch"${fl ? ' open' : ''}><summary>News &amp; pressure${fl ? ` <span class="tag watch">${fl} to note</span>` : ''}</summary>
      ${fl ? `<p class="wflags">${w.flags.map(esc).join(' · ')}</p>` : ''}
      <h4>Selling pressure</h4>${pr ? `<p><span class="tag ${pl[1]}">${esc(pl[0])}</span> <span class="meta">${pr.distributionDays} distribution / ${pr.accumulationDays} accumulation days in ${pr.sessions} sessions · up/down volume ${num(pr.upDownVolume) !== null ? num(pr.upDownVolume).toFixed(2) : '—'}${num(pr.lastCloseInRangePct) !== null ? ` · last close at ${Math.round(pr.lastCloseInRangePct)}% of the day's range` : ''}${num(w.deliveryPct) !== null ? ` · delivery ${num(w.deliveryPct).toFixed(1)}%` : ''}</span></p>` : '<p class="meta">—</p>'}
      ${deals}
      <h4>Exchange filings</h4>${filings}
      <h4>Global peers (last session)${w.peerGroup ? ` <span class="meta">${esc(w.peerGroup)}</span>` : ''}</h4>${peers}
      <h4>Headlines</h4>${headlines(w.news, w.newsAt ? 'No recent headlines found.' : 'Not checked yet.')}
      ${(w.sectorNews || []).length ? `<h4>Sector &amp; policy</h4>${headlines(w.sectorNews, '')}` : ''}
      <p class="meta">Filings and deals: NSE/BSE. Prices and headlines: Yahoo Finance (headlines are unverified; open the source). Filing labels are keyword-based. Not used by the entry or exit rules.</p></details>`;
  }
  // 2.9.18: Claude's evening read: what the news says and what it means for the stock.
  const IMPACT = { POSITIVE: ['Positive for the stock', 'go'], NEGATIVE: ['Negative for the stock', 'bad'],
    MIXED: ['Mixed', 'watch'], NEUTRAL: ['No real impact', ''] };
  function noteBlock(n, title = "News read") {
    if (!n) return '';
    const im = IMPACT[n.impact] || IMPACT.NEUTRAL;
    const todayIst = new Date(Date.now() + 5.5 * 36e5).toISOString().slice(0, 10);
    const stale = n.date && n.date < todayIst;
    return `<div class="tnote"><div class="tnote-h"><b>${esc(title)}</b> <span class="tag ${im[1]}">${esc(im[0])}</span> <span class="meta">${dt(n.date)}${stale ? ' · from an earlier day; refreshed each trading morning and evening' : ''}</span></div>
      <p>${esc(n.summary)}</p>
      ${(n.points || []).length ? `<ul class="wl">${n.points.map(x => `<li>${esc(x.text)}${x.source || x.url ? ` <span class="meta">${ext(x.url, esc(x.source || 'source'))}</span>` : ''}</li>`).join('')}</ul>` : ''}
      ${n.watch ? `<p class="meta"><b>Watch:</b> ${esc(n.watch)}</p>` : ''}
      <p class="meta">Written by Claude from exchange filings and news; check the sources before acting.</p></div>`;
  }
  // 2.9.28: RSI(14) on the weekly and monthly charts; bearish divergence = price
  // higher high with RSI lower high (momentum fading).
  function rsiBlock(r) {
    if (!r || (!r.weekly && !r.monthly)) return '';
    const DIV = { BEARISH: ['Bearish divergence', 'bad'], BULLISH: ['Bullish divergence', 'go'] };
    const one = (label, x) => {
      if (!x) return `<li><b>${label}:</b> <span class="meta">not available</span></li>`;
      if (x.rsi === null || x.rsi === undefined) return `<li><b>${label}:</b> <span class="meta">${esc(x.note || 'not enough history')}</span></li>`;
      const d = DIV[x.divergence];
      return `<li><b>${label} RSI ${num(x.rsi).toFixed(0)}</b> ${d ? `<span class="tag ${d[1]}">${d[0]}</span> ` : ''}<span class="meta">${esc(x.note || '')}</span></li>`;
    };
    const warn = [r.weekly, r.monthly].some(x => x && x.divergence === 'BEARISH');
    return `<div class="td rsib"><div class="td-h"><b>Momentum check</b> <span class="meta">RSI 14${r.source ? ' · ' + esc(r.source) : ''}</span></div>
      <ul class="wl">${one('Weekly', r.weekly)}${one('Monthly', r.monthly)}</ul>
      ${warn ? '<p class="meta"><b>What it means:</b> the trend is still up but each push is weaker. It is a warning, not an exit signal: keep the stop, do not add, and take it seriously if the price also loses the 21 EMA.</p>' : ''}</div>`;
  }
  // 2.9.28: closed trades (R and % from positions.json; rupees only from quantities on this device).
  function closedBlock() {
    const rows = S.data.closedPositions || [], sm = S.data.closedSummary;
    if (!rows.length) return '';
    let rupees = 0, haveQ = false;
    const body = rows.map(c => {
      let q = null; try { q = num(localStorage.getItem('qty:' + c.symbol)); } catch (e) { /* blocked */ }
      const rs = q ? (num(c.exitPrice) - num(c.entryPrice)) * q : null;
      if (rs !== null) { rupees += rs; haveQ = true; }
      return `<tr><td><b>${esc(c.symbol)}</b><div class="meta">${dt(c.entryDate)} → ${dt(c.exitDate)}${c.days !== null ? ` · ${c.days}d` : ''}</div></td>
        <td class="r n">${inr(c.entryPrice)}<div class="meta">${inr(c.exitPrice)}</div></td>
        <td class="r n ${cls(c.r)}">${num(c.r) !== null ? (num(c.r) > 0 ? '+' : '') + num(c.r).toFixed(2) + 'R' : '—'}<div class="meta ${cls(c.pnlPct)}">${pct(c.pnlPct)}</div></td>
        <td class="r n ${cls(rs)}">${rs !== null ? `${rs >= 0 ? '+' : '−'}₹${Math.abs(Math.round(rs)).toLocaleString('en-IN')}` : '—'}</td></tr>`;
    }).join('');
    return `<section class="card gm"><h3>Closed trades ${sm ? `<span class="meta">${sm.trades} trade${sm.trades > 1 ? 's' : ''} · ${sm.wins} win${sm.wins === 1 ? '' : 's'} · total ${num(sm.totalR) !== null ? (num(sm.totalR) > 0 ? '+' : '') + num(sm.totalR).toFixed(2) + 'R' : '—'}</span>` : ''}</h3>
      <div class="tscroll"><table class="sectbl"><thead><tr><th>Trade</th><th class="r">Entry / exit</th><th class="r">Result</th><th class="r">₹ (this device)</th></tr></thead><tbody>${body}</tbody></table></div>
      ${haveQ ? `<p class="meta">Rupee total on this device: <b class="${cls(rupees)}">${rupees >= 0 ? '+' : '−'}₹${Math.abs(Math.round(rupees)).toLocaleString('en-IN')}</b>. R = result divided by the planned risk (entry to stop).</p>` : '<p class="meta">R = result divided by the planned risk (entry to stop). Rupees appear when the quantity is entered on this device.</p>'}</section>`;
  }
  function globalBlock() {
    const g = S.data.globalMarkets;
    if (!g || !(g.markets || []).length) return '';
    return `<section class="card gm"><h3>Global markets <span class="meta">last session · updated ${when(g.at)}</span></h3>
      <div class="wpeers">${g.markets.map(r => `<div><span>${esc(r.name)}</span><b>${r.group === 'rates' || r.group === 'risk' ? `${num(r.last).toFixed(2)} ` : ''}${moveCell(r)}</b></div>`).join('')}</div>
      ${noteBlock(S.data.marketNote, 'Market tonight')}<h4>Market &amp; policy headlines</h4>${headlines(g.news, 'No headlines found.')}</section>`;
  }
  document.addEventListener('change', e => {
    const q = e.target.closest?.('.pos-qty');
    if (!q) return;
    try { localStorage.setItem('qty:' + q.dataset.sym, q.value || ''); } catch (err) { /* storage blocked */ }
    render();
  });
  // 2.9.23: copy the visible list to TradingView (watchlist import) or as a CSV of levels.
  const tvSym = it => { const m = String(P(it).links?.tradingview || '').match(/symbol=([A-Z]+)%3A([^&]+)/); return m ? `${m[1]}:${decodeURIComponent(m[2])}` : `NSE:${String(it.symbol || '').toUpperCase()}`; };
  const S_COPY = {};
  function copyBar(list, key) {
    S_COPY[key] = list;
    return list.length ? `<div class="copybar"><button type="button" data-copy="tv" data-key="${key}">Copy TradingView watchlist (${list.length})</button><button type="button" data-copy="csv" data-key="${key}">Copy levels (CSV)</button><span class="meta" data-copy-msg="${key}"></span></div>` : '';
  }
  function copyText(kind, list) {
    if (kind === 'tv') return list.map(tvSym).join(',');
    const head = 'symbol,signal,last,entry,stop,risk_pct,stage,result_day_low';
    return [head].concat(list.map(it => { const pl = P(it).plan || {}, pc = it.priceContext || {};
      return [it.symbol, pl.signal || '', pc.lastClose ?? '', pl.entry ?? '', pl.sl ?? '', pl.riskPct ?? '', pl.stage || '', it.resultDayLow ?? ''].join(','); })).join('\n');
  }
  function viewSetups() {
    let list = declared().filter(matches);
    if (S.bucket !== 'ALL') list = list.filter(it => (P(it).bucket?.code || 'NONE') === S.bucket);
    const all = list.length;
    if (S.liquid) list = list.filter(liquidOk);
    if (!list.length) return emptyHidden(all, 0, 'declared results');
    return copyBar(sorted(list), 'setups') + `<div class="grid">${sorted(list).map(card).join('')}</div>${all > list.length ? `<p class="foot">${all - list.length} illiquid results hidden.</p>` : ''}`;
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
    return row + `<div class="row-detail">${chart(it, 100)}${tvChart(it)}${topDownStrip(it)}${peersLine(it)}
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

  function sectorPeers(sector) {
    const list = (S.data.items || []).filter(it => P(it).sectorKey === sector && num(px(it).ret_63d_pct) !== null)
      .sort((a, b) => num(px(b).ret_63d_pct) - num(px(a).ret_63d_pct));
    if (!list.length) return '<p class="meta">No priced peers yet.</p>';
    const role = { LEADER: ['leader', 'warn'], LAGGARD: ['laggard', 'good'], MIDDLE: ['mid', ''] };
    return `<ol class="plist">${list.map(it => { const r = role[P(it).peers?.role]; const d = daysTo(it.resultDate);
      return `<li><b>${esc(it.symbol)}</b> <span class="n ${cls(px(it).ret_63d_pct)}">${pct(px(it).ret_63d_pct)}</span>${r ? ` <span class="tag ${r[1]}">${r[0]}</span>` : ''}
        <span class="meta">${it.resultsReleased ? `reported ${dt(it.resultDate)}${num(it.priceContext?.resultDayPct) !== null ? `, reaction ${pct(it.priceContext.resultDayPct)}` : ''}` : d !== null && d >= 0 ? `reports ${d === 0 ? 'today' : `in ${d}d`}` : ''}</span></li>`; }).join('')}</ol>
      <p class="meta">Ranked by 3-month move. Laggards in a sector whose reporters reacted well have more room to catch up.</p>`;
  }

  function viewSectors() {
    const rows = (S.data.sectors || []).filter(r => !S.q || r.sector.toLowerCase().includes(S.q.toLowerCase()));
    if (!rows.length) return '<div class="empty">Sector data appears once prices are tracked.</div>';
    const max = Math.max(10, ...rows.map(r => Math.abs(num(r.relativeToMarket) ?? 0)));
    const bar = v => { const n = num(v); if (n === null) return '—'; const w = Math.abs(n) / max * 50; return `<div class="bar" title="${pct(n)} vs market"><i style="left:${n >= 0 ? 50 : 50 - w}%;width:${w}%;background:${n >= 0 ? 'var(--gain)' : 'var(--loss)'}"></i><i style="left:50%;width:1px;background:var(--ink-3)"></i></div>`; };
    return `<p class="note">Tailwind combines how tracked peers moved over 3 months versus the whole universe, and how many peers that already reported had strong earnings. Checklist: five sector questions (demand, orders &amp; capex, earnings, peer confirmation, market strength), scored only where the data exists. Favour: five signs a sector is losing favour (relative weakness, poor reaction to good results, failed breakouts, shrinking participation, paying less for growth); "out of favour" with growing results is sentiment, "deteriorating" means results are weakening too. Tap a sector for details.</p>
      <div class="tscroll"><table class="sectbl"><thead><tr><th>Sector</th><th class="hide-sm">3-month vs market</th><th class="r">Relative</th><th>Favour</th><th class="r hide-sm">Reported</th><th class="r hide-sm">Strong</th><th class="r">Checklist</th><th class="hide-sm">Tailwind</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="srow" role="button" tabindex="0" data-sector="${esc(r.sector)}" aria-expanded="${S.open.has('sector:' + r.sector)}"><td><span class="chev-s" aria-hidden="true">${S.open.has('sector:' + r.sector) ? '▾' : '▸'}</span> ${esc(r.sector)} <span class="meta">(${r.stocks})</span></td><td class="hide-sm">${bar(r.relativeToMarket)}</td><td class="r n ${cls(r.relativeToMarket)}">${pct(r.relativeToMarket)}</td><td>${favTag(r.favour)}</td><td class="r n hide-sm">${r.declared}</td><td class="r n hide-sm">${r.strongResults}</td><td class="r n">${r.checklist && r.checklist.measured ? `${r.checklist.score}/${r.checklist.measured}` : '—'}</td><td class="hide-sm">${r.tailwind ? `<span class="tag ${TAIL_TONE[r.tailwind] || ''}">${r.tailwind.toLowerCase()}</span>` : '—'}</td></tr>${S.open.has('sector:' + r.sector) ? `<tr class="sdetail"><td colspan="8"><p class="meta">${r.index ? `<b>${esc(r.index.name)}</b> 1W ${pct(r.index.ret1w)} · 1M ${pct(r.index.ret1m)}${num((S.data.regime || {}).index1w) !== null ? ` (NIFTY 500 1W ${pct(S.data.regime.index1w)})` : ''}. ` : ''}<button type="button" data-act="sector-results" data-sector="${esc(r.sector)}">Show its ${r.declared} declared results</button></p>${favourBlock(r.favour)}${r.checklist ? `<div class="td"><div class="td-h"><b>Sector checklist</b> <span class="meta">${r.checklist.score} of ${r.checklist.measured} measured pass</span></div>${checklistChips(r.checklist)}</div>` : ''}${sectorPeers(r.sector)}</td></tr>` : ''}`).join('')}
      </tbody></table></div>`;
  }

  function viewScore() {
    const sc = S.data.scorecard || { rows: [] };
    return `<p class="note">${esc(sc.metric)}. This checks the core idea behind the buckets: did stocks with a strong Q1 reaction keep drifting? It fills in as more results are tracked; treat small samples as anecdotes.</p>
      <div class="tscroll"><table><thead><tr><th>Group</th><th class="r">Stocks</th><th class="r">Average</th><th class="r">Median</th><th class="r">Positive</th></tr></thead><tbody>
      ${sc.rows.map(r => `<tr><td>${esc(r.group)}</td><td class="r n">${r.n}</td><td class="r n ${cls(r.avg)}">${pct(r.avg)}</td><td class="r n ${cls(r.median)}">${pct(r.median)}</td><td class="r n">${num(r.winRate) === null ? '—' : num(r.winRate).toFixed(0) + '%'}</td></tr>`).join('')}
      </tbody></table></div>
      <h2 class="day">Entry timing: on the result vs after the concall</h2>
      <p class="note">Every triggered entry is recorded once: the starter taken before the call, and the full entry after it (or when no call is held). Returns run to today, or to the stop if it was hit.</p>
      <div class="tscroll"><table><thead><tr><th>Entry</th><th class="r">Trades</th><th class="r">Average</th><th class="r">Median</th><th class="r">Positive</th><th class="r">Stopped</th></tr></thead><tbody>
      ${(sc.timing || []).map(r => `<tr><td>${esc(r.group)}</td><td class="r n">${r.n}</td><td class="r n ${cls(r.avg)}">${pct(r.avg)}</td><td class="r n ${cls(r.median)}">${pct(r.median)}</td><td class="r n">${num(r.winRate) === null ? '—' : num(r.winRate).toFixed(0) + '%'}</td><td class="r n">${r.stopped ?? 0}</td></tr>`).join('')}
      </tbody></table></div>${runupTable(sc.runup)}`;
  }
  // 2.9.19: does a big run-up into the numbers spoil the reaction and the drift?
  function runupTable(ru) {
    if (!ru || !(ru.rows || []).length) return '';
    const rate = v => num(v) === null ? '—' : num(v).toFixed(0) + '%';
    return `<h2 class="day">Run-up into the result vs drift after it</h2>
      <p class="note">${esc(ru.metric)}. Drift is close to close after the reaction session, so it only counts stocks with at least one session since; it is days old this early in the season. Display only: no rule uses it.</p>
      <div class="tscroll"><table><thead><tr><th>20-day run-up</th><th class="r">Stocks</th><th class="r">Avg reaction</th><th class="r">With drift</th><th class="r">Avg drift</th><th class="r">Median drift</th><th class="r">Positive</th></tr></thead><tbody>
      ${ru.rows.map(r => `<tr><td>${esc(r.group)}</td><td class="r n">${r.n}</td><td class="r n ${cls(r.avgReaction)}">${pct(r.avgReaction)}</td><td class="r n">${r.nDrift}</td><td class="r n ${cls(r.avgDrift)}">${pct(r.avgDrift)}</td><td class="r n ${cls(r.medianDrift)}">${pct(r.medianDrift)}</td><td class="r n">${rate(r.winRate)}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  function viewHealth() {
    const h = S.data.health || {}, c = S.data.counts || {};
    const box = (l, v) => `<div class="hbox"><span>${l}</span><b>${v ?? '—'}</b></div>`;
    const sa = h.selfAudit || {};
    const tone = { OK: 'good', WARN: 'warn', FAIL: 'bad' };
    const nOk = (sa.checks || []).filter(c => c.status === 'OK').length, nIssue = (sa.checks || []).length - nOk;
    const shown = (sa.checks || []).filter(c => !S.issuesOnly || c.status !== 'OK');
    const audit = (sa.checks || []).length ? `<div class="audit"><h3>Automatic data check <span class="tag ${tone[sa.status] || ''}">${esc(sa.status || '')}</span></h3>
      ${nIssue ? `<button type="button" data-act="issues-only">${S.issuesOnly ? `Show all ${(sa.checks || []).length} checks` : `Show only the ${nIssue} to look at`}</button>` : ''}
      ${shown.map(c => `<div class="arow"><span class="tag ${tone[c.status] || ''}">${esc(c.status)}</span>
        <div><b>${esc(c.check)}</b>${c.count ? ` <span class="muted">(${c.count})</span>` : ''}${(c.examples || []).length ? `<div class="muted">${esc(c.examples.join(', '))}</div>` : ''}${c.status !== 'OK' && c.note ? `<div class="muted">${esc(c.note)}</div>` : ''}</div></div>`).join('')}</div>` : '';
    return audit + `<div class="health">
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
  const PAGE_VERSION = '2.9.37';   // must match the engine version (install check)
  function installBanner() {
    const d = S.data || {}, ic = (d.health || {}).installCheck || {};
    const engine = ic.engine || (String(d.version || '').match(/(\d+\.\d+\.\d+)\s*$/) || [])[1];
    const bad = [];
    // 2.9.27: a code push publishes the page at once while the data follows with
    // the next engine run (15-20 min). Newer page + recent data = "updating", not an error.
    const ver = v => String(v || '').split('.').map(Number);
    const newer = (a, b) => { const x = ver(a), y = ver(b); for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0); } return false; };
    const ageMin = d.generatedAt ? (Date.now() - new Date(d.generatedAt).getTime()) / 6e4 : Infinity;
    if (engine && engine !== PAGE_VERSION) {
      if (newer(PAGE_VERSION, engine) && ageMin < 90 && ic.ok !== false)
        return `<div class="install-wait"><b>Update in progress.</b> Version ${esc(PAGE_VERSION)} was just published; the data refreshes with the next run (usually within 20 minutes). Figures below are from ${esc(engine)} and still valid.</div>`;
      if (newer(engine, PAGE_VERSION)) bad.push(`this page (${PAGE_VERSION}) is older than the data (${engine}): reload the page`);
      else bad.push(`page files are ${PAGE_VERSION} but the engine (pead_v2.py) is ${engine} and no new run has published for ${Math.round(ageMin)} minutes`);
    }
    if (ic.ok === false) bad.push(`pead_plus.py is ${ic.pead_plus} but pead_v2.py is ${ic.engine}`);
    return bad.length ? `<div class="install-bad"><b>Files out of sync.</b> ${esc(bad.join('; '))}.</div>` : '';
  }
  function header() {
    const d = S.data, r = d.regime || {}, c = d.counts || {};
    $('sub').textContent = `${d.liveQuarter || 'Live quarter'} results season, updated ${d.generatedAt ? new Date(d.generatedAt).toLocaleString('en-IN', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'}`;
    const tone = { 'RISK-ON': 'on', MIXED: 'mixed', 'RISK-OFF': 'off' }[r.label] || '';
    const br = r.breadth || {}, ad = r.advanceDecline, fl = r.flows;
    const crs = v => { const n = num(v); return n === null ? '—' : `${n >= 0 ? '+' : '−'}₹${Math.abs(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`; };
    const internals = [
      num(br.above63Pct) !== null ? `Breadth: ${num(br.above63Pct).toFixed(0)}% of tracked stocks above their 63-day average${num(br.above21Pct) !== null ? `, ${num(br.above21Pct).toFixed(0)}% above 21-day` : ''}` : '',
      ad && num(ad.advancePct) !== null ? `NIFTY 500 last session: ${ad.advances} up / ${ad.declines} down` : '',
      fl ? `FII ${crs(fl.fiiNetCr)}, DII ${crs(fl.diiNetCr)} on ${dt(fl.date)}${num(fl.days) > 1 ? ` (last ${fl.days} days: FII ${crs(fl.fii5dCr)}, DII ${crs(fl.dii5dCr)})` : ''}${fl.fiiStreak ? `; FIIs net ${fl.fiiStreak > 0 ? 'buyers' : 'sellers'} ${Math.abs(fl.fiiStreak)} day${Math.abs(fl.fiiStreak) > 1 ? 's' : ''} running` : ''}` : '',
    ].filter(Boolean);
    $('internals').innerHTML = internals.map(t => `<span>${esc(t)}</span>`).join('');
    $('regime').innerHTML = r.label ? `<span class="dot ${tone}"></span><b>Market ${esc(r.label.toLowerCase())}</b>. ${esc(r.note)}${num(r.ret20dPct) !== null ? ` ${esc(r.index || 'Index')} ${pct(r.ret20dPct)} over 20 days.` : ''}` : `<span class="dot"></span>${esc(r.note && r.note !== 'Index history unavailable' ? r.note : 'Market regime appears after the next data refresh.')}`;
    const decl = declared(), act = decl.filter(it => sig(it)[1] === 'go').length;
    const soon = upcoming().filter(it => { const n = daysTo(it.resultDate); return n !== null && n >= 0 && n <= 7; }).length;
    const sa = (d.health || {}).selfAudit || {};
    const fails = (sa.checks || []).filter(c => c.status === 'FAIL');
    const auditBanner = fails.length ? `<div class="install-bad"><b>Data check found ${fails.length} problem${fails.length > 1 ? 's' : ''}:</b> ${esc(fails.map(c => c.check).join('; '))}. Details in Data health.</div>` : '';
    $('season').innerHTML = installBanner() + auditBanner + `<strong>${decl.length}</strong> results declared, <strong>${act}</strong> with an actionable setup, <strong>${soon}</strong> companies reporting in the next 7 days.`;
    $('c-setups').textContent = decl.length; $('c-radar').textContent = (() => { const r = radarAll().filter(it => !heldPos(it)); const a = r.filter(it => (P(it).grade || {}).tier === 'A').length; return a ? `${a} best · ${r.length}` : `${r.length}`; })(); $('c-trades').textContent = (d.positions || []).length; $('c-watch').textContent = upcoming().length; $('c-sectors').textContent = (d.sectors || []).length;
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
    $('controls').style.display = ['setups', 'radar', 'watch', 'sectors'].includes(S.tab) ? '' : 'none';
    $('sizing').style.display = S.tab === 'setups' ? '' : 'none';
    // 2.9.19: a hint only; the risk input keeps whatever the viewer typed.
    const rOff = (S.data.regime || {}).label === 'RISK-OFF', rNow = num(store.get('risk', 1));
    $('riskhint').innerHTML = rOff && rNow !== null && rNow > 0.5 ? '<span class="tag warn">Market risk-off: suggested 0.5% risk</span>' : '';
    $('liquid').parentElement.style.display = S.tab === 'sectors' ? 'none' : '';
    $('sort').style.display = S.tab === 'setups' ? '' : 'none';
    $('dir').style.display = S.tab === 'setups' ? '' : 'none';
    document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.tab === S.tab)));
    const active = document.querySelector('.tab[aria-selected="true"]');
    if (active && active.scrollIntoView) active.scrollIntoView({ inline: 'center', block: 'nearest' });
    $('view').innerHTML = { setups: viewSetups, radar: viewRadar, trades: viewTrades, watch: viewWatch, sectors: viewSectors, score: viewScore, health: viewHealth }[S.tab]();
  }

  // ---------- events ----------
  document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => { S.tab = t.dataset.tab; S.bucket = 'ALL'; store.set('tab', S.tab); render(); }));
  $('q').addEventListener('input', e => { S.q = e.target.value.trim(); render(); });
  $('liquid').addEventListener('change', e => { S.liquid = e.target.checked; store.set('liquid', S.liquid); render(); });
  $('sort').addEventListener('change', e => { S.sort = e.target.value; store.set('sort', S.sort); render(); });
  const showDir = () => { $('dir').textContent = S.dir === 'asc' ? '↑ Low → high' : '↓ High → low'; $('dir').setAttribute('aria-label', S.dir === 'asc' ? 'Ascending' : 'Descending'); };
  $('dir').addEventListener('click', () => { S.dir = S.dir === 'asc' ? 'desc' : 'asc'; store.set('dir', S.dir); showDir(); render(); });
  $('chips').addEventListener('click', e => { const b = e.target.closest('[data-bucket]'); if (b) { S.bucket = b.dataset.bucket; render(); } });
  ['cap', 'risk', 'pcap'].forEach(id => $(id).addEventListener('change', e => { store.set(id, num(e.target.value)); render(); }));
  $('view').addEventListener('click', e => {
    if (e.target.closest('[data-act="show-illiquid"]')) { S.liquid = false; $('liquid').checked = false; render(); return; }
    if (e.target.closest('[data-act="issues-only"]')) { S.issuesOnly = !S.issuesOnly; render(); return; }
    const sr = e.target.closest('[data-act="sector-results"]');
    if (sr) { S.tab = 'setups'; S.q = sr.dataset.sector; $('q').value = S.q; S.bucket = 'ALL'; render(); window.scrollTo(0, 0); return; }
    const cp = e.target.closest('[data-copy]');
    if (cp) {
      const txt = copyText(cp.dataset.copy, S_COPY[cp.dataset.key] || []), msg = document.querySelector(`[data-copy-msg="${cp.dataset.key}"]`);
      const done = ok => { if (msg) msg.textContent = ok ? (cp.dataset.copy === 'tv' ? 'Copied: in TradingView, Watchlist → Import list / paste.' : 'Copied as CSV.') : 'Copy blocked by the browser.'; };
      (navigator.clipboard ? navigator.clipboard.writeText(txt).then(() => done(true), () => done(false)) : done(false));
      return;
    }
    const qx = e.target.closest('[data-act="qty-copy"]');
    if (qx) {
      const out = (S.data.positions || []).map(p => { let q = null; try { q = localStorage.getItem('qty:' + p.symbol); } catch (err) { /* blocked */ } return q ? `${p.symbol}=${q}` : null; }).filter(Boolean).join(';');
      const m = $('qty-msg'); (navigator.clipboard ? navigator.clipboard.writeText(out).then(() => { if (m) m.textContent = out ? 'Copied. Paste it on your other device.' : 'No quantities entered yet.'; }, () => { if (m) m.textContent = out; }) : (m && (m.textContent = out)));
      return;
    }
    if (e.target.closest('[data-act="qty-apply"]')) {
      const v = ($('qty-in') || {}).value || '';
      let n = 0;
      v.split(/[;,\s]+/).forEach(pair => { const m = pair.match(/^([A-Za-z0-9&-]+)=(\d+)$/); if (m) { try { localStorage.setItem('qty:' + m[1].toUpperCase(), m[2]); n += 1; } catch (err) { /* blocked */ } } });
      render(); const m = $('qty-msg'); if (m) m.textContent = n ? `Saved ${n} quantit${n > 1 ? 'ies' : 'y'} on this device.` : 'Nothing recognised (format HCLTECH=114;GROWW=750).';
      return;
    }
    const srow = e.target.closest('.srow'); if (srow) { const k = 'sector:' + srow.dataset.sector; S.open.has(k) ? S.open.delete(k) : S.open.add(k); render(); return; }
    const row = e.target.closest('.row'); if (row && !e.target.closest('a')) { const id = row.dataset.id; S.open.has(id) ? S.open.delete(id) : S.open.add(id); render(); }
  });
  $('view').addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && (e.target.classList.contains('row') || e.target.classList.contains('srow'))) { e.preventDefault(); e.target.click(); } });
  const applyTheme = t => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; $('theme').textContent = (t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark' ? 'Light mode' : 'Dark mode'; };
  $('theme').addEventListener('click', () => { const cur = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); const next = cur === 'dark' ? 'light' : 'dark'; store.set('theme', next); applyTheme(next); });

  // ---------- boot ----------
  S.tab = store.get('tab', 'setups'); S.liquid = store.get('liquid', true); S.sort = store.get('sort', 'conviction');
  S.dir = store.get('dir', 'desc');
  if (!$('sort').querySelector(`option[value="${S.sort}"]`)) S.sort = 'conviction';
  $('liquid').checked = S.liquid; $('sort').value = S.sort; showDir();
  $('cap').value = store.get('cap', 1000000); $('risk').value = store.get('risk', 1); $('pcap').value = store.get('pcap', 15);
  applyTheme(store.get('theme', null));
  fetch('intelligence.json?t=' + Date.now(), { cache: 'no-store' })
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(d => { S.data = d; render(); })
    .catch(err => { $('view').innerHTML = `<div class="empty">Could not load intelligence.json (${esc(err.message)}). The page shows data once the engine has published at least once.</div>`; });
})();
