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
