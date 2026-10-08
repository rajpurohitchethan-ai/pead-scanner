"""Regression tests for the v2.2 data-integrity fixes.

Run:  python -m unittest discover -s tests -v
Every test reproduces a bug that was present in engine 2.1.7.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pead_v2 as p  # noqa: E402


def bse_row(code, dt, headline, name="Test Co"):
    return {"SCRIP_CD": code, "DT_TM": dt, "NEWS_DT": dt, "HEADLINE": headline, "NEWSSUB": f"{name} - {code} - Result",
            "MORE": "", "SLONGNAME": name, "CATEGORYNAME": "Result"}


class DateParsing(unittest.TestCase):
    def test_bse_iso_timestamp_is_not_day_first(self):
        # Was 2026-11-09 -> pushed a September Q1 filing into the future / Q2.
        self.assertEqual(p.parse_date("2026-09-11T16:08:22.73"), date(2026, 9, 11))
        self.assertEqual(p.parse_date("2026-10-06T21:36:47.333"), date(2026, 10, 6))
        self.assertEqual(p.parse_datetime("2026-09-11").date(), date(2026, 9, 11))
        self.assertEqual(p.parse_datetime("2026-09-11T16:08:22.73").hour, 16)

    def test_indian_formats_still_day_first(self):
        self.assertEqual(p.parse_date("11-09-2026"), date(2026, 9, 11))
        self.assertEqual(p.parse_date("24-Aug-2026 17:39:15"), date(2026, 8, 24))
        self.assertEqual(p.parse_date("31-OCT-2025"), date(2025, 10, 31))


class PeriodFromFilingText(unittest.TestCase):
    def test_cases_from_real_bse_headlines(self):
        cases = [
            ("Financial Results for the quarter ended June 2026", date(2026, 10, 5), date(2026, 6, 30)),  # Rentomojo
            ("Results for the quarter/half year ended on 30th September", date(2026, 10, 3), date(2026, 9, 30)),  # Hawa
            ("unaudited financial result for the quarter ended 31.12.2025", date(2026, 9, 15), date(2025, 12, 31)),  # CMI
            ("Financial Results of the Quarter & Financial Year ended on 31.03.2025", date(2026, 10, 7), date(2025, 3, 31)),
            ("Audited Results for quarter ended June 30, 2026", date(2026, 10, 6), date(2026, 6, 30)),
            ("Financial Results for Q2 FY 2026-27", date(2026, 10, 9), date(2026, 9, 30)),
        ]
        for text, filed, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(p.extract_period_from_text(text, filed), expected)

    def test_period_after_filing_date_is_impossible(self):
        self.assertIsNone(p.extract_period_from_text("quarter ended 30-Sep-2026", date(2026, 9, 20)))
        self.assertIsNone(p.extract_period_from_text("Outcome of board meeting", date(2026, 10, 7)))

    def test_late_filer_not_labelled_live_quarter(self):
        c = p.normalize_bse_announcement(bse_row(539833, "2026-10-07T12:21:05.41",
                                                 "Financial Results of the Quarter & Financial Year ended on 31.03.2025"))
        self.assertEqual(c["periodEnd"], date(2025, 3, 31))
        self.assertEqual(c["periodSource"], "FILING_TEXT")


def xbrl_doc(include_quarter=True, include_owner=True):
    ctx = []

    def c(cid, start, end, member=None):
        seg = f"<xbrli:segment><xbrldi:explicitMember dimension='x:Seg'>{member}</xbrldi:explicitMember></xbrli:segment>" if member else ""
        ctx.append(f"<xbrli:context id='{cid}'><xbrli:entity><xbrli:identifier scheme='s'>X</xbrli:identifier>{seg}</xbrli:entity>"
                   f"<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>")
    if include_quarter:
        c("Q", "2026-07-01", "2026-09-30")
    c("H1", "2026-04-01", "2026-09-30")
    c("PQ", "2025-07-01", "2025-09-30")
    c("LQ", "2026-04-01", "2026-06-30")
    c("SEG", "2026-07-01", "2026-09-30", "x:Chemicals")
    facts = [
        ("in-capmkt:NatureOfReportStandaloneConsolidated", "Q", None, "Consolidated"),
        ("in-capmkt:RevenueFromOperations", "H1", "INR", "20000000000"),
        ("in-capmkt:RevenueFromOperations", "PQ", "INR", "8000000000"),
        ("in-capmkt:RevenueFromOperations", "LQ", "INR", "9500000000"),
        ("in-capmkt:RevenueFromOperations", "SEG", "INR", "3000000000"),
        ("in-capmkt:ProfitBeforeTax", "Q", "INR", "1500000000"),
        ("in-capmkt:ProfitLossForPeriod", "Q", "INR", "1200000000"),
        ("in-capmkt:ProfitLossForPeriod", "PQ", "INR", "1000000000"),
        ("in-capmkt:ProfitLossForPeriod", "LQ", "INR", "1100000000"),
        ("in-capmkt:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "Q", "INRPerShare", "12.5"),
    ]
    if include_quarter:
        facts.append(("in-capmkt:RevenueFromOperations", "Q", "INR", "10000000000"))
    if include_owner:
        facts += [("in-capmkt:ProfitLossForPeriodAttributableToOwnersOfParent", "Q", "INR", "1100000000"),
                  ("in-capmkt:ProfitLossForPeriodAttributableToOwnersOfParent", "PQ", "INR", "900000000"),
                  ("in-capmkt:ProfitLossForPeriodAttributableToOwnersOfParent", "LQ", "INR", "1000000000")]
    body = "".join(
        f"<{n} contextRef='{cx}'" + (f" unitRef='{u}' decimals='-5'" if u else "") + f">{v}</{n}>" for n, cx, u, v in facts
    )
    units = ("<xbrli:unit id='INR'><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>"
             "<xbrli:unit id='INRPerShare'><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:INR</xbrli:measure>"
             "</xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>")
    return (
        "<xbrli:xbrl xmlns:xbrli='http://www.xbrl.org/2003/instance' xmlns:xbrldi='http://xbrl.org/2006/xbrldi' "
        "xmlns:in-capmkt='http://example/in-capmkt' xmlns:iso4217='http://www.xbrl.org/2003/iso4217'>"
        + "".join(ctx) + units + body + "</xbrli:xbrl>"
    ).encode()


class XbrlParser(unittest.TestCase):
    def test_quarter_owner_pat_same_document_comparatives(self):
        r = p.parse_xbrl_financials([xbrl_doc()], date(2026, 9, 30))
        self.assertEqual(r["revenue_cr"], 1000.0)          # not H1 (2000) and not segment (300)
        self.assertEqual(r["pat_cr"], 110.0)               # owners of parent, not PBT 150 / total 120
        self.assertEqual(r["prior_year_pat_cr"], 90.0)     # same concept as current
        self.assertEqual(r["revenue_yoy_pct"], 25.0)
        self.assertEqual(r["revenue_qoq_pct"], 5.26)
        self.assertEqual(r["eps"], 12.5)
        self.assertEqual(r["basis"], "CONSOLIDATED")

    def test_half_year_value_never_used_as_quarter(self):
        r = p.parse_xbrl_financials([xbrl_doc(include_quarter=False)], date(2026, 9, 30))
        self.assertIsNone(r["revenue_cr"])


class ExchangeTables(unittest.TestCase):
    def nse_payload(self):
        def row(fr, to, sale, total, np_, seq):
            return {"re_from_dt": fr, "re_to_dt": to, "re_net_sale": sale, "re_total_inc": total,
                    "re_net_profit": np_, "re_seq_num": seq, "re_basic_eps_for_cont_dic_opr": "1"}
        return {"bankNonBnking": "N", "resCmpData": [
            row("01-JUL-2026", "30-SEP-2026", "60000", "70000", "6000", "5"),
            row("01-APR-2026", "30-SEP-2026", "999999", "999999", "99999", "4"),   # half-year row
            row("01-APR-2026", "30-JUN-2026", "55000", "56000", "5000", "3"),
            row("01-JUL-2025", "30-SEP-2025", "50000", "52000", "4000", "1"),
        ]}

    def test_nse_revenue_is_net_sales_not_total_income(self):
        r = p.parse_nse_comparison(self.nse_payload(), date(2026, 9, 30))
        self.assertEqual(r["revenue_cr"], 600.0)
        self.assertEqual(r["revenue_yoy_pct"], 20.0)
        self.assertEqual(r["revenue_qoq_pct"], 9.09)

    def test_nse_stale_payload_rejected(self):
        self.assertEqual(p.parse_nse_comparison(self.nse_payload(), date(2026, 12, 31)), {})

    def test_bse_no_first_column_fallback_and_no_fy_qoq(self):
        snap = {"currency_unit": "in Cr.", "results_in_crores": {
            "fields": ["title", "Sep-26", "FY25-26", "Period3"],
            "data": [["Revenue", "20", "100", "--"], ["Net Profit", "1", "5", "--"], ["EPS", "1", "5", "--"]]}}
        r = p.parse_bse_snapshot(snap, date(2026, 9, 30))
        self.assertEqual(r["revenue_cr"], 20.0)
        self.assertIsNone(r["revenue_qoq_pct"])
        self.assertEqual(p.parse_bse_snapshot(snap, date(2026, 6, 30)), {})


class SnapshotSelection(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = p.EventStore(Path(self.td.name) / "events")
        self.e = self.store.ensure_event(security={"symbol": "T", "nseSymbol": "T"}, period_end=date(2026, 9, 30))

    def tearDown(self):
        self.td.cleanup()

    def test_one_snapshot_no_cross_source_borrowing(self):
        xbrl = p.parse_xbrl_financials([xbrl_doc()], date(2026, 9, 30))
        for k in ("prior_year_revenue_cr", "revenue_yoy_pct"):
            xbrl[k] = None   # winner lacks prior-year revenue
        nse = p.parse_nse_comparison(ExchangeTables().nse_payload(), date(2026, 9, 30))
        p.store_financial_snapshot(self.e, "NSE_RESULTS_COMPARISON", nse)
        p.store_financial_snapshot(self.e, "NSE_XBRL", xbrl)
        integ = p.apply_financial_snapshots(self.store, self.e)
        self.assertEqual(integ["selectedSource"], "NSE_XBRL")
        self.assertEqual(self.store.value(self.e, "revenue_cr"), 1000.0)
        self.assertIsNone(self.store.value(self.e, "revenue_yoy_pct"))   # not borrowed from standalone NSE
        self.assertEqual(self.store.value(self.e, "basis"), "CONSOLIDATED")

    def test_same_basis_mismatch_is_flagged(self):
        a = p.parse_nse_comparison(ExchangeTables().nse_payload(), date(2026, 9, 30))
        b = json.loads(json.dumps(a))
        b["revenue_cr"] = 700.0
        b["_meta"]["issues"] = []
        p.store_financial_snapshot(self.e, "NSE_RESULTS_COMPARISON", a)
        p.store_financial_snapshot(self.e, "NSE_XBRL", b | {"basis": "STANDALONE"})
        integ = p.apply_financial_snapshots(self.store, self.e)
        self.assertEqual(integ["status"], "FLAGGED")
        self.assertIn("CROSS_SOURCE_MISMATCH", integ["issues"])

    def test_rejected_parse_never_erases_good_snapshot(self):
        good = p.parse_nse_comparison(ExchangeTables().nse_payload(), date(2026, 9, 30))
        p.store_financial_snapshot(self.e, "NSE_RESULTS_COMPARISON", good)
        p.store_financial_snapshot(self.e, "NSE_RESULTS_COMPARISON", {"_meta": {"periodEnd": "2026-09-30"}})
        snap = self.e["financialSnapshots"]["NSE_RESULTS_COMPARISON"]
        self.assertEqual(snap["validation"]["status"], "VERIFIED")
        self.assertIn("lastRejected", snap)

    def test_wrong_period_snapshot_rejected(self):
        bad = p.parse_nse_comparison(ExchangeTables().nse_payload(), date(2026, 9, 30))
        bad["_meta"]["periodEnd"] = "2026-06-30"
        snap = p.store_financial_snapshot(self.e, "NSE_RESULTS_COMPARISON", bad)
        self.assertEqual(snap["validation"]["status"], "REJECTED")


class IntegrityPass(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.raw = root / "raw"
        (self.raw / "bse" / "DISCOVERY").mkdir(parents=True)
        rows = [
            bse_row(111111, "2026-10-03T15:46:22.89", "Results for the quarter/half year ended on 30th September"),
            bse_row(222222, "2026-09-11T16:08:22.73", "results for the quarter ended 31.12.2025"),
            bse_row(333333, "2026-10-07T16:33:17.86", "Un-Audited results for the Quarter ended on 30th September,2026"),
        ]
        (self.raw / "bse" / "DISCOVERY" / "result_announcements-x.json").write_text(json.dumps(rows))
        self.store = p.EventStore(root / "events")

    def tearDown(self):
        self.td.cleanup()

    def make(self, code, fields, state="RESULT_FILED"):
        e = self.store.ensure_event(security={"symbol": f"S{code}", "bseCode": str(code)}, period_end=date(2026, 9, 30))
        for f, v, src, note in fields:
            self.store.merge_field(e, f, v, source=src, note=note)
        e["state"] = state
        self.store.save(e)
        return e["eventId"]

    def test_false_future_declaration_revoked_and_reaction_purged(self):
        eid = self.make(222222, [
            ("results_released", True, "BSE_RESULT_ANNOUNCEMENT", "migrated last-known-good v1 release proof"),
            ("filing_timestamp", "2026-11-09T16:08:22+05:30", "BSE_RESULT_ANNOUNCEMENT", None),
            ("result_day_return_pct", -4.9, "BSE_PRICE", None),
            ("revenue_cr", 10, "V1_MIGRATION", "migrated from v1 data.json"),
        ])
        stats = p.integrity_pass(self.store, today=date(2026, 10, 7), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertEqual(stats["revoked"], 1)
        self.assertIsNot(p.boolish(self.store.value(e, "results_released")), True)
        self.assertIsNone(self.store.value(e, "result_day_return_pct"))
        self.assertIsNone(self.store.value(e, "revenue_cr"))
        self.assertIn(e["state"], {"SCHEDULED", "DISCOVERED"})
        self.assertEqual(e["fields"]["results_released"]["status"], "REVOKED")   # kept for audit

    def test_swapped_date_reverified_from_evidence(self):
        eid = self.make(111111, [
            ("results_released", True, "BSE_RESULT_ANNOUNCEMENT", None),
            ("filing_timestamp", "2026-03-10T15:46:22+05:30", "BSE_RESULT_ANNOUNCEMENT", None),
        ])
        p.integrity_pass(self.store, today=date(2026, 10, 7), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertTrue(self.store.value(e, "results_released"))
        self.assertEqual(self.store.value(e, "result_date"), "2026-10-03")

    def test_explicit_period_filing_promotes_scheduled_event(self):
        eid = self.make(333333, [("result_date", "2026-10-07", "BSE_RESULT_ANNOUNCEMENT", None)], state="SCHEDULED")
        p.integrity_pass(self.store, today=date(2026, 10, 7), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertTrue(self.store.value(e, "results_released"))
        self.assertEqual(e["state"], "RESULT_FILED")

    def test_unreleased_event_cannot_hold_reaction_data(self):
        eid = self.make(444444, [("result_date", "2026-10-20", "NSE_FINANCIAL_RESULTS", None),
                           ("result_day_rvol", 3.1, "NSE_PRICE", None)], state="SCHEDULED")
        p.integrity_pass(self.store, today=date(2026, 10, 7), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertIsNone(self.store.value(e, "result_day_rvol"))
        self.assertEqual(self.store.value(e, "result_date"), "2026-10-20")   # calendar date kept


class PublishGate(unittest.TestCase):
    base = {"liveQuarter": "Q2 FY27", "activeDashboardEvents": 480, "resultsFiled": 24, "declaredCompletenessPct": 30}

    def test_drop_blocked_without_matching_approval(self):
        new = dict(self.base, resultsFiled=2, integrity={"signature": "abc123", "revoked": 22})
        self.assertFalse(p.quality_gate(new, self.base)[0])
        self.assertFalse(p.quality_gate(new, self.base, "zzz999")[0])
        self.assertTrue(p.quality_gate(new, self.base, "abc123")[0])

    def test_approval_does_not_bypass_zero_rows(self):
        new = dict(self.base, activeDashboardEvents=0, resultsFiled=0, integrity={"signature": "abc123"})
        self.assertFalse(p.quality_gate(new, self.base, "abc123")[0])

    def test_baseline_is_last_published_feed_not_blocked_run(self):
        with tempfile.TemporaryDirectory() as td:
            old = (p.DATA_PATH, p.INTELLIGENCE_PATH, p.HEALTH_PATH)
            try:
                p.DATA_PATH, p.INTELLIGENCE_PATH, p.HEALTH_PATH = (Path(td) / "d.json", Path(td) / "i.json", Path(td) / "h.json")
                p.DATA_PATH.write_text(json.dumps({"health": {"resultsFiled": 24}}))
                p.HEALTH_PATH.write_text(json.dumps({"resultsFiled": 2}))   # blocked run
                self.assertEqual(p.previous_health()["resultsFiled"], 24)
            finally:
                p.DATA_PATH, p.INTELLIGENCE_PATH, p.HEALTH_PATH = old

    def test_signature_is_order_independent(self):
        a = [{"eventId": "A", "reasons": ["FUTURE_FILING_DATE"]}, {"eventId": "B", "reasons": ["X:1"]}]
        self.assertEqual(p.integrity_signature(a), p.integrity_signature(list(reversed(a))))


if __name__ == "__main__":
    unittest.main()
