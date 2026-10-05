/* =========================================================
   AUTOMATIC PEAD CHECKLIST + STOCK MODAL
   Replace your current openModal/checklist-rendering code
   with this complete block.
   ========================================================= */

function renderChecklist(stock) {
  const grid = document.getElementById('mChecklistGrid');
  if (!grid) return;

  grid.innerHTML = '';

  const checks = Array.isArray(stock.checks) ? stock.checks : [];

  if (!checks.length) {
    grid.innerHTML = `
      <div class="col-span-full p-4 rounded-xl bg-dark-900 border border-dark-750">
        <div class="text-xs text-slate-400">
          Checklist data unavailable for this stock.
        </div>
      </div>
    `;
    return;
  }

  checks.forEach((check, index) => {
    const status = check.status || 'Pending';
    const title = check.title || `Check ${index + 1}`;
    const detail = check.detail || 'No additional data available.';

    const div = document.createElement('div');

    div.className =
      'p-2.5 rounded-xl bg-dark-900 border border-dark-750 ' +
      'flex flex-col justify-between space-y-1';

    div.innerHTML = `
      <div class="flex items-start justify-between gap-2">

        <span class="text-[10px] text-slate-400 uppercase font-semibold leading-tight">
          Check ${index + 1}: ${esc(title)}
        </span>

        <span
          class="shrink-0 px-2 py-0.5 rounded text-[10px] font-bold border font-mono ${bdg(status)}"
        >
          ${esc(status)}
        </span>

      </div>

      <p class="text-[10px] text-slate-300 leading-snug font-sans">
        ${esc(detail)}
      </p>
    `;

    grid.appendChild(div);
  });
}


/* =========================================================
   VERDICT BANNER CLASSES
   Static Tailwind classes are used so production builds
   do not accidentally remove dynamically generated classes.
   ========================================================= */

function getVerdictStyle(item) {

  if (item.peadStatus === 'QUALIFIED') {
    return {
      banner:
        'p-3.5 rounded-xl border border-emerald-500/40 ' +
        'bg-emerald-500/10 font-mono flex items-center justify-between gap-3',

      title:
        'text-sm font-extrabold text-emerald-400 mt-0.5',

      badge:
        'px-2.5 py-1 rounded-full text-xs font-bold border ' +
        'border-emerald-500/40 bg-emerald-500/20 text-emerald-300'
    };
  }

  if (isCaution(item)) {
    return {
      banner:
        'p-3.5 rounded-xl border border-rose-500/40 ' +
        'bg-rose-500/10 font-mono flex items-center justify-between gap-3',

      title:
        'text-sm font-extrabold text-rose-400 mt-0.5',

      badge:
        'px-2.5 py-1 rounded-full text-xs font-bold border ' +
        'border-rose-500/40 bg-rose-500/20 text-rose-300'
    };
  }

  if (
    item.peadStatus === 'AWAITING RESULTS' ||
    item.peadStatus === 'CONFIRMATION PENDING'
  ) {
    return {
      banner:
        'p-3.5 rounded-xl border border-amber-500/40 ' +
        'bg-amber-500/10 font-mono flex items-center justify-between gap-3',

      title:
        'text-sm font-extrabold text-amber-300 mt-0.5',

      badge:
        'px-2.5 py-1 rounded-full text-xs font-bold border ' +
        'border-amber-500/40 bg-amber-500/20 text-amber-300'
    };
  }

  return {
    banner:
      'p-3.5 rounded-xl border border-blue-500/40 ' +
      'bg-blue-500/10 font-mono flex items-center justify-between gap-3',

    title:
      'text-sm font-extrabold text-blue-300 mt-0.5',

    badge:
      'px-2.5 py-1 rounded-full text-xs font-bold border ' +
      'border-blue-500/40 bg-blue-500/20 text-blue-300'
  };
}


/* =========================================================
   OPEN STOCK MODAL
   ========================================================= */

function openModal(itemOrSymbol) {

  const item =
    typeof itemOrSymbol === 'string'
      ? fullRadarData.find(stock => stock.symbol === itemOrSymbol)
      : itemOrSymbol;

  if (!item) {
    console.warn('Stock not found:', itemOrSymbol);
    return;
  }

  activeModalStock = item;


  /* ---------------------------
     BASIC STOCK INFORMATION
     --------------------------- */

  const symbolEl = document.getElementById('mSymbol');
  const nameEl = document.getElementById('mName');
  const sectorEl = document.getElementById('mSector');
  const stageEl = document.getElementById('mStage');
  const evidenceEl = document.getElementById('mEvidence');

  if (symbolEl) symbolEl.innerText = item.symbol || '–';

  if (nameEl) nameEl.innerText = item.name || '–';

  if (sectorEl) {
    sectorEl.innerText =
      `${item.sector || 'Unknown sector'} · Mcap: ${item.mcap || 'n/a'}`;
  }

  if (stageEl) {
    stageEl.innerText =
      `${item.stage || 'Unknown'} (${item.resultDate || 'date unavailable'})`;
  }

  if (evidenceEl) {
    evidenceEl.innerText = item.evidence || '';
  }


  /* ---------------------------
     VERDICT
     --------------------------- */

  const banner = document.getElementById('mVerdictBanner');
  const title = document.getElementById('mVerdictTitle');
  const scoreBadge = document.getElementById('mScoreBadge');

  const verdictStyle = getVerdictStyle(item);

  if (banner) {
    banner.className = verdictStyle.banner;
  }

  if (title) {
    title.innerText = item.peadStatus || 'PENDING';
    title.className = verdictStyle.title;
  }

  if (scoreBadge) {
    scoreBadge.innerText = item.scoreText || '0 of 8 Checks Satisfied';
    scoreBadge.className = verdictStyle.badge;
  }


  /* =====================================================
     AUTOMATIC CHECKLIST

     NO:
     - Mark Satisfied
     - Mark Failed
     - Clear
     - manual checklist buttons

     Status comes directly from item.checks,
     which mapScan() calculates.
     ===================================================== */

  renderChecklist(item);


  /* ---------------------------
     THESIS / PEAD DETAILS
     --------------------------- */

  const thesisGrid = document.getElementById('mThesisGrid');

  if (thesisGrid) {

    thesisGrid.innerHTML = '';

    const thesis = Array.isArray(item.thesis)
      ? item.thesis
      : [];

    if (!thesis.length) {

      thesisGrid.innerHTML = `
        <div class="p-2 rounded-lg bg-dark-850 border border-dark-750">
          <div class="text-[10px] text-slate-400">
            Thesis data unavailable.
          </div>
        </div>
      `;

    } else {

      thesis.forEach(section => {

        const div = document.createElement('div');

        div.className =
          'p-2 rounded-lg bg-dark-850 border border-dark-750';

        div.innerHTML = `
          <div class="text-[10px] text-slate-400 uppercase font-semibold">
            ${esc(section.label || '')}
          </div>

          <div class="text-[10px] text-slate-200 mt-0.5 leading-snug">
            ${esc(section.val || '')}
          </div>
        `;

        thesisGrid.appendChild(div);
      });
    }
  }


  /* ---------------------------
     OPEN MODAL
     --------------------------- */

  const modal = document.getElementById('stockModal');

  if (modal) {
    modal.style.display = 'flex';
    modal.classList.remove('hidden');
  }
}


/* =========================================================
   CLOSE MODAL
   ========================================================= */

function closeModal() {

  const modal = document.getElementById('stockModal');

  if (modal) {
    modal.style.display = 'none';
    modal.classList.add('hidden');
  }

  activeModalStock = null;
}
