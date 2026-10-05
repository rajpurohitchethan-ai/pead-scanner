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
  return text(v, '').replace(
    /[&<>'"]/g,
    ch =>
      ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;'
      })[ch]
  );
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

    if (
      [
        'yes',
        'true',
        'pass',
        'passed',
        'satisfied',
        'qualified',
        'ok',
        'green'
      ].includes(s)
    ) {
      return true;
    }

    if (
      [
        'no',
        'false',
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

function extractRows(payload) {
  if (Array.isArray(payload)) return payload;

  if (!payload || typeof payload !== 'object') {
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

  if (Array.isArray(payload.data?.companies)) {
    return payload.data.companies;
  }

  if (Array.isArray(payload.data?.stocks)) {
    return payload.data.stocks;
  }

  return [];
}

function normalizeChecks(raw) {
  if (Array.isArray(raw?.checks)) {
    return raw.checks.map((c, i) => {
      if (typeof c === 'string') {
        return {
          label: c,
          value: null,
          note: ''
        };
      }

      return {
        label: pick(c, 'label', 'name', 'title') || `Check ${i + 1}`,
        value: boolish(
          pick(
            c,
            'value',
            'pass',
            'passed',
            'satisfied',
            'status'
          )
        ),
        note:
          pick(
            c,
            'note',
            'detail',
            'reason',
            'evidence'
          ) || ''
      };
    });
  }

  return [
    {
      label: 'Market cap > ₹1,000 Cr',
      value: boolish(
        pick(raw, 'marketCapPass', 'mcapPass')
      ),
      note: ''
    },

    {
      label: 'Revenue / PAT acceleration',
      value: boolish(
        pick(
          raw,
          'revenuePatPass',
          'revenueAcceleration',
          'earningsAcceleration'
        )
      ),
      note: text(
        pick(
          raw,
          'revenuePatNote',
          'revenueGrowth',
          'patGrowth'
        ),
        ''
      )
    },

    {
      label: 'Earnings quality',
      value: boolish(
        pick(
          raw,
          'earningsQualityPass',
          'earningsQuality'
        )
      ),
      note: text(
        pick(raw, 'earningsQualityNote'),
        ''
      )
    },

    {
      label: 'Cash flow / surprise',
      value: boolish(
        pick(
          raw,
          'cashFlowPass',
          'surprisePass',
          'cashFlowSurprise'
        )
      ),
      note: text(
        pick(
          raw,
          'cashFlowNote',
          'surpriseNote'
        ),
        ''
      )
    },

    {
      label: 'Price / volume confirmation',
      value: boolish(
        pick(
          raw,
          'priceVolumePass',
          'technicalPass',
          'priceConfirmation'
        )
      ),
      note: text(
        pick(raw, 'technicalNote'),
        ''
      )
    },

    {
      label: 'Sector tailwind',
      value: boolish(
        pick(
          raw,
          'sectorTailwind',
          'sectorPass'
        )
      ),
      note: text(
        pick(raw, 'sectorNote'),
        ''
      )
    },

    {
      label: 'Entry trigger defined',
      value: boolish(
        pick(
          raw,
          'entryTriggerPass',
          'entryConfirmed'
        )
      ),
      note: text(
        pick(
          raw,
          'entry',
          'entryPrice'
        ),
        ''
      )
    },

    {
      label: 'Invalidation / stop defined',
      value:
        pick(
          raw,
          'sl',
          'stopLoss',
          'invalidation'
        ) != null
          ? true
          : null,
      note: text(
        pick(
          raw,
          'sl',
          'stopLoss',
          'invalidation'
        ),
        ''
      )
    }
  ];
}

function inferView(raw, resultDate, statusText) {
  const s = statusText.toLowerCase();

  if (
    s.includes('qualified') ||
    s.includes('entry confirmed') ||
    s.includes('hold')
  ) {
    return 'Qualified';
  }

  if (
    s.includes('caution') ||
    s.includes('priced')
  ) {
    return 'Caution';
  }

  if (
    s.includes('upcoming') ||
    s.includes('awaiting')
  ) {
    return 'Upcoming';
  }

  if (
    s.includes('post') ||
    s.includes('review') ||
    s.includes('declared')
  ) {
    return 'Post-results';
  }

  if (resultDate) {
    const d = new Date(resultDate);

    if (!Number.isNaN(d.getTime())) {
      return d.getTime() > Date.now()
        ? 'Upcoming'
        : 'Post-results';
    }
  }

  return 'Post-results';
}

function mapScan(raw, index = 0) {
  const symbol = text(
    pick(
      raw,
      'symbol',
      'sym',
      'ticker',
      'code'
    ),
    ''
  ).replace(/\.NS$/i, '');

  const resultDate = pick(
    raw,
    'resultDate',
    'result_date',
    'resultsDate',
    'earningsDate'
  );

  const statusText = text(
    pick(
      raw,
      'peadStatus',
      'stage',
      'status',
      'bucket'
    ),
    'In Review'
  );

  const marketCapCr = num(
    pick(
      raw,
      'marketCapCr',
      'mcapCr',
      'market_cap_cr'
    )
  );

  const checks = normalizeChecks(raw);

  const passedChecks = checks.filter(
    c => c.value === true
  ).length;

  const knownChecks = checks.filter(
    c => c.value !== null
  ).length;

  const sourceScore = num(
    pick(
      raw,
      'score',
      'scoreValue',
      'peadScore'
    )
  );

  const score =
    sourceScore ?? passedChecks;

  return {
    ...raw,

    _id:
      symbol ||
      `row-${index}`,

    symbol,

    name: text(
      pick(
        raw,
        'name',
        'company',
        'companyName'
      ),
      symbol || 'Unknown'
    ),

    sector: text(
      pick(
        raw,
        'sector',
        'industry'
      ),
      '—'
    ),

    earningsPeriod: text(
      pick(
        raw,
        'earningsPeriod',
        'quarter',
        'period'
      ),
      '—'
    ),

    resultDate,
    marketCapCr,

    marketCapPass:
      boolish(
        pick(
          raw,
          'marketCapPass',
          'mcapPass'
        )
      ) ??
      (
        marketCapCr == null
          ? null
          : marketCapCr >= MIN_MCAP_CR
      ),

    peadStatus: statusText,

    view: inferView(
      raw,
      resultDate,
      statusText
    ),

    score,

    scoreText: text(
      pick(
        raw,
        'scoreText'
      ),
      `${score}/${checks.length || 8}`
    ),

    checks,
    knownChecks,

    evidence: text(
      pick(
        raw,
        'evidence',
        'resultEvidence',
        'source'
      ),
      '—'
    ),

    thesis:
      pick(
        raw,
        'thesis',
        'thesisItems',
        'notes'
      ) || [],

    liveStatus: text(
      pick(
        raw,
        'liveStatus'
      ),
      '—'
    ),

    liveError:
      pick(
        raw,
        'liveError'
      ),

    price: num(
      pick(
        raw,
        'price',
        'lastPrice',
        'ltp'
      )
    ),

    changePct: num(
      pick(
        raw,
        'changePct',
        'change_percent'
      )
    ),

    entry:
      pick(
        raw,
        'entry',
        'entryPrice'
      ),

    sl:
      pick(
        raw,
        'sl',
        'stopLoss'
      ),

    tsl:
      pick(
        raw,
        'tsl',
        'trailingStopLoss'
      )
  };
}

function formatMcap(v) {
  const n = num(v);

  if (n == null) {
    return 'Unverified';
  }

  return `₹${n.toLocaleString(
    'en-IN',
    {
      maximumFractionDigits: 0
    }
  )} Cr`;
}

function formatDate(v) {
  if (!v) return '—';

  const d = new Date(v);

  return Number.isNaN(d.getTime())
    ? text(v)
    : d.toLocaleDateString(
        'en-IN',
        {
          day: '2-digit',
          month: 'short',
          year: 'numeric'
        }
      );
}

function checkBadge(
  value,
  labelTrue = 'Satisfied',
  labelFalse = 'Not satisfied'
) {
  if (value === true) {
    return `
      <span class="px-2 py-1 rounded text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/20">
        ${labelTrue}
      </span>
    `;
  }

  if (value === false) {
    return `
      <span class="px-2 py-1 rounded text-xs bg-red-500/10 text-red-300 border border-red-500/20">
        ${labelFalse}
      </span>
    `;
  }

  return `
    <span class="px-2 py-1 rounded text-xs bg-slate-500/10 text-slate-300 border border-slate-500/20">
      Unverified
    </span>
  `;
}

function findCheck(item, terms) {
  const lc =
    terms.map(
      x => x.toLowerCase()
    );

  return (
    item.checks.find(
      c =>
        lc.some(
          t =>
            c.label
              .toLowerCase()
              .includes(t)
        )
    ) || {
      value: null,
      note: ''
    }
  );
}

function setCardCount(cardId, count) {
  const card = $(cardId);

  if (!card) return;

  const target =
    card.querySelector('.text-2xl') ||
    card.querySelector('[data-count]');

  if (target) {
    target.textContent =
      String(count);
  }
}

function updateCounts() {
  const count =
    view =>
      fullRadarData.filter(
        x => x.view === view
      ).length;

  setCardCount(
    'cardAll',
    fullRadarData.length
  );

  setCardCount(
    'cardPostResults',
    count('Post-results')
  );

  setCardCount(
    'cardUpcoming',
    count('Upcoming')
  );

  setCardCount(
    'cardCaution',
    count('Caution')
  );

  setCardCount(
    'cardQualified',
    count('Qualified')
  );
}

function setTabClasses() {
  const ids = {
    ALL: 'tabBtnAll',
    Upcoming: 'tabBtnUpcoming',
    'Post-results': 'tabBtnPost',
    Caution: 'tabBtnCaution',
    Qualified: 'tabBtnQualified'
  };

  Object.entries(ids).forEach(
    ([tab, id]) => {
      const el = $(id);

      if (!el) return;

      el.classList.toggle(
        'tab-active',
        currentStageTab === tab
      );
    }
  );

  const cards = {
    ALL: 'cardAll',
    'Post-results':
      'cardPostResults',
    Upcoming:
      'cardUpcoming',
    Caution:
      'cardCaution',
    Qualified:
      'cardQualified'
  };

  Object.entries(cards).forEach(
    ([tab, id]) => {
      const el = $(id);

      if (el) {
        el.classList.toggle(
          'card-active',
          currentStageTab === tab
        );
      }
    }
  );
}

function currentRows() {
  const q =
    (
      $('searchInput')?.value ||
      ''
    )
      .trim()
      .toLowerCase();

  return fullRadarData.filter(
    item => {
      const tabOk =
        currentStageTab === 'ALL' ||
        item.view ===
          currentStageTab;

      const searchOk =
        !q ||
        [
          item.symbol,
          item.name,
          item.sector,
          item.peadStatus
        ]
          .join(' ')
          .toLowerCase()
          .includes(q);

      return tabOk && searchOk;
    }
  );
}

function rowHtml(item) {
  const revenue =
    findCheck(
      item,
      ['revenue', 'pat']
    );

  const quality =
    findCheck(
      item,
      ['earnings quality']
    );

  const cash =
    findCheck(
      item,
      [
        'cash flow',
        'surprise'
      ]
    );

  const live =
    item.liveStatus === 'ok'
      ? `
        <span class="text-xs text-emerald-300">
          Live ₹${item.price ?? '—'}
          ${
            item.changePct == null
              ? ''
              : ` (${
                  item.changePct > 0
                    ? '+'
                    : ''
                }${item.changePct}%)`
          }
        </span>
      `
      : `
        <span
          class="text-xs text-amber-300"
          title="${esc(
            item.liveError || ''
          )}"
        >
          ${
            item.liveStatus ===
            'unavailable'
              ? 'Quote unavailable'
              : esc(
                  item.liveStatus
                )
          }
        </span>
      `;

  return `
    <tr class="hover:bg-white/[0.025] transition-colors">

      <td class="py-3 px-4 align-top">

        <div class="font-semibold text-white">
          ${esc(
            item.symbol ||
            item.name
          )}
        </div>

        <div class="text-xs text-slate-400">
          ${esc(item.name)}
        </div>

        <div class="text-xs text-slate-500 mt-1">
          ${esc(
            formatMcap(
              item.marketCapCr
            )
          )}
        </div>

        ${live}

      </td>

      <td class="py-3 px-4 align-top">

        <div class="text-sm text-slate-200">
          ${esc(
            item.earningsPeriod
          )}
        </div>

        <div class="text-xs text-slate-400 mt-1">
          ${esc(
            item.peadStatus
          )}
        </div>

      </td>

      <td class="py-3 px-4 align-top text-sm text-slate-300">
        ${esc(
          formatDate(
            item.resultDate
          )
        )}
      </td>

      <td class="py-3 px-4 align-top">
        ${checkBadge(
          item.marketCapPass,
          'Yes',
          'No'
        )}
      </td>

      <td class="py-3 px-4 align-top">
        ${checkBadge(
          revenue.value
        )}
      </td>

      <td class="py-3 px-4 align-top">
        ${checkBadge(
          quality.value
        )}
      </td>

      <td class="py-3 px-4 align-top">
        ${checkBadge(
          cash.value
        )}
      </td>

      <td class="py-3 px-4 align-top">
        <span class="px-2 py-1 rounded text-xs border border-white/10">
          ${esc(
            item.peadStatus
          )}
        </span>
      </td>

      <td class="py-3 px-4 align-top">
        <span class="font-mono font-semibold">
          ${esc(
            item.scoreText
          )}
        </span>
      </td>

      <td class="py-3 px-4 align-top">

        <button
          type="button"
          class="px-3 py-1.5 rounded-md text-xs border border-white/10 hover:bg-white/5"
          onclick="openModal('${esc(
            item.symbol
          )}')"
        >
          View checklist
        </button>

      </td>

    </tr>
  `;
}

function renderTable() {
  const tbody =
    $('stocksTableBody');

  if (!tbody) return;

  const rows =
    currentRows();

  tbody.innerHTML =
    rows
      .map(rowHtml)
      .join('');

  const table =
    $('stocksTable');

  const empty =
    $('emptyViewMessage');

  const emptyDesc =
    $('emptyViewDesc');

  if (table) {
    table.style.display =
      rows.length
        ? ''
        : 'none';
  }

  if (empty) {
    empty.style.display =
      rows.length
        ? 'none'
        : '';
  }

  if (emptyDesc) {
    emptyDesc.textContent =
      fullRadarData.length
        ? 'No stocks match this view/search.'
        : 'No scanner data loaded. Check data.json / GitHub Action status.';
  }

  if ($('currentViewCount')) {
    $('currentViewCount')
      .textContent =
      String(rows.length);
  }

  if ($('currentViewTitle')) {
    $('currentViewTitle')
      .textContent =
      currentStageTab === 'ALL'
        ? 'All Stocks'
        : currentStageTab;
  }

  if ($('activeTabBanner')) {
    $('activeTabBanner')
      .textContent =
      currentStageTab === 'ALL'
        ? 'All monitored stocks'
        : currentStageTab;
  }
}

function renderAll() {
  updateCounts();
  setTabClasses();
  renderTable();
}

async function loadRadarData(
  {
    manual = false
  } = {}
) {
  const icon =
    $('refreshIcon');

  if (manual && icon) {
    icon.classList.add(
      'animate-spin'
    );
  }

  if ($('scanDiag')) {
    $('scanDiag')
      .textContent =
      'Loading data.json…';
  }

  try {

    const res =
      await fetch(
        `./data.json?v=${Date.now()}`,
        {
          cache: 'no-store'
        }
      );

    if (!res.ok) {
      throw new Error(
        `data.json HTTP ${res.status}`
      );
    }

    const payload =
      await res.json();

    const rows =
      extractRows(payload);

    lastPayload =
      payload;

    fullRadarData =
      rows.map(mapScan);

    renderAll();

    const stamp =
      pick(
        payload,
        'generatedAt',
        'last_scan',
        'lastScanAt'
      ) ||
      new Date()
        .toISOString();

    if ($('lastScanText')) {
      $('lastScanText')
        .textContent =
        `Last scan: ${
          new Date(
            stamp
          ).toLocaleString(
            'en-IN'
          )
        }`;
    }

    if ($('scanDiag')) {

      const errs =
        num(
          payload.errorCount
        ) ??
        rows.filter(
          r =>
            r.liveError
        ).length;

      $('scanDiag')
        .textContent =
        `Loaded ${
          fullRadarData.length
        } stocks${
          errs
            ? ` • ${errs} quote warnings`
            : ''
        }`;
    }

  } catch (err) {

    console.error(
      'PEAD radar load failed:',
      err
    );

    fullRadarData = [];

    renderAll();

    if ($('lastScanText')) {
      $('lastScanText')
        .textContent =
        'Last scan: unavailable';
    }

    if ($('scanDiag')) {
      $('scanDiag')
        .textContent =
        `Scanner data error: ${
          err.message
        }`;
    }

  } finally {

    if (icon) {
      icon.classList.remove(
        'animate-spin'
      );
    }
  }
}

function forceScanRefresh() {
  return loadRadarData({
    manual: true
  });
}

function selectStageTab(
  tab,
  scroll = false
) {
  currentStageTab =
    tab || 'ALL';

  setTabClasses();
  renderTable();

  if (scroll) {
    $('radarTableContainer')
      ?.scrollIntoView({
        behavior: 'smooth',
        block: 'start'
      });
  }
}

function filterRadarTable() {
  renderTable();
}

function toggleSection(id) {
  const el = $(id);

  if (!el) return;

  el.classList.toggle(
    'hidden'
  );
}

function verdictStyle(item) {
  if (
    item.view ===
    'Qualified'
  ) {
    return {
      title:
        'PEAD qualified / potential candidate',
      cls:
        'text-emerald-300'
    };
  }

  if (
    item.view ===
    'Caution'
  ) {
    return {
      title:
        'Caution / priced in',
      cls:
        'text-amber-300'
    };
  }

  if (
    item.view ===
    'Upcoming'
  ) {
    return {
      title:
        'Awaiting results',
      cls:
        'text-sky-300'
    };
  }

  return {
    title:
      'Post-results / in review',
    cls:
      'text-slate-200'
  };
}

function renderChecklist(item) {
  return item.checks
    .map(c => {

      const state =
        c.value === true
          ? '✓'
          : c.val
