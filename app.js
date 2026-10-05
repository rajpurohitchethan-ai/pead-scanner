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
  return (
    v === null ||
    v === undefined ||
    v === ''
  )
    ? fallback
    : String(v);
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n)
    ? n
    : null;
}

function esc(v) {
  return text(v, '').replace(
    /[&<>'"]/g,
    ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    }[ch])
  );
}

function pick(obj, ...keys) {
  for (const key of keys) {
    const v = obj?.[key];

    if (
      v !== undefined &&
      v !== null &&
      v !== ''
    ) {
      return v;
    }
  }

  return null;
}

function boolish(v) {
  if (
    v === true ||
    v === false
  ) {
    return v;
  }

  if (typeof v === 'number') {
    return v !== 0;
  }

  if (typeof v === 'string') {
    const s =
      v.trim().toLowerCase();

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
  if (Array.isArray(payload)) {
    return payload;
  }

  if (
    !payload ||
    typeof payload !== 'object'
  ) {
    return [];
  }

  if (
    Array.isArray(
      payload.companies
    )
  ) {
    return payload.companies;
  }

  if (
    Array.isArray(
      payload.stocks
    )
  ) {
    return payload.stocks;
  }

  if (
    Array.isArray(
      payload.data
    )
  ) {
    return payload.data;
  }

  if (
    Array.isArray(
      payload.data?.companies
    )
  ) {
    return payload.data.companies;
  }

  if (
    Array.isArray(
      payload.data?.stocks
    )
  ) {
    return payload.data.stocks;
  }

  return [];
}


function inferResultsReleased(
  raw,
  statusText
) {
  const explicit =
    boolish(
      pick(
        raw,
        'resultsReleased',
        'resultReleased',
        'results_declared'
      )
    );

  if (explicit !== null) {
    return explicit;
  }

  const source =
    text(
      pick(
        raw,
        'discoverySource',
        'source'
      ),
      ''
    ).toLowerCase();

  if (
    source.includes(
      'financial results'
    )
  ) {
    return true;
  }

  const s =
    text(
      statusText,
      ''
    ).toLowerCase();

  if (
    s.includes(
      'post-results'
    ) ||
    s.includes(
      'post results'
    ) ||
    s.includes(
      'results declared'
    ) ||
    s.includes(
      'in review'
    ) ||
    s.includes(
      'qualified'
    )
  ) {
    return true;
  }

  return false;
}


function normalizeChecks(raw) {
  if (
    Array.isArray(
      raw?.checks
    ) &&
    raw.checks.length
  ) {
    return raw.checks.map(
      (c, i) => {

        if (
          typeof c ===
          'string'
        ) {
          return {
            label: c,
            value: null,
            note: ''
          };
        }

        return {
          label:
            pick(
              c,
              'label',
              'name',
              'title'
            )
            ||
            `Check ${i + 1}`,

          value:
            boolish(
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
            )
            ||
            ''
        };
      }
    );
  }

  return [
    {
      label:
        'Results released',

      value:
        boolish(
          pick(
            raw,
            'resultsReleased',
            'resultReleased'
          )
        ),

      note:
        text(
          pick(
            raw,
            'resultsEvidence'
          ),
          ''
        )
    },

    {
      label:
        'Market cap > ₹1,000 Cr',

      value:
        boolish(
          pick(
            raw,
            'marketCapPass',
            'mcapPass'
          )
        ),

      note: ''
    },

    {
      label:
        'Earnings acceleration',

      value:
        boolish(
          pick(
            raw,
            'earningsAccelerationPass',
            'revenuePatPass',
            'revenueAcceleration'
          )
        ),

      note:
        text(
          pick(
            raw,
            'earningsEvidence',
            'revenuePatNote'
          ),
          ''
        )
    },

    {
      label:
        'Earnings quality',

      value:
        boolish(
          pick(
            raw,
            'earningsQualityPass',
            'earningsQuality'
          )
        ),

      note:
        text(
          pick(
            raw,
            'qualityEvidence',
            'earningsQualityNote'
          ),
          ''
        )
    },

    {
      label:
        'Cash flow',

      value:
        boolish(
          pick(
            raw,
            'cashFlowPass'
          )
        ),

      note:
        text(
          pick(
            raw,
            'cashFlowEvidence',
            'cashFlowNote'
          ),
          ''
        )
    },

    {
      label:
        'Surprise',

      value:
        boolish(
          pick(
            raw,
            'surprisePass'
          )
        ),

      note:
        text(
          pick(
            raw,
            'surpriseEvidence',
            'surpriseNote'
          ),
          ''
        )
    },

    {
      label:
        'Post-result price/volume confirmation',

      value:
        boolish(
          pick(
            raw,
            'priceVolumePass',
            'technicalPass'
          )
        ),

      note:
        text(
          pick(
            raw,
            'priceVolumeEvidence',
            'technicalNote'
          ),
          ''
        )
    },

    {
      label:
        'Liquidity',

      value:
        boolish(
          pick(
            raw,
            'liquidityPass'
          )
        ),

      note:
        text(
          pick(
            raw,
            'liquidityEvidence'
          ),
          ''
        )
    }
  ];
}


function inferView(
  raw,
  resultDate,
  statusText,
  resultsReleased
) {
  const s =
    text(
      statusText,
      ''
    ).toLowerCase();

  if (
    s.includes(
      'qualified'
    ) ||
    s.includes(
      'entry confirmed'
    ) ||
    s.includes(
      'hold'
    )
  ) {
    return 'Qualified';
  }

  if (
    s.includes(
      'caution'
    ) ||
    s.includes(
      'priced'
    )
  ) {
    return 'Caution';
  }

  if (
    s.includes(
      'upcoming'
    ) ||
    s.includes(
      'awaiting'
    )
  ) {
    return 'Upcoming';
  }

  if (
    s.includes(
      'post'
    ) ||
    s.includes(
      'review'
    ) ||
    s.includes(
      'declared'
    )
  ) {
    return 'Post-results';
  }

  if (
    resultsReleased === true
  ) {
    return 'Post-results';
  }

  if (resultDate) {
    const d =
      new Date(
        resultDate
      );

    if (
      !Number.isNaN(
        d.getTime()
      ) &&
      d.getTime() >
      Date.now()
    ) {
      return 'Upcoming';
    }
  }

  return 'Post-results';
}


function mapScan(
  raw,
  index = 0
) {
  const symbol =
    text(
      pick(
        raw,
        'symbol',
        'sym',
        'ticker',
        'code'
      ),
      ''
    ).replace(
      /\.NS$/i,
      ''
    );

  const resultDate =
    pick(
      raw,
      'resultDate',
      'result_date',
      'resultsDate',
      'earningsDate'
    );

  const statusText =
    text(
      pick(
        raw,
        'peadStatus',
        'stage',
        'status',
        'bucket'
      ),
      'In Review'
    );

  const marketCapCr =
    num(
      pick(
        raw,
        'marketCapCr',
        'mcapCr',
        'market_cap_cr'
      )
    );

  const resultsReleased =
    inferResultsReleased(
      raw,
      statusText
    );

  const checks =
    normalizeChecks({
      ...raw,
      resultsReleased
    });

  const passedChecks =
    checks.filter(
      c =>
        c.value === true
    ).length;

  const knownChecks =
    checks.filter(
      c =>
        c.value !== null
    ).length;

  const score =
    num(
      pick(
        raw,
        'score',
        'scoreValue',
        'peadScore'
      )
    )
    ??
    passedChecks;

  const view =
    inferView(
      raw,
      resultDate,
      statusText,
      resultsReleased
    );

  return {
    ...raw,

    _id:
      symbol ||
      `row-${index}`,

    symbol,

    name:
      text(
        pick(
          raw,
          'name',
          'company',
          'companyName'
        ),
        symbol ||
        'Unknown'
      ),

    sector:
      text(
        pick(
          raw,
          'sector',
          'industry'
        ),
        '—'
      ),

    earningsPeriod:
      text(
        pick(
          raw,
          'earningsPeriod',
          'quarter',
          'period'
        ),
        '—'
      ),

    resultDate,
    resultsReleased,
    marketCapCr,

    marketCapPass:
      boolish(
        pick(
          raw,
          'marketCapPass',
          'mcapPass'
        )
      )
      ??
      (
        marketCapCr == null
          ? null
          : marketCapCr >=
            MIN_MCAP_CR
      ),

    peadStatus:
      statusText,

    view,

    stageView:
      view,

    score,

    scoreText:
      text(
        pick(
          raw,
          'scoreText'
        ),
        `${score}/${checks.length || 8}`
      ),

    checks,
    knownChecks,

    evidence:
      text(
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
      )
      ||
      [],

    liveStatus:
      text(
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

    price:
      num(
        pick(
          raw,
          'price',
          'lastPrice',
          'ltp'
        )
      ),

    changePct:
      num(
        pick(
          raw,
          'changePct',
          'change_percent'
        )
      ),

    priceTimestamp:
      pick(
        raw,
        'priceTimestamp',
        'quoteTimestamp',
        'lastUpdateTime'
      ),

    revenueYoY:
      num(
        pick(
          raw,
          'revenueYoY'
        )
      ),

    patYoY:
      num(
        pick(
          raw,
          'patYoY'
        )
      ),

    revenueQoQ:
      num(
        pick(
          raw,
          'revenueQoQ'
        )
      ),

    patQoQ:
      num(
        pick(
          raw,
          'patQoQ'
        )
      ),

    relativeVolume:
      num(
        pick(
          raw,
          'relativeVolume',
          'rvol'
        )
      ),

    avgTurnover20dCr:
      num(
        pick(
          raw,
          'avgTurnover20dCr'
        )
      ),

    sectorTailwind:
      boolish(
        pick(
          raw,
          'sectorTailwind',
          'sectorPass'
        )
      ),

    preResultRunupPct:
      num(
        pick(
          raw,
          'preResultRunupPct'
        )
      ),

    resultDayReturnPct:
      num(
        pick(
          raw,
          'resultDayReturnPct'
        )
      ),

    pricedIn:
      boolish(
        pick(
          raw,
          'pricedIn'
        )
      ),

    candidateStatus:
      pick(
        raw,
        'candidateStatus'
      ),

    allocationPct:
      num(
        pick(
          raw,
          'allocationPct'
        )
      ),

    entryTriggerPass:
      boolish(
        pick(
          raw,
          'entryTriggerPass'
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
  const n =
    num(v);

  return n == null
    ? 'Unverified'
    : `₹${n.toLocaleString(
        'en-IN',
        {
          maximumFractionDigits: 0
        }
      )} Cr`;
}


function formatDate(v) {
  if (!v) {
    return '—';
  }

  const d =
    new Date(v);

  return Number.isNaN(
    d.getTime()
  )
    ? text(v)
    : d.toLocaleDateString(
        'en-IN',
        {
          day:
            '2-digit',

          month:
            'short',

          year:
            'numeric'
        }
      );
}


function formatDateTime(v) {
  if (!v) {
    return '—';
  }

  const d =
    new Date(v);

  return Number.isNaN(
    d.getTime()
  )
    ? text(v)
    : d.toLocaleString(
        'en-IN',
        {
          day:
            '2-digit',

          month:
            'short',

          hour:
            '2-digit',

          minute:
            '2-digit'
        }
      );
}


function fmtPct(v) {
  const n =
    num(v);

  return n == null
    ? '—'
    : `${n > 0 ? '+' : ''}${n.toFixed(1)}%`;
}


function fmtX(v) {
  const n =
    num(v);

  return n == null
    ? '—'
    : `${n.toFixed(2)}x`;
}


function fmtPrice(v) {
  const n =
    num(v);

  return n == null
    ? '—'
    : `₹${n.toFixed(2)}`;
}


function checkBadge(
  value,
  labelTrue = 'Satisfied',
  labelFalse = 'Not satisfied'
) {
  if (
    value === true
  ) {
    return `
      <span
        class="
          px-2 py-1 rounded text-xs
          bg-emerald-500/10
          text-emerald-300
          border border-emerald-500/20
        "
      >
        ${esc(labelTrue)}
      </span>
    `;
  }

  if (
    value === false
  ) {
    return `
      <span
        class="
          px-2 py-1 rounded text-xs
          bg-red-500/10
          text-red-300
          border border-red-500/20
        "
      >
        ${esc(labelFalse)}
      </span>
    `;
  }

  return `
    <span
      class="
        px-2 py-1 rounded text-xs
        bg-slate-500/10
        text-slate-300
        border border-slate-500/20
      "
    >
      Unverified
    </span>
  `;
}


function gateBadge(value) {
  if (
    value === true
  ) {
    return `
      <span
        class="
          text-emerald-400
          font-bold
        "
      >
        ✓ PASS
      </span>
    `;
  }

  if (
    value === false
  ) {
    return `
      <span
        class="
          text-rose-400
          font-bold
        "
      >
        ✕ FAIL
      </span>
    `;
  }

  return `
    <span
      class="
        text-amber-400
        font-bold
      "
    >
      ? PENDING
    </span>
  `;
}


function findCheck(
  item,
  terms
) {
  const lc =
    terms.map(
      x =>
        x.toLowerCase()
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
    )
    ||
    {
      value: null,
      note: ''
    }
  );
}


function setCardCount(
  cardId,
  count
) {
  const card =
    $(cardId);

  if (!card) {
    return;
  }

  const target =
    card.querySelector(
      '.text-2xl'
    )
    ||
    card.querySelector(
      '[data-count]'
    );

  if (target) {
    target.textContent =
      String(count);
  }
}


function qualificationReason(
  item
) {
  const failed =
    item.checks
      .filter(
        c =>
          c.value === false
      )
      .map(
        c =>
          c.label
      );

  const pending =
    item.checks
      .filter(
        c =>
          c.value === null
      )
      .map(
        c =>
          c.label
      );

  if (
    item.pricedIn === true
    ||
    item.view ===
      'Caution'
  ) {
    const runup =
      num(
        item.preResultRunupPct
      );

    return (
      runup == null
        ? (
            'CAUTION / PRICED IN: ' +
            'the pre-result move is flagged as extended.'
          )
        : (
            `CAUTION / PRICED IN: ` +
            `pre-result run-up ${runup.toFixed(1)}% ` +
            `exceeded the configured threshold.`
          )
    );
  }

  if (
    item.resultsReleased
    !== true
  ) {
    return (
      'AWAITING RESULTS: ' +
      'official result filing has not been confirmed yet.'
    );
  }

  if (
    failed.length
  ) {
    return (
      'NOT QUALIFIED: failed ' +
      failed.join(', ') +
      '.'
    );
  }

  if (
    pending.length
  ) {
    return (
      'IN REVIEW: waiting for ' +
      pending.join(', ') +
      '.'
    );
  }

  if (
    item.checks.length
    &&
    item.checks.every(
      c =>
        c.value === true
    )
  ) {
    return (
      item.entryTriggerPass
      === true
    )
      ? (
          'QUALIFIED: all 8 PEAD gates passed ' +
          'and the planned entry trigger has fired.'
        )
      : (
          'QUALIFIED: all 8 PEAD gates passed. ' +
          'Waiting for the planned entry trigger.'
        );
  }

  if (
    item.qualificationError
  ) {
    return (
      'QUALIFICATION ERROR: ' +
      item.qualificationError
    );
  }

  return (
    'Post-result review is complete, ' +
    'but the stock has not been marked qualified.'
  );
}


function updateCounts() {
  const all =
    fullRadarData.length;

  const upcoming =
    fullRadarData.filter(
      x =>
        x.resultsReleased
        !== true
        &&
        x.view !==
        'Caution'
    ).length;

  const postResults =
    fullRadarData.filter(
      x =>
        x.view ===
        'Post-results'
    ).length;

  const declared =
    fullRadarData.filter(
      x =>
        x.resultsReleased
        === true
    ).length;

  const caution =
    fullRadarData.filter(
      x =>
        x.view ===
        'Caution'
    ).length;

  const qualified =
    fullRadarData.filter(
      x =>
        x.view ===
        'Qualified'
    ).length;

  setCardCount(
    'cardAll',
    all
  );

  setCardCount(
    'cardPostResults',
    postResults
  );

  setCardCount(
    'cardUpcoming',
    upcoming
  );

  setCardCount(
    'cardCaution',
    caution
  );

  setCardCount(
    'cardQualified',
    qualified
  );

  const labels = {
    tabBtnAll:
      `All Stocks (${all})`,

    tabBtnUpcoming:
      `Awaiting Results (${upcoming})`,

    tabBtnPost:
      `Results Declared (${declared})`,

    tabBtnCaution:
      `Caution / Priced In (${caution})`,

    tabBtnQualified:
      `Fully Qualified (${qualified})`
  };

  Object.entries(
    labels
  ).forEach(
    ([id, label]) => {

      const el =
        $(id);

      if (el) {
        el.textContent =
          label;
      }
    }
  );
}


function setTabClasses() {
  const ids = {
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
    ids
  ).forEach(
    ([tab, id]) => {

      const el =
        $(id);

      if (!el) {
        return;
      }

      el.classList.toggle(
        'tab-active',
        currentStageTab ===
        tab
      );
    }
  );

  const cards = {
    ALL:
      'cardAll',

    'Post-results':
      'cardPostResults',

    Upcoming:
      'cardUpcoming',

    Caution:
      'cardCaution',

    Qualified:
      'cardQualified'
  };

  Object.entries(
    cards
  ).forEach(
    ([tab, id]) => {

      const el =
        $(id);

      if (el) {
        el.classList.toggle(
          'card-active',
          currentStageTab ===
          tab
    
