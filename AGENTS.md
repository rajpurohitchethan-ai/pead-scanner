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
