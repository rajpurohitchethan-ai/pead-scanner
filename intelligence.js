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
  if (v === null || v === undefined || v === '') {
    return null;
  }

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

  function tone(label) {
    const s = String(label || '').toUpperCase();

    if (
      s.includes('GENUINE') ||
      s.includes('LOW EXPECTATIONS') ||
      s === 'ATTRACTIVE' ||
      s === 'CONFIRMED' ||
      s.includes('HIGH-CONVICTION')
    ) {
      return 'good';
    }

    if (
      s.includes('LOW QUALITY') ||
      s.includes('PRICED IN') ||
      s === 'EXCESSIVE' ||
      s === 'NEGATIVE'
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

    return `
      <div class="metric"><span>Revenue YoY</span><b>${pct(item.revenueYoY ?? fs.revenueYoYCalc)}</b></div>
      <div class="metric"><span>PAT YoY</span><b>${
        item.patYoYTurnaround || fs.patYoYTurnaround
          ? 'TURNAROUND'
          : pct(item.patYoY ?? fs.patYoYCalc)
      }</b></div>
      <div class="metric"><span>Pre-result 20D</span><b>${pct(pc.pre20dPct)}</b></div>
      <div class="metric"><span>Result day</span><b>${pct(pc.resultDayPct)}</b></div>
      <div class="metric"><span>RVOL</span><b>${xfmt(pc.relativeVolume)}</b></div>
      <div class="metric"><span>52W high distance</span><b>${pct(pc.distanceFrom52wHighPct)}</b></div>
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

  function card(item) {
    const commentary = item.managementCommentary
      ? `<div class="commentary"><b>Management / source commentary:</b> ${esc(item.managementCommentary)}</div>`
      : `<div class="commentary muted"><b>Management commentary:</b> not verified in source data.</div>`;

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

        <div class="verdict ${tone(item.verdict)}">${esc(item.verdict)}</div>

        <div class="metrics">
          ${metricRows(item)}
        </div>

        <div class="evidence-grid">
          ${evidenceList('WHY IT WORKS', item.reasons, 'positive')}
          ${evidenceList('RISKS / WHAT TO VERIFY', item.risks, 'risk')}
        </div>

        ${commentary}
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
      if (currentFilter === 'UPCOMING') ok = item.resultsReleased !== true;
      if (currentFilter === 'ALL') ok = true;

      const searchable = [
        item.symbol,
        item.name,
        item.sector,
        item.verdict,
        item.resultReality?.label,
        item.expectationReality?.label,
        item.valuationReality?.label
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

    $('generated').textContent = DATA.generatedAt
      ? new Date(DATA.generatedAt).toLocaleString('en-IN')
      : '—';

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
