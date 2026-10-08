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


if __name__ == "__main__":
    unittest.main()
