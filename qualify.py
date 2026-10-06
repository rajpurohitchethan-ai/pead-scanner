#!/usr/bin/env python3

import asyncio
import json
import math
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from nse_mcp import fetch_nse_market_layer

try:
    from nse import NSE
except Exception:
    NSE = None

try:
    from bse import BSE
except Exception:
    BSE = None


BASE = Path(__file__).resolve().parent
DATA = BASE / "data.json"
IST = ZoneInfo("Asia/Kolkata")

REV_YOY_MIN = 10.0
PAT_YOY_MIN = 15.0
PAT_QOQ_FLOOR = -10.0
PRICED_IN_RUNUP_PCT = 15.0
RVOL_MIN = 1.20
EPS_SURPRISE_MIN = 5.0
MARKET_SURPRISE_PROXY_MIN = 3.0
LIQUIDITY_TURNOVER_CR_MIN = 5.0
CASHFLOW_TO_PAT_MIN = 0.50


SECTOR_PROXIES = {
    "bank": "^NSEBANK",
    "private bank": "^NSEBANK",
    "public bank": "^NSEBANK",
    "financial": "^CNXFIN",
    "nbfc": "^CNXFIN",
    "finance": "^CNXFIN",
    "wealth": "^CNXFIN",
    "asset management": "^CNXFIN",
    "insurance": "^CNXFIN",
    "it services": "^CNXIT",
    "software": "^CNXIT",
    "technology": "^CNXIT",
    "auto": "^CNXAUTO",
    "tyre": "^CNXAUTO",
    "pharma": "^CNXPHARMA",
    "healthcare": "^CNXPHARMA",
    "fmcg": "^CNXFMCG",
    "food": "^CNXFMCG",
    "steel": "^CNXMETAL",
    "metal": "^CNXMETAL",
    "mining": "^CNXMETAL",
    "real estate": "^CNXREALTY",
    "realty": "^CNXREALTY",
    "cement": "^CNXINFRA",
    "construction": "^CNXINFRA",
    "infrastructure": "^CNXINFRA",
    "energy": "^CNXENERGY",
    "oil": "^CNXENERGY",
    "gas": "^CNXENERGY",
    "media": "^CNXMEDIA",
}


def num(v):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def round2(v):
    x = num(v)
    return round(x, 2) if x is not None else None


def bool_value(v):
    if v is True or v is False:
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"true", "yes", "pass", "passed", "qualified", "satisfied", "ok"}:
        return True
    if s in {"false", "no", "fail", "failed", "not satisfied"}:
        return False
    return None


def parse_date(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v

    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def result_release_detection(row):
    """Strict result-release detection.

    A calendar/board-meeting date is NEVER enough to call a result released.
    The scanner/result-enrichment layer must provide explicit release proof.
    """
    rd = parse_date(row.get("resultDate") or row.get("result_date") or row.get("resultsDate") or row.get("earningsDate"))
    explicit = bool_value(row.get("resultsReleased"))
    if explicit is None:
        explicit = bool_value(row.get("resultReleased"))
    if explicit is None:
        explicit = bool_value(row.get("results_declared"))

    source = " ".join(
        str(row.get(k) or "")
        for k in (
            "discoverySource", "resultSource", "resultDataSource", "source",
            "resultsEvidence", "resultEvidence", "evidence",
            "resultSourceUrl", "filingUrl", "announcementUrl",
            "sourceUrl", "evidenceUrl", "resultsUrl",
        )
    ).lower()
    status = str(row.get("bucket") or row.get("peadStatus") or row.get("stage") or row.get("status") or "").lower()

    # Calendar / meeting rows are upcoming evidence, not release evidence.
    upcoming_tokens = (
        "board meeting", "result calendar", "scheduled", "upcoming",
        "awaiting result", "awaiting results", "pre-result", "pre result",
    )
    postponed_tokens = (
        "postponed", "rescheduled", "deferred", "cancelled", "canceled",
        "date changed", "board meeting postponed",
    )
    if any(token in f"{status} {source}" for token in postponed_tokens):
        return False, "Result appears postponed/rescheduled; waiting for a confirmed filing."

    # Explicit flag from scan/result enrichment is authoritative.
    if explicit is True:
        return True, row.get("resultsEvidence") or "Scanner/result layer explicitly verified the result release."
    if explicit is False:
        return False, row.get("resultsEvidence") or (
            f"Scheduled result date {rd} is not release proof; waiting for an actual filing."
            if rd else "Scanner marks the result as pending; waiting for an actual filing."
        )

    # Legacy rows without an explicit flag: accept only strong release-proof sources.
    release_tokens = (
        "nse financial results filing",
        "bse result announcement",
        "nse results comparison",
        "bse results snapshot",
        "quarterly statement fallback",
        "official quarterly financial-results filing",
        "official quarterly financial results filing",
        "result release verified",
    )
    if any(token in source for token in release_tokens) and not any(token in source for token in upcoming_tokens):
        return True, "Verified result-release source detected."

    # Do not infer release just because the result date has arrived or passed.
    if rd is not None:
        today = datetime.now(IST).date()
        if rd > today:
            return False, f"Scheduled result date is still in the future: {rd}."
        return False, f"Scheduled result date {rd} has arrived/passed, but no verified result filing was found."

    return False, "No verified result-release evidence is available."


def load_data():
    if not DATA.exists():
        raise RuntimeError("data.json missing; run scan.py first")

    payload = json.loads(DATA.read_text(encoding="utf-8"))

    if payload.get("scannerMode") != "live-discovery":
        raise RuntimeError("Expected scannerMode=live-discovery")

    rows = payload.get("stocks") or payload.get("companies") or []
    if not rows:
        raise RuntimeError("No live-discovery stocks found")

    return payload, rows


def clean_symbol(row):
    return (
        str(row.get("symbol") or row.get("sym") or "")
        .upper()
        .replace(".NS", "")
        .replace(".BO", "")
        .strip()
    )


def yahoo_ticker(row):
    explicit = str(row.get("ticker") or row.get("yahooTicker") or "").strip().upper()
    if explicit.endswith((".NS", ".BO")):
        return explicit
    code = str(row.get("bseCode") or row.get("bse_code") or row.get("scripCode") or "").strip()
    if code.isdigit() and len(code) == 6:
        return f"{code}.BO"
    s = clean_symbol(row)
    return f"{s}.NS" if s else None


def records_to_df(records):
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    if df.empty or "Date" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame()

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).copy()
    if df.empty:
        return pd.DataFrame()

    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.sort_values("Date").set_index("Date")
    return df


def history_for_yf(batch, ticker):
    if batch is None or batch.empty or not ticker:
        return pd.DataFrame()

    try:
        if isinstance(batch.columns, pd.MultiIndex):
            level0 = batch.columns.get_level_values(0)
            level1 = batch.columns.get_level_values(1)

            if ticker in level0:
                h = batch[ticker].copy()
            elif ticker in level1:
                h = batch.xs(ticker, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        else:
            h = batch.copy()

        if "Close" in h:
            h = h[h["Close"].notna()]

        return h.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def technicals(h):
    if h.empty or "Close" not in h:
        return {}

    close = h["Close"].astype(float)

    out = {
        "lastClose": round2(close.iloc[-1]),
        "ma10": round2(close.ewm(span=10, adjust=False).mean().iloc[-1]) if len(close) >= 10 else None,
        "ma20": round2(close.ewm(span=20, adjust=False).mean().iloc[-1]) if len(close) >= 20 else None,
        "ma50": round2(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None,
        "ma200": round2(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None,
    }

    if "Volume" in h and len(h) >= 21:
        volume = h["Volume"].astype(float)
        average_volume = float(volume.iloc[-21:-1].mean())

        out["relativeVolume"] = (
            round2(float(volume.iloc[-1]) / average_volume)
            if average_volume > 0
            else None
        )

        turnover = ((close.tail(20) * volume.tail(20)).mean() / 10_000_000)
        out["avgTurnover20dCr"] = round2(turnover)
    else:
        out["relativeVolume"] = None
        out["avgTurnover20dCr"] = None

    return out


def result_metrics(h, result_date):
    out = {
        "preResultRunupPct": None,
        "resultDayReturnPct": None,
        "resultDayHigh": None,
        "resultDayLow": None,
        "preResult20dHigh": None,
        "pricedIn": False,
    }

    if h.empty or result_date is None or "Close" not in h:
        return out

    result_index = None
    for i, idx in enumerate(h.index):
        if pd.Timestamp(idx).date() >= result_date:
            result_index = i
            break

    end_index = len(h) if result_index is None else result_index
    pre = h.iloc[:end_index]

    if len(pre) >= 20:
        start_close = num(pre["Close"].iloc[-20])
        end_close = num(pre["Close"].iloc[-1])

        if start_close not in (None, 0) and end_close is not None:
            runup = (end_close / start_close - 1) * 100
            out["preResultRunupPct"] = round2(runup)
            out["pricedIn"] = runup > PRICED_IN_RUNUP_PCT

        if "High" in pre:
            out["preResult20dHigh"] = round2(pre["High"].tail(20).max())

    if result_index is not None and result_index < len(h):
        rr = h.iloc[result_index]
        previous_close = num(h.iloc[result_index - 1]["Close"]) if result_index > 0 else None
        result_close = num(rr.get("Close"))

        if previous_close not in (None, 0) and result_close is not None:
            out["resultDayReturnPct"] = round2((result_close / previous_close - 1) * 100)

        if "High" in h:
            out["resultDayHigh"] = round2(rr.get("High"))
        if "Low" in h:
            out["resultDayLow"] = round2(rr.get("Low"))

    return out


def sector_proxy(row):
    text = f"{row.get('sector', '')} {row.get('industry', '')}".lower()
    for key, value in SECTOR_PROXIES.items():
        if key in text:
            return value
    return "^NSEI"


def sector_tailwind(h):
    if h.empty or "Close" not in h or len(h) < 200:
        return None, {}

    close = h["Close"].astype(float)
    last = float(close.iloc[-1])
    ma10 = float(close.ewm(span=10, adjust=False).mean().iloc[-1])
    ma20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])

    return all(last > x for x in (ma10, ma20, ma50, ma200)), {
        "sectorClose": round2(last),
        "sectorMa10": round2(ma10),
        "sectorMa20": round2(ma20),
        "sectorMa50": round2(ma50),
        "sectorMa200": round2(ma200),
    }


def is_financial(row):
    text = f"{row.get('sector', '')} {row.get('industry', '')}".lower()
    return any(
        x in text
        for x in (
            "bank", "financial", "finance", "nbfc",
            "insurance", "asset management", "wealth",
        )
    )


def statement_row(df, names):
    if df is None or getattr(df, "empty", True):
        return None

    normalized = {str(index).strip().lower(): index for index in df.index}

    for name in names:
        if name.lower() in normalized:
            return normalized[name.lower()]

    for low, original in normalized.items():
        if any(name.lower() in low for name in names):
            return original

    return None


def cashflow_gate(ticker, row):
    if is_financial(row):
        pat_yoy = num(row.get("patYoY"))
        quality = bool_value(row.get("earningsQualityPass"))
        passed = quality is True and pat_yoy is not None and pat_yoy > 0
        return passed, {
            "cashFlowMethod": "financial-sector proxy",
            "cashFlowEvidence": "CFO not comparable for lenders/financials; positive earnings quality + PAT YoY growth used",
        }

    try:
        cashflow = ticker.quarterly_cash_flow

        operating_row = statement_row(
            cashflow,
            [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
                "Cash Flow From Continuing Operating Activities",
            ],
        )

        income_row = statement_row(
            cashflow,
            [
                "Net Income From Continuing Operations",
                "Net Income",
            ],
        )

        if operating_row is None:
            return None, {
                "cashFlowMethod": "yfinance quarterly cash flow",
                "cashFlowEvidence": "Operating cash flow unavailable",
            }

        operating_series = cashflow.loc[operating_row].dropna()
        income_series = cashflow.loc[income_row].dropna() if income_row is not None else pd.Series(dtype=float)

        operating_cashflow = num(operating_series.iloc[0]) if not operating_series.empty else None
        net_income = num(income_series.iloc[0]) if not income_series.empty else None

        if operating_cashflow is None:
            return None, {
                "cashFlowMethod": "yfinance quarterly cash flow",
                "cashFlowEvidence": "Latest operating cash flow unavailable",
            }

        ratio = operating_cashflow / abs(net_income) if net_income not in (None, 0) else None
        passed = operating_cashflow > 0 and (ratio is None or ratio >= CASHFLOW_TO_PAT_MIN)

        return passed, {
            "operatingCashFlow": round2(operating_cashflow),
            "cashFlowToNetIncome": round2(ratio),
            "cashFlowMethod": "yfinance quarterly cash flow",
            "cashFlowEvidence": "OCF positive" + (f"; OCF/net income {ratio:.2f}x" if ratio is not None else ""),
        }

    except Exception as exc:
        return None, {
            "cashFlowMethod": "yfinance quarterly cash flow",
            "cashFlowEvidence": f"Unavailable: {type(exc).__name__}",
        }


def eps_surprise(ticker, result_date):
    try:
        earnings = ticker.get_earnings_dates(limit=12)
        if earnings is None or earnings.empty or result_date is None:
            return None

        matches = []
        for index, row in earnings.iterrows():
            distance = abs((pd.Timestamp(index).date() - result_date).days)
            if distance <= 4:
                matches.append((distance, row))

        if not matches:
            return None

        row = sorted(matches, key=lambda x: x[0])[0][1]

        for column in ("Surprise(%)", "Surprise %", "Surprise"):
            if column in row.index and num(row.get(column)) is not None:
                return num(row.get(column))

        estimate = num(row.get("EPS Estimate"))
        reported = num(row.get("Reported EPS"))
        if estimate not in (None, 0) and reported is not None:
            return (reported / estimate - 1) * 100

        return None
    except Exception:
        return None



def expected_period_end(row):
    explicit = parse_date(
        row.get("resultPeriodEnd")
        or row.get("periodEnd")
        or row.get("periodEnded")
        or row.get("toDate")
    )
    if explicit is not None:
        return explicit

    rd = parse_date(
        row.get("resultDate")
        or row.get("result_date")
        or row.get("resultsDate")
        or row.get("earningsDate")
    )
    if rd is None:
        return None

    candidates = [
        date(rd.year - 1, 12, 31),
        date(rd.year, 3, 31),
        date(rd.year, 6, 30),
        date(rd.year, 9, 30),
        date(rd.year, 12, 31),
    ]
    prior = [d for d in candidates if d < rd]
    return max(prior) if prior else None


def _nse_client():
    if NSE is None:
        return None
    return NSE(download_folder=str(BASE))


def _metric_from_record(record, *aliases):
    if not isinstance(record, dict):
        return None
    lowered = {str(k).lower().replace("-", "_"): v for k, v in record.items()}
    for key in aliases:
        v = record.get(key)
        if v not in (None, ""):
            return num(v)
        v = lowered.get(key.lower().replace("-", "_"))
        if v not in (None, ""):
            return num(v)
    return None


def _record_date(record):
    if not isinstance(record, dict):
        return None
    for key in (
        "re_to_dt", "toDate", "periodEnd", "periodEnded", "endDate",
        "re_end_dt", "quarterEnd", "date",
    ):
        d = parse_date(record.get(key))
        if d is not None:
            return d
    return None


def _pct(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def parse_nse_comparison(payload, row):
    records = []
    if isinstance(payload, dict):
        records = payload.get("resCmpData") or payload.get("data") or []
    if not isinstance(records, list) or not records:
        return {}

    dated = [(d, r) for r in records if isinstance(r, dict) for d in [_record_date(r)] if d is not None]
    dated.sort(key=lambda x: x[0], reverse=True)
    if not dated:
        return {}

    expected = expected_period_end(row)
    if expected is not None:
        candidates = sorted(dated, key=lambda x: abs((x[0] - expected).days))
        current_date, current = candidates[0]
        if abs((current_date - expected).days) > 45:
            return {}
    else:
        current_date, current = dated[0]

    older = [(d, r) for d, r in dated if d < current_date]
    previous = older[0][1] if older else None
    previous_date = older[0][0] if older else None

    yoy_target = date(current_date.year - 1, current_date.month, min(current_date.day, 28))
    yoy_candidates = [(abs((d - yoy_target).days), d, r) for d, r in dated if d < current_date]
    yoy_row = None
    yoy_date = None
    if yoy_candidates:
        distance, yoy_date, yoy_row = min(yoy_candidates, key=lambda x: x[0])
        if distance > 50:
            yoy_row = None
            yoy_date = None

    revenue_aliases = (
        "re_net_sale", "re_net_sales", "re_revenue", "re_revenue_from_operations",
        "re_total_inc", "re_total_income", "re_income",
    )
    pat_aliases = (
        "re_net_profit", "re_profit_after_tax", "re_pat",
        "re_profit_loss", "net_profit",
    )
    eps_aliases = (
        "re_basic_eps", "re_basic_eps_for_cont_dic_opr",
        "re_bsc_eps_bfr_exi", "re_eps", "basic_eps", "eps",
    )

    revenue = _metric_from_record(current, *revenue_aliases)
    pat = _metric_from_record(current, *pat_aliases)
    eps = _metric_from_record(current, *eps_aliases)
    prev_revenue = _metric_from_record(previous, *revenue_aliases)
    prev_pat = _metric_from_record(previous, *pat_aliases)
    yoy_revenue = _metric_from_record(yoy_row, *revenue_aliases)
    yoy_pat = _metric_from_record(yoy_row, *pat_aliases)

    out = {
        "resultsReleased": True,
        "resultReleased": True,
        "resultVerifiedAt": datetime.now(IST).isoformat(),
        "resultSource": "NSE results comparison",
        "resultSourceUrl": "https://www.nseindia.com/companies-listing/corporate-filings-financial-results",
        "resultPeriodEnd": current_date.isoformat(),
        "latestRevenueLakh": revenue,
        "latestPatLakh": pat,
        "reportedEps": eps,
        "revenueQoQ": round2(_pct(revenue, prev_revenue)),
        "patQoQ": round2(_pct(pat, prev_pat)),
        "revenueYoY": round2(_pct(revenue, yoy_revenue)),
        "patYoY": round2(_pct(pat, yoy_pat)),
        "resultDataSource": "NSE official results comparison",
        "resultDataPeriod": current_date.isoformat(),
    }
    bits = [f"NSE quarter ended {current_date.isoformat()} verified"]
    if revenue is not None:
        bits.append(f"income/revenue ₹{revenue/100:.2f} Cr")
    if pat is not None:
        bits.append(f"PAT ₹{pat/100:.2f} Cr")
    out["resultsEvidence"] = "; ".join(bits) + "."
    return out


def _filing_period_end(item):
    if not isinstance(item, dict):
        return None
    return parse_date(
        item.get("toDate")
        or item.get("periodEnd")
        or item.get("periodEnded")
        or item.get("endDate")
    )


def _filing_broadcast_date(item):
    if not isinstance(item, dict):
        return None
    return parse_date(
        item.get("broadCastDate")
        or item.get("broadcastDate")
        or item.get("filingDate")
        or item.get("date")
    )


def fetch_nse_result_enrichment(rows):
    """Fetch result-release proof and quarterly numbers from NSE.

    Important resilience rule: failure of the market-wide financial_results()
    endpoint must NOT prevent per-symbol results_comparison() checks. GitHub
    runners are sometimes blocked/throttled on one NSE endpoint while another
    still works.
    """
    enrichment = {}
    errors = []
    stats = {
        "filings": 0,
        "matched": 0,
        "comparisonAttempts": 0,
        "comparisons": 0,
    }
    if NSE is None:
        return enrichment, ["nse package unavailable for results"], stats

    now = datetime.now(IST).replace(tzinfo=None)
    today = datetime.now(IST).date()
    client = None
    by_symbol = {}

    try:
        client = _nse_client()
        if client is None:
            return enrichment, ["NSE result client could not be created"], stats

        # Best-effort market-wide filing index. Do not abort the whole result
        # layer when this single NSE endpoint is blocked or changes shape.
        try:
            filings = client.financial_results(
                segment="equities",
                period="quarterly",
                from_date=now - timedelta(days=60),
                to_date=now,
            ) or []
            if not isinstance(filings, list):
                filings = []
            stats["filings"] = len(filings)

            for item in filings:
                if not isinstance(item, dict):
                    continue
                symbol = str(
                    item.get("symbol")
                    or item.get("Symbol")
                    or item.get("sm_symbol")
                    or ""
                ).upper().replace(".NS", "").strip()
                if symbol:
                    by_symbol.setdefault(symbol, []).append(item)
        except Exception as exc:
            errors.append(
                f"NSE financial_results index unavailable: {type(exc).__name__}: {exc}; "
                "continuing with per-symbol results_comparison checks"
            )

        for row in rows:
            symbol = clean_symbol(row)
            if not symbol:
                continue

            expected = expected_period_end(row)
            rd = parse_date(row.get("resultDate") or row.get("result_date"))
            due = (
                rd is not None
                and rd <= today
                and (today - rd).days <= 60
            )

            filing_list = by_symbol.get(symbol) or []
            best = None
            if filing_list:
                if expected is not None:
                    ranked = []
                    for item in filing_list:
                        pe = _filing_period_end(item)
                        if pe is not None:
                            ranked.append((abs((pe - expected).days), item))
                    if ranked:
                        distance, candidate = min(ranked, key=lambda x: x[0])
                        if distance <= 45:
                            best = candidate
                if best is None:
                    best = max(
                        filing_list,
                        key=lambda x: _filing_broadcast_date(x) or date.min,
                    )

            base = {}
            if best is not None:
                stats["matched"] += 1
                period_end = _filing_period_end(best) or expected
                broadcast = _filing_broadcast_date(best)
                base.update({
                    "resultsReleased": True,
                    "resultReleased": True,
                    "resultVerifiedAt": datetime.now(IST).isoformat(),
                    "resultSource": "NSE financial results filing",
                    "resultSourceUrl": (
                        best.get("xbrl")
                        or best.get("xbrlLink")
                        or best.get("filePath")
                        or "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
                    ),
                    "resultPeriodEnd": period_end.isoformat() if period_end else None,
                    "resultsEvidence": (
                        "Official NSE quarterly financial-results filing detected"
                        + (f" on {broadcast.isoformat()}" if broadcast else "")
                        + "."
                    ),
                })

            # This is the critical fallback: for every result that is due, ask
            # NSE for the symbol's latest quarterly P&L even when the filing-index
            # request above failed. parse_nse_comparison() only accepts it when
            # the reported period matches the expected quarter, so stale quarters
            # are not falsely marked released.
            if best is not None or due:
                stats["comparisonAttempts"] += 1
                try:
                    comp = client.results_comparison(symbol)
                    parsed = parse_nse_comparison(comp, row)
                    if parsed:
                        base.update(parsed)
                        stats["comparisons"] += 1
                except Exception as exc:
                    errors.append(
                        f"NSE comparison {symbol}: {type(exc).__name__}: {exc}"
                    )

            if base:
                enrichment[symbol] = base

    except Exception as exc:
        errors.append(f"NSE result client failure: {type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            try:
                client.exit()
            except Exception:
                pass

    return enrichment, errors, stats

def _snapshot_table(snapshot):
    block = snapshot.get("results_in_crores") if isinstance(snapshot, dict) else None
    if not isinstance(block, dict):
        return {}, []
    fields = block.get("fields") or []
    data = block.get("data") or []
    if not fields or len(fields) < 2:
        return {}, []
    periods = [str(x) for x in fields[1:]]
    table = {}
    for row in data:
        if not isinstance(row, list) or not row:
            continue
        title = str(row[0]).strip().lower()
        table[title] = row[1:]
    return table, periods


def _snapshot_value(table, period_index, *titles):
    for title in titles:
        values = table.get(title.lower())
        if values and period_index < len(values):
            return num(values[period_index])
    return None


def _period_label(d):
    return d.strftime("%b-%y") if d is not None else None


def _parse_snapshot_period(value):
    """Parse BSE snapshot labels such as Sep-26 / Sep 2026 / 30-Sep-2026."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    for fmt in (
        "%b-%y", "%b %y", "%b-%Y", "%b %Y",
        "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return pd.Timestamp(s).date()
    except Exception:
        return None


def _snapshot_yoy_index(periods, current_index=0):
    if not periods or current_index >= len(periods):
        return None
    current = _parse_snapshot_period(periods[current_index])
    if current is None:
        return None
    target_year = current.year - 1
    target_month = current.month
    for i, label in enumerate(periods):
        if i == current_index:
            continue
        d = _parse_snapshot_period(label)
        if d is not None and d.year == target_year and d.month == target_month:
            return i
    return None


def fetch_bse_result_enrichment(rows, already=None):
    enrichment = {}
    errors = []
    stats = {"lookups": 0, "snapshots": 0, "matched": 0}
    if BSE is None:
        return enrichment, ["bse package unavailable for results"], stats

    already = already or {}
    today = datetime.now(IST).date()
    try:
        with BSE(str(BASE)) as bse:
            for row in rows:
                symbol = clean_symbol(row)
                if not symbol or symbol in already:
                    continue
                rd = parse_date(row.get("resultDate") or row.get("result_date"))
                if rd is None or rd > today or (today - rd).days > 60:
                    continue
                expected = expected_period_end(row)
                try:
                    code = str(
                        row.get("bseCode")
                        or row.get("bse_code")
                        or row.get("scripCode")
                        or row.get("scrip_code")
                        or ""
                    ).strip()
                    if not code:
                        stats["lookups"] += 1
                        try:
                            code = str(bse.getScripCode(symbol)).strip()
                        except Exception:
                            lookup = bse.lookup(str(row.get("name") or symbol))
                            code = str((lookup or {}).get("bse_code") or "").strip()
                    if not code:
                        continue
                    snapshot = bse.resultsSnapshot(code)
                    stats["snapshots"] += 1
                    table, periods = _snapshot_table(snapshot or {})
                    if not periods:
                        continue
                    expected_label = _period_label(expected)
                    latest_label = periods[0]
                    if expected_label and latest_label.lower() != expected_label.lower():
                        continue

                    revenue_cr = _snapshot_value(table, 0, "Revenue", "Total Income", "Net Sales")
                    pat_cr = _snapshot_value(table, 0, "Net Profit", "PAT", "Profit After Tax")
                    eps = _snapshot_value(table, 0, "EPS")
                    prev_revenue_cr = _snapshot_value(table, 1, "Revenue", "Total Income", "Net Sales")
                    prev_pat_cr = _snapshot_value(table, 1, "Net Profit", "PAT", "Profit After Tax")

                    yoy_index = _snapshot_yoy_index(periods, 0)
                    yoy_revenue_cr = (
                        _snapshot_value(table, yoy_index, "Revenue", "Total Income", "Net Sales")
                        if yoy_index is not None else None
                    )
                    yoy_pat_cr = (
                        _snapshot_value(table, yoy_index, "Net Profit", "PAT", "Profit After Tax")
                        if yoy_index is not None else None
                    )

                    base = {
                        "resultsReleased": True,
                        "resultReleased": True,
                        "resultVerifiedAt": datetime.now(IST).isoformat(),
                        "resultSource": "BSE results snapshot",
                        "resultSourceUrl": f"https://www.bseindia.com/stock-share-price/x/{code}/",
                        "bseCode": code,
                        "ticker": f"{code}.BO",
                        "yahooTicker": f"{code}.BO",
                        "resultPeriodEnd": expected.isoformat() if expected else latest_label,
                        "latestRevenueLakh": revenue_cr * 100 if revenue_cr is not None else None,
                        "latestPatLakh": pat_cr * 100 if pat_cr is not None else None,
                        "reportedEps": eps,
                        "revenueQoQ": round2(_pct(revenue_cr, prev_revenue_cr)),
                        "patQoQ": round2(_pct(pat_cr, prev_pat_cr)),
                        "revenueYoY": round2(_pct(revenue_cr, yoy_revenue_cr)),
                        "patYoY": round2(_pct(pat_cr, yoy_pat_cr)),
                        "resultDataSource": "BSE official results snapshot",
                        "resultDataPeriod": latest_label,
                        "resultsEvidence": f"BSE results snapshot updated for {latest_label}"
                        + (f"; Revenue ₹{revenue_cr:.2f} Cr" if revenue_cr is not None else "")
                        + (f"; PAT ₹{pat_cr:.2f} Cr" if pat_cr is not None else "") + ".",
                    }
                    enrichment[symbol] = base
                    stats["matched"] += 1
                except Exception as exc:
                    errors.append(f"BSE result {symbol}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        errors.append(f"BSE result layer: {type(exc).__name__}: {exc}")
    return enrichment, errors, stats


def _statement_row_local(df, names):
    if df is None or getattr(df, "empty", True):
        return None
    normalized = {str(index).strip().lower(): index for index in df.index}
    for name in names:
        if name.lower() in normalized:
            return normalized[name.lower()]
    for low, original in normalized.items():
        if any(name.lower() in low for name in names):
            return original
    return None


def fetch_yfinance_quarterly_enrichment(row):
    """Last-resort verification when exchange endpoints are unavailable."""
    expected = expected_period_end(row)
    if expected is None:
        return {}
    symbol = clean_symbol(row)
    candidates = []
    explicit = str(row.get("ticker") or "").strip().upper()
    if explicit:
        candidates.append(explicit)
    bse_code = str(row.get("bseCode") or row.get("bse_code") or "").strip()
    if bse_code.isdigit() and len(bse_code) == 6:
        candidates.append(f"{bse_code}.BO")
    if symbol:
        candidates.append(f"{symbol}.NS")
    candidates = list(dict.fromkeys(candidates))

    for ticker_symbol in candidates:
        try:
            ticker = yf.Ticker(ticker_symbol)
            stmt = ticker.quarterly_income_stmt
            if stmt is None or stmt.empty:
                stmt = ticker.quarterly_financials
            if stmt is None or stmt.empty:
                continue
            cols = []
            for col in stmt.columns:
                try:
                    d = pd.Timestamp(col).date()
                    cols.append((d, col))
                except Exception:
                    pass
            if not cols:
                continue
            cols.sort(reverse=True)
            current_date, current_col = min(cols, key=lambda x: abs((x[0] - expected).days))
            if abs((current_date - expected).days) > 45:
                continue

            rev_row = _statement_row_local(stmt, ["Total Revenue", "Operating Revenue", "Revenue", "Total Operating Income"])
            pat_row = _statement_row_local(stmt, ["Net Income", "Net Income Common Stockholders", "Profit After Tax"])
            if rev_row is None and pat_row is None:
                continue

            revenue = num(stmt.loc[rev_row, current_col]) if rev_row is not None else None
            pat = num(stmt.loc[pat_row, current_col]) if pat_row is not None else None

            older = [(d, c) for d, c in cols if d < current_date]
            prev_col = older[0][1] if older else None
            prev_revenue = num(stmt.loc[rev_row, prev_col]) if rev_row is not None and prev_col is not None else None
            prev_pat = num(stmt.loc[pat_row, prev_col]) if pat_row is not None and prev_col is not None else None

            yoy_target = date(current_date.year - 1, current_date.month, min(current_date.day, 28))
            yoy_candidates = [(abs((d - yoy_target).days), c) for d, c in cols if d < current_date]
            yoy_col = None
            if yoy_candidates:
                dist, yoy_col = min(yoy_candidates, key=lambda x: x[0])
                if dist > 50:
                    yoy_col = None
            yoy_revenue = num(stmt.loc[rev_row, yoy_col]) if rev_row is not None and yoy_col is not None else None
            yoy_pat = num(stmt.loc[pat_row, yoy_col]) if pat_row is not None and yoy_col is not None else None

            return {
                "resultsReleased": True,
                "resultReleased": True,
                "resultVerifiedAt": datetime.now(IST).isoformat(),
                "resultSource": "Yahoo Finance quarterly statement fallback",
                "resultPeriodEnd": current_date.isoformat(),
                "latestRevenueLakh": revenue / 100000 if revenue is not None else None,
                "latestPatLakh": pat / 100000 if pat is not None else None,
                "revenueQoQ": round2(_pct(revenue, prev_revenue)),
                "patQoQ": round2(_pct(pat, prev_pat)),
                "revenueYoY": round2(_pct(revenue, yoy_revenue)),
                "patYoY": round2(_pct(pat, yoy_pat)),
                "resultDataSource": "yfinance quarterly statement fallback",
                "resultDataPeriod": current_date.isoformat(),
                "resultsEvidence": f"Quarterly statement for {current_date.isoformat()} verified via Yahoo Finance fallback.",
                "ticker": ticker_symbol,
            }
        except Exception:
            continue
    return {}


def build_result_enrichment(rows):
    nse_map, nse_errors, nse_stats = fetch_nse_result_enrichment(rows)
    bse_map, bse_errors, bse_stats = fetch_bse_result_enrichment(rows, already=nse_map)
    merged = dict(nse_map)
    merged.update(bse_map)

    today = datetime.now(IST).date()
    yf_count = 0
    yf_supplemented = 0
    for row in rows:
        symbol = clean_symbol(row)
        if not symbol:
            continue
        rd = parse_date(row.get("resultDate") or row.get("result_date"))
        if rd is None or rd > today or (today - rd).days > 60:
            continue

        existing = merged.get(symbol)
        needs_numbers = (
            existing is None
            or existing.get("revenueYoY") is None
            or existing.get("patYoY") is None
        )
        if not needs_numbers:
            continue

        data = fetch_yfinance_quarterly_enrichment({**row, **(existing or {})})
        if not data:
            continue

        if existing is None:
            merged[symbol] = data
            yf_count += 1
        else:
            # Preserve official NSE/BSE release proof and use Yahoo only to fill
            # numeric fields that the exchange snapshot does not provide.
            for key in (
                "latestRevenueLakh", "latestPatLakh", "reportedEps",
                "revenueQoQ", "patQoQ", "revenueYoY", "patYoY",
            ):
                if existing.get(key) is None and data.get(key) is not None:
                    existing[key] = data[key]
            existing["numericFallbackSource"] = data.get("resultDataSource")
            yf_supplemented += 1

    stats = {
        "nse": nse_stats,
        "bse": bse_stats,
        "yfinanceFallbackMatched": yf_count,
        "yfinanceSupplemented": yf_supplemented,
        "totalMatched": len(merged),
    }
    return merged, nse_errors + bse_errors, stats

def build_checks(row):
    market_cap = num(row.get("marketCapCr"))

    return [
        {"label": "Results released", "value": row.get("resultsReleased"), "note": row.get("resultsEvidence", "")},
        {
            "label": "Market cap > ₹1,000 Cr",
            "value": row.get("marketCapPass"),
            "note": f"₹{market_cap:,.0f} Cr" if market_cap is not None else "Unverified",
        },
        {"label": "Earnings acceleration", "value": row.get("earningsAccelerationPass"), "note": row.get("earningsEvidence", "")},
        {"label": "Earnings quality", "value": row.get("earningsQualityPass"), "note": row.get("qualityEvidence", "")},
        {"label": "Cash flow", "value": row.get("cashFlowPass"), "note": row.get("cashFlowEvidence", "")},
        {"label": "Surprise", "value": row.get("surprisePass"), "note": row.get("surpriseEvidence", "")},
        {
            "label": "Post-result price/volume confirmation",
            "value": row.get("priceVolumePass"),
            "note": row.get("priceVolumeEvidence", ""),
        },
        {"label": "Liquidity", "value": row.get("liquidityPass"), "note": row.get("liquidityEvidence", "")},
    ]


def qualify(row, stock_history, sector_history):
    out = dict(row)
    today = datetime.now(IST).date()
    result_date = parse_date(out.get("resultDate") or out.get("result_date"))

    results_released, release_evidence = result_release_detection(out)

    out["resultsReleased"] = results_released
    out["resultReleased"] = results_released
    out["resultsEvidence"] = release_evidence

    out.update(technicals(stock_history))
    out.update(result_metrics(stock_history, result_date))

    revenue_yoy = num(out.get("revenueYoY"))
    pat_yoy = num(out.get("patYoY"))
    pat_qoq = num(out.get("patQoQ"))

    if not results_released or revenue_yoy is None or pat_yoy is None:
        earnings_pass = None
    else:
        earnings_pass = (
            revenue_yoy >= REV_YOY_MIN
            and pat_yoy >= PAT_YOY_MIN
            and (pat_qoq is None or pat_qoq >= PAT_QOQ_FLOOR)
        )

    out["earningsAccelerationPass"] = earnings_pass
    out["revenuePatPass"] = earnings_pass
    out["earningsEvidence"] = (
        f"Revenue YoY {revenue_yoy:.1f}%, PAT YoY {pat_yoy:.1f}%"
        + (f", PAT QoQ {pat_qoq:.1f}%" if pat_qoq is not None else "")
        if revenue_yoy is not None and pat_yoy is not None
        else "Growth data unavailable"
    )

    latest_revenue = num(out.get("latestRevenueLakh"))
    latest_pat = num(out.get("latestPatLakh"))
    margin = (
        latest_pat / latest_revenue * 100
        if latest_revenue not in (None, 0) and latest_pat is not None
        else None
    )

    if not results_released:
        quality_pass = None
    elif latest_revenue is not None and latest_pat is not None:
        quality_pass = latest_revenue > 0 and latest_pat > 0 and (margin is None or margin >= 3)
    else:
        quality_pass = bool_value(out.get("earningsQualityPass"))

    out["earningsQualityPass"] = quality_pass
    out["netMarginPct"] = round2(margin)
    out["qualityEvidence"] = (
        f"PAT positive; net margin {margin:.1f}%"
        if margin is not None
        else ("Positive revenue/PAT required" if results_released else "Awaiting results")
    )

    sector_pass, sector_data = sector_tailwind(sector_history)
    out["sectorTailwind"] = sector_pass
    out.update(sector_data)

    turnover = num(out.get("avgTurnover20dCr"))
    out["liquidityPass"] = turnover >= LIQUIDITY_TURNOVER_CR_MIN if turnover is not None else None
    out["liquidityEvidence"] = (
        f"20d avg traded value ₹{turnover:.1f} Cr"
        if turnover is not None
        else "20d turnover unavailable"
    )

    if not results_released:
        out["cashFlowPass"] = None
        out["cashFlowEvidence"] = "Awaiting results"
        out["surprisePass"] = None
        out["surprisePct"] = None
        out["surpriseEvidence"] = "Awaiting results"
        out["priceVolumePass"] = None
        out["priceVolumeEvidence"] = "Awaiting post-result confirmation"
    else:
        ticker_symbol = yahoo_ticker(out)
        ticker = yf.Ticker(ticker_symbol) if ticker_symbol else None

        if ticker is not None:
            cash_pass, cash_data = cashflow_gate(ticker, out)
        else:
            cash_pass, cash_data = None, {
                "cashFlowEvidence": "Ticker unavailable",
                "cashFlowMethod": "unavailable",
            }

        out["cashFlowPass"] = cash_pass
        out.update(cash_data)

        surprise = eps_surprise(ticker, result_date) if ticker is not None else None
        result_return = num(out.get("resultDayReturnPct"))
        rvol = num(out.get("relativeVolume"))

        if surprise is not None:
            surprise_pass = surprise >= EPS_SURPRISE_MIN
            surprise_text = f"Reported EPS surprise {surprise:.1f}%"
            surprise_method = "reported EPS surprise"
        elif result_return is not None:
            surprise_pass = (
                result_return >= MARKET_SURPRISE_PROXY_MIN
                and (rvol is None or rvol >= RVOL_MIN)
            )
            surprise_text = (
                f"Market-reaction proxy: result-day {result_return:.1f}%"
                + (f", RVOL {rvol:.2f}x" if rvol is not None else "")
            )
            surprise_method = "market-reaction proxy"
        else:
            surprise_pass = None
            surprise_text = "EPS surprise and result-day reaction unavailable"
            surprise_method = "unavailable"

        out["surprisePass"] = surprise_pass
        out["surprisePct"] = round2(surprise)
        out["surpriseMethod"] = surprise_method
        out["surpriseEvidence"] = surprise_text

        last_close = num(out.get("lastClose")) or num(out.get("price"))
        ma10 = num(out.get("ma10"))

        if None in (last_close, ma10, result_return, rvol):
            price_volume_pass = None
        else:
            price_volume_pass = (
                last_close > ma10
                and rvol >= RVOL_MIN
                and result_return >= 2
            )

        out["priceVolumePass"] = price_volume_pass
        out["priceVolumeEvidence"] = (
            f"Close ₹{last_close:.2f} vs EMA10 ₹{ma10:.2f}; RVOL {rvol:.2f}x; result-day {result_return:.1f}%"
            if None not in (last_close, ma10, rvol, result_return)
            else "Post-result evidence incomplete"
        )

    checklist = build_checks(out)
    values = [item["value"] for item in checklist]

    out["checks"] = checklist
    out["score"] = sum(value is True for value in values)
    out["knownChecks"] = sum(value is not None for value in values)
    out["scoreText"] = f'{out["score"]}/8'

    all_eight = all(value is True for value in values)
    priced_in = out.get("pricedIn") is True

    if priced_in:
        bucket = "Caution"
    elif not results_released:
        bucket = "Upcoming"
    elif all_eight:
        bucket = "Qualified"
    else:
        bucket = "Post-results"

    out["bucket"] = bucket
    out["peadStatus"] = bucket
    out["stage"] = bucket
    out["entry"] = None
    out["sl"] = None
    out["tsl"] = None
    out["entryTriggerPass"] = None
    out["candidateStatus"] = None
    out["allocationPct"] = None

    if bucket == "Qualified":
        highs = [
            value
            for value in (
                num(out.get("resultDayHigh")),
                num(out.get("preResult20dHigh")),
                num(stock_history["High"].tail(5).max())
                if not stock_history.empty and "High" in stock_history
                else None,
            )
            if value is not None
        ]

        if highs:
            entry = max(highs) * 1.002
            out["entry"] = round2(entry)
            current = num(out.get("price")) or num(out.get("lastClose"))
            out["entryTriggerPass"] = current is not None and current >= entry

        lows = [
            value
            for value in (
                num(out.get("resultDayLow")),
                num(out.get("ma20")),
            )
            if value is not None
        ]

        if lows:
            out["sl"] = round2(min(lows) * 0.995)

        out["tsl"] = round2(out.get("ma10"))
        out["candidateStatus"] = (
            "Entry Triggered" if out.get("entryTriggerPass") else "Potential Candidate"
        )

        rvol = num(out.get("relativeVolume"))
        if (
            out.get("entryTriggerPass")
            and out.get("sectorTailwind") is True
            and rvol is not None
            and rvol >= 2
        ):
            out["allocationPct"] = 30
        elif out.get("entryTriggerPass"):
            out["allocationPct"] = 20
        else:
            out["allocationPct"] = 10

    out["qualificationVersion"] = "pead-v1.1-nse-mcp"
    return out


def main():
    warnings.filterwarnings("ignore")
    payload, rows = load_data()

    print(f"Fetching official NSE/BSE result layer for {len(rows)} stocks...")
    result_layer, result_errors, result_stats = build_result_enrichment(rows)
    print("Result layer matched:", len(result_layer), "| stats:", result_stats)

    print(f"Requesting official NSE MCP market layer for {len(rows)} stocks...")
    mcp_layer = asyncio.run(fetch_nse_market_layer(rows, lookback_days=430, concurrency=4))

    mcp_quotes = mcp_layer.get("quotes") or {}
    mcp_histories = mcp_layer.get("histories") or {}

    missing_stock_tickers = []
    for row in rows:
        s = clean_symbol(row)
        if s and s not in mcp_histories:
            yt = yahoo_ticker(row)
            if yt:
                missing_stock_tickers.append(yt)

    proxies = sorted({sector_proxy(row) for row in rows})
    fallback_tickers = sorted(set(missing_stock_tickers + proxies))

    print(
        f"NSE MCP quotes: {len(mcp_quotes)}/{len(rows)} | "
        f"NSE MCP histories: {len(mcp_histories)}/{len(rows)} | "
        f"fallback tickers: {len(fallback_tickers)}"
    )

    fallback_history = pd.DataFrame()
    if fallback_tickers:
        fallback_history = yf.download(
            tickers=fallback_tickers,
            period="14mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
            timeout=20,
        )

    output = []
    counts = {
        "Upcoming": 0,
        "Post-results": 0,
        "Caution": 0,
        "Qualified": 0,
    }

    for index, row in enumerate(rows, 1):
        out = dict(row)
        s = clean_symbol(out)
        print(f"[{index}/{len(rows)}] {s or 'UNKNOWN'}")

        if s in result_layer:
            out.update(result_layer[s])

        quote = mcp_quotes.get(s)
        if quote:
            if quote.get("price") is not None:
                out["price"] = quote["price"]
                out["lastPrice"] = quote["price"]
            if quote.get("previousClose") is not None:
                out["previousClose"] = quote["previousClose"]
            if quote.get("changePct") is not None:
                out["changePct"] = quote["changePct"]
            if quote.get("priceTimestamp"):
                out["priceTimestamp"] = quote["priceTimestamp"]
            out["priceSource"] = "NSE MCP CM Market"
        else:
            out["priceSource"] = out.get("priceSource") or "NSE live-discovery fallback"

        if s in mcp_histories:
            stock_history = records_to_df(mcp_histories[s])
            out["historySource"] = "NSE MCP Bhavcopy"
        else:
            yt = yahoo_ticker(out)
            stock_history = history_for_yf(fallback_history, yt)
            out["historySource"] = "yfinance fallback"

        sector_history = history_for_yf(fallback_history, sector_proxy(out))
        out["sectorHistorySource"] = "yfinance index fallback"

        try:
            qualified = qualify(out, stock_history, sector_history)
        except Exception as exc:
            qualified = dict(out)
            qualified["qualificationError"] = f"{type(exc).__name__}: {exc}"
            qualified["bucket"] = (
                "Upcoming"
                if str(out.get("bucket", "")).lower() == "upcoming"
                else "Post-results"
            )
            qualified["peadStatus"] = qualified["bucket"]
            qualified["stage"] = qualified["bucket"]

        output.append(qualified)
        bucket = qualified.get("bucket", "Post-results")
        counts[bucket] = counts.get(bucket, 0) + 1

        if qualified.get("resultsReleased"):
            time.sleep(0.10)

    payload["stocks"] = output
    payload["companies"] = output
    payload["qualificationVersion"] = "pead-v1.1-nse-mcp"
    payload["marketDataMode"] = "nse-mcp-primary"
    payload["nseMcp"] = mcp_layer.get("meta") or {}
    payload["nseMcpErrors"] = mcp_layer.get("errors") or []
    payload["bucketCounts"] = counts
    payload["qualifiedCount"] = counts.get("Qualified", 0)
    payload["cautionCount"] = counts.get("Caution", 0)
    payload["postResultCount"] = counts.get("Post-results", 0)
    payload["upcomingCount"] = counts.get("Upcoming", 0)
    payload["resultDataVersion"] = "exchange-results-v2-nse-bse-yf"
    payload["resultSourceStats"] = result_stats
    payload["resultSourceErrors"] = result_errors
    payload["qualificationRules"] = {
        "revenueYoYMin": REV_YOY_MIN,
        "patYoYMin": PAT_YOY_MIN,
        "patQoQFloor": PAT_QOQ_FLOOR,
        "pricedInRunupPct": PRICED_IN_RUNUP_PCT,
        "relativeVolumeMin": RVOL_MIN,
        "liquidity20dTurnoverCrMin": LIQUIDITY_TURNOVER_CR_MIN,
        "epsSurpriseMin": EPS_SURPRISE_MIN,
        "marketSurpriseProxyMin": MARKET_SURPRISE_PROXY_MIN,
    }

    temp = DATA.with_suffix(".json.qualify.tmp")
    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(DATA)

    print("PEAD QUALIFICATION COMPLETE")
    print(json.dumps(counts, indent=2))
    print("NSE MCP metadata:")
    print(json.dumps(payload["nseMcp"], indent=2))
    if payload["nseMcpErrors"]:
        print("NSE MCP warnings:")
        for error in payload["nseMcpErrors"][:20]:
            print(" -", error)
    if payload["resultSourceErrors"]:
        print("Result-source warnings:")
        for error in payload["resultSourceErrors"][:20]:
            print(" -", error)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"QUALIFIER FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
