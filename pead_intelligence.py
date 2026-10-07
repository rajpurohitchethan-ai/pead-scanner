#!/usr/bin/env python3
"""
PEAD Intelligence Lab — read-only add-on.

Reads:  data.json
Writes: the path passed with --output (normally /tmp/intelligence.new.json)

Safety:
- Never modifies data.json.
- Preserves every base row even if enrichment fails for one symbol.
- Refuses to write 0 rows or a row count different from the base universe.
- Missing evidence stays UNVERIFIED; nothing is invented.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from public_sources import concall_for_row, screeningmantis_for_row, stockscans_for_row

IST = ZoneInfo("Asia/Kolkata")
PRICED_IN_RUNUP_PCT = 15.0
RVOL_CONFIRM = 1.20
RESULT_DATE_RELEASE_LOOKBACK_DAYS = 45
RESULT_DAY_AUTO_RELEASE_HOUR_IST = 18

RESULT_POINTS = {
    "GENUINE": 25,
    "MIXED": 14,
    "LOW QUALITY": 4,
    "AWAITING RESULT": 0,
    "UNVERIFIED": 0,
}
EXPECTATION_POINTS = {
    "LOW EXPECTATIONS": 15,
    "PARTLY PRICED": 8,
    "PRICED IN": 0,
    "AWAITING PRICE HISTORY": 4,
    "UNVERIFIED": 0,
}
VALUATION_POINTS = {
    "ATTRACTIVE": 10,
    "FAIR": 7,
    "EXPENSIVE BUT JUSTIFIED": 4,
    "EXCESSIVE": 0,
    "UNVERIFIED": 3,
}


def num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "").replace("%", "").strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def bval(v: Any) -> bool | None:
    if v is True or v is False:
        return v
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in {"true", "yes", "pass", "passed", "qualified", "satisfied", "ok", "green"}:
        return True
    if s in {"false", "no", "fail", "failed", "not satisfied", "red"}:
        return False
    return None


def pick(obj: dict, *keys: str):
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def parse_date(v: Any):
    if not v:
        return None
    try:
        return pd.Timestamp(v).date()
    except Exception:
        return None


def clean_symbol(row: dict) -> str:
    return (
        str(pick(row, "symbol", "sym", "ticker", "code") or "")
        .upper()
        .replace(".NS", "")
        .replace(".BO", "")
        .strip()
    )


def extract_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("stocks", "companies", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    nested = payload.get("data")
    if isinstance(nested, dict):
        for key in ("stocks", "companies"):
            val = nested.get(key)
            if isinstance(val, list):
                return val
    return []


def result_date(row: dict):
    return parse_date(pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"))


def expected_result_period_end(row: dict):
    """Return the calendar quarter-end most likely reported on resultDate."""
    rd = result_date(row)
    if rd is None:
        return None

    candidates = [
        datetime(rd.year - 1, 12, 31).date(),
        datetime(rd.year, 3, 31).date(),
        datetime(rd.year, 6, 30).date(),
        datetime(rd.year, 9, 30).date(),
        datetime(rd.year, 12, 31).date(),
    ]
    prior = [d for d in candidates if d < rd]
    return max(prior) if prior else None


def result_source_blob(row: dict) -> str:
    values = [
        pick(row, "discoverySource", "resultSource", "source"),
        pick(row, "resultsEvidence", "resultEvidence", "evidence"),
        pick(
            row,
            "resultSourceUrl", "filingUrl", "announcementUrl",
            "sourceUrl", "evidenceUrl", "resultsUrl",
        ),
    ]
    return " ".join(str(v) for v in values if v not in (None, "")).lower()


def release_detection(row: dict) -> dict:
    """Strictly detect whether the current tracked result is released.

    Result dates and board-meeting calendars are scheduling evidence only. A row
    becomes post-result only when the upstream scanner/result layer has explicit
    release proof or a verified filing/snapshot source.
    """
    rd = result_date(row)
    explicit = bval(pick(row, "resultsReleased", "resultReleased", "results_declared"))
    source = result_source_blob(row)
    status = str(pick(row, "bucket", "peadStatus", "stage", "status") or "").lower()

    postponement_tokens = (
        "postponed", "rescheduled", "deferred", "cancelled", "canceled",
        "date changed", "board meeting postponed",
    )
    if any(token in f"{status} {source}" for token in postponement_tokens):
        return {
            "released": False,
            "method": "POSTPONED_OR_RESCHEDULED",
            "confidence": "HIGH",
            "reason": "Result appears postponed/rescheduled; waiting for a confirmed filing.",
            "resultDate": str(rd) if rd else None,
        }

    # The qualified data feed already contains the scanner/result-layer verdict.
    # Respect it before looking at descriptive source text such as
    # 'NSE board meeting - financial results'.
    if explicit is True:
        return {
            "released": True,
            "method": "EXPLICIT_VERIFIED_RELEASE",
            "confidence": "HIGH",
            "reason": str(pick(row, "resultsEvidence", "resultEvidence") or "Base feed explicitly verifies the result release."),
            "resultDate": str(rd) if rd else None,
        }
    if explicit is False:
        return {
            "released": False,
            "method": "EXPLICIT_PENDING",
            "confidence": "HIGH",
            "reason": str(pick(row, "resultsEvidence", "resultEvidence") or (
                f"Scheduled result date {rd} is not release proof; waiting for an actual filing."
                if rd else "Base feed marks the result as pending."
            )),
            "resultDate": str(rd) if rd else None,
        }

    upcoming_tokens = (
        "board meeting", "result calendar", "scheduled", "upcoming",
        "awaiting result", "awaiting results", "pre-result", "pre result",
    )
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
        return {
            "released": True,
            "method": "VERIFIED_SOURCE_EVIDENCE",
            "confidence": "HIGH",
            "reason": "Verified result-release source detected.",
            "resultDate": str(rd) if rd else None,
        }

    if rd is not None:
        today = datetime.now(IST).date()
        if rd > today:
            reason = f"Scheduled result date is still in the future: {rd}."
            method = "FUTURE_RESULT_DATE"
        else:
            reason = f"Scheduled result date {rd} has arrived/passed, but no verified result filing was found."
            method = "DATE_PASSED_NO_FILING"
        return {
            "released": False,
            "method": method,
            "confidence": "HIGH",
            "reason": reason,
            "resultDate": str(rd),
        }

    return {
        "released": False,
        "method": "NO_RELEASE_EVIDENCE",
        "confidence": "HIGH",
        "reason": "No verified current result-release evidence is available.",
        "resultDate": None,
    }



def results_released(row: dict) -> bool:
    """Compatibility helper used throughout the intelligence engine."""
    return release_detection(row).get("released") is True


def yahoo_ticker_candidates(row: dict) -> list[str]:
    """Return best-effort Yahoo tickers, preferring explicit/BSE identifiers."""
    candidates: list[str] = []

    explicit = str(
        pick(row, "ticker", "yahooTicker", "yahoo_symbol") or ""
    ).strip().upper()
    if explicit:
        if explicit.endswith((".NS", ".BO")):
            candidates.append(explicit)
        else:
            candidates.append(f"{explicit}.NS")

    bse_code = str(
        pick(row, "bseCode", "bse_code", "scripCode", "scrip_code") or ""
    ).strip()
    if bse_code.isdigit() and len(bse_code) == 6:
        candidates.append(f"{bse_code}.BO")

    symbol = clean_symbol(row)
    if symbol:
        candidates.append(f"{symbol}.NS")

    return list(dict.fromkeys(x for x in candidates if x))


def history_for_row(row: dict) -> pd.DataFrame:
    """Fetch history using the best available NSE/BSE Yahoo ticker."""
    for ticker in yahoo_ticker_candidates(row):
        try:
            h = yf.download(
                ticker,
                period="14mo",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
                timeout=15,
            )
            if h is None or h.empty:
                continue
            if isinstance(h.columns, pd.MultiIndex):
                try:
                    h = h.xs(ticker, axis=1, level=1)
                except Exception:
                    h.columns = h.columns.get_level_values(0)
            if "Close" in h:
                h = h[h["Close"].notna()]
            h = h.dropna(how="all")
            if not h.empty:
                h.attrs["ticker"] = ticker
                return h
        except Exception:
            continue
    return pd.DataFrame()


def _apply_public_price_fallback(row: dict, out: dict) -> dict:
    """Fill missing technical/reaction fields from public ScreeningMantis.

    E-Day% is used only when the displayed past-earnings date matches the
    tracked result date.  Existing exchange/Yahoo values always win.
    """
    try:
        mantis = screeningmantis_for_row(row)
    except Exception as exc:
        mantis = {"error": f"{type(exc).__name__}: {exc}"}

    out["screeningMantis"] = mantis
    if mantis.get("error"):
        return out

    if out.get("relativeVolume") is None and mantis.get("volumeSpike") is not None:
        out["relativeVolume"] = num(mantis.get("volumeSpike"))
        out["rvolMode"] = "SCREENINGMANTIS_PUBLIC_1D_9D"

    if out.get("distanceFrom52wHighPct") is None and mantis.get("distanceFrom52wHighPct") is not None:
        out["distanceFrom52wHighPct"] = num(mantis.get("distanceFrom52wHighPct"))

    if (
        out.get("resultDayPct") is None
        and mantis.get("resultDateMatched") is True
        and mantis.get("earningsDayPct") is not None
    ):
        out["resultDayPct"] = num(mantis.get("earningsDayPct"))
        out["resultDaySource"] = "ScreeningMantis public E-Day%"

    if out.get("lastClose") is None and mantis.get("price") is not None:
        # This is only a last-resort displayed public price; callers still see
        # the source metadata and exchange/Yahoo values take precedence.
        out["lastClose"] = num(mantis.get("price"))
        out["lastCloseSource"] = "ScreeningMantis public table"

    return out


def price_context(row: dict, h: pd.DataFrame) -> dict:
    out = {
        "pre5dPct": None,
        "pre10dPct": None,
        "pre20dPct": num(row.get("preResultRunupPct")),
        "resultDayPct": num(row.get("resultDayReturnPct")),
        "relativeVolume": num(pick(row, "relativeVolume", "rvol")),
        "rawFullDayRvol": None,
        "rvolMode": "UNVERIFIED",
        "rvolIsPartial": False,
        "rvolAsOf": None,
        "distanceFrom52wHighPct": None,
        "lastClose": num(pick(row, "price", "lastPrice", "ltp")),
        "historyAvailable": False,
    }

    if h.empty or "Close" not in h:
        return _apply_public_price_fallback(row, out)

    close = pd.to_numeric(h["Close"], errors="coerce").dropna()
    if close.empty:
        return _apply_public_price_fallback(row, out)

    out["historyAvailable"] = True
    out["lastClose"] = float(close.iloc[-1])

    high = pd.to_numeric(h["High"], errors="coerce").dropna() if "High" in h else close
    if not high.empty:
        high52 = float(high.tail(252).max())
        if high52 > 0:
            out["distanceFrom52wHighPct"] = (out["lastClose"] / high52 - 1.0) * 100.0

    rd = result_date(row)
    end = len(h)
    if rd is not None:
        for i, ts in enumerate(h.index):
            try:
                if pd.Timestamp(ts).date() >= rd:
                    end = i
                    break
            except Exception:
                pass

    pre = h.iloc[:end]
    pre_close = (
        pd.to_numeric(pre["Close"], errors="coerce").dropna()
        if "Close" in pre
        else pd.Series(dtype=float)
    )

    def pre_return(days: int):
        if len(pre_close) <= days:
            return None
        return pct_change(float(pre_close.iloc[-1]), float(pre_close.iloc[-1 - days]))

    out["pre5dPct"] = pre_return(5)
    out["pre10dPct"] = pre_return(10)
    if out["pre20dPct"] is None:
        out["pre20dPct"] = pre_return(20)

    if rd is not None and out["resultDayPct"] is None and 0 < end < len(h):
        out["resultDayPct"] = pct_change(
            num(h["Close"].iloc[end]),
            num(h["Close"].iloc[end - 1]),
        )

    if "Volume" in h and len(h) >= 21:
        vol = pd.to_numeric(h["Volume"], errors="coerce")
        avg_full_day = num(vol.iloc[-21:-1].mean())
        current_volume = num(vol.iloc[-1])

        if avg_full_day not in (None, 0) and current_volume is not None:
            raw_rvol = current_volume / avg_full_day
            out["rawFullDayRvol"] = raw_rvol

            if out["relativeVolume"] is not None:
                out["rvolMode"] = "SOURCE_PROVIDED"
            else:
                now = datetime.now(IST)
                try:
                    last_bar_date = pd.Timestamp(h.index[-1]).date()
                except Exception:
                    last_bar_date = None

                open_min = 9 * 60 + 15
                close_min = 15 * 60 + 30
                now_min = now.hour * 60 + now.minute
                intraday = (
                    last_bar_date == now.date()
                    and now.weekday() < 5
                    and open_min <= now_min < close_min
                )

                if intraday:
                    elapsed_fraction = max(
                        0.05,
                        min(1.0, (now_min - open_min) / (close_min - open_min)),
                    )
                    expected_so_far = avg_full_day * elapsed_fraction
                    if expected_so_far > 0:
                        out["relativeVolume"] = current_volume / expected_so_far
                        out["rvolMode"] = "INTRADAY_TIME_ADJUSTED_ESTIMATE"
                        out["rvolIsPartial"] = True
                        out["rvolAsOf"] = now.isoformat()
                else:
                    out["relativeVolume"] = raw_rvol
                    out["rvolMode"] = "FULL_DAY"

    return _apply_public_price_fallback(row, out)


def expectation_reality(row: dict, pc: dict) -> dict:
    run20 = num(pc.get("pre20dPct"))
    move = num(pc.get("resultDayPct"))
    rvol = num(pc.get("relativeVolume"))
    released = results_released(row)
    reasons, risks = [], []

    if run20 is None:
        label = "AWAITING PRICE HISTORY"
        reasons.append("20-day pre-result move could not be verified.")
    elif run20 > PRICED_IN_RUNUP_PCT:
        label = "PRICED IN"
        risks.append(
            f"20-day pre-result run-up was {run20:.1f}%, above the {PRICED_IN_RUNUP_PCT:.0f}% threshold."
        )
    elif run20 > 5:
        label = "PARTLY PRICED"
        reasons.append(f"Pre-result move was moderate at {run20:.1f}%.")
    else:
        label = "LOW EXPECTATIONS"
        reasons.append(f"Pre-result move was only {run20:.1f}%.")

    if released and move is not None:
        if move >= 3:
            reasons.append(f"Result-day move was +{move:.1f}%.")
        elif move <= -3:
            risks.append(f"Result-day move was {move:.1f}%.")

    if released and rvol is not None:
        suffix = " (intraday time-adjusted estimate)" if pc.get("rvolIsPartial") else ""
        if rvol >= RVOL_CONFIRM:
            reasons.append(f"Post-result relative volume was {rvol:.2f}x{suffix}.")
        else:
            risks.append(f"Post-result relative volume was only {rvol:.2f}x{suffix}.")

    return {"label": label, "reasons": reasons, "risks": risks}


def row_from_statement(df: pd.DataFrame, aliases: list[str]):
    if df is None or getattr(df, "empty", True):
        return None
    norm = {str(i).strip().lower(): i for i in df.index}
    for alias in aliases:
        if alias.lower() in norm:
            return norm[alias.lower()]
    for low, original in norm.items():
        if any(alias.lower() in low for alias in aliases):
            return original
    return None


def statement_series(df: pd.DataFrame, aliases: list[str]) -> list[float]:
    idx = row_from_statement(df, aliases)
    if idx is None:
        return []
    s = pd.to_numeric(df.loc[idx], errors="coerce").dropna()
    return [float(x) for x in s.tolist()]


def latest_statement_date(df: pd.DataFrame):
    if df is None or getattr(df, "empty", True):
        return None
    dates = []
    for col in df.columns:
        try:
            dates.append(pd.Timestamp(col).date())
        except Exception:
            continue
    return max(dates) if dates else None


def fundamentals_current_for_result(row: dict, fs: dict) -> tuple[bool, str]:
    """Guard against scoring an old Yahoo quarter as the newly released result."""
    expected = expected_result_period_end(row)
    latest = parse_date(fs.get("latestIncomeQuarterEnd"))
    detection = release_detection(row)

    row_metrics = any(
        pick(row, key) not in (None, "")
        for key in (
            "revenueYoY", "patYoY", "patQoQ",
            "earningsQualityPass", "cashFlowPass", "surprisePass",
        )
    )
    if row_metrics and detection.get("confidence") == "HIGH":
        return True, "Base feed contains result metrics with high-confidence release evidence."

    if expected is None:
        if latest is not None and detection.get("confidence") == "HIGH":
            return True, f"Latest available reported quarter is {latest}."
        return False, "Result is detected, but its reporting period cannot yet be matched to fresh fundamentals."

    if latest is None:
        return False, f"Expected reported period is about {expected}, but current-quarter Yahoo fundamentals are not available yet."

    distance = abs((latest - expected).days)
    if distance <= 10:
        return True, f"Latest reported quarter ({latest}) matches the expected period ({expected})."

    return False, (
        f"Result is detected, but Yahoo still shows {latest} while the expected reported period is {expected}."
    )


def empty_fundamentals() -> dict:
    return {
        "source": "not requested before result",
        "latestIncomeQuarterEnd": None,
        "latestCashFlowQuarterEnd": None,
        "revenueYoYCalc": None,
        "patYoYCalc": None,
        "operatingMarginNow": None,
        "operatingMarginYoY": None,
        "operatingCashFlow": None,
        "cashFlowToNetIncome": None,
        "otherIncomeToPretaxPct": None,
        "trailingPE": None,
        "forwardPE": None,
        "pegRatio": None,
        "priceToBook": None,
        "enterpriseToEbitda": None,
        "returnOnEquityPct": None,
        "debtToEquity": None,
        "freeCashFlowYieldPct": None,
        "marketCapCr": None,
        "returnOnCapitalEmployedPct": None,
        "publicSources": {},
        "errors": [],
    }


def fundamental_snapshot(row: dict) -> dict:
    out = empty_fundamentals()
    out["source"] = "yfinance best-effort"
    candidates = yahoo_ticker_candidates(row)
    if not candidates:
        return out

    t = None
    chosen_ticker = None
    for candidate in candidates:
        try:
            probe = yf.Ticker(candidate)
            income_probe = probe.quarterly_income_stmt
            if income_probe is not None and not income_probe.empty:
                t = probe
                chosen_ticker = candidate
                break
        except Exception:
            continue

    if t is None:
        chosen_ticker = candidates[0]
        t = yf.Ticker(chosen_ticker)

    out["ticker"] = chosen_ticker

    try:

        try:
            income = t.quarterly_income_stmt
            income_date = latest_statement_date(income)
            out["latestIncomeQuarterEnd"] = str(income_date) if income_date else None
            rev = statement_series(income, ["Total Revenue", "Operating Revenue"])
            pat = statement_series(income, ["Net Income", "Net Income Common Stockholders"])
            opi = statement_series(income, ["Operating Income", "EBIT"])

            if len(rev) >= 5:
                out["revenueYoYCalc"] = pct_change(rev[0], rev[4])
            if len(pat) >= 5:
                out["patYoYCalc"] = pct_change(pat[0], pat[4])
            if rev and opi and rev[0] != 0:
                out["operatingMarginNow"] = opi[0] / rev[0] * 100.0
            if len(rev) >= 5 and len(opi) >= 5 and rev[4] != 0 and out["operatingMarginNow"] is not None:
                out["operatingMarginYoY"] = out["operatingMarginNow"] - opi[4] / rev[4] * 100.0

            pretax = statement_series(income, ["Pretax Income", "Income Before Tax"])
            other = statement_series(
                income,
                ["Other Non Operating Income Expenses", "Other Income Expense", "Other Non Operating Income"],
            )
            if pretax and other and pretax[0] != 0:
                out["otherIncomeToPretaxPct"] = abs(other[0]) / abs(pretax[0]) * 100.0
        except Exception as exc:
            out["errors"].append(f"income:{type(exc).__name__}")

        try:
            cf = t.quarterly_cash_flow
            cashflow_date = latest_statement_date(cf)
            out["latestCashFlowQuarterEnd"] = str(cashflow_date) if cashflow_date else None
            ocf = statement_series(
                cf,
                ["Operating Cash Flow", "Total Cash From Operating Activities", "Cash Flow From Continuing Operating Activities"],
            )
            ni = statement_series(cf, ["Net Income", "Net Income From Continuing Operations"])
            if ocf:
                out["operatingCashFlow"] = ocf[0]
            if ocf and ni and ni[0] != 0:
                out["cashFlowToNetIncome"] = ocf[0] / abs(ni[0])
        except Exception as exc:
            out["errors"].append(f"cashflow:{type(exc).__name__}")

        try:
            info = t.get_info() or {}
            out["trailingPE"] = num(info.get("trailingPE"))
            out["forwardPE"] = num(info.get("forwardPE"))
            out["pegRatio"] = num(info.get("pegRatio"))
            out["priceToBook"] = num(info.get("priceToBook"))
            out["enterpriseToEbitda"] = num(info.get("enterpriseToEbitda"))
            roe = num(info.get("returnOnEquity"))
            out["returnOnEquityPct"] = roe * 100.0 if roe is not None and abs(roe) <= 5 else roe
            out["debtToEquity"] = num(info.get("debtToEquity"))
            fcf = num(info.get("freeCashflow"))
            mcap = num(info.get("marketCap"))
            if fcf is not None and mcap not in (None, 0):
                out["freeCashFlowYieldPct"] = fcf / mcap * 100.0
        except Exception as exc:
            out["errors"].append(f"valuation:{type(exc).__name__}")

    except Exception as exc:
        out["errors"].append(f"ticker:{type(exc).__name__}")

    # Public web fallbacks.  They FILL MISSING values only and never replace
    # stronger yfinance/exchange data.  StockScans quarter metrics are accepted
    # only when its latest quarter matches the tracked result period.
    try:
        stock = stockscans_for_row(row)
    except Exception as exc:
        stock = {"error": f"{type(exc).__name__}: {exc}", "source": "StockScans"}

    try:
        concall = concall_for_row(row)
    except Exception as exc:
        concall = {"error": f"{type(exc).__name__}: {exc}", "source": "Concall.in"}

    out["publicSources"] = {"stockScans": stock, "concall": concall}
    sources_used = [out.get("source") or "yfinance best-effort"]

    if not stock.get("error"):
        quarter_ok = stock.get("periodMatchesExpected") is not False
        if quarter_ok:
            mapping = {
                "latestIncomeQuarterEnd": "latestIncomeQuarterEnd",
                "revenueYoYCalc": "revenueYoY",
                "patYoYCalc": "patYoY",
                "operatingMarginNow": "operatingMarginNow",
            }
            for dst, key in mapping.items():
                if out.get(dst) is None and stock.get(key) is not None:
                    out[dst] = stock.get(key)

        valuation_mapping = {
            "trailingPE": "trailingPE",
            "priceToBook": "priceToBook",
            "enterpriseToEbitda": "enterpriseToEbitda",
            "returnOnEquityPct": "returnOnEquityPct",
            "returnOnCapitalEmployedPct": "returnOnCapitalEmployedPct",
            "marketCapCr": "marketCapCr",
        }
        for dst, key in valuation_mapping.items():
            if out.get(dst) is None and stock.get(key) is not None:
                out[dst] = stock.get(key)
        sources_used.append("StockScans public")

    if not concall.get("error"):
        for dst, key in (
            ("trailingPE", "trailingPE"),
            ("priceToBook", "priceToBook"),
            ("marketCapCr", "marketCapCr"),
        ):
            if out.get(dst) is None and concall.get(key) is not None:
                out[dst] = concall.get(key)
        sources_used.append("Concall.in public")

    out["source"] = " + ".join(dict.fromkeys(x for x in sources_used if x))

    return out


def result_reality(row: dict, fs: dict) -> dict:
    detection = release_detection(row)
    if not detection["released"]:
        return {
            "label": "AWAITING RESULT",
            "reasons": [detection.get("reason") or "Result has not been confirmed as released."],
            "risks": [],
        }

    current_ok, current_note = fundamentals_current_for_result(row, fs)
    if not current_ok:
        return {
            "label": "UNVERIFIED",
            "reasons": [
                detection.get("reason") or "Result release detected.",
                current_note,
            ],
            "risks": [
                "Current-quarter fundamentals are not verified yet, so result quality is not scored from stale numbers."
            ],
        }

    rev = num(pick(row, "revenueYoY"))
    if rev is None:
        rev = num(fs.get("revenueYoYCalc"))
    pat = num(pick(row, "patYoY"))
    if pat is None:
        pat = num(fs.get("patYoYCalc"))

    pat_qoq = num(pick(row, "patQoQ"))
    margin_delta = num(fs.get("operatingMarginYoY"))
    other_ratio = num(fs.get("otherIncomeToPretaxPct"))
    ocf = num(fs.get("operatingCashFlow"))
    cf_ratio = num(fs.get("cashFlowToNetIncome"))
    quality_pass = bval(pick(row, "earningsQualityPass"))
    cash_pass = bval(pick(row, "cashFlowPass"))
    surprise_pass = bval(pick(row, "surprisePass"))

    score = 0
    red = 0
    reasons, risks = [], []

    if rev is not None:
        if rev >= 10:
            score += 2
            reasons.append(f"Revenue YoY growth is {rev:.1f}%.")
        elif rev < 0:
            red += 1
            risks.append(f"Revenue YoY declined {abs(rev):.1f}%.")

    if pat is not None:
        if pat >= 15:
            score += 2
            reasons.append(f"PAT YoY growth is {pat:.1f}%.")
        elif pat < 0:
            red += 1
            risks.append(f"PAT YoY declined {abs(pat):.1f}%.")

    if pat_qoq is not None and pat_qoq >= 0:
        score += 1
        reasons.append(f"PAT QoQ is {pat_qoq:+.1f}%.")

    if margin_delta is not None:
        if margin_delta > 0.5:
            score += 1
            reasons.append(f"Operating margin improved {margin_delta:.1f} percentage points YoY.")
        elif margin_delta < -1.5:
            red += 1
            risks.append(f"Operating margin contracted {abs(margin_delta):.1f} percentage points YoY.")

    if quality_pass is True:
        score += 1
        reasons.append("Base earnings-quality gate passed.")
    elif quality_pass is False:
        red += 1
        risks.append("Base earnings-quality gate failed.")

    if cash_pass is True:
        score += 1
        reasons.append("Cash-flow gate passed.")
    elif cash_pass is False:
        red += 1
        risks.append("Cash-flow gate failed.")
    elif ocf is not None:
        if ocf > 0:
            score += 1
            reasons.append("Operating cash flow is positive.")
        else:
            red += 1
            risks.append("Operating cash flow is negative.")

    if cf_ratio is not None:
        if cf_ratio >= 0.5:
            score += 1
            reasons.append(f"Operating cash flow / net income is {cf_ratio:.2f}x.")
        elif cf_ratio < 0.25:
            risks.append(f"Operating cash flow / net income is weak at {cf_ratio:.2f}x.")

    if other_ratio is not None:
        if other_ratio <= 15:
            score += 1
            reasons.append(f"Other income is only {other_ratio:.1f}% of pretax income.")
        elif other_ratio >= 30:
            red += 1
            risks.append(f"Other income is {other_ratio:.1f}% of pretax income; one-off support needs review.")

    if surprise_pass is True:
        score += 1
        reasons.append("Surprise gate passed.")

    # Do not call a released result "LOW QUALITY" merely because most
    # current-quarter inputs are still missing. Missing evidence is UNVERIFIED,
    # not negative evidence.
    core_evidence_count = sum(x is not None for x in (rev, pat))
    evidence_count = sum(
        x is not None
        for x in (
            rev,
            pat,
            pat_qoq,
            margin_delta,
            quality_pass,
            cash_pass if cash_pass is not None else ocf,
            cf_ratio,
            other_ratio,
            surprise_pass,
        )
    )

    if evidence_count < 4 and red < 2:
        label = "UNVERIFIED"
        risks.append(
            f"Only {evidence_count} current-quarter quality input(s) are verified; "
            "insufficient evidence to label the result weak."
        )
    elif core_evidence_count < 2 and red < 2:
        label = "UNVERIFIED"
        risks.append(
            "Revenue YoY and PAT YoY are not both verified; result quality remains pending."
        )
    elif score >= 6 and red == 0:
        label = "GENUINE"
    elif red >= 2 or score <= 2:
        label = "LOW QUALITY"
    else:
        label = "MIXED"

    return {
        "label": label,
        "reasons": reasons,
        "risks": risks,
        "evidenceCount": evidence_count,
        "coreEvidenceCount": core_evidence_count,
    }


def valuation_reality(row: dict, fs: dict) -> dict:
    pe = num(fs.get("trailingPE"))
    fpe = num(fs.get("forwardPE"))
    peg = num(fs.get("pegRatio"))
    pb = num(fs.get("priceToBook"))
    eve = num(fs.get("enterpriseToEbitda"))
    roe = num(fs.get("returnOnEquityPct"))
    de = num(fs.get("debtToEquity"))
    fcf_yield = num(fs.get("freeCashFlowYieldPct"))
    growth = num(pick(row, "patYoY"))

    if peg is None and pe is not None and growth not in (None, 0) and growth > 0:
        peg = pe / growth

    metrics = {
        "trailingPE": pe,
        "forwardPE": fpe,
        "peg": peg,
        "priceToBook": pb,
        "enterpriseToEbitda": eve,
        "roePct": roe,
        "debtToEquity": de,
        "fcfYieldPct": fcf_yield,
    }

    evidence_count = sum(x is not None for x in (pe, fpe, peg, pb, eve, roe, fcf_yield))
    confidence = "HIGH" if evidence_count >= 5 else ("MEDIUM" if evidence_count >= 3 else "LIMITED")
    reasons, risks = [], []

    if evidence_count < 3:
        return {
            "label": "UNVERIFIED",
            "confidence": confidence,
            "evidenceCount": evidence_count,
            "reasons": [
                f"Only {evidence_count} valuation input(s) available; at least 3 are required before assigning a valuation label."
            ],
            "risks": [],
            "metrics": metrics,
        }

    score = 0
    if peg is not None:
        if peg <= 1.0:
            score += 3
            reasons.append(f"PEG ≈ {peg:.2f}.")
        elif peg <= 1.8:
            score += 2
            reasons.append(f"PEG ≈ {peg:.2f}, broadly reasonable for growth.")
        elif peg >= 2.5:
            score -= 2
            risks.append(f"PEG ≈ {peg:.2f}; valuation is high relative to PAT growth.")

    if pe is not None and fpe is not None:
        if fpe < pe:
            score += 1
            reasons.append(f"Forward P/E {fpe:.1f}x is below trailing P/E {pe:.1f}x.")
        elif fpe > pe * 1.15:
            risks.append(f"Forward P/E {fpe:.1f}x is above trailing P/E {pe:.1f}x.")

    if roe is not None:
        if roe >= 18:
            score += 1
            reasons.append(f"ROE is {roe:.1f}%.")
        elif roe < 10:
            risks.append(f"ROE is only {roe:.1f}%.")

    if fcf_yield is not None:
        if fcf_yield >= 3:
            score += 1
            reasons.append(f"FCF yield is {fcf_yield:.1f}%.")
        elif fcf_yield < 0:
            risks.append("Free cash flow is negative.")

    if de is not None and de > 200:
        score -= 1
        risks.append(f"Debt/equity is elevated at {de:.0f}.")

    if pe is not None and growth is not None and pe > 70 and growth < 20:
        score -= 2
        risks.append(f"P/E is {pe:.1f}x while PAT growth is only {growth:.1f}%.")

    label = (
        "ATTRACTIVE" if score >= 4
        else "FAIR" if score >= 2
        else "EXPENSIVE BUT JUSTIFIED" if score >= 0
        else "EXCESSIVE"
    )
    return {
        "label": label,
        "confidence": confidence,
        "evidenceCount": evidence_count,
        "reasons": reasons,
        "risks": risks,
        "metrics": metrics,
    }


def price_response(row: dict, pc: dict) -> dict:
    if not results_released(row):
        return {"label": "AWAITING RESULT", "points": 0}
    move = num(pc.get("resultDayPct"))
    rvol = num(pc.get("relativeVolume"))
    if move is not None and rvol is not None:
        if move >= 2 and rvol >= RVOL_CONFIRM:
            return {"label": "CONFIRMED", "points": 5}
        if move <= -2:
            return {"label": "NEGATIVE", "points": 0}
        return {"label": "MIXED", "points": 2}
    return {"label": "UNVERIFIED", "points": 1}


def result_verification(row: dict, released: bool) -> dict:
    detection = release_detection(row)
    if not released:
        return {
            "label": "AWAITING RESULT",
            "official": False,
            "source": None,
            "sourceUrl": None,
            "verifiedAt": None,
            "detectionMethod": detection.get("method"),
            "detectionConfidence": detection.get("confidence"),
            "detectionReason": detection.get("reason"),
        }

    source_url = pick(
        row,
        "resultSourceUrl", "filingUrl", "announcementUrl",
        "sourceUrl", "evidenceUrl", "resultsUrl",
    )
    source_text = pick(
        row,
        "resultSource", "discoverySource", "source",
        "resultsEvidence", "resultEvidence", "evidence",
    )
    verified_at = pick(
        row,
        "resultVerifiedAt", "resultDetectedAt", "filingTimestamp",
        "announcementTimestamp", "sourceTimestamp",
    )

    combined = " ".join(
        x for x in (str(source_text or ""), str(source_url or "")) if x
    ).lower()
    official = any(
        token in combined
        for token in (
            "nseindia.com", "bseindia.com", "nse filing", "bse filing",
            "official exchange", "exchange filing",
        )
    )

    if official:
        label = "OFFICIAL EXCHANGE EVIDENCE"
    elif source_url:
        label = "SOURCE LINK PRESENT — VERIFY DOMAIN"
    elif source_text:
        label = "SOURCE TEXT PRESENT — NOT OFFICIAL VERIFIED"
    elif detection.get("method") in {"RECENT_RESULT_DATE_PASSED", "RESULT_DATE_TODAY_EVENING"}:
        label = "AUTO-DETECTED FROM RESULT DATE — SOURCE PENDING"
    else:
        label = "BASE CONFIRMED — SOURCE NOT ATTACHED"

    return {
        "label": label,
        "official": official,
        "source": str(source_text) if source_text else None,
        "sourceUrl": str(source_url) if source_url else None,
        "verifiedAt": verified_at,
        "detectionMethod": detection.get("method"),
        "detectionConfidence": detection.get("confidence"),
        "detectionReason": detection.get("reason"),
    }



def event_key(item: dict) -> str:
    quarter = str(item.get("quarter") or "").strip()
    result_date_value = str(item.get("resultDate") or "").strip()

    if quarter and quarter != "—":
        return f"quarter:{quarter}"

    if result_date_value:
        return f"date:{result_date_value}"

    return "current-unidentified-quarter"


def expectation_gap(item: dict) -> dict:
    """
    Estimate how much positive expectation appears embedded BEFORE the result.

    This is deliberately separate from result quality. A genuine result can still
    be a weak PEAD setup if expectations were already extreme.
    """
    pc = item.get("priceContext") or {}

    pre5 = num(pc.get("pre5dPct"))
    pre10 = num(pc.get("pre10dPct"))
    pre20 = num(pc.get("pre20dPct"))
    high_dist = num(pc.get("distanceFrom52wHighPct"))

    burden = 0
    reasons: list[str] = []
    offsets: list[str] = []

    if pre20 is None:
        reasons.append("20-day pre-result return is unverified.")
    elif pre20 <= 0:
        offsets.append(f"20-day move is subdued at {pre20:+.1f}%.")
    elif pre20 <= 5:
        burden += 10
        offsets.append(f"20-day run-up is modest at {pre20:+.1f}%.")
    elif pre20 <= 10:
        burden += 25
        reasons.append(f"20-day run-up is {pre20:+.1f}%.")
    elif pre20 <= 15:
        burden += 45
        reasons.append(f"20-day run-up is already {pre20:+.1f}%.")
    elif pre20 <= 25:
        burden += 70
        reasons.append(f"20-day run-up is elevated at {pre20:+.1f}%.")
    else:
        burden += 90
        reasons.append(f"20-day run-up is extreme at {pre20:+.1f}%.")

    if pre10 is not None:
        if pre10 >= 10:
            burden += 8
            reasons.append(f"10-day run-up is strong at {pre10:+.1f}%.")
        elif pre10 <= -3:
            burden = max(0, burden - 5)
            offsets.append(f"10-day move is weak at {pre10:+.1f}%.")

    if pre5 is not None:
        if pre5 >= 6:
            burden += 7
            reasons.append(f"5-day acceleration is {pre5:+.1f}%.")
        elif pre5 <= -3:
            burden = max(0, burden - 4)
            offsets.append(f"5-day move is weak at {pre5:+.1f}%.")

    if high_dist is not None:
        if high_dist >= -3:
            burden += 8
            reasons.append("Stock is within 3% of its 52-week high.")
        elif high_dist >= -7:
            burden += 5
            reasons.append("Stock is close to its 52-week high.")
        elif high_dist <= -25:
            burden = max(0, burden - 4)
            offsets.append("Stock is well below its 52-week high.")

    burden = max(0, min(100, int(round(burden))))

    if pre20 is None:
        label = "UNVERIFIED"
        surprise_room = "UNVERIFIED"
    elif burden <= 20:
        label = "LOW"
        surprise_room = "HIGH"
    elif burden <= 45:
        label = "NORMAL"
        surprise_room = "FAIR"
    elif burden <= 70:
        label = "ELEVATED"
        surprise_room = "LOW"
    else:
        label = "EXTREME"
        surprise_room = "VERY LOW"

    return {
        "label": label,
        "burdenScore": burden,
        "surpriseRoom": surprise_room,
        "pre5dPct": pre5,
        "pre10dPct": pre10,
        "pre20dPct": pre20,
        "distanceFrom52wHighPct": high_dist,
        "reasons": reasons[:5],
        "offsets": offsets[:4],
    }


def compact_history_snapshot(
    item: dict,
    gap: dict,
    *,
    first_seen_at: str | None = None,
    seen_at: str | None = None,
) -> dict:
    seen_at = seen_at or datetime.now(IST).isoformat()

    return {
        "eventKey": event_key(item),
        "quarter": item.get("quarter"),
        "resultDate": item.get("resultDate"),
        "firstSeenAt": first_seen_at or seen_at,
        "lastSeenAt": seen_at,
        "resultsReleased": item.get("resultsReleased") is True,
        "resultReality": (item.get("resultReality") or {}).get("label"),
        "expectationReality": (item.get("expectationReality") or {}).get("label"),
        "expectationGap": gap.get("label"),
        "expectationBurdenScore": gap.get("burdenScore"),
        "priceResponse": (item.get("priceResponse") or {}).get("label"),
        "valuationReality": (item.get("valuationReality") or {}).get("label"),
        "baseBucket": item.get("baseBucket"),
        "baseScore": item.get("baseScore"),
        "convictionScore": num(item.get("convictionScore")),
        "verdict": item.get("verdict"),
        "sectorTailwind": item.get("sectorTailwind"),
    }


def previous_quarter_history(previous_payload: dict | None) -> dict[str, list[dict]]:
    """
    Read prior quarter memory from the previous intelligence.json.

    Migration behavior:
    - If quarterHistory already exists, preserve it.
    - If it does not exist yet, seed one snapshot from the previous items so the
      first upgraded run does not lose the previously tracked quarter.
    """
    if not isinstance(previous_payload, dict):
        return {}

    existing = previous_payload.get("quarterHistory")
    if isinstance(existing, dict):
        clean: dict[str, list[dict]] = {}
        for symbol, events in existing.items():
            if isinstance(events, list):
                clean[str(symbol).upper()] = [
                    dict(event) for event in events if isinstance(event, dict)
                ][-12:]
        return clean

    seeded: dict[str, list[dict]] = {}
    previous_generated = previous_payload.get("generatedAt") or datetime.now(IST).isoformat()

    for item in previous_payload.get("items") or []:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            continue

        gap = item.get("expectationGap")
        if not isinstance(gap, dict):
            gap = expectation_gap(item)

        seeded[symbol] = [
            compact_history_snapshot(
                item,
                gap,
                first_seen_at=str(previous_generated),
                seen_at=str(previous_generated),
            )
        ]

    return seeded


def update_quarter_history(
    previous_payload: dict | None,
    items: list[dict],
) -> dict[str, list[dict]]:
    history = previous_quarter_history(previous_payload)
    seen_at = datetime.now(IST).isoformat()

    for item in items:
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            continue

        gap = item.get("expectationGap") or expectation_gap(item)
        events = history.setdefault(symbol, [])
        key = event_key(item)

        existing = next(
            (event for event in events if event.get("eventKey") == key),
            None,
        )

        snapshot = compact_history_snapshot(
            item,
            gap,
            first_seen_at=existing.get("firstSeenAt") if existing else None,
            seen_at=seen_at,
        )

        if existing is None:
            events.append(snapshot)
        else:
            existing.clear()
            existing.update(snapshot)

        # Bound persistent memory to the most recent 12 tracked quarters/events.
        if len(events) > 12:
            history[symbol] = events[-12:]

    return history


def quarter_memory(item: dict, history: dict[str, list[dict]]) -> dict:
    symbol = str(item.get("symbol") or "").upper().strip()
    events = list(history.get(symbol) or [])

    events.sort(
        key=lambda e: (
            str(e.get("firstSeenAt") or ""),
            str(e.get("resultDate") or ""),
            str(e.get("eventKey") or ""),
        )
    )

    key = event_key(item)
    current_index = next(
        (i for i, event in enumerate(events) if event.get("eventKey") == key),
        None,
    )

    current = (
        events[current_index]
        if current_index is not None
        else (events[-1] if events else None)
    )

    prior = (
        events[:current_index]
        if current_index is not None
        else events[:-1]
    )

    previous = prior[-1] if prior else None
    status = "FIRST OBSERVATION"
    reasons: list[str] = []

    if previous and current:
        prev_rr = str(previous.get("resultReality") or "")
        curr_rr = str(current.get("resultReality") or "")
        prev_verdict = str(previous.get("verdict") or "")
        curr_verdict = str(current.get("verdict") or "")

        if current.get("resultsReleased") is not True:
            status = "TRACKING NEXT QUARTER"
            reasons.append("A prior tracked quarter exists; current result is still pending.")
        elif prev_rr in {"LOW QUALITY", "MIXED"} and curr_rr == "GENUINE":
            status = "SECOND-CHANCE CONFIRMATION"
            reasons.append(
                "A previously weak/mixed tracked quarter is now followed by a genuine result."
            )
        elif prev_rr == "GENUINE" and curr_rr == "GENUINE":
            status = "CONFIRMED AGAIN"
            reasons.append("Two consecutive tracked quarters are classified as genuine.")
        elif prev_rr == "GENUINE" and curr_rr == "LOW QUALITY":
            status = "EXECUTION BROKE"
            reasons.append(
                "Current result quality deteriorated after a previously genuine quarter."
            )
        elif (
            "HIGH-CONVICTION" in prev_verdict
            and "HIGH-CONVICTION" in curr_verdict
        ):
            status = "REPEATED HIGH CONVICTION"
            reasons.append("High-conviction PEAD status persisted across tracked quarters.")
        else:
            status = "TRACKING"
            reasons.append(
                "Quarter-to-quarter evidence exists but has not formed a stronger pattern yet."
            )
    elif previous:
        status = "TRACKING"
        reasons.append("A prior tracked quarter exists.")

    return {
        "status": status,
        "trackedQuarterCount": len(events),
        "previous": previous,
        "current": current,
        "recentEvents": events[-4:],
        "reasons": reasons,
    }


def jcurve_phase(item: dict, memory: dict) -> dict:
    released = item.get("resultsReleased") is True
    rr = str((item.get("resultReality") or {}).get("label") or "")
    pr = str((item.get("priceResponse") or {}).get("label") or "")
    memory_status = str(memory.get("status") or "")

    if not released:
        label = "PRE-RESULT EXPECTATION SETUP"
        note = "Wait for the result; judge how much expectation is already embedded."
    elif rr == "LOW QUALITY":
        label = "THESIS REVIEW / BROKEN"
        note = "Result quality is weak; do not force a PEAD thesis."
    elif rr == "GENUINE" and pr == "CONFIRMED":
        label = "EARNINGS INFLECTION + MARKET CONFIRMATION"
        note = "Fundamentals and post-result price/volume are aligned."
    elif rr == "GENUINE":
        label = "RESULT CONFIRMED / MARKET TEST PENDING"
        note = "Result quality is genuine; market confirmation is still incomplete."
    elif memory_status in {
        "CONFIRMED AGAIN",
        "SECOND-CHANCE CONFIRMATION",
        "REPEATED HIGH CONVICTION",
    }:
        label = "EXECUTION TREND IMPROVING"
        note = "Quarter memory is strengthening."
    else:
        label = "IN REVIEW"
        note = "Evidence is mixed or incomplete."

    return {"label": label, "note": note}


def action_bias(item: dict, gap: dict, memory: dict) -> dict:
    released = item.get("resultsReleased") is True
    rr = str((item.get("resultReality") or {}).get("label") or "")
    pr = str((item.get("priceResponse") or {}).get("label") or "")
    gap_label = str(gap.get("label") or "")
    memory_status = str(memory.get("status") or "")

    if not released:
        if gap_label in {"ELEVATED", "EXTREME"}:
            label = "WAIT — EXPECTATIONS HIGH"
            reason = "Result is pending and the pre-result expectation burden is elevated."
        elif gap_label == "LOW":
            label = "WATCH — SURPRISE ROOM"
            reason = "Result is pending and pre-result expectations appear relatively low."
        else:
            label = "WATCH"
            reason = "Result is pending; expectation evidence is normal or incomplete."
    elif rr == "GENUINE" and pr == "CONFIRMED" and gap_label in {"LOW", "NORMAL"}:
        label = "PEAD SETUP STRONG"
        reason = (
            "Genuine result, acceptable prior expectations and market confirmation are aligned."
        )
    elif rr == "GENUINE" and gap_label in {"ELEVATED", "EXTREME"}:
        label = "GOOD RESULT — PRICED-IN RISK"
        reason = (
            "The result is genuine, but expectations were already elevated before the print."
        )
    elif rr == "GENUINE":
        label = "REVIEW ENTRY"
        reason = "Result quality is genuine; confirmation or entry structure still needs review."
    elif rr == "LOW QUALITY":
        label = "AVOID / REVIEW"
        reason = "Result quality is weak."
    elif memory_status == "SECOND-CHANCE CONFIRMATION":
        label = "SECOND-CHANCE WATCH"
        reason = "Quarter memory improved after an earlier weak/mixed setup."
    else:
        label = "IN REVIEW"
        reason = "Not enough aligned evidence for a stronger PEAD classification."

    return {"label": label, "reason": reason}


def base_points(row: dict) -> int:
    score = num(pick(row, "score"))
    if score is None and isinstance(row.get("checks"), list):
        score = sum(
            bval(c.get("value")) is True
            for c in row["checks"]
            if isinstance(c, dict)
        )
    return 0 if score is None else max(0, min(40, round(score / 8.0 * 40)))


def verdict(row: dict, rr: dict, er: dict, conviction: int) -> str:
    if not results_released(row):
        return (
            "AWAIT RESULT — EXPECTATIONS ALREADY ELEVATED"
            if er["label"] == "PRICED IN"
            else "AWAIT RESULT — WATCHLIST"
        )
    if er["label"] == "PRICED IN":
        return "GOOD RESULT MAY BE PRICED IN — WAIT"
    if rr["label"] == "LOW QUALITY":
        return "RESULT QUALITY WEAK — AVOID / REVIEW"
    if conviction >= 75 and rr["label"] == "GENUINE" and er["label"] == "LOW EXPECTATIONS":
        return "HIGH-CONVICTION PEAD CANDIDATE"
    if conviction >= 60:
        return "PEAD CANDIDATE — REVIEW ENTRY"
    return "IN REVIEW — NOT ENOUGH EDGE YET"


def build_item(row: dict, index: int) -> dict:
    symbol = clean_symbol(row)
    released = results_released(row)
    history = history_for_row(row)
    pc = price_context(row, history)
    er = expectation_reality(row, pc)

    status = str(pick(row, "bucket", "peadStatus", "stage", "status") or "").lower()
    fs = (
        fundamental_snapshot(row)
        if released or status in {"caution", "qualified", "post-results", "post results"}
        else empty_fundamentals()
    )

    rr = result_reality(row, fs)
    vr = valuation_reality(row, fs)
    pr = price_response(row, pc)
    rv = result_verification(row, released)

    conviction = min(
        100,
        base_points(row)
        + RESULT_POINTS.get(rr["label"], 0)
        + EXPECTATION_POINTS.get(er["label"], 0)
        + VALUATION_POINTS.get(vr["label"], 0)
        + (5 if bval(pick(row, "sectorTailwind", "sectorPass")) is True else 0)
        + pr["points"],
    )

    commentary = pick(
        row,
        "managementCommentary", "guidance", "commentary",
        "resultCommentary", "note", "evidence",
    )

    public_sources = dict(fs.get("publicSources") or {})
    public_sources["screeningMantis"] = pc.get("screeningMantis") or {}
    stock_public = public_sources.get("stockScans") or {}
    concall_public = public_sources.get("concall") or {}
    management_documents = []
    for key, title in (
        ("earningsCallTranscriptUrl", "StockScans earnings-call transcript"),
        ("transcriptSummaryUrl", "StockScans transcript summary"),
        ("investorPresentationUrl", "StockScans investor presentation"),
        ("quarterlyResultUrl", "StockScans quarterly result"),
    ):
        if stock_public.get(key):
            management_documents.append({"title": title, "url": stock_public[key]})
    management_documents.extend(concall_public.get("documents") or [])

    return {
        "id": symbol or f"row-{index}",
        "symbol": symbol,
        "name": str(pick(row, "name", "company", "companyName") or symbol),
        "sector": str(pick(row, "sector", "industry") or "—"),
        "quarter": str(pick(row, "quarter", "earningsPeriod", "period") or "—"),
        "resultDate": pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"),
        "resultsReleased": released,
        "releaseDetection": release_detection(row),
        "baseBucket": str(pick(row, "bucket", "peadStatus", "stage", "status") or "—"),
        "baseScore": num(pick(row, "score")),
        "baseScoreText": str(pick(row, "scoreText") or "—"),
        "marketCapCr": (
            num(pick(row, "marketCapCr", "mcapCr", "market_cap_cr"))
            if num(pick(row, "marketCapCr", "mcapCr", "market_cap_cr")) is not None
            else num(fs.get("marketCapCr"))
        ),
        "price": num(pick(row, "price", "lastPrice", "ltp")),
        "priceTimestamp": pick(row, "priceTimestamp", "marketTime", "quoteTimestamp"),
        "resultReality": rr,
        "expectationReality": er,
        "valuationReality": vr,
        "priceResponse": pr,
        "resultVerification": rv,
        "priceContext": pc,
        "fundamentalSnapshot": fs,
        "publicSources": public_sources,
        "managementDocuments": management_documents[:12],
        "sectorTailwind": bval(pick(row, "sectorTailwind", "sectorPass")),
        "entry": pick(row, "entry", "entryPrice"),
        "sl": pick(row, "sl", "stopLoss"),
        "tsl": pick(row, "tsl", "trailingStopLoss"),
        "candidateStatus": pick(row, "candidateStatus"),
        "allocationPct": num(pick(row, "allocationPct")),
        "managementCommentary": commentary,
        "commentaryVerified": bool(commentary),
        "convictionScore": conviction,
        "verdict": verdict(row, rr, er, conviction),
        "reasons": rr["reasons"][:5] + er["reasons"][:3] + vr["reasons"][:3],
        "risks": rr["risks"][:4] + er["risks"][:3] + vr["risks"][:3],
    }


def fallback_item(row: dict, index: int, exc: Exception) -> dict:
    symbol = clean_symbol(row) or f"row-{index}"
    return {
        "id": symbol,
        "symbol": clean_symbol(row),
        "name": str(pick(row, "name", "company", "companyName") or symbol),
        "sector": str(pick(row, "sector", "industry") or "—"),
        "quarter": str(pick(row, "quarter", "earningsPeriod", "period") or "—"),
        "resultDate": pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"),
        "resultsReleased": results_released(row),
        "releaseDetection": release_detection(row),
        "baseBucket": str(pick(row, "bucket", "peadStatus", "stage", "status") or "—"),
        "baseScore": num(pick(row, "score")),
        "baseScoreText": str(pick(row, "scoreText") or "—"),
        "marketCapCr": num(pick(row, "marketCapCr", "mcapCr", "market_cap_cr")),
        "price": num(pick(row, "price", "lastPrice", "ltp")),
        "priceTimestamp": pick(row, "priceTimestamp", "marketTime", "quoteTimestamp"),
        "resultReality": {"label": "UNVERIFIED", "reasons": [], "risks": []},
        "expectationReality": {"label": "UNVERIFIED", "reasons": [], "risks": []},
        "valuationReality": {
            "label": "UNVERIFIED", "confidence": "LIMITED", "evidenceCount": 0,
            "reasons": [], "risks": [], "metrics": {},
        },
        "priceResponse": {"label": "UNVERIFIED", "points": 0},
        "resultVerification": {
            "label": "UNVERIFIED", "official": False, "source": None,
            "sourceUrl": None, "verifiedAt": None,
        },
        "priceContext": {},
        "fundamentalSnapshot": {"errors": [f"{type(exc).__name__}: {exc}"]},
        "sectorTailwind": bval(pick(row, "sectorTailwind", "sectorPass")),
        "entry": pick(row, "entry", "entryPrice"),
        "sl": pick(row, "sl", "stopLoss"),
        "tsl": pick(row, "tsl", "trailingStopLoss"),
        "candidateStatus": pick(row, "candidateStatus"),
        "allocationPct": num(pick(row, "allocationPct")),
        "managementCommentary": None,
        "commentaryVerified": False,
        "convictionScore": base_points(row),
        "verdict": "INTELLIGENCE ERROR — BASE ROW PRESERVED",
        "reasons": [],
        "risks": [f"Intelligence calculation failed: {type(exc).__name__}"],
    }


def build_payload(
    data_path: Path,
    previous_payload: dict | None = None,
) -> dict:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    rows = extract_rows(payload)
    if not rows:
        raise RuntimeError(
            "Base data.json contains 0 stocks. Intelligence output will NOT be published."
        )

    items = []
    for i, row in enumerate(rows, 1):
        symbol = clean_symbol(row) or f"row-{i}"
        print(f"[{i}/{len(rows)}] {symbol}")
        try:
            item = build_item(row, i)
        except Exception as exc:
            print(f"WARNING: {symbol}: {type(exc).__name__}: {exc}")
            item = fallback_item(row, i, exc)

        item["expectationGap"] = expectation_gap(item)
        items.append(item)

        if item.get("resultsReleased"):
            time.sleep(0.15)

    if len(items) != len(rows):
        raise RuntimeError(
            f"Row-preservation failure: base={len(rows)}, intelligence={len(items)}"
        )

    quarter_history = update_quarter_history(previous_payload, items)

    for item in items:
        memory = quarter_memory(item, quarter_history)
        item["quarterMemory"] = memory
        item["jCurvePhase"] = jcurve_phase(item, memory)
        item["actionBias"] = action_bias(
            item,
            item.get("expectationGap") or {},
            memory,
        )

    counts = {
        "total": len(items),
        "resultsDeclared": sum(x.get("resultsReleased") is True for x in items),
        "genuineResults": sum(
            x.get("resultReality", {}).get("label") == "GENUINE"
            for x in items
        ),
        "lowExpectations": sum(
            x.get("expectationReality", {}).get("label") == "LOW EXPECTATIONS"
            for x in items
        ),
        "pricedIn": sum(
            x.get("expectationReality", {}).get("label") == "PRICED IN"
            for x in items
        ),
        "attractiveValuation": sum(
            x.get("valuationReality", {}).get("label") == "ATTRACTIVE"
            for x in items
        ),
        "highConviction": sum(
            x.get("verdict") == "HIGH-CONVICTION PEAD CANDIDATE"
            for x in items
        ),
        "expectationGapLow": sum(
            x.get("expectationGap", {}).get("label") == "LOW"
            for x in items
        ),
        "expectationGapElevated": sum(
            x.get("expectationGap", {}).get("label") in {"ELEVATED", "EXTREME"}
            for x in items
        ),
        "secondChance": sum(
            x.get("quarterMemory", {}).get("status") == "SECOND-CHANCE CONFIRMATION"
            for x in items
        ),
        "confirmedAgain": sum(
            x.get("quarterMemory", {}).get("status")
            in {"CONFIRMED AGAIN", "REPEATED HIGH CONVICTION"}
            for x in items
        ),
        "peadStrong": sum(
            x.get("actionBias", {}).get("label") == "PEAD SETUP STRONG"
            for x in items
        ),
    }

    return {
        "generatedAt": datetime.now(IST).isoformat(),
        "sourceDataGeneratedAt": pick(
            payload,
            "generatedAt",
            "last_scan",
            "lastScanAt",
        ),
        "sourceScannerMode": payload.get("scannerMode"),
        "sourceQualificationVersion": payload.get("qualificationVersion"),
        "sourceStockCount": len(rows),
        "intelligenceVersion": "pead-intelligence-v5-public-fallbacks",
        "safety": {
            "dataJsonReadOnly": True,
            "zeroPublishProtection": True,
            "exactRowPreservation": True,
            "contextMergedIntoIntelligence": True,
            "noThirdPageRequired": True,
        },
        "methodNotes": [
            "Result release detection combines explicit flags, source/status evidence and recent scheduled result dates; generic In Review is not release proof.",
            "Calendar-detected releases are blocked from scoring stale Yahoo quarters until the latest reported period matches the expected quarter.",
            "Result Reality uses reported growth, earnings quality, cash flow and best-effort quarterly fundamentals.",
            "Expectation Reality uses pre-result movement; RVOL is a post-result confirmation input.",
            "Expectation Gap separately estimates how much optimism is already embedded before results using 5D/10D/20D movement and 52-week-high proximity.",
            "Quarter Memory persists inside intelligence.json and tracks one evolving snapshot per symbol/quarter.",
            "Second-chance confirmation highlights a genuine current result after a previously weak/mixed tracked quarter.",
            "J-Curve Phase is a lightweight context label based only on verified result/price/quarter-memory evidence; it does not invent capacity or management facts.",
            "Intraday RVOL is time-adjusted and explicitly marked as an estimate.",
            "Valuation requires at least 3 inputs before a label is assigned; otherwise it remains UNVERIFIED.",
            "Official result evidence is marked official only when the source explicitly points to NSE/BSE or an official exchange filing.",
            "StockScans public pages fill missing quarterly growth/valuation fields only when period consistency checks pass.",
            "ScreeningMantis public E-Day% is used only when its past-earnings date matches the tracked result date; its public table may cover only the rendered subset.",
            "Concall.in documents are read only when a company URL/ID is already mapped; no login bypass or search-engine scraping is used.",
            "Management commentary is never invented; document links can be surfaced without claiming commentary was verified.",
            "This add-on never modifies data.json or the main PEAD radar.",
        ],
        "counts": counts,
        "quarterHistory": quarter_history,
        "items": items,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data.json")
    parser.add_argument("--output", default="intelligence.json")
    parser.add_argument(
        "--previous",
        default="intelligence.json",
        help="Previous intelligence.json used only for quarter-memory carry-forward.",
    )
    args = parser.parse_args()

    data_path = Path(args.data)
    output_path = Path(args.output)
    previous_path = Path(args.previous)

    print("PEAD Intelligence starting...")
    print("Input:", data_path)
    print("Output:", output_path)
    print("Previous memory source:", previous_path)

    if not data_path.exists():
        raise RuntimeError(f"Input file not found: {data_path}")

    previous_payload = None
    if previous_path.exists():
        try:
            previous_payload = json.loads(
                previous_path.read_text(encoding="utf-8")
            )
            print(
                "Previous quarter-memory source loaded:",
                previous_payload.get("generatedAt"),
            )
        except Exception as exc:
            # Memory enrichment must never block a fresh intelligence build.
            print(
                "WARNING: previous intelligence memory could not be read:",
                f"{type(exc).__name__}: {exc}",
            )

    payload = build_payload(
        data_path,
        previous_payload=previous_payload,
    )

    source_count = int(payload.get("sourceStockCount", 0))
    output_count = len(payload.get("items") or [])

    print("Source stocks:", source_count)
    print("Generated intelligence rows:", output_count)

    if source_count <= 0:
        raise RuntimeError("Base source count is 0. Refusing to publish.")

    if output_count <= 0:
        raise RuntimeError("Intelligence generated 0 rows. Refusing to publish.")

    if output_count != source_count:
        raise RuntimeError(
            f"Intelligence count mismatch: source={source_count}, output={output_count}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = Path(str(output_path) + ".tmp")

    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(output_path)

    if not output_path.exists():
        raise RuntimeError("Output file was not created.")

    if output_path.stat().st_size == 0:
        raise RuntimeError("Output file was created but is empty.")

    print("OUTPUT CREATED:", output_path)
    print("OUTPUT SIZE:", output_path.stat().st_size, "bytes")
    print("PEAD INTELLIGENCE COMPLETE")
    print(json.dumps(payload.get("counts", {}), indent=2))


if __name__ == "__main__":
    main()
