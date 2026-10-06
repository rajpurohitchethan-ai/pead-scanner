#!/usr/bin/env python3
"""PEAD scanner data builder.

Compatibility goals:
- Accept both old company keys: sym/resultDate and symbol/result_date.
- Never collapse the whole feed to zero because one ticker/API request fails.
- Emit both old and new top-level shapes so either frontend can read data.json:
  {generatedAt, companies} and {last_scan, stocks}.
- Keep the ₹1,000 crore market-cap rule, but retain unknown market-cap rows as
  unverified rather than silently deleting the whole universe when live data fails.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "companies.json"
OUTPUT = ROOT / "data.json"
MIN_MCAP_CR = float(os.getenv("MIN_MCAP_CR", "1000"))
IST = ZoneInfo("Asia/Kolkata")
RESULT_DATE_RELEASE_LOOKBACK_DAYS = 45
RESULT_DAY_AUTO_RELEASE_HOUR_IST = 18


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
        x = float(value)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
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


def parse_date(value: Any):
    if value in (None, ""):
        return None
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return None


def result_source_blob(row: dict[str, Any]) -> str:
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


def result_release_detection(row: dict[str, Any]) -> dict[str, Any]:
    """Automatic, bounded detection for the currently tracked result event."""
    now = datetime.now(IST)
    today = now.date()
    rd = parse_date(pick(row, "resultDate", "result_date", "resultsDate", "earningsDate"))
    explicit = boolish(pick(row, "resultsReleased", "resultReleased", "results_declared"))
    source = result_source_blob(row)
    status = str(pick(row, "bucket", "peadStatus", "stage", "status", default="") or "").lower()

    if any(token in f"{status} {source}" for token in (
        "postponed", "rescheduled", "deferred", "cancelled", "canceled",
        "date changed", "board meeting postponed",
    )):
        return {
            "released": False,
            "method": "POSTPONED_OR_RESCHEDULED",
            "confidence": "HIGH",
            "reason": "Result appears postponed/rescheduled; calendar auto-release was suppressed.",
            "resultDate": str(rd) if rd else None,
        }

    if any(token in source for token in (
        "financial results", "result announced", "results announced",
        "exchange filing", "nse filing", "bse filing",
        "nseindia.com", "bseindia.com",
    )):
        return {
            "released": True,
            "method": "SOURCE_EVIDENCE",
            "confidence": "HIGH",
            "reason": "Result release evidence is present in the source data.",
            "resultDate": str(rd) if rd else None,
        }

    if any(token in status for token in (
        "post-results", "post results", "results declared", "result declared",
        "results released", "result released", "qualified", "entry confirmed",
    )):
        return {
            "released": True,
            "method": "STATUS",
            "confidence": "HIGH",
            "reason": f"Base status indicates a released result: {status}.",
            "resultDate": str(rd) if rd else None,
        }

    if explicit is True:
        return {
            "released": True,
            "method": "EXPLICIT_FLAG",
            "confidence": "HIGH",
            "reason": "Base feed explicitly marks the result as released.",
            "resultDate": str(rd) if rd else None,
        }

    if rd is not None:
        age_days = (today - rd).days
        if age_days < 0:
            return {
                "released": False,
                "method": "FUTURE_RESULT_DATE",
                "confidence": "HIGH",
                "reason": f"Scheduled result date is still in the future: {rd}.",
                "resultDate": str(rd),
            }
        if age_days == 0:
            released = now.hour >= RESULT_DAY_AUTO_RELEASE_HOUR_IST
            return {
                "released": released,
                "method": "RESULT_DATE_TODAY_EVENING" if released else "RESULT_DATE_TODAY_WAIT",
                "confidence": "MEDIUM",
                "reason": (
                    "Scheduled result date is today and the evening release window has begun."
                    if released
                    else "Scheduled result date is today; waiting for evening/source confirmation."
                ),
                "resultDate": str(rd),
            }
        if age_days <= RESULT_DATE_RELEASE_LOOKBACK_DAYS:
            return {
                "released": True,
                "method": "RECENT_RESULT_DATE_PASSED",
                "confidence": "MEDIUM",
                "reason": f"Scheduled result date passed {age_days} day(s) ago; treating it as released pending source verification.",
                "resultDate": str(rd),
            }
        return {
            "released": False,
            "method": "STALE_RESULT_DATE",
            "confidence": "LOW",
            "reason": f"Result date is {age_days} days old and is not reused as current-quarter release evidence.",
            "resultDate": str(rd),
        }

    return {
        "released": False,
        "method": "EXPLICIT_NOT_RELEASED" if explicit is False else "NO_RELEASE_EVIDENCE",
        "confidence": "MEDIUM" if explicit is False else "LOW",
        "reason": (
            "Base feed marks the result as not released and no stronger evidence overrides it."
            if explicit is False
            else "No current result-release evidence is available."
        ),
        "resultDate": None,
    }


def normalize_symbol(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().upper()
    return s.replace("NSE:", "").strip()


def yahoo_symbol(symbol: str) -> str:
    if not symbol:
        return symbol
    if symbol.endswith((".NS", ".BO")):
        return symbol
    return f"{symbol}.NS"


def _extract_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("stocks") or raw.get("companies") or raw.get("data") or []
        if isinstance(rows, dict):
            rows = rows.get("stocks") or rows.get("companies") or []
    else:
        rows = []
    return [x for x in rows if isinstance(x, dict)]


def _row_key(row: dict[str, Any]) -> str:
    return normalize_symbol(pick(row, "symbol", "sym", "ticker", "code"))


def _largest_recent_git_universe(max_commits: int = 30) -> list[dict[str, Any]]:
    """Recover the last healthy data.json universe after an accidental scanner shrink.

    GitHub Actions normally has repository history available. If it is shallow or git
    is unavailable, this simply returns an empty list and normal scanning continues.
    """
    best: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(
            ["git", "rev-list", f"--max-count={max_commits}", "HEAD", "--", "data.json"],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
        )
        commits = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
        for sha in commits:
            shown = subprocess.run(
                ["git", "show", f"{sha}:data.json"],
                cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
            )
            if shown.returncode != 0 or not shown.stdout.strip():
                continue
            try:
                rows = _extract_rows(json.loads(shown.stdout))
            except Exception:
                continue
            if len(rows) > len(best):
                best = rows
    except Exception:
        return []
    return best


def load_companies() -> list[dict[str, Any]]:
    """Load the working universe without allowing a small seed file to erase it.

    Priority for field values is:
      previous healthy data.json -> current data.json -> companies.json.
    The union is by symbol, so the small manual seed can update rows while the wider
    live-discovery universe is preserved.
    """
    seed_rows: list[dict[str, Any]] = []
    if INPUT.exists():
        try:
            seed_rows = _extract_rows(json.loads(INPUT.read_text(encoding="utf-8")))
        except Exception:
            seed_rows = []

    current_rows: list[dict[str, Any]] = []
    if OUTPUT.exists():
        try:
            current_rows = _extract_rows(json.loads(OUTPUT.read_text(encoding="utf-8")))
        except Exception:
            current_rows = []

    historical_rows = _largest_recent_git_universe()

    merged: dict[str, dict[str, Any]] = {}
    for source_rows in (historical_rows, current_rows, seed_rows):
        for row in source_rows:
            key = _row_key(row)
            if not key:
                continue
            if key not in merged:
                merged[key] = dict(row)
            else:
                merged[key].update({k: v for k, v in row.items() if v not in (None, "")})

    rows = list(merged.values())
    if historical_rows and len(rows) > len(seed_rows):
        print(
            f"Universe recovery active: seed={len(seed_rows)} current={len(current_rows)} "
            f"historical_best={len(historical_rows)} merged={len(rows)}"
        )
    else:
        print(f"Universe loaded: seed={len(seed_rows)} current={len(current_rows)} merged={len(rows)}")

    return rows


def safe_market_cap_cr(ticker: yf.Ticker, last_price: float | None) -> float | None:
    market_cap = None
    try:
        fast = ticker.fast_info
        market_cap = as_float(fast.get("market_cap") if hasattr(fast, "get") else None)
    except Exception:
        market_cap = None

    if not market_cap:
        try:
            info = ticker.info or {}
            market_cap = as_float(info.get("marketCap"))
            if not market_cap and last_price:
                shares = as_float(info.get("sharesOutstanding"))
                if shares:
                    market_cap = shares * last_price
        except Exception:
            market_cap = None

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
    row.update(
        {
            "symbol": symbol,
            "sym": symbol,
            "resultDate": result_date,
            "result_date": result_date,
            "liveStatus": "unavailable",
            "liveError": None,
        }
    )

    if not symbol:
        row["liveError"] = "Missing symbol/sym in companies.json"
        return row

    ys = yahoo_symbol(symbol)
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

        row.update(
            {
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
            }
        )
    except Exception as exc:
        row["liveError"] = f"{type(exc).__name__}: {exc}"
        # Keep source market cap if present so a temporary quote failure does not
        # erase the row from the dashboard.
        source_mcap = as_float(pick(company, "marketCapCr", "mcapCr", "market_cap_cr"))
        if source_mcap is not None:
            row["marketCapCr"] = source_mcap

    mcap = as_float(row.get("marketCapCr"))
    row["marketCapPass"] = None if mcap is None else mcap >= MIN_MCAP_CR

    # Detect whether the tracked result has crossed from Upcoming -> Post-results.
    # A stale explicit False is intentionally allowed to be overridden by a recent
    # scheduled date that has passed, while old dates are bounded to avoid recycling
    # a prior quarter forever.
    detection = result_release_detection(row)
    row["resultsReleased"] = detection["released"] is True
    row["resultReleased"] = row["resultsReleased"]
    row["resultDetection"] = detection

    existing_status = str(pick(row, "peadStatus", "stage", "status", "bucket", default="") or "")
    existing_status_l = existing_status.lower()

    if row["resultsReleased"]:
        # Prevent an old Upcoming label from winning over the new release flag in the UI.
        if (
            not existing_status
            or any(token in existing_status_l for token in ("upcoming", "awaiting", "pre-result", "pre result"))
        ):
            row["peadStatus"] = "Post-results"
        if not pick(row, "resultsEvidence", "resultEvidence"):
            row["resultsEvidence"] = detection.get("reason")
    else:
        rd = parse_date(result_date)
        if rd is not None and rd >= datetime.now(IST).date() and (not existing_status or existing_status_l == "in review"):
            row["peadStatus"] = "Upcoming"
        elif not existing_status:
            row["peadStatus"] = "In Review"

    return row


def main() -> int:
    started = utc_now_iso()
    companies = load_companies()

    scanned: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for i, company in enumerate(companies):
        row = fetch_live(company)
        # Enforce ₹1,000 Cr only when market cap is actually known.
        # Unknown rows remain visible as unverified rather than being dropped.
        if row.get("marketCapPass") is not False:
            scanned.append(row)
        if row.get("liveError"):
            errors.append({"symbol": row.get("symbol", ""), "error": row["liveError"]})
        # Be kind to upstream quote endpoints.
        if i and i % 20 == 0:
            time.sleep(0.5)

    payload = {
        "generatedAt": started,
        "last_scan": started,
        "lastScanAt": started,
        "scannerMode": "live-discovery",
        "minMarketCapCr": MIN_MCAP_CR,
        "sourceCount": len(companies),
        "scanCount": len(scanned),
        "universeProtection": "git-history-recovery-v1",
        "errorCount": len(errors),
        "errors": errors,
        # Compatibility aliases: old and new frontends can both consume this.
        "companies": scanned,
        "stocks": scanned,
    }

    tmp = OUTPUT.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    tmp.replace(OUTPUT)

    print(f"PEAD scan complete: source={len(companies)} visible={len(scanned)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
