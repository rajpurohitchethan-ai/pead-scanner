(() => {
  'use strict';

  let DATA = null;
  let currentFilter = 'DECLARED';

  const $ = id => document.getElementById(id);

  function esc(v) {
    return String(v ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[ch]));
  }

  function n(v) {
    if (v === null || v === undefined || v === '') return null;
    const x = Number(v);
    return Number.isFinite(x) ? x : null;
  }

  function pct(v) {
    const x = n(v);
    return x == null ? '—' : `${x > 0 ? '+' : ''}${x.toFixed(1)}%`;
  }

  function xfmt(v) {
    const x = n(v);
    return x == null ? '—' : `${x.toFixed(2)}x`;
  }

  function money(v) {
    const x = n(v);
    return x == null ? '—' : `₹${x.toFixed(2)}`;
  }

  function dateFmt(v) {
    if (!v) return '—';
    const d = new Date(v);
    return Number.isNaN(d.getTime())
      ? esc(v)
      : d.toLocaleDateString('en-IN', {
          day: '2-digit',
          month: 'short',
          year: 'numeric'
        });
  }

  function dateTimeFmt(v) {
    if (!v) return '—';
    const d = new Date(v);
    return Number.isNaN(d.getTime())
      ? esc(v)
      : d.toLocaleString('en-IN', {
          day: '2-digit',
          month: 'short',
          hour: '2-digit',
          minute: '2-digit'
        });
  }

  function tone(label) {
    const s = String(label || '').toUpperCase();

    if (
      s.includes('GENUINE') ||
      s.includes('LOW EXPECTATIONS') ||
      s === 'LOW' ||
      s === 'ATTRACTIVE' ||
      s === 'CONFIRMED' ||
      s.includes('HIGH-CONVICTION') ||
      s.includes('CONFIRMED AGAIN') ||
      s.includes('SECOND-CHANCE') ||
      s.includes('PEAD SETUP STRONG') ||
      s.includes('SURPRISE ROOM')
    ) {
      return 'good';
    }

    if (
      s.includes('LOW QUALITY') ||
      s.includes('PRICED IN') ||
      s.includes('PRICED-IN') ||
      s === 'EXCESSIVE' ||
      s === 'NEGATIVE' ||
      s === 'EXTREME' ||
      s === 'ELEVATED' ||
      s.includes('EXECUTION BROKE') ||
      s.includes('AVOID')
    ) {
      return 'bad';
    }

    return 'warn';
  }

  function badge(label) {
    return `<span class="badge ${tone(label)}">${esc(label || 'UNVERIFIED')}</span>`;
  }

  function metricRows(item) {
    const pc = item.priceContext || {};
    const vm = item.valuationReality?.metrics || {};
    const fs = item.fundamentalSnapshot || {};
    const gap = item.expectationGap || {};

    const revenueLabel = item.resultsReleased
      ? 'Revenue YoY'
      : 'Previous Revenue YoY';

    const patLabel = item.resultsReleased
      ? 'PAT YoY'
      : 'Previous PAT YoY';

    const rvolLabel = pc.rvolIsPartial
      ? 'Intraday RVOL*'
      : 'RVOL';

    return `
      <div class="metric"><span>${revenueLabel}</span><b>${pct(fs.revenueYoYCalc)}</b></div>
      <div class="metric"><span>${patLabel}</span><b>${pct(fs.patYoYCalc)}</b></div>
      <div class="metric"><span>Pre-result 5D</span><b>${pct(gap.pre5dPct)}</b></div>
      <div class="metric"><span>Pre-result 10D</span><b>${pct(gap.pre10dPct)}</b></div>
      <div class="metric"><span>Pre-result 20D</span><b>${pct(gap.pre20dPct)}</b></div>
      <div class="metric"><span>Result day</span><b>${pct(pc.resultDayPct)}</b></div>
      <div class="metric"><span>${rvolLabel}</span><b>${xfmt(pc.relativeVolume)}</b></div>
      <div class="metric"><span>52W high distance</span><b>${pct(pc.distanceFrom52wHighPct)}</b></div>
      <div class="metric"><span>Expectation burden</span><b>${n(gap.burdenScore) == null ? '—' : `${Math.round(n(gap.burdenScore))}/100`}</b></div>
      <div class="metric"><span>P/E</span><b>${n(vm.trailingPE) == null ? '—' : n(vm.trailingPE).toFixed(1) + 'x'}</b></div>
      <div class="metric"><span>Forward P/E</span><b>${n(vm.forwardPE) == null ? '—' : n(vm.forwardPE).toFixed(1) + 'x'}</b></div>
      <div class="metric"><span>PEG</span><b>${n(vm.peg) == null ? '—' : n(vm.peg).toFixed(2)}</b></div>
      <div class="metric"><span>ROE</span><b>${pct(vm.roePct)}</b></div>
      <div class="metric"><span>FCF Yield</span><b>${pct(vm.fcfYieldPct)}</b></div>
      <div class="metric"><span>Entry</span><b>${money(item.entry)}</b></div>
      <div class="metric"><span>SL</span><b>${money(item.sl)}</b></div>
      <div class="metric"><span>TSL</span><b>${money(item.tsl)}</b></div>
    `;
  }

  function evidenceList(title, arr, cls) {
    if (!Array.isArray(arr) || !arr.length) return '';
    return `
      <div class="evidence ${cls}">
        <h4>${esc(title)}</h4>
        <ul>${arr.slice(0, 6).map(x => `<li>${esc(x)}</li>`).join('')}</ul>
      </div>
    `;
  }

  function quarterMemoryHtml(item) {
    const memory = item.quarterMemory || {};
    const events = Array.isArray(memory.recentEvents) ? memory.recentEvents : [];

    if (!events.length) {
      return `<div class="muted">No prior quarter history yet.</div>`;
    }

    return `
      <div class="timeline">
        ${events.map(event => `
          <div class="timeline-row">
            <div>
              <b>${esc(event.quarter || event.resultDate || 'Tracked quarter')}</b>
              <span>${esc(event.resultReality || 'AWAITING')}</span>
            </div>
            <div class="timeline-right">
              <b>${esc(event.expectationGap || 'UNVERIFIED')}</b>
              <span>${esc(event.verdict || '—')}</span>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  }

  function card(item) {
    const gap = item.expectationGap || {};
    const memory = item.quarterMemory || {};
    const phase = item.jCurvePhase || {};
    const action = item.actionBias || {};
    const verification = item.resultVerification || {};
    const pc = item.priceContext || {};

    const commentary = item.managementCommentary
      ? `<div class="commentary"><b>Management / source commentary:</b> ${esc(item.managementCommentary)}</div>`
      : `<div class="commentary muted"><b>Management commentary:</b> not verified in source data.</div>`;

    const rvolNote = pc.rvolIsPartial
      ? `<div class="micro-note">* Intraday RVOL is a time-adjusted estimate and becomes normal full-day RVOL after market close.</div>`
      : '';

    return `
      <article class="stock-card">
        <div class="stock-head">
          <div>
            <div class="symbol">${esc(item.symbol)}</div>
            <div class="name">${esc(item.name)} · ${esc(item.sector)}</div>
            <div class="sub">
              ${esc(item.quarter)} · Result ${dateFmt(item.resultDate)} · Base ${esc(item.baseScoreText)}
            </div>
          </div>
          <div class="conviction">
            <div>${esc(item.convictionScore)}<span>/100</span></div>
            <small>CONVICTION</small>
          </div>
        </div>

        <div class="reality-grid">
          <div><small>RESULT REALITY</small>${badge(item.resultReality?.label)}</div>
          <div><small>EXPECTATION REALITY</small>${badge(item.expectationReality?.label)}</div>
          <div><small>VALUATION REALITY</small>${badge(item.valuationReality?.label)}</div>
          <div><small>PRICE RESPONSE</small>${badge(item.priceResponse?.label)}</div>
        </div>

        <div class="context-grid">
          <div>
            <small>EXPECTATION GAP</small>
            ${badge(gap.label)}
            <span>Surprise room: ${esc(gap.surpriseRoom || 'UNVERIFIED')}</span>
          </div>
          <div>
            <small>QUARTER MEMORY</small>
            ${badge(memory.status)}
            <span>${esc(memory.trackedQuarterCount ?? 0)} tracked quarter(s)</span>
          </div>
          <div>
            <small>J-CURVE PHASE</small>
            <b>${esc(phase.label || 'IN REVIEW')}</b>
            <span>${esc(phase.note || '')}</span>
          </div>
        </div>

        <div class="verdict ${tone(item.verdict)}">${esc(item.verdict)}</div>

        <div class="action-bias ${tone(action.label)}">
          <b>${esc(action.label || 'IN REVIEW')}</b>
          <span>${esc(action.reason || '')}</span>
        </div>

        <div class="metrics">
          ${metricRows(item)}
        </div>

        ${rvolNote}

        <div class="freshness">
          <div>
            <span>RESULT EVIDENCE</span>
            <b class="${verification.official ? 'good' : 'warn'}">${esc(verification.label || 'UNVERIFIED')}</b>
          </div>
          <div>
            <span>RESULT VERIFIED AT</span>
            <b>${dateTimeFmt(verification.verifiedAt)}</b>
          </div>
          <div>
            <span>PRICE UPDATED AT</span>
            <b>${dateTimeFmt(item.priceTimestamp)}</b>
          </div>
          <div>
            <span>VALUATION CONFIDENCE</span>
            <b>${esc(item.valuationReality?.confidence || 'LIMITED')}</b>
          </div>
        </div>

        <div class="evidence-grid">
          ${evidenceList('WHY IT WORKS', item.reasons, 'positive')}
          ${evidenceList('RISKS / WHAT TO VERIFY', item.risks, 'risk')}
          ${evidenceList('EXPECTATION SIGNALS', gap.reasons, 'positive')}
          ${evidenceList('EXPECTATION OFFSETS', gap.offsets, 'neutral')}
        </div>

        ${commentary}

        <details class="quarter-history">
          <summary>Quarter-to-quarter memory</summary>
          ${quarterMemoryHtml(item)}
        </details>
      </article>
    `;
  }

  function filteredItems() {
    const items = DATA?.items || [];
    const q = ($('search')?.value || '').trim().toLowerCase();

    return items.filter(item => {
      let ok = true;

      if (currentFilter === 'DECLARED') ok = item.resultsReleased === true;
      if (currentFilter === 'HIGH') ok = item.verdict === 'HIGH-CONVICTION PEAD CANDIDATE';
      if (currentFilter === 'PRICED') ok = item.expectationReality?.label === 'PRICED IN';
      if (currentFilter === 'LOW_GAP') ok = item.expectationGap?.label === 'LOW';
      if (currentFilter === 'HIGH_GAP') {
        ok = ['ELEVATED', 'EXTREME'].includes(item.expectationGap?.label);
      }
      if (currentFilter === 'SECOND_CHANCE') {
        ok = item.quarterMemory?.status === 'SECOND-CHANCE CONFIRMATION';
      }
      if (currentFilter === 'CONFIRMED_AGAIN') {
        ok = ['CONFIRMED AGAIN', 'REPEATED HIGH CONVICTION']
          .includes(item.quarterMemory?.status);
      }
      if (currentFilter === 'UPCOMING') ok = item.resultsReleased !== true;
      if (currentFilter === 'ALL') ok = true;

      const searchable = [
        item.symbol,
        item.name,
        item.sector,
        item.verdict,
        item.resultReality?.label,
        item.expectationReality?.label,
        item.valuationReality?.label,
        item.expectationGap?.label,
        item.quarterMemory?.status,
        item.jCurvePhase?.label,
        item.actionBias?.label
      ].join(' ').toLowerCase();

      return ok && (!q || searchable.includes(q));
    });
  }

  function render() {
    if (!DATA) return;

    const c = DATA.counts || {};
    $('total').textContent = c.total ?? 0;
    $('declared').textContent = c.resultsDeclared ?? 0;
    $('genuine').textContent = c.genuineResults ?? 0;
    $('priced').textContent = c.pricedIn ?? 0;
    $('high').textContent = c.highConviction ?? 0;
    $('lowGap').textContent = c.expectationGapLow ?? 0;
    $('highGap').textContent = c.expectationGapElevated ?? 0;
    $('secondChance').textContent = c.secondChance ?? 0;

    $('generated').textContent = dateTimeFmt(DATA.generatedAt);

    const items = filteredItems();
    $('count').textContent = `${items.length} records`;

    $('cards').innerHTML = items.length
      ? items.map(card).join('')
      : `<div class="empty">No records in this view. The working PEAD radar is unaffected.</div>`;

    document.querySelectorAll('[data-filter]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.filter === currentFilter);
    });
  }

  async function load() {
    try {
      const r = await fetch(`./intelligence.json?t=${Date.now()}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);

      DATA = await r.json();

      if (!Array.isArray(DATA.items) || !DATA.items.length) {
        throw new Error('intelligence.json contains 0 rows');
      }

      render();
    } catch (err) {
      $('cards').innerHTML = `
        <div class="empty">
          Intelligence layer unavailable: ${esc(err.message)}.<br>
          Your main PEAD radar and data.json are not affected.
        </div>
      `;
    }
  }

  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-filter]');
    if (!btn) return;

    currentFilter = btn.dataset.filter;
    render();
  });

  $('search')?.addEventListener('input', render);
  $('reload')?.addEventListener('click', load);

  load();
})();
        
