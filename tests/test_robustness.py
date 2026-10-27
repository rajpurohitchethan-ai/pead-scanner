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



class HalfYearOnlyFilingTests(unittest.TestCase):
    """CORDELIA's Sep-2026 XBRL carries only the Apr-Sep (six-month) figure.
    Not a parse failure: the self-check flagged it red as "not parsed"."""

    def test_half_year_only_document(self):
        import pead_plus as pp
        blob = (Path(__file__).parent / "fixtures" / "nse" / "CORDELIA_H1_2026-09-30.xml").read_bytes()
        out = p.parse_xbrl_financials([blob], date(2026, 9, 30))
        issues = out.get("_meta", {}).get("issues") or []
        self.assertIn("HALF_YEAR_ONLY", issues)
        self.assertIsNone(out.get("revenue_cr"))      # no quarter figure invented from H1
        items = [{"symbol": "CORDELIA", "resultsReleased": True, "resultDate": "2026-10-09", "halfYearOnly": True,
                  "fundamentalSnapshot": {}, "plus": {"price": {"last_session": "2026-10-09"}}}]
        audit = pp.self_audit(items, {"asOf": "2026-10-09"}, {}, date(2026, 10, 11))
        bad = [c for c in audit["checks"] if c["status"] == "FAIL"]
        self.assertEqual(bad, [])


    def test_suspect_figures_warn_not_fail(self):
        import pead_plus as pp
        items = [{"symbol": "LCCINFOTEC", "resultsReleased": True, "resultDate": "2026-10-08",
                  "financialIssues": ["EXTREME_REVENUE_YOY", "UNIT_SUSPECT"], "fundamentalSnapshot": {},
                  "plus": {"price": {"last_session": "2026-10-09"}}}]
        audit = pp.self_audit(items, {"asOf": "2026-10-09"}, {}, date(2026, 10, 11))
        st = {c["check"]: c["status"] for c in audit["checks"]}
        self.assertEqual(st["Declared results withheld as suspect figures"], "WARN")
        self.assertEqual(st["Declared results have revenue/profit (filed 2+ days ago)"], "OK")



class Audit11OctTests(unittest.TestCase):
    """Second audit (11 Oct 2026)."""

    def setUp(self):
        p.RUN_GUARD.reset()
        p._SERIES_CALLS["n"] = 0
        self.sleep = mock.patch.object(p.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def _adapter(self, client):
        a = p.NSEAdapter.__new__(p.NSEAdapter)
        a.client, a.ctx = client, mock.Mock()
        a.ctx.raw.save.return_value = "raw/ref"
        return a

    def test_gap_in_eq_history_fetches_be(self):
        # FAIRCHEMOR: EQ rows jump from 03 Jul to 05 Oct 2026 (stock was in BE).
        calls = []

        def hist(symbol, from_date=None, to_date=None, series="eq"):
            calls.append(series)
            if series == "eq":
                return [{"mtimestamp": d, "chClosingPrice": 100} for d in ("02-Jul-2026", "03-Jul-2026", "05-Oct-2026", "09-Oct-2026")]
            return [{"mtimestamp": d, "chClosingPrice": 90} for d in ("06-Jul-2026", "15-Sep-2026", "01-Oct-2026")]
        c = mock.Mock(spec=["fetch_equity_historical_data"])
        c.fetch_equity_historical_data.side_effect = hist
        a = self._adapter(c)
        rows, _ = a.history("FAIRCHEMOR", date(2025, 8, 1), date(2026, 10, 10), "e")
        self.assertEqual(calls, ["eq", "be"])
        self.assertEqual(len(rows), 7)
        self.assertEqual(a.last_series, "EQ")

    def test_known_series_used_without_meta_call(self):
        # VIJIFIN trades in BE; the stored series is reused directly.
        calls = []

        def hist(symbol, from_date=None, to_date=None, series="eq"):
            calls.append(series)
            return ([{"mtimestamp": "18-Mar-2026", "chClosingPrice": 3.18}] if series == "eq"
                    else [{"mtimestamp": "09-Oct-2026", "chClosingPrice": 13.46}])
        c = mock.Mock(spec=["fetch_equity_historical_data", "equity_meta_info"])
        c.fetch_equity_historical_data.side_effect = hist
        a = self._adapter(c)
        rows, _ = a.history("VIJIFIN", date(2025, 8, 1), date(2026, 10, 10), "e", series_hint="BE")
        self.assertEqual(calls, ["eq", "be"])
        c.equity_meta_info.assert_not_called()
        self.assertEqual(p.nse_rows_last_date(rows), date(2026, 10, 9))

    def test_run_up_and_q1_replay_never_span_a_gap(self):
        import pead_plus as pp
        import pandas as pd
        from datetime import timedelta
        ds = [date(2026, 6, 1) + timedelta(days=i) for i in range(40) if (date(2026, 6, 1) + timedelta(days=i)).weekday() < 5]
        later = [p.now_ist().date() - timedelta(days=k) for k in range(12, 0, -1)]
        later = [d for d in later if d.weekday() < 5]
        f = pd.DataFrame([{"Date": pd.Timestamp(d), "Open": 100, "High": 101, "Low": 99, "Close": 100 if d in ds else 130,
                           "Volume": 1000} for d in ds + later])
        m = p.price_metrics(f, None)
        self.assertIsNone(m["pre_result_20d_pct"])        # 20 sessions back crosses the gap
        x = pp.extended_features(f, None, released=False, q1_reaction_date=date(2026, 8, 13))
        self.assertNotIn("q1_reaction_return_pct", x)       # TIGERLOGS: reaction day missing
        import tempfile
        with tempfile.TemporaryDirectory() as td:   # stored FAIRCHEMOR +31% run-up is withdrawn
            store = p.EventStore(Path(td) / "events")
            e = store.ensure_event(security={"symbol": "FAIRCHEMOR"}, period_end=date(2026, 9, 30))
            store.merge_field(e, "pre_result_20d_pct", 31.37, source="NSE_PRICE")
            self.assertTrue(p.drop_stale_pre_result(store, e, p.price_metrics(f, None)))
            self.assertIsNone(store.value(e, "pre_result_20d_pct"))

    def test_concall_notice_date_from_file_name(self):
        import pead_plus as pp
        from datetime import datetime
        filings = [  # NSE lists newest first
            {"filedAt": datetime(2026, 10, 1, 17, 23), "url": "https://x/CRAMC_01102026172256_Investor_Meet_October_12_2026.pdf",
             "text": "Analysts/Institutional Investor Meet/Con. Call Updates Intimation under Regulation 30"},
            {"filedAt": datetime(2026, 9, 22, 18, 16), "url": "https://x/CRAMC_22092026181511_InvestorMeet_28092026.pdf",
             "text": "Analysts/Institutional Investor Meet/Con. Call Updates the Management will meet investors"},
        ]
        c = pp.concall_status(filings, date(2026, 10, 9), date(2026, 10, 12))
        self.assertEqual((c["status"], c["callDate"]), ("SCHEDULED", "2026-10-12"))
        self.assertIn("October_12", c["noticeUrl"])

    def test_past_meeting_date_does_not_replace_future_one(self):
        import tempfile
        rows = [{"bm_symbol": "BSHSL", "bm_date": "15-Oct-2026", "bm_purpose": "Board Meeting Intimation",
                 "bm_desc": "Board Meeting to be held on 15-Oct-2026 to consider the Half Yearly Unaudited Financial results"},
                {"bm_symbol": "BSHSL", "bm_date": "10-Oct-2026", "bm_purpose": "Financial Results",
                 "bm_desc": "To consider and approve the financial results for the period ended September 30, 2026"}]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(p, "now_ist", lambda: p.datetime(2026, 10, 11, 9, 0)):
            store = p.EventStore(Path(td) / "events")
            for r in rows:
                c = p.normalize_nse_meeting(r)
                self.assertIsNotNone(c)
                e = p.apply_discovery(store, p.SymbolMaster(), c, None)
            self.assertEqual(store.value(store.load(e["eventId"]), "result_date"), "2026-10-15")

    def test_breadth_ignores_untraded(self):
        import pead_plus as pp
        items = [{"priceContext": {"lastClose": 10}, "plus": {"price": {"ema21": 20, "ema63": 20, "last_session": "2021-03-22"}}},
                 {"priceContext": {"lastClose": 30}, "plus": {"price": {"ema21": 20, "ema63": 20, "last_session": "2026-10-09"}}}]
        b = pp.market_internals(items, None, date(2026, 10, 11))["breadth"]
        self.assertEqual((b["n"], b["above21Pct"]), (1, 100.0))



class GrowthLensAndSectorChecklistTests(unittest.TestCase):
    """2.9.8: QoQ / YoY / YTD lens and the 5-question sector checklist."""

    def test_ytd_from_filing_and_year_ago_document(self):
        # TCS Q2 FY27: Apr-Sep revenue 1,45,463 Cr; last year's from last year's filing.
        C = {"revenue": "rev", "pat": "pat"}
        parsed = {"revenue_cr": 73188.0, "pat_cr": 13884.0, "ytd_revenue_cr": 145463.0, "ytd_pat_cr": 26662.0,
                  "_meta": {"concepts": C}}
        other = {"revenue_cr": 65799.0, "pat_cr": 12075.0, "ytd_revenue_cr": 129236.0, "ytd_pat_cr": 24313.0,
                 "_meta": {"concepts": C}}
        listing = [{"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "CONSOLIDATED", "cumulative": False,
                    "xbrlUrl": "ya", "filedAt": None}]
        p.fill_comparatives_from_listing(parsed, listing, date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: other)
        self.assertEqual(parsed["ytd_revenue_yoy_pct"], 12.56)
        self.assertEqual(parsed["ytd_pat_yoy_pct"], 9.66)

    def test_low_base_and_acceleration_notes(self):
        import pead_plus as pp
        L = pp.growth_lens(12.3, 70.2, 73.5, 21.8, 405.2, 399.0)   # POONAWALLA
        self.assertTrue(any("low base" in n for n in L["notes"]))
        L = pp.growth_lens(1.3, 11.2, 12.6, 4.0, 15.0, 9.7)        # TCS
        self.assertTrue(any("speeding up" in n for n in L["notes"]))
        self.assertIsNone(pp.growth_lens(None, None, None, None, None, None))
        L = pp.growth_lens(15.0, 38.3, 36.5, 8.0, 22.5, 34.8)      # PREMIERPOL: profit slowing, not "consistent"
        self.assertFalse(any("Consistent" in n for n in L["notes"]))

    def _it(self, sym, sector, rev, pat, prev, released=True, last=110, ema=100):
        return {"symbol": sym, "resultsReleased": released, "revenueYoY": rev, "patYoY": pat,
                "priceContext": {"lastClose": last},
                "plus": {"sectorKey": sector, "liquidity": {"pass": True}, "growth": {"prevRevenueYoY": prev},
                         "margins": {}, "price": {"ema63": ema, "last_session": "2026-10-09"}}}

    def test_sector_checklist_and_execution(self):
        import pead_plus as pp
        items = [self._it("A", "IT", 12, 15, 8), self._it("B", "IT", 10, 9, 7), self._it("C", "IT", 14, 20, 9),
                 self._it("D", "IT", 4, -2, 6), self._it("E", "IT", None, None, None, released=False)]
        sectors = {"IT": {"sector": "IT", "relativeToMarket": 5.0}}
        pp.sector_checklists(items, sectors, date(2026, 10, 11))
        cl = sectors["IT"]["checklist"]
        st = {c["key"]: c["status"] for c in cl["checks"]}
        self.assertEqual(st["orders"], "na")                  # never invented
        self.assertEqual(st["demand"], "ok")                  # median 11.0 vs 7.5 last quarter
        self.assertEqual(st["peers"], "ok")
        self.assertEqual(st["market"], "ok")
        self.assertEqual((cl["score"], cl["measured"]), (4, 4))
        self.assertEqual(items[3]["plus"]["execution"]["label"], "BEHIND")
        self.assertEqual(items[2]["plus"]["execution"]["label"], "AHEAD")

    def test_too_few_reports_is_not_scored(self):
        import pead_plus as pp
        items = [self._it("A", "FMCG", 12, 15, 8)]
        sectors = {"FMCG": {"sector": "FMCG", "relativeToMarket": -3.0}}
        pp.sector_checklists(items, sectors, date(2026, 10, 11))
        st = {c["key"]: c["status"] for c in sectors["FMCG"]["checklist"]["checks"]}
        self.assertEqual((st["demand"], st["earnings"], st["peers"]), ("na", "na", "na"))



class LiveQuoteTests(unittest.TestCase):
    """NSE's getSymbolData quote keeps the last price under orderBook; reading
    priceInfo.lastPrice gave None, so no live price was ever stored."""

    def test_last_price_from_real_quote(self):
        import json
        q = json.loads((Path(__file__).parent / "fixtures" / "nse" / "quote_PREMIERPOL_2026-10-13.json").read_text())
        self.assertEqual(p.quote_last_price(q), 109.12)
        self.assertEqual(p.quote_last_price({"priceInfo": {"lastPrice": 50.5}}), 50.5)
        self.assertIsNone(p.quote_last_price({"priceInfo": {}, "orderBook": {}}))


class OwnersZeroComparativeTests(unittest.TestCase):
    """SWANDEF Q2 FY27 (13 Oct 2026): last year's consolidated filing put the
    owners' PAT line at 0 (profit for period -19.86 Cr), so its parse used the
    total-profit concept and fill_comparatives_from_listing refused it as a
    "different concept". The card showed no PAT change at all instead of
    TURNAROUND (loss -> profit). This year's owners line equals total profit
    (no minority interest), so the two concepts are the same number."""

    def _parse(self, name, pe):
        blob = (Path(__file__).parent / "fixtures" / "nse" / name).read_bytes()
        return p.parse_xbrl_financials([blob], pe)

    def _listing(self):
        return [{"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "CONSOLIDATED", "cumulative": False,
                 "xbrlUrl": "ya"}]

    def test_turnaround_against_owners_zero_year_ago_filing(self):
        cur = self._parse("SWANDEF_CONS_2026-09-30.xml", date(2026, 9, 30))
        ya = self._parse("SWANDEF_CONS_2025-09-30.xml", date(2025, 9, 30))
        self.assertIn("OWNERS_PAT_ZERO_USED_TOTAL", ya["_meta"]["issues"])
        self.assertAlmostEqual(ya["pat_cr"], -19.86, places=2)
        self.assertIsNone(cur["prior_year_pat_cr"])
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertEqual(cur["prior_year_pat_cr"], -19.86)
        self.assertEqual(cur["pat_trend"], "TURNAROUND")
        self.assertIsNone(cur["pat_yoy_pct"])                  # no % across a loss
        self.assertAlmostEqual(cur["revenue_yoy_pct"], 73.99, places=2)

    def test_not_mixed_when_owners_differ_from_total(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitorlossattributabletoownersofparent"}
        cur = {"revenue_cr": 100.0, "pat_cr": 10.0, "revenue_qoq_pct": 1.0,
               "_meta": {"concepts": concepts, "patTotalCr": 12.0}}       # minority interest 2 Cr
        ya = {"revenue_cr": 90.0, "pat_cr": 5.0,
              "_meta": {"concepts": {**concepts, "pat": "profitlossforperiod"}, "issues": ["OWNERS_PAT_ZERO_USED_TOTAL"]}}
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertIsNone(cur.get("prior_year_pat_cr"))
        self.assertIsNone(cur.get("pat_yoy_pct"))
        # without the zero-owners fallback a different concept is never mixed either
        ya2 = {**ya, "_meta": {**ya["_meta"], "issues": []}}
        cur2 = {**cur, "_meta": {**cur["_meta"], "patTotalCr": 10.0}}
        p.fill_comparatives_from_listing(cur2, self._listing(), date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya2)
        self.assertIsNone(cur2.get("prior_year_pat_cr"))



class UnpricedResultAuditTests(unittest.TestCase):
    """TYPHOON (BSE XT group, no price history, no results table) turned the
    whole page red; only possible trade candidates should FAIL."""

    def test_unpriced_result_warns_not_fails(self):
        import pead_plus as pp
        items = [{"symbol": "TYPHOON", "resultsReleased": True, "resultDate": "2026-10-12", "reactionSession": "2026-10-13",
                  "fundamentalSnapshot": {}, "priceContext": {}, "plus": {"liquidity": {"pass": None}}}]
        audit = pp.self_audit(items, {"asOf": "2026-10-13"}, {}, date(2026, 10, 14))
        self.assertEqual([c["check"] for c in audit["checks"] if c["status"] == "FAIL"], [])
        st = {c["check"]: c["status"] for c in audit["checks"]}
        self.assertEqual(st["Illiquid or unpriced results not parsed (2+ days)"], "WARN")
        self.assertEqual(st["Reaction not measurable (no price history or illiquid)"], "WARN")

    def test_liquid_candidate_still_fails(self):
        import pead_plus as pp
        items = [{"symbol": "BIGCO", "resultsReleased": True, "resultDate": "2026-10-12", "reactionSession": "2026-10-13",
                  "fundamentalSnapshot": {}, "priceContext": {},
                  "plus": {"liquidity": {"pass": True}, "price": {"last_session": "2026-10-13"}}}]
        audit = pp.self_audit(items, {"asOf": "2026-10-13"}, {}, date(2026, 10, 14))
        fails = {c["check"] for c in audit["checks"] if c["status"] == "FAIL"}
        self.assertIn("Declared results have revenue/profit (filed 2+ days ago)", fails)
        self.assertIn("Reaction measured once the session has closed", fails)



class EarlyEntryStrictTests(unittest.TestCase):
    def test_close_at_result_day_high_is_near_not_triggered(self):
        import pead_plus as pp
        x = {"sessions_since_reaction": 0, "reaction_close": 1326.0, "ema10": 1250, "ema21": 1240, "ema63": 1200}
        kw = dict(released=True, reaction_traded=True, quality_ok=True, x=x, result_return=8.4, rvol=7.8,
                  result_low=1238.2, result_high=1326.0, box_high=None)
        import inspect
        params = inspect.signature(pp.trade_plan).parameters
        kw = {k: v for k, v in kw.items() if k in params}
        if "last_price" in params:
            kw["last_price"] = 1326.0
        plan = pp.trade_plan(**kw)
        self.assertEqual(plan["signal"], "NEAR_ENTRY")
        self.assertEqual(plan["entry"], 1326.0)
        kw["last_price"] = 1330.0
        self.assertEqual(pp.trade_plan(**kw)["signal"], "ENTRY_EARLY")



class PositionTrackerTests(unittest.TestCase):
    """2.9.12 My trades: HCLTECH bought 14 Oct at 1331.80, stop 1225.82."""
    POS = {"symbol": "HCLTECH", "entryDate": "2026-10-14", "entryPrice": 1331.80, "stop": 1225.82}

    def item(self, close, live=None, e21=1242.8, e63=1257.66):
        return {"symbol": "HCLTECH", "resultDayLow": 1238.2, "priceContext": {"lastClose": close},
                "plus": {"price": {"ema21": e21, "ema63": e63, "last_session": "2026-10-20"}, "live": {"price": live, "at": "x"} if live else {}}}

    def test_hold_then_trail_and_stops_never_drop(self):
        import pead_plus as pp
        v, st = pp.position_view(self.POS, self.item(1326.0, live=1351.9), None, date(2026, 10, 14))
        self.assertEqual(v["status"], "HOLD")
        self.assertEqual((v["oneR"], v["swingStop"]), (1437.78, 1225.82))
        self.assertAlmostEqual(v["rNow"], 0.19, places=2)
        v, st = pp.position_view(self.POS, self.item(1445.0, e21=1360, e63=1300), st, date(2026, 10, 20))
        self.assertEqual(v["status"], "TRAILING")
        self.assertEqual((v["swingStop"], v["positionStop"]), (1360.0, 1331.8))   # cost floor for the 63 EMA leg
        v, st = pp.position_view(self.POS, self.item(1420.0, e21=1340, e63=1290), st, date(2026, 10, 21))
        self.assertEqual(v["swingStop"], 1360.0)          # never moves down
        self.assertEqual(v["status"], "TRAILING")

    def test_close_below_stop_says_exit(self):
        import pead_plus as pp
        v, _ = pp.position_view(self.POS, self.item(1220.0), None, date(2026, 10, 16))
        self.assertEqual(v["status"], "EXIT")

    def test_missing_item_is_waiting_not_crash(self):
        import pead_plus as pp
        v, _ = pp.position_view(self.POS, None, None, date(2026, 10, 14))
        self.assertEqual(v["status"], "NO_DATA")


class LiveQuoteAfterCloseTests(unittest.TestCase):
    """14 Oct 2026 evening: My trades showed HCLTECH "Live 1,327.80 - close
    1,327.00" at 20:21 IST. The live quote was read at 15:19 IST; once the
    day's close is stored (after 15:45 IST) the close is the newer price and
    the 15:19 snapshot must not drive last price, R or P&L."""

    def test_quote_superseded_by_stored_close(self):
        import pead_plus as pp
        from datetime import datetime, timedelta, timezone
        ist = timezone(timedelta(hours=5, minutes=30))
        at = "2026-10-14T09:49:13Z"                                   # 15:19 IST
        evening = datetime(2026, 10, 14, 20, 21, tzinfo=ist)
        self.assertFalse(pp.live_quote_current(at, "2026-10-14", evening))
        # 3 pm alert run: session still open -> the quote is the latest price
        self.assertTrue(pp.live_quote_current(at, "2026-10-13", datetime(2026, 10, 14, 15, 20, tzinfo=ist)))
        # even if a partial row for today is already stored, before 15:45 the quote stands
        self.assertTrue(pp.live_quote_current(at, "2026-10-14", datetime(2026, 10, 14, 15, 30, tzinfo=ist)))
        # evening, but today's close not yet stored -> keep the quote
        self.assertTrue(pp.live_quote_current(at, "2026-10-13", evening))
        # yesterday's quote is never current
        self.assertFalse(pp.live_quote_current(at, "2026-10-13", datetime(2026, 10, 15, 10, 0, tzinfo=ist)))
        self.assertFalse(pp.live_quote_current(None, "2026-10-13", evening))

    def test_position_uses_close_when_no_current_quote(self):
        import pead_plus as pp
        pos = {"symbol": "HCLTECH", "entryDate": "2026-10-14", "entryPrice": 1331.80, "stop": 1225.82}
        item = {"symbol": "HCLTECH", "resultDayLow": 1238.2, "priceContext": {"lastClose": 1327.0},
                "plus": {"price": {"ema21": 1250.46, "ema63": 1259.83, "last_session": "2026-10-14"}, "live": None}}
        v, _ = pp.position_view(pos, item, None, date(2026, 10, 14))
        self.assertEqual((v["last"], v["live"]), (1327.0, False))


if __name__ == "__main__":
    unittest.main()


class HoldingsWatchTests(unittest.TestCase):
    """2.9.15: news / filings / deals / pressure / global peers for open trades."""

    def test_announcement_classes(self):
        import pead_plus as pp
        cases = {
            "Bagging/Receiving of orders/contracts: order worth Rs 450 Cr from PGCIL": ("ORDER", "pos"),
            "Disclosure under SEBI (SAST) Regulations, 2011": ("INSIDER", "info"),
            "GST demand order received from the department": ("LEGAL", "neg"),
            "Credit Rating: ICRA reaffirmed AA+": ("RATING", "info"),
            "Resignation of Chief Financial Officer": ("MGMT", "neg"),
            "Creation of pledge on promoter shares": ("PLEDGE", "neg"),
            "Release of pledge": ("PLEDGE", "pos"),
            "Dividend: Board of Directors at its meeting declared an interim dividend": ("CORP", "info"),
            "Appointment of Independent Director": ("MGMT", "info"),
            "Research and development update": ("OTHER", "info"),
        }
        for text, want in cases.items():
            self.assertEqual(pp.classify_announcement(text), want, text)

    def test_filings_newest_first_routine_dropped(self):
        import pead_plus as pp
        from datetime import datetime
        rows = [
            {"filedAt": datetime(2026, 10, 10, 10), "text": "Trading Window: closure of trading window", "url": "https://x/1.pdf"},
            {"filedAt": datetime(2026, 10, 12, 18), "text": "Bagging/Receiving of orders: new deal win", "url": "https://x/2.pdf"},
            {"filedAt": datetime(2026, 10, 11, 9), "text": "Updates: Credit rating", "url": None},
            {"filedAt": None, "text": "no date", "url": None},
        ]
        out = pp.holding_filings(rows)
        self.assertEqual([f["kind"] for f in out], ["ORDER", "RATING"])

    def test_deal_rows_filter_and_parse(self):
        import pead_plus as pp
        bulk = [{"BD_DT_DATE": "14-OCT-2026", "BD_SYMBOL": "HCLTECH", "BD_CLIENT_NAME": "SOME FUND", "BD_BUY_SELL": "SELL",
                 "BD_QTY_TRD": 1200000, "BD_TP_WATP": 1330.5},
                {"BD_DT_DATE": "14-OCT-2026", "BD_SYMBOL": "OTHER", "BD_BUY_SELL": "BUY", "BD_QTY_TRD": 1}]
        short = [{"SS_DATE": "13-OCT-2026", "SS_SYMBOL": "HCLTECH", "SS_QTY": 600}]
        b = pp.parse_deal_rows(bulk, "BULK", {"HCLTECH"})
        s = pp.parse_deal_rows(short, "SHORT", {"HCLTECH"})
        self.assertEqual(b, [{"symbol": "HCLTECH", "date": "2026-10-14", "kind": "BULK", "client": "SOME FUND",
                              "side": "SELL", "qty": 1200000.0, "price": 1330.5}])
        self.assertEqual(s, [{"symbol": "HCLTECH", "date": "2026-10-13", "kind": "SHORT", "qty": 600.0}])

    def _frame(self, closes, vols):
        import pandas as pd
        n = len(closes)
        return pd.DataFrame({"Date": pd.bdate_range("2026-08-03", periods=n), "Open": closes,
                             "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
                             "Close": closes, "Volume": vols})

    def test_selling_pressure_counts_down_days_on_rising_volume(self):
        import pead_plus as pp
        closes, vols, c = [], [], 100.0
        for i in range(40):
            if i >= 28 and i % 2 == 0:
                c *= 0.98; v = 2500          # down on heavy volume: distribution
            elif i >= 28:
                c *= 1.003; v = 800          # weak, light bounces
            else:
                c *= 1.001; v = 1000
            closes.append(round(c, 2)); vols.append(v)
        pr = pp.selling_pressure(self._frame(closes, vols))
        self.assertGreaterEqual(pr["distributionDays"], 6)
        self.assertIn(pr["label"], ("SELLING", "HEAVY_SELLING"))
        flat = pp.selling_pressure(self._frame([100 + (i % 3) * 0.01 for i in range(40)], [1000] * 40))
        self.assertEqual(flat["label"], "NEUTRAL")
        self.assertIsNone(pp.selling_pressure(self._frame([100.0] * 10, [1000] * 10)))

    def test_news_shapes_and_bad_links(self):
        import pead_plus as pp
        items = [
            {"title": "Old shape", "publisher": "Reuters", "link": "https://a.example/1", "providerPublishTime": 1760000000},
            {"id": "x", "content": {"title": "New shape", "pubDate": "2026-10-14T05:00:00Z",
                                    "provider": {"displayName": "Mint"}, "canonicalUrl": {"url": "https://b.example/2"}}},
            {"title": "Script link", "link": "javascript:alert(1)"},
            {"title": "new shape", "link": "https://dup.example"},
        ]
        out = pp.normalize_news(items)
        self.assertEqual([n["title"] for n in out], ["New shape", "Old shape"])
        self.assertEqual(out[0]["publisher"], "Mint")
        self.assertEqual(pp.normalize_news(out), out)      # merging already-normalised lists keeps them

    def test_balanced_news_one_source_cannot_crowd_out(self):
        import pead_plus as pp
        busy = [{"title": f"Accenture story {i}", "url": f"https://a/{i}", "at": f"2026-10-14T1{i}:00:00Z"} for i in range(6)]
        quiet = [{"title": "H-1B fee update", "url": "https://h/1", "at": "2026-10-10T09:00:00Z"}]
        out = pp.balanced_news([busy, quiet], 4)
        self.assertEqual(len(out), 4)
        self.assertIn("H-1B fee update", [n["title"] for n in out])
        self.assertEqual(out, sorted(out, key=lambda n: n["at"], reverse=True))

    def test_series_summary(self):
        import pead_plus as pp
        rows = [(f"2026-10-{d:02d}", v) for d, v in zip(range(5, 12), (100, 101, 102, 103, 104, 105, 99.75))]
        s = pp.summarize_series(rows)
        self.assertEqual((s["date"], s["last"], s["chg1dPct"], s["chg5dPct"]), ("2026-10-11", 99.75, -5.0, -1.24))
        self.assertIsNone(pp.summarize_series([]))

    def test_watch_view_flags(self):
        import pead_plus as pp
        from datetime import date
        v = pp.holding_watch_view(
            "HCLTECH", {"filings": [{"kind": "LEGAL", "tone": "neg", "text": "Tax demand", "at": "2026-10-14T10:00:00"}]},
            [{"symbol": "HCLTECH", "date": "2026-10-14", "kind": "BULK", "side": "SELL", "qty": 1},
             {"symbol": "TARIL", "date": "2026-10-14", "kind": "BULK", "side": "SELL", "qty": 1}],
            {"label": "SELLING", "distributionDays": 5, "sessions": 25, "lastDownOnVolume": False},
            41.2, {"ACN": {"chg1dPct": -2.0}, "CTSH": {"chg1dPct": -1.5}}, "Information Technology", date(2026, 10, 15))
        self.assertEqual(len(v["deals"]), 1)
        self.assertEqual(v["peerAvg1dPct"], -1.75)
        self.assertEqual(len(v["flags"]), 4)
        self.assertEqual([p_["ticker"] for p_ in v["peers"]], ["ACN", "CTSH"])
        quiet = pp.holding_watch_view("HCLTECH", None, [], None, None, {}, None, date(2026, 10, 15))
        self.assertEqual(quiet["flags"], [])
        self.assertIsNone(quiet["deliveryPct"])

    def test_position_views_attach_watch(self):
        import json, tempfile
        from datetime import date
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "positions.json").write_text(json.dumps({"positions": [
                {"symbol": "HCLTECH", "entryDate": "2026-10-14", "entryPrice": 1331.8, "stop": 1225.82, "status": "OPEN"}]}))
            (d / "watch.json").write_text(json.dumps({"symbols": {"HCLTECH": {"filings": [], "filingsAt": "2026-10-15T04:00:00Z"}},
                                                      "deals": [], "global": {"rows": {"ACN": {"chg1dPct": 0.5}}}}))
            item = {"symbol": "HCLTECH", "resultDayLow": 1238.2, "priceContext": {"lastClose": 1327.0},
                    "plus": {"sector": {"sector": "Information Technology", "tailwind": "POSITIVE"},
                             "sectorKey": "Information Technology", "deliveryPct": 55.0,
                             "price": {"last_session": "2026-10-14", "pressure": {"label": "NEUTRAL"}}}}
            with mock.patch.object(p, "POSITIONS_PATH", d / "positions.json"), \
                 mock.patch.object(p, "POSITIONS_STATE_PATH", d / "state.json"), \
                 mock.patch.object(p, "HOLDINGS_WATCH_PATH", d / "watch.json"):
                views = p.position_views([item], date(2026, 10, 15))
        w = views[0]["watch"]
        self.assertEqual(w["pressure"]["label"], "NEUTRAL")
        self.assertEqual(w["deliveryPct"], 55.0)
        self.assertEqual(w["peers"][0]["name"], "Accenture")


class QuoteMarketCapTests(unittest.TestCase):
    """15 Oct 2026: NSE's quote returned totalMarketCap = ffmc (free float) for
    PREMIERPOL (Rs 294.6 Cr; 10.47 Cr shares x Rs 109.2 = Rs 1,143.8 Cr),
    HCLTECH, DMART, RBLBANK, GSFC and GRANDOAK. PREMIERPOL fell below the
    Rs 1,000 Cr minimum and left Radar; HCLTECH showed Rs 1.41 lakh Cr."""

    FIX = Path(__file__).parent / "fixtures" / "nse" / "quote_PREMIERPOL_ffmc_as_mcap_2026-10-15.json"

    def test_free_float_reported_as_total_is_replaced(self):
        import json
        q = p.parse_nse_quote(json.loads(self.FIX.read_text()))
        self.assertAlmostEqual(q["market_cap_cr"], 1143.79, places=1)
        self.assertAlmostEqual(q["ffmc_cr"], 294.64, places=1)

    def test_consistent_total_is_kept(self):
        q = p.parse_nse_quote({"tradeInfo": {"totalMarketCap": 1.11e10, "ffmc": 2.8e9, "issuedSize": 104742475,
                                             "lastPrice": 106.0}})
        self.assertEqual(q["market_cap_cr"], 1110.0)

    def test_missing_issued_size_keeps_reported_value(self):
        q = p.parse_nse_quote({"tradeInfo": {"totalMarketCap": 2.946e9, "ffmc": 2.946e9}})
        self.assertEqual(q["market_cap_cr"], 294.6)

    def test_stored_value_repaired_from_saved_quote(self):
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as td:
            store = p.EventStore(Path(td) / "events")
            raw = Path(td) / "quote.json"
            shutil.copy(self.FIX, raw)
            e = store.ensure_event(security={"symbol": "PREMIERPOL", "nseSymbol": "PREMIERPOL"}, period_end=date(2026, 9, 30))
            store.merge_field(e, "market_cap_cr", 294.64, source="NSE_QUOTE", raw_ref=str(raw))
            store.save(e)
            self.assertEqual(p.repair_quote_market_caps(store), 1)
            e = store.load(e["eventId"])
            self.assertAlmostEqual(store.value(e, "market_cap_cr"), 1143.79, places=1)
            self.assertEqual(p.repair_quote_market_caps(store), 0)      # idempotent
class TotalProfitOnlyFilingTests(unittest.TestCase):
    """2.9.17 (15 Oct 2026): TATATECH and MRPL's Q2 FY27 consolidated XBRL has no
    owners' PAT line, only ProfitLossForPeriod. Last year's filings carry both
    lines, so their parse picked the owners' concept and fill_comparatives_from_listing
    refused them as "another concept": revenue YoY showed, PAT YoY stayed blank.
    The year-ago filing's own ProfitLossForPeriod is the SAME concept and is now used."""

    def _parse(self, name, pe):
        blob = (Path(__file__).parent / "fixtures" / "nse" / name).read_bytes()
        return p.parse_xbrl_financials([blob], pe)

    def _listing(self):
        return [{"periodEnd": pe, "fromDate": fd, "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": pe}
                for pe, fd in (("2026-06-30", "2026-04-01"), ("2025-09-30", "2025-07-01"), ("2025-06-30", "2025-04-01"))]

    def _fill(self, cur, docs):
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED",
                                         lambda url, pe: docs[url])

    def test_tatatech_total_profit_compared_with_total_profit(self):
        cur = self._parse("TATATECH_CONS_2026-09-30.xml", date(2026, 9, 30))
        self.assertEqual(cur["_meta"]["concepts"]["pat"], "profitlossforperiod")
        docs = {"2025-09-30": self._parse("TATATECH_CONS_2025-09-30.xml", date(2025, 9, 30)),
                "2026-06-30": self._parse("TATATECH_CONS_2026-06-30.xml", date(2026, 6, 30)),
                "2025-06-30": self._parse("TATATECH_CONS_2025-06-30.xml", date(2025, 6, 30))}
        self.assertEqual(docs["2025-09-30"]["_meta"]["concepts"]["pat"], "profitorlossattributabletoownersofparent")
        self._fill(cur, docs)
        self.assertEqual(cur["prior_year_pat_cr"], 165.5)
        self.assertEqual(cur["pat_trend"], "PROFIT_GROWTH")
        self.assertAlmostEqual(cur["pat_yoy_pct"], 3.59, places=2)
        self.assertEqual(cur["pat_qoq_trend"], "PROFIT_DECLINE")
        self.assertIsNotNone(cur.get("prev_q_pat_yoy_pct"))

    def test_mrpl_uses_year_ago_total_not_owners_line(self):
        cur = self._parse("MRPL_CONS_2026-09-30.xml", date(2026, 9, 30))
        ya = self._parse("MRPL_CONS_2025-09-30.xml", date(2025, 9, 30))
        self.assertEqual(ya["pat_cr"], 623.67)                 # owners' line
        self.assertEqual(ya["_meta"]["patTotalCr"], 627.36)    # profit for the period
        self._fill(cur, {"2025-09-30": ya})
        self.assertEqual(cur["prior_year_pat_cr"], 627.36)
        self.assertAlmostEqual(cur["pat_yoy_pct"], round((1900.76 / 627.36 - 1) * 100, 2), places=1)

    def test_owners_line_current_is_still_never_mixed(self):
        # TRF case (owner's decision pending): current owners' line vs a
        # year-ago filing with total profit only stays blank.
        concepts = {"revenue": "revenuefromoperations", "pat": "profitorlossattributabletoownersofparent"}
        cur = {"revenue_cr": 100.0, "pat_cr": 50.26, "revenue_qoq_pct": 1.0,
               "_meta": {"concepts": concepts, "patTotalCr": 49.85, "patTotalConcept": "profitlossforperiod"}}
        ya = {"revenue_cr": 90.0, "pat_cr": -6.81,
              "_meta": {"concepts": {**concepts, "pat": "profitlossforperiod"}, "patTotalCr": -6.81,
                        "patTotalConcept": "profitlossforperiod"}}
        p.fill_comparatives_from_listing(cur, self._listing()[1:2], date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertIsNone(cur.get("prior_year_pat_cr"))

    def test_different_total_concepts_not_mixed(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitlossforperiod"}
        cur = {"revenue_cr": 100.0, "pat_cr": 10.0, "revenue_qoq_pct": 1.0,
               "_meta": {"concepts": concepts, "patTotalCr": 10.0, "patTotalConcept": "profitlossforperiod"}}
        ya = {"revenue_cr": 90.0, "pat_cr": 8.0,
              "_meta": {"concepts": {**concepts, "pat": "profitorlossattributabletoownersofparent"},
                        "patTotalCr": 9.0, "patTotalConcept": "profitloss"}}
        p.fill_comparatives_from_listing(cur, self._listing()[1:2], date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertIsNone(cur.get("prior_year_pat_cr"))


class OwnersEqualTotalVsTotalOnlyTests(unittest.TestCase):
    """2.9.21 (16 Oct 2026): DLF's Q2 FY27 consolidated XBRL carries an owners'
    PAT line (1,301.35 Cr) EQUAL to its profit for the period (no minority
    interest); last year's filing has only ProfitLossForPeriod (1,180.09 Cr).
    same_pat_concept refused them, so the card showed revenue YoY but no PAT
    change. When the current owners' line equals its own total, the total
    element is the same number, so it is compared with the other filing's total
    of the SAME element. TRF (owners != total) stays blank: owner's decision."""

    def _parse(self, name, pe):
        blob = (Path(__file__).parent / "fixtures" / "nse" / name).read_bytes()
        return p.parse_xbrl_financials([blob], pe)

    def test_dlf_pat_yoy_from_year_ago_total(self):
        cur = self._parse("DLF_CONS_2026-09-30.xml", date(2026, 9, 30))
        ya = self._parse("DLF_CONS_2025-09-30.xml", date(2025, 9, 30))
        self.assertEqual(cur["_meta"]["concepts"]["pat"], "profitorlossattributabletoownersofparent")
        self.assertEqual(ya["_meta"]["concepts"]["pat"], "profitlossforperiod")
        listing = [{"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "CONSOLIDATED",
                    "cumulative": False, "xbrlUrl": "ya"}]
        p.fill_comparatives_from_listing(cur, listing, date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertEqual(cur["prior_year_pat_cr"], 1180.09)
        self.assertEqual(cur["pat_trend"], "PROFIT_GROWTH")
        self.assertAlmostEqual(cur["pat_yoy_pct"], round((1301.35 / 1180.09 - 1) * 100, 2), places=1)
        self.assertEqual(cur["prior_year_revenue_cr"], 1643.04)

    def test_other_direction_owners_equal_total_in_year_ago(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitlossforperiod"}
        cur = {"revenue_cr": 100.0, "pat_cr": 12.0, "revenue_qoq_pct": 1.0,
               "_meta": {"concepts": concepts, "patTotalCr": 12.0, "patTotalConcept": "profitlossforperiod"}}
        ya = {"revenue_cr": 90.0, "pat_cr": 10.0,
              "_meta": {"concepts": {**concepts, "pat": "profitorlossattributabletoownersofparent"},
                        "patTotalCr": 10.0, "patTotalConcept": "profitlossforperiod"}}
        listing = [{"periodEnd": "2025-09-30", "fromDate": "2025-07-01", "basis": "CONSOLIDATED",
                    "cumulative": False, "xbrlUrl": "ya"}]
        p.fill_comparatives_from_listing(cur, listing, date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: ya)
        self.assertEqual(cur["prior_year_pat_cr"], 10.0)

    def test_owners_different_from_total_still_blank(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitorlossattributabletoownersofparent"}
        cur = {"revenue_cr": 100.0, "pat_cr": 50.26, "revenue_qoq_pct": 1.0,
               "_meta": {"concepts": concepts, "patTotalCr": 49.85, "patTotalConcept": "profitlossforperiod"}}
        ya = {"revenue_cr": 90.0, "pat_cr": -6.81,
              "_meta": {"concepts": {**concepts, "pat": "profitlossforperiod"}, "patTotalCr": -6.81,
                        "patTotalConcept": "profitlossforperiod"}}
        self.assertFalse(p.same_pat_concept(cur, ya))
        self.assertIsNone(p.comparable_pat(cur, ya))

    def test_missing_pat_change_triggers_comparative_retry(self):
        self.assertTrue(p.comparatives_missing({"revenue_yoy_pct": -23.28, "pat_cr": 1301.35}))
        self.assertTrue(p.comparatives_missing({}))
        self.assertFalse(p.comparatives_missing({"revenue_yoy_pct": 5.0, "pat_yoy_pct": 3.0}))
        self.assertFalse(p.comparatives_missing({"revenue_yoy_pct": 5.0, "pat_yoy_pct": None, "pat_trend": "TURNAROUND"}))

    def test_owners_equal_total_but_other_total_element_differs(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitorlossattributabletoownersofparent"}
        cur = {"revenue_cr": 100.0, "pat_cr": 10.0,
               "_meta": {"concepts": concepts, "patTotalCr": 10.0, "patTotalConcept": "profitlossforperiod"}}
        ya = {"revenue_cr": 90.0, "pat_cr": 8.0,
              "_meta": {"concepts": {**concepts, "pat": "profitloss"}, "patTotalCr": 8.0, "patTotalConcept": "profitloss"}}
        self.assertIsNone(p.comparable_pat(cur, ya))


class BreakoutStrictTests(unittest.TestCase):
    """2.9.17: VIJIFIN 15 Oct (locked upper circuit, open = high = low = close
    16.34, RVOL 0.4) was ENTRY_BREAKOUT "Closed above the post-result box high"
    with the close EQUAL to the box high. Like the early entry (2.9.11), a
    breakout needs a close ABOVE the box high; at the high it is NEAR_ENTRY."""

    def _kw(self, last):
        x = {"sessions_since_reaction": 0, "reaction_close": 16.34, "ema10": 15.0, "ema21": 15.2, "ema63": 15.0}
        return dict(released=True, reaction_traded=True, quality_ok=True, x=x, last_price=last, result_return=4.95,
                    rvol=0.4, result_low=16.34, result_high=16.34, box_high=16.34)

    def test_close_at_box_high_is_near_entry(self):
        import pead_plus as pp
        plan = pp.trade_plan(**self._kw(16.34))
        self.assertEqual(plan["signal"], "NEAR_ENTRY")
        self.assertEqual(plan["entry"], 16.34)

    def test_close_above_box_high_triggers(self):
        import pead_plus as pp
        self.assertEqual(pp.trade_plan(**self._kw(16.5))["signal"], "ENTRY_BREAKOUT")


class SameDayTradeLogTests(unittest.TestCase):
    """A trade-log entry recorded today by a run whose verdict a later run on
    the same day no longer reaches (VIJIFIN's at-the-high 'breakout') is
    withdrawn, so the entry-timing scorecard does not count a trade that
    never triggered. Older entries are never touched."""

    def test_same_day_untriggered_entry_withdrawn(self):
        import pead_plus as pp
        log = {"full": {"date": "2026-10-15", "entry": 16.34, "sl": 16.18, "signal": "ENTRY_BREAKOUT",
                        "minSeen": 16.34, "last": 16.34, "returnPct": 0.0}}
        plan = {"signal": "NEAR_ENTRY", "stage": "FULL", "entry": 16.34, "sl": 16.18}
        out = pp.update_trade_log(log, plan, 16.34, date(2026, 10, 15))
        self.assertNotIn("full", out)

    def test_earlier_entry_kept(self):
        import pead_plus as pp
        log = {"full": {"date": "2026-10-14", "entry": 16.34, "sl": 16.18, "signal": "ENTRY_BREAKOUT",
                        "minSeen": 16.34, "last": 16.34, "returnPct": 0.0}}
        plan = {"signal": "NEAR_ENTRY", "stage": "FULL", "entry": 16.34, "sl": 16.18}
        out = pp.update_trade_log(log, plan, 16.0, date(2026, 10, 15))
        self.assertIn("full", out)
        self.assertEqual(out["full"]["last"], 16.0)

    def test_same_day_triggered_entry_kept(self):
        import pead_plus as pp
        log = {"full": {"date": "2026-10-15", "entry": 16.34, "sl": 16.18, "signal": "ENTRY_BREAKOUT", "minSeen": 16.5}}
        plan = {"signal": "ENTRY_BREAKOUT", "stage": "FULL", "entry": 16.34, "sl": 16.18}
        self.assertIn("full", pp.update_trade_log(log, plan, 16.5, date(2026, 10, 15)))

    def test_same_day_starter_kept_when_call_ends_and_full_triggers(self):
        import pead_plus as pp
        log = {"starter": {"date": "2026-10-15", "entry": 100.0, "sl": 95.0, "signal": "ENTRY_EARLY", "minSeen": 101.0}}
        plan = {"signal": "ENTRY_EARLY", "stage": "FULL", "entry": 100.0, "sl": 95.0}
        out = pp.update_trade_log(log, plan, 101.0, date(2026, 10, 15))
        self.assertIn("starter", out)
        self.assertIn("full", out)


class HoldingsNotesTests(unittest.TestCase):
    """2.9.18: the evening check writes master/holdings_notes.json (summary +
    impact per open trade); the page shows it on My trades."""

    def _write(self, d, data):
        import json
        (d / "notes.json").write_text(json.dumps(data))

    def test_fresh_notes_attach_and_old_ones_hide(self):
        import json, tempfile
        from datetime import date
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "positions.json").write_text(json.dumps({"positions": [
                {"symbol": "SEJALLTD", "entryDate": "2026-10-16", "entryPrice": 879.95, "stop": 838.03, "status": "OPEN"},
                {"symbol": "GROWW", "entryDate": "2026-10-16", "entryPrice": 200.8, "stop": 188.75, "status": "OPEN"}]}))
            self._write(d, {"market": {"date": "2026-10-16", "impact": "negative", "summary": "US tariffs widened."},
                            "symbols": {"sejalltd": {"date": "2026-10-16", "impact": "POSITIVE", "summary": "Order win.",
                                                     "points": [{"text": "Rs 40 Cr order", "source": "NSE", "url": "https://x/a.pdf"},
                                                                {"text": "bad link", "url": "javascript:x"}]},
                                        "GROWW": {"date": "2026-10-09", "impact": "NEGATIVE", "summary": "Old news."}}})
            with mock.patch.object(p, "POSITIONS_PATH", d / "positions.json"), \
                 mock.patch.object(p, "POSITIONS_STATE_PATH", d / "state.json"), \
                 mock.patch.object(p, "HOLDINGS_WATCH_PATH", d / "missing.json"), \
                 mock.patch.object(p, "HOLDINGS_NOTES_PATH", d / "notes.json"):
                views = {v["symbol"]: v for v in p.position_views([], date(2026, 10, 16))}
                market = p.holdings_notes(date(2026, 10, 16))["market"]
        n = views["SEJALLTD"]["notes"]
        self.assertEqual((n["impact"], n["summary"]), ("POSITIVE", "Order win."))
        self.assertEqual(n["points"][0]["url"], "https://x/a.pdf")
        self.assertIsNone(n["points"][1]["url"])           # only http(s) links
        self.assertIsNone(views["GROWW"]["notes"])          # older than 4 days
        self.assertEqual(market["impact"], "NEGATIVE")

    def test_missing_or_broken_file_is_empty(self):
        import tempfile
        from datetime import date
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "notes.json").write_text("{not json")
            with mock.patch.object(p, "HOLDINGS_NOTES_PATH", d / "notes.json"):
                self.assertEqual(p.holdings_notes(date(2026, 10, 16)), {})


class EnrichmentSlotTests(unittest.TestCase):
    """16 Oct 2026: 123 declared results > 120 slots. The declared-first sort
    gave every slot to declared companies, so upcoming ones (BPCL, results
    23 Oct) were last priced 12 Oct and never-enriched ones (LICI, TEAMLEASE)
    were never reached."""

    def _ev(self, i, released, rd, last_session=None, sector=True, attempt=None):
        from datetime import date as _d
        ev = {"eventId": f"E{i:04d}", "period": {"end": "2026-09-30"},
              "security": {"nseSymbol": f"S{i}", **({"exchangeSector": "Information Technology"} if sector else {})},
              "fields": {"result_date": {"value": rd.isoformat(), "status": "OK"},
                         "results_released": {"value": released, "status": "OK"},
                         "revenue_cr": {"value": 10.0, "status": "OK"}, "pat_cr": {"value": 1.0, "status": "OK"}},
              "fetch": {"PRICE_HISTORY": {"lastAttempt": attempt or "2026-10-16T05:00:00Z"}}}
        if last_session:
            ev["plus"] = {"price": {"last_session": last_session.isoformat()}}
        return ev

    def test_upcoming_and_never_enriched_get_slots_when_declared_exceed_cap(self):
        from datetime import date as _d, datetime as _dt
        now = _dt(2026, 10, 16, 11, 0, tzinfo=p.IST)
        today = now.date()
        declared = [self._ev(i, True, _d(2026, 10, 9), last_session=_d(2026, 10, 12)) for i in range(123)]
        upcoming = [self._ev(500 + i, False, _d(2026, 10, 23), last_session=_d(2026, 10, 9)) for i in range(5)]
        never = [self._ev(900, False, _d(2026, 11, 5), sector=False)]
        cands = declared + upcoming + never
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            old = sorted(cands, key=p._enrichment_priority)[:120]
            # The bug: the old order is all declared.
            self.assertTrue(all(e in declared for e in old))
            sel, counts = p.select_for_enrichment(cands, 120, today)
        self.assertEqual(len(sel), 120)
        for e in upcoming + never:
            self.assertIn(e, sel)
        self.assertEqual(counts["upcomingStale"], 5)
        self.assertEqual(counts["neverEnriched"], 1)
        # Unused upcoming / never-enriched slots flow back to declared work.
        self.assertEqual(sum(1 for e in sel if e in declared), 114)

    def test_settled_declared_yields_to_work_and_fresh_results_come_first(self):
        from datetime import date as _d, datetime as _dt
        now = _dt(2026, 10, 16, 11, 0, tzinfo=p.IST)
        today = now.date()
        settled = self._ev(1, True, _d(2026, 10, 1), last_session=_d(2026, 10, 15))
        fresh = self._ev(2, True, _d(2026, 10, 15), last_session=_d(2026, 10, 14))
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.enrichment_group(settled, today), ("D", 0))
            self.assertEqual(p.enrichment_group(fresh, today), ("A", 0))
            # During market hours the last closed session is yesterday.
            self.assertEqual(p.last_completed_session(today), _d(2026, 10, 15))
            sel, _ = p.select_for_enrichment([settled, fresh], 1, today)
        self.assertEqual(sel, [fresh])

    def test_upcoming_rotation_is_least_recently_enriched_first(self):
        from datetime import date as _d, datetime as _dt
        now = _dt(2026, 10, 16, 11, 0, tzinfo=p.IST)
        a = self._ev(1, False, _d(2026, 10, 17), last_session=_d(2026, 10, 9), attempt="2026-10-16T04:00:00Z")
        b = self._ev(2, False, _d(2026, 10, 23), last_session=_d(2026, 10, 9), attempt="2026-10-12T04:00:00Z")
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]), \
                mock.patch.object(p, "ENRICH_SLOTS_DECLARED", 0), mock.patch.object(p, "ENRICH_SLOTS_UPCOMING", 1):
            sel, _ = p.select_for_enrichment([a, b], 1, now.date())
        self.assertEqual(sel, [b])


class FilingTagsAndPeerGroupTests(unittest.TestCase):
    """2.9.19: PREMIERPOL's NSE volume queries were tagged Order ("The
    Exchange, in order to ensure..."); SEJALLTD's corrigendum was a plain
    results tag; PREMIERPOL / SEJALLTD got power-grid peers and GROWW banks."""

    SPURT = ("Spurt in Volume: Significant increase in volume has been observed in Premier Polyfilm Limited. "
             "The Exchange, in order to ensure that investors have latest relevant information about the company "
             "and to inform the market place so that the interest of the investors is safeguarded, has sought "
             "clarification from the company")
    CORR = ("General Updates: Sejal Glass Limited has informed the Exchange about Corrigendum To Standalone "
            "Financial Results For Quarter and Half Year Ended September 30, 2026")

    def test_volume_query_is_not_an_order(self):
        import pead_plus as pp
        self.assertEqual(pp.classify_announcement(self.SPURT), ("QUERY", "info"))
        self.assertEqual(pp.classify_announcement("Movement in price: the Exchange sought clarification"), ("QUERY", "info"))
        # Real orders still are.
        self.assertEqual(pp.classify_announcement("Bagging/Receiving of orders/contracts: order worth Rs 450 Cr"), ("ORDER", "pos"))
        self.assertEqual(pp.classify_announcement("Receipt of purchase order from NTPC"), ("ORDER", "pos"))
        self.assertEqual(pp.classify_announcement("Company received orders in order to expand"), ("ORDER", "pos"))

    def test_corrigendum_is_flagged(self):
        import pead_plus as pp
        self.assertEqual(pp.classify_announcement(self.CORR), ("CORRECTION", "warn"))
        self.assertEqual(pp.classify_announcement("Revised Financial Results for the quarter"), ("CORRECTION", "warn"))
        self.assertEqual(pp.classify_announcement("Outcome of Board Meeting: financial results"), ("RESULT", "info"))

    def test_stored_filings_are_relabelled_in_the_view(self):
        import pead_plus as pp
        from datetime import date as _d
        watch = {"filings": [{"at": "2026-09-24T10:00:00", "kind": "ORDER", "tone": "pos", "text": self.SPURT},
                             {"at": "2026-10-15T18:55:00", "kind": "RESULT", "tone": "info", "text": self.CORR}]}
        v = pp.holding_watch_view("PREMIERPOL", watch, [], None, None, {}, "Capital Goods", _d(2026, 10, 16),
                                  industry="Plastic Products - Industrial")
        self.assertEqual([(f["kind"], f["tone"]) for f in v["filings"]], [("QUERY", "info"), ("CORRECTION", "warn")])
        self.assertIn("correction / corrigendum filed", v["flags"])
        self.assertEqual(v["peerGroup"], "Plastics & packaging")

    def test_peers_follow_the_industry(self):
        import pead_plus as pp
        names = lambda s, i: {t for t, _ in pp.global_peers_for(s, i)}
        power = {"GEV", "ETN", "HUBB"}
        self.assertFalse(power & names("Capital Goods", "Plastic Products - Industrial"))
        self.assertFalse(power & names("Capital Goods", "Glass - Industrial"))
        self.assertIn("SGO.PA", names("Capital Goods", "Glass - Industrial"))
        self.assertTrue(power <= names("Capital Goods", "Heavy Electrical Equipment"))
        self.assertTrue(power <= names("Capital Goods", "Cables - Electricals"))
        broking = names("Financial Services", "Stockbroking & Allied")
        self.assertIn("HOOD", broking)
        self.assertFalse({"HDB", "IBN"} & broking)
        # Banks keep the bank list; an unknown Capital Goods industry gets broad industrials, not power.
        self.assertIn("HDB", names("Financial Services", "Private Sector Bank"))
        self.assertFalse(power & names("Capital Goods", "Industrial Products"))
        self.assertEqual(names("Information Technology", None), names("Information Technology", "Computers - Software & Consulting"))
        self.assertEqual(pp.global_peers_for(None, None), ())
        self.assertIn("SEBI", pp.macro_queries_for("Financial Services", "Stockbroking & Allied"))


class RunupScorecardTests(unittest.TestCase):
    def test_buckets_reaction_and_drift(self):
        import pead_plus as pp
        rows = [
            {"runup20": 1.33, "reaction": 8.42, "sinceResult": 11.2, "sessions": 2, "liquid": True},   # HCLTECH: drift +2.56%
            {"runup20": -7.8, "reaction": -2.0, "sinceResult": None, "sessions": 0, "liquid": True},    # reaction only
            {"runup20": 27.9, "reaction": -5.0, "sinceResult": -9.75, "sessions": 1, "liquid": True},   # drift -5%
            {"runup20": 15.0, "reaction": 4.0, "sinceResult": 6.0, "sessions": 1, "liquid": False},     # illiquid: out
            {"runup20": None, "reaction": 4.0, "sinceResult": 6.0, "sessions": 1, "liquid": True},      # no run-up: out
            {"runup20": 10.0, "reaction": 3.0, "sinceResult": 3.0, "sessions": 3, "liquid": None},      # boundary -> warm
        ]
        r = {x["group"]: x for x in pp.runup_scorecard(rows)["rows"]}
        tight = r["0% to 10% (tight base)"]
        self.assertEqual((tight["n"], tight["nDrift"]), (1, 1))
        self.assertAlmostEqual(tight["avgDrift"], 2.56, places=2)
        cool = r["Below 0% (cool / pullback)"]
        self.assertEqual((cool["n"], cool["nDrift"], cool["avgDrift"], cool["winRate"]), (1, 0, None, None))
        self.assertAlmostEqual(r["Above 20% (extended)"]["avgDrift"], -5.0, places=2)
        warm = r["10% to 20% (warm)"]
        self.assertEqual((warm["n"], warm["avgDrift"], warm["winRate"]), (1, 0.0, 0.0))


class SectorFavourTests(unittest.TestCase):
    """2.9.20: "How to know when a sector is out of favour" (@SureshKBN)."""

    def _frame(self, closes):
        import pandas as pd
        n = len(closes)
        return pd.DataFrame({"Date": pd.bdate_range("2025-06-02", periods=n), "Open": closes, "High": closes,
                             "Low": closes, "Close": closes, "Volume": [1000] * n})

    def test_failed_breakout_and_52w_highs(self):
        import pead_plus as pp
        base = [100.0 + (i % 5) * 0.1 for i in range(180)]
        # breaks out 10 sessions ago to 120, then sinks to 110 (> 5% under the peak)
        failed = base[:170] + [120.0, 119.0, 116.0, 113.0, 112.0, 111.0, 110.5, 110.0, 110.0, 110.0]
        f = pp.favour_signals(pp._clean(self._frame(failed)))
        self.assertTrue(f["breakout_recent"])
        self.assertTrue(f["breakout_failed"])
        self.assertTrue(f["nh52_recent"])
        held = base[:170] + [120.0, 121.0, 122.0, 121.5, 122.5, 123.0, 122.0, 123.5, 124.0, 124.5]
        self.assertFalse(pp.favour_signals(pp._clean(self._frame(held)))["breakout_failed"])
        flat = pp.favour_signals(pp._clean(self._frame(base)))
        self.assertFalse(flat["breakout_recent"])
        self.assertIsNone(flat["breakout_failed"])
        short = pp.favour_signals(pp._clean(self._frame(base[:100])))
        self.assertIsNone(short["nh52_recent"])                     # < 160 sessions: unknown, not False

    def _item(self, sym, r21, r63, *, strong=None, react=None, ema_above=True, bo=None, bof=None, nhr=False, nhp=False):
        return {"symbol": sym, "resultsReleased": strong is not None,
                "priceContext": {"lastClose": 100.0, "resultDayPct": react},
                "plus": {"sectorKey": "S", "strength": strong, "liquidity": {"pass": True},
                         "price": {"last_session": "2026-10-16", "ret_21d_pct": r21, "ret_63d_pct": r63,
                                   "ema63": 90.0 if ema_above else 110.0, "breakout_recent": bo, "breakout_failed": bof,
                                   "nh52_recent": nhr, "nh52_prior": nhp}}}

    def test_out_of_favour_vs_deteriorating_vs_in_favour(self):
        import pead_plus as pp
        from datetime import date
        reg = {"ret20dPct": -1.7, "ret63dPct": -4.6}
        weak = [self._item(f"W{i}", -8, -15, strong="STRONG", react=-2, ema_above=False, bo=True, bof=True,
                           nhr=False, nhp=True) for i in range(6)]
        sectors = {"S": {"checklist": {"reported": 6, "medianRevenueYoY": 15.0, "medianProfitYoY": 25.0}}}
        pp.sector_favour(weak, sectors, reg, date(2026, 10, 16))
        f = sectors["S"]["favour"]
        self.assertEqual(f["status"], "OUT_OF_FAVOUR")
        self.assertEqual(f["fundamentals"], "ok")
        self.assertEqual({s["key"]: s["status"] for s in f["signals"]},
                         {"relative": "bad", "reaction": "bad", "breakouts": "bad", "participation": "bad", "valuation": "bad"})
        self.assertEqual(weak[0]["plus"]["favour"]["status"], "OUT_OF_FAVOUR")
        sectors = {"S": {"checklist": {"reported": 6, "medianRevenueYoY": -4.0, "medianProfitYoY": -12.0}}}
        pp.sector_favour(weak, sectors, reg, date(2026, 10, 16))
        self.assertEqual(sectors["S"]["favour"]["status"], "DETERIORATING")
        strong = [self._item(f"G{i}", 4, 6, strong="STRONG", react=5, bo=True, bof=False, nhr=True, nhp=True) for i in range(6)]
        sectors = {"S": {"checklist": {"reported": 6, "medianRevenueYoY": 15.0, "medianProfitYoY": 25.0}}}
        pp.sector_favour(strong, sectors, reg, date(2026, 10, 16))
        self.assertEqual(sectors["S"]["favour"]["status"], "IN_FAVOUR")

    def test_strong_sector_with_some_failed_breakouts_is_not_out_of_favour(self):
        # Capital Goods 16 Oct: beating the index but 11 of 16 breakouts failed -> NEUTRAL, not out of favour.
        import pead_plus as pp
        from datetime import date
        rows = [self._item(f"C{i}", 2, 6, bo=True, bof=(i < 4), ema_above=(i % 2 == 0)) for i in range(6)]
        sectors = {"S": {"checklist": {"reported": 6, "medianRevenueYoY": 30.0, "medianProfitYoY": 44.0}}}
        pp.sector_favour(rows, sectors, {"ret20dPct": -1.7, "ret63dPct": -4.6}, date(2026, 10, 16))
        self.assertEqual(sectors["S"]["favour"]["status"], "NEUTRAL")

    def test_too_little_data(self):
        import pead_plus as pp
        from datetime import date
        rows = [self._item("A", 1, 1)]
        sectors = {"S": {}}
        pp.sector_favour(rows, sectors, {}, date(2026, 10, 16))
        self.assertEqual(sectors["S"]["favour"]["status"], "NOT_ENOUGH")


class TriggeredEntryTests(unittest.TestCase):
    """16 Oct 2026: PREMIERPOL was PULLBACK_ZONE on 15 Oct (enter above 109.39);
    on 16 Oct it closed at 111.51 (owner bought at 109.49), but the card said
    WATCH "no entry pattern yet": the close was more than 3% above the 10 EMA,
    so the pullback branch never ran. HCLTECH's early entry of 13 Oct became
    RISK_TOO_WIDE "wait for a tighter base" once the price moved on."""

    def _plan(self, x, last, **kw):
        import pead_plus as pp
        args = dict(released=True, reaction_traded=True, quality_ok=True, x=x, last_price=last,
                    result_return=11.73, rvol=6.59, result_low=106.2, result_high=113.6, box_high=120.0,
                    concall={"status": "NONE_FOUND"})
        args.update(kw)
        return pp.trade_plan(**args)

    def test_breakout_day_from_pullback_zone_is_an_entry(self):
        x = {"ema10": 105.25, "ema21": 99.85, "ema63": 87.35, "sessions_since_reaction": 4, "reaction_close": 113.0,
             "prev_close": 106.13, "prev_ema10": 104.08, "prev_ema21": 98.68,
             "reclaimed_prev_high": True, "day_before_high": 109.39, "prev_session_high": 112.0}
        plan = self._plan(x, 111.51)
        self.assertEqual(plan["signal"], "ENTRY_PULLBACK")
        self.assertEqual((plan["entry"], plan["sl"]), (109.39, 105.14))
        # without the previous session in the zone it stays a watch
        x2 = dict(x, prev_close=118.0)
        self.assertEqual(self._plan(x2, 111.51)["signal"], "WATCH")

    def test_triggered_entry_is_held_not_reset(self):
        import pead_plus as pp
        from datetime import date
        log = {"full": {"date": "2026-10-13", "entry": 1326.0, "sl": 1225.82, "signal": "ENTRY_EARLY",
                        "minSeen": 1326.0, "maxSeen": 1384.8, "last": 1380.0}}
        plan = {"signal": "RISK_TOO_WIDE", "why": "Stop under the result-day low is 13.0% away", "entry": None, "sl": None,
                "stage": "FULL", "sizeFraction": 1.0}
        held = pp.hold_triggered_entry(plan, log, 1380.0, {"ema21": 1271.29, "ema63": 1259.83},
                                       {"status": "DONE"}, date(2026, 10, 16))
        self.assertEqual(held["signal"], "ENTRY_TRIGGERED")
        self.assertEqual((held["entry"], held["sl"], held["triggeredOn"], held["triggeredBy"]),
                         (1326.0, 1225.82, "2026-10-13", "ENTRY_EARLY"))
        self.assertIn("13 Oct", held["why"])
        self.assertNotIn("tighter base", held["why"])        # no contradicting "wait" text
        self.assertEqual(held["tslSwing"], 1225.82)          # +1R (1426.18) not reached yet
        # stopped trades and a fresh entry signal are left alone
        stopped = {"full": dict(log["full"], stopped=True, stoppedOn="2026-10-15")}
        self.assertEqual(pp.hold_triggered_entry(plan, stopped, 1380.0, {}, None, date(2026, 10, 16))["signal"], "RISK_TOO_WIDE")
        fresh = dict(plan, signal="ENTRY_PULLBACK", entry=1390.0, sl=1225.82)
        self.assertEqual(pp.hold_triggered_entry(fresh, log, 1395.0, {}, None, date(2026, 10, 16))["signal"], "ENTRY_PULLBACK")
        # close below the stop: not held
        self.assertEqual(pp.hold_triggered_entry(plan, log, 1220.0, {}, None, date(2026, 10, 16))["signal"], "RISK_TOO_WIDE")

    def test_held_trail_after_one_r_uses_best_close(self):
        import pead_plus as pp
        from datetime import date
        log = {"full": {"date": "2026-10-13", "entry": 1326.0, "sl": 1225.82, "signal": "ENTRY_EARLY",
                        "minSeen": 1326.0, "maxSeen": 1440.0, "last": 1410.0}}
        held = pp.hold_triggered_entry({"signal": "EXTENDED", "stage": "FULL"}, log, 1410.0,
                                       {"ema21": 1300.0, "ema63": 1270.0}, None, date(2026, 10, 20))
        self.assertEqual((held["tslSwing"], held["tslPosition"]), (1326.0, 1326.0))   # stop to cost after +1R

    def test_starter_held_and_trade_log_tracks_best_close(self):
        import pead_plus as pp
        from datetime import date
        log = pp.update_trade_log(None, {"signal": "ENTRY_EARLY", "stage": "STARTER", "entry": 876.0, "sl": 838.03},
                                  966.15, date(2026, 10, 15))
        log = pp.update_trade_log(log, {"signal": "NEAR_ENTRY", "stage": "STARTER"}, 870.35, date(2026, 10, 16))
        self.assertEqual(log["starter"]["maxSeen"], 966.15)
        held = pp.hold_triggered_entry({"signal": "NEAR_ENTRY", "stage": "STARTER", "entry": 876.0},
                                       log, 870.35, {}, {"status": "SCHEDULED"}, date(2026, 10, 16))
        self.assertEqual((held["signal"], held["stage"]), ("ENTRY_TRIGGERED", "STARTER"))
        self.assertIn("below the entry", held["why"])

    def test_box_still_forming_is_said(self):
        x = {"ema10": 194.17, "ema21": 192.48, "sessions_since_reaction": 1, "reaction_close": 196.0}
        plan = self._plan(x, 200.85, result_return=2.15, rvol=1.78, result_low=190.66, result_high=199.0, box_high=204.0)
        self.assertEqual(plan["signal"], "NEAR_ENTRY")
        self.assertIn("still forming", plan["why"])


class SectorIndexRsAndMarginTests(unittest.TestCase):
    """2.9.23: RS vs the stock's sector index (1W / 1M) and impossible margins."""

    def test_index_mapping(self):
        import pead_plus as pp
        self.assertEqual(pp.sector_index_for({"sectorIndex": "NIFTY IT"}, "Information Technology"), "NIFTY IT")
        self.assertEqual(pp.sector_index_for({"sectorIndex": "NIFTY 500", "industry": "Banks - Regional"}, "Financial Services"), "NIFTY BANK")
        self.assertEqual(pp.sector_index_for({}, "Fast Moving Consumer Goods"), "NIFTY FMCG")
        self.assertEqual(pp.sector_index_for(None, "Textiles"), "NIFTY 500")

    def _series(self, start, n, step):
        from datetime import date, timedelta
        d0 = date(2026, 9, 1)
        ds, cs, d = [], [], d0
        while len(ds) < n:
            if d.weekday() < 5:
                ds.append(d.isoformat()); cs.append(round(start * (1 + step) ** len(cs), 4))
            d += timedelta(days=1)
        return {"dates": ds, "closes": cs}

    def test_rs_same_dates(self):
        import pead_plus as pp
        ser = self._series(100.0, 40, 0.01)                 # index +1% a session
        d = ser["dates"]
        px = {"last_session": d[-1], "ret_5d_pct": 10.0, "ret_21d_pct": 30.0, "d5_from": d[-6], "d21_from": d[-22]}
        r = pp.rs_vs_sector_index(px, "NIFTY IT", ser)
        self.assertAlmostEqual(r["index1w"], 5.1, places=1)   # 1.01^5 - 1
        self.assertAlmostEqual(r["rs1w"], 10.0 - r["index1w"], places=2)
        self.assertAlmostEqual(r["index1m"], 23.24, places=1)
        # stock data far newer than the index series: no RS
        stale = dict(ser, dates=ser["dates"][:-10], closes=ser["closes"][:-10])
        self.assertIsNone(pp.rs_vs_sector_index(px, "NIFTY IT", stale))
        self.assertIsNone(pp.rs_vs_sector_index(px, "NIFTY IT", None))

    def test_apply_falls_back_to_nifty500(self):
        import pead_plus as pp
        ser = self._series(100.0, 40, 0.0)
        d = ser["dates"]
        it = {"plus": {"sectorKey": "Capital Goods", "identity": {"sectorIndex": None},
                       "price": {"last_session": d[-1], "ret_5d_pct": 2.0, "ret_21d_pct": 4.0, "d5_from": d[-6], "d21_from": d[-22]}}}
        sectors = {"Capital Goods": {}}
        pp.apply_sector_index_rs([it], sectors, {"NIFTY 500": ser})     # NIFTY INDIA MANUFACTURING not fetched
        self.assertEqual(it["plus"]["rsSector"]["index"], "NIFTY 500")
        self.assertEqual(it["plus"]["rsSector"]["rs1m"], 4.0)
        self.assertEqual(sectors["Capital Goods"]["index"]["name"], "NIFTY 500")

    def test_impossible_margin_is_nm(self):
        import pead_plus as pp
        self.assertEqual(pp.plausible_margin(8150.0)[0], None)          # AURUM exchange OPM
        self.assertIn("n/m", pp.plausible_margin(193.17)[1])           # DLF
        self.assertEqual(pp.plausible_margin(13.4), (13.4, None))
        self.assertEqual(pp.plausible_margin(None), (None, None))

    def test_audit_flags_published_margin_beyond_100(self):
        import pead_plus as pp
        from datetime import date
        items = [{"symbol": "AURUM", "resultsReleased": False, "plus": {"margins": {"opmTtm": 8150.0}}, "priceContext": {}}]
        a = pp.self_audit(items, {}, {}, date(2026, 10, 18))
        chk = [c for c in a["checks"] if c["check"].startswith("No implausible values")][0]
        self.assertIn("AURUM", chk.get("examples") or [])

    def test_chart_carries_volume(self):
        import pandas as pd
        import pead_plus as pp
        f = pd.DataFrame({"Date": pd.bdate_range("2026-08-03", periods=30), "Open": [10.0] * 30, "High": [10.5] * 30,
                          "Low": [9.5] * 30, "Close": [10.0] * 30, "Volume": [1000] * 29 + [5000]})
        ch = pp._chart(pp._clean(f), None)
        self.assertEqual(ch["volume"][-1], 5000)
        self.assertEqual(len(ch["volume"]), len(ch["close"]))


class SectorIndexOutageTests(unittest.TestCase):
    """2.9.24: a run-guard outage (SourceSkipped) on 19 Oct marked every sector
    index 'rejected for the day', so the evening close never loaded and RS vs
    sector stayed on 16 Oct. Only a name NSE rejects (no series at all) is
    skipped for the day; an outage is retried on the next run."""

    def _run(self, cache, history):
        from datetime import datetime
        saved = {}

        class FakeNSE:
            def __init__(self, ctx): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def index_history(self, name, start, end):
                return history(name)

        now = datetime(2026, 10, 19, 20, 30, tzinfo=p.IST)
        with mock.patch.object(p, "load_sector_indices", return_value=cache), \
             mock.patch.object(p, "sector_index_names", return_value=["NIFTY IT", "NIFTY FOO"]), \
             mock.patch.object(p, "NSEAdapter", FakeNSE), \
             mock.patch.object(p, "now_ist", return_value=now), \
             mock.patch.object(p, "json_dump_atomic", side_effect=lambda path, obj: saved.update(obj)):
            return p.refresh_sector_indices(None, None)

    def _rows(self, last_day):
        from datetime import date, timedelta
        d0 = date.fromisoformat(last_day)
        return [{"EOD_TIMESTAMP": (d0 - timedelta(days=i)).isoformat(), "EOD_CLOSE_INDEX_VAL": 100 + i} for i in range(20)], None

    def test_outage_does_not_mark_rejected(self):
        cache = {"series": {"NIFTY IT": {"dates": ["2026-10-16"], "closes": [1.0], "fetchedOn": "2026-10-19",
                                         "attemptedAt": "2026-10-19T08:00:00Z"}}, "failed": {}}

        def boom(name):
            raise p.SourceSkipped("NSE switched off for this run")
        out = self._run(cache, boom)
        self.assertNotIn("NIFTY IT", out["failed"])

    def test_old_outage_mark_on_known_series_is_retried(self):
        cache = {"series": {"NIFTY IT": {"dates": ["2026-10-16"], "closes": [1.0], "fetchedOn": "2026-10-19",
                                         "attemptedAt": "2026-10-19T08:00:00Z"}},
                 "failed": {"NIFTY IT": "2026-10-19", "NIFTY FOO": "2026-10-19"}}
        called = []

        def hist(name):
            called.append(name)
            return self._rows("2026-10-19")
        out = self._run(cache, hist)
        self.assertEqual(called, ["NIFTY IT"])            # FOO (never had a series) stays skipped today
        self.assertEqual(out["series"]["NIFTY IT"]["dates"][-1], "2026-10-19")
        self.assertNotIn("NIFTY IT", out["failed"])

    def test_outage_does_not_start_retry_clock(self):
        """2.9.29: on 21 Oct the 18:44 IST run hit the NSE run guard while
        fetching NIFTY FINANCIAL SERVICES; attemptedAt was stamped anyway, so
        the 19:39 run thought it had tried < 2 h ago and skipped it: GROWW's
        RS vs sector stayed on 19 Oct all evening. A skipped call is not an
        attempt."""
        before = "2026-10-19T08:00:00Z"
        cache = {"series": {"NIFTY IT": {"dates": ["2026-10-16"], "closes": [1.0], "fetchedOn": "2026-10-19",
                                         "attemptedAt": before}}, "failed": {}}

        def boom(name):
            raise p.SourceSkipped("NSE switched off for this run")
        out = self._run(cache, boom)
        self.assertEqual(out["series"]["NIFTY IT"].get("attemptedAt"), before)
        # and the next run (same evening) retries it
        called = []

        def hist(name):
            called.append(name)
            return self._rows("2026-10-19")
        out = self._run(out, hist)
        self.assertIn("NIFTY IT", called)
        self.assertEqual(out["series"]["NIFTY IT"]["dates"][-1], "2026-10-19")


class ExchangeHolidayTests(_StoreCase):
    """2.9.24: master/nse_holidays.json was empty, so 20 Oct 2026 (Dussehra,
    markets shut) was the reaction session of every result filed on 19 Oct;
    after 15:45 on the 20th the session would count as closed with no bar
    (reactions 'missing', every price 'behind'). Holidays now come from the
    exchange calendar; a session moved off a holiday is only relabelled
    (the first bar on or after the holiday was already the one measured)."""
    HOL = {date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10)}

    def rows(self):
        return [bse_row(500111, "2026-09-11T19:06:04.000", "Unaudited Financial Results for the quarter ended 30th June 2026")]

    def test_calendar_file_lists_dussehra(self):
        self.assertIn(date(2026, 10, 20), p.load_holidays())
        self.assertIn(date(2026, 11, 10), p.load_holidays())

    def test_reaction_session_skips_holiday(self):
        from datetime import datetime
        with mock.patch.object(p, "load_holidays", return_value=self.HOL):
            after = datetime(2026, 10, 19, 17, 39, tzinfo=p.IST)
            self.assertEqual(p.reaction_session(after, None)[0], date(2026, 10, 21))
            intraday = datetime(2026, 10, 19, 13, 47, tzinfo=p.IST)
            self.assertEqual(p.reaction_session(intraday, None), (date(2026, 10, 21), "INTRADAY"))
            with mock.patch.object(p, "now_ist", return_value=datetime(2026, 10, 20, 20, 0, tzinfo=p.IST)):
                self.assertEqual(p.last_completed_session(), date(2026, 10, 19))

    def test_holiday_relabel_keeps_measured_reaction(self):
        e = self.store.ensure_event(security={"symbol": "S500111", "bseCode": "500111"}, period_end=date(2026, 6, 30))
        for f, v in (("results_released", True), ("filing_timestamp", "2026-09-11T19:06:04+05:30"),
                     ("reaction_session", "2026-09-14"), ("result_day_return_pct", -3.85), ("result_day_low", 101.5)):
            self.store.merge_field(e, f, v, source="BSE_RESULT_ANNOUNCEMENT" if f != "reaction_session" else "DERIVED")
        e["state"] = "REACTION_READY"
        self.store.save(e)
        with mock.patch.object(p, "load_holidays", return_value=self.HOL):
            p.integrity_pass(self.store, today=date(2026, 10, 19), raw_root=self.raw)
        e = self.store.load(e["eventId"])
        self.assertEqual(self.store.value(e, "reaction_session"), "2026-09-15")
        self.assertEqual(self.store.value(e, "result_day_return_pct"), -3.85)
        self.assertEqual(self.store.value(e, "result_day_low"), 101.5)

    def test_no_live_quotes_on_a_holiday(self):
        from datetime import datetime
        with mock.patch.object(p, "load_holidays", return_value=self.HOL):
            with mock.patch.object(p, "now_ist", return_value=datetime(2026, 10, 20, 11, 0, tzinfo=p.IST)):
                self.assertFalse(p.market_open_now())
            with mock.patch.object(p, "now_ist", return_value=datetime(2026, 10, 21, 11, 0, tzinfo=p.IST)):
                self.assertTrue(p.market_open_now())


class RadarGradeTests(unittest.TestCase):
    """2.9.25: best-to-worst Radar for an owner who takes only a few trades."""

    def _it(self, **kw):
        it = {"symbol": "X", "marketCapCr": 26266.0, "priceContext": {"lastClose": 615.4, "resultDayPct": 2.89, "relativeVolume": 3.81},
              "plus": {"strength": "STRONG", "bucket": {"code": "CONFIRMATION"},
                       "plan": {"signal": "ENTRY_PULLBACK", "entry": 607.05, "sl": 584.6, "stage": "FULL", "redFlags": []},
                       "price": {"pressure": {"label": "ACCUMULATION"}}, "rsSector": {"rs1m": 4.58},
                       "favour": {"status": "OUT_OF_FAVOUR"}}}
        for k, v in kw.items():
            if k in it["plus"]:
                it["plus"][k] = v
            else:
                it[k] = v
        return it

    def test_iifl_like_setup_is_top(self):
        import pead_plus as pp
        g = pp.radar_grade(self._it())
        self.assertEqual(g["tier"], "A")
        self.assertIn("strong earnings", g["pros"])
        self.assertIn("sector out of favour", g["cons"])
        self.assertAlmostEqual(g["riskNowPct"], 5.0, places=1)       # from 615.40, not the 607 trigger

    def test_red_flags_and_small_caps_never_top(self):
        import pead_plus as pp
        flagged = self._it(plan={"signal": "PULLBACK_ZONE", "entry": 2989.9, "sl": 2806.65, "stage": "FULL",
                                 "redFlags": ["turnaround from a loss", "figures flagged for review"]})
        self.assertNotEqual(pp.radar_grade(flagged)["tier"], "A")
        small = self._it(marketCapCr=832.0)
        g = pp.radar_grade(small)
        self.assertIn(g["tier"], ("C", "D"))
        self.assertIn("below the market-cap minimum", g["cons"])

    def test_wide_stop_and_selling_rank_low(self):
        import pead_plus as pp
        wide = self._it(plan={"signal": "RISK_TOO_WIDE", "entry": None, "sl": None, "stage": "FULL", "redFlags": []},
                        price={"pressure": {"label": "SELLING"}}, strength="AVERAGE+", bucket={"code": "NO_CONFIRMATION"})
        g = pp.radar_grade(wide)
        self.assertEqual(g["tier"], "D")
        self.assertLess(g["score"], pp.radar_grade(self._it())["score"])

    def test_late_buy_after_trigger_is_not_top(self):
        import pead_plus as pp
        g = pp.radar_grade(self._it(plan={"signal": "ENTRY_TRIGGERED", "entry": 1326.0, "sl": 1225.82, "stage": "FULL", "redFlags": []},
                                    priceContext={"lastClose": 1380.0, "resultDayPct": 8.42, "relativeVolume": 7.8}))
        self.assertNotEqual(g["tier"], "A")
        self.assertIn("entry triggered earlier: buying now is late", g["cons"])


class ExactSymbolOtherCompanyTests(unittest.TestCase):
    """2.9.26 (20 Oct 2026): BSE's calendar row 509423 'SEL' (Sanathnagar
    Enterprises) was looked up on NSE; NSE lists 'SEL' exactly, but for
    Sungarner Energies (SME). pick_lookup_row took the exact symbol without
    checking the name, so the NSE:SEL event mixed Sungarner's prices / quote
    with Sanathnagar's BSE code, Realty sector, P/B, ROE and result date."""
    import json as _json
    import tempfile as _tempfile
    P = date(2026, 9, 30)
    META = {"ISIN": "INE367E01033", "PE": "138.38", "PB": "-0.89", "ROE": "-0.65", "OPM": "-", "Group": "X",
            "Sector": "Consumer Discretionary", "IndustryNew": "Realty", "IGroup": "Realty",
            "ISubGroup": "Residential, Commercial Projects"}

    def test_exact_symbol_of_another_company_is_rejected(self):
        rows = [{"symbol": "ROSSTECH", "companyName": "Rossell Techsys Limited"},
                {"symbol": "SEL", "companyName": "Sungarner Energies Limited"}]
        self.assertIsNone(p.pick_lookup_row(rows, "SEL", "Sanathnagar Enterprises Ltd"))
        self.assertEqual(p.pick_lookup_row(rows, "SEL", "Sungarner Energies Ltd")["symbol"], "SEL")
        self.assertEqual(p.pick_lookup_row(rows, "SEL")["symbol"], "SEL")          # no name: exact symbol

    def _setup(self):
        root = Path(self._tempfile.mkdtemp())
        st = p.EventStore(root / "events")
        raw = root / "raw"
        folder = raw / "bse" / "NSE_SEL_2026-09-30"
        folder.mkdir(parents=True)
        (folder / "lookup-1.json").write_text("null")                 # BSE lookup by the NSE name found nothing
        (folder / "equity_meta-1.json").write_text(self._json.dumps(self.META))
        master = p.SymbolMaster(root / "master" / "symbols.json")
        owner = st.ensure_event(security={"isin": "INE367E01033", "symbol": "SEL", "bseSymbol": "SEL",
                                          "bseCode": "509423", "name": "Sanathnagar Enterprises Ltd"}, period_end=self.P)
        st.save(owner)
        sel = st.ensure_event(security={"symbol": "SEL", "nseSymbol": "SEL", "name": "Sungarner Energies Limited"},
                              period_end=self.P)
        sec = sel["security"]
        sec.update({"bseSymbol": "SEL", "bseCode": "509423", "bseGroup": "X", "nseSeries": "SM",
                    "basicIndustry": "Other Electrical Equipment", "macroSector": "Consumer Discretionary",
                    "exchangeSector": "Realty", "industry": "Realty"})
        st.merge_field(sel, "market_cap_cr", 71.88, source="NSE_QUOTE")
        st.merge_field(sel, "last_price", 310.0, source="NSE_PRICE")
        st.merge_field(sel, "pb", -0.89, source="BSE_META")
        st.merge_field(sel, "roe_pct", -0.65, source="BSE_META")
        st.merge_field(sel, "result_date", "2026-10-21", source="BSE_RESULT_ANNOUNCEMENT")
        sel["mergedFrom"] = ["BSE:509423|2026-09-30"]
        st.save(sel)
        st.add_alias("BSE:509423|2026-09-30", owner["eventId"])
        master.data["securities"]["NSE:SEL"] = dict(sec, name="Sanathnagar Enterprises Ltd", securityKey="NSE:SEL")
        return st, raw, master, sel["eventId"], owner["eventId"]

    def test_repair_detaches_code_owned_by_another_company(self):
        st, raw, master, sel_id, owner_id = self._setup()
        fixed = p.repair_lookup_mismatches(st, master, raw)
        self.assertTrue(any(f["eventId"] == sel_id for f in fixed))
        ev = st.load(sel_id)
        sec = ev["security"]
        for k in ("bseCode", "bseSymbol", "bseGroup", "macroSector", "exchangeSector", "industry"):
            self.assertNotIn(k, sec, k)
        self.assertEqual(sec.get("basicIndustry"), "Other Electrical Equipment")   # NSE's own label kept
        self.assertEqual(sec.get("nseSymbol"), "SEL")
        self.assertIsNone(st.value(ev, "pb"))
        self.assertIsNone(st.value(ev, "roe_pct"))
        self.assertIsNone(st.value(ev, "result_date"))                # the other company's date
        self.assertEqual(st.value(ev, "market_cap_cr"), 71.88)        # own values kept
        self.assertEqual(st.value(ev, "last_price"), 310.0)
        self.assertFalse(ev.get("mergedFrom"))
        self.assertEqual(st.resolve("BSE:509423|2026-09-30"), owner_id)
        own = st.load(owner_id)
        self.assertEqual(own["security"]["bseCode"], "509423")        # owner untouched
        self.assertNotIn("bseCode", master.data["securities"]["NSE:SEL"])
        # Idempotent: a second pass changes nothing.
        self.assertFalse([f for f in p.repair_lookup_mismatches(st, master, raw) if f["eventId"] == sel_id])

    def test_shared_code_of_the_same_company_is_left_alone(self):
        st, raw, master, sel_id, owner_id = self._setup()
        ev = st.load(sel_id)
        ev["security"]["name"] = "Sanathnagar Enterprises Limited"
        st.save(ev)
        p.repair_lookup_mismatches(st, master, raw)
        self.assertEqual(st.load(sel_id)["security"].get("bseCode"), "509423")


class RsiDivergenceAndClosedTradeTests(unittest.TestCase):
    """2.9.28: PREMIERPOL was sold at 99.96 (entry 109.49, stop 105.14) on 21 Oct;
    the owner saw a weekly / monthly RSI bearish divergence before it failed."""

    def test_closed_trade_r_multiple(self):
        import pead_plus as pp
        c = pp.closed_trade_view({"symbol": "PREMIERPOL", "entryDate": "2026-10-16", "entryPrice": 109.49, "stop": 105.14,
                                  "exitDate": "2026-10-21", "exitPrice": 99.96, "status": "CLOSED"})
        self.assertEqual((c["r"], c["pnlPct"], c["days"]), (-2.19, -8.7, 5))
        s = pp.closed_summary([c, dict(c, r=1.5)])
        self.assertEqual((s["trades"], s["wins"], s["totalR"]), (2, 1, -0.69))
        self.assertIsNone(pp.closed_trade_view({"symbol": "X", "entryPrice": 10}))

    def test_rsi_wilder_bounds(self):
        import pead_plus as pp
        up = pp.rsi_series([float(i) for i in range(1, 40)])
        self.assertEqual(up[-1], 100.0)
        self.assertIsNone(up[5])
        mixed = pp.rsi_series([10, 11, 10.5, 11.5, 11, 12, 11.4, 12.4, 12, 13, 12.5, 13.5, 13, 14, 13.4, 14.4, 14.0])
        self.assertTrue(0 < mixed[-1] < 100)

    def _bars(self, closes):
        from datetime import date, timedelta
        d0 = date(2025, 1, 6)
        return [((d0 + timedelta(weeks=i)).isoformat(), c * 1.01, c * 0.99, c) for i, c in enumerate(closes)]

    def test_bearish_divergence_found(self):
        import pead_plus as pp
        # strong rally to a first high, a pullback, then a slow grind to a slightly higher high
        closes = [100 + i * 0.2 for i in range(20)]                      # base
        closes += [104 + i * 3 for i in range(8)]                          # sharp rally -> high ~125 (RSI high)
        closes += [124, 121, 118, 116, 117]                                 # pullback
        closes += [118, 119.5, 121, 122.5, 124, 125.5, 126.5]               # weaker grind to a higher high
        g = pp.rsi_divergence(self._bars(closes), "weekly")
        self.assertEqual(g["divergence"], "BEARISH", g)
        self.assertIn("lower high", g["note"])

    def test_no_divergence_on_steady_trend_and_short_history(self):
        import pead_plus as pp
        g = pp.rsi_divergence(self._bars([100 + (i % 4) for i in range(40)]), "weekly")
        self.assertIsNone(g["divergence"])
        short = pp.rsi_divergence(self._bars([100.0] * 12), "monthly")
        self.assertIsNone(short["rsi"])
        self.assertIn("Not enough monthly history", short["note"])

    def test_resample_weekly_monthly(self):
        import pead_plus as pp
        daily = [("2026-10-12", 11, 9, 10), ("2026-10-13", 12, 10, 11), ("2026-10-19", 13, 11, 12)]
        wk = pp.resample_bars(daily, "weekly")
        self.assertEqual(wk[0], ("2026-10-13", 12, 9, 11))
        self.assertEqual(len(pp.resample_bars(daily, "monthly")), 1)

    def test_divergence_raises_a_holding_flag(self):
        import pead_plus as pp
        from datetime import date
        v = pp.holding_watch_view("X", {"rsi": {"monthly": {"rsi": 66, "divergence": "BEARISH", "note": "n"}, "source": "Yahoo X.NS"}},
                                  [], None, None, {}, None, date(2026, 10, 21),
                                  rsi_weekly_local={"rsi": 58, "divergence": None, "note": "No divergence."})
        self.assertIn("monthly RSI bearish divergence", v["flags"])
        self.assertEqual(v["rsi"]["weekly"]["rsi"], 58)


class DiscontinuedOpsComparativeTests(unittest.TestCase):
    """2.9.29 (21 Oct 2026): LTTS sold its Smart World & Communication unit.
    The Q2 FY27 XBRL shows revenue from CONTINUING operations (Rs 3,040.4 Cr)
    and a discontinued-operations profit (Rs 168.6 Cr); last year's Sep-2025
    filing, read by fill_comparatives_from_listing because this document has
    no comparatives, still included the unit (Rs 2,979.5 Cr, discontinued 0).
    The card showed revenue +2.0% YoY; the company's restated growth is +12.1%.
    Revenue from a filing with discontinued operations is never compared with
    an older filing that had none (different perimeter): it stays blank.
    Profit (owners' line includes discontinued in both) is still compared;
    the Jun-2026 filing already split the unit, so QoQ stays."""

    def _parse(self, name, pe):
        blob = (Path(__file__).parent / "fixtures" / "nse" / name).read_bytes()
        return p.parse_xbrl_financials([blob], pe)

    def _listing(self):
        return [{"periodEnd": pe, "fromDate": fd, "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": pe}
                for pe, fd in (("2026-06-30", "2026-04-01"), ("2025-09-30", "2025-07-01"), ("2025-06-30", "2025-04-01"))]

    def _docs(self):
        return {"2025-09-30": self._parse("LTTS_CONS_2025-09-30.xml", date(2025, 9, 30)),
                "2026-06-30": self._parse("LTTS_CONS_2026-06-30.xml", date(2026, 6, 30)),
                "2025-06-30": self._parse("LTTS_CONS_2025-06-30.xml", date(2025, 6, 30))}

    def test_parser_records_discontinued_operations(self):
        cur = self._parse("LTTS_CONS_2026-09-30.xml", date(2026, 9, 30))
        self.assertAlmostEqual(cur["_meta"]["discontinuedCr"], 168.6, places=1)
        ya = self._parse("LTTS_CONS_2025-09-30.xml", date(2025, 9, 30))
        self.assertEqual(ya["_meta"]["discontinuedCr"], 0)

    def test_ltts_revenue_yoy_withheld_profit_kept(self):
        cur = self._parse("LTTS_CONS_2026-09-30.xml", date(2026, 9, 30))
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED",
                                         lambda url, pe: self._docs()[url])
        self.assertIsNone(cur.get("revenue_yoy_pct"))          # was 2.04 (wrong perimeter)
        self.assertIsNone(cur.get("prior_year_revenue_cr"))
        self.assertIsNone(cur.get("ytd_revenue_yoy_pct"))       # was 2.31
        self.assertAlmostEqual(cur["pat_yoy_pct"], 66.78, places=1)   # owners' PAT both years
        self.assertAlmostEqual(cur["revenue_qoq_pct"], 3.41, places=1)  # Jun-26 already split the unit
        self.assertIsNone(cur.get("prev_q_revenue_yoy_pct"))    # Jun-26 (split) vs Jun-25 (not)
        self.assertTrue(any(i.startswith("DISCONTINUED_OPS_NOT_RESTATED") for i in cur["_meta"]["issues"]))

    def test_no_discontinued_ops_unchanged(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitlossforperiod"}
        mk = lambda rev, pat, disc: {"revenue_cr": rev, "pat_cr": pat, "_meta": {"concepts": concepts, "discontinuedCr": disc,
                                                                            "patTotalCr": pat, "patTotalConcept": "profitlossforperiod"}}
        cur = mk(110.0, 11.0, None)
        cur["revenue_qoq_pct"] = 1.0
        p.fill_comparatives_from_listing(cur, self._listing()[1:2], date(2026, 9, 30), "CONSOLIDATED",
                                         lambda u, pe: mk(100.0, 10.0, 0))
        self.assertAlmostEqual(cur["revenue_yoy_pct"], 10.0)
        # an older filing that had discontinued ops (unit since gone) vs a current one without: comparable
        cur = mk(110.0, 11.0, 0)
        cur["revenue_qoq_pct"] = 1.0
        p.fill_comparatives_from_listing(cur, self._listing()[1:2], date(2026, 9, 30), "CONSOLIDATED",
                                         lambda u, pe: mk(100.0, 10.0, 5.0))
        self.assertAlmostEqual(cur["revenue_yoy_pct"], 10.0)


class ThemeTagTests(unittest.TestCase):
    """2.9.30: theme tags from master/themes.json (display only)."""

    def test_tags_by_symbol(self):
        import json
        import pead_plus as pp
        themes = json.loads((Path(__file__).resolve().parent.parent / "master" / "themes.json").read_text())["themes"]
        items = [{"symbol": "STLTECH", "plus": {}}, {"symbol": "syngene", "plus": {}}, {"symbol": "HCLTECH", "plus": {}}]
        self.assertEqual(pp.theme_tags(items, themes), 2)
        self.assertEqual(items[0]["plus"]["themes"], ["AI infra / OFC"])
        self.assertEqual(items[1]["plus"]["themes"], ["Pharma CDMO"])
        self.assertNotIn("themes", items[2]["plus"])
        self.assertEqual(pp.theme_tags(items, None), 0)


class ComparativeUnitErrorTests(unittest.TestCase):
    """2.9.31 (22 Oct 2026): METROBRAND's Q2 FY27 consolidated XBRL is correct
    (revenue Rs 730.27 Cr, +1.4% QoQ against the Jun-2026 filing), but its
    year-ago Sep-2025 filing was filed 100x too small (Rs 6.51 Cr revenue,
    Rs 0.68 Cr profit). fill_comparatives_from_listing took that as last year,
    the snapshot showed revenue +11,115% YoY and was REJECTED as UNIT_SUSPECT,
    so the exchange XBRL lost to the BSE table and the card had no YoY at all.
    When a comparative read from ANOTHER filing is unit-like (> 20x off) while
    the other comparative agrees with this filing, that other filing is the
    suspect one: its figures are dropped (missing, not invented, never
    rescaled) and the current filing stays VERIFIED."""

    def _parse(self, name, pe):
        blob = (Path(__file__).parent / "fixtures" / "nse" / name).read_bytes()
        return p.parse_xbrl_financials([blob], pe)

    def _listing(self):
        return [{"periodEnd": pe, "fromDate": fd, "basis": "CONSOLIDATED", "cumulative": False, "xbrlUrl": pe}
                for pe, fd in (("2026-06-30", "2026-04-01"), ("2025-09-30", "2025-07-01"), ("2025-06-30", "2025-04-01"))]

    def _docs(self):
        return {pe: self._parse(f"METROBRAND_CONS_{pe}.xml", date.fromisoformat(pe))
                for pe in ("2025-09-30", "2026-06-30", "2025-06-30")}

    def test_mis_scaled_year_ago_filing_is_dropped(self):
        cur = self._parse("METROBRAND_CONS_2026-09-30.xml", date(2026, 9, 30))
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED",
                                         lambda url, pe: self._docs()[url])
        self.assertIsNone(cur.get("revenue_yoy_pct"))          # was 11115.25
        self.assertIsNone(cur.get("prior_year_revenue_cr"))
        self.assertIsNone(cur.get("pat_yoy_pct"))
        self.assertIsNone(cur.get("pat_trend"))
        self.assertIsNone(cur.get("prior_ytd_revenue_cr"))
        self.assertIsNone(cur.get("ytd_revenue_yoy_pct"))
        self.assertIsNone(cur.get("ytd_pat_yoy_pct"))
        self.assertAlmostEqual(cur["revenue_qoq_pct"], 1.38, places=1)   # Jun-2026 filing agrees
        self.assertAlmostEqual(cur["prev_q_revenue_yoy_pct"], 14.66, places=1)
        self.assertTrue(any(i.startswith("COMPARATIVE_UNIT_SUSPECT:2025-09-30") for i in cur["_meta"]["issues"]))
        self.assertNotIn("2025-09-30", cur["_meta"].get("comparativeSources", {}))
        v = p.validate_financial_snapshot(cur, "NSE_XBRL", date(2026, 9, 30))
        self.assertEqual(v["status"], "VERIFIED")              # was REJECTED (EXTREME_REVENUE_YOY, UNIT_SUSPECT)

    def test_lone_wild_comparative_still_rejects(self):
        # Only one comparative and it is 100x off: we cannot tell which filing is wrong.
        cur = self._parse("METROBRAND_CONS_2026-09-30.xml", date(2026, 9, 30))
        p.fill_comparatives_from_listing(cur, self._listing()[1:2], date(2026, 9, 30), "CONSOLIDATED",
                                         lambda url, pe: self._docs()[url])
        self.assertAlmostEqual(cur["revenue_yoy_pct"], 11115.25, delta=1)
        self.assertEqual(p.validate_financial_snapshot(cur, "NSE_XBRL", date(2026, 9, 30))["status"], "REJECTED")

    def test_real_jump_kept(self):
        concepts = {"revenue": "revenuefromoperations", "pat": "profitlossforperiod"}
        mk = lambda rev, pat: {"revenue_cr": rev, "pat_cr": pat, "_meta": {"concepts": concepts,
                                                                          "patTotalCr": pat, "patTotalConcept": "profitlossforperiod"}}
        cur = mk(300.0, 30.0)
        docs = {"2025-09-30": mk(100.0, 10.0), "2026-06-30": mk(250.0, 25.0), "2025-06-30": mk(90.0, 9.0)}
        p.fill_comparatives_from_listing(cur, self._listing(), date(2026, 9, 30), "CONSOLIDATED", lambda u, pe: docs[u])
        self.assertAlmostEqual(cur["revenue_yoy_pct"], 200.0)   # 3x is a real jump, not a unit error
        self.assertAlmostEqual(cur["revenue_qoq_pct"], 20.0)


class RenamedSymbolTests(unittest.TestCase):
    """2.9.32 (23 Oct 2026): ICICI Prudential Life (ISIN INE726G01019, NSE
    ICICIPRULI, BSE 540133) became ICICI Life Insurance, NSE symbol ICICILIFE.
    Discovery created a second event NSE:ICICILIFE without an ISIN (the NSE
    lookup row has none; the saved quote has isinCode INE726G01019). The two
    names differ, so the duplicate fold did not join them, the 2.9.26 repair
    took BSE 540133 away as 'another company's code' and the self-check FAILed
    'Declared results have revenue/profit' for ICICILIFE; the radar listed the
    company twice."""
    import json as _json
    import tempfile as _tempfile
    P = date(2026, 9, 30)

    def _quote(self, raw, prefix, symbol, isin, name):
        folder = raw / "nse" / f"{prefix}_2026-09-30"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "quote-1.json").write_text(self._json.dumps(
            {"metaData": {"symbol": symbol, "isinCode": isin, "companyName": name, "closePrice": 431}}))

    def _setup(self):
        root = Path(self._tempfile.mkdtemp())
        st = p.EventStore(root / "events")
        raw = root / "raw"
        old = st.ensure_event(security={"isin": "INE726G01019", "symbol": "ICICIPRULI", "nseSymbol": "ICICIPRULI",
                                        "bseSymbol": "ICICIPRULI", "bseCode": "540133",
                                        "name": "ICICI Prudential Life Insurance Company Ltd",
                                        "yahooTicker": "540133.BO"}, period_end=self.P)
        st.merge_field(old, "results_released", True, source="BSE_RESULT_ANNOUNCEMENT")
        st.merge_field(old, "market_cap_cr", 62548.3, source="NSE_QUOTE")
        st.save(old)
        new = st.ensure_event(security={"symbol": "ICICILIFE", "nseSymbol": "ICICILIFE",
                                        "name": "ICICI Life Insurance Ltd", "yahooTicker": "ICICILIFE.NS",
                                        "bseCode": "540133"}, period_end=self.P)
        st.merge_field(new, "results_released", True, source="NSE_INTEGRATED_FILING")
        st.merge_field(new, "last_price", 431.0, source="NSE_PRICE")
        st.save(new)
        self._quote(raw, "NSE_ICICILIFE", "ICICILIFE", "INE726G01019", "ICICI Life Insurance Limited")
        return st, raw, old["eventId"], new["eventId"]

    def test_renamed_symbol_folds_into_the_isin_event(self):
        st, raw, old_id, new_id = self._setup()
        master = p.SymbolMaster(Path(self._tempfile.mkdtemp()) / "symbols.json")
        stats = p.integrity_pass(st, today=date(2026, 10, 23), raw_root=raw, master=master)
        ids = [e["eventId"] for e in st.all()]
        self.assertEqual(ids, [old_id])                         # one company, one event
        self.assertEqual(st.resolve(new_id), old_id)
        ev = st.load(old_id)
        sec = ev["security"]
        self.assertEqual(sec["nseSymbol"], "ICICILIFE")         # current NSE symbol
        self.assertIn("ICICIPRULI", sec.get("formerSymbols") or [])
        self.assertEqual(sec["bseCode"], "540133")              # not stripped as another company's code
        self.assertNotIn("identityRepaired", sec)
        self.assertEqual(st.value(ev, "market_cap_cr"), 62548.3)  # verified values kept
        self.assertEqual(st.value(ev, "last_price"), 431.0)
        self.assertGreaterEqual(stats["duplicatesMerged"], 1)

    def test_earlier_wrong_code_repair_is_undone_for_a_rename(self):
        st, raw, old_id, new_id = self._setup()
        ev = st.load(new_id)
        ev["security"].pop("bseCode")
        ev["security"]["identityRepaired"] = {"wrongBseCode": "540133",
                                              "wrongBseName": "ICICI Prudential Life Insurance Company Ltd"}
        st.save(ev)
        adopted = p.adopt_quote_isins(st, raw)
        self.assertEqual([a["eventId"] for a in adopted], [new_id])
        sec = st.load(new_id)["security"]
        self.assertEqual(sec["isin"], "INE726G01019")
        self.assertNotIn("identityRepaired", sec)

    def test_quote_of_another_symbol_or_company_is_not_adopted(self):
        root = Path(self._tempfile.mkdtemp())
        st = p.EventStore(root / "events")
        raw = root / "raw"
        a = st.ensure_event(security={"symbol": "SEL", "nseSymbol": "SEL", "name": "Sanathnagar Enterprises Ltd"},
                            period_end=self.P)
        st.save(a)
        self._quote(raw, "NSE_SEL", "SEL", "INE0SUN01011", "Sungarner Energies Limited")   # other company's name
        b = st.ensure_event(security={"symbol": "ABC", "nseSymbol": "ABC", "name": "Abc Ltd"}, period_end=self.P)
        st.save(b)
        self._quote(raw, "NSE_ABC", "XYZ", "INE111A01011", "Abc Limited")                   # other symbol
        self.assertEqual(p.adopt_quote_isins(st, raw), [])
        self.assertNotIn("isin", st.load(a["eventId"])["security"])
        self.assertNotIn("isin", st.load(b["eventId"])["security"])

    def test_rebuild_accepts_price_rows_under_the_former_symbol(self):
        rows = [{"chSymbol": "ICICIPRULI", "mtimestamp": "22-Oct-2026", "chClosingPrice": 431}]
        sec = {"nseSymbol": "ICICILIFE", "formerSymbols": ["ICICIPRULI"]}
        self.assertTrue(p._payload_symbol_ok(rows, sec))
        self.assertFalse(p._payload_symbol_ok(rows, {"nseSymbol": "ICICILIFE"}))
        self.assertTrue(p._payload_symbol_ok([{"mtimestamp": "x"}], {"nseSymbol": "ICICILIFE"}))


class HeldTradeEnrichmentFirstTests(unittest.TestCase):
    """2.9.32 (23 Oct 2026): after the close, 47 results of the last two days
    (rank A0) came before the open trades (A1) in every run; ~60 declared slots
    per run and new A0 rows each hour meant GROWW / THELEELA / HCLTECH were
    never re-priced after 15:45 (last attempt 22 Oct 19:46 IST), so My trades
    judged the stops on the 22 Oct close at 20:42 IST."""

    def _ev(self, i, sym, rd, last_session):
        return {"eventId": f"E{i:04d}", "period": {"end": "2026-09-30"},
                "security": {"nseSymbol": sym, "exchangeSector": "Information Technology"},
                "fields": {"result_date": {"value": rd.isoformat(), "status": "OK"},
                           "results_released": {"value": True, "status": "OK"},
                           "revenue_cr": {"value": 10.0, "status": "OK"}, "pat_cr": {"value": 1.0, "status": "OK"}},
                "fetch": {"PRICE_HISTORY": {"lastAttempt": "2026-10-22T14:17:00Z"}},
                "plus": {"price": {"last_session": last_session.isoformat()}}}

    def test_open_trade_behind_goes_before_fresh_results(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 23, 20, 0, tzinfo=p.IST)
        today = now.date()
        fresh = [self._ev(i, f"F{i}", date(2026, 10, 22), date(2026, 10, 22)) for i in range(80)]
        held = self._ev(999, "GROWW", date(2026, 10, 14), date(2026, 10, 22))
        with mock.patch.object(p, "now_ist", return_value=now), \
                mock.patch.object(p, "open_positions", return_value=[{"symbol": "GROWW"}]):
            self.assertEqual(p.last_completed_session(today), date(2026, 10, 23))
            self.assertEqual(p.enrichment_group(held, today, held={"GROWW"})[0], "A")
            sel, _ = p.select_for_enrichment(fresh + [held], 20, today)
        self.assertEqual(sel[0], held)


class YoYMissingEnrichmentTests(unittest.TestCase):
    """2.9.33 (26 Oct 2026): DRREDDY (results 23 Oct 17:30 IST, Rs 1 lakh Cr)
    showed the BSE table (basis unknown, no YoY) for three days. Its NSE
    listing was read at 18:31 IST, before the Integrated Filing appeared, and
    the event was never enriched again: results of the last two days without
    figures (rank A0, re-tried every hour even when looked at minutes ago) and
    the prices-behind rank filled every declared slot, while a declared result
    with figures but no YoY sat in the lowest rank (A4)."""

    def _ev(self, i, sym, rd, last_session, attempt, figures=True, yoy=None):
        fields = {"result_date": {"value": rd.isoformat(), "status": "OK"},
                  "results_released": {"value": True, "status": "OK"}}
        if figures:
            fields["revenue_cr"] = {"value": 5572.6, "status": "OK"}
            fields["pat_cr"] = {"value": 695.8, "status": "OK"}
        if yoy is not None:
            fields["revenue_yoy_pct"] = {"value": yoy, "status": "OK"}
            fields["pat_yoy_pct"] = {"value": yoy, "status": "OK"}
        return {"eventId": f"E{i:04d}", "period": {"end": "2026-09-30"},
                "security": {"nseSymbol": sym, "exchangeSector": "Healthcare"},
                "fields": fields,
                "fetch": {"PRICE_HISTORY": {"lastAttempt": attempt}},
                "plus": {"price": {"last_session": last_session.isoformat()}}}

    def test_yoy_missing_is_reached_before_fresh_rows_tried_minutes_ago(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 26, 11, 0, tzinfo=p.IST)        # market hours: last closed session 23 Oct
        today = now.date()
        fresh = [self._ev(i, f"F{i}", date(2026, 10, 26), date(2026, 10, 23), "2026-10-26T05:10:00Z", figures=False)
                 for i in range(40)]
        drreddy = self._ev(999, "DRREDDY", date(2026, 10, 23), date(2026, 10, 23), "2026-10-23T15:03:04Z")
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.last_completed_session(today), date(2026, 10, 23))
            self.assertEqual(p.enrichment_group(drreddy, today), ("A", 3))
            sel, _ = p.select_for_enrichment(fresh + [drreddy], 35, today)
        self.assertIn(drreddy, sel)

    def test_fresh_row_without_figures_still_first_when_not_tried_for_2h(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 26, 11, 0, tzinfo=p.IST)
        fresh = self._ev(1, "F1", date(2026, 10, 26), date(2026, 10, 23), "2026-10-26T02:00:00Z", figures=False)
        recent = self._ev(2, "F2", date(2026, 10, 26), date(2026, 10, 23), "2026-10-26T05:10:00Z", figures=False)
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.enrichment_group(fresh, now.date()), ("A", 0))
            self.assertEqual(p.enrichment_group(recent, now.date()), ("A", 3))

    def test_complete_result_and_old_result_unchanged(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 26, 11, 0, tzinfo=p.IST)
        done = self._ev(1, "D1", date(2026, 10, 23), date(2026, 10, 23), "2026-10-23T15:03:04Z", yoy=12.0)
        old = self._ev(2, "D2", date(2026, 10, 9), date(2026, 10, 23), "2026-10-23T15:03:04Z")   # > 10 days: no re-read
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.enrichment_group(done, now.date()), ("A", 4))
            self.assertEqual(p.enrichment_group(old, now.date()), ("D", 0))


class ReactionDueEnrichmentTests(unittest.TestCase):
    """2.9.34 (27 Oct 2026): COFORGE (Rs 84,000 Cr) and CUBEXTUB filed after
    the close on Fri 23 Oct; their reaction session was Mon 26 Oct. Rank A0
    ("result in the last 2 days") counts calendar days, so on Monday evening
    the result was 3 days old and both rotated in rank A2 with ~315 other
    "prices behind" rows (35 declared slots per run). By Tuesday evening the
    reaction was still unmeasured (self-check FAIL) - prices last fetched
    26 Oct 09:04 IST, before the session. A closed reaction session that the
    stored prices do not yet cover now ranks A0 (when the stock traded within
    10 days of the session and was not tried in the last 2 h)."""

    def _ev(self, sym, rd, rs, last_session, attempt):
        return {"eventId": f"E-{sym}", "period": {"end": "2026-09-30"},
                "security": {"nseSymbol": sym, "exchangeSector": "Information Technology"},
                "fields": {"result_date": {"value": rd.isoformat(), "status": "OK"},
                           "results_released": {"value": True, "status": "OK"},
                           "reaction_session": {"value": rs.isoformat(), "status": "OK"},
                           "revenue_cr": {"value": 4000.0, "status": "OK"},
                           "pat_cr": {"value": 400.0, "status": "OK"},
                           "revenue_yoy_pct": {"value": 20.0, "status": "OK"},
                           "pat_yoy_pct": {"value": 20.0, "status": "OK"}},
                "fetch": {"PRICE_HISTORY": {"lastAttempt": attempt}},
                "plus": {"price": {"last_session": last_session.isoformat()}}}

    def test_friday_after_close_result_reaction_due_on_tuesday(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 27, 20, 0, tzinfo=p.IST)
        today = now.date()
        coforge = self._ev("COFORGE", date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 23),
                           "2026-10-26T03:34:01Z")
        behind = [self._ev(f"X{i}", date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 24 - (i % 3)),
                           "2026-10-20T03:00:00Z") for i in range(60)]
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.enrichment_group(coforge, today), ("A", 0))
            sel, _ = p.select_for_enrichment(behind + [coforge], 35, today)
        self.assertIn(coforge, sel)

    def test_untraded_or_recently_tried_or_covered_not_promoted(self):
        from datetime import datetime as _dt
        now = _dt(2026, 10, 27, 20, 0, tzinfo=p.IST)
        today = now.date()
        untraded = self._ev("ALSTONE", date(2026, 10, 7), date(2026, 10, 8), date(2026, 5, 25), "2026-10-26T03:34:01Z")
        tried = self._ev("CUBEXTUB", date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 23), "2026-10-27T13:30:00Z")
        covered = self._ev("DONE", date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 26), "2026-10-26T13:30:00Z")
        with mock.patch.object(p, "now_ist", return_value=now), mock.patch.object(p, "open_positions", return_value=[]):
            self.assertEqual(p.enrichment_group(untraded, today), ("A", 2))
            self.assertEqual(p.enrichment_group(tried, today), ("A", 2))
            self.assertEqual(p.enrichment_group(covered, today)[1] != 0, True)
