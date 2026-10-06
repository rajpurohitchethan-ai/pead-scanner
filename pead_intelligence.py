#!/usr/bin/env python3
"""
PEAD Intelligence Lab — read-only add-on.

Safety:
- Reads data.json.
- NEVER writes data.json.
- Writes only the requested intelligence output.
- Missing evidence stays UNVERIFIED; it is never invented.
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

IST = ZoneInfo("Asia/Kolkata")
PRICED_IN_RUNUP_PCT = 15.0
RVOL_CONFIRM = 1.20

RESULT_POINTS = {"GENUINE": 25, "MIXED": 14, "LOW QUALITY": 4, "AWAITING RESULT": 0, "UNVERIFIED": 0}
EXPECTATION_POINTS = {"LOW EXPECTATIONS": 15, "PARTLY PRICED": 8, "PRICED IN": 0, "AWAITING PRICE HISTORY": 4, "UNVERIFIED": 0}
VALUATION_POINTS = {"ATTRACTIVE": 10, "FAIR": 7, "EXPENSIVE BUT JUSTIFIED": 4, "EXCESSIVE": 0, "UNVERIFIED": 3}


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
    return str(pick(row, "symbol", "sym", "ticker", "code") or "").upper().replace(".NS", "").strip()


def extract_rows(payload: dict) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("stocks", "companies", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    if isinstance(payload.get("data"), dict):
        for key in ("stocks", "companies"):
            val = payload["data"].get(key)
            if isinstance(val, list):
                return val
    return []


def results_released(row: dict) -> bool:
    explicit = bval(pick(row, "resultsReleased", "resultReleased", "results_declared"))
    if explicit is not None:
        return explicit

    source = str(pick(row, "discoverySource", "source") or "").lower()
    status = str(pick(row, "bucket", "peadStatus", "stage", "status") or "").lower()

    if "financial results" in source:
        return True

    return any(
        token in status
        for token in ("post-results", "post results", "results declared", "in review", "qualified")
    )


def result_date(row: dict):
    return parse_date(pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"))


def history_for_symbol(symbol: str) -> pd.DataFrame:
    if not symbol:
        return pd.DataFrame()
    try:
        h = yf.download(
            f"{symbol}.NS",
            period="14mo",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=15,
        )
        if h is None or h.empty:
            return pd.DataFrame()

        if isinstance(h.columns, pd.MultiIndex):
            try:
                h = h.xs(f"{symbol}.NS", axis=1, level=1)
            except Exception:
                h.columns = h.columns.get_level_values(0)

        if "Close" in h:
            h = h[h["Close"].notna()]
        return h.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def price_context(row: dict, h: pd.DataFrame) -> dict:
    out = {
        "pre5dPct": None,
        "pre10dPct": None,
        "pre20dPct": num(row.get("preResultRunupPct")),
        "resultDayPct": num(row.get("resultDayReturnPct")),
        "relativeVolume": num(pick(row, "relativeVolume", "rvol")),
        "distanceFrom52wHighPct": None,
        "lastClose": num(pick(row, "price", "lastPrice")),
        "historyAvailable": False,
    }

    if h.empty or "Close" not in h:
        return out

    close = pd.to_numeric(h["Close"], errors="coerce").dropna()
    if close.empty:
        return out

    out["historyAvailable"] = True
    out["lastClose"] = float(close.iloc[-1])

    high = pd.to_numeric(h["High"], errors="coerce").dropna() if "High" in h else close
    if not high.empty:
        high52 = float(high.tail(252).max())
        if high52 > 0:
            out["distanceFrom52wHighPct"] = (out["lastClose"] / high52 - 1) * 100

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
    pre_close = pd.to_numeric(pre["Close"], errors="coerce").dropna() if "Close" in pre else pd.Series(dtype=float)

    def pre_return(days: int):
        if len(pre_close) <= days:
            return None
        return pct_change(float(pre_close.iloc[-1]), float(pre_close.iloc[-1 - days]))

    out["pre5dPct"] = pre_return(5)
    out["pre10dPct"] = pre_return(10)
    if out["pre20dPct"] is None:
        out["pre20dPct"] = pre_return(20)

    if rd is not None and out["resultDayPct"] is None and end < len(h) and end > 0:
        out["resultDayPct"] = pct_change(num(h["Close"].iloc[end]), num(h["Close"].iloc[end - 1]))

    if out["relativeVolume"] is None and "Volume" in h and len(h) >= 21:
        vol = pd.to_numeric(h["Volume"], errors="coerce")
        avg = num(vol.iloc[-21:-1].mean())
        cur = num(vol.iloc[-1])
        if avg not in (None, 0) and cur is not None:
            out["relativeVolume"] = cur / avg

    return out


def expectation_reality(row: dict, pc: dict) -> dict:
    run20 = num(pc.get("pre20dPct"))
    result_move = num(pc.get("resultDayPct"))
    rvol = num(pc.get("relativeVolume"))
    reasons, risks = [], []

    if run20 is None:
        label = "AWAITING PRICE HISTORY"
        reasons.append("20-day pre-result move could not be verified.")
    elif run20 > PRICED_IN_RUNUP_PCT:
        label = "PRICED IN"
        risks.append(f"20-day pre-result run-up was {run20:.1f}%, above the {PRICED_IN_RUNUP_PCT:.0f}% threshold.")
    elif run20 > 5:
        label = "PARTLY PRICED"
        reasons.append(f"Pre-result move was moderate at {run20:.1f}%.")
    else:
        label = "LOW EXPECTATIONS"
        reasons.append(f"Pre-result move was only {run20:.1f}%.")

    if result_move is not None:
        if result_move >= 3:
            reasons.append(f"Result-day move was +{result_move:.1f}%.")
        elif result_move <= -3:
            risks.append(f"Result-day move was {result_move:.1f}%.")

    if rvol is not None:
        if rvol >= RVOL_CONFIRM:
            reasons.append(f"Relative volume was {rvol:.2f}x.")
        elif results_released(row):
            risks.append(f"Relative volume was only {rvol:.2f}x.")

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


def statement_series(df: pd.DataFrame, aliases: list[str]):
    idx = row_from_statement(df, aliases)
    if idx is None:
        return []
    s = pd.to_numeric(df.loc[idx], errors="coerce").dropna()
    return [float(x) for x in s.tolist()]


def fundamental_snapshot(symbol: str) -> dict:
    out = {
        "source": "yfinance best-effort",
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
        "errors": [],
    }

    if not symbol:
        return out

    try:
        t = yf.Ticker(f"{symbol}.NS")

        try:
            income = t.quarterly_income_stmt
            rev = statement_series(income, ["Total Revenue", "Operating Revenue"])
            pat = statement_series(income, ["Net Income", "Net Income Common Stockholders"])
            opi = statement_series(income, ["Operating Income", "EBIT"])

            if len(rev) >= 5:
                out["revenueYoYCalc"] = pct_change(rev[0], rev[4])
            if len(pat) >= 5:
                out["patYoYCalc"] = pct_change(pat[0], pat[4])

            if rev and opi and rev[0] != 0:
                out["operatingMarginNow"] = opi[0] / rev[0] * 100
            if len(rev) >= 5 and len(opi) >= 5 and rev[4] != 0 and out["operatingMarginNow"] is not None:
                out["operatingMarginYoY"] = out["operatingMarginNow"] - (opi[4] / rev[4] * 100)

            pretax = statement_series(income, ["Pretax Income", "Income Before Tax"])
            other = statement_series(
                income,
                ["Other Non Operating Income Expenses", "Other Income Expense", "Other Non Operating Income"],
            )
            if pretax and other and pretax[0] != 0:
                out["otherIncomeToPretaxPct"] = abs(other[0]) / abs(pretax[0]) * 100
        except Exception as exc:
            out["errors"].append(f"income:{type(exc).__name__}")

        try:
            cf = t.quarterly_cash_flow
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
            out["returnOnEquityPct"] = roe * 100 if roe is not None and abs(roe) <= 5 else roe

            out["debtToEquity"] = num(info.get("debtToEquity"))

            fcf = num(info.get("freeCashflow"))
            mcap = num(info.get("marketCap"))
            if fcf is not None and mcap not in (None, 0):
                out["freeCashFlowYieldPct"] = fcf / mcap * 100
        except Exception as exc:
            out["errors"].append(f"valuation:{type(exc).__name__}")

    except Exception as exc:
        out["errors"].append(f"ticker:{type(exc).__name__}")

    return out


def result_reality(row: dict, fs: dict) -> dict:
    if not results_released(row):
        return {"label": "AWAITING RESULT", "reasons": ["Result has not been confirmed as released."], "risks": []}

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

    score, red = 0, 0
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

    label = "GENUINE" if score >= 6 and red == 0 else ("LOW QUALITY" if red >= 2 or score <= 2 else "MIXED")
    return {"label": label, "reasons": reasons, "risks": risks}


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
    reasons, risks = [], []

    if evidence_count < 2:
        return {
            "label": "UNVERIFIED",
            "reasons": ["Insufficient valuation data; no valuation conclusion forced."],
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

    label = "ATTRACTIVE" if score >= 4 else ("FAIR" if score >= 2 else ("EXPENSIVE BUT JUSTIFIED" if score >= 0 else "EXCESSIVE"))
    return {"label": label, "reasons": reasons, "risks": risks, "metrics": metrics}


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


def base_points(row: dict) -> int:
    score = num(pick(row, "score"))
    if score is None and isinstance(row.get("checks"), list):
        score = sum(
            bval(c.get("value")) is True
            for c in row["checks"]
            if isinstance(c, dict)
        )
    return 0 if score is None else max(0, min(40, round(score / 8 * 40)))


def verdict(row: dict, rr: dict, er: dict, vr: dict, conviction: int) -> str:
    if not results_released(row):
        return "AWAIT RESULT — EXPECTATIONS ALREADY ELEVATED" if er["label"] == "PRICED IN" else "AWAIT RESULT — WATCHLIST"

    if er["label"] == "PRICED IN":
        return "GOOD RESULT MAY BE PRICED IN — WAIT"

    if rr["label"] == "LOW QUALITY":
        return "RESULT QUALITY WEAK — AVOID / REVIEW"

    if conviction >= 75 and rr["label"] == "GENUINE" and er["label"] == "LOW EXPECTATIONS":
        return "HIGH-CONVICTION PEAD CANDIDATE"

    if conviction >= 60:
        return "PEAD CANDIDATE — REVIEW ENTRY"

    return "IN REVIEW — NOT ENOUGH EDGE YET"


def empty_fundamentals():
    return {
        "source": "not requested before result",
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
        "errors": [],
    }


def build_item(row: dict, index: int) -> dict:
    symbol = clean_symbol(row)
    released = results_released(row)

    h = history_for_symbol(symbol)
    pc = price_context(row, h)
    er = expectation_reality(row, pc)

    status = str(pick(row, "bucket", "peadStatus", "stage", "status") or "")
    fs = fundamental_snapshot(symbol) if (released or status.lower() in {"caution", "qualified", "post-results"}) else empty_fundamentals()

    rr = result_reality(row, fs)
    vr = valuation_reality(row, fs)
    pr = price_response(row, pc)

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
        "managementCommentary",
        "guidance",
        "commentary",
        "resultCommentary",
        "note",
        "evidence",
    )

    return {
        "id": symbol or f"row-{index}",
        "symbol": symbol,
        "name": str(pick(row, "name", "company", "companyName") or symbol),
        "sector": str(pick(row, "sector", "industry") or "—"),
        "quarter": str(pick(row, "quarter", "earningsPeriod", "period") or "—"),
        "resultDate": pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"),
        "resultsReleased": released,
        "baseBucket": str(pick(row, "bucket", "peadStatus", "stage", "status") or "—"),
        "baseScore": num(pick(row, "score")),
        "baseScoreText": str(pick(row, "scoreText") or "—"),
        "marketCapCr": num(pick(row, "marketCapCr", "mcapCr")),
        "price": num(pick(row, "price", "lastPrice")),
        "resultReality": rr,
        "expectationReality": er,
        "valuationReality": vr,
        "priceResponse": pr,
        "priceContext": pc,
        "fundamentalSnapshot": fs,
        "sectorTailwind": bval(pick(row, "sectorTailwind", "sectorPass")),
        "entry": pick(row, "entry", "entryPrice"),
        "sl": pick(row, "sl", "stopLoss"),
        "tsl": pick(row, "tsl", "trailingStopLoss"),
        "candidateStatus": pick(row, "candidateStatus"),
        "allocationPct": num(pick(row, "allocationPct")),
        "managementCommentary": commentary,
        "commentaryVerified": bool(commentary),
        "convictionScore": conviction,
        "verdict": verdict(row, rr, er, vr, conviction),
        "reasons": (rr["reasons"][:5] + er["reasons"][:3] + vr["reasons"][:3]),
        "risks": (rr["risks"][:4] + er["risks"][:3] + vr["risks"][:3]),
    }


def build_payload(data_path: Path) -> dict:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    rows = extract_rows(payload)

    if not rows:
        raise RuntimeError("Base data.json contains 0 stocks. Intelligence output will NOT be published.")

    items = []
    for i, row in enumerate(rows, 1):
        symbol = clean_symbol(row) or f"row-{i}"
        print(f"[{i}/{len(rows)}] {symbol}")

        try:
            item = build_item(row, i)
        except Exception as exc:
            # Preserve the base row instead of dropping it.
            item = {
                "id": symbol,
                "symbol": clean_symbol(row),
                "name": str(pick(row, "name", "company", "companyName") or symbol),
                "sector": str(pick(row, "sector", "industry") or "—"),
                "quarter": str(pick(row, "quarter", "earningsPeriod", "period") or "—"),
                "resultDate": pick(row, "resultDate", "result_date", "resultsDate"),
                "resultsReleased": results_released(row),
                "baseBucket": str(pick(row, "bucket", "peadStatus", "stage", "status") or "—"),
                "baseScore": num(pick(row, "score")),
                "baseScoreText": str(pick(row, "scoreText") or "—"),
                "marketCapCr": num(pick(row, "marketCapCr", "mcapCr")),
                "price": num(pick(row, "price", "lastPrice")),
                "resultReality": {"label": "UNVERIFIED", "reasons": [], "risks": []},
                "expectationReality": {"label": "UNVERIFIED", "reasons": [], "risks": []},
                "valuationReality": {"label": "UNVERIFIED", "reasons": [], "risks": [], "metrics": {}},
                "priceResponse": {"label": "UNVERIFIED", "points": 0},
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

        items.append(item)
        if item.get("resultsReleased"):
            time.sleep(0.15)

    if not items:
        raise RuntimeError("Intelligence produced 0 rows. Refusing to publish.")

    counts = {
        "total": len(items),
        "resultsDeclared": sum(x.get("resultsReleased") is True for x in items),
        "genuineResults": sum(x.get("resultReality", {}).get("label") == "GENUINE" for x in items),
        "lowExpectations": sum(x.get("expectationReality", {}).get("label") == "LOW EXPECTATIONS" for x in items),
        "pricedIn": sum(x.get("expectationReality", {}).get("label") == "PRICED IN" for x in items),
        "attractiveValuation": sum(x.get("valuationReality", {}).get("label") == "ATTRACTIVE" for x in items),
        "highConviction": sum(x.get("verdict") == "HIGH-CONVICTION PEAD CANDIDATE" for x in items),
    }

    return {
        "generatedAt": datetime.now(IST).isoformat(),
        "sourceDataGeneratedAt": pick(payload, "generatedAt", "last_scan", "lastScanAt"),
        "sourceScannerMode": payload.get("scannerMode"),
        "sourceQualificationVersion": payload.get("qualificationVersion"),
        "sourceStockCount": len(rows),
        "intelligenceVersion": "pead-intelligence-v1",
        "safety": {
            "dataJsonReadOnly": True,
            "zeroPublishProtection": True,
            "minimumPublishRatio": 0.80,
        },
        "methodNotes": [
            "Result Reality uses reported growth, quality/cash-flow gates and best-effort quarterly fundamentals.",
            "Expectation Reality is driven primarily by pre-result price movement, plus result-day move and RVOL.",
            "Valuation Reality is growth-adjusted and best-effort; missing valuation inputs remain UNVERIFIED.",
            "Management commentary is not invented. It is shown only when already present in the source row.",
            "This add-on does not modify the base PEAD radar or data.json.",
        ],
        "counts": counts,
        "items": items,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data.json")
    parser.add_argument("--output", default="intelligence.json")
    args = parser.parse_args()

    data_path = Path(args.data)
    output_path = Path(args.output)

    if not data_path.exists():
        raise RuntimeError(f"{data_path} not found")

    payload = build_payload(data_path)

    source_count = int(payload["sourceStockCount"])
    output_count = len(payload["items"])
    minimum = max(1, math.floor(source_count * 0.80))

    if output_count < minimum:
        raise RuntimeError(
            f"Safety stop: intelligence has {output_count} rows from {source_count} base rows; "
            f"minimum allowed is {minimum}. Existing intelligence.json must be kept."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(output_path)

    print("PEAD INTELLIGENCE COMPLETE")
    print(json.dumps(payload["counts"], indent=2))


if __name__ == "__main__":
    main()
