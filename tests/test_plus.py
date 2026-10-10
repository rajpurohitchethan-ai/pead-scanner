"""Tests for the engine 2.4 analytics layer (pead_plus) and its wiring."""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402
import pead_plus as pp  # noqa: E402
import pead_v2 as p  # noqa: E402


def frame(closes, start=date(2026, 1, 1), vols=None, hl=True):
    rows = []
    d = start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"Date": pd.Timestamp(d), "Open": c * 0.99 if hl else None, "High": c * 1.01 if hl else None,
                     "Low": c * 0.98 if hl else None, "Close": c, "Volume": (vols[i] if vols else 1000)})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


class NseHistoryFormat(unittest.TestCase):
    def test_new_camelcase_fields_parse(self):
        rec = [{"chClosingPrice": 2287.4, "chOpeningPrice": 2285.7, "chTradeHighPrice": 2340, "chTradeLowPrice": 2252.5,
                "chTotTradedQty": 6878, "chSeries": "EQ", "mtimestamp": "20-Aug-2025"}]
        f = p.nse_history_to_frame(rec)   # returned None before engine 2.4
        self.assertIsNotNone(f)
        self.assertEqual(float(f["High"].iloc[0]), 2340)


class Features(unittest.TestCase):
    def setUp(self):
        closes = [100 + i * 0.1 for i in range(200)]
        vols = [1000] * 200
        closes[150] = closes[149] * 1.08      # result-day jump
        vols[150] = 9000                      # highest volume of the year
        for i in range(151, 200):
            closes[i] = closes[150] * (1 + (i - 150) * 0.002)
        self.f = frame(closes, vols=vols)
        self.rdate = self.f["Date"].iloc[150].date()

    def test_reaction_and_hv_flags(self):
        x = pp.extended_features(self.f, self.rdate, released=True)
        self.assertEqual(x["hv_label"], "HVY")
        self.assertFalse(x["base_broken"])
        self.assertEqual(x["sessions_since_reaction"], 49)
        self.assertAlmostEqual(x["return_since_result_pct"], 18.58, places=1)
        self.assertIsNotNone(x["fwd_20d_pct"])
        self.assertEqual(len(x["chart"]["close"]), 75)

    def test_unreleased_has_no_reaction_fields(self):
        x = pp.extended_features(self.f, self.rdate, released=False)
        self.assertNotIn("reaction_close", x)
        self.assertNotIn("hv_label", x)

    def test_q1_replay(self):
        q1 = self.f["Date"].iloc[150].date()
        x = pp.extended_features(self.f, None, released=False, q1_reaction_date=q1)
        self.assertAlmostEqual(x["q1_reaction_return_pct"], 8.0, places=1)
        self.assertTrue(x["q1_sustained"])
        self.assertTrue(pp.q1_setup(x["q1_reaction_return_pct"], x["q1_reaction_rvol"]))

    def test_base_broken(self):
        f = self.f.copy()
        f.loc[180, "Close"] = f.loc[150, "Low"] * 0.95
        x = pp.extended_features(f, self.rdate, released=True)
        self.assertTrue(x["base_broken"])


class Buckets(unittest.TestCase):
    def test_suresh_buckets(self):
        c = lambda **k: pp.classify_bucket(**k)["code"]
        self.assertEqual(c(released=True, q2_strength="STRONG", setup=True, sustained=True), "CONFIRMATION")
        self.assertEqual(c(released=True, q2_strength="STRONG", setup=True, sustained=False), "RE_PEAD")
        self.assertEqual(c(released=True, q2_strength="STRONG", setup=False, sustained=None), "FRESH_PEAD")
        self.assertEqual(c(released=True, q2_strength="WEAK", setup=True, sustained=True), "NO_CONFIRMATION")
        self.assertEqual(c(released=False, q2_strength=None, setup=True, sustained=False), "WATCH_REPEAD")
        self.assertIsNone(pp.classify_bucket(released=False, q2_strength=None, setup=None, sustained=None))

    def test_strength(self):
        self.assertEqual(pp.earnings_strength(25, 40, "PROFIT_GROWTH"), "STRONG")
        self.assertEqual(pp.earnings_strength(5, None, "TURNAROUND", 150), "STRONG")
        self.assertEqual(pp.earnings_strength(-5, -10, "PROFIT_DECLINE"), "WEAK")
        self.assertIsNone(pp.earnings_strength(None, None, None))


class Plan(unittest.TestCase):
    base = dict(released=True, reaction_traded=True, quality_ok=True, result_return=6, rvol=3,
                result_low=97, result_high=104)

    def test_levels_hidden_without_verified_earnings(self):
        r = pp.trade_plan(**(self.base | {"quality_ok": None}), x={"ema21": 100}, last_price=101)
        self.assertEqual(r["signal"], "DATA_PENDING")
        self.assertIsNone(r["entry"])

    def test_early_entry(self):
        x = {"sessions_since_reaction": 1, "reaction_close": 103, "ema10": 99, "ema21": 97, "ema63": 92}
        r = pp.trade_plan(**self.base, x=x, last_price=105)
        self.assertEqual(r["signal"], "ENTRY_EARLY")
        self.assertEqual(r["entry"], 104)
        self.assertEqual(r["sl"], 96.03)
        self.assertEqual(r["tslSwing"], 96.03)       # trail = SL until +1R

    def test_base_broken_is_no_entry(self):
        r = pp.trade_plan(**self.base, x={"base_broken": True}, last_price=90)
        self.assertEqual(r["signal"], "NO_ENTRY")

    def test_risk_too_wide(self):
        x = {"sessions_since_reaction": 1, "reaction_close": 130, "ema21": 120}
        r = pp.trade_plan(**(self.base | {"result_high": 131}), x=x, last_price=131)
        self.assertEqual(r["signal"], "RISK_TOO_WIDE")
        self.assertIsNone(r["entry"])

    def test_after_1r_trail_moves_to_cost_or_ema(self):
        x = {"sessions_since_reaction": 2, "reaction_close": 103, "ema10": 108, "ema21": 106, "ema63": 98}
        r = pp.trade_plan(**self.base, x=x, last_price=115)
        self.assertEqual(r["tslSwing"], 106)
        self.assertEqual(r["tslPosition"], 104)


class CrossSection(unittest.TestCase):
    def test_sector_tailwind(self):
        items = [{"sectorKey": "Cables", "ret63": 25, "released": True, "strength": "STRONG", "reaction": 6} for _ in range(3)]
        items += [{"sectorKey": "Cement", "ret63": -8, "released": False} for _ in range(3)]
        items += [{"sectorKey": "Other", "ret63": 5} for _ in range(5)]
        st = pp.sector_stats(items)
        self.assertEqual(st["Cables"]["tailwind"], "STRONG")
        self.assertEqual(st["Cement"]["tailwind"], "WEAK")

    def test_regime(self):
        up = [100 + i for i in range(220)]
        self.assertEqual(pp.market_regime(up)["label"], "RISK-ON")
        self.assertIsNone(pp.market_regime(up[:20])["label"])

    def test_liquidity_and_valuation(self):
        self.assertFalse(pp.liquidity(0.004, 80)["pass"])
        self.assertTrue(pp.liquidity(12, 450)["pass"])
        self.assertEqual(pp.valuation_view(15, 30, 18, 25)["label"], "ATTRACTIVE")
        self.assertEqual(pp.valuation_view(120, 30, 10, 5)["label"], "EXPENSIVE")


class ExchangeParsers(unittest.TestCase):
    def test_nse_quote_and_bse_meta(self):
        q = {"tradeInfo": {"totalMarketCap": 11776654496720.4, "deliveryToTradedQuantity": 58.82},
             "secInfo": {"pdSymbolPe": "15.43", "pdSectorPe": "15.21", "basicIndustry": "Private Sector Bank", "pdSectorInd": "NIFTY BANK"},
             "priceInfo": {"yearHigh": 1020.5}}
        r = p.parse_nse_quote(q)
        self.assertEqual(r["market_cap_cr"], 1177665.45)
        self.assertEqual(r["identity"]["sectorIndex"], "NIFTY BANK")
        b = p.parse_bse_meta({"PE": "102.65", "PB": "10.75", "ROE": "10.46", "OPM": "27.89", "IndustryNew": "Consumer Services", "ISubGroup": "E-Retail/ E-Commerce"})
        self.assertEqual(b["exchange_pe"], 102.65)
        self.assertEqual(b["identity"]["exchangeSector"], "Consumer Services")

    def test_bse_snapshot_margins(self):
        snap = {"currency_unit": "in Cr.", "results_in_crores": {"fields": ["title", "Sep-26", "Jun-26", "FY25-26"],
                "data": [["Revenue", "20.93", "23.74", "114.04"], ["Net Profit", "0.57", "0.65", "2.65"], ["EPS", "1.62", "1.84", "7.51"], ["OPM %", "7.98", "8.10", "6.43"]]}}
        r = p.parse_bse_snapshot(snap, date(2026, 9, 30))
        self.assertEqual(r["opm_pct"], 7.98)
        self.assertEqual(r["margin_change_bps"], -12)


class PreviousQuarterLookup(unittest.TestCase):
    def test_q1_date_from_filing_ts(self):
        with tempfile.TemporaryDirectory() as td:
            store = p.EventStore(Path(td) / "events")
            e = store.ensure_event(security={"symbol": "ABC", "nseSymbol": "ABC"}, period_end=date(2026, 9, 30))
            store.merge_field(e, "prev_quarter_result_ts", "2026-08-12T17:30:00+05:30", source="NSE_FINANCIAL_RESULTS")
            self.assertEqual(p.previous_quarter_reaction(store, e), date(2026, 8, 13))   # after close -> next session


class Concall(unittest.TestCase):
    def test_detection_and_dates(self):
        d = date(2026, 10, 8)
        self.assertEqual(pp.classify_call_filing("Intimation of Earnings Conference Call scheduled on 13th October, 2026"), "NOTICE")
        self.assertEqual(pp.call_date_from_text("con-call on 14-10-2026", d), date(2026, 10, 14))
        self.assertEqual(pp.classify_call_filing("Transcript of Earnings Call held on October 3, 2026"), "TRANSCRIPT")
        self.assertIsNone(pp.classify_call_filing("Outcome of Board Meeting - Financial results"))
        self.assertIsNone(pp.call_date_from_text("call on 01-01-2026", d))   # not 0-21 days after filing

    def test_status_transitions(self):
        from datetime import datetime
        notice = {"filedAt": datetime(2026, 10, 8, 18), "text": "Earnings call scheduled on October 13, 2026", "url": "n"}
        s1 = pp.concall_status([notice], date(2026, 10, 8), date(2026, 10, 10))
        self.assertEqual((s1["status"], s1["callDate"]), ("SCHEDULED", "2026-10-13"))
        s2 = pp.concall_status([notice], date(2026, 10, 8), date(2026, 10, 14))
        self.assertEqual(s2["status"], "DONE")
        s3 = pp.concall_status([notice, {"filedAt": datetime(2026, 10, 14), "text": "Transcript of earnings call", "url": "t"}],
                               date(2026, 10, 8), date(2026, 10, 14))
        self.assertEqual(s3["transcriptUrl"], "t")
        self.assertEqual(pp.concall_status([], date(2026, 10, 8), date(2026, 10, 9))["status"], "NONE_FOUND")

    def test_recheck_stored_held_verdict(self):
        old = {"status": "DONE", "callDate": None, "noticeUrl": "n", "transcriptUrl": None, "audioUrl": None}
        self.assertEqual(pp.recheck_concall(old, date(2026, 10, 8), date(2026, 10, 8))["status"], "SCHEDULED")
        self.assertEqual(pp.recheck_concall(old, date(2026, 10, 8), date(2026, 10, 11))["status"], "DONE")
        proven = old | {"transcriptUrl": "t"}
        self.assertEqual(pp.recheck_concall(proven, date(2026, 10, 8), date(2026, 10, 8))["status"], "DONE")

    def test_early_notice_stays_pending_until_after_result(self):
        # TCS-style: notice filed a week before the result, letter dated on the
        # filing day, call date not readable -> pending until result + 2 days.
        from datetime import datetime
        early = {"filedAt": datetime(2026, 10, 1, 20), "text": "October 1, 2026. Intimation of earnings call", "url": "n"}
        self.assertEqual(pp.call_date_from_text(early["text"], date(2026, 10, 1)), date(2026, 10, 1))
        s = pp.concall_status([early], date(2026, 10, 8), date(2026, 10, 8))
        self.assertEqual((s["status"], s["callDate"], s["pendingUntil"]), ("SCHEDULED", None, "2026-10-10"))
        self.assertEqual(pp.concall_status([early], date(2026, 10, 8), date(2026, 10, 11))["status"], "DONE")
        # Letter date plus the real call date: the later one wins.
        both = "Date: 01-10-2026. Earnings call on October 8, 2026 at 7 pm"
        self.assertEqual(pp.call_date_from_text(both, date(2026, 10, 1)), date(2026, 10, 8))

    def test_plan_waits_for_concall(self):
        x = {"sessions_since_reaction": 1, "reaction_close": 103, "ema10": 99, "ema21": 97, "ema63": 92}
        base = dict(released=True, reaction_traded=True, quality_ok=True, result_return=6, rvol=3, result_low=97, result_high=104)
        pending = {"status": "SCHEDULED", "callDate": "2026-10-13"}
        starter = pp.trade_plan(**base, x=x, last_price=105, concall=pending)
        self.assertEqual((starter["signal"], starter["stage"]), ("ENTRY_EARLY", "STARTER"))
        self.assertAlmostEqual(starter["sizeFraction"], 1 / 3)
        self.assertIn("1/3", starter["why"])
        wait = pp.trade_plan(**base, x=x, last_price=105, concall=pending, red_flags=["turnaround from a loss"])
        self.assertEqual(wait["signal"], "WAIT_CONCALL")
        self.assertIsNone(wait["entry"])
        full = pp.trade_plan(**base, x=x, last_price=105, concall={"status": "DONE"})
        self.assertEqual((full["signal"], full["stage"], full["sizeFraction"]), ("ENTRY_EARLY", "FULL", 1.0))
        nocall = pp.trade_plan(**base, x=x, last_price=105, concall={"status": "NONE_FOUND"})
        self.assertEqual(nocall["stage"], "FULL")

    def test_red_flags(self):
        self.assertEqual(pp.result_red_flags(rev_yoy=25, pat_yoy=40, pat_trend="PROFIT_GROWTH", margin_change_bps=120), [])
        self.assertIn("profit growth without revenue growth",
                      pp.result_red_flags(rev_yoy=3, pat_yoy=80, pat_trend="PROFIT_GROWTH", margin_change_bps=600))

    def test_trade_log_tracks_both_entries(self):
        d = date(2026, 10, 9)
        log = pp.update_trade_log(None, {"signal": "ENTRY_EARLY", "stage": "STARTER", "entry": 104, "sl": 96}, 105, d)
        log = pp.update_trade_log(log, {"signal": "ENTRY_PULLBACK", "stage": "FULL", "entry": 108, "sl": 100}, 110, date(2026, 10, 14))
        log = pp.update_trade_log(log, {"signal": "WATCH"}, 120, date(2026, 10, 20))
        self.assertEqual(log["starter"]["returnPct"], 15.38)
        self.assertEqual(log["full"]["returnPct"], 11.11)
        log = pp.update_trade_log(log, {"signal": "WATCH"}, 95, date(2026, 10, 25))
        self.assertTrue(log["starter"]["stopped"])
        self.assertEqual(log["starter"]["returnPct"], -7.69)     # exit at the stop, not the low
        rows = pp.entry_timing_scorecard([log])
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["stopped"], 1)


class IntradayReaction(unittest.TestCase):
    """GM Breweries filed at 12:30 pm: reaction = filing day + next session."""

    def test_session_rule(self):
        from datetime import datetime
        ts = datetime(2026, 10, 8, 12, 30, tzinfo=p.IST)
        session, timing = p.reaction_session(ts, None)
        self.assertEqual((session, timing), (date(2026, 10, 9), "INTRADAY"))
        self.assertEqual(p.reaction_window_start(ts, timing, session), date(2026, 10, 8))
        after = datetime(2026, 10, 8, 16, 0, tzinfo=p.IST)
        self.assertEqual(p.reaction_window_start(after, "AFTER_CLOSE", date(2026, 10, 9)), date(2026, 10, 9))

    def test_two_session_metrics(self):
        closes = [100.0] * 40 + [103.0, 108.0, 109.0]     # filing day +3%, next day +5% more
        vols = [1000] * 40 + [5000, 9000, 2000]
        f = frame(closes, vols=vols)
        fd, rd = f["Date"].iloc[40].date(), f["Date"].iloc[41].date()
        m = p.price_metrics(f, rd, fd)
        self.assertEqual(m["result_day_return_pct"], 8.0)          # vs close before the filing day
        self.assertEqual(m["result_day_low"], round(103 * 0.98, 2))  # lowest low of both sessions
        self.assertEqual(m["result_day_high"], round(108 * 1.01, 2))
        self.assertEqual(m["result_day_rvol"], 9.0)
        x = pp.extended_features(f, rd, released=True, window_start=fd)
        self.assertEqual(x["reaction_window_sessions"], 2)
        self.assertEqual(x["sessions_since_reaction"], 1)
        self.assertEqual(x["chart"]["reactionIndex"], 40 - (len(f) - 75) if len(f) > 75 else 40)


class DuplicateMergeTests(unittest.TestCase):
    """2.5.2 showed 82 companies twice (NSE copy + BSE copy)."""

    def _store(self):
        d = tempfile.mkdtemp()
        return p.EventStore(Path(d) / "events")

    def _ev(self, st, sec, fields=None):
        e = st.ensure_event(security=sec, period_end=date(2026, 9, 30))
        for k, v in (fields or {}).items():
            st.merge_field(e, k, v, source="TEST")
        st.save(e)
        return e

    def test_symbol_vs_bse_code_copies_fold(self):
        st = self._store()
        self._ev(st, {"isin": "INE179A01014", "nseSymbol": "PGHH", "symbol": "PGHH", "name": "Procter & Gamble Hygiene and Health Care Limited", "sector": "Personal Care"})
        self._ev(st, {"symbol": "PGHH", "bseSymbol": "PGHH", "bseCode": "500459", "name": "Procter & Gamble Hygiene and Health Care Ltd", "sector": "—"},
                 {"last_price": 15000})
        merges = p.merge_duplicate_events(st)
        evs = st.all()
        self.assertEqual((len(merges), len(evs)), (1, 1))
        e = evs[0]
        self.assertEqual(e["security"]["sector"], "Personal Care")      # placeholder did not overwrite
        self.assertEqual(e["security"]["bseCode"], "500459")
        self.assertEqual(st.value(e, "last_price"), 15000)
        # Discovery under the old key reuses the survivor instead of re-creating it.
        again = st.ensure_event(security={"bseCode": "500459", "symbol": "PGHH"}, period_end=date(2026, 9, 30))
        self.assertEqual(again["eventId"], e["eventId"])
        self.assertEqual(p.merge_duplicate_events(st), [])

    def test_post_split_isin_folds_and_is_kept_as_alias(self):
        st = self._store()
        self._ev(st, {"isin": "INE690A01010", "nseSymbol": "TTKPRESTIG", "name": "TTK Prestige Limited"})
        self._ev(st, {"isin": "INE690A01028", "bseSymbol": "TTKPRESTIG", "bseCode": "517506", "name": "TTK Prestige Ltd"})
        p.merge_duplicate_events(st)
        evs = st.all()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["security"]["altIsins"], ["INE690A01028"])
        self.assertIn("INE690A01028|2026-09-30", evs[0]["mergedFrom"])

    def test_different_companies_are_not_merged(self):
        st = self._store()
        self._ev(st, {"isin": "INE457A01014", "nseSymbol": "MAHABANK", "symbol": "ABAN", "name": "Bank of Maharashtra"})
        self._ev(st, {"isin": "INE421A01028", "bseSymbol": "ABAN", "bseCode": "523204", "name": "Aban Offshore Ltd"})
        self.assertEqual(p.merge_duplicate_events(st), [])
        self.assertTrue(p.same_company({"name": "Kabra Extrusiontechnik Ltd"}, {"name": "Kabra Extrusion Technik Limited"}))
        self.assertTrue(p.same_company({"name": "Dr Reddys Laboratories Ltd"}, {"name": "Dr. Reddy's Laboratories Limited"}))

    def test_gate_discounts_duplicate_folds(self):
        old = {"activeDashboardEvents": 379, "resultsFiled": 7, "liveQuarter": "Q2 FY27", "declaredCompletenessPct": 27}
        new = {"activeDashboardEvents": 297, "resultsFiled": 7, "liveQuarter": "Q2 FY27", "declaredCompletenessPct": 27,
               "integrity": {"signature": "abc", "duplicatesMerged": 82, "declaredMerged": 0}}
        ok, reasons = p.quality_gate(new, old)
        self.assertTrue(ok, reasons)
        new["integrity"]["duplicatesMerged"] = 0
        self.assertFalse(p.quality_gate(new, old)[0])     # a real drop still blocks


class LookupIdentityTests(unittest.TestCase):
    ROWS = [{"symbol": "GUJALKALI", "companyName": "Gujarat Alkalies and Chemicals Limited"},
            {"symbol": "TUTIALKA", "companyName": "Tuticorin Alkali Chemicals"}]

    def test_fuzzy_first_row_is_rejected(self):
        self.assertIsNone(p.pick_lookup_row(self.ROWS, "ALKALI", "Alkali Metals Ltd"))
        rows = [{"symbol": "SWARAJENG", "companyName": "Swaraj Engines Limited"},
                {"symbol": "SWARAJ", "companyName": "Swaraj Suiting Limited"}]
        self.assertEqual(p.pick_lookup_row(rows, "SWARAJ")["symbol"], "SWARAJ")
        self.assertEqual(p.pick_lookup_row(rows, "SWRJ", "Swaraj Engines Ltd")["symbol"], "SWARAJENG")

    def test_repair_strips_wrong_company_data(self):
        root = Path(tempfile.mkdtemp())
        st = p.EventStore(root / "events")
        raw = root / "raw"
        (raw / "nse" / "NSE_ALKALI_2026-09-30").mkdir(parents=True)
        (raw / "nse" / "NSE_ALKALI_2026-09-30" / "lookup-1.json").write_text(json.dumps({"data": self.ROWS}))
        master = p.SymbolMaster(root / "master" / "symbols.json")
        master.data["securities"]["NSE:ALKALI"] = {"symbol": "ALKALI", "bseCode": "533029", "name": "Alkali Metals Ltd"}
        e = st.ensure_event(security={"symbol": "ALKALI", "bseSymbol": "ALKALI", "bseCode": "533029",
                                      "nseSymbol": "GUJALKALI", "name": "Gujarat Alkalies and Chemicals Limited"},
                            period_end=date(2026, 9, 30))
        st.merge_field(e, "last_price", 596.65, source="NSE_PRICE")
        st.merge_field(e, "result_date", "2026-10-12", source="BSE_RESULT_ANNOUNCEMENT")
        st.save(e)
        fixed = p.repair_lookup_mismatches(st, master, raw)
        self.assertEqual(len(fixed), 1)
        ev = st.all()[0]
        self.assertNotIn("nseSymbol", ev["security"])
        self.assertEqual(ev["security"]["name"], "Alkali Metals Ltd")
        self.assertIsNone(st.value(ev, "last_price"))                 # wrong company's price gone
        self.assertEqual(st.value(ev, "result_date"), "2026-10-12")   # BSE facts kept
        self.assertEqual(p.repair_lookup_mismatches(st, master, raw), [])


class AuditFixTests(unittest.TestCase):
    """Engine 2.5.4 full-audit fixes."""

    def test_split_adjustment(self):
        days = pd.date_range("2026-08-01", periods=10, freq="B")
        close = [1000, 1010, 1020, 1015, 102, 103, 104, 105, 104, 106]   # 1:10 split on day 5
        f = pd.DataFrame({"Date": days, "Open": close, "High": [c * 1.01 for c in close], "Low": [c * 0.99 for c in close],
                          "Close": close, "Volume": [100] * 4 + [1000] * 6})
        out = pp.adjust_corporate_actions(f)
        self.assertAlmostEqual(out["Close"].iloc[0], 100.0)
        self.assertAlmostEqual(out["Volume"].iloc[0], 1000.0)
        self.assertEqual(out.attrs["corporateActions"][0]["factor"], 0.1)
        crash = f.copy()
        crash["Close"] = [100, 101, 102, 100, 52, 50, 49, 48, 47, 46]       # -48% intraday collapse, not a ratio gap
        crash["High"] = [101, 102, 103, 101, 99, 51, 50, 49, 48, 47]
        self.assertEqual(pp.adjust_corporate_actions(crash)["Close"].iloc[0], 100)

    def test_one_sector_taxonomy(self):
        self.assertEqual(pp.canonical_sector("Finance"), "Financial Services")
        self.assertEqual(pp.canonical_sector(None, "—", "Computers - Software"), "Information Technology")
        self.assertEqual(pp.canonical_sector("Industrial Gases"), "Chemicals")
        self.assertIsNone(pp.canonical_sector("Miscellaneous", "-"))
        self.assertEqual(p.sector_key({"sector": "Cement And Cement Products"}), "Construction Materials")

    def test_regime_needs_200_sessions(self):
        r = pp.market_regime([100 + i * 0.1 for i in range(70)])
        self.assertIsNone(r["dma200"])
        self.assertEqual(r["label"], "RISK-ON")

    def test_negative_book_is_not_meaningful(self):
        v = pp.valuation_view(10, None, -3827, None, -20)
        self.assertTrue(v["tinyBook"])
        self.assertEqual(v["bookNote"], "negative book value")
        self.assertFalse(pp.valuation_view(30, None, 79, None, 60)["tinyBook"])   # high P/B alone is fine

    def test_yoy_from_year_ago_filing(self):
        listing = [
            {"periodEnd": "2026-09-30", "fromDate": "2026-07-01", "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": "cur"},
            {"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "STANDALONE", "cumulative": False, "xbrlUrl": "ya_s"},
            {"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": "ya_c"},
            {"periodEnd": "2026-06-30", "fromDate": "2026-04-01", "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": "pq_c"},
        ]
        concepts = {"revenue": "revenuefromoperations", "pat": "profitlossforperiodattributabletoownersofparent"}
        docs = {"ya_c": {"revenue_cr": 1000.0, "pat_cr": 100.0, "_meta": {"concepts": concepts}},
                "pq_c": {"revenue_cr": 1100.0, "pat_cr": 110.0, "_meta": {"concepts": concepts}}}
        calls = []
        def fetch(url, pe):
            calls.append(url)
            return docs[url]
        parsed = {"revenue_cr": 1200.0, "pat_cr": 130.0, "prior_year_revenue_cr": None, "prior_year_pat_cr": None,
                  "revenue_qoq_pct": None, "_meta": {"concepts": concepts}}
        p.fill_comparatives_from_listing(parsed, listing, date(2026, 9, 30), "CONSOLIDATED", fetch)
        self.assertEqual(calls, ["ya_c", "pq_c"])                     # same basis only
        self.assertEqual((parsed["revenue_yoy_pct"], parsed["pat_yoy_pct"]), (20.0, 30.0))
        self.assertAlmostEqual(parsed["revenue_qoq_pct"], 9.09, places=2)
        self.assertFalse(parsed["_meta"]["comparativesFromSameDocument"])
        # A different PAT concept in the old filing is not mixed in.
        other = {"ya_c": {"revenue_cr": 1000.0, "pat_cr": 90.0, "_meta": {"concepts": {**concepts, "pat": "profitloss"}}}}
        parsed2 = {"revenue_cr": 1200.0, "pat_cr": 130.0, "revenue_qoq_pct": 1.0, "_meta": {"concepts": concepts}}
        p.fill_comparatives_from_listing(parsed2, listing, date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: other[u])
        self.assertEqual(parsed2["revenue_yoy_pct"], 20.0)
        self.assertIsNone(parsed2.get("pat_yoy_pct"))


FIX = Path(__file__).parent / "fixtures" / "nse"


@unittest.skipUnless((FIX / "integrated_INFY_Q1FY27_consolidated.xml").exists(), "NSE fixture files not present")
class RealNseFilingTests(unittest.TestCase):
    """Engine 2.6.1: results moved to NSE Integrated Filing; parse real filings."""

    def parse(self, name, pe):
        return p.parse_xbrl_financials([(FIX / name).read_bytes()], pe)

    def test_company_bank_insurer_and_old_format(self):
        r = self.parse("integrated_INFY_Q1FY27_consolidated.xml", date(2026, 6, 30))
        self.assertEqual((r["revenue_cr"], r["pat_cr"], r["eps"], r["basis"]), (48211.0, 7769.0, 19.19, "CONSOLIDATED"))
        b = self.parse("integrated_HDFCBANK_Q1FY27_consolidated.xml", date(2026, 6, 30))
        self.assertEqual((b["revenue_cr"], b["pat_cr"], b["eps"]), (90575.33, 19244.71, 12.5))   # owners' share, not total
        self.assertEqual(b["_meta"]["revenueDefinition"], "INTEREST_EARNED")
        g = self.parse("integrated_ICICIGI_Q1FY27_standalone.xml", date(2026, 6, 30))
        self.assertEqual(g["_meta"]["revenueDefinition"], "PREMIUM_INCOME")
        self.assertIsNotNone(g["pat_cr"])
        lf = self.parse("integrated_SBILIFE_Q1FY27_standalone.xml", date(2026, 6, 30))
        self.assertIsNotNone(lf["pat_cr"])
        old = self.parse("results_INFY_Q3FY25_consolidated.xml", date(2024, 12, 31))   # mislabelled YTD context
        self.assertEqual((old["revenue_cr"], old["pat_cr"]), (41764.0, 6806.0))
        self.assertEqual(old["_meta"]["issues"], [])

    def test_integrated_index_rows(self):
        rows = json.loads((FIX / "integrated_filings_INFY_trimmed.json").read_text())["data"]
        cands = [p.normalize_nse_integrated(r) for r in rows]
        self.assertIsNone(cands[0])                                   # governance row dropped
        con = next(c for c in cands if c and c["basis"] == "CONSOLIDATED")
        self.assertEqual((con["periodEnd"], con["fromDate"]), (date(2026, 6, 30), date(2026, 4, 1)))
        self.assertTrue(con["xbrlUrl"].endswith("_WEB.xml"))
        self.assertEqual(con["filingTimestamp"].isoformat(), "2026-07-23T17:40:57+05:30")
        self.assertEqual(con["source"], "NSE_INTEGRATED_FILING")
        row = p.listing_row([{"periodEnd": "2026-06-30", "fromDate": "2026-04-01", "basis": c["basis"],
                              "cumulative": False, "xbrlUrl": c["xbrlUrl"], "filedAt": None} for c in cands if c],
                            date(2026, 6, 30), "CONSOLIDATED")
        self.assertEqual(row["xbrlUrl"], con["xbrlUrl"])


class BseNseSymbolClashTests(unittest.TestCase):
    """2.6.1: BRIGHT is Bright Outdoor Media on BSE and Bright Solar on NSE."""

    def test_bse_row_does_not_land_in_nse_namesake(self):
        st = p.EventStore(Path(tempfile.mkdtemp()) / "events")
        nse = st.ensure_event(security={"symbol": "BRIGHT", "nseSymbol": "BRIGHT", "name": "Bright Solar Limited"},
                              period_end=date(2026, 9, 30))
        st.save(nse)
        bse = st.ensure_event(security={"symbol": "BRIGHT", "bseSymbol": "BRIGHT", "bseCode": "543831",
                                        "name": "Bright Outdoor Media Ltd"}, period_end=date(2026, 9, 30))
        self.assertNotEqual(bse["eventId"], nse["eventId"])
        same = st.ensure_event(security={"symbol": "BRIGHT", "nseSymbol": "BRIGHT", "name": "BRIGHT SOLAR LTD"},
                               period_end=date(2026, 9, 30))
        self.assertEqual(same["eventId"], nse["eventId"])


class SelfAuditTests(unittest.TestCase):
    def test_flags_problems(self):
        today = date(2026, 10, 10)
        items = [
            {"eventId": "a", "symbol": "TCS", "name": "Tata Consultancy Services Ltd", "resultsReleased": True,
             "resultDate": "2026-10-08", "reactionSession": "2026-10-09", "priceContext": {"resultDayPct": None},
             "fundamentalSnapshot": {"revenueCr": None}, "plus": {"price": {"last_session": "2026-10-09"}, "sectorKey": "Information Technology"}},
            {"eventId": "b", "symbol": "TCS", "name": "Tata Consultancy Services Limited", "resultsReleased": False,
             "resultDate": "2026-10-12", "priceContext": {"resultDayPct": 3.0}, "plus": {}},
        ]
        r = pp.self_audit(items, {"asOf": "2026-10-09"}, {"installCheck": {"ok": True}}, today)
        st = {c["check"]: c["status"] for c in r["checks"]}
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(st["Each company listed once"], "FAIL")
        self.assertEqual(st["No reaction data before a result"], "FAIL")
        self.assertEqual(st["Declared results have revenue/profit (filed 2+ days ago)"], "FAIL")
        self.assertEqual(st["Reaction measured once the session has closed"], "FAIL")
        self.assertEqual(st["Market index data is current"], "OK")


class GmbrewOwnersZeroTests(unittest.TestCase):
    """2.6.2: GMBREW files owners' profit as 0 in its consolidated XBRL."""

    def test_zero_owners_line_uses_total_profit(self):
        f = FIX / "integrated_GMBREW_Q2FY27_consolidated.xml"
        if not f.exists():
            self.skipTest("fixture missing")
        r = p.parse_xbrl_financials([f.read_bytes()], date(2026, 9, 30))
        self.assertEqual((r["revenue_cr"], r["pat_cr"]), (860.62, 39.29))
        self.assertIn("OWNERS_PAT_ZERO_USED_TOTAL", r["_meta"]["issues"])

    def test_session_closed(self):
        from unittest import mock
        ist = p.now_ist()
        with mock.patch.object(p, "now_ist", return_value=ist.replace(hour=6, minute=20)):
            self.assertFalse(p.session_closed(ist.date()))
            self.assertTrue(p.session_closed(ist.date() - timedelta(days=1)))
        with mock.patch.object(p, "now_ist", return_value=ist.replace(hour=16, minute=0)):
            self.assertTrue(p.session_closed(ist.date()))

    def test_profit_swing_with_steady_revenue_is_not_a_unit_error(self):
        parsed = {"revenue_cr": 91.43, "pat_cr": -3.8, "revenue_qoq_pct": -0.57, "pat_qoq_pct": -19100.0,
                  "_meta": {"periodEnd": "2026-09-30", "reference": {"fy_revenue_cr": 579.55, "fy_pat_cr": 0.1}}}
        v = p.validate_financial_snapshot(parsed, "BSE_RESULTS_SNAPSHOT", date(2026, 9, 30))
        self.assertNotEqual(v["status"], "REJECTED")
        self.assertIn("PAT_SWING", v["issues"])


class Engine270Tests(unittest.TestCase):
    """Earnings acceleration, relative strength vs NIFTY 500, live entry status."""

    def test_acceleration_labels(self):
        self.assertEqual(pp.earnings_acceleration(25, 30, 12, 18)["label"], "ACCELERATING")
        self.assertEqual(pp.earnings_acceleration(5, 4, 15, 20)["label"], "DECELERATING")
        self.assertEqual(pp.earnings_acceleration(12, 15, 10, 13)["label"], "STEADY")
        self.assertEqual(pp.earnings_acceleration(25, None, 12, None)["label"], "ACCELERATING")
        self.assertIsNone(pp.earnings_acceleration(25, 30, None, None)["label"])
        # a turnaround's profit % is not comparable; revenue alone decides
        self.assertEqual(pp.earnings_acceleration(20, 900, 8, -50, "TURNAROUND")["label"], "ACCELERATING")
        # faster but still shrinking is not "accelerating"
        self.assertNotEqual(pp.earnings_acceleration(-2, -1, -15, -20)["label"], "ACCELERATING")

    def test_relative_strength(self):
        dates = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
        closes = [100.0, 101.0, 102.0, 99.0]
        r = pp.relative_strength(6.0, "2026-10-07", "2026-10-09", dates, closes)
        self.assertEqual((r["indexReturnPct"], r["relativePct"]), (-1.98, 7.98))
        self.assertIsNone(pp.relative_strength(None, "2026-10-07", "2026-10-09", dates, closes)["relativePct"])

    def test_live_entry_status(self):
        plan = {"entry": 100.0}
        self.assertEqual(pp.live_entry_status(plan, {"price": 101, "at": "x"})["state"], "ABOVE_TRIGGER")
        self.assertEqual(pp.live_entry_status(plan, {"price": 98.5, "at": "x"})["state"], "NEAR_TRIGGER")
        self.assertEqual(pp.live_entry_status(plan, {"price": 95, "at": "x"})["state"], "BELOW_TRIGGER")
        self.assertIsNone(pp.live_entry_status(plan, None))
        self.assertIsNone(pp.live_entry_status({}, {"price": 95})["state"])

    def test_previous_quarter_yoy_from_filings(self):
        C = {"revenue": "revenuefromoperations", "pat": "profitlossforperiod"}
        rows = [{"periodEnd": pe, "fromDate": fd, "basis": "STANDALONE", "cumulative": False, "xbrlUrl": u, "filedAt": None}
                for pe, fd, u in (("2025-09-30", "2025-07-01", "ya"), ("2026-06-30", "2026-04-01", "pq"),
                                  ("2025-06-30", "2025-04-01", "pq_ya"))]
        docs = {"ya": (1000.0, 100.0), "pq": (1150.0, 120.0), "pq_ya": (1000.0, 100.0)}
        fetch = lambda u, pe: {"revenue_cr": docs[u][0], "pat_cr": docs[u][1], "_meta": {"concepts": C}}
        parsed = {"revenue_cr": 1300.0, "pat_cr": 140.0, "_meta": {"concepts": C}}
        p.fill_comparatives_from_listing(parsed, rows, date(2026, 9, 30), "STANDALONE", fetch)
        self.assertEqual((parsed["revenue_yoy_pct"], parsed["pat_yoy_pct"]), (30.0, 40.0))
        self.assertEqual((parsed["prev_q_revenue_yoy_pct"], parsed["prev_q_pat_yoy_pct"]), (15.0, 20.0))
        g = pp.earnings_acceleration(parsed["revenue_yoy_pct"], parsed["pat_yoy_pct"],
                                     parsed["prev_q_revenue_yoy_pct"], parsed["prev_q_pat_yoy_pct"])
        self.assertEqual(g["label"], "ACCELERATING")


class PeersTopDownTests(unittest.TestCase):
    """2.8.0: peers (who moved, who lags) and the top-down checklist."""

    def item(self, sym, r63, released=False, reaction=None, rd="2026-10-15"):
        return {"symbol": sym, "resultsReleased": released, "resultDate": rd,
                "priceContext": {"resultDayPct": reaction, "lastClose": 110.0},
                "plus": {"sectorKey": "Information Technology", "sector": {"tailwind": "POSITIVE"},
                         "price": {"ret_63d_pct": r63, "ema21": 105.0, "ema63": 100.0, "ret_21d_pct": 2.0},
                         "valuation": {"label": "FAIR"}}}

    def test_roles_reported_peers_and_checklist(self):
        items = [self.item("CYIENT", 37.8), self.item("COFORGE", 24.2), self.item("PERSISTENT", 13.2),
                 self.item("TECHM", 4.9), self.item("TCS", 4.2, True, 3.85, "2026-10-08"), self.item("HCLTECH", 2.3),
                 self.item("LTM", 1.6)]
        pp.peer_context(items, {"label": "RISK-OFF"})
        by = {i["symbol"]: i["plus"] for i in items}
        self.assertEqual(by["CYIENT"]["peers"]["role"], "LEADER")
        self.assertEqual(by["HCLTECH"]["peers"]["role"], "LAGGARD")
        self.assertEqual(by["HCLTECH"]["peers"]["reported"][0]["symbol"], "TCS")
        self.assertEqual(by["HCLTECH"]["peers"]["reportedAvgReaction"], 3.85)
        self.assertEqual(by["TCS"]["peers"]["reportedCount"], 0)          # a stock is not its own peer
        st = {c["key"]: c["status"] for c in by["HCLTECH"]["topDown"]["checks"]}
        self.assertEqual(st, {"market": "bad", "sector": "ok", "peers": "ok", "trend": "ok", "momentum": "ok", "valuation": "ok"})
        self.assertEqual(by["HCLTECH"]["topDown"]["passed"], 5)
        self.assertEqual({c["key"]: c["status"] for c in by["CYIENT"]["topDown"]["checks"]}["peers"], "warn")

    def test_peers_that_fell_and_small_sectors(self):
        items = [self.item("A", 10), self.item("B", 5, True, -6.0), self.item("C", 3), self.item("D", 1)]
        pp.peer_context(items, None)
        self.assertEqual({c["key"]: c["status"] for c in items[3]["plus"]["topDown"]["checks"]}["peers"], "bad")
        small = [self.item("X", 5), self.item("Y", 3)]
        pp.peer_context(small, None)
        self.assertIsNone(small[0]["plus"]["peers"]["role"])
        self.assertEqual(small[0]["plus"]["topDown"]["checks"][0]["status"], "na")


class MarketInternalsTests(unittest.TestCase):
    """2.9.0: breadth, FII/DII flows and peer fundamentals (bottom-up approach)."""

    def test_breadth_and_flows(self):
        items = [{"priceContext": {"lastClose": c}, "plus": {"price": {"ema21": 100.0, "ema63": 100.0}}} for c in (110, 105, 95, 90)]
        internals = {"advanceDecline": {"advances": 150, "declines": 350, "unchanged": 0, "at": "2026-10-09T10:00:00Z"},
                     "flows": [{"date": d, "fiiNetCr": f, "diiNetCr": -f / 2} for d, f in
                               (("2026-10-05", 500.0), ("2026-10-06", -800.0), ("2026-10-07", -1200.0),
                                ("2026-10-08", -300.0), ("2026-10-09", -950.0))]}
        r = pp.market_internals(items, internals, date(2026, 10, 9))
        self.assertEqual((r["breadth"]["above63Pct"], r["breadth"]["n"]), (50.0, 4))
        self.assertEqual(r["advanceDecline"]["advancePct"], 30.0)
        self.assertEqual((r["flows"]["fiiStreak"], r["flows"]["fii5dCr"]), (-4, -2750.0))
        stale = pp.market_internals(items, {"flows": [{"date": "2026-09-01", "fiiNetCr": 1.0}]}, date(2026, 10, 9))
        self.assertNotIn("flows", stale)

    def test_parse_fii_dii(self):
        payload = [{"category": "DII **", "date": "09-Oct-2026", "buyValue": "12000", "sellValue": "9000", "netValue": "3000.55"},
                   {"category": "FII/FPI **", "date": "09-Oct-2026", "buyValue": "10000", "sellValue": "11500", "netValue": "-1500.2"}]
        self.assertEqual(p.parse_fii_dii(payload), {"diiNetCr": 3000.55, "fiiNetCr": -1500.2, "date": "2026-10-09"})
        self.assertIsNone(p.parse_fii_dii({"unexpected": True}))

    def test_peer_fundamentals_rank(self):
        def it(sym, rev, pat, opm, roe, rel=True):
            return {"symbol": sym, "resultsReleased": rel, "revenueYoY": rev, "patYoY": pat,
                    "plus": {"margins": {"opmTtm": opm}, "valuation": {"roe": roe}}}
        members = [it("A", 11.2, 15.0, 26.0, 60.0), it("B", 8.0, 5.0, 21.0, 30.0), it("C", 4.0, 9.0, 18.0, 20.0),
                   it("D", None, None, 15.0, 1500.0, rel=False)]
        members[3]["plus"]["valuation"]["tinyBook"] = True
        f = pp.peer_fundamentals(members[0], members)
        self.assertEqual((f["revenueYoY"]["rank"], f["revenueYoY"]["of"]), (1, 3))
        self.assertEqual((f["margin"]["rank"], f["margin"]["of"]), (1, 4))
        self.assertEqual(f["roe"]["of"], 3)                 # tiny-book ROE excluded
        self.assertNotIn("revenueYoY", pp.peer_fundamentals(members[3], members))


if __name__ == "__main__":
    unittest.main()


class BseWrongCodeLoopTests(unittest.TestCase):
    """2.9.1: NSE:BRIGHT (Bright Solar) kept getting BSE 543831 (Bright Outdoor
    Media) back every run through a stale alias, and the repair left Bright
    Outdoor's sector, P/E, mergedFrom and master entry on Bright Solar."""
    P = date(2026, 9, 30)
    META = {"ISIN": "INE0OMI01019", "PE": "30.85", "PB": "-", "ROE": "-", "OPM": "-", "Group": "M",
            "Sector": "Consumer Discretionary", "IndustryNew": "Media, Entertainment & Publication",
            "IGroup": "Media", "ISubGroup": "Advertising & Media Agencies"}

    def _setup(self):
        root = Path(tempfile.mkdtemp())
        st = p.EventStore(root / "events")
        raw = root / "raw"
        folder = raw / "bse" / "NSE_BRIGHT_2026-09-30"
        folder.mkdir(parents=True)
        (folder / "lookup-1.json").write_text(json.dumps({"bse_code": "543831", "company_name": "BRIGHT OUTDOOR MEDIA LTD",
                                                          "isin": "INE0OMI01019", "symbol": "BRIGHT"}))
        (folder / "equity_meta-1.json").write_text(json.dumps(self.META))
        master = p.SymbolMaster(root / "master" / "symbols.json")
        outdoor = st.ensure_event(security={"isin": "INE0OMI01019", "symbol": "BRIGHT", "bseSymbol": "BRIGHT",
                                            "bseCode": "543831", "name": "Bright Outdoor Media Ltd"}, period_end=self.P)
        st.save(outdoor)
        solar = st.ensure_event(security={"symbol": "BRIGHT", "nseSymbol": "BRIGHT", "name": "Bright Solar Limited"},
                                period_end=self.P)
        sec = solar["security"]
        sec.update({"bseCode": "543831", "bseSymbol": "BRIGHT", "bseGroup": "M",
                    "macroSector": "Consumer Discretionary", "exchangeSector": "Media, Entertainment & Publication",
                    "industry": "Media", "basicIndustry": "Advertising & Media Agencies"})
        st.merge_field(solar, "exchange_pe", 30.85, source="BSE_META")
        st.merge_field(solar, "trailing_pe", 30.85, source="NSE_QUOTE")     # mislabelled BSE value
        st.merge_field(solar, "market_cap_cr", 20.87, source="YAHOO_FUNDAMENTALS")
        solar["mergedFrom"] = ["BSE:543831|2026-09-30"]
        st.save(solar)
        st.add_alias("BSE:543831|2026-09-30", solar["eventId"])
        master.data["securities"]["NSE:BRIGHT"] = dict(sec, securityKey="NSE:BRIGHT")
        return st, raw, master, solar["eventId"], outdoor["eventId"]

    def test_repair_removes_every_trace_of_the_wrong_company(self):
        st, raw, master, solar_id, outdoor_id = self._setup()
        p.repair_lookup_mismatches(st, master, raw)
        ev = st.load(solar_id)
        sec = ev["security"]
        for k in ("bseCode", "bseSymbol", "bseGroup", "macroSector", "exchangeSector", "industry", "basicIndustry"):
            self.assertNotIn(k, sec, k)
        self.assertIsNone(st.value(ev, "trailing_pe"))                 # Bright Outdoor's P/E gone
        self.assertEqual(st.value(ev, "market_cap_cr"), 20.87)         # own values kept
        self.assertFalse(ev.get("mergedFrom"))
        self.assertEqual(st.resolve("BSE:543831|2026-09-30"), outdoor_id)   # alias now points at the owner
        m = master.data["securities"]["NSE:BRIGHT"]
        self.assertNotIn("bseCode", m)
        self.assertNotIn("exchangeSector", m)

    def test_already_repaired_event_is_cleaned_and_bse_row_goes_to_owner(self):
        st, raw, master, solar_id, outdoor_id = self._setup()
        ev = st.load(solar_id)
        for k in ("bseCode", "bseSymbol", "bseGroup"):            # state left by 2.9.0's repair
            ev["security"].pop(k)
        ev["fields"].pop("exchange_pe")
        ev["security"]["identityRepaired"] = {"wrongBseCode": "543831", "wrongBseName": "BRIGHT OUTDOOR MEDIA LTD"}
        st.save(ev)
        p.repair_lookup_mismatches(st, master, raw)
        ev = st.load(solar_id)
        self.assertNotIn("exchangeSector", ev["security"])
        self.assertIsNone(st.value(ev, "trailing_pe"))
        # Next discovery of the BSE row (no ISIN) must not land in Bright Solar.
        row = st.ensure_event(security={"symbol": "BRIGHT", "bseSymbol": "BRIGHT", "bseCode": "543831",
                                        "name": "Bright Outdoor Media Ltd"}, period_end=self.P)
        self.assertEqual(row["eventId"], outdoor_id)

    def test_stale_alias_cannot_route_bse_row_into_namesake(self):
        st, raw, master, solar_id, outdoor_id = self._setup()
        st.save(dict(st.load(solar_id), security={"symbol": "BRIGHT", "nseSymbol": "BRIGHT",
                                                  "name": "Bright Solar Limited"}))
        row = st.ensure_event(security={"symbol": "BRIGHT", "bseSymbol": "BRIGHT", "bseCode": "543831",
                                        "name": "Bright Outdoor Media Ltd"}, period_end=self.P)
        self.assertNotEqual(row["eventId"], solar_id)

    def test_trailing_pe_keeps_the_exchange_pe_source(self):
        st = p.EventStore(Path(tempfile.mkdtemp()) / "events")
        e = st.ensure_event(security={"symbol": "BRIGHT", "nseSymbol": "BRIGHT", "bseCode": "543831",
                                      "name": "Bright Solar Limited"}, period_end=self.P)

        class Fake:
            def __init__(self, ctx): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def quote(self, sym, eid): return {}, None              # SME: NSE quote has no P/E
            def meta(self, code, eid, sym): return dict(BseWrongCodeLoopTests.META), None

        saved = (p.NSEAdapter, p.BSEAdapter, p.EXTRA_CALL_BUDGET)
        p.NSEAdapter = p.BSEAdapter = Fake
        p.EXTRA_CALL_BUDGET = 10 ** 6
        try:
            p.enrich_exchange_meta(e, st, None)
        finally:
            p.NSEAdapter, p.BSEAdapter, p.EXTRA_CALL_BUDGET = saved
        self.assertEqual(st.value(e, "trailing_pe"), 30.85)
        self.assertEqual(p.field_source(e, "trailing_pe"), "BSE_META")   # was labelled NSE_QUOTE

    def test_alias_never_hides_a_live_event(self):
        # events/_aliases.map has "NSE:BRIGHT|P" -> "NSE:BCG|P" from the 2.5.4
        # repair; once a BCG event exists, Bright Solar must not resolve to it.
        st, raw, master, solar_id, outdoor_id = self._setup()
        bcg = st.ensure_event(security={"symbol": "BCG", "nseSymbol": "BCG", "name": "Brightcom Group Ltd"},
                              period_end=self.P)
        st.save(bcg)
        st.add_alias(solar_id, bcg["eventId"])
        self.assertEqual(st.resolve(solar_id), solar_id)
        self.assertEqual(st.load(solar_id)["security"]["name"], "Bright Solar Limited")


class Q1StrengthFromFilingsTests(unittest.TestCase):
    """2.9.2: POONAWALLA Q2 FY27 was labelled 'Fresh PEAD: Average Q1, sudden
    Q2 pivot' although its Q1 filing showed revenue +77.9% / PAT +391.6% YoY.
    No archived Q1 event exists, so Q1 strength was unknown; the Q1 YoY read
    from the filings (prev_q_*_yoy_pct) must be used."""

    def _event(self, st, with_prev_yoy=True):
        e = st.ensure_event(security={"isin": "INE511C01022", "nseSymbol": "POONAWALLA", "symbol": "POONAWALLA",
                                      "name": "Poonawalla Fincorp Ltd"}, period_end=date(2026, 9, 30))
        for f, v in (("results_released", True), ("revenue_yoy_pct", 70.17), ("pat_yoy_pct", 405.19),
                     ("pat_trend", "PROFIT_GROWTH"), ("result_date", "2026-10-09"), ("reaction_session", "2026-10-12")):
            st.merge_field(e, f, v, source="NSE_XBRL")
        if with_prev_yoy:
            st.merge_field(e, "prev_q_revenue_yoy_pct", 77.85, source="NSE_XBRL")
            st.merge_field(e, "prev_q_pat_yoy_pct", 391.55, source="NSE_XBRL")
        e["plus"] = {"price": {"q1_reaction_return_pct": -2.11, "q1_reaction_rvol": 2.84, "q1_sustained": False}}
        return e

    def _bucket(self, e, st):
        out = p.build_plus(st, e, released=True, result_label="GENUINE", price_label="UNVERIFIED", sector_info=None,
                           box_high=None, last_price=447.3, result_ret=None, rvol=None, result_low=None,
                           result_high=None)
        return out["q1Strength"], out["bucket"]

    def test_q1_strength_from_filing_yoy(self):
        st = p.EventStore(Path(tempfile.mkdtemp()) / "events")
        q1, bucket = self._bucket(self._event(st), st)
        self.assertEqual(q1, "STRONG")
        self.assertEqual(bucket["code"], "RE_PEAD")      # strong Q1 numbers, price faded, Q2 confirms

    def test_unknown_q1_does_not_claim_average_q1(self):
        st = p.EventStore(Path(tempfile.mkdtemp()) / "events")
        q1, bucket = self._bucket(self._event(st, with_prev_yoy=False), st)
        self.assertIsNone(q1)
        self.assertEqual(bucket["code"], "FRESH_PEAD")
        self.assertNotIn("Average Q1", bucket["why"])

    def test_known_average_q1_keeps_the_text(self):
        b = pp.classify_bucket(released=True, q2_strength="STRONG", setup=False, sustained=None, q1_strength="AVERAGE")
        self.assertEqual(b["why"], "Average Q1, sudden Q2 earnings pivot.")


class LossMakerPeTests(unittest.TestCase):
    """2.9.2: NSE quotes P/E 0 for loss-makers (CROMPTON, BAJAJELEC, TRF...);
    the card showed '0.0x P/E vs sector 20.8x' - a fake zero."""

    def test_zero_pe_is_not_published(self):
        v = pp.valuation_view(0.0, 20.8, -6.88, None, 3.98)
        self.assertEqual(v["label"], "LOSS-MAKING")
        self.assertIsNone(v["pe"])
        self.assertEqual(v["sectorPe"], 20.8)

    def test_data_row_trailing_pe_zero_is_null(self):
        st = p.EventStore(Path(tempfile.mkdtemp()) / "events")
        e = st.ensure_event(security={"isin": "INE299U01018", "nseSymbol": "CROMPTON", "symbol": "CROMPTON",
                                      "name": "Crompton Greaves Consumer Electricals Ltd"}, period_end=date(2026, 9, 30))
        st.merge_field(e, "trailing_pe", 0.0, source="NSE_QUOTE")
        self.assertIsNone(p.event_to_data_row(e)["trailingPE"])


class IlliquidPeerTests(unittest.TestCase):
    """2.9.3 (owner's decision, 10 Oct 2026): peers that fail the liquidity test
    (₹1 Cr/day, ₹20) do not count as 'peers that reported'. INDBNK (revenue
    ₹0.26 Cr, turnover ~0) alone set POONAWALLA's peers check to 'bad'."""

    def item(self, sym, r63, released=False, reaction=None, liquid=True, rev=None):
        return {"symbol": sym, "resultsReleased": released, "resultDate": "2026-10-08", "revenueYoY": rev,
                "priceContext": {"resultDayPct": reaction, "lastClose": 447.3},
                "plus": {"sectorKey": "Financial Services", "sector": {"tailwind": "NEUTRAL"},
                         "liquidity": {"pass": liquid},
                         "price": {"ret_63d_pct": r63, "ema21": 453.0, "ema63": 457.0, "ret_21d_pct": -1.8},
                         "valuation": {"label": "FAIR"}}}

    def test_illiquid_reported_peer_ignored(self):
        items = [self.item("POONAWALLA", -6.2, True, None, rev=70.2), self.item("INDBNK", 11.8, True, -7.21, False, 400.0),
                 self.item("A", 5.0), self.item("B", -2.0), self.item("C", -9.0)]
        pp.peer_context(items, None)
        pe = items[0]["plus"]["peers"]
        self.assertEqual(pe["reportedCount"], 0)
        self.assertIsNone(pe["reportedAvgReaction"])
        self.assertNotEqual({c["key"]: c["status"] for c in items[0]["plus"]["topDown"]["checks"]}["peers"], "bad")
        self.assertNotIn("revenueYoY", items[0]["plus"]["peerFundamentals"])   # only itself left (< 3 values)

    def test_liquid_reported_peer_still_counts(self):
        items = [self.item("POONAWALLA", -6.2, True, None), self.item("BAJFINANCE", 3.0, True, -4.0),
                 self.item("A", 5.0), self.item("B", -2.0)]
        pp.peer_context(items, None)
        self.assertEqual(items[0]["plus"]["peers"]["reportedAvgReaction"], -4.0)

    def test_sector_stats_skip_illiquid_results(self):
        rows = [{"sectorKey": "Financial Services", "ret63": r, "released": rel, "strength": s, "reaction": re_, "liquid": lq}
                for r, rel, s, re_, lq in ((-6, True, "STRONG", None, True), (12, True, None, -7.21, False),
                                           (5, False, None, None, True), (-2, False, None, None, None))]
        s = pp.sector_stats(rows)["Financial Services"]
        self.assertEqual(s["declared"], 1)
        self.assertIsNone(s["avgReaction"])
        self.assertEqual(s["stocks"], 4)               # momentum universe unchanged
