"""Tests for the US scanner's own logic (the shared rules are tested in tests/)."""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pead_us as us  # noqa: E402


def frame(closes, vols, start=date(2026, 6, 1)):
    days, d = [], start
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return pd.DataFrame({"Date": pd.to_datetime(days), "Open": closes, "High": [c * 1.01 for c in closes],
                         "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols})


WIKI = """{| class="wikitable sortable" id="constituents"
! Symbol !! Security !! GICS Sector
|-
| {{NyseSymbol|MMM}}
| [[3M]]
| [[Industrials]]
| Industrial Conglomerates
|-
|{{NasdaqSymbol|AAPL}}||[[Apple Inc.|Apple]]||Information Technology||Technology Hardware
|-
| {{NyseSymbol|BRK.B}}
| [[Berkshire Hathaway]]
| Financials
|-
| {{NyseSymbol|MMM}}
| [[3M]]
| Industrials
|}"""


class ParsingTests(unittest.TestCase):
    def test_wiki_rows(self):
        intro = "Cross referenced with index funds: Vanguard ({{NYSE Arca|VIOO}}) and SPDR ({{NYSE Arca|SPSM}}).\n\n"
        rows = us.parse_wiki_constituents(intro + WIKI, "S&P 500")
        self.assertEqual([r["symbol"] for r in rows], ["MMM", "AAPL", "BRK.B"])          # duplicate row and intro ETFs dropped
        self.assertEqual(rows[1], {"symbol": "AAPL", "name": "Apple", "sector": "Information Technology", "index": "S&P 500"})
        self.assertEqual(rows[2]["sector"], "Financials")

    def test_money_and_symbols(self):
        self.assertEqual(us.money("$4.10"), 4.1)
        self.assertEqual(us.money("($0.09)"), -0.09)
        self.assertEqual(us.money("$277,622,751,000"), 277622751000.0)
        self.assertIsNone(us.money("N/A"))
        self.assertIsNone(us.money(""))
        self.assertEqual(us.sym_key("BRK.B"), us.sym_key("BRK/B"))
        self.assertEqual(us.yahoo_symbol("BRK.B"), "BRK-B")

    def test_calendar_rows_keep_only_the_universe(self):
        payload = {"data": {"rows": [
            {"eps": "$3.27", "surprise": "6.86", "time": "time-not-supplied", "symbol": "COP", "marketCap": "$162,996,716,000",
             "fiscalQuarterEnding": "Sep/2026", "epsForecast": "$3.06"},
            {"eps": "($0.09)", "surprise": "-550", "time": "time-after-hours", "symbol": "NET", "marketCap": "$112,893,559,000",
             "fiscalQuarterEnding": "Sep/2026", "epsForecast": "$0.02"},
            {"eps": "$4.10", "surprise": "N/A", "time": "time-pre-market", "symbol": "TM", "marketCap": "", "epsForecast": ""}]}}
        rows = us.parse_calendar(payload, date(2026, 11, 5), {"COP", "NET"})
        self.assertEqual([r["key"] for r in rows], ["COP", "NET"])
        self.assertEqual((rows[0]["eps"], rows[0]["epsForecast"], rows[0]["surprisePct"], rows[0]["time"]), (3.27, 3.06, 6.86, None))
        self.assertEqual((rows[1]["eps"], rows[1]["surprisePct"], rows[1]["time"]), (-0.09, -550.0, "post"))
        self.assertEqual(rows[0]["marketCap"], 162996716000.0)


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.closes = [100.0] * 40 + [100.0, 108.0, 109.0, 110.0]
        self.f = frame(self.closes, [1000] * 40 + [1100, 6000, 2500, 2000])
        self.day = self.f["Date"].iloc[40].date()         # result date on the calendar

    def test_time_known(self):
        self.assertEqual(us.pick_reaction_index(self.f, self.day, "pre"), 40)
        self.assertEqual(us.pick_reaction_index(self.f, self.day, "post"), 41)

    def test_time_unknown_takes_the_heavier_session(self):
        self.assertEqual(us.pick_reaction_index(self.f, self.day, None), 41)      # reported after the close
        f2 = frame([100.0] * 40 + [108.0, 109.0, 110.0], [1000] * 40 + [6000, 2500, 2000])
        self.assertEqual(us.pick_reaction_index(f2, f2["Date"].iloc[40].date(), None), 40)   # reported before the open

    def test_too_early_and_weekend(self):
        f3 = self.f.iloc[:41]                              # only the result date has traded, on normal volume
        self.assertIsNone(us.pick_reaction_index(f3, self.day, None))
        self.assertIsNone(us.pick_reaction_index(f3, self.day, "post"))
        sat = self.f["Date"].iloc[39].date() + timedelta(days=1) if self.f["Date"].iloc[39].date().weekday() == 4 else None
        if sat:
            self.assertEqual(us.pick_reaction_index(self.f, sat, None), 40)


class GrowthTests(unittest.TestCase):
    Q = {"2026-09-30": {"rev": 1200.0, "ni": 150.0}, "2026-06-30": {"rev": 1100.0, "ni": 120.0},
         "2025-09-30": {"rev": 1000.0, "ni": 100.0}}

    def test_yoy_uses_the_same_quarter_last_year(self):
        g = us.yoy_from_quarters(self.Q, date(2026, 10, 28))
        self.assertEqual((g["quarterEnd"], g["revYoY"], g["niYoY"], g["niTrend"]), ("2026-09-30", 20.0, 50.0, "PROFIT_GROWTH"))

    def test_missing_year_ago_quarter_stays_null(self):
        g = us.yoy_from_quarters({k: v for k, v in self.Q.items() if k != "2025-09-30"}, date(2026, 10, 28))
        self.assertEqual(g["revenue"], 1200.0)
        self.assertIsNone(g["revYoY"])
        self.assertIsNone(g["niYoY"])
        self.assertEqual(us.yoy_from_quarters({}, date(2026, 10, 28)), {})

    def test_loss_to_profit_is_a_turnaround_not_a_percentage(self):
        g = us.yoy_from_quarters({"2026-09-30": {"rev": 500.0, "ni": 20.0}, "2025-09-30": {"rev": 400.0, "ni": -30.0}}, date(2026, 11, 2))
        self.assertEqual((g["niTrend"], g["niYoY"]), ("TURNAROUND", None))


class BuildItemTests(unittest.TestCase):
    META = {"symbol": "TEST", "name": "Test Corp", "sector": "Industrials", "index": "S&P 400"}

    def _item(self, fund):
        closes = [100.0 + i * 0.05 for i in range(60)] + [110.0, 111.0, 110.5, 112.0, 111.5, 113.0]
        vols = [1000] * 60 + [6000, 2200, 700, 2300, 600, 2400]
        f = frame(closes, vols)
        ev = {"key": "TEST", "date": f["Date"].iloc[60].date().isoformat(), "time": "pre", "eps": 1.2, "epsForecast": 1.0,
              "surprisePct": 20.0, "marketCap": 5.0e9, "fiscalQuarter": "Sep/2026"}
        return us.build_item(self.META, ev, f, fund, today=date(2026, 9, 1))[0], f

    def test_strong_result_gets_a_plan_with_the_stop_under_the_result_day_low(self):
        item, f = self._item({"revYoY": 20.0, "niYoY": 50.0, "niTrend": "PROFIT_GROWTH", "revenue": 1200.0, "netIncome": 150.0})
        self.assertEqual(item["strength"], "STRONG")
        self.assertEqual(item["reaction"]["date"], f["Date"].iloc[60].date().isoformat())
        self.assertAlmostEqual(item["reaction"]["pct"], (110.0 / (100.0 + 59 * 0.05) - 1) * 100, places=1)
        self.assertEqual(item["reaction"]["low"], 108.9)
        self.assertIn(item["grade"]["tier"], "ABCD")
        if item["plan"]["sl"] is not None:
            self.assertEqual(item["plan"]["sl"], round(108.9 * 0.99, 2))
        self.assertEqual(item["marketCapUsdM"], 5000.0)
        self.assertIsNotNone(item["hands"])

    def test_no_growth_figures_means_no_levels(self):
        item, _ = self._item({})
        self.assertIsNone(item["strength"])
        self.assertEqual(item["plan"]["signal"], "DATA_PENDING")
        self.assertIsNone(item["plan"]["entry"])
        self.assertIsNone(item["revYoY"])


if __name__ == "__main__":
    unittest.main()
