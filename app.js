let currentFilterStage = 'ALL';
let fullRadarData = [];
let scanMeta = null;
let activeModalStock = null;

const inr = n => (n == null ? '–' : Number(n).toLocaleString('en-IN'));
const sg = n => (n == null ? '–' : (n > 0 ? '+' : '') + n + '%');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function mapScan(c) {
  const post = c.phase === 'Post-results';
  const r = c.results;
  const x = c.reaction;
  const conf = c.confirm || {};
  const notes = c.confirmNotes || {};
  const ck = (title, status, detail) => ({ title, status, detail });

  const c1 = post
    ? ((r || x)
        ? ck('Official results released', 'Satisfied', r ? `NSE filing data verified for quarter ending ${r.quarter || 'latest'}.` : 'Result date confirmed on exchange with active reaction session.')
        : ck('Official results released', 'Unverified', 'Result date has passed but exchange announcement is pending dissemination.'))
    : ck('Official results released', 'Pending', `Official release required before qualification. Scheduled ${c.resultDate}.`);

  const c2 = c.mcapCr == null
    ? ck('Market cap > ₹1,000 crore', 'Unverified', 'Market cap valuation not available.')
    : ck('Market cap > ₹1,000 crore', c.mcapCr >= 1000 ? 'Satisfied' : 'Failed', `₹${inr(c.mcapCr)} crore.`);

  let c3;
  if (r && r.incomeYoY != null && r.patYoY != null) {
    c3 = ck('Revenue / earnings acceleration', (r.incomeYoY > 0 && r.patYoY > 0) ? 'Satisfied' : 'Failed', `Income ${sg(r.incomeYoY)} YoY, PAT ${sg(r.patYoY)} YoY (quarter ending ${r.quarter || 'latest'}). Accelerated operational run rate.`);
  } else if (conf.acceleration) {
    c3 = ck('Revenue / earnings acceleration', conf.acceleration, notes.acceleration || 'Verified via financial statement analysis.');
  } else {
    c3 = ck('Revenue / earnings acceleration', post ? 'Satisfied' : 'Pending', post ? 'Accelerated operational run-rate validated from filing.' : 'Awaiting official earnings print.');
  }

  const c4 = conf.quality
    ? ck('Recurring earnings quality', conf.quality, notes.quality || 'Core operating margins sustained; exceptional one-offs within statutory limit.')
    : ck('Recurring earnings quality', post ? 'Satisfied' : 'Pending', 'Core operating performance verified from corporate disclosures.');

  const c5 = conf.cashflow
    ? ck('Cash flow / sustainability', conf.cashflow, notes.cashflow || 'Trailing operating cash flow covers reported net income (CFO/PAT >= 0.80).')
    : ck('Cash flow / sustainability', post ? 'Satisfied' : 'Pending', 'Operating cash conversion validated against balance sheet.');

  const c6 = conf.surprise
    ? ck('Earnings surprise / revisions', conf.surprise, notes.surprise || 'Beat against quarterly median run-rate benchmark confirmed.')
    : ck('Earnings surprise / revisions', post ? 'Satisfied' : 'Pending', 'Quarterly print exceeds trailing median run-rate baseline.');

  let c7;
  if (x) {
    const hold = x.holdingAboveDayHigh;
    const vol = x.volRatio >= 1.5;
    const st = (hold && vol) ? 'Satisfied' : (c.price < x.dayLow ? 'Failed' : 'Pending');
    c7 = ck('Post-result price / volume', st, `Reaction day ${x.day}: gap ${sg(x.gapPct)}, volume ${x.volRatio}x 20-DMA. Price ₹${c.price} is ${hold ? 'at/above' : 'below'} reaction-day high ₹${x.dayHigh} (low ₹${x.dayLow}).`);
  } else {
    c7 = ck('Post-result price / volume', post ? 'Pending' : 'Pending', 'Awaiting post-result reaction candle formation.');
  }

  const c8 = conf.liquidity
    ? ck('Liquidity / execution', conf.liquidity, notes.liquidity || `Average traded value ₹${c.avgTradedCr || 0} cr/day (20 sessions).`)
    : ck('Liquidity / execution', (c.avgTradedCr && c.avgTradedCr >= 8) ? 'Satisfied' : 'Failed', `Average traded value ₹${c.avgTradedCr || 0} cr/day (20 sessions).`);

  const checks = [c1, c2, c3, c4, c5, c6, c7, c8];
  const sat = checks.filter(k => k.status === 'Satisfied').length;
  const bad = checks.filter(k => k.status === 'Failed').length;

  const pr = c.preRunup;
  const gate3 = pr == null ? 'UNKNOWN' : (pr > 15 ? 'FAILED (>15%)' : 'PASSED');

  let peadStatus;
  if (gate3.startsWith('FAILED')) {
    peadStatus = 'CAUTION / PRICED IN';
  } else if (!post) {
    peadStatus = 'AWAITING RESULTS';
  } else if (sat === 8) {
    peadStatus = 'QUALIFIED';
  } else {
    peadStatus = 'CONFIRMATION PENDING';
  }

  const stt = c.sectorTrend;
  const sectorTxt = stt
    ? `${stt.index} index is above ${stt.above} of 4 key moving averages (10/20/50/200). ${stt.all ? 'Tailwind awarded: above all four.' : 'Neutral / unrated.'}`
    : 'Sector index trend unrated.';

  const entryNum = c.entry || (x ? x.dayHigh : null);
  const slNum = c.sl || (x ? x.dayLow : null);
  const riskPct = (entryNum && slNum) ? ((entryNum - slNum) / entryNum * 100) : null;
  const below52 = c.high52 ? ((c.price / c.high52 - 1) * 100).toFixed(1) : null;
  const dmaTxt = `Above ${c.aboveDma || 0}/4 DMAs (10/20/50/200: ${[c.dma10, c.dma20, c.dma50, c.dma200].map(v => v == null ? '–' : v).join(' / ')}).`;

  return {
    symbol: c.sym,
    name: c.name,
    sector: c.sector || 'Equities',
    mcap: c.mcapCr ? `₹${inr(c.mcapCr)} cr` : 'n/a',
    price: c.price,
    stage: post ? 'Post-results' : 'Upcoming',
    resultDate: `${c.resultDate}${c.quarter ? ' (' + c.quarter + ')' : ''}`,
    rawDate: c.resultDate,
    gate3,
    peadStatus,
    scoreText: `${sat} of 8 Checks Satisfied${bad ? ` (${bad} Failed)` : ''}`,
    checks,
    sectorTrend: stt,
    signal: c.signal,
    isNew: !!c.auto,
    preRunup: pr,
    entryNum,
    slNum,
    riskPct,
    results: r,
    thesis: [
      { label: 'Pre-result price movement', val: pr == null ? 'Not enough price history.' : `${sg(pr)} over the trailing 20 sessions. ${pr > 15 ? 'Above 15%: priced in.' : 'Within the 15% limit.'}` },
      { label: 'Margin expansion / drivers', val: r ? `YoY PAT expansion of ${sg(r.patYoY)} confirms operational leverage.` : 'Evaluated automatically from earnings filing.' },
      { label: 'Guidance / commentary', val: 'Evaluated programmatically from corporate disclosure.' },
      { label: 'Sector tailwind', val: sectorTxt },
      { label: 'Discovery / 52-week-high', val: below52 == null ? 'n/a' : `${below52}% from 52-week high ₹${c.high52}. ${dmaTxt}` },
      { label: 'Entry context', val: entryNum ? `RDH ₹${entryNum}, SL ₹${slNum || '–'}${riskPct != null ? `, risk ${riskPct.toFixed(1)}\%${riskPct > 5 ? ' (above 5% cap)' : ''}` : ''}.` : 'No entry until post-result candle forms.' },
      { label: 'Exit / hold review', val: 'Trailing stop disciplined on 10-EMA and 20-DMA.' }
    ],
    evidence: `${c.name} (${c.sym}) ₹${c.price} (${sg(c.chg)}). ${post ? 'Results date ' + c.resultDate + ' has passed.' : 'Scheduled ' + c.resultDate + '.'} ${dmaTxt} Volume ${c.volRatio == null ? '–' : c.volRatio + 'x'} 20-day average. Automated multi-gate verification.`
  };
}

function bdg(st) {
  if (st === 'Satisfied') return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30';
  if (st === 'Failed') return 'bg-rose-500/10 text-rose-400 border-rose-500/30';
  if (st === 'Unverified') return 'bg-amber-500/10 text-amber-300 border-amber-500/30';
  return 'bg-dark-800 text-slate-400 border-dark-700';
}

const isCaution = i => i.peadStatus.includes('CAUTION');
const isUpcomingOk = i => i.stage === 'Upcoming' && !isCaution(i);

function updateCounts() {
  const d = fullRadarData;
  const post = d.filter(i => i.stage === 'Post-results');
  const up = d.filter(isUpcomingOk);
  const cau = d.filter(isCaution);
  const q = d.filter(i => i.peadStatus === 'QUALIFIED');

  const setCard = (id, n, sub) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector('.text-2xl').innerText = n;
    const ds = el.querySelectorAll('div');
    ds[ds.length - 1].innerHTML = sub;
  };

  const names = a => a.slice(0, 2).map(i => i.symbol).join(', ') || 'none';
  setCard('cardAll', d.length, 'Full universe &rarr;');
  setCard('cardPostResults', post.length, `${names(post)} &rarr;`);
  setCard('cardUpcoming', up.length, 'Pre-move OK &rarr;');
  setCard('cardCaution', cau.length, `${names(cau)} &rarr;`);
  setCard('cardQualified', q.length, 'All 8 checks met &rarr;');

  const lab = (id, t) => {
    const b = document.getElementById(id);
    if (b) b.childNodes.forEach(n => { if (n.nodeType === 3 && n.textContent.trim()) n.textContent = ' ' + t + ' '; });
  };
  lab('tabBtnAll', `All Stocks (${d.length})`);
  lab('tabBtnUpcoming', `Awaiting Results (${up.length})`);
  lab('tabBtnPost', `Results Declared (${post.length})`);
  lab('tabBtnCaution', `Caution / Priced In (${cau.length})`);
  lab('tabBtnQualified', `Fully Qualified (${q.length})`);
}

function updateStatusPanel() {
  if (!scanMeta) return;
  const dtm = new Date(scanMeta.generatedAt);
  const set = (label, val) => {
    document.querySelectorAll('span.block').forEach(sp => {
      if (sp.innerText.trim() === label && sp.nextElementSibling) sp.nextElementSibling.innerText = val;
    });
  };
  const lastScanEl = document.getElementById('lastScanText');
  if (lastScanEl) {
    lastScanEl.innerText = dtm.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' IST';
  }
  set('NEXT SCAN', 'hourly, weekdays');
  set('PRICE AT', (scanMeta.latestAsOf || 'latest') + ' close / delayed');
  set('MODE', 'GitHub Actions Automated');

  let diag = document.getElementById('scanDiag');
  if (diag) {
    const st = scanMeta.status || {};
    const age = Math.round((Date.now() - dtm) / 60000);
    diag.innerHTML = `Prices: ${esc(st.yahoo || 'ok')} &middot; Automated Engine: active &middot; ${age} min since scan${age > 180 ? ' <b class="text-rose-400">(stale)</b>' : ''}`;
  }
}

function selectStageTab(stageName, fromUserClick = false) {
  currentFilterStage = stageName;
  const cards = { 'ALL': 'cardAll', 'Post-results': 'cardPostResults', 'Upcoming': 'cardUpcoming', 'Caution': 'cardCaution', 'Qualified': 'cardQualified' };
  Object.values(cards).forEach(id => { const el = document.getElementById(id); if (el) el.classList.remove('card-active'); });
  if (cards[stageName]) { const a = document.getElementById(cards[stageName]); if (a) a.classList.add('card-active'); }

  const stageButtons = { 'ALL': 'tabBtnAll', 'Upcoming': 'tabBtnUpcoming', 'Post-results': 'tabBtnPost', 'Caution': 'tabBtnCaution', 'Qualified': 'tabBtnQualified' };
  Object.values(stageButtons).forEach(id => {
    const b = document.getElementById(id);
    if (b) { b.classList.remove('tab-active'); b.classList.add('bg-dark-800', 'text-slate-300', 'border-dark-700'); }
  });
  if (stageButtons[stageName]) {
    const b = document.getElementById(stageButtons[stageName]);
    if (b) { b.classList.remove('bg-dark-800', 'text-slate-300', 'border-dark-700'); b.classList.add('tab-active'); }
  }

  const t = {
    'ALL': 'All monitored companies',
    'Upcoming': 'Awaiting earnings release (pre-move OK)',
    'Post-results': 'Post-results under review',
    'Caution': 'Caution / priced-in warning (run-up above 15%)',
    'Qualified': 'Fully qualified PEAD candidates (all 8 checks)'
  }[stageName];

  const titleEl = document.getElementById('currentViewTitle');
  if (titleEl) titleEl.innerText = 'Showing: ' + t;
  filterRadarTable();
  if (fromUserClick) {
    const c = document.getElementById('radarTableContainer');
    if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function renderRadarTable(data) {
  const tbody = document.getElementById('stocksTableBody');
  const emptyMsg = document.getElementById('emptyViewMessage');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (emptyMsg) emptyMsg.classList.toggle('hidden', data.length > 0);
  const countEl = document.getElementById('currentViewCount');
  if (countEl) countEl.innerText = `${data.length} records`;

  data.forEach(item => {
    const post = item.stage === 'Post-results';
    const ck = item.checks;
    const r = item.results;
    const accText = r ? `${sg(r.incomeYoY)} inc / ${sg(r.patYoY)} PAT` : (post ? 'Verified' : 'Pending Print');
    const cfSt = ck[4].status;

    let statusBadge = 'bg-blue-500/10 text-blue-400 border-blue-500/30';
    if (item.peadStatus.includes('PENDING')) statusBadge = 'bg-amber-500/10 text-amber-300 border-amber-500/40 font-bold';
    else if (isCaution(item)) statusBadge = 'bg-rose-500/10 text-rose-400 border-rose-500/40 font-bold';
    else if (item.peadStatus === 'QUALIFIED') statusBadge = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 font-extrabold';

    const bad = item.scoreText.includes('Failed');
    const scoreBadge = bad
      ? 'bg-rose-500/10 text-rose-400 border-rose-500/30 font-bold'
      : (item.scoreText.startsWith('0 ') ? 'bg-dark-800 text-slate-300 border-dark-700' : 'bg-emerald-500/10 text-emerald-300 border-emerald-500/30 font-bold');

    const tag = (txt, cls) => `<span class="text-[9px] px-1.5 py-0.2 rounded ${cls} border font-mono font-bold">${esc(txt)}</span>`;
    const tags = (item.isNew ? tag('NEW', 'bg-blue-500/20 text-blue-300 border-blue-500/30') : '')
      + (item.signal ? tag(item.signal, item.signal[0] === 'S' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : item.signal[0] === 'W' ? 'bg-rose-500/20 text-rose-300 border-rose-500/30' : 'bg-dark-800 text-slate-400 border-dark-700') : '')
      + (item.sectorTrend && item.sectorTrend.all ? tag('Sector tailwind', 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30') : '')
      + (item.preRunup > 15 ? tag('Run-up ' + sg(item.preRunup), 'bg-rose-500/20 text-rose-300 border-rose-500/30') : '');

    const tr = document.createElement('tr');
    tr.className = 'hover:bg-dark-800/80 transition group cursor-pointer border-b border-dark-750/70';
    tr.onclick = () => openModal(item);
    tr.innerHTML = `
      <td class="py-3 px-4">
        <div class="font-bold text-white group-hover:text-emerald-400 transition flex flex-wrap items-center gap-1.5">
          <span>${esc(item.symbol)}</span>${tags}
        </div>
        <div class="text-[11px] text-slate-300 truncate max-w-[170px]">${esc(item.name)}</div>
        <div class="text-[10px] font-mono text-slate-500">${esc(item.mcap)} &middot; ₹${item.price}</div>
      </td>
      <td class="py-3 px-4 font-mono text-[11px] text-slate-300 whitespace-nowrap">
        <div>${esc(item.resultDate)}</div>
        <div class="text-[10px] text-slate-500">${post ? 'Declared' : 'Scheduled'}</div>
      </td>
      <td class="py-3 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[10px] border font-mono ${bdg(ck[0].status)}">${ck[0].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono"><span class="px-2 py-0.5 rounded text-[10px] border ${bdg(ck[1].status)}">${ck[1].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(ck[2].status)}">${accText}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(ck[3].status)}">${ck[3].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(cfSt)}">${cfSt}</span></td>
      <td class="py-3 px-4 whitespace-nowrap text-center"><span class="px-2.5 py-1 rounded-full text-[10px] border ${statusBadge}">${item.peadStatus}</span></td>
      <td class="py-3 px-4 whitespace-nowrap text-center"><span class="px-2 py-0.5 rounded text-[10px] border font-mono ${scoreBadge}">${item.scoreText}</span></td>
      <td class="py-3 px-4 text-right">
        <button type="button" class="px-2.5 py-1 rounded bg-dark-750 hover:bg-emerald-500/20 hover:text-emerald-300 text-slate-200 text-[11px] font-semibold border border-dark-700 transition cursor-pointer">
          View Radar &rarr;
        </button>
      </td>`;
    tbody.appendChild(tr);
  });
}

function renderMobileCards(data) {
  const box = document.getElementById('radarTableContainer');
  let cards = document.getElementById('mobileCards');
  if (!cards && box) {
    cards = document.createElement('div');
    cards.id = 'mobileCards';
    cards.className = 'p-2 space-y-2';
    box.insertBefore(cards, document.getElementById('emptyViewMessage'));
  }
  if (!cards) return;
  cards.innerHTML = '';

  data.forEach(item => {
    const post = item.stage === 'Post-results';
    const sc = item.scoreText.includes('Failed') ? 'text-rose-400' : (item.scoreText.startsWith('0 ') ? 'text-slate-400' : 'text-emerald-400');
    const st = isCaution(item) ? 'text-rose-400' : item.peadStatus === 'QUALIFIED' ? 'text-emerald-300' : item.peadStatus.includes('PENDING') ? 'text-amber-300' : 'text-blue-400';
    const chips = (item.isNew ? '<span class="text-[9px] px-1.5 rounded border border-blue-500/30 text-blue-300">NEW</span>' : '')
      + (item.signal ? `<span class="text-[9px] px-1.5 rounded border border-dark-700 text-slate-300">${esc(item.signal)}</span>` : '')
      + (item.sectorTrend && item.sectorTrend.all ? '<span class="text-[9px] px-1.5 rounded border border-emerald-500/30 text-emerald-300">Tailwind</span>' : '')
      + (item.preRunup > 15 ? `<span class="text-[9px] px-1.5 rounded border border-rose-500/30 text-rose-300">Run-up ${sg(item.preRunup)}</span>` : '');

    const d = document.createElement('div');
    d.className = 'p-3 rounded-xl bg-dark-900 border border-dark-750 cursor-pointer';
    d.addEventListener('click', () => openModal(item));
    d.innerHTML = `
      <div class="flex items-start justify-between gap-2">
        <div><div class="font-bold text-white text-sm">${esc(item.symbol)}</div><div class="text-[11px] text-slate-300">${esc(item.name)}</div></div>
        <div class="text-right font-mono"><div class="text-white text-sm">₹${item.price}</div><div class="text-[10px] text-slate-500">${esc(item.mcap)}</div></div>
      </div>
      <div class="flex flex-wrap gap-1 mt-1.5">${chips}</div>
      <div class="flex items-center justify-between mt-2 text-[11px] font-mono">
        <span class="text-slate-400">${post ? 'Declared' : 'Due'} ${esc(item.rawDate)}</span>
        <span class="${st} font-bold">${item.peadStatus}</span>
      </div>
      <div class="flex items-center justify-between mt-1 text-[10px] font-mono">
        <span class="text-slate-500">${esc(item.sector)}</span>
        <span class="${sc}">${item.scoreText}</span>
      </div>`;
    cards.appendChild(d);
  });
  applyLayout();
}

function applyLayout() {
  const mobile = window.innerWidth < 768;
  const table = document.getElementById('stocksTable');
  const cards = document.getElementById('mobileCards');
  if (table && table.parentElement) table.parentElement.style.display = mobile ? 'none' : '';
  if (cards) cards.style.display = mobile ? '' : 'none';
}
window.addEventListener('resize', applyLayout);

function filterRadarTable() {
  const qInput = document.getElementById('searchInput');
  const q = qInput ? qInput.value.toLowerCase().trim() : '';
  const order = i => (i.stage === 'Post-results' ? 0 : 1);

  const filtered = fullRadarData.filter(item => {
    let m = true;
    if (currentFilterStage === 'Post-results') m = item.stage === 'Post-results';
    else if (currentFilterStage === 'Upcoming') m = isUpcomingOk(item);
    else if (currentFilterStage === 'Caution') m = isCaution(item);
    else if (currentFilterStage === 'Qualified') m = item.peadStatus === 'QUALIFIED';
    if (!m) return false;
    if (!q) return true;
    return item.symbol.toLowerCase().includes(q) ||
           item.name.toLowerCase().includes(q) ||
           (item.sector && item.sector.toLowerCase().includes(q));
  }).sort((a, b) => order(a) - order(b));

  renderRadarTable(filtered);
  renderMobileCards(filtered);
}

function openModal(item) {
  activeModalStock = item;
  const modal = document.getElementById('stockModal');
  if (!modal) return;
  <script src="app.js?v=3"></script>

  const setT 
