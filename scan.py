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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "companies.json"
OUTPUT = ROOT / "data.json"
MIN_MCAP_CR = float(os.getenv("MIN_MCAP_CR", "1000"))


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


def load_companies() -> list[dict[str, Any]]:
    with INPUT.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("companies") or raw.get("stocks") or raw.get("data") or []
    else:
        rows = []

    return [x for x in rows if isinstance(x, dict)]


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

    # Preserve strategy classifications already present in companies.json.
    # Only add a neutral status when the source has none.
    if not pick(row, "peadStatus", "stage", "status", "bucket"):
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
        "minMarketCapCr": MIN_MCAP_CR,
        "sourceCount": len(companies),
        "scanCount": len(scanned),
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
    
