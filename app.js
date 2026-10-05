let currentFilterStage = 'ALL';
let fullRadarData = [];
let scanMeta = null;
let activeModalStock = null;
const inr = n => (n == null ? '–' : Number(n).toLocaleString('en-IN'));
const sg = n => (n == null ? '–' : (n > 0 ? '+' : '') + n + '%');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const LS_KEY = 'peadConfirm';
const getOv = () => { try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { return {}; } };
const MANUAL = { 3: ['quality', 'Recurring earnings quality'], 4: ['cashflow', 'Cash flow / sustainability'], 5: ['surprise', 'Earnings surprise / revisions'], 7: ['liquidity', 'Liquidity / execution'] };
function setConfirm(sym, key, val) {
  const all = getOv(); all[sym] = all[sym] || {};
  if (val) all[sym][key] = val; else delete all[sym][key];
  try { localStorage.setItem(LS_KEY, JSON.stringify(all)); } catch (e) {}
  fullRadarData = scanMeta.companies.filter(c => !c.error).map(mapScan);
  updateCounts(); filterRadarTable(); openModal(sym);
}

function mapScan(c) {
  const post = c.phase === 'Post-results';
  const r = c.results, x = c.reaction, conf = Object.assign({}, c.confirm || {}, getOv()[c.sym] || {}), notes = c.confirmNotes || {};
  const ck = (title, status, detail) => ({ title, status, detail });
  const manual = (key, title, dflt) => conf[key] ? ck(title, conf[key], notes[key] || 'Confirmed manually in companies.json.') : ck(title, post ? 'Unverified' : 'Pending', dflt);
  const c1 = post
    ? ((r || x) ? ck('Official results released', 'Satisfied', r ? `NSE filing data found for quarter ending ${r.quarter}.` : 'Result date has passed and a post-result session exists. Filing itself not machine-verified.')
                : ck('Official results released', 'Unverified', 'Result date has passed but no filing or post-result session data found yet.'))
    : ck('Official results released', 'Pending', `Official release required before selection. Scheduled ${c.resultDate}.`);
  const c2 = c.mcapCr == null ? ck('Market cap > ₹1,000 crore', 'Unverified', 'Market cap not available from data source.')
    : ck('Market cap > ₹1,000 crore', c.mcapCr >= 1000 ? 'Satisfied' : 'Failed', `₹${inr(c.mcapCr)} crore.`);
  let c3;
  if (r && r.incomeYoY != null && r.patYoY != null) {
    c3 = ck('Revenue / earnings acceleration', (r.incomeYoY > 0 && r.patYoY > 0) ? 'Satisfied' : 'Failed', `Income ${sg(r.incomeYoY)} YoY, profit ${sg(r.patYoY)} YoY (quarter ending ${r.quarter}). Check QoQ and one-offs in the filing.`);
  } else {
    c3 = ck('Revenue / earnings acceleration', post ? 'Unverified' : 'Pending', post ? 'Growth figures not available from NSE feed. Read the filing.' : 'Awaiting official earnings print.');
  }
  const c4 = manual('quality', 'Recurring earnings quality', post ? 'Exceptional items and core operating performance need manual review of the filing.' : 'Awaiting official earnings print.');
  const c5 = manual('cashflow', 'Cash flow / sustainability', 'Operating cash flow vs PAT and capex needs manual review.');
  const c6 = manual('surprise', 'Earnings surprise / revisions', 'Needs pre-result consensus and guidance. Not available from free data.');
  let c7;
  if (x) {
    const hold = x.holdingAboveDayHigh, vol = x.volRatio >= 2;
    const st = (hold && vol) ? 'Satisfied' : (c.price < x.dayLow ? 'Failed' : 'Pending');
    c7 = ck('Post-result price / volume', st, `Reaction day ${x.day}: gap ${sg(x.gapPct)}, day ${sg(x.dayPct)}, volume ${x.volRatio}x. Price ₹${c.price} is ${hold ? 'at/above' : 'below'} reaction-day high ₹${x.dayHigh} (low ₹${x.dayLow}). Rule: close above high on volume >2x.`);
  } else {
    c7 = ck('Post-result price / volume', post ? 'Unverified' : 'Pending', 'Needs a post-result session. RDH breakout test on volume >2x.');
  }
  const c8 = conf.liquidity ? ck('Liquidity / execution', conf.liquidity, notes.liquidity || 'Confirmed manually.')
    : ck('Liquidity / execution', 'Unverified', `Average traded value ₹${c.avgTradedCr} cr/day (20 sessions). Spread and circuit limits need manual check.`);
  const checks = [c1, c2, c3, c4, c5, c6, c7, c8];
  const sat = checks.filter(k => k.status === 'Satisfied').length, bad = checks.filter(k => k.status === 'Failed').length;
  const pr = c.preRunup;
  const gate3 = pr == null ? 'UNKNOWN' : (pr > 15 ? 'FAILED (>15%)' : 'PASSED');
  let peadStatus;
  if (gate3.startsWith('FAILED')) peadStatus = 'CAUTION / PRICED IN';
  else if (!post) peadStatus = 'AWAITING RESULTS';
  else if (sat === 8) peadStatus = 'QUALIFIED';
  else peadStatus = 'CONFIRMATION PENDING';
  const stt = c.sectorTrend;
  const sectorTxt = stt ? `${stt.index} index is above ${stt.above} of 4 key moving averages (10/20/50/200). ${stt.all ? 'Tailwind awarded: above all four.' : 'Not a tailwind: needs all four.'}` : 'Sector index not available for this stock. Unrated.';
  const entryNum = c.entry || (x ? x.dayHigh : null), slNum = c.sl || (x ? x.dayLow : null);
  const riskPct = (entryNum && slNum) ? ((entryNum - slNum) / entryNum * 100) : null;
  const below52 = c.high52 ? ((c.price / c.high52 - 1) * 100).toFixed(1) : null;
  const dmaTxt = `Above ${c.aboveDma}/4 DMAs (10/20/50/200: ${[c.dma10, c.dma20, c.dma50, c.dma200].map(v => v == null ? '–' : v).join(' / ')}).`;
  return {
    symbol: c.sym, name: c.name, sector: c.sector,
    mcap: c.mcapCr ? `₹${inr(c.mcapCr)} cr` : 'n/a', price: c.price, stage: post ? 'Post-results' : 'Upcoming',
    resultDate: `${c.resultDate}${c.quarter ? ' (' + c.quarter + ')' : ''}`,
    gate3, peadStatus, scoreText: `${sat} of 8 Checks Satisfied${bad ? ` (${bad} Failed)` : ''}`,
    checks, sectorTrend: stt, signal: c.signal, isNew: !!c.auto, preRunup: pr, nseDate: c.nseDate, rawDate: c.resultDate,
    entryNum, slNum, riskPct, results: r,
    thesis: [
      { label: 'Pre-result price movement', val: pr == null ? 'Not enough price history.' : `${sg(pr)} over the 20 sessions before ${post ? 'the result date' : 'now'}. ${pr > 15 ? 'Above 15%: possibly priced in, wait.' : 'Within the 15% pre-move limit.'}` },
      { label: 'Margin expansion / drivers', val: 'Needs manual review. Not available from free data.' },
      { label: 'Guidance / commentary / calls', val: 'Needs manual review.' },
      { label: 'Sector tailwind', val: sectorTxt },
      { label: 'Management delivery record', val: 'Needs manual review.' },
      { label: 'Discovery / 52-week-high review', val: below52 == null ? 'n/a' : `${below52}% from 52-week high ₹${c.high52}. ${dmaTxt} A 52-week high is a lead, not an entry trigger.` },
      { label: 'Entry context', val: entryNum ? `RDH ₹${entryNum}, stop ₹${slNum || '–'}${riskPct != null ? `, risk ${riskPct.toFixed(1)}%${riskPct > 5 ? ' (above the 5% cap)' : ''}` : ''}.` : 'No entry until a post-result reaction candle exists.' },
      { label: 'Exit / hold review', val: 'Existing SL / TSL discipline applies (10-EMA / 20-DMA trail).' }
    ],
    evidence: `${c.name} (${c.sym}) ₹${c.price} (${sg(c.chg)} on ${c.asOf}). ${post ? 'Results date ' + c.resultDate + ' has passed.' : 'Results scheduled ' + c.resultDate + '.'} ${dmaTxt} Volume ${c.volRatio == null ? '–' : c.volRatio + 'x'} 20-day average.${c.nseDate && c.nseDate !== c.resultDate ? ' NSE lists a different board-meeting date: ' + c.nseDate + '.' : ''} Automated data, delayed. Verify on NSE/BSE before acting.`
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
  const post = d.filter(i => i.stage === 'Post-results'), up = d.filter(isUpcomingOk), cau = d.filter(isCaution), q = d.filter(i => i.peadStatus === 'QUALIFIED');
  const setCard = (id, n, sub) => { const el = document.getElementById(id); if (!el) return; el.querySelector('.text-2xl').innerText = n; const ds = el.querySelectorAll('div'); ds[ds.length - 1].innerHTML = sub; };
  const names = a => a.slice(0, 2).map(i => i.symbol).join(', ') || 'none';
  setCard('cardAll', d.length, 'Full universe &rarr;');
  setCard('cardPostResults', post.length, `${names(post)} &rarr;`);
  setCard('cardUpcoming', up.length, 'Pre-move OK &rarr;');
  setCard('cardCaution', cau.length, `${names(cau)} &rarr;`);
  setCard('cardQualified', q.length, 'All 8 checks met &rarr;');
  const lab = (id, t) => { const b = document.getElementById(id); if (b) b.childNodes.forEach(n => { if (n.nodeType === 3 && n.textContent.trim()) n.textContent = ' ' + t + ' '; }); };
  lab('tabBtnAll', `All Stocks (${d.length})`); lab('tabBtnUpcoming', `Awaiting Results (${up.length})`);
  lab('tabBtnPost', `Results Declared (${post.length})`); lab('tabBtnCaution', `Caution / Priced In (${cau.length})`);
  lab('tabBtnQualified', `Fully Qualified (${q.length})`);
  return { post, up, cau, q };
}
function updateStatusPanel(info) {
  if (!scanMeta) return;
  const dtm = new Date(scanMeta.generatedAt);
  const set = (label, val) => { document.querySelectorAll('span.block').forEach(sp => { if (sp.innerText.trim() === label && sp.nextElementSibling) sp.nextElementSibling.innerText = val; }); };
  document.getElementById('lastScanText').innerText = dtm.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' IST';
  set('NEXT SCAN', 'hourly, weekdays');
  set('PRICE AT', (scanMeta.latestAsOf || 'latest') + ' close / delayed');
  set('MODE', 'GitHub hourly');
  let diag = document.getElementById('scanDiag');
  if (!diag) {
    diag = document.createElement('div'); diag.id = 'scanDiag'; diag.className = 'text-[10px] text-slate-500 pt-1 font-mono';
    const grid = document.getElementById('lastScanText').closest('.grid'); grid.parentElement.appendChild(diag);
  }
  const st = scanMeta.status || {};
  const age = Math.round((Date.now() - dtm) / 60000);
  diag.innerHTML = `Prices: ${esc(st.yahoo)} &middot; ${esc(st.discovered || '')} &middot; ${esc(st.sectorIdx || '')} &middot; NSE: ${esc(Object.entries(st.nse || {}).map(([k, v]) => k + ' ' + v).join(' | ') || 'no response')} &middot; ${age} min since scan${age > 180 ? ' <b class="text-rose-400">(stale)</b>' : ''}`;
}
function selectStageTab(stageName, fromUserClick = false) {
  currentFilterStage = stageName;
  const cards = { 'ALL': 'cardAll', 'Post-results': 'cardPostResults', 'Upcoming': 'cardUpcoming', 'Caution': 'cardCaution', 'Qualified': 'cardQualified' };
  Object.values(cards).forEach(id => { const el = document.getElementById(id); if (el) el.classList.remove('card-active'); });
  if (cards[stageName]) { const a = document.getElementById(cards[stageName]); if (a) a.classList.add('card-active'); }
  const stageButtons = { 'ALL': 'tabBtnAll', 'Upcoming': 'tabBtnUpcoming', 'Post-results': 'tabBtnPost', 'Caution': 'tabBtnCaution', 'Qualified': 'tabBtnQualified' };
  Object.values(stageButtons).forEach(id => { const b = document.getElementById(id); if (b) { b.classList.remove('tab-active'); b.classList.add('bg-dark-800', 'text-slate-300', 'border-dark-700'); } });
  if (stageButtons[stageName]) { const b = document.getElementById(stageButtons[stageName]); if (b) { b.classList.remove('bg-dark-800', 'text-slate-300', 'border-dark-700'); b.classList.add('tab-active'); } }
  const t = { 'ALL': 'All monitored companies', 'Upcoming': 'Awaiting earnings release (pre-move OK)', 'Post-results': 'Post-results under review', 'Caution': 'Caution / priced-in warning (run-up above 15%)', 'Qualified': 'Fully qualified PEAD candidates (all 8 checks)' }[stageName];
  document.getElementById('currentViewTitle').innerText = 'Showing: ' + t;
  filterRadarTable();
  if (fromUserClick) { const c = document.getElementById('radarTableContainer'); if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
}
function renderRadarTable(data) {
  const tbody = document.getElementById('stocksTableBody'), emptyMsg = document.getElementById('emptyViewMessage');
  tbody.innerHTML = '';
  emptyMsg.classList.toggle('hidden', data.length > 0);
  document.getElementById('currentViewCount').innerText = `${data.length} records`;
  data.forEach(item => {
    const post = item.stage === 'Post-results', ck = item.checks;
    const r = item.results;
    const accText = r ? `${sg(r.incomeYoY)} inc / ${sg(r.patYoY)} PAT YoY` : (post ? 'No data' : 'Pending Print');
    const cfSt = (ck[4].status === 'Satisfied' && ck[5].status === 'Satisfied') ? 'Satisfied' : (post ? 'Unverified' : 'Pending');
    let statusBadge = 'bg-blue-500/10 text-blue-400 border-blue-500/30';
    if (item.peadStatus.includes('PENDING')) statusBadge = 'bg-amber-500/10 text-amber-300 border-amber-500/40 font-bold';
    else if (isCaution(item)) statusBadge = 'bg-rose-500/10 text-rose-400 border-rose-500/40 font-bold';
    else if (item.peadStatus === 'QUALIFIED') statusBadge = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40 font-extrabold';
    const bad = item.scoreText.includes('Failed');
    const scoreBadge = bad ? 'bg-rose-500/10 text-rose-400 border-rose-500/30 font-bold' : (item.scoreText.startsWith('0 ') ? 'bg-dark-800 text-slate-300 border-dark-700' : 'bg-amber-500/10 text-amber-300 border-amber-500/30 font-bold');
    const tag = (txt, cls) => `<span class="text-[9px] px-1.5 py-0.2 rounded ${cls} border font-mono font-bold">${esc(txt)}</span>`;
    const tags = (item.isNew ? tag('NEW', 'bg-blue-500/20 text-blue-300 border-blue-500/30') : '')
      + (item.signal ? tag(item.signal, item.signal[0] === 'S' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : item.signal[0] === 'W' ? 'bg-rose-500/20 text-rose-300 border-rose-500/30' : 'bg-dark-800 text-slate-400 border-dark-700') : '')
      + (item.sectorTrend ? tag(item.sectorTrend.all ? 'Sector tailwind' : 'Sector ' + item.sectorTrend.above + '/4', item.sectorTrend.all ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : 'bg-dark-800 text-slate-400 border-dark-700') : '')
      + (item.preRunup > 15 ? tag('Run-up ' + sg(item.preRunup), 'bg-rose-500/20 text-rose-300 border-rose-500/30') : '');
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-dark-800/80 transition group cursor-pointer border-b border-dark-750/70';
    tr.onclick = () => openModal(item);
    tr.innerHTML = `
      <td class="py-3 px-4">
        <div class="font-bold text-white group-hover:text-emerald-400 transition flex flex-wrap items-center gap-1.5"><span>${esc(item.symbol)}</span>${tags}</div>
        <div class="text-[11px] text-slate-300 truncate max-w-[170px]">${esc(item.name)}</div>
        <div class="text-[10px] font-mono text-slate-500">${esc(item.mcap)} &middot; ${esc(item.sector)} &middot; ₹${item.price}</div>
      </td>
      <td class="py-3 px-4 font-mono text-[11px] text-slate-300 whitespace-nowrap"><div>${esc(item.resultDate)}</div><div class="text-[10px] text-slate-500">${post ? 'Declared' : 'Scheduled'}</div></td>
      <td class="py-3 px-4 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-[10px] border font-mono ${bdg(ck[0].status)}">${ck[0].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono"><span class="px-2 py-0.5 rounded text-[10px] border ${bdg(ck[1].status)}">${ck[1].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(ck[2].status)}">${accText}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(ck[3].status)}">${ck[3].status}</span></td>
      <td class="py-3 px-4 whitespace-nowrap font-mono text-[10px]"><span class="px-2 py-0.5 rounded border ${bdg(cfSt)}">${cfSt}</span></td>
      <td class="py-3 px-4 whitespace-nowrap text-center"><span class="px-2.5 py-1 rounded-full text-[10px] border ${statusBadge}">${item.peadStatus}</span></td>
      <td class="py-3 px-4 whitespace-nowrap text-center"><span class="px-2 py-0.5 rounded text-[10px] border font-mono ${scoreBadge}">${item.scoreText}</span></td>
      <td class="py-3 px-4 text-right"><button type="button" class="px-2.5 py-1 rounded bg-dark-750 hover:bg-emerald-500/20 hover:text-emerald-300 text-slate-200 text-[11px] font-semibold border border-dark-700 transition cursor-pointer">View Checklist &rarr;</button></td>`;
    tbody.appendChild(tr);
  });
}
function renderMobileCards(data) {
  const box = document.getElementById('radarTableContainer');
  let cards = document.getElementById('mobileCards');
  if (!cards) {
    cards = document.createElement('div'); cards.id = 'mobileCards'; cards.className = 'p-2 space-y-2';
    box.insertBefore(cards, document.getElementById('emptyViewMessage'));
  }
  cards.innerHTML = '';
  data.forEach(item => {
    const post = item.stage === 'Post-results';
    const sc = item.scoreText.includes('Failed') ? 'text-rose-400' : (item.scoreText.startsWith('0 ') ? 'text-slate-400' : 'text-amber-300');
    const st = isCaution(item) ? 'text-rose-400' : item.peadStatus === 'QUALIFIED' ? 'text-emerald-300' : item.peadStatus.includes('PENDING') ? 'text-amber-300' : 'text-blue-400';
    const chips = (item.isNew ? '<span class="text-[9px] px-1.5 rounded border border-blue-500/30 text-blue-300">NEW</span>' : '')
      + (item.signal ? `<span class="text-[9px] px-1.5 rounded border border-dark-700 text-slate-300">${esc(item.signal)}</span>` : '')
      + (item.sectorTrend && item.sectorTrend.all ? '<span class="text-[9px] px-1.5 rounded border border-emerald-500/30 text-emerald-300">Sector tailwind</span>' : '')
      + (item.preRunup > 15 ? `<span class="text-[9px] px-1.5 rounded border border-rose-500/30 text-rose-300">Run-up ${sg(item.preRunup)}</span>` : '');
    const d = document.createElement('div');
    d.className = 'p-3 rounded-xl bg-dark-900 border border-dark-750 cursor-pointer';
    d.addEventListener('click', () => openModal(item));
    d.innerHTML = `<div class="flex items-start justify-between gap-2"><div><div class="font-bold text-white text-sm">${esc(item.symbol)}</div><div class="text-[11px] text-slate-300">${esc(item.name)}</div></div><div class="text-right font-mono"><div class="text-white text-sm">₹${item.price}</div><div class="text-[10px] text-slate-500">${esc(item.mcap)}</div></div></div>
      <div class="flex flex-wrap gap-1 mt-1.5">${chips}</div>
      <div class="flex items-center justify-between mt-2 text-[11px] font-mono"><span class="text-slate-400">${post ? 'Declared' : 'Due'} ${esc(item.rawDate)}</span><span class="${st} font-bold">${item.peadStatus}</span></div>
      <div class="flex items-center justify-between mt-1 text-[10px] font-mono"><span class="text-slate-500">${esc(item.sector)}</span><span class="${sc}">${item.scoreText}</span></div>`;
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
  const q = document.getElementById('searchInput').value.toLowerCase();
  const order = i => (i.stage === 'Post-results' ? 0 : 1);
  const filtered = fullRadarData.filter(item => {
    let m = true;
    if (currentFilterStage === 'Post-results') m = item.stage === 'Post-results';
    else if (currentFilterStage === 'Upcoming') m = isUpcomingOk(item);
    else if (currentFilterStage === 'Caution') m = isCaution(item);
    else if (currentFilterStage === 'Qualified') m = item.peadStatus === 'QUALIFIED';
    return m && (item.symbol.toLowerCase().includes(q) || item.name.toLowerCase().includes(q) || item.sector.toLowerCase().includes(q));
  }).sort((a, b) => order(a) - order(b) || (a.stage === 'Post-results' ? b.rawDate.localeCompare(a.rawDate) : a.rawDate.localeCompare(b.rawDate)));
  renderRadarTable(filtered);
  renderMobileCards(filtered);
}
function openModal(itemOrSymbol) {
  let item = typeof itemOrSymbol === 'string' ? fullRadarData.find(s => s.symbol === itemOrSymbol) : itemOrSymbol;
  if (!item) return;
  activeModalStock = item;
  document.getElementById('mSymbol').innerText = item.symbol;
  document.getElementById('mName').innerText = item.name;
  document.getElementById('mSector').innerText = `${item.sector} · Mcap: ${item.mcap}`;
  document.getElementById('mStage').innerText = `${item.stage} (${item.resultDate})`;
  document.getElementById('mEvidence').innerText = item.evidence;
  const banner = document.getElementById('mVerdictBanner'), title = document.getElementById('mVerdictTitle'), badge = document.getElementById('mScoreBadge');
  title.innerText = item.peadStatus; badge.innerText = item.scoreText;
  const pal = item.peadStatus === 'QUALIFIED' ? ['emerald', 'emerald-400', 'emerald-300'] : item.peadStatus.includes('PENDING') ? ['amber', 'amber-300', 'amber-300'] : isCaution(item) ? ['rose', 'rose-400', 'rose-300'] : ['blue', 'blue-300', 'blue-300'];
  banner.className = `p-3.5 rounded-xl border border-${pal[0]}-500/40 bg-${pal[0]}-500/10 font-mono flex items-center justify-between`;
  title.className = `text-sm font-extrabold text-${pal[1]} mt-0.5`;
  badge.className = `px-2.5 py-1 rounded-full text-xs font-bold border border-${pal[0]}-500/40 bg-${pal[0]}-500/20 text-${pal[2]}`;
  const grid = document.getElementById('mChecklistGrid'); grid.innerHTML = '';
  item.checks.forEach((c, n) => {
    const div = document.createElement('div');
    div.className = 'p-2.5 rounded-xl bg-dark-900 border border-dark-750 flex flex-col justify-between space-y-1';
    div.innerHTML = `<div class="flex items-center justify-between"><span class="text-[10px] text-slate-400 uppercase font-semibold">Check ${n + 1}: ${esc(c.title)}</span><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${bdg(c.status)}">${c.status}</span></div><p class="text-[10px] text-slate-300 leading-snug font-sans">${esc(c.detail)}</p>`;
    if (MANUAL[n]) {
      const bar = document.createElement('div'); bar.className = 'flex gap-1.5 pt-1';
      [['Satisfied', 'Mark Satisfied'], ['Failed', 'Mark Failed'], ['', 'Clear']].forEach(([val, label]) => {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
        b.className = 'px-2 py-1 rounded-lg bg-dark-800 border border-dark-700 text-[10px] text-slate-200 cursor-pointer';
        b.addEventListener('click', ev => { ev.stopPropagation(); setConfirm(item.symbol, MANUAL[n][0], val); });
        bar.appendChild(b);
      });
      div.appendChild(bar);
    }
    grid.appendChild(div);
  });
  const tg = document.getElementById('mThesisGrid'); tg.innerHTML = '';
  item.thesis.forEach(t => {
    const d = document.createElement('div'); d.className = 'p-2 rounded-lg bg-dark-850 border border-dark-750';
    d.innerHTML = `<div class="text-[10px] text-slate-400 uppercase font-semibold">${esc(t.label)}</div><div class="text-[10px] text-slate-200 mt-0.5 leading-snug">${esc(t.val)}</div>`;
    tg.appendChild(d);
  });
  const modal = document.getElementById('stockModal'); modal.style.display = 'flex'; modal.classList.remove('hidden');
}
function closeModal() {
  const modal = document.getElementById('stockModal');
  if (modal) { modal.style.display = 'none'; modal.classList.add('hidden'); }
  activeModalStock = null;
}
function calculateTrade() {
  const portfolio = parseFloat(document.getElementById('calcPortfolio').value) || 0;
  const entry = parseFloat(document.getElementById('calcEntry').value) || 0;
  const sl = parseFloat(document.getElementById('calcSL').value) || 0;
  if (entry > sl && sl > 0) {
    const riskPerShare = entry - sl;
    const riskPct = (riskPerShare / entry) * 100;
    const maxPortfolioRiskAmt = portfolio * 0.01;
    const maxShares = Math.floor(maxPortfolioRiskAmt / riskPerShare);
    const target1 = entry + (riskPerShare * 1.5);
    const target2 = entry + (riskPerShare * 2.5);
    document.getElementById('resRiskPct').innerText = `${riskPct.toFixed(2)}%`;
    document.getElementById('resRiskPerShare').innerText = `₹${riskPerShare.toFixed(2)}`;
    document.getElementById('resMaxShares').innerText = `${maxShares} Shares`;
    document.getElementById('resTarget1').innerText = `₹${target1.toFixed(2)}`;
    document.getElementById('resTarget2').innerText = `₹${target2.toFixed(2)}`;
    if (riskPct > 5) {
      document.getElementById('resRiskPct').className = "text-base font-bold text-rose-400 mt-0.5";
    } else {
      document.getElementById('resRiskPct').className = "text-base font-bold text-emerald-400 mt-0.5";
    }
  }
}
function toggleSection(sectionId) {
  const el = document.getElementById(sectionId);
  if (!el) return;
  const isHidden = el.classList.toggle('hidden');
  if (!isHidden) {
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  if (sectionId === 'sizingSection') {
    const btn = document.getElementById('tabBtnCalculator');
    if (btn) {
      if (!isHidden) btn.classList.add('btn-toggle-calc-active');
      else btn.classList.remove('btn-toggle-calc-active');
    }
  } else if (sectionId === 'rulesSection') {
    const btn = document.getElementById('tabBtnRules');
    if (btn) {
      if (!isHidden) btn.classList.add('btn-toggle-rules-active');
      else btn.classList.remove('btn-toggle-rules-active');
    }
  }
}
function loadStockInCalculator() {
  if (!activeModalStock) return;
  const s = activeModalStock;
  closeModal();
  const calcSec = document.getElementById('sizingSection');
  if (calcSec && calcSec.classList.contains('hidden')) toggleSection('sizingSection');
  if (s.entryNum) document.getElementById('calcEntry').value = s.entryNum;
  if (s.slNum) document.getElementById('calcSL').value = s.slNum;
  calculateTrade();
  if (calcSec) calcSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
async function loadData() {
  const icon = document.getElementById('refreshIcon');
  if (icon) icon.classList.add('animate-spin');
  try {
    let res;
    try {
      res = await fetch('https://raw.githubusercontent.com/rajpurohitchethan-ai/pead-scanner/main/data.json?' + Date.now());
      if (!res.ok) throw new Error('raw fetch failed');
    } catch (e) {
      res = await fetch('data.json?' + Date.now());
    }
    scanMeta = await res.json();
    const ok = scanMeta.companies.filter(c => !c.error);
    scanMeta.latestAsOf = ok.map(c => c.asOf).sort().pop();
    fullRadarData = ok.map(mapScan);
    const info = updateCounts();
    updateStatusPanel(info);
    selectStageTab(currentFilterStage);
  } catch (e) {
    document.getElementById('currentViewTitle').innerText = 'Could not load data.json. Run the PEAD scan workflow once.';
  }
  if (icon) icon.classList.remove('animate-spin');
}
function forceScanRefresh() { loadData(); }
document.addEventListener('DOMContentLoaded', () => {
  const modal = document.getElementById('stockModal');
  if (modal) modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
window.selectStageTab = selectStageTab; window.openModal = openModal; window.closeModal = closeModal;
window.toggleSection = toggleSection; window.loadStockInCalculator = loadStockInCalculator;
window.calculateTrade = calculateTrade; window.forceScanRefresh = forceScanRefresh; window.filterRadarTable = filterRadarTable;
selectStageTab('ALL');
calculateTrade();
loadData();
setInterval(loadData, 300000);
// END OF app.js
