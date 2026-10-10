AGENTS.md — PEAD Scanner Engineering Rules
Project
Repository: rajpurohitchethan-ai/pead-scanner
Before changing code, inspect the current repository, especially pead_v2.py, data.json, intelligence.json, intelligence.html, intelligence.js, app.js, companies.json, and .github/workflows/.
Do not patch only the screenshot symptom. Trace bugs through UI → published JSON → event store → computation → raw source. Add a regression test reproducing the bug before fixing it.
Core architecture
Use a persistent earnings-event store, not a fresh hourly snapshot. Stable identity: ISIN + fiscal period end. Maintain events/, raw/, logs/, master/symbols.json, and run_health.json.
A failed/null fetch must NEVER erase a verified value. Only a newer valid value from an equal or better source may replace it.
Scheduled vs declared results
Keep scheduled_result_date, result_date, and filing_timestamp separate. A board-meeting/result-calendar date does NOT prove release. resultsReleased=true only after actual NSE/BSE filing evidence.
Future/unreleased results must never have result-day return, RVOL, result-day high/low, post-result hold, Darvas post-result box, or post-result entry trigger. After-market filing uses the next trading session; pre-market filing uses the same session.
Live quarter vs archive
Live dashboard must show only the current reporting quarter. In Oct 2026, 30-Sep-2026 = Q2 FY27. Q1 FY27 and older events remain archived for history/backtesting but must not inflate live counts or consume hourly enrichment.
Source precedence
Result release/date: NSE/BSE actual filing > anything else. Financials: Exchange XBRL/XML > NSE/BSE official result table > verified fallback. Price/volume: NSE/BSE official > Yahoo fallback. Yahoo must never prove official result release. Prefer consolidated figures when available and record CONSOLIDATED/STANDALONE.
Missing values
Missing is not zero. Unavailable values remain null in JSON and render as —. Never show fake 0.0 values for Forward P/E, ROE, FCF Yield, Entry, SL, TSL, or missing 10D/20D returns.
PAT model
Do not calculate ordinary YoY % when prior PAT <= 0. Use: PROFIT_GROWTH, PROFIT_DECLINE, TURNAROUND, DETERIORATION, LOSS_NARROWING, LOSS_WIDENING.
PEAD core
Prioritize: earnings strength, pre-result expectation, result reaction + RVOL, post-result price acceptance. Valuation, sector, cash flow, and commentary are secondary modifiers.
Expectation inputs: 5D, 10D, 20D, distance from 52W high. Partial history must be labeled honestly, e.g. ELEVATED SHORT-TERM / PARTIAL HISTORY.
Entry system
Do not generate an entry just because results are strong. Preferred entries: confirmed reaction + holds result-day low + Darvas/PDH breakout, or pullback/reversal near 10 EMA followed by breakout confirmation.
Entry states: WAIT_RESULT, DATA_PENDING, WAIT_REACTION, WAIT_BOX, WAIT_ACCEPTANCE, WAIT_RECLAIM, WATCH_BREAKOUT, NEAR_ENTRY, ENTRY_TRIGGERED, NO_ENTRY.
Track current price, entry trigger, distance-to-trigger %, signal time, SL and TSL. If no valid setup: Entry —, SL —, TSL —.
Market cap and sector
Minimum market cap: ₹1,000 crore. Unknown market cap may stay in the database but must not qualify as a trade candidate. Sector tailwind only when the relevant sector index is above 10/20/50/200 DMA.
Dashboard
Preserve the existing design. Required filters: Results Declared, High Conviction, Priced In, Data Pending, Entry Signals, Upcoming.
Required asc/desc sorting: Conviction, Completeness, Result date, Revenue YoY, PAT YoY, Reaction %, RVOL, Current price, Distance to entry. Null values must sort safely.
Data health
Show active events, stored/archive events, results filed, financials parsed, reaction ready, fully scored, completeness %, and failures by source.
Publish protection
Never replace a healthy feed with a broken run. Block publication on severe unexpected deterioration: zero current-quarter events, >20% unexpected current-quarter event drop, >20% unexpected declared-result drop, major completeness collapse, corrupted JSON. Make checks quarter-aware.
Performance
Do not enrich the entire history hourly. Prioritize current-quarter declared/upcoming results, incomplete recent events, and active PEAD/entry-watch candidates. Archive old completed events. Log stored, active, selected, deferred, archived.
Do not regress
Calendar date alone never proves release.
Declared results persist across failed fetches.
Missing evidence does not become LOW QUALITY automatically.
PAT loss→profit is TURNAROUND.
BSE-only names are supported.
Q1 does not clutter live Q2.
Historical events stay stored.
Reaction day respects filing time.
Missing values remain null/—.
Source precedence is preserved.
Failed sources never erase good values.
Future scheduled results never show reaction data.
Testing
Do not call a fix complete because it compiles. Add deterministic regression tests for non-null merge, source precedence, NSE/BSE identity mapping, BSE-only ticker, PAT turnaround, future scheduled result cannot appear declared, after-market reaction session, Q2 FY27 mapping, Q1 archive/Q2 live filtering, null handling, valuation input sufficiency, sorting with nulls, entry-signal transitions, publish-collapse protection, migration, and persistence across simulated API failure.
For every important bug: reproduce it, add a regression test, fix the root cause, then run the full relevant test suite.
Working style
Inspect the repository, make cohesive changes, test them, then report exact files changed, what changed, test results, remaining limitations, and deployment steps. Correctness and persistence are more important than filling every box. Prefer — / DATA PENDING over a plausible but wrong number.
Data integrity (engine 2.2)
Dates: ISO strings (YYYY-MM-DD…) are parsed without day-first heuristics. Never send ISO strings to pandas with dayfirst=True; that turned 2026-09-11 into 2026-11-09.
Period: a filing's period comes from the exchange period field (NSE toDate) or the filing text (BSE headline: "quarter ended 31.12.2025", "June 2026", "30th September", "Q2 FY27"). Date inference is a last resort and is labelled INFERRED_FROM_FILING_DATE. A filing dated before its period end is rejected.
Evidence: every declared event is re-verified each run by integrity_pass() against saved raw exchange discovery payloads. Missing/other-period/future evidence revokes the declaration (status REVOKED, values kept for audit). Promotion from SCHEDULED requires a filing that explicitly names the period.
Financials: each source's parse is stored as a complete snapshot in event.financialSnapshots and validated (VERIFIED / FLAGGED / REJECTED). Exactly one snapshot is applied to the visible fields; fields it lacks show — and are never borrowed from another source. XBRL: no dimensional contexts, 80–100 day durations only, exact concept names, comparatives from the same document. NSE comparison revenue = re_net_sale, not re_total_inc.
Publish gate: the baseline is the last PUBLISHED data.json, never run_health.json. A large drop caused by revocations needs `--approve-baseline <signature>` from `--integrity-report`; a wrong signature approves nothing.
Tests: python -m unittest discover -s tests -v (plus --self-test).

Engine 2.4 (analytics layer)
pead_plus.py holds pure analytics: extended price features (EMAs, HVQ/HVY/HVE, return since result, base integrity, forward returns, Q1 replay), earnings strength, Q1->Q2 buckets (Confirmation / Re-PEAD / Fresh PEAD, after @SureshKBN's framework), the trade plan (early / pullback / breakout entry, SL 1% under the result-day low, trail = SL until +1R then 21 EMA swing / 63 EMA position, max risk 10%), liquidity (₹1 Cr/day and ₹20), valuation vs sector P/E, sector tailwind from peers, market regime (Nifty 500 vs 50/200 DMA) and the scorecard. It never fetches and never invents values.
Data sources added: NSE quote (P/E, sector P/E, market cap, delivery %, sector index), BSE equityMetaInfo (sector, P/E, P/B, ROE, OPM), NSE Nifty 500 history (cached daily in master/index_nifty500.json), previous-quarter filing time per company (NSE per-symbol financial results / BSE per-scrip announcements). New per-event calls are capped by EXTRA_CALL_BUDGET per run.
NSE history now uses camelCase fields (chClosingPrice, mtimestamp); both spellings are parsed. NSE results_comparison returns empty/stale data (Dec 2024); YoY must come from XBRL in the filing (validate with --validate-financials).
Entry levels are published only when earnings strength is verified (STRONG / AVERAGE+) and the plan signal is actionable.
Tests: python -m unittest discover -s tests -v (test_integrity.py + test_plus.py).

Engine 2.5 (concalls)
enrich_concall() reads NSE (per symbol) or BSE (per scrip) announcements from 20 days before the result, classifies concall notice / transcript / audio (pead_plus.classify_call_filing) and stores event["concall"] = {status SCHEDULED|DONE|NONE_FOUND, callDate, noticeUrl, transcriptUrl, audioUrl}. Hybrid entry (2.5.1): while a call is SCHEDULED, clean numbers allow a STARTER entry (plan.stage="STARTER", sizeFraction 1/3) on a valid pattern; numbers with red flags (pead_plus.result_red_flags: turnaround, profit growth without revenue growth, margin jump without revenue growth, flagged figures) return WAIT_CONCALL with no levels. After the call, or when no call is filed, the plan is FULL size. A notice whose date cannot be read counts as pending for 5 days. event["tradeLog"] records the first triggered starter and full entries and tracks them (stop hits, return) for the "entry timing" scorecard.

Engine 2.5.2 (intraday filings)
A result filed during market hours (09:00-15:30 IST) has a two-session reaction window: the filing day plus the next session. reaction_session = next session; field reaction_window_start = filing day. Reaction return is measured from the close before the filing day; result-day high/low and RVOL span both sessions; the plan waits for the second session's close.

Engine 2.5.3 (early concall notices)
Notices are often filed a week before the result (TCS filed 01 Oct for an 08 Oct result). An undated notice now stays pending until max(notice + 5 days, result + 2 days), stored as concall.pendingUntil. call_date_from_text prefers a date after the filing day over the letter's own date, and a parsed call date before the result day is treated as undated. Concalls are re-checked every 6 h until a transcript or recording is found.
Duplicates (2.5.3): merge_duplicate_events groups events of one period that share an NSE symbol / BSE scrip id / BSE code, guarded by same_company() (same ISIN issuer code, or matching names). Survivor = has ISIN, then NSE symbol, then most fields; the duplicate's id is written to events/_aliases.map (EventStore.resolve/ensure_event use it, so discovery never re-creates it), its ISIN to security.altIsins and its id to mergedFrom (raw price folders are found through it). The quality gate adds health.integrity.duplicatesMerged back before the row-count check, so folds never need --approve-baseline. First run folds 82 copies (379 -> 297 rows).
Result card (2.5.3): when the filing has no prior-year column, plus.margins.basis = "QoQ" and the card shows "Result vs last quarter" (revenueQoQ, patQoQ) instead of dashes. Script version v=28.
Also in 2.5.3: valuation.tinyBook (ROE > 100% or P/B > 50, e.g. ONIXSOLAR P/B 327) shows ROE as "n/m"; pead_plus.recheck_concall() re-opens a stored undated "held" verdict (no transcript/recording) until result + 2 days without waiting for a fetch.

Engine 2.5.4 (wrong-company NSE lookups)
NSE lookup is a fuzzy search; fill_security_identity used rows[0], so BSE symbols got another company's NSE symbol (ALKALI->GUJALKALI, AGRITECH->DHANUKA, DEEPA->DEEPAKNTR, NOVIS->INNOVISION, ABAN->MAHABANK, SWARAJ->SWARAJENG, MUDRA->MYMUDRA, WINSOME->WINSOMYARN, BRIGHT->BCG, DEN->ICICIAMC) and that company's prices/quote/sector. pick_lookup_row() now accepts only an exact symbol or a name match. repair_lookup_mismatches() (start of integrity_pass) uses saved raw/nse/NSE_<Q>_*/lookup payloads to find these, drops the wrong NSE symbol and every NSE/Yahoo-sourced field, plus/priceTrail/concall, wrong aliases and master entries, and rekeys; reason IDENTITY_REPAIRED. rebuild_plus_from_raw skips NSE price files whose chSymbol is not the event's nseSymbol. Health: integrity.revoked counts declaration revocations only; duplicatesMerged / identityRepaired are separate tiles; the gate adds back only folds made before discovery. EventStore.resolve ignores dangling aliases.

Engine 2.6.0 (full audit, 8 Oct 2026)
- Financials: NSE's date-filtered results listing returns a few stale rows, so no NSE XBRL was ever fetched (every NSE result showed "—"). nse_result_listing() now asks per company WITHOUT dates (cached as event.nseResultListing, refreshed every 3 h until the quarter's filing appears), records the filing, declares NSE-only companies, and is release evidence for the integrity pass. fill_comparatives_from_listing() reads YoY / QoQ figures from the year-ago / previous-quarter XBRL filing (same basis, same concept) when the current document has no comparatives.
- Market regime: NSE index history returns <=70 sessions per request; refresh_index_cache fetches 80-day windows. Regime is hidden when index data is >7 days old; 200 DMA needs 200 sessions.
- Enrichment rotation: _enrichment_priority = (bucket, tier, distance), tier 0 never priced, 1 price due, 2 only meta due, 3 nothing due. HEAVY_UPCOMING_DAYS = 45 (= dashboard window), so every listed company gets prices/sector.
- NSE price history only for a confirmed nseSymbol (a BSE scrip id can be another company's NSE symbol).
- Splits/bonus: pead_plus.adjust_corporate_actions() on every NSE/BSE frame (gap to a standard ratio, whole session below the old close). PLUS_VERSION forces one rebuild of stored analytics; replay now picks the payload with the latest session (not file mtime) and refreshes run-up / 52-week / turnover fields.
- Sectors: pead_plus.canonical_sector() maps exchange, legacy BSE and Yahoo labels to NSE's ~22 sectors; the card tag shows "Sector · industry".
- Valuation: ROE > 100% or negative book shows "n/m" (bookNote); high P/B alone no longer does.
- Install check: pead_plus.MODULE_VERSION and the page's PAGE_VERSION must equal ENGINE_VERSION; a red banner names the out-of-date file.
- Health tab: price-history / sector coverage and market-data date tiles.

Engine 2.6.1 (NSE Integrated Filing)
- Since the Mar-2025 quarter SEBI moved quarterly results to NSE "Integrated Filing - Financials" (/api/integrated-filing-results?index=equities&symbol=X&type=Integrated Filing- Financials). The old corporates-financial-results and results-comparision indexes stop at Dec-2024, which is why every NSE result showed "—". NSEAdapter.integrated_filings() + normalize_nse_integrated() (qe_Date, consolidated, broadcast_Date/revised_Date, xbrl; governance rows and /corporate/null links dropped) feed nse_result_listing (cache v=2), filings, NSE-only declarations (source NSE_INTEGRATED_FILING, release rank = NSE_FINANCIAL_RESULTS) and integrity evidence (raw/nse/*/integrated_filings-*.json). The old index is the fallback.
- XBRL parser verified on real filings (tests/fixtures/nse, from github.com/Aman4563/finresearch, MIT): company (INFY), bank (HDFCBANK: interest earned; owners' PAT = ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates; EPS BasicEarningsPerShareAfterExtraordinaryItems), insurers (ICICIGI, SBILIFE: premium income, ProfitLossAfterTax / ...AndExtraordinaryItems) and the pre-2025 format whose year-to-date context ("FourD") carries quarter dates (conflicts resolved to the "One*" contexts).
- tests/test_integrity.py restored (it had been overwritten with test_plus content); tests/test_plus.py added.
- index.html is the radar page (same file as intelligence.html).
- Self-audit: pead_plus.self_audit() checks every published run (same-release files, fresh index data, one row per company, no reaction data before a result, declared results have revenue/profit and YoY, reaction measured after its session, price/sector coverage, implausible values). Result in health.selfAudit and logs/self_audit.json; FAIL items show a red banner and the Data health tab lists every check. Read logs/self_audit.json first when auditing.

Engine 2.6.2 (first live run of 2.6.1, 9 Oct 2026)
- Live result: TCS Q2 FY27 read from NSE Integrated Filing XBRL (revenue ₹73,188 Cr +11.2% YoY, PAT ₹13,884 Cr +15.0%).
- Owners' PAT filed as 0 (GMBREW consolidated: owners 0, profit for period ₹39.29 Cr) -> total profit is used (issue OWNERS_PAT_ZERO_USED_TOTAL).
- Like-for-like YoY: when the year-ago quarter exists only STANDALONE, this quarter's STANDALONE filing is used (GMBREW started consolidated filing in 2026). XBRL snapshots are re-read when XBRL_PARSER_VERSION changes and every 3 h while YoY is missing (first 10 days after the result).
- session_closed(): a reaction session counts only after 15:45 IST on that day (at 06:20 the card said "Data pending" instead of "Awaiting reaction session").
- Unit check: a profit swing with revenue in line with last quarter / last year is PAT_SWING (flagged), not UNIT_SUSPECT (rejected) (LOTUSCHO).
- Untraded scrips (no trade in 20+ days: TIAANC, GOLKONDA, ALSTONE) get NO_ENTRY "No trades since ..." and are excluded from the self-check's reaction/financial FAIL checks (listed separately).

Engine 2.7.0 (new signals, 9 Oct 2026)
- Earnings acceleration: fill_comparatives_from_listing also reads the filing a year before the previous quarter, so prev_q_revenue_yoy_pct / prev_q_pat_yoy_pct (last quarter's own YoY) are stored; fallback = the archived previous-quarter event's YoY. pead_plus.earnings_acceleration(): ACCELERATING when YoY growth rose >= 5 pp (revenue and/or ordinary profit, both still growing), DECELERATING when it fell >= 5 pp, else STEADY. Card tag + "earnings acceleration" sort. Display only: conviction score unchanged.
- Volume signature: "volume signature" sort ranks HVE > HVY > HVQ, then relative volume.
- Relative strength: plus.relativeStrength = return since result minus NIFTY 500 return over the same sessions (close before the result window -> latest close; extended_features now records pre_result_session). Card line + "strength vs NIFTY 500" sort.
- Live entry status: refresh_live_quotes() reads NSE live prices during market hours (09:15-15:35 IST) for declared companies with an entry plan (max LIVE_QUOTE_MAX=40); plus.live = {price, at, entry, distancePct, state ABOVE_TRIGGER | NEAR_TRIGGER (within 2%) | BELOW_TRIGGER}, today's quotes only. Plans still use closing prices. A weekday 15:02 IST scheduled task reads it and sends the 3 pm entry alert.
- XBRL_PARSER_VERSION 4, PLUS_VERSION 2.7.0 (one re-read / rebuild).

Engine 2.8.0 (top-down check + peers, 9 Oct 2026)
- pead_plus.peer_context(items, regime) (run in publish, before the self-audit): plus.peers per company = rank of its 3-month move within its canonical sector (>= 4 priced peers; top third LEADER, bottom third LAGGARD, else MIDDLE), sector median, top movers, peers that already reported with their reaction and the average reaction.
- plus.topDown = six checks (ok / warn / bad / na) after the "top-down approach": market (NIFTY 500 regime), sector (tailwind), peers (laggard with peers reacting well = ok, leader = warn, reported peers falling = bad), trend (last vs 21/63 EMA), momentum (relative strength since the result, else 1-month return), valuation (label). Display + "top-down check" sort only; conviction score and entry rules unchanged. Capex/rerating, future triggers, FY27/28 EPS and fair value are deliberately NOT computed (no reliable free source) - the card shows them as the owner's homework with Screener / concall links.
- NO_ENTRY now states the exact reason (price move on the result, earnings strength with the thresholds, or quality flag).
- Relative strength is hidden when the index series ends before the stock's last session (was "index 0.0%"); the NIFTY 500 cache is re-fetched after 15:45 IST on weekdays until it contains the day's close (at most every 2 h).
- Page: Sectors rows expand to the ranked peer list; scorecard tables scroll horizontally on phones; "since result" line hidden on the reaction day; sorts added (top-down check, RVOL, revenue/profit YoY, price, completeness) with a low/high direction toggle and nulls always last.

Engine 2.9.0 (bottom-up approach additions, 9 Oct 2026)
- Peer fundamentals: pead_plus.peer_fundamentals() ranks a company's revenue / profit YoY against sector peers that have reported this quarter, and its operating margin (TTM, else quarter) and ROE (tiny-book excluded) against all sector peers; needs >= 3 values. plus.peerFundamentals, shown under the peers line.
- Market internals: refresh_market_internals() (each networked run) stores NIFTY 500 advances/declines (/api/equity-stockIndices-adu) and FII/DII provisional cash flows (/api/fiidiiTradeReact, parse_fii_dii) in master/market_internals.json (30-day flow history). pead_plus.market_internals() adds regime.breadth (share of tracked stocks above their 21 / 63-day EMA), regime.advanceDecline (if <= 3 days old) and regime.flows (latest, 5-day sums, FII buying/selling streak; hidden if > 5 days old). Shown under the market line and in the top-down market note; the market check's status is still the NIFTY 500 regime.
- PEAD cap: the sizing box takes "PEAD cap % of capital" (default 15%, per-viewer) after the tactical 10-20% bucket of the bottom-up portfolio split, and warns when one trade alone exceeds it.
- NSEAdapter._api_get() is the shared raw-endpoint helper.

Engine 2.9.1 (evening check, 9 Oct 2026)
- Wrong-company BSE code loop: NSE:BRIGHT (Bright Solar, NSE SME) was given BSE 543831 (Bright Outdoor Media) again every run through the stale alias BSE:543831 -> NSE:BRIGHT, then repaired again. The 2.9.0 repair also left Bright Outdoor's sector (Media), its P/E 30.85 (trailing_pe mislabelled NSE_QUOTE), mergedFrom and the master entry on Bright Solar.
- _repair_bse_lookup_mismatches now also calls _clean_wrong_bse_traces() (and does so for events repaired earlier: identityRepaired.wrongBseCode): sector keys / P/E equal to the other company's saved BSE meta (matched by the ISIN of the wrong lookup) are dropped, mergedFrom BSE:<code> removed, aliases BSE:<code>|P re-pointed to the event that owns the code (else dropped), master entry cleaned. repair_lookup_mismatches no longer returns early when there are no NSE lookup guesses, and saves the master every pass.
- ensure_event: when the BSE fallback key also resolves (via a stale alias) to a conflicting company, the alias is dropped and the BSE key is used.
- EventStore.resolve never redirects an id whose own event file exists (aliases are only left by deleted files; "NSE:BRIGHT" -> "NSE:BCG" from 2.5.4 would have hijacked Bright Solar once a BCG event appeared).
- enrich_exchange_meta labels trailing_pe with the source of exchange_pe (BSE_META for SME scrips whose NSE quote has no P/E).
- Tests: tests/test_plus.py BseWrongCodeLoopTests. Two rows with symbol BRIGHT are two different companies (Bright Outdoor Media, BSE; Bright Solar, NSE) - not a duplicate.

Engine 2.9.2 (10 Oct 2026)
- Q1 strength from filings: build_plus took Q1 earnings strength only from the archived previous-quarter event. POONAWALLA has no Q1 event, so its Q1 (revenue +77.9%, PAT +391.6% YoY) counted as unknown and the card said "Fresh PEAD: Average Q1, sudden Q2 earnings pivot". When the archived event gives no strength, the current event's prev_q_revenue_yoy_pct / prev_q_pat_yoy_pct (2.7.0, from the exchange filings) are used. POONAWALLA -> Re-PEAD (conviction 59 -> 61), GMBREW -> Confirmation (46 -> 52; still NO_ENTRY after its -10% reaction). Bucket rules unchanged.
- classify_bucket(q1_strength=None) no longer claims "Average Q1": FRESH_PEAD with unknown Q1 reads "Q1 numbers not verified and no Q1 price setup; Q2 earnings pivot."
- Loss-makers' P/E: NSE quotes P/E 0 (or negative) for loss-makers (CROMPTON, BAJAJELEC, SOLARA, TRF, SWSOLAR, ~40 negatives); the card showed "0.0x" / "-32.7x". valuation_view publishes pe = null when <= 0 (label stays LOSS-MAKING) and the data row's trailingPE is null when <= 0.
- Tests: Q1StrengthFromFilingsTests, LossMakerPeTests.
- Open (owner's call): peer_context / sector_stats count every reported peer, including untraded shells (INDBNK: revenue ₹0.26 Cr, turnover ~0), so one micro-cap's -7.2% sets POONAWALLA's peers check to "bad" and Financial Services' average reaction.
