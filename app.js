/* PEAD Qualification Radar — resilient data loader/render layer.
 *
 * Fixes the zero-stock problem by:
 * 1) loading data.json on page load and manual refresh,
 * 2) accepting both {companies:[...]} and {stocks:[...]} output shapes,
 * 3) accepting both sym/resultDate and symbol/result_date row keys,
 * 4) restoring the global functions referenced by index.html.
 */

'use strict';

let fullRadarData = [];
let currentStageTab = 'ALL';
let activeModalStock = null;
let lastPayload = null;

const MIN_MCAP_CR = 1000;

function $(id) { return document.getElementById(id); }
function text(v, fallback = '—') { return (v === null || v === undefined || v === '') ? fallback : String(v); }
function num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
function esc(v) {
  return text(v, '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function pick(obj, ...keys) {
  for (const k of keys) {
    const v = obj?.[k];
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return null;
}
function boolish(v) {
  if (v === true || v === false) return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (['yes','true','pass','passed','satisfied','qualified','ok','green'].includes(s)) return true;
    if (['no','false','fail','failed','not satisfied','red'].includes(s)) return false;
  }
  return null;
}

function extractRows(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== 'object') return [];
  if (Array.isArray(payload.companies)) return payload.companies;
  if (Array.isArray(payload.stocks)) return payload.stocks;
  if (Array.isArray(payload.data)) return payload.data;
  if (Array.isArray(payload.data?.companies)) return payload.data.companies;
  if (Array.isArray(payload.data?.stocks)) return payload.data.stocks;
  return [];
}

function inferResultsReleased(raw, statusText) {
  const explicit = boolish(pick(raw, 'resultsReleased', 'resultReleased', 'results_declared'));
  if (explicit !== null) return explicit;

  const source = text(pick(raw, 'discoverySource', 'source'), '').toLowerCase();
  if (source.includes('financial results')) return true;

  const s = text(statusText, '').toLowerCase();
  return (
    s.includes('post-results') ||
    s.includes('post results') ||
    s.includes('results declared') ||
    s.includes('in review') ||
    s.includes('qualified')
  );
}

function normalizeChecks(raw) {
  if (Array.isArray(raw?.checks)) {
    return raw.checks.map((c, i) => {
      if (typeof c === 'string') return { label: c, value: null, note: '' };
      return {
        label: pick(c, 'label', 'name', 'title') || `Check ${i + 1}`,
        value: boolish(pick(c, 'value', 'pass', 'passed', 'satisfied', 'status')),
        note: pick(c, 'note', 'detail', 'reason', 'evidence') || ''
      };
    });
  }

  return [
    { label: 'Market cap > ₹1,000 Cr', value: boolish(pick(raw, 'marketCapPass', 'mcapPass')), note: '' },
    { label: 'Revenue / PAT acceleration', value: boolish(pick(raw, 'revenuePatPass', 'revenueAcceleration', 'earningsAcceleration')), note: text(pick(raw, 'revenuePatNote', 'revenueGrowth', 'patGrowth'), '') },
    { label: 'Earnings quality', value: boolish(pick(raw, 'earningsQualityPass', 'earningsQuality')), note: text(pick(raw, 'earningsQualityNote'), '') },
    { label: 'Cash flow / surprise', value: boolish(pick(raw, 'cashFlowPass', 'surprisePass', 'cashFlowSurprise')), note: text(pick(raw, 'cashFlowNote', 'surpriseNote'), '') },
    { label: 'Price / volume confirmation', value: boolish(pick(raw, 'priceVolumePass', 'technicalPass', 'priceConfirmation')), note: text(pick(raw, 'technicalNote'), '') },
    { label: 'Sector tailwind', value: boolish(pick(raw, 'sectorTailwind', 'sectorPass')), note: text(pick(raw, 'sectorNote'), '') },
    { label: 'Entry trigger defined', value: boolish(pick(raw, 'entryTriggerPass', 'entryConfirmed')), note: text(pick(raw, 'entry', 'entryPrice'), '') },
    { label: 'Invalidation / stop defined', value: pick(raw, 'sl', 'stopLoss', 'invalidation') != null ? true : null, note: text(pick(raw, 'sl', 'stopLoss', 'invalidation'), '') },
  ];
}

function inferView(raw, resultDate, statusText) {
  const s = statusText.toLowerCase();
  if (s.includes('qualified') || s.includes('entry confirmed') || s.includes('hold')) return 'Qualified';
  if (s.includes('caution') || s.includes('priced')) return 'Caution';
  if (s.includes('upcoming') || s.includes('awaiting')) return 'Upcoming';
  if (s.includes('post') || s.includes('review') || s.includes('declared')) return 'Post-results';

  if (resultDate) {
    const d = new Date(resultDate);
    if (!Number.isNaN(d.getTime())) return d.getTime() > Date.now() ? 'Upcoming' : 'Post-results';
  }
  return 'Post-results';
}

function mapScan(raw, index = 0) {
  const symbol = text(pick(raw, 'symbol', 'sym', 'ticker', 'code'), '').replace(/\.NS$/i, '');
  const resultDate = pick(raw, 'resultDate', 'result_date', 'resultsDate', 'earningsDate');
  const statusText = text(pick(raw, 'peadStatus', 'stage', 'status', 'bucket'), 'In Review');
  const marketCapCr = num(pick(raw, 'marketCapCr', 'mcapCr', 'market_cap_cr'));
  const resultsReleased = inferResultsReleased(raw, statusText);
  const checks = normalizeChecks({...raw, resultsReleased});
  const passedChecks = checks.filter(c => c.value === true).length;
  const knownChecks = checks.filter(c => c.value !== null).length;
  const sourceScore = num(pick(raw, 'score', 'scoreValue', 'peadScore'));
  const score = sourceScore ?? passedChecks;
  const view = inferView(raw, resultDate, statusText);

  return {
    ...raw,
    _id: symbol || `row-${index}`,
    symbol,
    name: text(pick(raw, 'name', 'company', 'companyName'), symbol || 'Unknown'),
    sector: text(pick(raw, 'sector', 'industry'), '—'),
    earningsPeriod: text(pick(raw, 'earningsPeriod', 'quarter', 'period'), '—'),
    resultDate,
    resultsReleased,
    marketCapCr,
    marketCapPass: boolish(pick(raw, 'marketCapPass', 'mcapPass')) ?? (marketCapCr == null ? null : marketCapCr >= MIN_MCAP_CR),
    peadStatus: statusText,
    view,
    score,
    scoreText: text(pick(raw, 'scoreText'), `${score}/${checks.length || 8}`),
    checks,
    knownChecks,
    evidence: text(pick(raw, 'evidence', 'resultEvidence', 'source'), '—'),
    thesis: pick(raw, 'thesis', 'thesisItems', 'notes') || [],
    liveStatus: text(pick(raw, 'liveStatus'), '—'),
    liveError: pick(raw, 'liveError'),
    price: num(pick(raw, 'price', 'lastPrice', 'ltp')),
    changePct: num(pick(raw, 'changePct', 'change_percent')),
    priceTimestamp: pick(raw, 'priceTimestamp', 'quoteTimestamp', 'lastUpdateTime'),
    revenueYoY: num(pick(raw, 'revenueYoY')),
    patYoY: num(pick(raw, 'patYoY')),
    patQoQ: num(pick(raw, 'patQoQ')),
    relativeVolume: num(pick(raw, 'relativeVolume', 'rvol')),
    avgTurnover20dCr: num(pick(raw, 'avgTurnover20dCr')),
    sectorTailwind: boolish(pick(raw, 'sectorTailwind', 'sectorPass')),
    preResultRunupPct: num(pick(raw, 'preResultRunupPct')),
    resultDayReturnPct: num(pick(raw, 'resultDayReturnPct')),
    pricedIn: boolish(pick(raw, 'pricedIn')),
    candidateStatus: pick(raw, 'candidateStatus'),
    allocationPct: num(pick(raw, 'allocationPct')),
    entryTriggerPass: boolish(pick(raw, 'entryTriggerPass')),
    entry: pick(raw, 'entry', 'entryPrice'),
    sl: pick(raw, 'sl', 'stopLoss'),
    tsl: pick(raw, 'tsl', 'trailingStopLoss'),
  };
}

function formatMcap(v) {
  const n = num(v);
  if (n == null) return 'Unverified';
  return `₹${n.toLocaleString('en-IN', {maximumFractionDigits: 0})} Cr`;
}
function formatDate(v) {
  if (!v) return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? text(v) : d.toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'});
}
function checkBadge(value, labelTrue = 'Satisfied', labelFalse = 'Not satisfied') {
  if (value === true) return `<span class="px-2 py-1 rounded text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/20">${labelTrue}</span>`;
  if (value === false) return `<span class="px-2 py-1 rounded text-xs bg-red-500/10 text-red-300 border border-red-500/20">${labelFalse}</span>`;
  return '<span class="px-2 py-1 rounded text-xs bg-slate-500/10 text-slate-300 border border-slate-500/20">Unverified</span>';
}
function findCheck(item, terms) {
  const lc = terms.map(x => x.toLowerCase());
  return item.checks.find(c => lc.some(t => c.label.toLowerCase().includes(t))) || { value: null, note: '' };
}

function setCardCount(cardId, count) {
  const card = $(cardId);
  if (!card) return;
  const target = card.querySelector('.text-2xl') || card.querySelector('[data-count]');
  if (target) target.textContent = String(count);
}

function updateCounts() {
  const all = fullRadarData.length;
  const upcoming = fullRadarData.filter(x => x.resultsReleased !== true && x.view !== 'Caution').length;
  const postReview = fullRadarData.filter(x => x.view === 'Post-results').length;
  const declared = fullRadarData.filter(x => x.resultsReleased === true).length;
  const caution = fullRadarData.filter(x => x.view === 'Caution').length;
  const qualified = fullRadarData.filter(x => x.view === 'Qualified').length;

  setCardCount('cardAll', all);
  setCardCount('cardPostResults', postReview);
  setCardCount('cardUpcoming', upcoming);
  setCardCount('cardCaution', caution);
  setCardCount('cardQualified', qualified);

  const labels = {
    tabBtnAll: `All Stocks (${all})`,
    tabBtnUpcoming: `Awaiting Results (${upcoming})`,
    tabBtnPost: `Results Declared (${declared})`,
    tabBtnCaution: `Caution / Priced In (${caution})`,
    tabBtnQualified: `Fully Qualified (${qualified})`
  };

  Object.entries(labels).forEach(([id, label]) => {
    const el = $(id);
    if (el) el.textContent = label;
  });
}

function setTabClasses() {
  const ids = {
    'ALL': 'tabBtnAll',
    'Upcoming': 'tabBtnUpcoming',
    'Post-results': 'tabBtnPost',
    'Caution': 'tabBtnCaution',
    'Qualified': 'tabBtnQualified'
  };
  Object.entries(ids).forEach(([tab, id]) => {
    const el = $(id);
    if (!el) return;
    el.classList.toggle('tab-active', currentStageTab === tab);
  });

  const cards = {
    'ALL': 'cardAll',
    'Post-results': 'cardPostResults',
    'Upcoming': 'cardUpcoming',
    'Caution': 'cardCaution',
    'Qualified': 'cardQualified'
  };
  Object.entries(cards).forEach(([tab, id]) => {
    const el = $(id);
    if (el) el.classList.toggle('card-active', currentStageTab === tab);
  });
}

function currentRows() {
  const q = ($('searchInput')?.value || '').trim().toLowerCase();

  return fullRadarData.filter(item => {
    let tabOk = true;

    if (currentStageTab === 'Upcoming') {
      tabOk = item.resultsReleased !== true && item.view !== 'Caution';
    } else if (currentStageTab === 'Post-results') {
      // Results Declared tab intentionally includes post-result, caution and qualified names.
      tabOk = item.resultsReleased === true;
    } else if (currentStageTab !== 'ALL') {
      tabOk = item.view === currentStageTab;
    }

    const searchOk = !q || [
      item.symbol,
      item.name,
      item.sector,
      item.peadStatus,
      item.candidateStatus
    ].join(' ').toLowerCase().includes(q);

    return tabOk && searchOk;
  });
}


function fmtPct(v) {
  const n = num(v);
  return n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(1)}%`;
}

function fmtX(v) {
  const n = num(v);
  return n == null ? '—' : `${n.toFixed(2)}x`;
}

function fmtPrice(v) {
  const n = num(v);
  return n == null ? '—' : `₹${n.toFixed(2)}`;
}

function gateBadge(value) {
  if (value === true) return '<span class="text-emerald-400 font-bold">✓ PASS</span>';
  if (value === false) return '<span class="text-rose-400 font-bold">✕ FAIL</span>';
  return '<span class="text-amber-400 font-bold">? PENDING</span>';
}

function qualificationReason(item) {
  const failed = item.checks.filter(c => c.value === false).map(c => c.label);
  const pending = item.checks.filter(c => c.value === null).map(c => c.label);

  if (item.pricedIn === true || item.view === 'Caution') {
    const runup = num(item.preResultRunupPct);
    return runup == null
      ? 'CAUTION / PRICED IN: pre-result move is flagged as extended.'
      : `CAUTION / PRICED IN: pre-result run-up ${runup.toFixed(1)}% exceeded the configured threshold.`;
  }

  if (item.resultsReleased !== true) {
    return 'AWAITING RESULTS: official result filing has not been confirmed yet.';
  }

  if (failed.length) return `NOT QUALIFIED: failed ${failed.join(', ')}.`;
  if (pending.length) return `IN REVIEW: waiting for ${pending.join(', ')}.`;

  if (item.checks.length && item.checks.every(c => c.value === true)) {
    return item.entryTriggerPass === true
      ? 'QUALIFIED: all 8 PEAD gates passed and the planned entry trigger has fired.'
      : 'QUALIFIED: all 8 PEAD gates passed. Waiting for the planned entry trigger.';
  }

  if (item.qualificationError) return `QUALIFICATION ERROR: ${item.qualificationError}`;
  return 'Post-result review is complete, but the stock has not been marked qualified.';
}

function postResultDetailRow(item) {
  if (item.resultsReleased !== true) return '';

  const sectorText = item.sectorTailwind === true
    ? 'TAILWIND'
    : item.sectorTailwind === false
      ? 'NO TAILWIND'
      : 'UNVERIFIED';

  const sectorClass = item.sectorTailwind === true
    ? 'text-emerald-400'
    : item.sectorTailwind === false
      ? 'text-rose-400'
      : 'text-slate-400';

  const gates = item.checks.map(c => `
    <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
      <div class="text-[10px] uppercase tracking-wide text-slate-400">${esc(c.label)}</div>
      <div class="mt-1">${gateBadge(c.value)}</div>
      ${c.note ? `<div class="text-[10px] text-slate-500 mt-1">${esc(c.note)}</div>` : ''}
    </div>
  `).join('');

  return `
    <tr class="bg-dark-950/70 border-b border-dark-750/70">
      <td colspan="10" class="px-4 pb-5 pt-2">
        <div class="rounded-2xl border border-dark-750 bg-dark-900/70 p-4 space-y-4">

          <div class="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div class="text-xs uppercase tracking-wider text-slate-500">Post-result PEAD review</div>
              <div class="font-bold text-white mt-1">${esc(item.symbol)} · ${esc(item.name)}</div>
            </div>
            <div class="font-mono text-sm font-bold ${item.view === 'Qualified' ? 'text-emerald-400' : item.view === 'Caution' ? 'text-rose-400' : 'text-amber-400'}">
              ${esc(item.scoreText)} · ${esc(item.view)}
            </div>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
            ${gates}
          </div>

          <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2 text-[11px]">
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Revenue YoY</div><div class="font-bold text-white">${fmtPct(item.revenueYoY)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">PAT YoY</div><div class="font-bold text-white">${fmtPct(item.patYoY)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">PAT QoQ</div><div class="font-bold text-white">${fmtPct(item.patQoQ)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">RVOL</div><div class="font-bold text-white">${fmtX(item.relativeVolume)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Sector Tailwind</div><div class="font-bold ${sectorClass}">${sectorText}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">20D Turnover</div><div class="font-bold text-white">${item.avgTurnover20dCr == null ? '—' : `₹${Number(item.avgTurnover20dCr).toFixed(1)} Cr`}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Pre-result Run-up</div><div class="font-bold text-white">${fmtPct(item.preResultRunupPct)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Result-day Move</div><div class="font-bold text-white">${fmtPct(item.resultDayReturnPct)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Entry</div><div class="font-bold text-emerald-400">${fmtPrice(item.entry)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Stop Loss</div><div class="font-bold text-rose-400">${fmtPrice(item.sl)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">TSL</div><div class="font-bold text-amber-400">${fmtPrice(item.tsl)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Candidate</div><div class="font-bold text-white">${esc(item.candidateStatus || '—')}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Model Allocation</div><div class="font-bold text-white">${item.allocationPct == null ? '—' : `${esc(item.allocationPct)}%`}</div></div>
          </div>

          <div class="p-3 rounded-xl border border-dark-750 bg-dark-850">
            <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Verdict / Reason</div>
            <div class="text-xs font-semibold text-slate-200">${esc(qualificationReason(item))}</div>
          </div>
        </div>
      </td>
    </tr>
  `;
}

function rowHtml(item) {
  const earnings = findCheck(item, ['earnings acceleration', 'revenue', 'pat']);
  const quality = findCheck(item, ['earnings quality']);
  const cash = findCheck(item, ['cash flow']);
  const surprise = findCheck(item, ['surprise']);

  const live = item.liveStatus === 'ok'
    ? `<span class="text-xs text-emerald-300">Live ₹${item.price ?? '—'}${item.changePct == null ? '' : ` (${item.changePct > 0 ? '+' : ''}${item.changePct}%)`}</span>`
    : `<span class="text-xs text-amber-300" title="${esc(item.liveError || '')}">${item.liveStatus === 'unavailable' ? 'Quote unavailable' : esc(item.liveStatus)}</span>`;

  const main = `
    <tr class="hover:bg-white/[0.025] transition-colors">
      <td class="py-3 px-4 align-top">
        <div class="font-semibold text-white">${esc(item.symbol || item.name)}</div>
        <div class="text-xs text-slate-400">${esc(item.name)}</div>
        <div class="text-xs text-slate-500 mt-1">${esc(formatMcap(item.marketCapCr))}</div>
        ${live}
      </td>

      <td class="py-3 px-4 align-top">
        <div class="text-sm text-slate-200">${esc(item.earningsPeriod)}</div>
        <div class="text-xs text-slate-400 mt-1">${esc(item.peadStatus)}</div>
      </td>

      <td class="py-3 px-4 align-top">
        ${checkBadge(item.resultsReleased, 'Declared', 'Awaiting')}
        <div class="text-[10px] text-slate-500 mt-1">${esc(formatDate(item.resultDate))}</div>
      </td>

      <td class="py-3 px-4 align-top">${checkBadge(item.marketCapPass, 'Yes', 'No')}</td>
      <td class="py-3 px-4 align-top">${checkBadge(earnings.value)}</td>
      <td class="py-3 px-4 align-top">${checkBadge(quality.value)}</td>

      <td class="py-3 px-4 align-top">
        <div>CF ${checkBadge(cash.value)}</div>
        <div class="mt-1">SURP ${checkBadge(surprise.value)}</div>
      </td>

      <td class="py-3 px-4 align-top">
        <span class="px-2 py-1 rounded text-xs border border-white/10">${esc(item.peadStatus)}</span>
      </td>

      <td class="py-3 px-4 alig
