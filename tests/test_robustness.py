"""Engine 2.9.4 regression tests: run guard and the 10 Oct 2026 data audit."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pead_v2 as p  # noqa: E402


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class RunGuardTests(unittest.TestCase):
    """9 Oct 23:58 run: NSE stopped answering, every call waited out the
    library's retries, the job hit the 60-minute kill and the hour was lost."""

    def setUp(self):
        p.RUN_GUARD.reset()
        self.sleep = mock.patch.object(p.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        p.RUN_GUARD.reset()

    def test_host_switched_off_after_repeated_outage_failures(self):
        calls = {"n": 0}

        def down():
            calls["n"] += 1
            raise TimeoutError("read timeout")

        for _ in range(10):
            with self.assertRaises(Exception):
                p.with_retry(down, source="NSE_PRICE")
        self.assertIn("NSE", p.RUN_GUARD.down)
        # Once off, calls fail fast without touching the network.
        self.assertEqual(calls["n"], p.SOURCE_BREAKER_ATTEMPTS)
        with self.assertRaises(p.SourceSkipped):
            p.with_retry(down, source="NSE")
        # Other hosts are unaffected.
        self.assertEqual(p.with_retry(lambda: 7, source="BSE"), 7)

    def test_missing_pages_do_not_trip_the_breaker(self):
        def missing():
            raise HttpError(404)

        for _ in range(20):
            with self.assertRaises(HttpError):
                p.with_retry(missing, source="NSE")
        self.assertNotIn("NSE", p.RUN_GUARD.down)

    def test_success_resets_the_streak(self):
        for _ in range(3):
            with self.assertRaises(TimeoutError):
                p.with_retry(lambda: (_ for _ in ()).throw(TimeoutError()), source="BSE")
        p.with_retry(lambda: 1, source="BSE")
        self.assertEqual(p.RUN_GUARD.streak["BSE"], 0)
        self.assertNotIn("BSE", p.RUN_GUARD.down)

    def test_slow_failures_trip_on_time(self):
        p.RUN_GUARD.failed("NSE", TimeoutError(), p.SOURCE_BREAKER_FAIL_SEC + 1)
        self.assertIn("NSE", p.RUN_GUARD.down)

    def test_budget_spent_skips_calls(self):
        p.RUN_GUARD.reset(started=p.time.monotonic() - p.RUN_BUDGET_SEC - 1)
        with self.assertRaises(p.SourceSkipped):
            p.with_retry(lambda: 1, source="BSE")
        self.assertTrue(p.RUN_GUARD.summary()["budgetHit"])
        self.assertFalse(p._yahoo_budget("price", 99))

    def test_calls_without_source_are_unguarded(self):
        p.RUN_GUARD.reset(started=p.time.monotonic() - p.RUN_BUDGET_SEC - 1)
        self.assertEqual(p.with_retry(lambda: 3), 3)


def bse_row(code, dt, headline, name="Test Co"):
    return {"SCRIP_CD": code, "DT_TM": dt, "NEWS_DT": dt, "HEADLINE": headline, "NEWSSUB": f"{name} - {code} - Result",
            "MORE": "", "SLONGNAME": name, "CATEGORYNAME": "Result"}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.raw = root / "raw"
        (self.raw / "bse" / "DISCOVERY").mkdir(parents=True)
        (self.raw / "bse" / "DISCOVERY" / "result_announcements-x.json").write_text(json.dumps(self.rows()))
        self.store = p.EventStore(root / "events")

    def rows(self):
        return []

    def tearDown(self):
        self.td.cleanup()

    def make(self, code, fields, state):
        e = self.store.ensure_event(security={"symbol": f"S{code}", "bseCode": str(code)}, period_end=date(2026, 9, 30))
        for f, v, src in fields:
            self.store.merge_field(e, f, v, source=src)
        e["state"] = state
        self.store.save(e)
        return e["eventId"]


from datetime import date  # noqa: E402


class InferredPeriodTests(_StoreCase):
    """NATURO: "Revised Outcome Of The Board Meeting Held On Thursday, November
    20, 2025", filed 9 Oct 2026, was shown as a declared Q2 FY27 result."""
    HEADLINE = "Revised Outcome Of The Board Meeting Held On Thursday, November 20, 2025"

    def rows(self):
        return [bse_row(543579, "2026-10-09T14:45:47.217", self.HEADLINE, "Naturo Indiabull Ltd")]

    def test_inferred_period_filing_does_not_declare(self):
        c = p.normalize_bse_announcement(self.rows()[0])
        self.assertEqual(c["periodSource"], "INFERRED_FROM_FILING_DATE")
        self.assertEqual(p.apply_discovery(self.store, p.SymbolMaster(), c, None), {})
        self.assertEqual(self.store.all(), [])

    def test_declaration_resting_on_inferred_evidence_is_revoked(self):
        eid = self.make(543579, [("results_released", True, "BSE_RESULT_ANNOUNCEMENT"),
                                 ("filing_timestamp", "2026-10-09T14:45:47+05:30", "BSE_RESULT_ANNOUNCEMENT")],
                        "RESULT_FILED")
        stats = p.integrity_pass(self.store, today=date(2026, 10, 10), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertIsNot(p.boolish(self.store.value(e, "results_released")), True)
        self.assertIn("ONLY_DATE_INFERRED_EVIDENCE", stats["revocations"][0]["reasons"])


class LateEarlierQuarterMeetingTests(_StoreCase):
    """SRUSTEELS met on 9 Oct 2026 for its June-2026 results; the BSE calendar
    row (no period) put that date on the Q2 event as a result due that day."""

    def rows(self):
        return [bse_row(540914, "2026-10-09T16:13:02.943", "Unaudited Financial Results for Quarter ended 30th June 2026")]

    def test_calendar_date_detached_and_not_reapplied(self):
        cal = p.normalize_bse_calendar({"scrip_Code": "540914", "short_name": "SRUSTEELS", "meeting_date": "09 Oct 2026"})
        eid = p.apply_discovery(self.store, p.SymbolMaster(), cal, None)["eventId"]
        self.assertEqual(self.store.value(self.store.load(eid), "result_date"), "2026-10-09")
        stats = p.integrity_pass(self.store, today=date(2026, 10, 10), raw_root=self.raw)
        e = self.store.load(eid)
        self.assertIsNone(self.store.value(e, "result_date"))
        self.assertIsNot(p.boolish(self.store.value(e, "results_released")), True)
        self.assertEqual(stats.get("calendarDatesDetached"), 1)
        p.apply_discovery(self.store, p.SymbolMaster(), cal, None)
        self.assertEqual(len(self.store.all()), 1)
        self.assertIsNone(self.store.value(self.store.load(eid), "result_date"))
        # A later, genuine Q2 meeting date is accepted.
        cal2 = p.normalize_bse_calendar({"scrip_Code": "540914", "short_name": "SRUSTEELS", "meeting_date": "12 Nov 2026"})
        p.apply_discovery(self.store, p.SymbolMaster(), cal2, None)
        self.assertEqual(self.store.value(self.store.load(eid), "result_date"), "2026-11-12")

    def test_q2_meeting_with_no_earlier_filing_is_kept(self):
        eid = self.make(777777, [("result_date", "2026-10-09", "BSE_RESULT_ANNOUNCEMENT")], "SCHEDULED")
        p.integrity_pass(self.store, today=date(2026, 10, 10), raw_root=self.raw)
        self.assertEqual(self.store.value(self.store.load(eid), "result_date"), "2026-10-09")


class PatModelQoQTests(unittest.TestCase):
    """HATHWAYB 0.04 -> -0.10 Cr was published as "-350.0% profit"."""

    def test_profit_to_loss_has_no_ordinary_percent(self):
        self.assertIsNone(p.pat_change_pct(-0.10, 0.04))
        self.assertEqual(p.pat_trend(-0.10, 0.04)[0], "DETERIORATION")
        self.assertIsNone(p.pat_change_pct(-3.8, 0.02))
        self.assertIsNone(p.pat_change_pct(-1.0, -2.0))
        self.assertAlmostEqual(p.pat_change_pct(12.0, 10.0), 20.0)

    def _listing(self):
        return [{"periodEnd": pe, "fromDate": fd, "basis": "STANDALONE", "cumulative": False, "xbrlUrl": u, "filedAt": None}
                for pe, fd, u in (("2025-09-30", "2025-07-01", "ya"), ("2026-06-30", "2026-04-01", "pq"))]

    def test_comparatives_use_pat_model_and_exact_values(self):
        # INDBANK: PAT Rs 16,149k / 18,556k (year ago) / 23,753k (last quarter).
        C = {"revenue": "rev", "pat": "pat"}
        docs = {"ya": (10.0, 1.8556), "pq": (11.0, 2.3753)}
        fetch = lambda u, pe: {"revenue_cr": docs[u][0], "pat_cr": round(docs[u][1], 2),
                               "_meta": {"concepts": C, "exact": {"revenue_cr": docs[u][0], "pat_cr": docs[u][1]}}}
        parsed = {"revenue_cr": 10.5, "pat_cr": 1.61, "_meta": {"concepts": C, "exact": {"revenue_cr": 10.5, "pat_cr": 1.6149}}}
        p.fill_comparatives_from_listing(parsed, self._listing(), date(2026, 9, 30), "STANDALONE", fetch)
        self.assertEqual(parsed["pat_yoy_pct"], -12.97)       # -13.44 from rounded crores
        self.assertEqual(parsed["pat_qoq_pct"], -32.01)       # -32.35 from rounded crores

        docs["pq"] = (11.0, 0.04)
        parsed = {"revenue_cr": 10.5, "pat_cr": -0.1, "_meta": {"concepts": C}}
        p.fill_comparatives_from_listing(parsed, self._listing(), date(2026, 9, 30), "STANDALONE", fetch)
        self.assertIsNone(parsed["pat_qoq_pct"])
        self.assertEqual(parsed["pat_qoq_trend"], "DETERIORATION")
        # The unit check still sees the size of the swing.
        self.assertEqual(parsed["_meta"]["patQoQRawPct"], -350.0)


class ExchangeZeroPlaceholderTests(unittest.TestCase):
    """NSE sends pdSectorPe "0" (RALLIS, WESTLIFE, COFFEEDAY) and BSE ROE/OPM
    "0.00" (TIAANC, GOLKONDA) when it has no figure; missing is not zero."""

    def test_parsers_return_null(self):
        self.assertIsNone(p.parse_nse_quote({"metadata": {"pdSectorPe": "0", "pdSymbolPe": "25.1"}})["sector_pe"])
        m = p.parse_bse_meta({"PE": "0", "ROE": "0.00", "OPM": "0.00"})
        self.assertIsNone(m["roe_pct"])
        self.assertIsNone(m["exchange_opm_ttm_pct"])
        self.assertEqual(p.parse_bse_meta({"OPM": "-4.5"})["exchange_opm_ttm_pct"], -4.5)

    def test_stored_zeros_publish_as_null(self):
        import pead_plus as pp
        v = pp.valuation_view(20, 0.0, 0.0, 10)
        self.assertIsNone(v["sectorPe"])
        self.assertIsNone(v["roe"])
        self.assertIsNone(v["peVsSector"])
        self.assertIsNone(p.nonzero(0.0))
        self.assertEqual(p.nonzero(-3.2), -3.2)



def _frame(closes, start, vols=None):
    import pandas as pd
    from datetime import timedelta
    rows, d = [], start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"Date": pd.Timestamp(d), "Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c,
                     "Volume": (vols[i] if vols else 1000)})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


class Q1IntradayWindowTests(unittest.TestCase):
    """GMBREW's Q1 result was filed 09 Jul 12:04 (market hours): the replay
    measured 09 Jul close -> 10 Jul close (-2.66%, RVOL 1.9) instead of
    08 Jul close -> 10 Jul close (-2.07%, RVOL 19 on the filing day)."""

    def test_two_session_window(self):
        import pead_plus as pp
        closes = [100.0] * 30 + [98.0, 96.0] + [96.0] * 10
        vols = [1000] * 30 + [20000, 1500] + [1000] * 10
        f = _frame(closes, date(2026, 5, 25), vols)
        d1, d2 = f["Date"].iloc[30].date(), f["Date"].iloc[31].date()
        one = pp.extended_features(f, None, released=False, q1_reaction_date=d2)
        two = pp.extended_features(f, None, released=False, q1_reaction_date=d2, q1_window_start=d1)
        self.assertEqual(one["q1_reaction_return_pct"], -2.04)    # old: from the filing day's close
        self.assertEqual(two["q1_reaction_return_pct"], -4.0)     # from the close before the filing day
        self.assertEqual(two["q1_reaction_rvol"], 20.0)           # filing-day volume counts


class StaleRunUpTests(unittest.TestCase):
    """ALSTONE (no trades since May) showed 5D / 10D 0.0% from months-old closes."""

    def test_untraded_scrip_has_no_run_up(self):
        old = _frame([50.0] * 40, date(2026, 3, 2))
        m = p.price_metrics(old, None)
        self.assertIsNone(m["pre_result_5d_pct"])
        self.assertTrue(m["_preResultStale"])

    def test_recent_prices_keep_run_up(self):
        from datetime import timedelta
        today = p.now_ist().date()
        fresh = _frame([100.0 + i for i in range(40)], today - timedelta(days=58))
        m = p.price_metrics(fresh, None)
        self.assertIsNotNone(m["pre_result_5d_pct"])
        self.assertFalse(m["_preResultStale"])

    def test_stored_stale_values_are_withdrawn(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            store = p.EventStore(Path(td) / "events")
            e = store.ensure_event(security={"symbol": "ALSTONE", "bseCode": "1"}, period_end=date(2026, 9, 30))
            store.merge_field(e, "pre_result_5d_pct", 0.0, source="BSE_PRICE")
            m = p.price_metrics(_frame([50.0] * 40, date(2026, 3, 2)), None)
            self.assertTrue(p.drop_stale_pre_result(store, e, m))
            self.assertIsNone(store.value(e, "pre_result_5d_pct"))
            self.assertNotIn("_preResultStale", m)



class NseSeriesFallbackTests(unittest.TestCase):
    """KABRAEXTRU's EQ history stopped on 30 Sep 2026 and its EQ quote was all
    nulls: the stock trades in another series (BE, trade-to-trade)."""

    class Client:
        def __init__(self, eq_end, active):
            self.eq_end, self.active, self.calls = eq_end, active, []

        def fetch_equity_historical_data(self, symbol, from_date=None, to_date=None, series="eq"):
            from datetime import timedelta
            self.calls.append(series)
            if series == "eq":
                d0 = self.eq_end - timedelta(days=3)
                return [{"mtimestamp": (d0 + timedelta(days=i)).strftime("%d-%b-%Y"), "chClosingPrice": 100 + i,
                         "chSeries": "EQ"} for i in range(4)]
            return [{"mtimestamp": "06-Oct-2026", "chClosingPrice": 120, "chSeries": "BE"},
                    {"mtimestamp": "09-Oct-2026", "chClosingPrice": 121, "chSeries": "BE"}]

        def equity_meta_info(self, symbol):
            self.calls.append("meta")
            return {"symbol": symbol, "activeSeries": self.active}

    def adapter(self, client):
        a = p.NSEAdapter.__new__(p.NSEAdapter)
        a.client = client
        a.ctx = mock.Mock()
        a.ctx.raw.save.return_value = "raw/ref"
        return a

    def setUp(self):
        p.RUN_GUARD.reset()
        p._EXTRA_CALLS_USED["n"] = 0
        self.sleep = mock.patch.object(p.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_stale_eq_history_is_extended_with_active_series(self):
        c = self.Client(date(2026, 9, 30), ["BE"])
        a = self.adapter(c)
        rows, _ = a.history("KABRAEXTRU", date(2025, 8, 1), date(2026, 10, 10), "e1")
        self.assertEqual(a.last_series, "BE")
        self.assertEqual(p.nse_rows_last_date(rows), date(2026, 10, 9))
        self.assertEqual(len(rows), 6)
        saved = a.ctx.raw.save.call_args[0][3]
        self.assertEqual(saved, rows)          # one combined payload for replays

    def test_fresh_eq_history_needs_no_extra_calls(self):
        c = self.Client(date(2026, 10, 9), ["EQ"])
        a = self.adapter(c)
        a.history("TCS", date(2025, 8, 1), date(2026, 10, 10), "e2")
        self.assertEqual(c.calls, ["eq"])
        self.assertEqual(a.last_series, "EQ")

    def test_untraded_stock_without_other_series(self):
        c = self.Client(date(2026, 3, 18), [])
        a = self.adapter(c)
        rows, _ = a.history("VIJIFIN", date(2025, 8, 1), date(2026, 10, 10), "e3")
        self.assertEqual(c.calls, ["eq", "meta"])
        self.assertIsNone(a.last_series)
        self.assertEqual(len(rows), 4)

    def test_quote_uses_series(self):
        client = mock.Mock()
        client.quote.return_value = {"x": 1}
        a = self.adapter(client)
        a.quote("KABRAEXTRU", "e1", series="BE")
        client.quote.assert_called_with("KABRAEXTRU", series="be")


class ProvenanceTests(unittest.TestCase):
    """GMBREW showed standalone figures but its fields still pointed at the
    consolidated XBRL (same numbers, so force_field skipped the write)."""

    def test_same_value_new_document_updates_provenance(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            store = p.EventStore(Path(td) / "events")
            e = store.ensure_event(security={"symbol": "GMBREW"}, period_end=date(2026, 9, 30))
            store.force_field(e, "revenue_cr", 860.62, source="NSE_XBRL", raw_ref="cons.xml", note="basis=CONSOLIDATED")
            self.assertTrue(store.force_field(e, "revenue_cr", 860.62, source="NSE_XBRL", raw_ref="stand.xml",
                                              note="basis=STANDALONE"))
            meta = e["fields"]["revenue_cr"]
            self.assertEqual((meta["rawRef"], meta["note"]), ("stand.xml", "basis=STANDALONE"))
            self.assertFalse(e.get("fieldHistory"))
            self.assertFalse(store.force_field(e, "revenue_cr", 860.62, source="NSE_XBRL", raw_ref="stand.xml",
                                               note="basis=STANDALONE"))



class ReparsedSnapshotTests(unittest.TestCase):
    """After the PAT-model fix re-parsed HATHWAYB's BSE table, the snapshot no
    longer had pat_qoq_pct but the visible field kept the old -350.0."""

    def test_field_missing_from_applied_snapshot_is_cleared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            store = p.EventStore(Path(td) / "events")
            e = store.ensure_event(security={"symbol": "HATHWAYB"}, period_end=date(2026, 9, 30))
            base = {"revenue_cr": 0.51, "pat_cr": -0.10, "revenue_qoq_pct": -10.53,
                    "_meta": {"periodEnd": "2026-09-30", "revenueDefinition": "BSE_SNAPSHOT_REVENUE",
                              "concepts": {"revenue": "Revenue", "pat": "Net Profit"}}}
            old = dict(base, pat_qoq_pct=-350.0)
            p.store_financial_snapshot(e, "BSE_RESULTS_SNAPSHOT", old, raw_ref="r1")
            p.apply_financial_snapshots(store, e)
            self.assertEqual(store.value(e, "pat_qoq_pct"), -350.0)
            new = dict(base, pat_qoq_trend="DETERIORATION")
            p.store_financial_snapshot(e, "BSE_RESULTS_SNAPSHOT", new, raw_ref="r1")
            p.apply_financial_snapshots(store, e)
            self.assertIsNone(store.value(e, "pat_qoq_pct"))
            self.assertEqual(store.value(e, "pat_qoq_trend"), "DETERIORATION")
            self.assertEqual(store.value(e, "pat_cr"), -0.10)



class TradingViewEmbedTests(unittest.TestCase):
    """2.9.5: TradingView's embed shows BSE (EOD) but not NSE, and only BSE's
    own ticker resolves (BSE:532540 -> "This symbol doesn't exist")."""

    def test_embed_symbol_needs_bse_ticker(self):
        self.assertEqual(p.screener_links({"nseSymbol": "TCS", "bseSymbol": "TCS", "bseCode": "532540"})["tvEmbed"], "BSE:TCS")
        self.assertIsNone(p.screener_links({"nseSymbol": "NSEONLY"})["tvEmbed"])
        self.assertIsNone(p.screener_links({"symbol": "X", "bseCode": "500001"})["tvEmbed"])


if __name__ == "__main__":
    unittest.main()
