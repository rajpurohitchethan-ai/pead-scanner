#!/usr/bin/env python3
"""PEAD live discovery scanner.

What this scanner does
----------------------
1. Keeps the existing ``companies.json`` watch universe.
2. Discovers *recently declared* quarterly results market-wide from NSE.
3. Discovers *upcoming* result board meetings from NSE.
4. Adds BSE result announcements/calendar as a fallback, so BSE-only names are
   not silently missed.
5. Enriches every candidate with price/market-cap/technical data using Yahoo
   Finance and writes one compatible ``data.json`` feed.

The scanner is deliberately fault tolerant: one blocked source or ticker must
not collapse the whole feed.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

try:
    from nse import NSE
except Exception:  # pragma: no cover - dependency/runtime fallback
    NSE = None

try:
    from bse import BSE
except Exception:  # pragma: no cover - dependency/runtime fallback
    BSE = None

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "companies.json"
OUTPUT = ROOT / "data.json"
MIN_MCAP_CR = float(os.getenv("MIN_MCAP_CR", "1000"))
IST = ZoneInfo("Asia/Kolkata")
RECENT_RESULT_DAYS = int(os.getenv("RECENT_RESULT_DAYS", "45"))
BSE_RECENT_RESULT_DAYS = int(os.getenv("BSE_RECENT_RESULT_DAYS", "10"))
UPCOMING_RESULT_DAYS = int(os.getenv("UPCOMING_RESULT_DAYS", "45"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def as_float(value: Any) -> float | None:
    try:
        x = float(str(value).replace(",", "").replace("%", "").strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def boolish(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in {"true", "yes", "pass", "passed", "qualified", "released", "declared"}:
        return True
    if s in {"false", "no", "fail", "failed", "pending", "upcoming"}:
        return False
    return None


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d/%m/%Y",
        "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    try:
        return pd.Timestamp(s).date()
    except Exception:
        return None


def normalize_symbol(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().upper().replace("NSE:", "")
    return re.sub(r"\.(NS|BO)$", "", s).strip()


def fiscal_quarter_from_period_end(period_end: date | None) -> str | None:
    if period_end is None:
        return None
    month = period_end.month
    if month == 6:
        q, fy = 1, period_end.year + 1
    elif month == 9:
        q, fy = 2, period_end.year + 1
    elif month == 12:
        q, fy = 3, period_end.year + 1
    elif month == 3:
        q, fy = 4, period_end.year
    else:
        return None
    return f"Q{q} FY{str(fy)[-2:]}"


def expected_period_end_from_event(event_date: date | None) -> date | None:
    if event_date is None:
        return None
    candidates = [
        date(event_date.year - 1, 12, 31),
        date(event_date.year, 3, 31),
        date(event_date.year, 6, 30),
        date(event_date.year, 9, 30),
        date(event_date.year, 12, 31),
    ]
    prior = [d for d in candidates if d < event_date]
    return max(prior) if prior else None


def load_seed_companies() -> list[dict[str, Any]]:
    if not INPUT.exists():
        return []
    try:
        raw = json.loads(INPUT.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("companies") or raw.get("stocks") or raw.get("data") or []
    else:
        rows = []
    return [dict(x) for x in rows if isinstance(x, dict)]


def _nse_client():
    if NSE is None:
        return None
    # Plain HTTP/1.1 is sufficient here and avoids optional HTTP/2 extras.
    return NSE(download_folder=str(ROOT))


def discover_nse() -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if NSE is None:
        return rows, ["nse package unavailable"]

    now = datetime.now(IST).replace(tzinfo=None)
    try:
        client = _nse_client()
        if client is None:
            return rows, ["NSE client unavailable"]
        try:
            filings = client.financial_results(
                segment="equities",
                period="quarterly",
                from_date=now - timedelta(days=RECENT_RESULT_DAYS),
                to_date=now,
            ) or []
            for item in filings:
                if not isinstance(item, dict):
                    continue
                symbol = normalize_symbol(pick(item, "symbol", "Symbol", "symbolCode", "securitySymbol"))
                if not symbol:
                    continue
                broadcast = parse_date(pick(item, "broadCastDate", "broadcastDate", "filingDate", "date"))
                period_end = parse_date(pick(item, "toDate", "periodEnded", "periodEnd", "endDate"))
                quarter = fiscal_quarter_from_period_end(period_end)
                rows.append({
                    "symbol": symbol,
                    "sym": symbol,
                    "name": pick(item, "companyName", "company", "comp", default=symbol),
                    "resultDate": str(broadcast) if broadcast else None,
                    "result_date": str(broadcast) if broadcast else None,
                    "resultPeriodEnd": str(period_end) if period_end else None,
                    "quarter": quarter,
                    "resultsReleased": True,
                    "resultReleased": True,
                    "peadStatus": "Post-results",
                    "bucket": "Post-results",
                    "discoverySource": "NSE financial results filing",
                    "resultsEvidence": "Official NSE quarterly financial-results filing detected.",
                    "resultSourceUrl": pick(item, "xbrl", "xbrlLink", "fileName"),
                    "resultVerifiedAt": utc_now_iso(),
                })
        except Exception as exc:
            errors.append(f"NSE financial_results: {type(exc).__name__}: {exc}")

        try:
            meetings = client.board_meetings(
                index="equities",
                from_date=now,
                to_date=now + timedelta(days=UPCOMING_RESULT_DAYS),
            ) or []
            for item in meetings:
                if not isinstance(item, dict):
                    continue
                purpose = str(pick(
                    item, "bm_purpose", "purpose", "bmPurpose", "subject",
                    "description", "bm_desc", default=""
                ) or "")
                if "financial result" not in purpose.lower() and "result" not in purpose.lower():
                    continue
                symbol = normalize_symbol(pick(
                    item, "bm_symbol", "symbol", "Symbol", "symbolCode", "securitySymbol"
                ))
                meeting_date = parse_date(pick(
                    item, "bm_date", "meetingDate", "bmDate", "date", "boardMeetingDate"
                ))
                if not symbol or meeting_date is None:
                    continue
                period_end = expected_period_end_from_event(meeting_date)
                rows.append({
                    "symbol": symbol,
                    "sym": symbol,
                    "name": pick(item, "sm_name", "companyName", "company", "comp", default=symbol),
                    "sector": pick(item, "sm_indusrty", "industry", default=None),
                    "resultDate": str(meeting_date),
                    "result_date": str(meeting_date),
                    "resultPeriodEnd": str(period_end) if period_end else None,
                    "quarter": fiscal_quarter_from_period_end(period_end),
                    "resultsReleased": False,
                    "resultReleased": False,
                    "peadStatus": "Upcoming",
                    "bucket": "Upcoming",
                    "discoverySource": "NSE board meeting - financial results",
                    "resultsEvidence": f"NSE board meeting scheduled for financial results on {meeting_date}.",
                    "resultSourceUrl": pick(item, "attachment", "xbrl", "fileName"),
                })
        except Exception as exc:
            errors.append(f"NSE board_meetings: {type(exc).__name__}: {exc}")
        try:
            client.exit()
        except Exception:
            pass
    except Exception as exc:
        errors.append(f"NSE discovery init: {type(exc).__name__}: {exc}")
    return rows, errors


def _bse_get(d: dict[str, Any], *names: str):
    lowered = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def _bse_symbol(client, item: dict[str, Any]) -> tuple[str, str | None]:
    code = _bse_get(
        item, "SCRIP_CD", "SCRIPCODE", "SCRIP_CODE", "scripcode", "scrip_Code",
        "SecurityCode", "Code"
    )
    code = str(code).strip() if code not in (None, "") else None
    # Prefer true exchange short-symbol fields.  Announcement rows often carry
    # only a scrip code, in which case getScripName() is authoritative.
    sym = normalize_symbol(_bse_get(item, "SYMBOL", "symbol", "short_name", "SHORT_NAME"))
    if sym:
        return sym, code
    if code:
        try:
            return normalize_symbol(client.getScripName(code)), code
        except Exception:
            pass
    return "", code


def discover_bse() -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if BSE is None:
        return rows, ["bse package unavailable"]

    now = datetime.now(IST).replace(tzinfo=None)
    try:
        with BSE(str(ROOT)) as bse:
            # Recent released results. Category='Result' keeps pagination bounded.
            try:
                page = 1
                seen = 0
                total = None
                while page <= 20:
                    res = bse.announcements(
                        page_no=page,
                        from_date=now - timedelta(days=BSE_RECENT_RESULT_DAYS),
                        to_date=now,
                        category="Result",
                    ) or {}
                    table = res.get("Table") or []
                    if total is None:
                        try:
                            total = int((res.get("Table1") or [{}])[0].get("ROWCNT") or len(table))
                        except Exception:
                            total = len(table)
                    if not table:
                        break
                    for item in table:
                        if not isinstance(item, dict):
                            continue
                        symbol, code = _bse_symbol(bse, item)
                        if not symbol:
                            continue
                        headline = str(_bse_get(item, "HEADLINE", "NEWSSUB", "SUBJECT", "NEWS_SUB", "subject") or "")
                        event_date = parse_date(_bse_get(item, "NEWS_DT", "DT_TM", "NEWS_DATE", "BroadcastDate", "date"))
                        period_end = expected_period_end_from_event(event_date)
                        rows.append({
                            "symbol": symbol,
                            "sym": symbol,
                            "name": _bse_get(item, "SLONGNAME", "LONG_NAME", "COMPANYNAME", "CompanyName") or symbol,
                            "bseCode": code,
                            "resultDate": str(event_date) if event_date else None,
                            "result_date": str(event_date) if event_date else None,
                            "resultPeriodEnd": str(period_end) if period_end else None,
                            "quarter": fiscal_quarter_from_period_end(period_end),
                            "resultsReleased": True,
                            "resultReleased": True,
                            "peadStatus": "Post-results",
                            "bucket": "Post-results",
                            "discoverySource": "BSE result announcement",
                            "resultsEvidence": f"BSE result announcement detected{': ' + headline if headline else ''}.",
                            "resultVerifiedAt": utc_now_iso(),
                        })
                    seen += len(table)
                    if total is not None and seen >= total:
                        break
                    page += 1
            except Exception as exc:
                errors.append(f"BSE announcements: {type(exc).__name__}: {exc}")

            # Upcoming BSE result calendar.
            try:
                cal = bse.resultCalendar(from_date=now, to_date=now + timedelta(days=UPCOMING_RESULT_DAYS)) or []
                for item in cal:
                    if not isinstance(item, dict):
                        continue
                    symbol, code = _bse_symbol(bse, item)
                    event_date = parse_date(_bse_get(
                        item, "meeting_date", "ResultDate", "RESULTDATE", "MeetingDate",
                        "BoardMeetingDate", "date"
                    ))
                    if not symbol or event_date is None:
                        continue
                    period_end = expected_period_end_from_event(event_date)
                    rows.append({
                        "symbol": symbol,
                        "sym": symbol,
                        "name": _bse_get(
                            item, "Long_Name", "LongName", "SLONGNAME", "COMPANYNAME", "CompanyName"
                        ) or symbol,
                        "bseCode": code,
                        "resultDate": str(event_date),
                        "result_date": str(event_date),
                        "resultPeriodEnd": str(period_end) if period_end else None,
                        "quarter": fiscal_quarter_from_period_end(period_end),
                        "resultsReleased": False,
                        "resultReleased": False,
                        "peadStatus": "Upcoming",
                        "bucket": "Upcoming",
                        "discoverySource": "BSE result calendar",
                        "resultsEvidence": f"BSE result calendar date {event_date}.",
                    })
            except Exception as exc:
                errors.append(f"BSE resultCalendar: {type(exc).__name__}: {exc}")
    except Exception as exc:
        errors.append(f"BSE discovery init: {type(exc).__name__}: {exc}")
    return rows, errors


def merge_universe(seeds: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in seeds:
        symbol = normalize_symbol(pick(row, "symbol", "sym", "ticker", "code"))
        if not symbol:
            continue
        base = dict(row)
        base["symbol"] = symbol
        base["sym"] = symbol
        base["_seedRow"] = True
        merged[symbol] = base
        order.append(symbol)

    # Sort so upcoming is applied first and an actual released filing wins last.
    discovered = sorted(discovered, key=lambda r: boolish(r.get("resultsReleased")) is True)
    for row in discovered:
        symbol = normalize_symbol(pick(row, "symbol", "sym"))
        if not symbol:
            continue
        if symbol not in merged:
            merged[symbol] = {"symbol": symbol, "sym": symbol, "_seedRow": False}
            order.append(symbol)
        current = merged[symbol]
        released = boolish(row.get("resultsReleased")) is True
        current_released = boolish(current.get("resultsReleased")) is True
        if released or not current_released:
            # Result-specific official discovery data supersedes stale calendar/status data.
            for k, v in row.items():
                if v not in (None, ""):
                    current[k] = v
        else:
            for k, v in row.items():
                if current.get(k) in (None, "") and v not in (None, ""):
                    current[k] = v
    return [merged[s] for s in order]


def yahoo_symbol_for_row(row: dict[str, Any]) -> str:
    explicit = str(pick(row, "ticker", "yahooTicker", default="") or "").strip().upper()
    if explicit.endswith((".NS", ".BO")):
        return explicit
    bse_code = str(pick(row, "bseCode", "bse_code", "scripCode", "scrip_code", default="") or "").strip()
    if re.fullmatch(r"\d{6}", bse_code):
        return f"{bse_code}.BO"
    symbol = normalize_symbol(pick(row, "symbol", "sym", "code"))
    return f"{symbol}.NS" if symbol else ""


def safe_market_cap_cr(ticker: yf.Ticker, last_price: float | None) -> float | None:
    market_cap = None
    try:
        fast = ticker.fast_info
        market_cap = as_float(fast.get("market_cap") if hasattr(fast, "get") else None)
    except Exception:
        pass
    if not market_cap:
        try:
            info = ticker.info or {}
            market_cap = as_float(info.get("marketCap"))
            if not market_cap and last_price:
                shares = as_float(info.get("sharesOutstanding"))
                if shares:
                    market_cap = shares * last_price
        except Exception:
            pass
    return round(market_cap / 10_000_000, 2) if market_cap else None


def moving_average(close: pd.Series, n: int) -> float | None:
    if close is None or len(close) < n:
        return None
    val = close.rolling(n).mean().iloc[-1]
    return round(float(val), 2) if pd.notna(val) else None


def fetch_live(company: dict[str, Any]) -> dict[str, Any]:
    symbol = normalize_symbol(pick(company, "symbol", "sym", "ticker", "code"))
    result_date = pick(company, "resultDate", "result_date", "resultsDate", "earningsDate")
    row = dict(company)
    row.update({
        "symbol": symbol,
        "sym": symbol,
        "resultDate": result_date,
        "result_date": result_date,
        "liveStatus": "unavailable",
        "liveError": None,
    })
    if not symbol:
        row["liveError"] = "Missing symbol/sym"
        return row

    ys = yahoo_symbol_for_row(row)
    try:
        ticker = yf.Ticker(ys)
        hist = ticker.history(period="1y", interval="1d", auto_adjust=False, actions=False)
        if hist is None or hist.empty:
            raise RuntimeError("No price history returned")
        close = hist["Close"].dropna()
        volume = hist["Volume"].dropna() if "Volume" in hist.columns else pd.Series(dtype=float)
        if close.empty:
            raise RuntimeError("No closing prices returned")
        last = round(float(close.iloc[-1]), 2)
        prev = round(float(close.iloc[-2]), 2) if len(close) > 1 else None
        change_pct = round((last / prev - 1) * 100, 2) if prev else None
        mcap_cr = safe_market_cap_cr(ticker, last)
        vol20 = float(volume.tail(20).mean()) if len(volume) else None
        rel_vol = round(float(volume.iloc[-1]) / vol20, 2) if vol20 and vol20 > 0 else None
        row.update({
            "ticker": ys,
            "price": last,
            "lastPrice": last,
            "previousClose": prev,
            "changePct": change_pct,
            "marketCapCr": mcap_cr if mcap_cr is not None else as_float(pick(company, "marketCapCr", "mcapCr")),
            "ma10": moving_average(close, 10),
            "ma20": moving_average(close, 20),
            "ma50": moving_average(close, 50),
            "ma200": moving_average(close, 200),
            "relativeVolume": rel_vol,
            "priceTimestamp": hist.index[-1].isoformat() if len(hist.index) else utc_now_iso(),
            "liveStatus": "ok",
            "liveError": None,
        })
    except Exception as exc:
        row["liveError"] = f"{type(exc).__name__}: {exc}"
        source_mcap = as_float(pick(company, "marketCapCr", "mcapCr", "market_cap_cr"))
        if source_mcap is not None:
            row["marketCapCr"] = source_mcap

    mcap = as_float(row.get("marketCapCr"))
    row["marketCapPass"] = None if mcap is None else mcap >= MIN_MCAP_CR

    if boolish(row.get("resultsReleased")) is True:
        row["resultsReleased"] = True
        row["resultReleased"] = True
        row["bucket"] = "Post-results"
        row["peadStatus"] = "Post-results"
        row["stage"] = "Post-results"
    elif not pick(row, "peadStatus", "stage", "status", "bucket"):
        rd = parse_date(result_date)
        row["peadStatus"] = "Upcoming" if rd and rd >= datetime.now(IST).date() else "In Review"
    return row


def main() -> int:
    started = utc_now_iso()
    seeds = load_seed_companies()
    nse_rows, nse_errors = discover_nse()
    bse_rows, bse_errors = discover_bse()
    universe = merge_universe(seeds, nse_rows + bse_rows)

    print(
        f"Discovery: seeds={len(seeds)} NSE={len(nse_rows)} BSE={len(bse_rows)} "
        f"merged={len(universe)}"
    )

    scanned: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for i, company in enumerate(universe):
        row = fetch_live(company)
        # Keep seed rows when market cap is temporarily unavailable. For newly
        # discovered market-wide rows, require a known market cap >= threshold so
        # the result feed does not get flooded with tiny/unknown names.
        seed = bool(row.get("_seedRow"))
        mcap_pass = row.get("marketCapPass")
        if mcap_pass is True or (seed and mcap_pass is not False):
            row.pop("_seedRow", None)
            scanned.append(row)
        if row.get("liveError"):
            errors.append({"symbol": row.get("symbol", ""), "error": row["liveError"]})
        if i and i % 20 == 0:
            time.sleep(0.4)

    payload = {
        "generatedAt": started,
        "last_scan": started,
        "lastScanAt": started,
        "scannerMode": "live-discovery",
        "discoveryVersion": "result-discovery-v2-nse-bse",
        "minMarketCapCr": MIN_MCAP_CR,
        "sourceCount": len(seeds),
        "discoveredCandidateCount": len(universe),
        "nseDiscoveredCount": len(nse_rows),
        "bseDiscoveredCount": len(bse_rows),
        "scanCount": len(scanned),
        "errorCount": len(errors),
        "errors": errors,
        "discoveryWarnings": nse_errors + bse_errors,
        "companies": scanned,
        "stocks": scanned,
    }

    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(OUTPUT)

    print(
        f"PEAD scan complete: seeds={len(seeds)} discovered={len(universe)} "
        f"published={len(scanned)} warnings={len(payload['discoveryWarnings'])} errors={len(errors)}"
    )
    for warning in payload["discoveryWarnings"][:20]:
        print("DISCOVERY WARNING:", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
