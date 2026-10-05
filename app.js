'use strict';

const MIN_MCAP_CR = 1000;

let allStocks = [];
let currentStageTab = 'ALL';
let activeModalStock = null;

const $ = id => document.getElementById(id);

function num(v) {
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}

function pick(obj, ...keys) {
  for (const key of keys) {
    if (
      obj &&
      obj[key] !== undefined &&
      obj[key] !== null &&
      obj[key] !== ''
    ) {
      return obj[key];
    }
  }

  return null;
}

function boolValue(v) {
  if (v === true || v === false) return v;

  if (typeof v === 'number') return v !== 0;

  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();

    if (
      [
        'true',
        'yes',
        'pass',
        'passed',
        'qualified',
        'satisfied',
        'ok',
        'green'
      ].includes(s)
    ) {
      return true;
    }

    if (
      [
        'false',
        'no',
        'fail',
        'failed',
        'not satisfied',
        'red'
      ].includes(s)
    ) {
      return false;
    }
  }

  return null;
}

function esc(v) {
  return String(v ?? '').replace(
    /[&<>"']/g,
    ch =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      })[ch]
  );
}

function parseDate(v) {
  if (!v) return null;

  const d = new Date(v);

  return Number.isNaN(d.getTime())
    ? null
    : d;
}

function formatDate(v) {
  const d = parseDate(v);

  if (!d) {
    return v
      ? String(v)
      : '—';
  }

  return d.toLocaleDateString(
    'en-IN',
    {
      day: '2-digit',
      month: 'short',
      year: 'numeric'
    }
  );
}

function formatDateTime(v) {
  const d = parseDate(v);

  if (!d) {
    return 'unavailable';
  }

  return d.toLocaleString(
    'en-IN',
    {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit'
    }
  );
}


/* --------------------------------------------------
   DATA
-------------------------------------------------- */

function extractStocks(payload) {
  if (Array.isArray(payload)) {
    return payload;
  }

  if (!payload) {
    return [];
  }

  if (Array.isArray(payload.companies)) {
    return payload.companies;
  }

  if (Array.isArray(payload.stocks)) {
    return payload.stocks;
  }

  if (Array.isArray(payload.data)) {
    return payload.data;
  }

  if (
    payload.data &&
    Array.isArray(
      payload.data.companies
    )
  ) {
    return payload.data.companies;
  }

  return [];
}


function inferStage(row) {
  const raw = String(
    pick(
      row,
      'view',
      'peadStatus',
      'bucket',
      'stage',
      'status'
    ) ?? ''
  ).toLowerCase();

  if (raw.includes('qualified')) {
    return 'Qualified';
  }

  if (
    raw.includes('caution') ||
    raw.includes('priced')
  ) {
    return 'Caution';
  }

  if (
    raw.includes('upcoming') ||
    raw.includes('awaiting')
  ) {
    return 'Upcoming';
  }

  if (
    raw.includes('post') ||
    raw.includes('review') ||
    raw.includes('declared')
  ) {
    return 'Post-results';
  }

  const resultDate =
    parseDate(
      pick(
        row,
        'resultDate',
        'result_date',
        'resultsDate'
      )
    );

  if (resultDate) {
    const today =
      new Date();

    today.setHours(
      0,
      0,
      0,
      0
    );

    return resultDate >= today
      ? 'Upcoming'
      : 'Post-results';
  }

  return 'Post-results';
}


function criterion(
  row,
  keys
) {
  return boolValue(
    pick(
      row,
      ...keys
    )
  );
}


function normalizeRow(
  row,
  index
) {

  const symbol =
    String(
      pick(
        row,
        'symbol',
        'sym',
        'ticker',
        'code'
      ) ?? ''
    )
      .replace(
        /\.NS$/i,
        ''
      )
      .trim();

  const marketCapCr =
    num(
      pick(
        row,
        'marketCapCr',
        'mcapCr',
        'market_cap_cr'
      )
    );

  const resultDate =
    pick(
      row,
      'resultDate',
      'result_date',
      'resultsDate'
    );

  const revenue =
    criterion(
      row,
      [
        'revenuePatPass',
        'revenueAcceleration',
        'earningsAcceleration',
        'revPatPass'
      ]
    );

  const quality =
    criterion(
      row,
      [
        'earningsQualityPass',
        'earningsQuality',
        'qualityPass'
      ]
    );

  const cash =
    criterion(
      row,
      [
        'cashFlowPass',
        'surprisePass',
        'cashFlowSurprise'
      ]
    );

  const technical =
    criterion(
      row,
      [
        'priceVolumePass',
        'technicalPass',
        'priceConfirmation'
      ]
    );

  const sectorTailwind =
    criterion(
      row,
      [
        'sectorTailwind',
        'sectorPass'
      ]
    );

  const entryTrigger =
    criterion(
      row,
      [
        'entryTriggerPass',
        'entryConfirmed'
      ]
    );

  const stopDefined =
    pick(
      row,
      'sl',
      'stopLoss'
    ) != null
      ? true
      : null;

  const marketCapPass =
    boolValue(
      pick(
        row,
        'marketCapPass',
        'mcapPass'
      )
    ) ??
    (
      marketCapCr == null
        ? null
        : marketCapCr >=
          MIN_MCAP_CR
    );

  const gates = [
    marketCapPass,
    revenue,
    quality,
    cash,
    technical,
    sectorTailwind,
    entryTrigger,
    stopDefined
  ];

  const passed =
    gates.filter(
      x => x === true
    ).length;

  const explicitScore =
    num(
      pick(
        row,
        'score',
        'scoreValue',
        'peadScore'
      )
    );

  const score =
    explicitScore ?? passed;

  return {
    ...row,

    _id:
      symbol ||
      `row-${index}`,

    symbol,

    name:
      String(
        pick(
          row,
          'name',
          'company',
          'companyName'
        ) ??
        symbol
      ),

    sector:
      String(
        pick(
          row,
          'sector',
          'industry'
        ) ??
        '—'
      ),

    quarter:
      String(
        pick(
          row,
          'quarter',
          'earningsPeriod',
          'period'
        ) ??
        '—'
      ),

    resultDate,

    marketCapCr,

    marketCapPass,

    stageView:
      inferStage(row),

    statusText:
      String(
        pick(
          row,
          'bucket',
          'peadStatus',
          'stage',
          'status'
        ) ??
        'In Review'
      ),

    revenue,
    quality,
    cash,
    technical,
    sectorTailwind,
    entryTrigger,
    stopDefined,

    score,

    scoreText:
      String(
        pick(
          row,
          'scoreText'
        ) ??
        `${score}/8`
      ),

    price:
      num(
        pick(
          row,
          'price',
          'lastPrice',
          'ltp'
        )
      ),

    previousClose:
      num(
        pick(
          row,
          'previousClose'
        )
      ),

    changePct:
      num(
        pick(
          row,
          'changePct'
        )
      ),

    liveStatus:
      String(
        pick(
          row,
          'liveStatus'
        ) ??
        ''
      ),

    liveError:
      pick(
        row,
        'liveError'
      ),

    priceTimestamp:
      pick(
        row,
        'priceTimestamp',
        'marketTime'
      ),

    entry:
      pick(
        row,
        'entry',
        'entryPrice'
      ),

    sl:
      pick(
        row,
        'sl',
        'stopLoss'
      ),

    tsl:
      pick(
        row,
        'tsl',
        'trailingStopLoss'
      ),

    note:
      String(
        pick(
          row,
          'note',
          'evidence'
        ) ??
        ''
      )
  };
}


/* --------------------------------------------------
   HEADER
-------------------------------------------------- */

function setStatusByLabel(
  label,
  value
) {

  const wanted =
    label.toUpperCase();

  const labels =
    [
      ...document.querySelectorAll(
        'span'
      )
    ];

  const labelEl =
    labels.find(
      el =>
        el.textContent
          .trim()
          .toUpperCase() ===
        wanted
    );

  if (
    !labelEl ||
    !labelEl.parentElement
  ) {
    return;
  }

  const spans =
    [
      ...labelEl
        .parentElement
        .querySelectorAll(
          'span'
        )
    ];

  const valueEl =
    spans.find(
      el =>
        el !== labelEl
    );

  if (valueEl) {
    valueEl.textContent =
      value;
  }
}


function updateHeader(
  payload
) {

  const generatedAt =
    pick(
      payload,
      'generatedAt',
      'last_scan',
      'lastScanAt'
    );

  setStatusByLabel(
    'LAST SCAN',
    formatDateTime(
      generatedAt
    )
  );

  setStatusByLabel(
    'NEXT SCAN',
    'Hourly, weekdays'
  );

  setStatusByLabel(
    'MODE',
    'GitHub hourly'
  );

  const timestamps =
    allStocks
      .map(
        s =>
          parseDate(
            s.priceTimestamp
          )
      )
      .filter(Boolean)
      .sort(
        (a, b) =>
          b - a
      );

  setStatusByLabel(
    'PRICE AT',
    timestamps.length
      ? timestamps[0]
          .toLocaleTimeString(
            'en-IN',
            {
              hour:
                '2-digit',
              minute:
                '2-digit'
            }
          )
      : 'Latest scan'
  );
}


/* --------------------------------------------------
   COUNTS
-------------------------------------------------- */

function setCardCount(
  id,
  count
) {

  const card =
    $(id);

  if (!card) return;

  const el =
    card.querySelector(
      '.text-2xl'
    );

  if (el) {
    el.textContent =
      String(count);
  }
}


function updateCounts() {
  const all = allStocks.length;

  const upcoming = allStocks.filter(
    s => s.stageView === 'Upcoming'
  ).length;

  const post = allStocks.filter(
    s => s.stageView === 'Post-results'
  ).length;

  const caution = allStocks.filter(
    s => s.stageView === 'Caution'
  ).length;

  const qualified = allStocks.filter(
    s => s.stageView === 'Qualified'
  ).length;

  setCardCount('cardAll', all);
  setCardCount('cardUpcoming', upcoming);
  setCardCount('cardPostResults', post);
  setCardCount('cardCaution', caution);
  setCardCount('cardQualified', qualified);

  const labels = {
    tabBtnAll: `All Stocks (${all})`,
    tabBtnUpcoming: `Awaiting Results (${upcoming})`,
    tabBtnPost: `Results Declared (${post})`,
    tabBtnCaution: `Caution / Priced In (${caution})`,
    tabBtnQualified: `Fully Qualified (${qualified})`
  };

  Object.entries(labels).forEach(([id, label]) => {
    const el = document.getElementById(id);

    if (el) {
      el.textContent = label;
    }
  });
}


function updateTabButtons() {


/* --------------------------------------------------
   FILTERS
-------------------------------------------------- */

function updateTabButtons() {

  const map = {
    ALL:
      'tabBtnAll',

    Upcoming:
      'tabBtnUpcoming',

    'Post-results':
      'tabBtnPost',

    Caution:
      'tabBtnCaution',

    Qualified:
      'tabBtnQualified'
  };

  Object.entries(
    map
  ).forEach(
    ([key, id]) => {

      const el =
        $(id);

      if (el) {
        el.classList.toggle(
          'tab-active',
          currentStageTab ===
            key
        );
      }
    }
  );
}


function filteredStocks() {

  const q =
    String(
      $('searchInput')
        ?.value ??
        ''
    )
      .trim()
      .toLowerCase();

  return allStocks.filter(
    stock => {

      const stageOK =
        currentStageTab ===
          'ALL' ||
        stock.stageView ===
          currentStageTab;

      const searchOK =
        !q ||
        [
          stock.symbol,
          stock.name,
          stock.sector,
          stock.statusText
        ]
          .join(' ')
          .toLowerCase()
          .includes(q);

      return (
        stageOK &&
        searchOK
      );
    }
  );
}


/* --------------------------------------------------
   TABLE
-------------------------------------------------- */

function pill(
  value,
  yes = 'YES',
  no = 'NO'
) {

  if (value === true) {
    return `
      <span class="text-emerald-400 font-semibold">
        ${yes}
      </span>
    `;
  }

  if (value === false) {
    return `
      <span class="text-rose-400 font-semibold">
        ${no}
      </span>
    `;
  }

  return `
    <span class="text-slate-500">
      —
    </span>
  `;
}


function marketCapText(
  value
) {

  if (value == null) {
    return '—';
  }

  return (
    '₹' +
    Math.round(value)
      .toLocaleString(
        'en-IN'
      ) +
    ' Cr'
  );
}


function rowHtml(
  stock
) {

  const resultDate =
    parseDate(
      stock.resultDate
    );

  let declared =
    null;

  if (resultDate) {

    const today =
      new Date();

    today.setHours(
      23,
      59,
      59,
      999
    );

    declared =
      resultDate <= today;
  }

  const priceText =
    stock.price != null
      ? (
          `₹${stock.price}` +
          (
            stock.changePct !=
            null
              ? ` (${
                  stock.changePct >
                  0
                    ? '+'
                    : ''
                }${stock.changePct}%)`
              : ''
          )
        )
      : 'Quote unavailable';

  return `
    <tr class="hover:bg-white/[0.025]">

      <td class="py-3.5 px-4 align-top">

        <div class="font-bold text-white">
          ${esc(
            stock.symbol
          )}
        </div>

        <div class="text-[10px] text-slate-400 mt-1">
          ${esc(
            stock.name
          )}
        </div>

        <div class="text-[10px] text-slate-500 mt-1">
          ${esc(
            stock.sector
          )}
        </div>

        <div class="text-[10px] text-slate-500">
          ${esc(
            marketCapText(
              stock.marketCapCr
            )
          )}
        </div>

        <div class="text-[10px] mt-1 ${
          stock.liveStatus ===
          'ok'
            ? 'text-emerald-400'
            : 'text-amber-400'
        }">
          ${esc(
            priceText
          )}
        </div>

      </td>

      <td class="py-3.5 px-4 align-top">

        <div>
          ${esc(
            stock.quarter
          )}
        </div>

        <div class="text-[10px] text-slate-400 mt-1">
          ${esc(
            stock.statusText
          )}
        </div>

      </td>

      <td class="py-3.5 px-4 align-top">
        ${pill(
          declared
        )}
      </td>

      <td class="py-3.5 px-4 align-top">
        ${pill(
          stock.marketCapPass,
          'PASS',
          'FAIL'
        )}
      </td>

      <td class="py-3.5 px-4 align-top">
        ${pill(
          stock.revenue
        )}
      </td>

      <td class="py-3.5 px-4 align-top">
        ${pill(
          stock.quality
        )}
      </td>

      <td class="py-3.5 px-4 align-top">
        ${pill(
          stock.cash
        )}
      </td>

      <td class="py-3.5 px-4 text-center align-top">
        ${esc(
          stock.stageView
        )}
      </td>

      <td class="py-3.5 px-4 text-center align-top">

        <span class="font-mono font-bold">
          ${esc(
            stock.scoreText
          )}
        </span>

      </td>

      <td class="py-3.5 px-4 text-right align-top">

        <button
          type="button"
          data-open-stock="${esc(
            stock.symbol
          )}"
          class="px-3 py-1.5 rounded-lg border border-dark-700 hover:bg-dark-800"
        >
          View
        </button>

      </td>

    </tr>
  `;
}


function updateActiveBanner(
  rows
) {

  const count =
    $('currentViewCount');

  if (count) {
    count.textContent =
      `${rows.length} records`;
  }

  const banner =
    $('activeTabBanner');

  if (!banner) return;

  const labels = {
    ALL:
      'All Stocks',

    Upcoming:
      'Awaiting Results',

    'Post-results':
      'Results Declared / In Review',

    Caution:
      'Caution / Priced In',

    Qualified:
      'Fully Qualified'
  };

  const leaves =
    [
      ...banner.querySelectorAll(
        'span'
      )
    ].filter(
      el =>
        !el.id &&
        el.textContent.trim()
    );

  const labelEl =
    leaves.find(
      el =>
        /loading|all stocks|awaiting|results|caution|qualified/i
          .test(
            el.textContent
          )
    );

  if (labelEl) {
    labelEl.textContent =
      labels[
        currentStageTab
      ] ??
      currentStageTab;
  }
}


function renderTable() {

  const rows =
    filteredStocks();

  const tbody =
    $('stocksTableBody');

  if (tbody) {

    tbody.innerHTML =
      rows
        .map(rowHtml)
        .join('');

    tbody
      .querySelectorAll(
        '[data-open-stock]'
      )
      .forEach(
        button => {

          button.addEventListener(
            'click',
            () =>
              openModal(
                button.getAttribute(
                  'data-open-stock'
                )
              )
          );
        }
      );
  }

  const table =
    $('stocksTable');

  if (table) {
    table.style.display =
      rows.length
        ? ''
        : 'none';
  }

  const empty =
    $('emptyViewMessage');

  if (empty) {
    empty.classList.toggle(
      'hidden',
      rows.length > 0
    );
  }

  const desc =
    $('emptyViewDesc');

  if (desc) {
    desc.textContent =
      allStocks.length
        ? 'No stocks match this filter.'
        : 'No stocks loaded from data.json.';
  }

  updateActiveBanner(
    rows
  );
}


function renderAll() {
  updateCounts();
  updateTabButtons();
  renderTable();
}


/* --------------------------------------------------
   LOAD LIVE JSON
-------------------------------------------------- */

async function loadRadarData(
  manual = false
) {

  const icon =
    $('refreshIcon');

  if (manual) {
    icon?.classList.add(
      'animate-spin'
    );
  }

  try {

    const response =
      await fetch(
        `./data.json?t=${Date.now()}`,
        {
          cache:
            'no-store'
        }
      );

    if (!response.ok) {
      throw new Error(
        `data.json HTTP ${response.status}`
      );
    }

    const payload =
      await response.json();

    const rawStocks =
      extractStocks(
        payload
      );

    allStocks =
      rawStocks
        .map(
          normalizeRow
        )
        .filter(
          stock =>
            stock.symbol ||
            stock.name
        );

    console.log(
      'PEAD loaded',
      allStocks.length,
      'stocks'
    );

    updateHeader(
      payload
    );

    renderAll();

  } catch (error) {

    console.error(
      'PEAD load failed',
      error
    );

    allStocks = [];

    setStatusByLabel(
      'LAST SCAN',
      'Load error'
    );

    setStatusByLabel(
      'PRICE AT',
      'Unavailable'
    );

    renderAll();

    const desc =
      $('emptyViewDesc');

    if (desc) {
      desc.textContent =
        'Could not load data.json: ' +
        error.message;
    }

  } finally {

    icon?.classList.remove(
      'animate-spin'
    );
  }
}


/* --------------------------------------------------
   BUTTON FUNCTIONS
-------------------------------------------------- */

function forceScanRefresh() {
  return loadRadarData(
    true
  );
}


function selectStageTab(
  tab = 'ALL',
  scroll = false
) {

  currentStageTab =
    tab;

  updateTabButtons();

  renderTable();

  if (scroll) {
    $('radarTableContainer')
      ?.scrollIntoView({
        behavior:
          'smooth',
        block:
          'start'
      });
  }
}


function filterRadarTable() {
  renderTable();
}


function toggleSection(id) {
  $(id)
    ?.classList
    .toggle(
      'hidden'
    );
}


/* --------------------------------------------------
   MODAL
-------------------------------------------------- */

function openModal(symbol) {
  const stock = allStocks.find(
    s => s.symbol === symbol || s._id === symbol
  );

  if (!stock) return;

  activeModalStock = stock;

  if ($('mSymbol')) {
    $('mSymbol').textContent = stock.symbol || '—';
  }

  if ($('mStage')) {
    $('mStage').textContent = stock.stageView || 'In Review';
  }

  if ($('mName')) {
    $('mName').textContent = stock.name || stock.symbol || '—';
  }

  if ($('mSector')) {
    $('mSector').textContent = stock.sector || '—';
  }

  if ($('mScoreBadge')) {
    $('mScoreBadge').textContent = stock.scoreText || '0/8';
  }

  if ($('mEvidence')) {
    $('mEvidence').textContent =
      stock.note ||
      stock.evidence ||
      'No additional evidence stored.';
  }

  if ($('mVerdictTitle')) {
    $('mVerdictTitle').textContent =
      stock.stageView === 'Qualified'
        ? 'PEAD Qualified / Potential Candidate'
        : stock.stageView || 'In Review';
  }

  const checks = [
    ['Market cap > ₹1,000 Cr', stock.marketCapPass],
    ['Revenue / PAT acceleration', stock.revenue],
    ['Earnings quality', stock.quality],
    ['Cash flow / surprise', stock.cash],
    ['Price / volume confirmation', stock.technical],
    ['Sector tailwind', stock.sectorTailwind],
    ['Entry trigger defined', stock.entryTrigger],
    ['Stop loss defined', stock.stopDefined]
  ];

  if ($('mChecklistGrid')) {
    $('mChecklistGrid').innerHTML = checks
      .map(([label, value]) => {
        let state = 'UNVERIFIED';
        let cls = 'text-slate-400';

        if (value === true) {
          state = 'SATISFIED';
          cls = 'text-emerald-400';
        } else if (value === false) {
          state = 'NOT SATISFIED';
          cls = 'text-rose-400';
        }

        return `
          <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
            <div class="text-[11px] text-slate-300">
              ${esc(label)}
            </div>
            <div class="mt-1 font-semibold ${cls}">
              ${state}
            </div>
          </div>
        `;
      })
      .join('');
  }

  if ($('mThesisGrid')) {
    $('mThesisGrid').innerHTML = `
      <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
        <span class="text-slate-500">Entry:</span>
        ${esc(stock.entry ?? '—')}
      </div>

      <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
        <span class="text-slate-500">SL:</span>
        ${esc(stock.sl ?? '—')}
      </div>

      <div class="p-3 rounded-xl bg-dark-900 border border-dark-750">
        <span class="text-slate-500">TSL:</span>
        ${esc(stock.tsl ?? '—')}
      </div>
    `;
  }

  const modal = $('stockModal');

  if (modal) {
    modal.classList.remove('hidden');
    modal.style.display = 'flex';
  }
}


function closeModal() {
  const modal = $('stockModal');

  if (modal) {
    modal.style.display = 'none';
    modal.classList.add('hidden');
  }

  activeModalStock = null;
}


function calculateTrade() {
  const portfolio = num($('calcPortfolio')?.value);
  const entry = num($('calcEntry')?.value);
  const sl = num($('calcSL')?.value);

  if (!portfolio || !entry || sl == null) return;

  const riskPerShare = Math.abs(entry - sl);

  const riskPct =
    entry > 0
      ? (riskPerShare / entry) * 100
      : 0;

  const maxRiskCapital = portfolio * 0.01;

  const quantity =
    riskPerShare > 0
      ? Math.floor(maxRiskCapital / riskPerShare)
      : 0;

  const values = {
    resRiskPct: `${riskPct.toFixed(2)}%`,
    resRiskPerShare: `₹${riskPerShare.toFixed(2)}`,
    resMaxShares: quantity.toLocaleString('en-IN'),
    resMaxQty: quantity.toLocaleString('en-IN'),
    resTarget1: `₹${(entry + riskPerShare * 2).toFixed(2)}`,
    resTarget2: `₹${(entry + riskPerShare * 3).toFixed(2)}`
  };

  Object.entries(values).forEach(([id, value]) => {
    if ($(id)) {
      $(id).textContent = value;
    }
  });
}


/* Make HTML onclick functions available globally */

Object.assign(window, {
  forceScanRefresh,
  selectStageTab,
  filterRadarTable,
  toggleSection,
  calculateTrade,
  openModal,
  closeModal
});


/* Start application */

function boot() {
  console.log('PEAD Radar booting…');

  $('searchInput')?.addEventListener(
    'input',
    filterRadarTable
  );

  loadRadarData();

  setInterval(
    () => loadRadarData(),
    60 * 60 * 1000
  );
}


if (document.readyState === 'loading') {
  document.addEventListener(
    'DOMContentLoaded',
    boot,
    { once: true }
  );
} else {
  boot();
}
