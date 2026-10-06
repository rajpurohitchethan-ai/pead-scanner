'use strict';

let fullRadarData = [];
let currentStageTab = 'ALL';
let activeModalStock = null;

const MIN_MCAP_CR = 1000;

function $(id) {
  return document.getElementById(id);
}

function text(v, fallback = '—') {
  return (v === null || v === undefined || v === '') ? fallback : String(v);
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function esc(v) {
  return text(v, '').replace(/[&<>'"]/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;'
  }[ch]));
}

function pick(obj, ...keys) {
  for (const key of keys) {
    const v = obj && obj[key];
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return null;
}

function boolish(v) {
  if (v === true || v === false) return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (['true','yes','pass','passed','qualified','satisfied','ok','green'].includes(s)) return true;
    if (['false','no','fail','failed','not satisfied','red'].includes(s)) return false;
  }
  return null;
}

function extractRows(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== 'object') return [];
  if (Array.isArray(payload.companies)) return payload.companies;
  if (Array.isArray(payload.stocks)) return payload.stocks;
  if (Array.isArray(payload.data)) return payload.data;
  if (payload.data && Array.isArray(payload.data.companies)) return payload.data.companies;
  if (payload.data && Array.isArray(payload.data.stocks)) return payload.data.stocks;
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

function normalizeChecks(raw, resultsReleased) {
  if (Array.isArray(raw && raw.checks) && raw.checks.length) {
    return raw.checks.map((c, i) => {
      if (typeof c === 'string') {
        return { label: c, value: null, note: '' };
      }
      return {
        label: pick(c, 'label', 'name', 'title') || `Check ${i + 1}`,
        value: boolish(pick(c, 'value', 'pass', 'passed', 'satisfied', 'status')),
        note: text(pick(c, 'note', 'detail', 'reason', 'evidence'), '')
      };
    });
  }

  return [
    {
      label: 'Results released',
      value: resultsReleased,
      note: text(pick(raw, 'resultsEvidence'), '')
    },
    {
      label: 'Market cap > ₹1,000 Cr',
      value: boolish(pick(raw, 'marketCapPass', 'mcapPass')),
      note: ''
    },
    {
      label: 'Earnings acceleration',
      value: boolish(pick(raw, 'earningsAccelerationPass', 'revenuePatPass')),
      note: text(pick(raw, 'earningsEvidence', 'revenuePatNote'), '')
    },
    {
      label: 'Earnings quality',
      value: boolish(pick(raw, 'earningsQualityPass')),
      note: text(pick(raw, 'qualityEvidence'), '')
    },
    {
      label: 'Cash flow',
      value: boolish(pick(raw, 'cashFlowPass')),
      note: text(pick(raw, 'cashFlowEvidence'), '')
    },
    {
      label: 'Surprise',
      value: boolish(pick(raw, 'surprisePass')),
      note: text(pick(raw, 'surpriseEvidence'), '')
    },
    {
      label: 'Post-result price/volume confirmation',
      value: boolish(pick(raw, 'priceVolumePass', 'technicalPass')),
      note: text(pick(raw, 'priceVolumeEvidence', 'technicalNote'), '')
    },
    {
      label: 'Liquidity',
      value: boolish(pick(raw, 'liquidityPass')),
      note: text(pick(raw, 'liquidityEvidence'), '')
    }
  ];
}

function inferView(raw, resultDate, statusText, resultsReleased) {
  const s = text(statusText, '').toLowerCase();

  if (s.includes('qualified') || s.includes('entry confirmed') || s.includes('hold')) return 'Qualified';
  if (s.includes('caution') || s.includes('priced')) return 'Caution';
  if (s.includes('upcoming') || s.includes('awaiting')) return 'Upcoming';
  if (s.includes('post') || s.includes('review') || s.includes('declared')) return 'Post-results';
  if (resultsReleased === true) return 'Post-results';

  if (resultDate) {
    const d = new Date(resultDate);
    if (!Number.isNaN(d.getTime()) && d.getTime() > Date.now()) return 'Upcoming';
  }

  return 'Upcoming';
}

function mapScan(raw, index) {
  const symbol = text(pick(raw, 'symbol', 'sym', 'ticker', 'code'), '').replace(/\.NS$/i, '');
  const resultDate = pick(raw, 'resultDate', 'result_date', 'resultsDate', 'earningsDate');
  const statusText = text(pick(raw, 'peadStatus', 'stage', 'status', 'bucket'), 'Upcoming');
  const marketCapCr = num(pick(raw, 'marketCapCr', 'mcapCr', 'market_cap_cr'));
  const resultsReleased = inferResultsReleased(raw, statusText);
  const checks = normalizeChecks(raw, resultsReleased);
  const passed = checks.filter(c => c.value === true).length;
  const sourceScore = num(pick(raw, 'score', 'scoreValue', 'peadScore'));
  const score = sourceScore === null ? passed : sourceScore;
  const view = inferView(raw, resultDate, statusText, resultsReleased);

  return Object.assign({}, raw, {
    _id: symbol || `row-${index}`,
    symbol,
    name: text(pick(raw, 'name', 'company', 'companyName'), symbol || 'Unknown'),
    sector: text(pick(raw, 'sector', 'industry'), '—'),
    earningsPeriod: text(pick(raw, 'earningsPeriod', 'quarter', 'period'), '—'),
    resultDate,
    resultsReleased,
    marketCapCr,
    marketCapPass:
      boolish(pick(raw, 'marketCapPass', 'mcapPass')) !== null
        ? boolish(pick(raw, 'marketCapPass', 'mcapPass'))
        : (marketCapCr === null ? null : marketCapCr >= MIN_MCAP_CR),
    peadStatus: statusText,
    view,
    stageView: view,
    score,
    scoreText: text(pick(raw, 'scoreText'), `${score}/${checks.length || 8}`),
    checks,
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
    evidence: text(pick(raw, 'evidence', 'resultEvidence', 'source'), '—')
  });
}

function formatMcap(v) {
  const n = num(v);
  return n === null ? 'Unverified' : `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })} Cr`;
}

function formatDate(v) {
  if (!v) return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime())
    ? text(v)
    : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

function formatDateTime(v) {
  if (!v) return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime())
    ? text(v)
    : d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function fmtPct(v) {
  const n = num(v);
  return n === null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(1)}%`;
}

function fmtX(v) {
  const n = num(v);
  return n === null ? '—' : `${n.toFixed(2)}x`;
}

function fmtPrice(v) {
  const n = num(v);
  return n === null ? '—' : `₹${n.toFixed(2)}`;
}

function checkBadge(value, yes = 'Satisfied', no = 'Not satisfied') {
  if (value === true) {
    return `<span class="px-2 py-1 rounded text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/20">${esc(yes)}</span>`;
  }
  if (value === false) {
    return `<span class="px-2 py-1 rounded text-xs bg-red-500/10 text-red-300 border border-red-500/20">${esc(no)}</span>`;
  }
  return '<span class="px-2 py-1 rounded text-xs bg-slate-500/10 text-slate-300 border border-slate-500/20">Unverified</span>';
}

function gateBadge(value) {
  if (value === true) return '<span class="text-emerald-400 font-bold">✓ PASS</span>';
  if (value === false) return '<span class="text-rose-400 font-bold">✕ FAIL</span>';
  return '<span class="text-amber-400 font-bold">? PENDING</span>';
}

function findCheck(item, terms) {
  const lc = terms.map(x => x.toLowerCase());
  return item.checks.find(c => lc.some(t => c.label.toLowerCase().includes(t))) || { value: null, note: '' };
}

function setCardCount(cardId, count) {
  const card = $(cardId);
  if (!card) return;

  const target =
    card.querySelector('[data-count]') ||
    card.querySelector('.text-2xl') ||
    card.querySelector('.text-3xl') ||
    card.querySelector('.text-4xl');

  if (target) target.textContent = String(count);
}

function updateCounts() {
  const all = fullRadarData.length;
  const upcoming = fullRadarData.filter(x => x.resultsReleased !== true && x.view !== 'Caution').length;
  const inReview = fullRadarData.filter(x => x.view === 'Post-results').length;
  const declared = fullRadarData.filter(x => x.resultsReleased === true).length;
  const caution = fullRadarData.filter(x => x.view === 'Caution').length;
  const qualified = fullRadarData.filter(x => x.view === 'Qualified').length;

  setCardCount('cardAll', all);
  setCardCount('cardPostResults', inReview);
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
    ALL: 'tabBtnAll',
    Upcoming: 'tabBtnUpcoming',
    'Post-results': 'tabBtnPost',
    Caution: 'tabBtnCaution',
    Qualified: 'tabBtnQualified'
  };

  Object.entries(ids).forEach(([tab, id]) => {
    const el = $(id);
    if (el) el.classList.toggle('tab-active', currentStageTab === tab);
  });

  const cards = {
    ALL: 'cardAll',
    'Post-results': 'cardPostResults',
    Upcoming: 'cardUpcoming',
    Caution: 'cardCaution',
    Qualified: 'cardQualified'
  };

  Object.entries(cards).forEach(([tab, id]) => {
    const el = $(id);
    if (el) el.classList.toggle('card-active', currentStageTab === tab);
  });
}

function currentRows() {
  const input = $('searchInput');
  const q = text(input ? input.value : '', '').trim().toLowerCase();

  return fullRadarData.filter(item => {
    let tabOk = true;

    if (currentStageTab === 'Upcoming') {
      tabOk = item.resultsReleased !== true && item.view !== 'Caution';
    } else if (currentStageTab === 'Post-results') {
      tabOk = item.resultsReleased === true;
    } else if (currentStageTab !== 'ALL') {
      tabOk = item.view === currentStageTab;
    }

    const searchOk =
      !q ||
      [item.symbol, item.name, item.sector, item.peadStatus, item.candidateStatus]
        .join(' ')
        .toLowerCase()
        .includes(q);

    return tabOk && searchOk;
  });
}

function qualificationReason(item) {
  const failed = item.checks.filter(c => c.value === false).map(c => c.label);
  const pending = item.checks.filter(c => c.value === null).map(c => c.label);

  if (item.pricedIn === true || item.view === 'Caution') {
    return item.preResultRunupPct === null
      ? 'CAUTION / PRICED IN: pre-result move is extended.'
      : `CAUTION / PRICED IN: pre-result run-up ${item.preResultRunupPct.toFixed(1)}% exceeded the configured threshold.`;
  }

  if (item.resultsReleased !== true) {
    return 'AWAITING RESULTS: official result filing has not been confirmed.';
  }

  if (failed.length) return `NOT QUALIFIED: failed ${failed.join(', ')}.`;
  if (pending.length) return `IN REVIEW: waiting for ${pending.join(', ')}.`;

  if (item.checks.length && item.checks.every(c => c.value === true)) {
    return item.entryTriggerPass === true
      ? 'QUALIFIED: all 8 gates passed and entry trigger fired.'
      : 'QUALIFIED: all 8 gates passed; waiting for entry trigger.';
  }

  if (item.qualificationError) return `QUALIFICATION ERROR: ${item.qualificationError}`;
  return 'Post-result review in progress.';
}

function postResultDetailRow(item) {
  if (item.resultsReleased !== true) return '';

  const gates = item.checks.map(c => `
    <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
      <div class="text-[10px] uppercase tracking-wide text-slate-400">${esc(c.label)}</div>
      <div class="mt-1">${gateBadge(c.value)}</div>
      ${c.note ? `<div class="text-[10px] text-slate-500 mt-1">${esc(c.note)}</div>` : ''}
    </div>
  `).join('');

  const sectorText =
    item.sectorTailwind === true
      ? 'TAILWIND'
      : item.sectorTailwind === false
        ? 'NO TAILWIND'
        : 'UNVERIFIED';

  return `
    <tr class="bg-dark-950/70 border-b border-dark-750/70">
      <td colspan="10" class="px-4 pb-5 pt-2">
        <div class="rounded-2xl border border-dark-750 bg-dark-900/70 p-4 space-y-4">

          <div class="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div class="text-xs uppercase tracking-wider text-slate-500">Post-result PEAD review</div>
              <div class="font-bold text-white mt-1">${esc(item.symbol)} · ${esc(item.name)}</div>
            </div>
            <div class="font-mono text-sm font-bold text-emerald-300">${esc(item.scoreText)} · ${esc(item.view)}</div>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
            ${gates}
          </div>

          <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2 text-[11px]">
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Revenue YoY</div><div class="font-bold text-white">${fmtPct(item.revenueYoY)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">PAT YoY</div><div class="font-bold text-white">${fmtPct(item.patYoY)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">PAT QoQ</div><div class="font-bold text-white">${fmtPct(item.patQoQ)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">RVOL</div><div class="font-bold text-white">${fmtX(item.relativeVolume)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Sector</div><div class="font-bold text-white">${sectorText}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Pre-result run-up</div><div class="font-bold text-white">${fmtPct(item.preResultRunupPct)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Result-day move</div><div class="font-bold text-white">${fmtPct(item.resultDayReturnPct)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Entry</div><div class="font-bold text-emerald-400">${fmtPrice(item.entry)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">SL</div><div class="font-bold text-rose-400">${fmtPrice(item.sl)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">TSL</div><div class="font-bold text-amber-400">${fmtPrice(item.tsl)}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Candidate</div><div class="font-bold text-white">${esc(item.candidateStatus || '—')}</div></div>
            <div class="p-2 rounded-lg bg-dark-850 border border-dark-750"><div class="text-slate-500">Allocation</div><div class="font-bold text-white">${item.allocationPct === null ? '—' : `${item.allocationPct}%`}</div></div>
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

  const live =
    item.liveStatus === 'ok'
      ? `<span class="text-xs text-emerald-300">Live ${fmtPrice(item.price)}${item.changePct === null ? '' : ` (${item.changePct > 0 ? '+' : ''}${item.changePct}%)`}</span>`
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

      <td class="py-3 px-4 align-top">
        <span class="font-mono font-semibold">${esc(item.scoreText)}</span>
      </td>

      <td class="py-3 px-4 align-top">
        <button
          type="button"
          class="px-3 py-1.5 rounded-md text-xs border border-white/10 hover:bg-white/5"
          data-symbol="${esc(item.symbol)}"
          onclick="openModal(this.dataset.symbol)"
        >
          View checklist
        </button>
      </td>
    </tr>
  `;

  return main + (
    currentStageTab === 'Post-results' && item.resultsReleased === true
      ? postResultDetailRow(item)
      : ''
  );
}

function renderTable() {
  const tbody = $('stocksTableBody');
  if (!tbody) return;
                               
