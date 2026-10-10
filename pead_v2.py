#!/usr/bin/env python3
"""
PEAD Engine v2 — persistent Indian earnings-event engine.

Design goals
============
* Treat each earnings event as permanent state, keyed by security + fiscal period.
* Never overwrite a verified value with None or with a failed fetch.
* Save raw official result responses for replay/debugging.
* Keep a daily fetch log so endpoint failures are visible.
* Separate source data from derived PEAD calculations.
* Publish data.json/intelligence.json only after data-quality gates pass.
* Prefer exchange data; use Yahoo only as a fallback for price/valuation.

This file deliberately does NOT import scan.py, qualify.py, or pead_intelligence.py.
It can run beside the old system until the v2 workflow is proven stable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import pead_plus

# Optional third-party dependencies. Self-test and bootstrap remain usable even
# before the GitHub workflow installs the network packages.
try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

try:
    from nse import NSE
except Exception:  # pragma: no cover
    NSE = None

try:
    from bse import BSE
except Exception:  # pragma: no cover
    BSE = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
EVENTS_DIR = ROOT / "events"
RAW_DIR = ROOT / "raw"
LOG_DIR = ROOT / "logs"
MASTER_DIR = ROOT / "master"
DATA_PATH = ROOT / "data.json"
INTELLIGENCE_PATH = ROOT / "intelligence.json"
HEALTH_PATH = ROOT / "run_health.json"
SYMBOL_MASTER_PATH = MASTER_DIR / "symbols.json"
V1_MIGRATION_MARKER_PATH = MASTER_DIR / "v1_migration_complete.json"

SCHEMA_VERSION = "pead-event-v2.1"
ENGINE_VERSION = "2.9.5"

MIN_MCAP_CR = float(os.getenv("MIN_MCAP_CR", "1000"))
DISCOVERY_LOOKBACK_DAYS = int(os.getenv("DISCOVERY_LOOKBACK_DAYS", "75"))
UPCOMING_DAYS = int(os.getenv("UPCOMING_DAYS", "45"))
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "550"))
ACTIVE_ENRICH_DAYS = int(os.getenv("ACTIVE_ENRICH_DAYS", "75"))
ACTIVE_PERIOD_DAYS = int(os.getenv("ACTIVE_PERIOD_DAYS", "120"))
MAX_ACTIVE_ENRICH_EVENTS = int(os.getenv("MAX_ACTIVE_ENRICH_EVENTS", "120"))
DASHBOARD_RESULT_DAYS = int(os.getenv("DASHBOARD_RESULT_DAYS", "45"))
DASHBOARD_UPCOMING_DAYS = int(os.getenv("DASHBOARD_UPCOMING_DAYS", "45"))
PRICE_TRAIL_MAX_POINTS = int(os.getenv("PRICE_TRAIL_MAX_POINTS", "72"))
RECENT_RESULT_PRICE_REFRESH_HOURS = float(os.getenv("RECENT_RESULT_PRICE_REFRESH_HOURS", "1"))
SOURCE_RETRY_ATTEMPTS = int(os.getenv("SOURCE_RETRY_ATTEMPTS", "3"))
SOURCE_DELAY_SEC = float(os.getenv("SOURCE_DELAY_SEC", "1.25"))
HEAVY_UPCOMING_DAYS = int(os.getenv("HEAVY_UPCOMING_DAYS", "45"))   # = dashboard window (2.6.0)
VALUATION_UPCOMING_DAYS = int(os.getenv("VALUATION_UPCOMING_DAYS", "3"))
YAHOO_TIMEOUT_SEC = int(os.getenv("YAHOO_TIMEOUT_SEC", "8"))
YAHOO_NEGATIVE_TTL_HOURS = int(os.getenv("YAHOO_NEGATIVE_TTL_HOURS", "72"))
YAHOO_PRICE_BUDGET = int(os.getenv("YAHOO_PRICE_BUDGET", "40"))
YAHOO_FUNDAMENTALS_BUDGET = int(os.getenv("YAHOO_FUNDAMENTALS_BUDGET", "25"))
YAHOO_QUARTERLY_BUDGET = int(os.getenv("YAHOO_QUARTERLY_BUDGET", "25"))
YAHOO_NEGATIVE_CACHE_PATH = MASTER_DIR / "yahoo_negative_cache.json"
PENDING_INTEGRITY_PATH = MASTER_DIR / "pending_integrity.json"
INDEX_CACHE_PATH = MASTER_DIR / "index_nifty500.json"
EXCHANGE_META_REFRESH_HOURS = float(os.getenv("EXCHANGE_META_REFRESH_HOURS", "20"))
# New (engine 2.4) per-event calls are budgeted per run so hourly runs stay
# short; the backlog fills over the first few runs.
EXTRA_CALL_BUDGET = int(os.getenv("EXTRA_CALL_BUDGET", "120"))
_EXTRA_CALLS_USED = {"n": 0}


def _extra_budget_ok(cost: int = 1) -> bool:
    if _EXTRA_CALLS_USED["n"] + cost > EXTRA_CALL_BUDGET:
        return False
    _EXTRA_CALLS_USED["n"] += cost
    return True

# Per-run caches/budgets prevent repeated calls for the same security across
# several quarterly events. The persistent negative cache prevents known Yahoo
# 404/delisted tickers from consuming time every hour.
_YAHOO_NEGATIVE_CACHE: dict[str, dict[str, Any]] | None = None
_YAHOO_HISTORY_CACHE: dict[tuple[str, str], Any] = {}
_YAHOO_FUNDAMENTALS_CACHE: dict[str, dict[str, Any]] = {}
_YAHOO_QUARTERLY_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_YAHOO_BUDGET_USED = {"price": 0, "fundamentals": 0, "quarterly": 0}

# Core PEAD thresholds. They are intentionally compact; secondary metrics modify
# conviction but do not block a valid PEAD event from existing.
REV_YOY_STRONG = 10.0
PAT_YOY_STRONG = 15.0
PRICED_IN_RUNUP_PCT = 15.0
LOW_EXPECTATION_RUNUP_PCT = 5.0
RVOL_CONFIRM = 1.20
RESULT_RETURN_CONFIRM = 2.0
LIQUIDITY_TURNOVER_CR_MIN = 5.0

STATE_ORDER = {
    "DISCOVERED": 0,
    "SCHEDULED": 1,
    "RESULT_FILED": 2,
    "FINANCIALS_PARSED": 3,
    "REACTION_PENDING": 4,
    "REACTION_READY": 5,
    "PEAD_SCORED": 6,
    "ENTRY_WATCH": 7,
}

# General source quality. Field-specific precedence is applied by source_rank().
BASE_SOURCE_RANK = {
    "NSE_XBRL": 100,
    "BSE_XBRL": 100,
    "NSE_FINANCIAL_RESULTS": 98,
    "NSE_INTEGRATED_FILING": 98,
    "BSE_RESULT_ANNOUNCEMENT": 98,
    "NSE_RESULTS_COMPARISON": 95,
    "BSE_RESULTS_SNAPSHOT": 92,
    "NSE_PRICE": 95,
    "BSE_PRICE": 95,
    "EXCHANGE_IDENTITY": 95,
    "NSE_QUOTE": 90,
    "BSE_META": 88,
    "SCREENER_FALLBACK": 60,
    "YAHOO_QUARTERLY": 55,
    "YAHOO_PRICE": 55,
    "YAHOO_FUNDAMENTALS": 50,
    "V1_MIGRATION": 20,
    "DERIVED": 10,
}

FINANCIAL_FIELDS = {
    "revenue_cr", "pat_cr", "eps", "revenue_yoy_pct", "pat_yoy_pct",
    "revenue_qoq_pct", "pat_qoq_pct", "pat_qoq_trend", "pat_trend", "basis",
    "prior_year_revenue_cr", "prior_year_pat_cr",
    "opm_pct", "opm_prev_q_pct", "opm_prior_year_pct", "margin_change_bps",
    "prev_q_revenue_yoy_pct", "prev_q_pat_yoy_pct",
}
EXCHANGE_META_FIELDS = {
    "exchange_pe", "sector_pe", "pb", "roe_pct", "market_cap_cr", "ffmc_cr",
    "delivery_pct", "exchange_opm_ttm_pct",
}
PRICE_FIELDS = {
    "pre_result_5d_pct", "pre_result_10d_pct", "pre_result_20d_pct",
    "result_day_return_pct", "result_day_rvol", "distance_52w_high_pct",
    "result_day_low", "result_day_high", "post_result_hold_5d",
    "post_result_hold_10d", "box_high", "box_breakout", "last_price",
    "avg_turnover_20d_cr",
}


def source_rank(field: str, source: str) -> int:
    """Return field-aware source precedence."""
    source = (source or "").upper()
    rank = BASE_SOURCE_RANK.get(source, 0)

    if field in FINANCIAL_FIELDS:
        if source.endswith("XBRL"):
            return 110
        if source == "NSE_RESULTS_COMPARISON":
            return 100
        if source == "BSE_RESULTS_SNAPSHOT":
            return 96
        if source in {"YAHOO_QUARTERLY", "SCREENER_FALLBACK"}:
            return 55
    if field in PRICE_FIELDS:
        if source in {"NSE_PRICE", "BSE_PRICE"}:
            return 105
        if source == "YAHOO_PRICE":
            return 60
    if field in {"results_released", "filing_timestamp", "result_date"}:
        if source in {"NSE_FINANCIAL_RESULTS", "NSE_INTEGRATED_FILING", "BSE_RESULT_ANNOUNCEMENT"}:
            return 110
    if field in {"isin", "nse_symbol", "bse_code"}:
        if source in {"NSE_FINANCIAL_RESULTS", "BSE_RESULT_ANNOUNCEMENT", "EXCHANGE_IDENTITY"}:
            return 105
    return rank


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ist() -> datetime:
    return datetime.now(IST)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_num(value: Any) -> float | None:
    if value in (None, "", "—", "--", "NA", "N/A"):
        return None
    try:
        x = float(str(value).replace(",", "").replace("₹", "").replace("%", "").strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def round2(value: Any) -> float | None:
    x = safe_num(value)
    return round(x, 2) if x is not None else None


def boolish(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    if s in {"true", "yes", "1", "pass", "passed", "released", "declared", "ok", "green"}:
        return True
    if s in {"false", "no", "0", "fail", "failed", "pending", "upcoming", "red"}:
        return False
    return None


def first(d: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    lowered = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def normalize_symbol(value: Any) -> str:
    if value in (None, ""):
        return ""
    s = str(value).upper().strip()
    s = s.replace("NSE:", "").replace("BSE:", "")
    s = re.sub(r"\.(NS|BO)$", "", s)
    return re.sub(r"[^A-Z0-9&_-]", "", s)


_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _parse_iso(s: str) -> datetime | None:
    """Parse ISO-8601 strings (YYYY-MM-DD[THH:MM:SS[.fff][tz]]) without ever
    applying day-first heuristics.

    Regression: BSE DT_TM values such as "2026-09-11T16:08:22.73" were sent to
    pandas with dayfirst=True and came back as 2026-11-09. That single bug moved
    September Q1 filings into the future and into the Q2 FY27 live quarter.
    """
    if not _ISO_PREFIX.match(s):
        return None
    t = s.replace("Z", "+00:00")
    # Python < 3.11 fromisoformat rejects fractional seconds that are not 3/6
    # digits, so normalise them first ("22.73" -> "22.730000").
    m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", t)
    if m:
        t = f"{m.group(1)}.{(m.group(2) + '000000')[:6]}{m.group(3)}"
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    iso = _parse_iso(s)
    if iso is not None:
        return iso.date()
    if _ISO_PREFIX.match(s):
        # Looks ISO but is malformed: never guess with day-first rules.
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    for fmt in (
        "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y",
        "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d %b %Y", "%d %B %Y",
        "%b-%y", "%b %Y", "%b-%Y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    if pd is not None:
        try:
            # Only non-ISO, human formats reach this point (e.g. "11-Oct-2026").
            ts = pd.to_datetime(s, errors="coerce", dayfirst=True)
            if not pd.isna(ts):
                return ts.date()
        except Exception:
            pass
    return None


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, dtime(12, 0))
    else:
        s = str(value).strip()
        dt = _parse_iso(s) if len(s) > 10 else None
        if dt is None and _ISO_PREFIX.match(s):
            d = parse_date(s)
            dt = datetime.combine(d, dtime(12, 0)) if d else None
            if dt is None:
                return None
        if dt is None:
            for fmt in (
                "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d/%m/%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M",
            ):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    pass
        if dt is None:
            d = parse_date(s)
            if d is None:
                return None
            dt = datetime.combine(d, dtime(12, 0))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def fiscal_quarter(period_end: date | None) -> str | None:
    if period_end is None:
        return None
    if period_end.month in {4, 5, 6}:
        q = 1
    elif period_end.month in {7, 8, 9}:
        q = 2
    elif period_end.month in {10, 11, 12}:
        q = 3
    else:
        q = 4
    fy_end = period_end.year + 1 if period_end.month >= 4 else period_end.year
    return f"Q{q} FY{str(fy_end)[-2:]}"


def expected_period_end(event_date: date | None) -> date | None:
    """Best-effort quarter end corresponding to a result event date."""
    if event_date is None:
        return None
    candidates = []
    for y in (event_date.year - 1, event_date.year):
        for m, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = date(y, m, day)
            if d <= event_date:
                candidates.append(d)
    if not candidates:
        return None
    # Results are normally filed within ~90 days after period end. Prefer the
    # most recent quarter end that is at least a week before the event date.
    plausible = [d for d in candidates if 7 <= (event_date - d).days <= 120]
    return max(plausible) if plausible else max(candidates)


def live_reporting_period(today: date | None = None) -> date | None:
    """Fiscal period that belongs on the live PEAD dashboard.

    Example: on/after 07 Oct 2026 the live reporting period is 30 Sep 2026
    (Q2 FY27). Older quarters remain in the permanent event store but are not
    mixed into the live current-quarter radar.
    """
    return expected_period_end(today or now_ist().date())


def is_live_reporting_period(event: dict[str, Any], today: date | None = None) -> bool:
    target = live_reporting_period(today)
    if target is None:
        return True

    period_end = parse_date(event.get("period", {}).get("end"))
    if period_end is not None:
        return period_end == target

    result_date = parse_date(meta_value(event, "result_date"))
    inferred = expected_period_end(result_date) if result_date is not None else None
    return inferred == target


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def pat_trend(current: float | None, prior: float | None) -> tuple[str | None, float | None]:
    """Classify PAT direction without nonsensical percentages on <=0 bases."""
    if current is None or prior is None:
        return None, None
    if prior <= 0 < current:
        return "TURNAROUND", None
    if prior > 0 and current < 0:
        return "DETERIORATION", None
    if prior < 0 and current < 0:
        if abs(current) < abs(prior):
            return "LOSS_NARROWING", None
        if abs(current) > abs(prior):
            return "LOSS_WIDENING", None
        return "LOSS_FLAT", None
    if prior == 0:
        if current > 0:
            return "TURNAROUND", None
        if current < 0:
            return "DETERIORATION", None
        return "FLAT", None
    yoy = pct_change(current, prior)
    if yoy is None:
        return None, None
    return ("PROFIT_GROWTH" if yoy >= 0 else "PROFIT_DECLINE"), yoy


def nonzero(value: Any) -> float | None:
    """Exchange placeholders: an exact 0 for a ratio means 'not available'."""
    x = safe_num(value)
    return None if x is None or x == 0 else x


def pat_change_pct(current: float | None, prior: float | None) -> float | None:
    """Ordinary PAT % change only when both quarters are profits (2.9.4:
    HATHWAYB 0.04 -> -0.10 Cr was published as "-350.0% profit")."""
    return pat_trend(current, prior)[1]


def load_holidays() -> set[date]:
    path = MASTER_DIR / "nse_holidays.json"
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        values = raw if isinstance(raw, list) else raw.get("holidays", [])
        return {d for d in (parse_date(x) for x in values) if d is not None}
    except Exception:
        return set()


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in load_holidays()


def next_trading_day(d: date) -> date:
    x = d + timedelta(days=1)
    for _ in range(12):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    return x


def reaction_session(filing_ts: datetime | None, fallback_date: date | None) -> tuple[date | None, str]:
    """Resolve the market session that can first react to the filing."""
    if filing_ts is not None:
        local = filing_ts.astimezone(IST)
        d = local.date()
        if not is_trading_day(d):
            return next_trading_day(d), "NON_TRADING_DAY"
        t = local.time()
        if t < dtime(9, 0):
            return d, "BEFORE_OPEN"
        if t <= dtime(15, 30):
            # Only part of the filing day can react: the reaction is measured
            # over a two-session window (filing day + next session) and the
            # next session is the "reaction day" for entries and holds.
            return next_trading_day(d), "INTRADAY"
        return next_trading_day(d), "AFTER_CLOSE"
    if fallback_date is not None:
        # No timestamp = do not pretend the same-day candle is clean. Use the
        # next session and label the timing unknown so the UI can disclose it.
        return next_trading_day(fallback_date), "UNKNOWN_TIME_NEXT_SESSION"
    return None, "UNKNOWN"


def session_closed(d: date | None) -> bool:
    """A trading session counts only after its close (15:30 IST + 15 min for
    EOD data). Before 2.6.2 the reaction day counted from midnight, so at
    6 am the card said "Data pending" instead of "Awaiting reaction session"."""
    if d is None:
        return False
    now = now_ist()
    return d < now.date() or (d == now.date() and (now.hour, now.minute) >= (15, 45))


def reaction_window_start(filing_ts: datetime | None, timing: str | None, reaction: date | None) -> date | None:
    """First session of the reaction window: the filing day for intraday
    filings, otherwise the reaction session itself."""
    if timing == "INTRADAY" and filing_ts is not None:
        return filing_ts.astimezone(IST).date()
    return reaction


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown")
    return value[:180]


def event_id_for(security_key: str, period_end: date | None) -> str:
    period = period_end.isoformat() if period_end else "UNKNOWN_PERIOD"
    return f"{security_key}|{period}"


def security_key_from_values(isin: Any = None, nse_symbol: Any = None, bse_code: Any = None, symbol: Any = None) -> str:
    isin_s = str(isin or "").strip().upper()
    if re.fullmatch(r"IN[A-Z0-9]{10}", isin_s):
        return isin_s
    ns = normalize_symbol(nse_symbol or symbol)
    if ns:
        return f"NSE:{ns}"
    bc = str(bse_code or "").strip()
    if bc.isdigit():
        return f"BSE:{bc}"
    return f"SYM:{normalize_symbol(symbol) or 'UNKNOWN'}"


def json_dump_atomic(path: Path, payload: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = (json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) if compact
            else json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Raw cache and fetch log
# ---------------------------------------------------------------------------


class RawCache:
    def __init__(self, root: Path = RAW_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, source: str, event_id: str, endpoint: str, payload: Any, *, suffix: str = "json") -> str:
        if isinstance(payload, bytes):
            blob = payload
        elif isinstance(payload, str):
            blob = payload.encode("utf-8", errors="replace")
        else:
            blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()[:16]
        folder = self.root / safe_filename(source.lower()) / safe_filename(event_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{safe_filename(endpoint)}-{digest}.{suffix}"
        if not path.exists():
            path.write_bytes(blob)
        return str(path.relative_to(ROOT))


class FetchLogger:
    def __init__(self, root: Path = LOG_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        *,
        source: str,
        endpoint: str,
        status: str,
        event_id: str | None = None,
        symbol: str | None = None,
        error: str | None = None,
        http_status: int | None = None,
        raw_ref: str | None = None,
        elapsed_ms: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        day = now_ist().date().isoformat()
        path = self.root / f"fetch_{day}.jsonl"
        record = {
            "at": iso_now(),
            "source": source,
            "endpoint": endpoint,
            "status": status,
            "eventId": event_id,
            "symbol": symbol,
            "httpStatus": http_status,
            "error": error,
            "rawRef": raw_ref,
            "elapsedMs": elapsed_ms,
        }
        if extra:
            record.update(extra)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Persistent event store
# ---------------------------------------------------------------------------


def _identity_conflict(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """True when an incoming security clearly is a different company from the
    event its key resolved to (different BSE code / ISIN, or unrelated names)."""
    ia, ib = str(existing.get("isin") or "").upper(), str(incoming.get("isin") or "").upper()
    if ia and ib and (ia == ib or (_issuer_code(ia) and _issuer_code(ia) == _issuer_code(ib))):
        return False
    for k in ("bseCode", "isin"):
        a, b = str(existing.get(k) or "").strip().upper(), str(incoming.get(k) or "").strip().upper()
        if a and b and a != b and not (k == "isin" and b in [str(x).upper() for x in existing.get("altIsins") or []]):
            if k == "isin" and _issuer_code(a) and _issuer_code(a) == _issuer_code(b):
                continue
            return True
    na, nb = existing.get("name"), incoming.get("name")
    if (na and nb and not ib and normalize_symbol(nb) != normalize_symbol(incoming.get("symbol"))
            and not same_company({"name": na}, {"name": nb})):
        return True
    return False


class EventStore:
    def __init__(self, root: Path = EVENTS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, event_id: str) -> Path:
        return self.root / f"{safe_filename(event_id)}.json"

    # Merged duplicate ids point at the surviving event (engine 2.5.3), so
    # discovery under an old NSE:/second-ISIN key reuses it instead of
    # re-creating the duplicate every run. Stored as events/_aliases.map
    # (not *.json, so all() never treats it as an event).
    def _alias_path(self) -> Path:
        return self.root / "_aliases.map"

    def aliases(self) -> dict[str, str]:
        if not hasattr(self, "_aliases"):
            try:
                obj = json.loads(self._alias_path().read_text(encoding="utf-8"))
                self._aliases = obj if isinstance(obj, dict) else {}
            except Exception:
                self._aliases = {}
        return self._aliases

    def resolve(self, event_id: str) -> str:
        a, seen, start = self.aliases(), set(), event_id
        # 2.9.1: aliases are left by deleted (rekeyed/folded) files, so an id
        # whose own file exists is a live event and is never redirected
        # ("NSE:BRIGHT" -> "NSE:BCG" was left by the 2.5.4 identity repair).
        if event_id in a and self._path(event_id).exists():
            return event_id
        while event_id in a and event_id not in seen:
            seen.add(event_id)
            event_id = a[event_id]
        # A dangling alias (target deleted/rekeyed) must not resurrect the
        # old target id; fall back to the id that was asked for.
        if event_id != start and not self._path(event_id).exists():
            return start
        return event_id

    def add_alias(self, old_id: str, new_id: str) -> None:
        if old_id and new_id and old_id != new_id:
            self.aliases()[old_id] = new_id
            json_dump_atomic(self._alias_path(), self._aliases)

    def load(self, event_id: str) -> dict[str, Any] | None:
        path = self._path(self.resolve(event_id))
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def save(self, event: dict[str, Any]) -> None:
        event["schemaVersion"] = SCHEMA_VERSION
        event["updatedAt"] = iso_now()
        event.setdefault("createdAt", event["updatedAt"])
        json_dump_atomic(self._path(event["eventId"]), event)

    def all(self) -> list[dict[str, Any]]:
        events = []
        for path in sorted(self.root.glob("*.json")):
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and obj.get("eventId"):
                    events.append(obj)
            except Exception:
                continue
        return events

    def ensure_event(
        self,
        *,
        security: dict[str, Any],
        period_end: date | None,
        quarter: str | None = None,
    ) -> dict[str, Any]:
        security_key = security_key_from_values(
            security.get("isin"), security.get("nseSymbol"), security.get("bseCode"), security.get("symbol")
        )
        eid = self.resolve(event_id_for(security_key, period_end))
        event = self.load(eid)
        if event is not None and _identity_conflict(event.get("security") or {}, security):
            # 2.6.1: a BSE scrip id that equals another company's NSE symbol
            # (BRIGHT = Bright Outdoor Media on BSE, Bright Solar on NSE) must
            # not land in that company's event; key it by its BSE code.
            code = str(security.get("bseCode") or "").strip()
            alt = f"BSE:{code}" if code.isdigit() else f"SYM:{normalize_symbol(security.get('symbol'))}:{normalize_symbol(security.get('name'))}"
            alt_id = event_id_for(alt, period_end)
            eid = self.resolve(alt_id)
            event = self.load(eid)
            # 2.9.1: a stale alias (BSE:543831 -> NSE:BRIGHT) must not lead
            # back into the namesake; drop it and use the BSE key itself.
            if event is not None and _identity_conflict(event.get("security") or {}, security):
                self.aliases().pop(alt_id, None)
                json_dump_atomic(self._alias_path(), self.aliases())
                eid, event = alt_id, None
                if self._path(alt_id).exists():
                    event = json.loads(self._path(alt_id).read_text(encoding="utf-8"))
        if event is None:
            event = {
                "schemaVersion": SCHEMA_VERSION,
                "eventId": eid,
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
                "security": {},
                "period": {
                    "end": period_end.isoformat() if period_end else None,
                    "quarter": quarter or fiscal_quarter(period_end),
                },
                "state": "DISCOVERED",
                "stateHistory": [{"state": "DISCOVERED", "at": iso_now(), "reason": "event created"}],
                "fields": {},
                "fetch": {},
                "derived": {},
            }
        self.merge_security(event, security)
        if period_end and not event.get("period", {}).get("end"):
            event.setdefault("period", {})["end"] = period_end.isoformat()
        if quarter and not event.get("period", {}).get("quarter"):
            event.setdefault("period", {})["quarter"] = quarter
        return event

    def merge_security(self, event: dict[str, Any], incoming: dict[str, Any]) -> None:
        sec = event.setdefault("security", {})
        for key, value in incoming.items():
            if value not in (None, ""):
                # Identity fields are non-destructive; later verified exchange
                # values can fill blanks but not erase earlier mappings.
                if sec.get(key) in (None, "") or key in {"name", "sector", "industry"}:
                    sec[key] = value
        # Convenience canonical symbol.
        if not sec.get("symbol"):
            sec["symbol"] = normalize_symbol(sec.get("nseSymbol") or sec.get("yahooTicker") or sec.get("bseSymbol"))

    def rekey_and_merge(self, event: dict[str, Any]) -> dict[str, Any]:
        """Move an event to its strongest identity (prefer ISIN) and merge duplicates."""
        sec = event.get("security", {})
        period_end = parse_date(event.get("period", {}).get("end"))
        new_id = event_id_for(
            security_key_from_values(
                sec.get("isin"), sec.get("nseSymbol"), sec.get("bseCode"), sec.get("symbol")
            ),
            period_end,
        )
        old_id = event.get("eventId")
        if not old_id or new_id == old_id:
            return event

        target = self.load(new_id)
        if target is None:
            old_path = self._path(old_id)
            event["eventId"] = new_id
            self.save(event)
            if old_path.exists():
                old_path.unlink()
            self.add_alias(old_id, new_id)
            return event

        return self.fold_into(target, event)

    def fold_into(self, target: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        """Merge duplicate `event` into `target` (normal source precedence),
        delete the duplicate file and leave an alias to the target."""
        old_id = event.get("eventId")
        sec = event.get("security") or {}
        tsec = target.setdefault("security", {})
        if sec.get("isin") and tsec.get("isin") and sec["isin"] != tsec["isin"]:
            alt = tsec.setdefault("altIsins", [])
            if sec["isin"] not in alt:
                alt.append(sec["isin"])
        incoming = {}
        for k, v in sec.items():
            if k in {"isin", "altIsins"} and tsec.get("isin"):
                continue
            # merge_security lets name/sector/industry overwrite; a duplicate
            # must not replace a real sector with a placeholder like "—".
            if k in {"name", "sector", "industry"} and str(tsec.get(k) or "").strip() not in {"", "—", "-"}:
                continue
            incoming[k] = v
        self.merge_security(target, incoming)
        for extra in ("filings", "financialSnapshots", "priceTrail", "concall", "tradeLog"):
            if event.get(extra) and not target.get(extra):
                target[extra] = event[extra]
        if (event.get("plus") or {}).get("price") and not (target.get("plus") or {}).get("price"):
            target["plus"] = event["plus"]
        # Raw price/filing folders stay under the old id; remember it so
        # replays still find them.
        merged_from = target.setdefault("mergedFrom", [])
        for oid in [old_id, *(event.get("mergedFrom") or [])]:
            if oid and oid != target.get("eventId") and oid not in merged_from:
                merged_from.append(oid)
        for field, meta in (event.get("fields") or {}).items():
            if not isinstance(meta, dict) or meta.get("status") != "OK":
                continue
            self.merge_field(
                target, field, meta.get("value"),
                source=str(meta.get("source") or "V1_MIGRATION"),
                fetched_at=meta.get("fetchedAt"), raw_ref=meta.get("rawRef"), note=meta.get("note"),
            )
        # Keep the furthest reached state and all source health.
        if STATE_ORDER.get(event.get("state", "DISCOVERED"), 0) > STATE_ORDER.get(target.get("state", "DISCOVERED"), 0):
            target["state"] = event.get("state")
        target.setdefault("stateHistory", []).extend(event.get("stateHistory") or [])
        target_fetch = target.setdefault("fetch", {})
        for key, val in (event.get("fetch") or {}).items():
            old = target_fetch.get(key) or {}
            if str((val or {}).get("lastAttempt") or "") >= str(old.get("lastAttempt") or ""):
                target_fetch[key] = val
        self.save(target)
        old_path = self._path(old_id)
        if old_path.exists() and old_id != target.get("eventId"):
            old_path.unlink()
        self.add_alias(old_id, target.get("eventId"))
        return target

    def merge_field(
        self,
        event: dict[str, Any],
        field: str,
        value: Any,
        *,
        source: str,
        fetched_at: str | None = None,
        raw_ref: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Non-null, source-aware merge. Failed/missing fetches never erase data."""
        if value is None or value == "":
            return False
        fields = event.setdefault("fields", {})
        existing = fields.get(field)
        fetched_at = fetched_at or iso_now()
        new_rank = source_rank(field, source)

        replace = existing is None
        if isinstance(existing, dict):
            old_rank = int(existing.get("rank") or source_rank(field, str(existing.get("source") or "")))
            old_time = str(existing.get("fetchedAt") or "")
            if new_rank > old_rank:
                replace = True
            elif new_rank == old_rank and fetched_at >= old_time:
                replace = True
            else:
                replace = False

        if replace:
            fields[field] = {
                "value": value,
                "source": source,
                "fetchedAt": fetched_at,
                "status": "OK",
                "rank": new_rank,
                "rawRef": raw_ref,
                "note": note,
            }
            return True
        return False

    def force_field(
        self,
        event: dict[str, Any],
        field: str,
        value: Any,
        *,
        source: str,
        raw_ref: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Authoritative write used only by integrity logic (atomic financial
        snapshot selection, evidence re-verification). Unlike merge_field it may
        replace a higher-ranked value that was proven wrong or belongs to a
        different snapshot, and it keeps the previous value in history."""
        if value is None or value == "":
            return False
        fields = event.setdefault("fields", {})
        existing = fields.get(field)
        if isinstance(existing, dict) and existing.get("status") == "OK" and existing.get("value") == value and existing.get("source") == source:
            # 2.9.4: same value from another document of the same source (GMBREW
            # standalone vs consolidated XBRL): keep provenance true to the
            # document actually applied, without a history entry.
            if raw_ref and (existing.get("rawRef") != raw_ref or (note and existing.get("note") != note)):
                existing["rawRef"] = raw_ref
                if note:
                    existing["note"] = note
                return True
            return False
        if isinstance(existing, dict):
            hist = event.setdefault("fieldHistory", {}).setdefault(field, [])
            hist.append({k: existing.get(k) for k in ("value", "source", "fetchedAt", "status", "note")} | {"replacedAt": iso_now()})
            del hist[:-5]
        fields[field] = {
            "value": value,
            "source": source,
            "fetchedAt": iso_now(),
            "status": "OK",
            "rank": source_rank(field, source),
            "rawRef": raw_ref,
            "note": note,
        }
        return True

    def revoke_field(self, event: dict[str, Any], field: str, reason: str) -> bool:
        """Withdraw a value that is proven invalid (not merely missing).

        The value is kept for audit with status REVOKED; meta/value readers
        treat it as absent so the UI shows —. Failed fetches never call this.
        """
        meta = (event.get("fields") or {}).get(field)
        if not isinstance(meta, dict) or meta.get("status") != "OK":
            return False
        meta["status"] = "REVOKED"
        meta["revokedAt"] = iso_now()
        meta["revokedReason"] = reason
        return True

    def force_state(self, event: dict[str, Any], state: str, reason: str) -> None:
        """Move state backwards when the evidence that advanced it was invalid."""
        if event.get("state") == state:
            return
        event["state"] = state
        event.setdefault("stateHistory", []).append({"state": state, "at": iso_now(), "reason": reason})

    def record_fetch(
        self,
        event: dict[str, Any],
        source: str,
        *,
        ok: bool,
        error: str | None = None,
        raw_ref: str | None = None,
    ) -> None:
        fetch = event.get("fetch")
        if not isinstance(fetch, dict):
            fetch = {}
            event["fetch"] = fetch
        previous = fetch.get(source) or {}
        fetch[source] = {
            "lastAttempt": iso_now(),
            "lastSuccess": iso_now() if ok else previous.get("lastSuccess"),
            "status": "OK" if ok else "FAILED",
            "error": None if ok else error,
            "rawRef": raw_ref if raw_ref else previous.get("rawRef"),
        }

    def value(self, event: dict[str, Any], field: str, default: Any = None) -> Any:
        meta = event.get("fields", {}).get(field)
        if isinstance(meta, dict) and meta.get("status") == "OK":
            return meta.get("value", default)
        return default

    def set_state(self, event: dict[str, Any], state: str, reason: str) -> None:
        old = event.get("state") or "DISCOVERED"
        # Allow forward progression freely. Backward state is allowed only when
        # the old state was derived prematurely; with persistent fields this is rare.
        if old == state:
            return
        if STATE_ORDER.get(state, 0) >= STATE_ORDER.get(old, 0):
            event["state"] = state
            event.setdefault("stateHistory", []).append({"state": state, "at": iso_now(), "reason": reason})


# ---------------------------------------------------------------------------
# Symbol master
# ---------------------------------------------------------------------------


class SymbolMaster:
    def __init__(self, path: Path = SYMBOL_MASTER_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"schemaVersion": 1, "updatedAt": iso_now(), "securities": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
                    self.data.setdefault("securities", {})
            except Exception:
                pass

    def merge(self, security: dict[str, Any]) -> str:
        key = security_key_from_values(
            security.get("isin"), security.get("nseSymbol"), security.get("bseCode"), security.get("symbol")
        )
        current = self.data["securities"].setdefault(key, {})
        for k, v in security.items():
            if v not in (None, ""):
                if current.get(k) in (None, "") or k in {"name", "sector", "industry"}:
                    current[k] = v
        current["securityKey"] = key
        current["updatedAt"] = iso_now()
        return key

    def save(self) -> None:
        self.data["updatedAt"] = iso_now()
        json_dump_atomic(self.path, self.data)


# ---------------------------------------------------------------------------
# Resilient source wrappers
# ---------------------------------------------------------------------------


@dataclass
class SourceContext:
    raw: RawCache
    log: FetchLogger


def _exc_status(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


EXPLICIT_PERIOD_SOURCES = frozenset({"FILING_TEXT", "EXCHANGE_PERIOD_FIELD"})


# Run guard (2.9.4). The 23:58 run on 9 Oct hung for 60 minutes and was
# killed by the workflow timeout, losing the hour: when NSE stops answering,
# every call waits out the library's own retries (5 x 15 s + backoff) times
# ours. A host that keeps failing is switched off for the rest of the run, and
# once the run's network budget is spent remaining calls are skipped, so the
# run still saves the store and publishes what it has.
RUN_BUDGET_SEC = float(os.getenv("RUN_BUDGET_MIN", "40")) * 60
SOURCE_BREAKER_ATTEMPTS = int(os.getenv("SOURCE_BREAKER_ATTEMPTS", "12"))
SOURCE_BREAKER_FAIL_SEC = float(os.getenv("SOURCE_BREAKER_FAIL_SEC", "300"))


class SourceSkipped(RuntimeError):
    """Raised instead of calling a source that is down or after the run budget."""


class RunGuard:
    def __init__(self) -> None:
        self.reset()

    def reset(self, started: float | None = None) -> None:
        self.started = time.monotonic() if started is None else started
        self.streak: dict[str, int] = {}
        self.fail_sec: dict[str, float] = {}
        self.down: dict[str, str] = {}
        self.skipped: dict[str, int] = {}
        self.budget_hit = False

    @staticmethod
    def host(source: str | None) -> str:
        s = str(source or "").upper()
        for h in ("NSE", "BSE", "YAHOO"):
            if s.startswith(h):
                return h
        return s or "OTHER"

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self, source: str | None) -> None:
        h = self.host(source)
        if self.elapsed() >= RUN_BUDGET_SEC:
            self.budget_hit = True
            self.skipped[h] = self.skipped.get(h, 0) + 1
            raise SourceSkipped(f"run budget of {RUN_BUDGET_SEC / 60:.0f} min spent")
        if h in self.down:
            self.skipped[h] = self.skipped.get(h, 0) + 1
            raise SourceSkipped(f"{h} switched off for this run: {self.down[h]}")

    @staticmethod
    def outage_like(exc: Exception) -> bool:
        """A missing page (404 etc.) is an answer, not an outage."""
        status = _exc_status(exc)
        if status is None:
            return True
        return status in (401, 403, 429) or status >= 500

    def failed(self, source: str | None, exc: Exception, seconds: float) -> None:
        if not self.outage_like(exc):
            return
        h = self.host(source)
        self.streak[h] = self.streak.get(h, 0) + 1
        self.fail_sec[h] = self.fail_sec.get(h, 0.0) + max(0.0, seconds)
        if h not in self.down and (self.streak[h] >= SOURCE_BREAKER_ATTEMPTS
                                   or self.fail_sec[h] >= SOURCE_BREAKER_FAIL_SEC):
            self.down[h] = (f"{self.streak[h]} failures in a row, {self.fail_sec[h]:.0f} s spent failing; "
                            f"last: {type(exc).__name__}")
            print(f"[run guard] {h} switched off for the rest of this run ({self.down[h]})")

    def ok(self, source: str | None) -> None:
        self.streak[self.host(source)] = 0

    def summary(self) -> dict[str, Any]:
        return {"elapsedMin": round(self.elapsed() / 60, 1), "budgetMin": round(RUN_BUDGET_SEC / 60),
                "budgetHit": self.budget_hit, "sourcesDown": dict(self.down),
                "skippedCalls": dict(self.skipped)}


RUN_GUARD = RunGuard()


def with_retry(fn, *, attempts: int = SOURCE_RETRY_ATTEMPTS, base_delay: float = SOURCE_DELAY_SEC,
               source: str | None = None):
    last = None
    for attempt in range(1, attempts + 1):
        if source is not None:
            RUN_GUARD.check(source)
        started = time.monotonic()
        try:
            result = fn()
            if source is not None:
                RUN_GUARD.ok(source)
            return result
        except Exception as exc:
            last = exc
            if source is not None:
                RUN_GUARD.failed(source, exc, time.monotonic() - started)
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.05, 0.45)
            time.sleep(delay)
    assert last is not None
    raise last


class NSEAdapter:
    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.client = None

    def __enter__(self):
        if NSE is None:
            raise RuntimeError("nse package unavailable")
        kwargs_variants = [
            {"download_folder": str(ROOT), "use_http2": True},
            {"download_folder": str(ROOT), "server": True},
            {"download_folder": str(ROOT)},
        ]
        last = None
        for kwargs in kwargs_variants:
            try:
                self.client = NSE(**kwargs)
                return self
            except TypeError as exc:
                last = exc
                continue
        raise last or RuntimeError("unable to initialize NSE client")

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client is not None:
                self.client.exit()
        except Exception:
            pass

    def _call(self, endpoint: str, fn, *, event_id: str | None = None, symbol: str | None = None, raw_source: str = "NSE"):
        started = time.perf_counter()
        try:
            payload = with_retry(fn, source=raw_source)
            raw_ref = self.ctx.raw.save(raw_source, event_id or "DISCOVERY", endpoint, payload)
            self.ctx.log.write(
                source=raw_source, endpoint=endpoint, status="OK", event_id=event_id,
                symbol=symbol, raw_ref=raw_ref,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            time.sleep(SOURCE_DELAY_SEC)
            return payload, raw_ref
        except Exception as exc:
            self.ctx.log.write(
                source=raw_source, endpoint=endpoint,
                status="SKIPPED" if isinstance(exc, SourceSkipped) else "FAILED", event_id=event_id,
                symbol=symbol, error=f"{type(exc).__name__}: {exc}", http_status=_exc_status(exc),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            raise

    def financial_results(self, from_dt: datetime | None, to_dt: datetime | None, symbol: str | None = None,
                          event_id: str | None = None):
        # 2.5.4: NSE's date-filtered query returns a handful of stale rows; a
        # per-symbol query WITHOUT dates returns that company's filings.
        return self._call(
            "financial_results",
            lambda: self.client.financial_results(
                segment="equities", period="quarterly", symbol=symbol,
                from_date=from_dt, to_date=to_dt,
            ) or [],
            event_id=event_id, symbol=symbol,
        )

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an NSE /api endpoint the library has no method for (same
        transport, cookies and throttling as the library's own calls)."""
        url = f"{getattr(self.client, 'base_url', 'https://www.nseindia.com/api')}/{path.lstrip('/')}"
        transport = getattr(self.client, "_transport", None)
        if transport is not None:
            resp = transport.request(url, params=params)
        elif hasattr(self.client, "_req"):
            resp = self.client._req(url, params=params)
        else:
            sess = getattr(self.client, "session", None) or getattr(self.client, "_NSE__session", None)
            if sess is None:
                raise RuntimeError("NSE client has no request transport")
            resp = sess.get(url, params=params)
        return resp.json() if hasattr(resp, "json") else resp

    def market_breadth(self, index: str = "NIFTY 500"):
        """Advances / declines of an index today (2.9.0)."""
        return self._call("advance_decline", lambda: self._api_get("equity-stockIndices-adu", {"index": index.upper()}),
                          symbol=index, raw_source="NSE_INDEX")

    def fii_dii(self):
        """Provisional FII/FPI and DII cash-market net buy/sell for the latest day (2.9.0)."""
        return self._call("fii_dii", lambda: self._api_get("fiidiiTradeReact"), symbol="FII_DII", raw_source="NSE_INDEX")

    def integrated_filings(self, symbol: str, event_id: str | None = None):
        """NSE "Integrated Filing - Financials" index for one company (engine 2.6.1).
        Since the Mar-2025 quarter SEBI moved quarterly results here; the old
        corporates-financial-results index stops at Dec-2024."""
        def go():
            url = f"{getattr(self.client, 'base_url', 'https://www.nseindia.com/api')}/integrated-filing-results"
            params = {"index": "equities", "symbol": symbol, "type": "Integrated Filing- Financials"}
            transport = getattr(self.client, "_transport", None)
            if transport is not None:
                resp = transport.request(url, params=params)
            elif hasattr(self.client, "_req"):
                resp = self.client._req(url, params=params)
            else:
                sess = getattr(self.client, "session", None) or getattr(self.client, "_NSE__session", None)
                if sess is None:
                    raise RuntimeError("NSE client has no request transport")
                resp = sess.get(url, params=params)
            data = resp.json() if hasattr(resp, "json") else resp
            rows = data.get("data", []) if isinstance(data, dict) else (data or [])
            return [r for r in rows if isinstance(r, dict) and "financ" in str(r.get("type") or "").lower()]
        return self._call("integrated_filings", go, event_id=event_id, symbol=symbol)

    def board_meetings(self, from_dt: datetime, to_dt: datetime):
        return self._call(
            "board_meetings",
            lambda: self.client.board_meetings(index="equities", from_date=from_dt, to_date=to_dt) or [],
        )

    def results_comparison(self, symbol: str, event_id: str):
        return self._call(
            "results_comparison",
            lambda: self.client.results_comparison(symbol),
            event_id=event_id, symbol=symbol,
        )

    def lookup(self, symbol: str, event_id: str):
        return self._call(
            "lookup",
            lambda: self.client.lookup(query=symbol, segment="equity"),
            event_id=event_id, symbol=symbol,
        )

    def quote(self, symbol: str, event_id: str, series: str | None = None):
        fn = getattr(self.client, "quote", None)
        if fn is None:
            raise RuntimeError("NSE quote method unavailable")
        if series and series.upper() != "EQ":
            # 2.9.4: a BE/BZ stock's EQ quote is all nulls (no market cap).
            return self._call("quote", lambda: fn(symbol, series=series.lower()), event_id=event_id, symbol=symbol)
        return self._call("quote", lambda: fn(symbol), event_id=event_id, symbol=symbol)

    def announcements(self, symbol: str, start: datetime, end: datetime, event_id: str):
        fn = getattr(self.client, "announcements", None)
        if fn is None:
            raise RuntimeError("NSE announcements method unavailable")
        return self._call("announcements", lambda: fn(index="equities", symbol=symbol, from_date=start, to_date=end) or [],
                          event_id=event_id, symbol=symbol)

    def index_history(self, index: str, start: date, end: date):
        fn = getattr(self.client, "fetch_historical_index_data", None)
        if fn is None:
            raise RuntimeError("NSE index-history method unavailable")
        return self._call("index_history", lambda: fn(index, from_date=start, to_date=end), symbol=index, raw_source="NSE_INDEX")

    def history(self, symbol: str, start: date, end: date, event_id: str):
        """EQ-series history, plus the stock's active series when EQ stops
        early (2.9.4). NSE moves stocks under surveillance to BE (trade-to-
        trade); an EQ-only request then ends on the switch day (KABRAEXTRU
        30 Sep, MODISONLTD 10 Sep 2026) and the EQ quote comes back empty.
        One combined payload is saved, so replays see the whole history."""
        fn = getattr(self.client, "fetch_equity_historical_data", None)
        if fn is None:
            raise RuntimeError("NSE historical-data method unavailable")
        self.last_series = None

        def go():
            rows = list(fn(symbol=symbol, from_date=start, to_date=end) or [])
            last = nse_rows_last_date(rows)
            if last is not None and (end - last).days <= NSE_SERIES_STALE_DAYS:
                self.last_series = "EQ"
                return rows
            series = self.active_series(symbol)
            alt = [x for x in series if x in NSE_ALT_PRICE_SERIES]
            if not alt or not _extra_budget_ok(1):
                return rows
            try:
                more = list(fn(symbol=symbol, from_date=start, to_date=end, series=alt[0].lower()) or [])
            except Exception:
                return rows
            if more:
                self.last_series = alt[0]
            return merge_nse_history_rows(rows, more)

        return self._call("equity_history", go, event_id=event_id, symbol=symbol, raw_source="NSE_PRICE")

    def active_series(self, symbol: str) -> list[str]:
        """Trading series NSE lists as active for a symbol (getMetaData)."""
        meta_fn = getattr(self.client, "equity_meta_info", None)
        if meta_fn is None or not _extra_budget_ok(1):
            return []
        try:
            meta = meta_fn(symbol) or {}
        except Exception:
            return []
        return [str(x).upper() for x in (meta.get("activeSeries") or []) if x]


class BSEAdapter:
    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.client = None

    def __enter__(self):
        if BSE is None:
            raise RuntimeError("bse package unavailable")
        self.client = BSE(str(ROOT))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client is not None:
                self.client.exit()
        except Exception:
            pass

    def _call(self, endpoint: str, fn, *, event_id: str | None = None, symbol: str | None = None, raw_source: str = "BSE"):
        started = time.perf_counter()
        try:
            payload = with_retry(fn, source=raw_source)
            raw_ref = self.ctx.raw.save(raw_source, event_id or "DISCOVERY", endpoint, payload)
            self.ctx.log.write(
                source=raw_source, endpoint=endpoint, status="OK", event_id=event_id,
                symbol=symbol, raw_ref=raw_ref,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            time.sleep(SOURCE_DELAY_SEC)
            return payload, raw_ref
        except Exception as exc:
            self.ctx.log.write(
                source=raw_source, endpoint=endpoint,
                status="SKIPPED" if isinstance(exc, SourceSkipped) else "FAILED", event_id=event_id,
                symbol=symbol, error=f"{type(exc).__name__}: {exc}", http_status=_exc_status(exc),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            raise

    def result_announcements(self, from_dt: datetime, to_dt: datetime):
        # Package versions differ: announcements() in older versions, circulars()
        # in newer versions. Try both without changing the higher-level engine.
        if hasattr(self.client, "announcements"):
            def fn():
                collected = []
                page = 1
                while page <= 30:
                    res = self.client.announcements(
                        page_no=page, from_date=from_dt, to_date=to_dt, category="Result"
                    ) or {}
                    table = res.get("Table") or []
                    if not table:
                        break
                    collected.extend(table)
                    total = None
                    try:
                        total = int((res.get("Table1") or [{}])[0].get("ROWCNT"))
                    except Exception:
                        pass
                    if total is not None and len(collected) >= total:
                        break
                    page += 1
                return collected
        elif hasattr(self.client, "circulars"):
            def fn():
                res = self.client.circulars(
                    from_date=from_dt, to_date=to_dt, segment="Equity", category="Result"
                ) or {}
                return res.get("Table") or res.get("data") or []
        else:
            raise RuntimeError("BSE announcement API unavailable")
        return self._call("result_announcements", fn)

    def result_calendar(self, from_dt: datetime, to_dt: datetime):
        fn = getattr(self.client, "resultCalendar", None) or getattr(self.client, "result_calendar", None)
        if fn is None:
            raise RuntimeError("BSE result calendar API unavailable")
        return self._call("result_calendar", lambda: fn(from_date=from_dt, to_date=to_dt) or [])

    def lookup(self, text: str, event_id: str):
        return self._call("lookup", lambda: self.client.lookup(text), event_id=event_id, symbol=text)

    def scrip_name(self, code: str, event_id: str):
        return self._call("get_scrip_name", lambda: self.client.getScripName(code), event_id=event_id, symbol=code)

    def results_snapshot(self, code: str, event_id: str, symbol: str | None = None):
        return self._call(
            "results_snapshot", lambda: self.client.resultsSnapshot(str(code)),
            event_id=event_id, symbol=symbol,
        )

    def scrip_announcements(self, code: str, start: datetime, end: datetime, event_id: str):
        def fn():
            res = self.client.announcements(page_no=1, from_date=start, to_date=end, scripcode=str(code)) or {}
            return res.get("Table") or []
        return self._call("scrip_announcements", fn, event_id=event_id, symbol=str(code))

    def scrip_results(self, code: str, start: datetime, end: datetime, event_id: str):
        def fn():
            res = self.client.announcements(page_no=1, from_date=start, to_date=end, scripcode=str(code), category="Result") or {}
            return res.get("Table") or []
        return self._call("scrip_results", fn, event_id=event_id, symbol=str(code))

    def meta(self, code: str, event_id: str, symbol: str | None = None):
        fn = getattr(self.client, "equityMetaInfo", None)
        if fn is None:
            raise RuntimeError("BSE equityMetaInfo unavailable")
        return self._call("equity_meta", lambda: fn(str(code)), event_id=event_id, symbol=symbol)

    def price_history(self, code: str, event_id: str, symbol: str | None = None):
        return self._call(
            "price_volume_12m", lambda: self.client.equityPriceVolumeT12M(str(code)),
            event_id=event_id, symbol=symbol, raw_source="BSE_PRICE",
        )



def _load_yahoo_negative_cache() -> dict[str, dict[str, Any]]:
    global _YAHOO_NEGATIVE_CACHE
    if _YAHOO_NEGATIVE_CACHE is not None:
        return _YAHOO_NEGATIVE_CACHE
    cache: dict[str, dict[str, Any]] = {}
    try:
        if YAHOO_NEGATIVE_CACHE_PATH.exists():
            raw = json.loads(YAHOO_NEGATIVE_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cache = {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        cache = {}
    _YAHOO_NEGATIVE_CACHE = cache
    return cache


def _save_yahoo_negative_cache() -> None:
    cache = _load_yahoo_negative_cache()
    now = now_ist()
    cleaned = {}
    for ticker, meta in cache.items():
        until = parse_datetime(meta.get("until"))
        if until is None or until > now:
            cleaned[ticker] = meta
    global _YAHOO_NEGATIVE_CACHE
    _YAHOO_NEGATIVE_CACHE = cleaned
    try:
        YAHOO_NEGATIVE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        json_dump_atomic(YAHOO_NEGATIVE_CACHE_PATH, cleaned)
    except Exception:
        pass


def _yahoo_is_negative(ticker: str) -> bool:
    ticker = str(ticker or "").upper().strip()
    meta = _load_yahoo_negative_cache().get(ticker)
    if not meta:
        return False
    until = parse_datetime(meta.get("until"))
    if until is None:
        return False
    if until <= now_ist():
        _load_yahoo_negative_cache().pop(ticker, None)
        return False
    return True


def _yahoo_mark_negative(ticker: str, reason: str, hours: int | None = None) -> None:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return
    ttl = int(hours or YAHOO_NEGATIVE_TTL_HOURS)
    _load_yahoo_negative_cache()[ticker] = {
        "failedAt": iso_now(),
        "until": (now_ist() + timedelta(hours=ttl)).isoformat(),
        "reason": str(reason or "Yahoo unavailable")[:500],
    }


def _yahoo_clear_negative(ticker: str) -> None:
    _load_yahoo_negative_cache().pop(str(ticker or "").upper().strip(), None)


def _yahoo_hard_failure(text: str) -> bool:
    low = str(text or "").lower()
    return any(token in low for token in (
        "quote not found", "possibly delisted", "may be delisted",
        "no data found", "404", "empty history", "empty info",
    ))


def _yahoo_budget(kind: str, limit: int) -> bool:
    used = int(_YAHOO_BUDGET_USED.get(kind, 0))
    if used >= max(0, int(limit)):
        return False
    if RUN_GUARD.elapsed() >= RUN_BUDGET_SEC or "YAHOO" in RUN_GUARD.down:
        return False
    _YAHOO_BUDGET_USED[kind] = used + 1
    return True


def _heavy_enrichment_due(event: dict[str, Any], store: EventStore) -> bool:
    if boolish(store.value(event, "results_released")) is True:
        return True
    rd = parse_date(store.value(event, "result_date")) or parse_date(event.get("period", {}).get("end"))
    if rd is None:
        return False
    days = (rd - now_ist().date()).days
    return -ACTIVE_ENRICH_DAYS <= days <= HEAVY_UPCOMING_DAYS


def _valuation_due(event: dict[str, Any], store: EventStore) -> bool:
    if boolish(store.value(event, "results_released")) is True:
        return True
    rd = parse_date(store.value(event, "result_date"))
    if rd is None:
        return False
    return 0 <= (rd - now_ist().date()).days <= VALUATION_UPCOMING_DAYS


def _enrichment_priority(event: dict[str, Any]) -> tuple[int, int, int, str]:
    released = boolish(meta_value(event, "results_released")) is True
    rd = parse_date(meta_value(event, "result_date")) or parse_date(event.get("period", {}).get("end"))
    today = now_ist().date()
    if released:
        bucket = 0
    elif rd is not None and rd <= today:
        bucket = 1
    elif rd is not None and (rd - today).days <= HEAVY_UPCOMING_DAYS:
        bucket = 2
    else:
        bucket = 3
    distance = abs((rd - today).days) if rd is not None else 9999
    # 2.5.4: events whose price/sector are already fresh drop behind those
    # that still need work, so the 120 slots rotate through the whole list
    # (HINDUNILVR, TATAPOWER... never got a price because the same nearest
    # 120 events won every run).
    fetch = event.get("fetch") or {}
    def age_h(key: str) -> float:
        t = parse_datetime((fetch.get(key) or {}).get("lastSuccess")) or parse_datetime((fetch.get(key) or {}).get("lastAttempt"))
        return (now_ist() - t).total_seconds() / 3600 if t else 1e9
    never_priced = not (event.get("plus") or {}).get("price") and not (fetch.get("PRICE_HISTORY") or {}).get("lastSuccess")
    price_due = age_h("PRICE_HISTORY") >= (1 if released else 4)
    meta_due = age_h("EXCHANGE_META") >= EXCHANGE_META_REFRESH_HOURS
    # 0 never priced, 1 price due, 2 only sector/valuation due, 3 nothing due
    tier = 0 if (released or never_priced) else 1 if price_due else 2 if meta_due else 3
    return bucket, tier, distance, str(event.get("eventId") or "")


class YahooAdapter:
    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    def _ticker_candidates(self, event: dict[str, Any]) -> list[str]:
        sec = event.get("security", {})
        out: list[str] = []
        explicit = str(sec.get("yahooTicker") or "").upper().strip()
        nse_symbol = normalize_symbol(sec.get("nseSymbol"))
        bse_code = str(sec.get("bseCode") or "").strip()

        # Do not invent .NS for a BSE-only company. This was the main source of
        # MICROSE.NS / MACIND.NS 404 loops. Prefer the exchange identity we
        # actually know.
        if nse_symbol:
            if explicit:
                out.append(explicit)
            out.append(f"{nse_symbol}.NS")
            if bse_code.isdigit():
                out.append(f"{bse_code}.BO")
        elif bse_code.isdigit():
            out.append(f"{bse_code}.BO")
            if explicit and explicit.endswith(".BO"):
                out.append(explicit)
        else:
            if explicit:
                out.append(explicit)
            generic = normalize_symbol(sec.get("symbol"))
            if generic and not generic.isdigit():
                out.append(f"{generic}.NS")

        return [t for t in dict.fromkeys(out) if t and not _yahoo_is_negative(t)]

    def history(self, event: dict[str, Any], period: str = "18mo") -> tuple[Any, str | None, str | None]:
        if yf is None:
            return None, None, "yfinance unavailable"
        eid = event["eventId"]
        symbol = event.get("security", {}).get("symbol")
        errors = []
        candidates = self._ticker_candidates(event)
        if not candidates:
            return None, None, "no eligible Yahoo ticker (negative-cached or unmapped)"
        for ticker in candidates:
            key = (ticker, period)
            if key in _YAHOO_HISTORY_CACHE:
                cached = _YAHOO_HISTORY_CACHE[key]
                return cached.copy() if hasattr(cached, "copy") else cached, ticker, None
            if not _yahoo_budget("price", YAHOO_PRICE_BUDGET):
                return None, None, "Yahoo price budget exhausted for this run"
            started = time.perf_counter()
            try:
                frame = yf.download(
                    ticker, period=period, interval="1d", auto_adjust=False,
                    progress=False, threads=False, timeout=YAHOO_TIMEOUT_SEC,
                )
                if frame is None or frame.empty:
                    raise RuntimeError("empty history")
                if pd is not None and isinstance(frame.columns, pd.MultiIndex):
                    try:
                        frame = frame.xs(ticker, axis=1, level=1)
                    except Exception:
                        frame.columns = frame.columns.get_level_values(0)
                _YAHOO_HISTORY_CACHE[key] = frame.copy() if hasattr(frame, "copy") else frame
                _yahoo_clear_negative(ticker)
                self.ctx.log.write(
                    source="YAHOO_PRICE", endpoint="history", status="OK",
                    event_id=eid, symbol=str(symbol or ticker),
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
                return frame, ticker, None
            except Exception as exc:
                msg = f"{ticker}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                if _yahoo_hard_failure(msg):
                    _yahoo_mark_negative(ticker, msg)
                self.ctx.log.write(
                    source="YAHOO_PRICE", endpoint="history", status="FAILED",
                    event_id=eid, symbol=str(symbol or ticker), error=msg,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
        return None, None, "; ".join(errors)

    def fundamentals(self, event: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None]:
        if yf is None:
            return {}, None, "yfinance unavailable"
        eid = event["eventId"]
        symbol = event.get("security", {}).get("symbol")
        errors = []
        candidates = self._ticker_candidates(event)
        if not candidates:
            return {}, None, "no eligible Yahoo ticker (negative-cached or unmapped)"
        for ticker in candidates:
            if ticker in _YAHOO_FUNDAMENTALS_CACHE:
                return dict(_YAHOO_FUNDAMENTALS_CACHE[ticker]), ticker, None
            if not _yahoo_budget("fundamentals", YAHOO_FUNDAMENTALS_BUDGET):
                return {}, None, "Yahoo fundamentals budget exhausted for this run"
            started = time.perf_counter()
            try:
                t = yf.Ticker(ticker)
                info = t.info or {}
                if not info:
                    raise RuntimeError("empty info")
                out = {
                    "ticker": ticker,
                    "market_cap_cr": round2((safe_num(info.get("marketCap")) or 0) / 1e7) if safe_num(info.get("marketCap")) is not None else None,
                    "trailing_pe": safe_num(info.get("trailingPE")),
                    "forward_pe": safe_num(info.get("forwardPE")),
                    "peg": safe_num(info.get("pegRatio") or info.get("trailingPegRatio")),
                    "roe_pct": (safe_num(info.get("returnOnEquity")) * 100) if safe_num(info.get("returnOnEquity")) is not None and abs(safe_num(info.get("returnOnEquity"))) <= 5 else safe_num(info.get("returnOnEquity")),
                    "free_cash_flow": safe_num(info.get("freeCashflow")),
                    "sector": info.get("sector"),
                    "industry": info.get("industry"),
                    "name": info.get("longName") or info.get("shortName"),
                    "shares_outstanding": safe_num(info.get("sharesOutstanding")),
                    "last_price": safe_num(info.get("currentPrice") or info.get("regularMarketPrice")),
                }
                mcap = safe_num(info.get("marketCap"))
                fcf = safe_num(info.get("freeCashflow"))
                out["fcf_yield_pct"] = (fcf / mcap * 100) if fcf is not None and mcap not in (None, 0) else None
                _YAHOO_FUNDAMENTALS_CACHE[ticker] = dict(out)
                _yahoo_clear_negative(ticker)
                self.ctx.log.write(
                    source="YAHOO_FUNDAMENTALS", endpoint="info", status="OK",
                    event_id=eid, symbol=str(symbol or ticker),
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
                return out, ticker, None
            except Exception as exc:
                msg = f"{ticker}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                if _yahoo_hard_failure(msg):
                    _yahoo_mark_negative(ticker, msg)
                self.ctx.log.write(
                    source="YAHOO_FUNDAMENTALS", endpoint="info", status="FAILED",
                    event_id=eid, symbol=str(symbol or ticker), error=msg,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
        return {}, None, "; ".join(errors)

    def quarterly(self, event: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        if yf is None or pd is None:
            return {}, "yfinance/pandas unavailable"
        period_end = parse_date(event.get("period", {}).get("end"))
        if period_end is None:
            return {}, "period end unavailable"
        errors = []
        candidates = self._ticker_candidates(event)
        if not candidates:
            return {}, "no eligible Yahoo ticker (negative-cached or unmapped)"
        period_key = period_end.isoformat()
        for ticker in candidates:
            cache_key = (ticker, period_key)
            if cache_key in _YAHOO_QUARTERLY_CACHE:
                return dict(_YAHOO_QUARTERLY_CACHE[cache_key]), None
            if not _yahoo_budget("quarterly", YAHOO_QUARTERLY_BUDGET):
                return {}, "Yahoo quarterly budget exhausted for this run"
            try:
                t = yf.Ticker(ticker)
                stmt = t.quarterly_income_stmt
                if stmt is None or stmt.empty:
                    stmt = t.quarterly_financials
                if stmt is None or stmt.empty:
                    raise RuntimeError("quarterly statement empty")
                cols = []
                for col in stmt.columns:
                    try:
                        cols.append((pd.Timestamp(col).date(), col))
                    except Exception:
                        pass
                if not cols:
                    raise RuntimeError("statement has no date columns")
                current_date, current_col = min(cols, key=lambda x: abs((x[0] - period_end).days))
                if abs((current_date - period_end).days) > 5:
                    raise RuntimeError("latest statement does not match event quarter")

                def row_name(names):
                    normalized = {str(idx).strip().lower(): idx for idx in stmt.index}
                    for name in names:
                        if name.lower() in normalized:
                            return normalized[name.lower()]
                    for low, original in normalized.items():
                        if any(name.lower() in low for name in names):
                            return original
                    return None

                rev_row = row_name(["Total Revenue", "Operating Revenue", "Revenue", "Total Operating Income"])
                pat_row = row_name(["Net Income", "Net Income Common Stockholders", "Profit After Tax"])
                eps_row = row_name(["Basic EPS", "Diluted EPS"])
                if rev_row is None and pat_row is None:
                    raise RuntimeError("revenue/PAT rows unavailable")

                current_rev = safe_num(stmt.loc[rev_row, current_col]) if rev_row is not None else None
                current_pat = safe_num(stmt.loc[pat_row, current_col]) if pat_row is not None else None
                eps = safe_num(stmt.loc[eps_row, current_col]) if eps_row is not None else None

                older = sorted([(d, c) for d, c in cols if d < current_date], reverse=True)
                prev_col = older[0][1] if older else None
                prev_rev = safe_num(stmt.loc[rev_row, prev_col]) if rev_row is not None and prev_col is not None else None
                prev_pat = safe_num(stmt.loc[pat_row, prev_col]) if pat_row is not None and prev_col is not None else None
                target = date(current_date.year - 1, current_date.month, min(current_date.day, 28))
                yoy_options = [(abs((d - target).days), c) for d, c in cols if d < current_date]
                yoy_col = min(yoy_options, key=lambda x: x[0])[1] if yoy_options and min(yoy_options, key=lambda x: x[0])[0] <= 50 else None
                prior_rev = safe_num(stmt.loc[rev_row, yoy_col]) if rev_row is not None and yoy_col is not None else None
                prior_pat = safe_num(stmt.loc[pat_row, yoy_col]) if pat_row is not None and yoy_col is not None else None
                trend, pat_yoy = pat_trend(current_pat, prior_pat)

                scale = 1e7  # yfinance statements are INR; convert to crore.
                out = {
                    "revenue_cr": current_rev / scale if current_rev is not None else None,
                    "pat_cr": current_pat / scale if current_pat is not None else None,
                    "eps": eps,
                    "prior_year_revenue_cr": prior_rev / scale if prior_rev is not None else None,
                    "prior_year_pat_cr": prior_pat / scale if prior_pat is not None else None,
                    "revenue_yoy_pct": pct_change(current_rev, prior_rev),
                    "pat_yoy_pct": pat_yoy,
                    "pat_trend": trend,
                    "revenue_qoq_pct": pct_change(current_rev, prev_rev),
                    "pat_qoq_pct": pat_change_pct(current_pat, prev_pat),
                    "pat_qoq_trend": pat_trend(current_pat, prev_pat)[0],
                    "basis": "UNKNOWN",
                    "statementPeriodEnd": period_end.isoformat() if abs((current_date - period_end).days) <= 5 else current_date.isoformat(),
                    "ticker": ticker,
                    "statement_period": current_date.isoformat(),
                }
                _YAHOO_QUARTERLY_CACHE[cache_key] = dict(out)
                _yahoo_clear_negative(ticker)
                return out, None
            except Exception as exc:
                msg = f"{ticker}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                if _yahoo_hard_failure(msg):
                    _yahoo_mark_negative(ticker, msg, hours=24)
        return {}, "; ".join(errors)


# ---------------------------------------------------------------------------
# XBRL parser (generic, exchange-first)
# ---------------------------------------------------------------------------


class XBRLParser:
    REVENUE_NAMES = [
        "revenuefromoperations", "revenuefromsaleofproducts", "revenue",
        "totalrevenue", "totalincome",
    ]
    PAT_NAMES = [
        "profitloss", "profitaftertax", "netprofitloss", "profitlossfortheperiod",
        "profitlossattributabletoownersofparent",
    ]
    EPS_NAMES = [
        "basicearningslosspershare", "basiceps", "dilutedearningslosspershare",
    ]

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    def _fetch(self, url: str, event_id: str, source: str) -> tuple[bytes, str]:
        if requests is None:
            raise RuntimeError("requests unavailable")
        if url.startswith("/"):
            base = "https://www.nseindia.com" if "NSE" in source else "https://www.bseindia.com"
            url = urljoin(base, url)
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134 Safari/537.36",
            "Accept": "application/xml,text/xml,application/xhtml+xml,text/html,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/" if "NSE" in source else "https://www.bseindia.com/",
        })
        if "nseindia.com" in url:
            try:
                sess.get("https://www.nseindia.com/", timeout=12)
            except Exception:
                pass
        started = time.perf_counter()
        try:
            response = with_retry(lambda: sess.get(url, timeout=25), source=source)
            response.raise_for_status()
            blob = response.content
            suffix = "zip" if blob[:2] == b"PK" else "xml"
            raw_ref = self.ctx.raw.save(source, event_id, "xbrl", blob, suffix=suffix)
            self.ctx.log.write(
                source=source, endpoint="xbrl", status="OK", event_id=event_id,
                http_status=response.status_code, raw_ref=raw_ref,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                extra={"url": url},
            )
            return blob, raw_ref
        except Exception as exc:
            self.ctx.log.write(
                source=source, endpoint="xbrl",
                status="SKIPPED" if isinstance(exc, SourceSkipped) else "FAILED", event_id=event_id,
                error=f"{type(exc).__name__}: {exc}", http_status=_exc_status(exc),
                elapsed_ms=round((time.perf_counter() - started) * 1000), extra={"url": url},
            )
            raise

    @staticmethod
    def _local(tag: str) -> str:
        return tag.split("}")[-1].split(":")[-1]

    @staticmethod
    def _num_text(text: str | None, scale: int = 0) -> float | None:
        if text is None:
            return None
        s = text.strip().replace(",", "")
        if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s):
            return None
        try:
            return float(s) * (10 ** scale)
        except Exception:
            return None

    def _documents(self, blob: bytes) -> list[bytes]:
        if blob[:2] != b"PK":
            return [blob]
        docs = []
        try:
            from io import BytesIO
            with zipfile.ZipFile(BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith((".xml", ".xbrl", ".html", ".xhtml")):
                        docs.append(zf.read(name))
        except Exception:
            pass
        return docs

    def parse(self, blob: bytes, period_end: date) -> dict[str, Any]:
        return parse_xbrl_financials(self._documents(blob), period_end)

    def fetch_parse(self, url: str, event_id: str, period_end: date, source: str) -> tuple[dict[str, Any], str]:
        blob, raw_ref = self._fetch(url, event_id, source)
        return self.parse(blob, period_end), raw_ref


# ---------------------------------------------------------------------------
# XBRL financial extraction (strict)
# ---------------------------------------------------------------------------

# Exact concept names (lower-case local names). Order = preference. No substring
# matching: "profitloss" must never resolve to ProfitLossBeforeTax.
XBRL_REVENUE_CONCEPTS = [
    "revenuefromoperations", "totalrevenuefromoperations", "revenuefromoperationsnet",
    "netsalesincomefromoperations", "incomefromoperations", "revenuefromcontractswithcustomers",
]
XBRL_BANK_REVENUE_CONCEPTS = ["interestearned", "totalinterestearned"]
# Insurers (in-capmkt insurance taxonomy, verified on ICICIGI / SBILIFE Q1 FY27 filings).
XBRL_INSURANCE_REVENUE_CONCEPTS = ["premiumearned", "netpremiumincome", "netpremiumwritten", "grosspremiumincome",
                                   "grosspremiumswritten"]
XBRL_TOTAL_INCOME_CONCEPTS = ["income", "totalincome", "totalrevenue"]
XBRL_PAT_OWNER_CONCEPTS = [
    "profitlossforperiodattributabletoownersofparent", "profitorlossattributabletoownersofparent",
    "profitlossattributabletoownersofparent", "netprofitlossforperiodattributabletoownersofparent",
    "profitlossfortheperiodattributabletoownersofparent",
    # banking taxonomy (HDFCBANK consolidated Q1 FY27)
    "profitlossaftertaxesminorityinterestandshareofprofitlossofassociates",
]
XBRL_PAT_CONCEPTS = [
    "profitlossforperiod", "profitloss", "netprofitlossforperiod", "profitlossfortheperiod",
    "netprofitloss", "netprofitlossfortheperiod", "profitaftertax",
    # insurance taxonomy
    "profitlossaftertax", "profitlossaftertaxandextraordinaryitems", "profitlossaftertaxbeforeextraordinaryitems",
]
XBRL_EPS_CONCEPTS = [
    "basicearningslosspershareforcontinuinganddiscontinuedoperations",
    "basicearningslosspersharefromcontinuinganddiscontinuedoperations",
    "basicearningslosspershare", "basicearningspershare", "basiceps",
    "basicearningspershareafterextraordinaryitems", "basicearningspersharebeforeextraordinaryitems",
]
QUARTER_DAYS = (80, 100)
SNAPSHOT_PARSER_VERSION = 2   # 2.9.4: re-parse saved BSE/NSE result tables once (PAT model on QoQ)
XBRL_PARSER_VERSION = 5   # bump to re-read every stored XBRL snapshot once


def _xbrl_local(tag: str) -> str:
    return tag.split("}")[-1].split(":")[-1]


def _shift_quarters(d: date, quarters: int) -> date:
    month_index = d.year * 12 + (d.month - 1) + 3 * quarters
    y, m = divmod(month_index, 12)
    m += 1
    last = {1: 31, 2: 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 3: 31, 4: 30,
            5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}[m]
    return date(y, m, min(d.day if d.day < 28 else last, last))


def parse_xbrl_financials(documents: list[bytes], period_end: date) -> dict[str, Any]:
    """Extract one quarter's revenue/PAT/EPS from an exchange XBRL instance.

    Rules that prevent mixed or wrong numbers:
    * only contexts without segment/scenario dimensions (no segment revenue);
    * only 80–100 day duration contexts (no half-year / YTD cumulative values);
    * exact concept names, in preference order (no PBT/OCI/minority leakage);
    * prior-year and previous-quarter comparatives come from the SAME document,
      so basis (standalone/consolidated) and restatements always match;
    * monetary facts must be in an INR unit; values are converted to crore.
    """
    contexts: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    facts: list[dict[str, Any]] = []
    text_facts: dict[str, str] = {}
    for doc in documents:
        try:
            root = ET.fromstring(doc)
        except Exception:
            continue
        for elem in root.iter():
            local = _xbrl_local(elem.tag).lower()
            if local == "context":
                cid = elem.attrib.get("id")
                if not cid:
                    continue
                info: dict[str, Any] = {"start": None, "end": None, "instant": None, "dimensional": False}
                for x in elem.iter():
                    name = _xbrl_local(x.tag).lower()
                    if name == "startdate":
                        info["start"] = parse_date(x.text)
                    elif name == "enddate":
                        info["end"] = parse_date(x.text)
                    elif name == "instant":
                        info["instant"] = parse_date(x.text)
                    elif name in {"explicitmember", "typedmember"}:
                        info["dimensional"] = True
                contexts[cid] = info
                continue
            if local == "unit":
                uid = elem.attrib.get("id")
                if uid:
                    units[uid] = " ".join((x.text or "").strip() for x in elem.iter() if _xbrl_local(x.tag).lower() == "measure").upper()
                continue
            cref = elem.attrib.get("contextRef") or elem.attrib.get("contextref")
            if not cref:
                continue
            text = (elem.text or "").strip()
            unit_ref = elem.attrib.get("unitRef") or elem.attrib.get("unitref")
            if unit_ref is None:
                if text and local not in text_facts:
                    text_facts[local] = text
                continue
            num = text.replace(",", "")
            if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", num):
                continue
            value = float(num)
            if elem.attrib.get("sign") == "-":
                value = -value
            facts.append({"name": local, "context": cref, "unit": unit_ref, "value": value})

    issues: list[str] = []

    def quarter_contexts(target_end: date) -> set[str]:
        out = set()
        for cid, c in contexts.items():
            if c["dimensional"] or c["start"] is None or c["end"] is None:
                continue
            days = (c["end"] - c["start"]).days
            if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1] and abs((c["end"] - target_end).days) <= 3:
                out.add(cid)
        return out

    def pick(concepts: list[str], ctx_ids: set[str], monetary: bool = True) -> tuple[float | None, str | None]:
        for concept in concepts:
            values = {round(f["value"], 4) for f in facts if f["name"] == concept and f["context"] in ctx_ids}
            if not values:
                continue
            use_ids = ctx_ids
            if len(values) > 1:
                # Pre-2025 NSE results XBRL labels its year-to-date context
                # ("FourD") with quarter dates; the period column is "OneD".
                one = {c for c in ctx_ids if c.lower().startswith("one")}
                one_vals = {round(f["value"], 4) for f in facts if f["name"] == concept and f["context"] in one}
                if len(one_vals) == 1:
                    values, use_ids = one_vals, one
            fact = next(f for f in facts if f["name"] == concept and f["context"] in use_ids)
            if len(values) > 1:
                issues.append(f"CONFLICTING_FACTS:{concept}")
                return None, concept
            unit = units.get(fact["unit"], str(fact["unit"]).upper())
            if monetary:
                if "INR" not in unit or "SHARE" in unit:
                    issues.append(f"NON_INR_UNIT:{concept}:{unit}")
                    return None, concept
                return fact["value"] / 1e7, concept
            return fact["value"], concept
        return None, None

    current_ctx = quarter_contexts(period_end)
    prior_ctx = quarter_contexts(_shift_quarters(period_end, -4))
    prev_ctx = quarter_contexts(_shift_quarters(period_end, -1))
    if not current_ctx:
        issues.append("NO_QUARTER_CONTEXT_FOR_PERIOD")

    nature = " ".join(v for k, v in text_facts.items() if "natureofreport" in k or "standaloneconsolidated" in k).lower()
    basis = "CONSOLIDATED" if "consolidated" in nature and "standalone" not in nature else ("STANDALONE" if "standalone" in nature else "UNKNOWN")

    revenue_definition = "REVENUE_FROM_OPERATIONS"
    rev, rev_concept = pick(XBRL_REVENUE_CONCEPTS, current_ctx)
    revenue_concepts = XBRL_REVENUE_CONCEPTS
    if rev is None and rev_concept is None:
        rev, rev_concept = pick(XBRL_BANK_REVENUE_CONCEPTS, current_ctx)
        revenue_concepts, revenue_definition = XBRL_BANK_REVENUE_CONCEPTS, "INTEREST_EARNED"
    if rev is None and rev_concept is None:
        rev, rev_concept = pick(XBRL_INSURANCE_REVENUE_CONCEPTS, current_ctx)
        revenue_concepts, revenue_definition = XBRL_INSURANCE_REVENUE_CONCEPTS, "PREMIUM_INCOME"
    if rev is None and rev_concept is None:
        rev, rev_concept = pick(XBRL_TOTAL_INCOME_CONCEPTS, current_ctx)
        revenue_concepts, revenue_definition = XBRL_TOTAL_INCOME_CONCEPTS, "TOTAL_INCOME"
        if rev is not None:
            issues.append("REVENUE_IS_TOTAL_INCOME")

    pat_concepts = (XBRL_PAT_OWNER_CONCEPTS + XBRL_PAT_CONCEPTS) if basis != "STANDALONE" else XBRL_PAT_CONCEPTS
    pat, pat_concept = pick(pat_concepts, current_ctx)
    if pat == 0 and pat_concept in XBRL_PAT_OWNER_CONCEPTS:
        # 2.6.2: companies without minority interest often file the owners'
        # line as 0 (GMBREW Q2 FY27: owners 0, profit for period ₹39.29 Cr).
        total, total_concept = pick(XBRL_PAT_CONCEPTS, current_ctx)
        if total not in (None, 0):
            pat, pat_concept = total, total_concept
            issues.append("OWNERS_PAT_ZERO_USED_TOTAL")
    # Comparatives must use the SAME concept as the current quarter.
    same_rev = [rev_concept] if rev_concept else revenue_concepts
    same_pat = [pat_concept] if pat_concept else pat_concepts
    prior_rev, _ = pick(same_rev, prior_ctx)
    prior_pat, _ = pick(same_pat, prior_ctx)
    prev_rev, _ = pick(same_rev, prev_ctx)
    prev_pat, _ = pick(same_pat, prev_ctx)
    eps, eps_concept = pick(XBRL_EPS_CONCEPTS, current_ctx, monetary=False)

    trend, pat_yoy = pat_trend(pat, prior_pat)
    return {
        "revenue_cr": round2(rev),
        "pat_cr": round2(pat),
        "eps": round2(eps),
        "prior_year_revenue_cr": round2(prior_rev),
        "prior_year_pat_cr": round2(prior_pat),
        "revenue_yoy_pct": round2(pct_change(rev, prior_rev)),
        "pat_yoy_pct": round2(pat_yoy),
        "pat_trend": trend,
        "revenue_qoq_pct": round2(pct_change(rev, prev_rev)),
        "pat_qoq_pct": round2(pat_change_pct(pat, prev_pat)),
        "pat_qoq_trend": pat_trend(pat, prev_pat)[0],
        "basis": basis,
        "_meta": {
            "periodEnd": period_end.isoformat(),
            "revenueDefinition": revenue_definition if rev is not None else None,
            "concepts": {"revenue": rev_concept, "pat": pat_concept, "eps": eps_concept},
            # 2.9.4: unrounded crore figures; growth from rounded crores was off
            # for small companies (INDBANK PAT -13.44% vs -12.97% exact).
            "exact": {"revenue_cr": rev, "pat_cr": pat},
            "patQoQRawPct": round2(pct_change(pat, prev_pat)),
            "issues": issues,
            "comparativesFromSameDocument": True,
        },
    }


# ---------------------------------------------------------------------------
# Discovery normalization
# ---------------------------------------------------------------------------


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}


def extract_period_from_text(text: Any, filed_on: date | None = None) -> date | None:
    """Return the fiscal quarter-end explicitly named in a filing headline.

    BSE announcements carry the period only in free text ("quarter ended
    31.12.2025", "Half year ended on 30th September,2026", "June 30, 2026").
    Inferring the period from the filing date alone mislabels late filers: a
    Q4 FY25 result filed on 07-Oct-2026 is NOT a Q2 FY27 result.
    Only real quarter-end dates on/before the filing date are accepted.
    """
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text))
    found: list[date] = []

    def add(y: Any, m: Any, d: Any) -> None:
        try:
            y, m, d = int(y), int(m), int(d)
        except (TypeError, ValueError):
            return
        if y < 100:
            y += 2000
        if (m, d) not in _QUARTER_ENDS or not (2000 <= y <= 2100):
            return
        candidate = date(y, m, d)
        if filed_on is not None and candidate > filed_on:
            return
        found.append(candidate)

    for d, m, y in re.findall(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})(?!\d)", t):
        add(y, m, d)
    for d, mon, y in re.findall(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s.\-]*(?:of\s+)?([A-Za-z]{3,9})[\s,.\-]*(\d{4})", t):
        if mon.lower() in _MONTHS:
            add(y, _MONTHS[mon.lower()], d)
    for mon, d, y in re.findall(r"([A-Za-z]{3,9})[\s.\-]*(\d{1,2})(?:st|nd|rd|th)?[\s,]*(\d{4})", t):
        if mon.lower() in _MONTHS:
            add(y, _MONTHS[mon.lower()], d)
    # "Q2 FY27", "Q2 FY 2026-27", "Q2FY2027".
    for q, a, b in re.findall(r"\bQ([1-4])\s*(?:FY|F\.Y\.?)\s*'?(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?", t, flags=re.I):
        fy_end = int(b or a)
        fy_end = fy_end + 2000 if fy_end < 100 else fy_end
        q = int(q)
        y, m, d = {1: (fy_end - 1, 6, 30), 2: (fy_end - 1, 9, 30), 3: (fy_end - 1, 12, 31), 4: (fy_end, 3, 31)}[q]
        add(y, m, d)
    # "quarter ended June 2026" / "Sep-2026": month + year names a quarter end.
    for mon, y in re.findall(r"\b([A-Za-z]{3,9})[\s,.\-']*(\d{4})(?!\d)", t):
        m = _MONTHS.get(mon.lower())
        if m in (3, 6, 9, 12):
            add(y, m, 31 if m in (3, 12) else 30)
    if found:
        return max(found)
    # Last resort: "half year ended on 30th September" (no year). Use the most
    # recent such quarter end on/before the filing date.
    if filed_on is not None:
        pairs = re.findall(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?([A-Za-z]{3,9})\b", t)
        pairs += [(d, mon) for mon, d in re.findall(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)]
        for d, mon in pairs:
            m = _MONTHS.get(mon.lower())
            if m is None:
                continue
            for y in (filed_on.year, filed_on.year - 1):
                before = len(found)
                add(y, m, d)
                if len(found) > before:
                    break
    return max(found) if found else None



def extract_isin(item: dict[str, Any]) -> str | None:
    for key, value in item.items():
        if "isin" in str(key).lower() and value:
            s = str(value).strip().upper()
            if re.fullmatch(r"IN[A-Z0-9]{10}", s):
                return s
    return None


def extract_xbrl_url(item: dict[str, Any]) -> str | None:
    for key in (
        "xbrl", "xbrlLink", "xbrl_url", "xbrlURL", "filePath", "fileName",
        "attachment", "ATTACHMENTNAME", "ATTACHMENT", "NSURL",
    ):
        value = first(item, key)
        if value and any(token in str(value).lower() for token in ("xbrl", ".xml", ".zip", ".xhtml")):
            return str(value)
    return None


def normalize_nse_filing(item: dict[str, Any]) -> dict[str, Any] | None:
    symbol = normalize_symbol(first(item, "symbol", "Symbol", "symbolCode", "securitySymbol"))
    if not symbol:
        return None
    filing_ts = parse_datetime(first(item, "broadCastDate", "broadcastDate", "filingDate", "date"))
    period_end = parse_date(first(item, "toDate", "periodEnded", "periodEnd", "endDate"))
    period_source = "EXCHANGE_PERIOD_FIELD"
    if period_end is None:
        period_end = expected_period_end(filing_ts.date() if filing_ts else None)
        period_source = "INFERRED_FROM_FILING_DATE"
    consolidated = str(first(item, "consolidated", default="") or "").strip().lower()
    basis = "CONSOLIDATED" if consolidated == "consolidated" else ("STANDALONE" if consolidated in {"non-consolidated", "standalone"} else "UNKNOWN")
    cumulative = str(first(item, "cumulative", default="") or "").strip().lower()
    return {
        "security": {
            "symbol": symbol,
            "nseSymbol": symbol,
            "isin": extract_isin(item),
            "name": first(item, "companyName", "company", "comp", default=symbol),
        },
        "periodEnd": period_end,
        "quarter": fiscal_quarter(period_end),
        "released": True,
        "filingTimestamp": filing_ts,
        "source": "NSE_FINANCIAL_RESULTS",
        "xbrlUrl": extract_xbrl_url(item),
        "basis": basis,
        "cumulative": cumulative == "cumulative",
        "periodSource": period_source,
        "fromDate": parse_date(first(item, "fromDate")),
        "headline": f"{first(item, 'relatingTo', default='')} {first(item, 'period', default='')} {first(item, 'consolidated', default='')}".strip(),
        "raw": item,
    }


def normalize_nse_integrated(item: dict[str, Any]) -> dict[str, Any] | None:
    """Row of NSE's Integrated Filing (Financials) index -> filing candidate."""
    if "financ" not in str(item.get("type") or "").lower():
        return None
    symbol = normalize_symbol(first(item, "symbol"))
    period_end = parse_date(first(item, "qe_Date", "qeDate"))
    if not symbol or period_end is None:
        return None
    filed = parse_datetime(first(item, "broadcast_Date", "broadcastDate", "creation_Date"))
    revised = parse_datetime(item.get("revised_Date"))
    nature = str(item.get("consolidated") or "").strip().lower()
    basis = "CONSOLIDATED" if nature.startswith("consolidated") else ("STANDALONE" if nature.startswith("standalone") else "UNKNOWN")
    xbrl = str(item.get("xbrl") or "").strip()
    if not xbrl.startswith("http") or xbrl.rstrip("/").endswith("/null"):
        xbrl = None
    m = period_end.month - 2
    from_date = date(period_end.year if m > 0 else period_end.year - 1, m if m > 0 else m + 12, 1)
    return {
        "security": {"symbol": symbol, "nseSymbol": symbol, "isin": None,
                     "name": first(item, "cmName", "smName", default=symbol)},
        "periodEnd": period_end,
        "quarter": fiscal_quarter(period_end),
        "released": True,
        "filingTimestamp": filed,
        "revisedAt": revised,
        "source": "NSE_INTEGRATED_FILING",
        "xbrlUrl": xbrl,
        "basis": basis,
        "cumulative": False,
        "periodSource": "EXCHANGE_PERIOD_FIELD",
        "fromDate": from_date,
        "headline": f"Integrated Filing - Financials {item.get('consolidated') or ''} {item.get('type_Sub') or ''}".strip(),
        "raw": item,
    }


def normalize_nse_meeting(item: dict[str, Any]) -> dict[str, Any] | None:
    purpose = str(first(item, "bm_purpose", "purpose", "bmPurpose", "subject", "description", "bm_desc", default="") or "")
    if "result" not in purpose.lower():
        return None
    symbol = normalize_symbol(first(item, "bm_symbol", "symbol", "Symbol", "symbolCode", "securitySymbol"))
    d = parse_date(first(item, "bm_date", "meetingDate", "bmDate", "date", "boardMeetingDate"))
    if not symbol or d is None:
        return None
    period_end = expected_period_end(d)
    return {
        "security": {
            "symbol": symbol,
            "nseSymbol": symbol,
            "name": first(item, "sm_name", "companyName", "company", "comp", default=symbol),
            "sector": first(item, "sm_indusrty", "industry"),
            "isin": extract_isin(item),
        },
        "periodEnd": period_end,
        "quarter": fiscal_quarter(period_end),
        "released": False,
        "resultDate": d,
        "source": "NSE_FINANCIAL_RESULTS",
        "raw": item,
    }


def bse_symbol_code(item: dict[str, Any]) -> tuple[str, str | None]:
    code = first(item, "SCRIP_CD", "SCRIPCODE", "SCRIP_CODE", "scripcode", "scrip_Code", "SecurityCode", "Code")
    code_s = str(code).strip() if code not in (None, "") else None
    symbol = normalize_symbol(first(item, "SYMBOL", "symbol", "short_name", "SHORT_NAME"))
    return symbol, code_s


def normalize_bse_announcement(item: dict[str, Any]) -> dict[str, Any] | None:
    symbol, code = bse_symbol_code(item)
    event_ts = parse_datetime(first(item, "NEWS_DT", "DT_TM", "NEWS_DATE", "BroadcastDate", "date"))
    if event_ts is None:
        return None
    headline = " ".join(str(first(item, k, default="") or "") for k in ("HEADLINE", "NEWSSUB", "MORE"))
    text_period = extract_period_from_text(headline, event_ts.date())
    if text_period is not None:
        period_end, period_source = text_period, "FILING_TEXT"
    else:
        period_end, period_source = expected_period_end(event_ts.date()), "INFERRED_FROM_FILING_DATE"
    low = headline.lower()
    basis = "CONSOLIDATED" if "consolidated" in low else ("STANDALONE" if "standalone" in low else "UNKNOWN")
    return {
        "security": {
            "symbol": symbol,
            "bseSymbol": symbol or None,
            "bseCode": code,
            "isin": extract_isin(item),
            "name": first(item, "SLONGNAME", "LONG_NAME", "COMPANYNAME", "CompanyName", default=symbol or code),
        },
        "periodEnd": period_end,
        "quarter": fiscal_quarter(period_end),
        "released": True,
        "filingTimestamp": event_ts,
        "source": "BSE_RESULT_ANNOUNCEMENT",
        "xbrlUrl": extract_xbrl_url(item),
        "basis": basis,
        "periodSource": period_source,
        "headline": headline.strip()[:300],
        "raw": item,
    }


def normalize_bse_calendar(item: dict[str, Any]) -> dict[str, Any] | None:
    symbol, code = bse_symbol_code(item)
    d = parse_date(first(item, "meeting_date", "ResultDate", "RESULTDATE", "MeetingDate", "BoardMeetingDate", "date"))
    if d is None:
        return None
    period_end = expected_period_end(d)
    return {
        "security": {
            "symbol": symbol,
            "bseSymbol": symbol or None,
            "bseCode": code,
            "isin": extract_isin(item),
            "name": first(item, "SLONGNAME", "LONG_NAME", "COMPANYNAME", "CompanyName", default=symbol or code),
        },
        "periodEnd": period_end,
        "quarter": fiscal_quarter(period_end),
        "released": False,
        "resultDate": d,
        "source": "BSE_RESULT_ANNOUNCEMENT",
        "raw": item,
    }


FILING_HISTORY_MAX = 20


def record_filing(event: dict[str, Any], candidate: dict[str, Any], raw_ref: str | None) -> None:
    """Keep every exchange filing seen for this event (evidence + XBRL choice)."""
    filing_ts = candidate.get("filingTimestamp")
    entry = {
        "source": candidate.get("source"),
        "filedAt": filing_ts.isoformat() if filing_ts else None,
        "periodEnd": candidate["periodEnd"].isoformat() if candidate.get("periodEnd") else None,
        "periodSource": candidate.get("periodSource"),
        "basis": candidate.get("basis") or "UNKNOWN",
        "cumulative": candidate.get("cumulative"),
        "xbrlUrl": candidate.get("xbrlUrl"),
        "headline": candidate.get("headline"),
        "rawRef": raw_ref,
        "seenAt": iso_now(),
    }
    filings = event.setdefault("filings", [])
    key = (entry["source"], entry["filedAt"], entry["xbrlUrl"])
    for existing in filings:
        if (existing.get("source"), existing.get("filedAt"), existing.get("xbrlUrl")) == key:
            existing.update({k: v for k, v in entry.items() if v is not None})
            break
    else:
        filings.append(entry)
    filings.sort(key=lambda f: str(f.get("filedAt") or ""))
    del filings[:-FILING_HISTORY_MAX]


def select_xbrl_filing(event: dict[str, Any]) -> dict[str, Any] | None:
    """Pick one XBRL document for the event quarter.

    Preference: matching period > consolidated > non-cumulative > latest filed
    (a later filing for the same period is usually a revision). Standalone and
    consolidated documents are never blended; the parser reads only one.
    """
    period_end = event.get("period", {}).get("end")
    options = [f for f in event.get("filings") or [] if f.get("xbrlUrl") and f.get("periodEnd") == period_end]
    if not options:
        return None
    return max(
        options,
        key=lambda f: (
            f.get("basis") == "CONSOLIDATED",
            f.get("cumulative") is not True,
            str(f.get("filedAt") or ""),
        ),
    )


def apply_discovery(store: EventStore, master: SymbolMaster, candidate: dict[str, Any], raw_ref: str | None = None) -> dict[str, Any]:
    security = candidate["security"]
    master.merge(security)
    period_end = candidate.get("periodEnd")
    filing_ts = candidate.get("filingTimestamp")
    if candidate.get("released") and period_end and filing_ts and filing_ts.date() < period_end:
        # A result cannot be filed before its own period ends. This only happens
        # when a date was parsed wrongly; never let it create a declared event.
        return {}
    if candidate.get("released") and candidate.get("periodSource") == "INFERRED_FROM_FILING_DATE":
        # 2.9.4: a filing whose period is only guessed from its date never
        # declares a result (NATURO: "Revised Outcome Of The Board Meeting Held
        # On Thursday, November 20, 2025", filed 9 Oct 2026, was shown as a
        # declared Q2 FY27 result). The raw payload stays saved; promotion needs
        # a filing that names the period (integrity_pass).
        return {}
    event = store.ensure_event(security=security, period_end=period_end, quarter=candidate.get("quarter"))
    source = candidate["source"]
    if candidate.get("released"):
        record_filing(event, candidate, raw_ref)
        store.merge_field(event, "results_released", True, source=source, raw_ref=raw_ref)
        effective_result_date = (
            candidate.get("filingTimestamp").date()
            if candidate.get("filingTimestamp")
            else candidate.get("resultDate") or now_ist().date()
        )
        store.merge_field(event, "result_date", effective_result_date.isoformat(), source=source, raw_ref=raw_ref)
        if candidate.get("filingTimestamp"):
            store.merge_field(event, "filing_timestamp", candidate["filingTimestamp"].isoformat(), source=source, raw_ref=raw_ref)
        store.merge_field(event, "result_source", source, source=source, raw_ref=raw_ref)
        store.set_state(event, "RESULT_FILED", "official exchange result filing/announcement detected")
    else:
        rd_meta = (event.get("fields") or {}).get("result_date") or {}
        detached = (candidate.get("resultDate") is not None and rd_meta.get("status") == "REVOKED"
                    and str(rd_meta.get("revokedReason") or "").startswith("meeting ")
                    and rd_meta.get("value") == candidate["resultDate"].isoformat())
        if detached:
            # 2.9.4: this meeting was shown to be for an earlier quarter.
            store.save(event)
            return event
        if candidate.get("resultDate"):
            store.merge_field(event, "result_date", candidate["resultDate"].isoformat(), source=source, raw_ref=raw_ref)
        if boolish(store.value(event, "results_released")) is not True:
            store.set_state(event, "SCHEDULED", "board meeting/result calendar discovered")
    chosen = select_xbrl_filing(event)
    if chosen:
        store.force_field(
            event, "xbrl_url", chosen["xbrlUrl"], source=chosen.get("source") or source,
            raw_ref=chosen.get("rawRef"), note=f"basis={chosen.get('basis')} cumulative={chosen.get('cumulative')}",
        )
    store.save(event)
    return event


# ---------------------------------------------------------------------------
# Financial result parsing helpers
# ---------------------------------------------------------------------------


def record_period_end(record: dict[str, Any]) -> date | None:
    return parse_date(first(record, "re_to_dt", "toDate", "periodEnd", "endDate", "period", "quarterEnd"))


def metric(record: dict[str, Any], *aliases: str) -> float | None:
    if not isinstance(record, dict):
        return None
    lowered = {str(k).lower(): v for k, v in record.items()}
    for alias in aliases:
        value = lowered.get(alias.lower())
        x = safe_num(value)
        if x is not None:
            return x
    for k, value in lowered.items():
        if any(alias.lower() in k for alias in aliases):
            x = safe_num(value)
            if x is not None:
                return x
    return None


def margin_bps(now: Any, before: Any) -> float | None:
    a, b = safe_num(now), safe_num(before)
    if a is None or b is None or abs(a) > 150 or abs(b) > 150:
        return None
    return round((a - b) * 100)


def _exact_metric(record: dict[str, Any] | None, *keys: str) -> tuple[float | None, str | None]:
    """Exact-key lookup only (no substring fallback that could pick PBT/OCI)."""
    if not isinstance(record, dict):
        return None, None
    for key in keys:
        x = safe_num(record.get(key))
        if x is not None:
            return x, key
    return None, None


def parse_nse_comparison(payload: dict[str, Any], period_end: date) -> dict[str, Any]:
    """NSE 'results comparison' (5 quarters, values in ₹ lakh).

    * Revenue = re_net_sale (revenue from operations). re_total_inc includes
      other income and is used only as a flagged fallback.
    * Only true quarterly rows (80–100 day from→to span) are used, so half-year
      or annual rows can never be read as a quarter.
    * Current row must end within 3 days of the event period end.
    * This endpoint publishes the standalone statement; the basis is recorded as
      STANDALONE with an explicit assumption flag.
    """
    records = []
    if isinstance(payload, dict):
        records = payload.get("resCmpData") or payload.get("data") or []
    if not isinstance(records, list):
        return {}
    rows: list[tuple[date, dict[str, Any]]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        to_d = parse_date(first(r, "re_to_dt", "toDate"))
        from_d = parse_date(first(r, "re_from_dt", "fromDate"))
        if to_d is None:
            continue
        if from_d is not None and not (QUARTER_DAYS[0] <= (to_d - from_d).days <= QUARTER_DAYS[1]):
            continue
        rows.append((to_d, r))

    def row_for(target: date) -> dict[str, Any] | None:
        matches = [r for d, r in rows if abs((d - target).days) <= 3]
        if not matches:
            return None
        # A later filing for the same quarter (revision) wins.
        return max(matches, key=lambda r: str(first(r, "re_seq_num", default="") or ""))

    current = row_for(period_end)
    if current is None:
        return {}
    prior = row_for(_shift_quarters(period_end, -4))
    prev = row_for(_shift_quarters(period_end, -1))
    bank = str(payload.get("bankNonBnking") or "").upper() == "Y"

    issues: list[str] = ["BASIS_ASSUMED_STANDALONE"]
    rev_keys = ("re_int_earned", "re_net_sale") if bank else ("re_net_sale",)
    revenue, rev_key = _exact_metric(current, *rev_keys)
    definition = "INTEREST_EARNED" if rev_key == "re_int_earned" else "REVENUE_FROM_OPERATIONS"
    if revenue is None:
        revenue, rev_key = _exact_metric(current, "re_total_inc", "re_tot_inc")
        definition = "TOTAL_INCOME"
        if revenue is not None:
            issues.append("REVENUE_IS_TOTAL_INCOME")
    pat, pat_key = _exact_metric(current, "re_net_profit", "re_con_pro_loss")
    eps, _ = _exact_metric(current, "re_basic_eps_for_cont_dic_opr", "re_basic_eps")
    same_rev = (rev_key,) if rev_key else ()
    same_pat = (pat_key,) if pat_key else ()
    prior_rev, _ = _exact_metric(prior, *same_rev)
    prior_pat, _ = _exact_metric(prior, *same_pat)
    prev_rev, _ = _exact_metric(prev, *same_rev)
    prev_pat, _ = _exact_metric(prev, *same_pat)

    def cr(lakh: float | None) -> float | None:
        return lakh / 100 if lakh is not None else None

    def opm(row: dict[str, Any] | None, sales: float | None) -> float | None:
        """Operating margin = (PBT + interest + depreciation - other income) / revenue."""
        if row is None or not sales or bank:
            return None
        pbt, _ = _exact_metric(row, "re_pro_loss_bef_tax", "re_pro_loss_bef_tax_sum")
        if pbt is None:
            return None
        interest, _ = _exact_metric(row, "re_int_new")
        dep, _ = _exact_metric(row, "re_depr_und_exp")
        oth, _ = _exact_metric(row, "re_oth_inc_new", "re_oth_inc")
        ebitda = pbt + (interest or 0) + (dep or 0) - (oth or 0)
        return ebitda / sales * 100

    opm_now = opm(current, revenue)
    opm_prev = opm(prev, prev_rev)
    opm_prior = opm(prior, prior_rev)
    trend, pat_yoy = pat_trend(cr(pat), cr(prior_pat))
    return {
        "revenue_cr": round2(cr(revenue)),
        "pat_cr": round2(cr(pat)),
        "eps": round2(eps),
        "prior_year_revenue_cr": round2(cr(prior_rev)),
        "prior_year_pat_cr": round2(cr(prior_pat)),
        "revenue_yoy_pct": round2(pct_change(revenue, prior_rev)),
        "pat_yoy_pct": round2(pat_yoy),
        "pat_trend": trend,
        "revenue_qoq_pct": round2(pct_change(revenue, prev_rev)),
        "pat_qoq_pct": round2(pat_change_pct(pat, prev_pat)),
        "pat_qoq_trend": pat_trend(pat, prev_pat)[0],
        "opm_pct": round2(opm_now),
        "opm_prev_q_pct": round2(opm_prev),
        "opm_prior_year_pct": round2(opm_prior),
        "margin_change_bps": margin_bps(opm_now, opm_prior if opm_prior is not None else opm_prev),
        "basis": "STANDALONE",
        "_meta": {
            "periodEnd": period_end.isoformat(),
            "revenueDefinition": definition if revenue is not None else None,
            "patQoQRawPct": round2(pct_change(pat, prev_pat)),
            "concepts": {"revenue": rev_key, "pat": pat_key},
            "issues": issues,
            "comparativesFromSameDocument": True,
            "filedOn": str(first(current, "re_create_dt", default="") or "") or None,
        },
    }


def parse_bse_snapshot(snapshot: dict[str, Any], period_end: date) -> dict[str, Any]:
    """BSE 'results snapshot' (₹ crore, latest 1–2 quarters + last FY).

    * The event quarter must be an exact month/year column; there is no
      "use the first column if close enough" fallback any more.
    * QoQ uses only the column for the immediately preceding quarter, never a
      full-year (FYxx-yy) column.
    * BSE does not say whether these are standalone or consolidated figures and
      provides no prior-year quarter, so YoY stays null (—) from this source.
    """
    if not isinstance(snapshot, dict):
        return {}
    block = snapshot.get("results_in_crores")
    if not isinstance(block, dict):
        return {}
    unit = str(snapshot.get("currency_unit") or "").lower()
    if unit and "cr" not in unit:
        return {}
    fields = block.get("fields") or []
    data = block.get("data") or []
    if len(fields) < 2 or not data:
        return {}
    periods = [str(x).strip() for x in fields[1:]]

    def norm_title(x: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()

    table = {norm_title(str(row[0])): row[1:] for row in data if isinstance(row, list) and row}

    def column_for(target: date) -> int | None:
        for i, label in enumerate(periods):
            if not re.fullmatch(r"[A-Za-z]{3}-\d{2}", label):
                continue  # skip FY25-26, Period2, etc.
            d = parse_date(label)
            if d and d.year == target.year and d.month == target.month:
                return i
        return None

    current_idx = column_for(period_end)
    if current_idx is None:
        return {}
    fy_idx = next((i for i, label in enumerate(periods) if re.fullmatch(r"FY\d{2}-\d{2}", label)), None)
    prev_idx = column_for(_shift_quarters(period_end, -1))
    prior_idx = column_for(_shift_quarters(period_end, -4))

    def val(title: str, idx: int | None) -> float | None:
        values = table.get(title)
        if values is None or idx is None or not (0 <= idx < len(values)):
            return None
        return safe_num(values[idx])

    revenue, pat, eps = val("revenue", current_idx), val("net profit", current_idx), val("eps", current_idx)
    opm_now, opm_prev, opm_prior = val("opm", current_idx), val("opm", prev_idx), val("opm", prior_idx)
    prev_rev, prev_pat = val("revenue", prev_idx), val("net profit", prev_idx)
    prior_rev, prior_pat = val("revenue", prior_idx), val("net profit", prior_idx)
    trend, pat_yoy = pat_trend(pat, prior_pat)
    return {
        "revenue_cr": round2(revenue),
        "pat_cr": round2(pat),
        "eps": round2(eps),
        "prior_year_revenue_cr": round2(prior_rev),
        "prior_year_pat_cr": round2(prior_pat),
        "revenue_yoy_pct": round2(pct_change(revenue, prior_rev)),
        "pat_yoy_pct": round2(pat_yoy),
        "pat_trend": trend,
        "revenue_qoq_pct": round2(pct_change(revenue, prev_rev)),
        "pat_qoq_pct": round2(pat_change_pct(pat, prev_pat)),
        "pat_qoq_trend": pat_trend(pat, prev_pat)[0],
        "opm_pct": round2(opm_now),
        "opm_prev_q_pct": round2(opm_prev),
        "opm_prior_year_pct": round2(opm_prior),
        "margin_change_bps": margin_bps(opm_now, opm_prior if opm_prior is not None else opm_prev),
        "basis": "UNKNOWN",
        "_meta": {
            "periodEnd": period_end.isoformat(),
            "revenueDefinition": "BSE_SNAPSHOT_REVENUE" if revenue is not None else None,
            "patQoQRawPct": round2(pct_change(pat, prev_pat)),
            "concepts": {"revenue": "Revenue", "pat": "Net Profit"},
            "issues": ["BASIS_UNKNOWN"] + ([] if prior_idx is not None else ["NO_PRIOR_YEAR_COLUMN"]),
            "comparativesFromSameDocument": True,
            "reference": {"fy_revenue_cr": val("revenue", fy_idx), "fy_pat_cr": val("net profit", fy_idx)},
        },
    }


# ---------------------------------------------------------------------------
# Price normalization and reaction metrics
# ---------------------------------------------------------------------------


NSE_SERIES_STALE_DAYS = 7
NSE_ALT_PRICE_SERIES = ("BE", "BZ", "SM", "ST")


def _nse_row_date(r: dict[str, Any]) -> date | None:
    return parse_date(first(r, "mTIMESTAMP", "mtimestamp", "CH_TIMESTAMP", "chTimestamp", "date", "Date"))


def nse_rows_last_date(rows: list[Any]) -> date | None:
    ds = [d for d in (_nse_row_date(r) for r in rows if isinstance(r, dict)) if d]
    return max(ds) if ds else None


def merge_nse_history_rows(rows: list[Any], more: list[Any]) -> list[Any]:
    """One row per session across series (a stock trades in one at a time)."""
    by: dict[date, dict[str, Any]] = {}
    for r in list(rows) + list(more):
        d = _nse_row_date(r) if isinstance(r, dict) else None
        if d is not None:
            by[d] = r
    return [by[d] for d in sorted(by)]


def nse_history_to_frame(records: Any):
    if pd is None or not isinstance(records, list) or not records:
        return None
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        # NSE renamed its history fields (2025: CH_CLOSING_PRICE -> chClosingPrice,
        # mTIMESTAMP -> mtimestamp). Both spellings are accepted; before this fix
        # every NSE history was silently discarded and BSE close-only data used.
        d = parse_date(first(r, "mTIMESTAMP", "mtimestamp", "CH_TIMESTAMP", "chTimestamp", "date", "Date"))
        if d is None:
            continue
        series = str(first(r, "CH_SERIES", "chSeries", default="EQ") or "EQ").upper()
        if series not in {"EQ", "BE", "BZ", "SM", "ST"}:
            continue
        rows.append({
            "Date": pd.Timestamp(d),
            "Open": safe_num(first(r, "CH_OPENING_PRICE", "chOpeningPrice", "open", "Open")),
            "High": safe_num(first(r, "CH_TRADE_HIGH_PRICE", "chTradeHighPrice", "high", "High")),
            "Low": safe_num(first(r, "CH_TRADE_LOW_PRICE", "chTradeLowPrice", "low", "Low")),
            "Close": safe_num(first(r, "CH_CLOSING_PRICE", "chClosingPrice", "close", "Close")),
            "Volume": safe_num(first(r, "CH_TOT_TRADED_QTY", "chTotTradedQty", "volume", "Volume")),
        })
    if not rows:
        return None
    frame = pd.DataFrame(rows).dropna(subset=["Close"]).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    return pead_plus.adjust_corporate_actions(frame)


def bse_history_to_frame(payload: Any):
    if pd is None or not isinstance(payload, dict):
        return None
    block = payload.get("Data") or payload.get("data") or {}
    if isinstance(block, dict):
        fields = block.get("fields") or []
        rows = block.get("data") or []
    else:
        return None
    if not fields or not rows:
        return None
    lookup = {str(name).lower(): i for i, name in enumerate(fields)}
    date_i = lookup.get("dttm")
    close_i = lookup.get("vale1")
    volume_i = lookup.get("vole")
    high_i = lookup.get("highe")
    low_i = lookup.get("lowe")
    open_i = lookup.get("opene")
    if date_i is None or close_i is None:
        return None
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        d = parse_date(row[date_i] if date_i < len(row) else None)
        close = safe_num(row[close_i] if close_i < len(row) else None)
        if d is None or close is None:
            continue
        out.append({
            "Date": pd.Timestamp(d),
            "Open": safe_num(row[open_i]) if open_i is not None and open_i < len(row) else None,
            "High": safe_num(row[high_i]) if high_i is not None and high_i < len(row) else None,
            "Low": safe_num(row[low_i]) if low_i is not None and low_i < len(row) else None,
            "Close": close,
            "Volume": safe_num(row[volume_i]) if volume_i is not None and volume_i < len(row) else None,
        })
    if not out:
        return None
    return pead_plus.adjust_corporate_actions(pd.DataFrame(out).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True))


def normalize_yahoo_frame(frame: Any):
    if pd is None or frame is None or getattr(frame, "empty", True):
        return None
    f = frame.copy().reset_index()
    date_col = next((c for c in f.columns if str(c).lower() in {"date", "datetime", "index"}), f.columns[0])
    rename = {date_col: "Date"}
    for name in ("Open", "High", "Low", "Close", "Volume"):
        if name in f.columns:
            rename[name] = name
    f = f.rename(columns=rename)
    if "Date" not in f.columns or "Close" not in f.columns:
        return None
    f["Date"] = pd.to_datetime(f["Date"], errors="coerce").dt.tz_localize(None)
    for name in ("Open", "High", "Low", "Close", "Volume"):
        if name in f.columns:
            f[name] = pd.to_numeric(f[name], errors="coerce")
        else:
            f[name] = None
    return f.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)


PRE_RESULT_MAX_GAP_DAYS = 10
PRE_RESULT_FIELDS = ("pre_result_5d_pct", "pre_result_10d_pct", "pre_result_20d_pct")


def drop_stale_pre_result(store: "EventStore", event: dict[str, Any], metrics: dict[str, Any]) -> bool:
    """Withdraw stored run-up values when the price history before the result is
    months old (untraded scrip): they were computed from stale closes."""
    if not metrics.pop("_preResultStale", False):
        return False
    return any([store.revoke_field(event, f, "no trades in the sessions before the result") for f in PRE_RESULT_FIELDS])


def price_metrics(frame: Any, reaction_date: date | None, window_start: date | None = None) -> dict[str, Any]:
    if pd is None or frame is None or getattr(frame, "empty", True):
        return {}
    f = frame.copy()
    f["Date"] = pd.to_datetime(f["Date"], errors="coerce").dt.tz_localize(None)
    f = f.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)
    if f.empty:
        return {}

    def pct_idx(a: int, b: int):
        old = safe_num(f.loc[a, "Close"])
        new = safe_num(f.loc[b, "Close"])
        return round2(pct_change(new, old))

    reaction_idx = None
    if reaction_date is not None:
        candidates = f.index[f["Date"].dt.date >= reaction_date].tolist()
        reaction_idx = candidates[0] if candidates else None
    # Two-session window for intraday filings: measure from the close before
    # the filing day; highs/lows/volume span both sessions.
    win_idx = reaction_idx
    if reaction_idx is not None and window_start is not None and window_start < reaction_date:
        w = f.index[(f["Date"].dt.date >= window_start) & (f.index <= reaction_idx)].tolist()
        win_idx = w[0] if w else reaction_idx
    pre_end = win_idx if win_idx is not None else len(f)
    # 2.9.4: a run-up needs recent sessions. ALSTONE (no trades since May)
    # showed 5D/10D 0.0% from months-old closes; missing is not zero.
    today = now_ist().date()
    ref = min(window_start or reaction_date or today, today)
    last_pre = f.loc[pre_end - 1, "Date"].date() if pre_end >= 1 else None
    pre_stale = last_pre is None or (ref - last_pre).days > PRE_RESULT_MAX_GAP_DAYS

    def pre_move(n: int):
        if pre_stale or pre_end < n + 1:
            return None
        return pct_idx(pre_end - n - 1, pre_end - 1)

    close = pd.to_numeric(f["Close"], errors="coerce")
    latest = safe_num(close.iloc[-1])
    high_52 = safe_num(close.tail(252).max()) if len(close) else None
    out = {
        "pre_result_5d_pct": pre_move(5),
        "pre_result_10d_pct": pre_move(10),
        "pre_result_20d_pct": pre_move(20),
        "_preResultStale": pre_stale,
        "distance_52w_high_pct": round2(pct_change(latest, high_52)) if latest is not None and high_52 not in (None, 0) else None,
        "last_price": round2(latest),
        "result_day_return_pct": None,
        "result_day_rvol": None,
        "result_day_low": None,
        "result_day_high": None,
        "post_result_hold_5d": None,
        "post_result_hold_10d": None,
        "box_high": None,
        "box_breakout": None,
        "avg_turnover_20d_cr": None,
    }

    if "Volume" in f.columns:
        volume = pd.to_numeric(f["Volume"], errors="coerce")
        turnover = close * volume / 1e7
        if turnover.tail(20).notna().any():
            out["avg_turnover_20d_cr"] = round(float(turnover.tail(20).mean()), 4)

    if reaction_idx is not None and reaction_idx < len(f):
        if win_idx > 0:
            out["result_day_return_pct"] = pct_idx(win_idx - 1, reaction_idx)
        window = f.loc[win_idx:reaction_idx]
        lows = pd.to_numeric(window["Low"], errors="coerce").dropna()
        highs_w = pd.to_numeric(window["High"], errors="coerce").dropna()
        closes_w = pd.to_numeric(window["Close"], errors="coerce").dropna()
        result_low = safe_num(lows.min()) if not lows.empty else safe_num(closes_w.min())
        result_high = safe_num(highs_w.max()) if not highs_w.empty else safe_num(closes_w.max())
        out["result_day_low"] = round2(result_low)
        out["result_day_high"] = round2(result_high)

        if "Volume" in f.columns:
            vols_w = pd.to_numeric(window["Volume"], errors="coerce").dropna()
            rv = safe_num(vols_w.max()) if not vols_w.empty else None
            prior = pd.to_numeric(f.loc[max(0, win_idx - 20):win_idx - 1, "Volume"], errors="coerce").dropna()
            avg = float(prior.mean()) if not prior.empty else None
            if rv is not None and avg not in (None, 0):
                out["result_day_rvol"] = round2(rv / avg)

        for n in (5, 10):
            end = min(len(f), reaction_idx + n + 1)
            segment = f.iloc[reaction_idx + 1:end]
            if len(segment) >= min(n, 1) and result_low is not None:
                lows = pd.to_numeric(segment["Low"], errors="coerce").dropna()
                if lows.empty:
                    lows = pd.to_numeric(segment["Close"], errors="coerce").dropna()
                if not lows.empty:
                    out[f"post_result_hold_{n}d"] = bool((lows >= result_low).all())

        # Simple Darvas-style post-result box: first 5 sessions after reaction.
        box = f.iloc[win_idx:min(len(f), reaction_idx + 6)]
        highs = pd.to_numeric(box["High"], errors="coerce").dropna()
        if highs.empty:
            highs = pd.to_numeric(box["Close"], errors="coerce").dropna()
        if not highs.empty:
            box_high = float(highs.max())
            out["box_high"] = round2(box_high)
            if latest is not None:
                out["box_breakout"] = bool(latest > box_high * 1.002)

    return out


# ---------------------------------------------------------------------------
# Bootstrap / v1 migration
# ---------------------------------------------------------------------------


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("stocks", "companies", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _canonical_v1_source(row: dict[str, Any]) -> str:
    text = " ".join(
        str(first(row, key, default="") or "")
        for key in ("resultSource", "resultDataSource", "discoverySource", "resultsEvidence", "resultSourceUrl")
    ).lower()
    if "nse" in text and ("financial" in text or "result" in text or "filing" in text):
        return "NSE_FINANCIAL_RESULTS"
    if "bse" in text and "snapshot" in text:
        return "BSE_RESULTS_SNAPSHOT"
    if "bse" in text and ("result" in text or "announcement" in text or "filing" in text):
        return "BSE_RESULT_ANNOUNCEMENT"
    if "yahoo" in text:
        return "YAHOO_QUARTERLY"
    return "V1_MIGRATION"


def _lakh_to_cr(value: Any) -> float | None:
    x = safe_num(value)
    return x / 100 if x is not None else None


def bootstrap_from_v1(store: EventStore, master: SymbolMaster) -> int:
    """One-time, idempotent migration of the last known good v1 feed.

    The old implementation skipped migration as soon as *any* v2 event file
    existed.  A partial run could therefore create exchange-discovered events
    and permanently prevent the previous good data.json from being preserved.
    A committed marker is safer: until migration completes successfully, the
    v1 feed may be replayed and merged non-destructively.
    """
    if V1_MIGRATION_MARKER_PATH.exists() or not DATA_PATH.exists():
        return 0
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    count = 0
    for row in extract_rows(payload):
        symbol = normalize_symbol(first(row, "symbol", "sym", "ticker", "code"))
        if not symbol:
            continue
        rd = parse_date(first(row, "resultDate", "result_date", "resultsDate", "earningsDate"))
        period_end = parse_date(first(row, "resultPeriodEnd")) or expected_period_end(rd)
        ticker = str(first(row, "yahooTicker", "ticker", default="") or "").upper()
        bse_code = first(row, "bseCode", "bse_code", "scripCode")
        security = {
            "symbol": symbol,
            "nseSymbol": symbol if ticker.endswith(".NS") or (not bse_code and not ticker.endswith(".BO")) else None,
            "bseCode": bse_code,
            "isin": first(row, "isin", "ISIN"),
            "yahooTicker": ticker or None,
            "name": first(row, "name", "company", "companyName", default=symbol),
            "sector": first(row, "sector", "industry"),
            "industry": first(row, "industry"),
        }
        master.merge(security)
        event = store.ensure_event(
            security=security,
            period_end=period_end,
            quarter=first(row, "quarter", "earningsPeriod") or fiscal_quarter(period_end),
        )
        release_source = _canonical_v1_source(row)
        released = boolish(first(row, "resultsReleased", "resultReleased"))
        if released is True:
            store.merge_field(event, "results_released", True, source=release_source, note="migrated last-known-good v1 release proof")
        if rd:
            store.merge_field(event, "result_date", rd.isoformat(), source=release_source if released else "V1_MIGRATION")
        filing_ts = parse_datetime(first(row, "resultVerifiedAt", "filingTimestamp", "broadcastDate"))
        if filing_ts and released:
            store.merge_field(event, "filing_timestamp", filing_ts.isoformat(), source=release_source)
        source_url = first(row, "resultSourceUrl")
        if source_url and any(tok in str(source_url).lower() for tok in ("xbrl", ".xml", ".zip", ".xhtml")):
            store.merge_field(event, "xbrl_url", str(source_url), source=release_source)

        # Preserve all useful last-known-good numeric fields. They have low rank
        # unless the v1 row explicitly names an official source, so v2 exchange
        # data can upgrade them later without losing anything in the meantime.
        revenue_cr = first(row, "latestRevenueCr", "revenueCr")
        if revenue_cr in (None, ""):
            revenue_cr = _lakh_to_cr(first(row, "latestRevenueLakh"))
        pat_cr = first(row, "latestPatCr", "patCr")
        if pat_cr in (None, ""):
            pat_cr = _lakh_to_cr(first(row, "latestPatLakh"))

        mappings = {
            "revenue_cr": revenue_cr,
            "pat_cr": pat_cr,
            "eps": first(row, "reportedEps", "eps"),
            "revenue_yoy_pct": first(row, "revenueYoY"),
            "pat_yoy_pct": first(row, "patYoY"),
            "pat_trend": first(row, "patYoYStatus") or ("TURNAROUND" if boolish(first(row, "patYoYTurnaround")) else None),
            "revenue_qoq_pct": first(row, "revenueQoQ"),
            "pat_qoq_pct": first(row, "patQoQ"),
            "pre_result_5d_pct": first(row, "preResult5dPct", "pre5dPct"),
            "pre_result_10d_pct": first(row, "preResult10dPct", "pre10dPct"),
            "pre_result_20d_pct": first(row, "preResultRunupPct", "pre20dPct"),
            "result_day_return_pct": first(row, "resultDayReturnPct", "resultDayPct"),
            "result_day_rvol": first(row, "relativeVolume", "rvol"),
            "distance_52w_high_pct": first(row, "distanceFrom52wHighPct"),
            "market_cap_cr": first(row, "marketCapCr", "mcapCr"),
            "last_price": first(row, "price", "lastPrice", "ltp"),
            "avg_turnover_20d_cr": first(row, "avgTurnover20dCr"),
            "trailing_pe": first(row, "trailingPE"),
            "forward_pe": first(row, "forwardPE"),
            "peg": first(row, "peg"),
            "roe_pct": first(row, "roePct"),
            "fcf_yield_pct": first(row, "fcfYieldPct"),
        }
        financial_source = _canonical_v1_source(row) if _canonical_v1_source(row) in {
            "NSE_FINANCIAL_RESULTS", "BSE_RESULTS_SNAPSHOT", "YAHOO_QUARTERLY"
        } else "V1_MIGRATION"
        for field, value in mappings.items():
            if value in (None, ""):
                continue
            src = financial_source if field in FINANCIAL_FIELDS else "V1_MIGRATION"
            store.merge_field(event, field, value, source=src, note="migrated from v1 data.json")

        if released is True:
            store.set_state(event, "RESULT_FILED", "migrated last-known-good released result")
            if store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
                store.set_state(event, "FINANCIALS_PARSED", "migrated current-quarter financials")
        elif rd:
            store.set_state(event, "SCHEDULED", "migrated result calendar row")
        store.save(event)
        count += 1
    master.save()
    if count > 0:
        V1_MIGRATION_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        json_dump_atomic(
            V1_MIGRATION_MARKER_PATH,
            {
                "completedAt": iso_now(),
                "rowsProcessed": count,
                "source": "data.json",
                "engineVersion": ENGINE_VERSION,
            },
        )
    return count


def bootstrap_from_v1_intelligence(store: EventStore) -> int:
    """Fill migration gaps from the old intelligence.json without downgrading data."""
    if not INTELLIGENCE_PATH.exists():
        return 0
    try:
        payload = json.loads(INTELLIGENCE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return 0

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for event in store.all():
        sym = normalize_symbol(event.get("security", {}).get("symbol") or event.get("security", {}).get("nseSymbol"))
        q = str(event.get("period", {}).get("quarter") or "")
        if sym:
            index[(sym, q)] = event

    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = normalize_symbol(item.get("symbol"))
        q = str(item.get("quarter") or "")
        event = index.get((sym, q))
        if event is None:
            # Fall back to symbol only when there is a single event for it.
            matches = [e for (s, _), e in index.items() if s == sym]
            event = matches[0] if len(matches) == 1 else None
        if event is None:
            continue
        fs = item.get("fundamentalSnapshot") or {}
        pc = item.get("priceContext") or {}
        vm = (item.get("valuationReality") or {}).get("metrics") or {}
        values = {
            "revenue_yoy_pct": item.get("revenueYoY") if item.get("revenueYoY") is not None else fs.get("revenueYoYCalc"),
            "pat_yoy_pct": item.get("patYoY") if item.get("patYoY") is not None else fs.get("patYoYCalc"),
            "pat_trend": item.get("patYoYStatus") or fs.get("patYoYStatus") or ("TURNAROUND" if item.get("patYoYTurnaround") or fs.get("patYoYTurnaround") else None),
            "pre_result_5d_pct": pc.get("pre5dPct"),
            "pre_result_10d_pct": pc.get("pre10dPct"),
            "pre_result_20d_pct": pc.get("pre20dPct"),
            "result_day_return_pct": pc.get("resultDayPct"),
            "result_day_rvol": pc.get("relativeVolume"),
            "distance_52w_high_pct": pc.get("distanceFrom52wHighPct"),
            "last_price": pc.get("lastClose"),
            "trailing_pe": vm.get("trailingPE"),
            "forward_pe": vm.get("forwardPE"),
            "peg": vm.get("peg"),
            "roe_pct": vm.get("roePct"),
            "fcf_yield_pct": vm.get("fcfYieldPct"),
        }
        local_changed = False
        for field, value in values.items():
            if value not in (None, ""):
                local_changed |= store.merge_field(event, field, value, source="V1_MIGRATION", note="migrated from v1 intelligence.json")
        if local_changed:
            store.save(event)
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_events(store: EventStore, master: SymbolMaster, ctx: SourceContext) -> dict[str, Any]:
    stats = {"nseFilings": 0, "nseUpcoming": 0, "bseFilings": 0, "bseUpcoming": 0, "errors": []}
    now_naive = now_ist().replace(tzinfo=None)

    try:
        with NSEAdapter(ctx) as nse:
            filings, raw_ref = nse.financial_results(
                now_naive - timedelta(days=DISCOVERY_LOOKBACK_DAYS), now_naive
            )
            for item in filings:
                if not isinstance(item, dict):
                    continue
                c = normalize_nse_filing(item)
                if c:
                    apply_discovery(store, master, c, raw_ref)
                    stats["nseFilings"] += 1
            meetings, raw_ref2 = nse.board_meetings(now_naive, now_naive + timedelta(days=UPCOMING_DAYS))
            for item in meetings:
                if not isinstance(item, dict):
                    continue
                c = normalize_nse_meeting(item)
                if c:
                    apply_discovery(store, master, c, raw_ref2)
                    stats["nseUpcoming"] += 1
    except Exception as exc:
        stats["errors"].append(f"NSE discovery: {type(exc).__name__}: {exc}")

    try:
        with BSEAdapter(ctx) as bse:
            announcements, raw_ref = bse.result_announcements(
                now_naive - timedelta(days=min(DISCOVERY_LOOKBACK_DAYS, 30)), now_naive
            )
            for item in announcements:
                if not isinstance(item, dict):
                    continue
                c = normalize_bse_announcement(item)
                if c:
                    # Resolve missing BSE symbol if code exists.
                    if not c["security"].get("symbol") and c["security"].get("bseCode"):
                        try:
                            name_payload, _ = bse.scrip_name(c["security"]["bseCode"], "DISCOVERY")
                            c["security"]["symbol"] = normalize_symbol(name_payload)
                            c["security"]["bseSymbol"] = c["security"]["symbol"]
                        except Exception:
                            pass
                    apply_discovery(store, master, c, raw_ref)
                    stats["bseFilings"] += 1
            calendar, raw_ref2 = bse.result_calendar(now_naive, now_naive + timedelta(days=UPCOMING_DAYS))
            for item in calendar:
                if not isinstance(item, dict):
                    continue
                c = normalize_bse_calendar(item)
                if c:
                    apply_discovery(store, master, c, raw_ref2)
                    stats["bseUpcoming"] += 1
    except Exception as exc:
        stats["errors"].append(f"BSE discovery: {type(exc).__name__}: {exc}")

    master.save()
    return stats


# ---------------------------------------------------------------------------
# Integrity pass: re-verify declarations against saved exchange evidence
# ---------------------------------------------------------------------------

POST_RESULT_FIELDS = (
    "result_day_return_pct", "result_day_rvol", "result_day_low", "result_day_high",
    "post_result_hold_5d", "post_result_hold_10d", "box_high", "box_breakout",
)
RELEASE_FIELDS = ("results_released", "filing_timestamp", "result_source", "reaction_session", "filing_session")


def _security_keys(sec: dict[str, Any]) -> set[str]:
    keys = set()
    if sec.get("isin"):
        keys.add("ISIN:" + str(sec["isin"]).upper())
    for alt in sec.get("altIsins") or []:
        keys.add("ISIN:" + str(alt).upper())
    if sec.get("bseCode"):
        keys.add("BSE:" + str(sec["bseCode"]).strip())
    if sec.get("nseSymbol"):
        keys.add("NSE:" + normalize_symbol(sec["nseSymbol"]))
    return keys


def build_evidence_index(raw_root: Path = RAW_DIR) -> dict[str, list[dict[str, Any]]]:
    """Re-normalise every saved exchange discovery payload with today's
    (corrected) parsers. Evidence = official filing + explicit/derived period."""
    index: dict[str, list[dict[str, Any]]] = {}
    sources = (
        (raw_root / "bse" / "DISCOVERY", "result_announcements-*.json", normalize_bse_announcement),
        (raw_root / "nse" / "DISCOVERY", "financial_results-*.json", normalize_nse_filing),
        # 2.5.4: per-company NSE result listings are evidence too.
        (raw_root / "nse", "[!D]*/financial_results-*.json", normalize_nse_filing),
        (raw_root / "nse", "[!D]*/integrated_filings-*.json", normalize_nse_integrated),
    )
    for folder, pattern, normalizer in sources:
        if not folder.exists():
            continue
        for path in sorted(folder.glob(pattern)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = payload if isinstance(payload, list) else (payload.get("data") or payload.get("Table") or []) if isinstance(payload, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                c = normalizer(item)
                if not c or not c.get("released") or not c.get("periodEnd") or not c.get("filingTimestamp"):
                    continue
                c = dict(c)
                c.pop("raw", None)
                c["rawRef"] = str(path.relative_to(raw_root.parent)) if raw_root.parent in path.parents else str(path)
                for key in _security_keys(c["security"]):
                    index.setdefault(key, []).append(c)
    return index


def _detach_calendar_date_of_other_period(store: EventStore, event: dict[str, Any], period_end: date,
                                          index: dict[str, list[dict[str, Any]]], sec_keys: list[str],
                                          stats: dict[str, Any]) -> bool:
    """BSE's result calendar gives a meeting date but no period; the period is
    guessed from the date. A late filer's meeting for an EARLIER quarter then
    lands on this quarter (2.9.4: SRUSTEELS / KRRAIL met on 8-9 Oct 2026 for
    their June-2026 results and were listed as Q2 results due that day). When
    the company filed results naming an earlier period on the meeting day (or
    within two days), the date belongs to that period, not this one."""
    meta = (event.get("fields") or {}).get("result_date") or {}
    meeting = parse_date(meta.get("value")) if meta.get("status") == "OK" else None
    if meeting is None:
        return False
    for c in (c for k in sec_keys for c in index.get(k, [])):
        if (c.get("periodSource") in EXPLICIT_PERIOD_SOURCES and c["periodEnd"] < period_end
                and 0 <= (c["filingTimestamp"].date() - meeting).days <= 2):
            store.revoke_field(event, "result_date",
                               f"meeting {meeting.isoformat()} was for period {c['periodEnd'].isoformat()} "
                               f"(filing {c['filingTimestamp'].date().isoformat()} names it)")
            stats["calendarDatesDetached"] = stats.get("calendarDatesDetached", 0) + 1
            if event.get("state") == "SCHEDULED":
                store.force_state(event, "DISCOVERED", "calendar date belonged to an earlier quarter")
            return True
    return False


def _revoke_release(store: EventStore, event: dict[str, Any], reasons: list[str], today: date) -> dict[str, Any]:
    reason = "; ".join(reasons)
    before = {f: store.value(event, f) for f in ("results_released", "result_date", "filing_timestamp")}
    for field in RELEASE_FIELDS + POST_RESULT_FIELDS:
        store.revoke_field(event, field, f"release evidence invalid: {reason}")
    rd_meta = (event.get("fields") or {}).get("result_date") or {}
    rd = parse_date(rd_meta.get("value"))
    if rd_meta.get("status") == "OK" and (rd_meta.get("source") in {"BSE_RESULT_ANNOUNCEMENT", "NSE_FINANCIAL_RESULTS", "NSE_INTEGRATED_FILING"} or (rd and rd < today)):
        store.revoke_field(event, "result_date", f"release evidence invalid: {reason}")
    for field in FINANCIAL_FIELDS:
        store.revoke_field(event, field, "financials belonged to a revoked declaration")
    event.pop("financialIntegrity", None)
    has_calendar = store.value(event, "result_date") is not None
    store.force_state(event, "SCHEDULED" if has_calendar else "DISCOVERED", f"declaration revoked: {reason}")
    entry = {"at": iso_now(), "reasons": reasons, "before": before}
    event.setdefault("integrity", {}).setdefault("revocations", []).append(entry)
    return entry


def rederive_snapshots_from_raw(event: dict[str, Any], raw_root: Path = RAW_DIR) -> int:
    """Replay saved NSE/BSE result payloads through the strict parsers."""
    period_end = parse_date(event.get("period", {}).get("end"))
    if period_end is None:
        return 0
    # Payloads are company-level (multi-quarter) and the parsers select the
    # event's own period column/row, so any saved payload for the same company
    # (including folders of duplicate/sibling events) can be replayed safely.
    sec = event.get("security") or {}
    prefixes = {safe_filename(event["eventId"]).rsplit("_", 1)[0]}
    for oid in event.get("mergedFrom") or []:
        prefixes.add(safe_filename(oid).rsplit("_", 1)[0])
    for alt in sec.get("altIsins") or []:
        prefixes.add(safe_filename(str(alt).upper()))
    if sec.get("isin"):
        prefixes.add(safe_filename(str(sec["isin"]).upper()))
    if sec.get("nseSymbol") or sec.get("symbol"):
        prefixes.add(safe_filename("NSE:" + normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))))
    replay = (
        ("NSE_RESULTS_COMPARISON", raw_root / "nse", "results_comparison-*.json", parse_nse_comparison),
        ("BSE_RESULTS_SNAPSHOT", raw_root / "bse", "results_snapshot-*.json", parse_bse_snapshot),
    )
    added = 0
    for source, base, pattern, parser in replay:
        current = ((event.get("financialSnapshots") or {}).get(source) or {}).get("parserVersion") == SNAPSHOT_PARSER_VERSION
        if not base.exists() or (_has_snapshot(event, source) and current):
            continue
        paths = sorted(
            path for prefix in prefixes for folder in base.glob(f"{prefix}_*") if folder.is_dir()
            for path in folder.glob(pattern)
        )
        for path in sorted(set(paths), key=lambda x: x.stat().st_mtime):
            try:
                parsed = parser(json.loads(path.read_text(encoding="utf-8")) or {}, period_end)
            except Exception:
                continue
            if parsed:
                rel = str(path.relative_to(raw_root.parent)) if raw_root.parent in path.parents else str(path)
                snap = store_financial_snapshot(event, source, parsed, raw_ref=rel)
                added += int(snap["validation"]["status"] != "REJECTED")
    return added


_NAME_STOP = {"ltd", "limited", "the", "co", "company", "corp", "corporation", "inc", "and", "of", "pvt", "private"}


def _issuer_code(isin: Any) -> str | None:
    s = str(isin or "").strip().upper()
    return s[3:7] if re.fullmatch(r"IN[A-Z0-9]{10}", s) else None


def _name_tokens(name: Any) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", str(name or "").lower().replace("&", " and "))
    return [t for t in toks if t not in _NAME_STOP]


def same_company(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Guard before folding two events that share a symbol or BSE code.
    Two ISINs: same issuer code (INE690A01010 / INE690A01028 = old and
    post-split ISIN of one company). Otherwise the first two name words must
    agree; with no names, only an identical BSE code counts."""
    ta, tb = _name_tokens(a.get("name")), _name_tokens(b.get("name"))
    ja, jb = "".join(ta), "".join(tb)
    names_match = bool(ta and tb) and (
        ta[:2] == tb[:2] or (min(len(ta), len(tb)) == 1 and ta[0] == tb[0])
        or (min(len(ja), len(jb)) >= 8 and ja[:12] == jb[:12]))   # "Extrusiontechnik" vs "Extrusion Technik"
    ia, ib = _issuer_code(a.get("isin")), _issuer_code(b.get("isin"))
    if ia and ib and ia != ib:
        # Different issuer codes: only the same NSE symbol AND the same full
        # name overrides (one of the two ISINs is malformed/stale).
        na, nb = normalize_symbol(a.get("nseSymbol")), normalize_symbol(b.get("nseSymbol"))
        return bool(na and na == nb and ja and ja == jb)
    if ia and ib:
        return True
    if ta and tb:
        return names_match
    bc_a, bc_b = str(a.get("bseCode") or "").strip(), str(b.get("bseCode") or "").strip()
    return bool(bc_a and bc_a == bc_b)


def _merge_keys(sec: dict[str, Any]) -> set[str]:
    keys = set()
    for k in ("nseSymbol", "symbol", "bseSymbol"):
        sym = normalize_symbol(sec.get(k))
        if sym and not sym.endswith((".BO", ".NS")):
            keys.add("SYM:" + sym)
    bc = str(sec.get("bseCode") or "").strip()
    if bc.isdigit():
        keys.add("BSE:" + bc)
    return keys


def merge_duplicate_events(store: EventStore) -> list[dict[str, Any]]:
    """One company + one period = one event.

    Regressions: GOLKONDA appeared twice (NSE:GOLKONDA|… and INE327C01031|…);
    in 2.5.2, 82 companies appeared twice in the watchlist because the NSE
    list gave one copy (ISIN + NSE symbol) and the BSE list another (BSE code
    + scrip id, sometimes the post-split ISIN, e.g. TTKPRESTIG INE690A01010 vs
    INE690A01028), and the copies shared no identical key. Events of the same
    period sharing an NSE symbol / BSE scrip id / BSE code are folded into one
    when same_company() agrees. Survivor: has ISIN, then has NSE symbol, then
    most fields. The duplicate id becomes an alias of the survivor.
    """
    events = store.all()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    by_id = {e["eventId"]: e for e in events}
    seen: dict[tuple[str, str], list[str]] = {}
    for e in events:
        pe = (e.get("period") or {}).get("end")
        if not pe:
            continue
        sec = e.get("security") or {}
        for k in _merge_keys(sec):
            for other in seen.get((k, pe), []):
                if find(other) != find(e["eventId"]) and same_company(sec, by_id[other].get("security") or {}):
                    parent[find(e["eventId"])] = find(other)
            seen.setdefault((k, pe), []).append(e["eventId"])

    groups: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        groups.setdefault(find(e["eventId"]), []).append(e)

    def rank(e: dict[str, Any]) -> tuple:
        sec = e.get("security") or {}
        ok = sum(1 for m in (e.get("fields") or {}).values() if isinstance(m, dict) and m.get("status") == "OK")
        return (bool(sec.get("isin")), bool(sec.get("nseSymbol")), ok, e["eventId"])

    merged: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=rank, reverse=True)
        target = members[0]
        for e in members[1:]:
            both_declared = (boolish(store.value(e, "results_released")) is True
                             and boolish(store.value(target, "results_released")) is True)
            target = store.fold_into(target, e)
            merged.append({"eventId": e["eventId"], "symbol": (e.get("security") or {}).get("symbol"), "at": iso_now(),
                           "reasons": [f"DUPLICATE_MERGED_INTO:{target['eventId']}"], "before": {},
                           "bothDeclared": both_declared})
    return merged


def integrity_pass(store: EventStore, *, today: date | None = None, raw_root: Path = RAW_DIR,
                   master: "SymbolMaster | None" = None) -> dict[str, Any]:
    """Idempotent repair run before scoring/publishing.

    1. Every declared event must be backed by an exchange filing for the SAME
       period (re-checked with corrected date/period parsing). Matching evidence
       re-writes filing timestamp and result date; a filing in the future, a
       filing dated before period end, a filing for another period, or a
       migrated v1 'released' flag with no evidence revokes the declaration.
    2. Post-result fields (reaction, RVOL, box…) can only exist after a valid
       release whose reaction session has occurred.
    3. Financial snapshots are replayed from saved raw payloads and exactly one
       validated snapshot is applied.
    Revocation keeps the old values with status REVOKED for audit.
    """
    today = today or now_ist().date()
    index = build_evidence_index(raw_root)
    stats: dict[str, Any] = {"checked": 0, "reverified": 0, "revoked": 0, "postResultPurged": 0,
                             "snapshotsReplayed": 0, "financialsApplied": 0, "revocations": []}
    repaired = repair_lookup_mismatches(store, master, raw_root)
    stats["identityRepaired"] = len(repaired)
    stats["revocations"].extend(repaired)
    merges = merge_duplicate_events(store)
    stats["duplicatesMerged"] = len(merges)
    stats["declaredMerged"] = sum(1 for r in merges if r.get("bothDeclared"))
    # Merges change the event count, so they are part of the approval signature.
    stats["revocations"].extend(merges)
    for event in store.all():
        period_end = parse_date(event.get("period", {}).get("end"))
        if period_end is None:
            continue
        changed = False
        released = boolish(store.value(event, "results_released")) is True
        if released:
            stats["checked"] += 1
            sec_keys = _security_keys(event.get("security") or {})
            evidence = [c for k in sec_keys for c in index.get(k, [])]
            # 2.9.4: only evidence that names the period keeps a declaration
            # (same rule as promotion below); a date-inferred period does not.
            inferred_only = [c for c in evidence if c["periodEnd"] == period_end
                             and c.get("periodSource") not in EXPLICIT_PERIOD_SOURCES]
            matching = [c for c in evidence if c["periodEnd"] == period_end
                        and c.get("periodSource") in EXPLICIT_PERIOD_SOURCES]
            if matching:
                best = min(matching, key=lambda c: c["filingTimestamp"])
                ts = best["filingTimestamp"]
                store.force_field(event, "results_released", True, source=best["source"], raw_ref=best["rawRef"],
                                  note="re-verified from saved exchange evidence")
                store.force_field(event, "filing_timestamp", ts.isoformat(), source=best["source"], raw_ref=best["rawRef"])
                store.force_field(event, "result_date", ts.date().isoformat(), source=best["source"], raw_ref=best["rawRef"])
                for c in matching:
                    record_filing(event, c, c["rawRef"])
                new_session, timing = reaction_session(ts, None)
                old_session = store.value(event, "reaction_session")
                if new_session and old_session != new_session.isoformat():
                    store.force_field(event, "reaction_session", new_session.isoformat(), source="DERIVED", note=timing)
                    store.force_field(event, "filing_session", timing, source="DERIVED")
                    ws = reaction_window_start(ts, timing, new_session)
                    if ws and ws != new_session:
                        store.force_field(event, "reaction_window_start", ws.isoformat(), source="DERIVED", note="intraday filing")
                    for f in POST_RESULT_FIELDS:
                        store.revoke_field(event, f, f"reaction session corrected {old_session} -> {new_session}; recompute")
                    (event.setdefault("fetch", {}).get("PRICE_HISTORY") or {}).pop("lastSuccess", None)
                stats["reverified"] += 1
                changed = True
            else:
                rel = (event.get("fields") or {}).get("results_released") or {}
                fts = parse_datetime(store.value(event, "filing_timestamp"))
                rd = parse_date(store.value(event, "result_date"))
                filed_on = fts.date() if fts else rd
                reasons = []
                if filed_on and filed_on > today:
                    reasons.append("FUTURE_FILING_DATE")
                if filed_on and filed_on < period_end:
                    reasons.append("FILED_BEFORE_PERIOD_END")
                if "migrated" in str(rel.get("note") or "") and not rel.get("rawRef"):
                    reasons.append("UNVERIFIED_MIGRATED_RELEASE")
                other_periods = sorted({c["periodEnd"].isoformat() for c in evidence if c["periodEnd"] != period_end})
                if inferred_only:
                    reasons.append("ONLY_DATE_INFERRED_EVIDENCE")
                if other_periods:
                    reasons.append("EVIDENCE_IS_FOR_OTHER_PERIOD:" + ",".join(other_periods))
                if reasons:
                    entry = _revoke_release(store, event, reasons, today)
                    stats["revoked"] += 1
                    stats["revocations"].append({"eventId": event["eventId"], "symbol": event.get("security", {}).get("symbol"), **entry})
                    changed = True
                    released = False

        elif period_end == live_reporting_period(today) or period_end >= today - timedelta(days=200):
            # Promote a scheduled/discovered event only on an official filing that
            # EXPLICITLY names this period (never on a date-inferred period).
            sec_keys = _security_keys(event.get("security") or {})
            explicit = [
                c for k in sec_keys for c in index.get(k, [])
                if c["periodEnd"] == period_end and c.get("periodSource") in EXPLICIT_PERIOD_SOURCES
                and c["filingTimestamp"].date() <= today
            ]
            if explicit:
                best = min(explicit, key=lambda c: c["filingTimestamp"])
                ts = best["filingTimestamp"]
                store.force_field(event, "results_released", True, source=best["source"], raw_ref=best["rawRef"],
                                  note="declared from saved exchange evidence (explicit period)")
                store.force_field(event, "filing_timestamp", ts.isoformat(), source=best["source"], raw_ref=best["rawRef"])
                store.force_field(event, "result_date", ts.date().isoformat(), source=best["source"], raw_ref=best["rawRef"])
                store.force_field(event, "result_source", best["source"], source=best["source"], raw_ref=best["rawRef"])
                session, timing = reaction_session(ts, None)
                if session:
                    store.force_field(event, "reaction_session", session.isoformat(), source="DERIVED", note=timing)
                    store.force_field(event, "filing_session", timing, source="DERIVED")
                for c in explicit:
                    record_filing(event, c, c["rawRef"])
                store.set_state(event, "RESULT_FILED", "official filing naming this period found in saved evidence")
                (event.setdefault("fetch", {}).get("PRICE_HISTORY") or {}).pop("lastSuccess", None)
                stats["promoted"] = stats.get("promoted", 0) + 1
                released = True
                changed = True
            else:
                changed |= _detach_calendar_date_of_other_period(store, event, period_end, index, sec_keys, stats)

        # Post-result fields are impossible without a released, already-traded reaction session.
        reaction = parse_date(store.value(event, "reaction_session"))
        if not released or (reaction is not None and reaction > today):
            purged = sum(store.revoke_field(event, f, "no valid released result / reaction session not yet traded") for f in POST_RESULT_FIELDS)
            if purged:
                stats["postResultPurged"] += 1
                changed = True
            if not released:
                for field in FINANCIAL_FIELDS:
                    changed |= store.revoke_field(event, field, "financial fields require a verified release for this period")

        if released:
            replayed = rederive_snapshots_from_raw(event, raw_root)
            stats["snapshotsReplayed"] += replayed
            if event.get("financialSnapshots") or any(((event.get("fields") or {}).get(f) or {}).get("status") == "OK" for f in FINANCIAL_FIELDS):
                integ = apply_financial_snapshots(store, event)
                stats["financialsApplied"] += int(integ.get("selectedSource") is not None)
                changed = True
        if changed:
            store.save(event)
    return stats


def append_price_snapshot(event: dict[str, Any], price: Any, source: str | None) -> None:
    """Keep a compact hourly price trail for active-event tracking.

    Snapshots are non-authoritative UI telemetry. Official/exchange price fields
    remain in the field store with normal source precedence.
    """
    p = safe_num(price)
    if p is None or p <= 0:
        return
    trail = event.get("priceTrail")
    if not isinstance(trail, list):
        trail = []
        event["priceTrail"] = trail

    ts = iso_now()
    hour_bucket = ts[:13]
    snap = {
        "timestamp": ts,
        "price": round2(p),
        "source": source or field_source(event, "last_price") or "UNKNOWN",
    }
    if trail and isinstance(trail[-1], dict) and str(trail[-1].get("timestamp") or "")[:13] == hour_bucket:
        trail[-1] = snap
    else:
        trail.append(snap)
    event["priceTrail"] = trail[-PRICE_TRAIL_MAX_POINTS:]


def price_trail_change(event: dict[str, Any]) -> float | None:
    trail = event.get("priceTrail")
    if not isinstance(trail, list):
        return None
    prices = []
    for rec in trail:
        if not isinstance(rec, dict):
            continue
        p = safe_num(rec.get("price"))
        if p is not None and p > 0:
            prices.append(p)
    if len(prices) < 2:
        return None
    # Move since the first tracked price (hour-over-hour was almost always
    # 0.0% for thinly traded stocks and looked like a fake zero).
    return round2(pct_change(prices[-1], prices[0]))


def latest_price_timestamp(event: dict[str, Any]) -> str | None:
    trail = event.get("priceTrail")
    if isinstance(trail, list) and trail and isinstance(trail[-1], dict):
        return trail[-1].get("timestamp")
    return None




# ---------------------------------------------------------------------------
# Event enrichment
# ---------------------------------------------------------------------------


def pick_lookup_row(rows: Any, symbol: str | None, name: Any = None) -> dict[str, Any] | None:
    """NSE lookup is a fuzzy search: "ALKALI" returns GUJALKALI first,
    "AGRITECH" returns DHANUKA, "DEEPA" returns DEEPAKNTR. Accept only the row
    whose symbol is exactly the one asked for, or a row whose company name
    matches the name we already have (2.5.4). Otherwise: no NSE mapping."""
    if not isinstance(rows, list):
        return None
    rows = [r for r in rows if isinstance(r, dict)]
    want = normalize_symbol(symbol)
    for r in rows:
        if want and normalize_symbol(r.get("symbol")) == want:
            return r
    if name:
        for r in rows[:3]:
            if r.get("companyName") and same_company({"name": name}, {"name": r.get("companyName")}):
                return r
    return None


def _lookup_guesses(raw_root: Path = RAW_DIR) -> dict[str, dict[str, Any]]:
    """Saved NSE lookups whose first row was NOT the symbol asked for:
    {queried symbol: {"guess": symbol taken, "exact": exact row or None}}."""
    out: dict[str, dict[str, Any]] = {}
    base = raw_root / "nse"
    if not base.exists():
        return out
    for folder in base.glob("NSE_*"):
        q = normalize_symbol(folder.name[4:].rsplit("_", 1)[0])
        for f in folder.glob("lookup-*.json"):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            guess = normalize_symbol(rows[0].get("symbol"))
            if q and guess and guess != q:
                exact = next((r for r in rows if isinstance(r, dict) and normalize_symbol(r.get("symbol")) == q), None)
                out[q] = {"guess": guess, "exact": exact}
    return out


_NSE_IDENTITY_KEYS = ("nseSymbol", "yahooTicker", "basicIndustry", "macroSector", "sectorIndex", "exchangeSector",
                      "sector", "industry")


def repair_lookup_mismatches(store: EventStore, master: "SymbolMaster | None" = None,
                             raw_root: Path = RAW_DIR) -> list[dict[str, Any]]:
    """Undo identities created by the old first-row NSE lookup (pre-2.5.4).
    An event whose BSE symbol Q was looked up on NSE and got another company's
    symbol N loses N and every NSE/Yahoo-sourced field (prices, quote, sector,
    concall) so the next run refetches them for the right company. N is
    replaced by Q only when NSE lists Q itself under a matching name."""
    guesses = _lookup_guesses(raw_root)
    bse_names: dict[str, str] = {}
    if master is not None:
        for k, v in (master.data.get("securities") or {}).items():
            if isinstance(v, dict) and k.startswith("NSE:") and not v.get("nseSymbol") and v.get("name"):
                bse_names[normalize_symbol(k[4:])] = v["name"]
    fixed: list[dict[str, Any]] = []
    for e in store.all():
        sec = e.get("security") or {}
        n = normalize_symbol(sec.get("nseSymbol"))
        for q in {normalize_symbol(sec.get("bseSymbol")), normalize_symbol(sec.get("symbol"))} - {None, "", n}:
            g = guesses.get(q)
            if not n or not g or g["guess"] != n:
                continue
            bse_name = bse_names.get(q)
            exact = g["exact"]
            new_n = None
            if exact and (not bse_name or same_company({"name": bse_name}, {"name": exact.get("companyName")})):
                new_n = q
            old_id = e["eventId"]
            for k in _NSE_IDENTITY_KEYS:
                sec.pop(k, None)
            if new_n:
                sec["nseSymbol"] = new_n
                sec["yahooTicker"] = f"{new_n}.NS"
            elif str(sec.get("bseCode") or "").isdigit():
                sec["yahooTicker"] = f"{sec['bseCode']}.BO"
            sec["symbol"] = q
            if bse_name or (exact and new_n):
                sec["name"] = bse_name or exact.get("companyName")
            sec["identityRepaired"] = {"wrongNseSymbol": n, "at": iso_now()}
            for f_name in list((e.get("fields") or {}).keys()):
                src = str((e["fields"][f_name] or {}).get("source") or "")
                if src.startswith(("NSE_PRICE", "NSE_QUOTE", "YAHOO", "NSE_COMPARISON", "NSE_XBRL")) or src == "DERIVED_PRICE":
                    e["fields"].pop(f_name, None)
            for extra in ("plus", "priceTrail", "concall", "tradeLog", "mergedFrom"):
                e.pop(extra, None)
            # Aliases that pointed at this event under the wrong NSE symbol go.
            for k in [k for k, v in store.aliases().items() if v == old_id or (n and k.startswith(f"NSE:{n}|"))]:
                store.aliases().pop(k, None)
            store.save(e)
            if store._alias_path().exists() or store.aliases():
                json_dump_atomic(store._alias_path(), store.aliases())
            merged = store.rekey_and_merge(e)
            if merged.get("eventId") != old_id:
                store.aliases().pop(old_id, None)          # never route N back here
                json_dump_atomic(store._alias_path(), store.aliases())
            if master is not None:
                for k in [k for k, v in master.data["securities"].items()
                          if isinstance(v, dict) and normalize_symbol(v.get("nseSymbol")) == n
                          and str(v.get("bseCode") or "") == str(sec.get("bseCode") or "")]:
                    master.data["securities"].pop(k, None)
            fixed.append({"eventId": old_id, "symbol": q, "at": iso_now(), "before": {"nseSymbol": n},
                          "reasons": [f"IDENTITY_REPAIRED:{n}->{new_n or 'BSE_ONLY'}"]})
            break
    fixed += _repair_bse_lookup_mismatches(store, raw_root, master)
    if master is not None:
        master.save()
    return fixed


_BSE_IDENTITY_KEYS = ("macroSector", "exchangeSector", "industry", "basicIndustry", "bseGroup")


def _repair_bse_lookup_mismatches(store: EventStore, raw_root: Path = RAW_DIR,
                                  master: "SymbolMaster | None" = None) -> list[dict[str, Any]]:
    """Undo BSE codes attached by a BSE lookup that returned a different
    company (NSE:BRIGHT 'Bright Solar' got 543831 'Bright Outdoor Media')."""
    base = raw_root / "bse"
    if not base.exists():
        return []
    fixed = []
    for e in store.all():
        sec = e.get("security") or {}
        code, name = str(sec.get("bseCode") or ""), sec.get("name")
        if not code or not name or not sec.get("nseSymbol"):
            # 2.9.1: events repaired by 2.9.0 still carry the other company's
            # sector / P/E / alias / mergedFrom; finish the clean-up.
            done = (sec.get("identityRepaired") or {}).get("wrongBseCode")
            if done and not code and sec.get("nseSymbol"):
                _clean_wrong_bse_traces(store, e, str(done), raw_root, master)
            continue
        prefix = safe_filename(e["eventId"]).rsplit("_", 1)[0]
        wrong = None
        for f in base.glob(f"{prefix}_*/lookup-*.json"):
            try:
                pl = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(pl, dict) and str(pl.get("bse_code") or "") == code and pl.get("company_name") \
                    and not same_company({"name": name}, {"name": pl["company_name"]}):
                wrong = pl["company_name"]
                break
        if not wrong:
            continue
        for k in ("bseCode", "bseSymbol", "bseGroup"):
            sec.pop(k, None)
        if str(sec.get("yahooTicker") or "").endswith(".BO"):
            sec["yahooTicker"] = f"{normalize_symbol(sec['nseSymbol'])}.NS"
        sec["identityRepaired"] = {"wrongBseCode": code, "wrongBseName": wrong, "at": iso_now()}
        bse_pe = store.value(e, "exchange_pe") if field_source(e, "exchange_pe") == "BSE_META" else None
        for f_name in list((e.get("fields") or {}).keys()):
            src = str((e["fields"][f_name] or {}).get("source") or "")
            if src.startswith("BSE_") or (f_name in {"reaction_session", "filing_session"} and src == "DERIVED"):
                e["fields"].pop(f_name, None)
        if bse_pe is not None and safe_num(store.value(e, "trailing_pe")) == safe_num(bse_pe):
            e["fields"].pop("trailing_pe", None)
        if str((e.get("plus") or {}).get("priceSource") or "").startswith("BSE"):
            e.pop("plus", None)
        for extra in ("concall", "tradeLog", "financialSnapshots", "financialIntegrity"):
            e.pop(extra, None)
        _clean_wrong_bse_traces(store, e, code, raw_root, master, save=False)
        store.save(e)
        fixed.append({"eventId": e["eventId"], "symbol": sec.get("symbol"), "at": iso_now(), "before": {"bseCode": code},
                      "reasons": [f"IDENTITY_REPAIRED:BSE {code} ({wrong}) removed"]})
    return fixed


def _clean_wrong_bse_traces(store: EventStore, e: dict[str, Any], code: str, raw_root: Path,
                            master: "SymbolMaster | None", save: bool = True) -> bool:
    """Remove what the wrong BSE code left behind (2.9.1): sector labels and
    P/E that equal the other company's saved BSE meta, mergedFrom / aliases
    keyed by that code (re-pointed to the event that owns the code), and the
    code in the symbol master. Idempotent; returns True when something changed."""
    sec = e.setdefault("security", {})
    eid = e["eventId"]
    changed = False
    prefix = safe_filename(eid).rsplit("_", 1)[0]
    wrong_isins = set()
    for f in (raw_root / "bse").glob(f"{prefix}_*/lookup-*.json"):
        try:
            pl = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(pl, dict) and str(pl.get("bse_code") or "") == code and pl.get("isin"):
            wrong_isins.add(str(pl["isin"]).upper())
    wrong_ident: dict[str, set] = {}
    wrong_pe: set = set()
    for f in (raw_root / "bse").glob(f"{prefix}_*/equity_meta-*.json"):
        try:
            pl = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(pl, dict) or str(pl.get("ISIN") or "").upper() not in wrong_isins:
            continue
        parsed = parse_bse_meta(pl)
        for k, v in (parsed.get("identity") or {}).items():
            if v:
                wrong_ident.setdefault(k, set()).add(v)
        if parsed.get("exchange_pe") is not None:
            wrong_pe.add(safe_num(parsed["exchange_pe"]))
    for k in _BSE_IDENTITY_KEYS:
        if sec.get(k) is not None and sec.get(k) in wrong_ident.get(k, set()):
            sec.pop(k, None)
            changed = True
    for f_name in ("trailing_pe", "exchange_pe", "pb", "roe_pct", "exchange_opm_ttm_pct"):
        meta = (e.get("fields") or {}).get(f_name) or {}
        if f_name == "trailing_pe" and safe_num(meta.get("value")) in wrong_pe and wrong_pe:
            e["fields"].pop(f_name, None)
            changed = True
        elif f_name != "trailing_pe" and str(meta.get("source") or "").startswith("BSE_"):
            e["fields"].pop(f_name, None)
            changed = True
    bse_ids = {i for i in (e.get("mergedFrom") or []) if str(i).startswith(f"BSE:{code}|")}
    if bse_ids:
        e["mergedFrom"] = [i for i in e["mergedFrom"] if i not in bse_ids] or None
        if not e["mergedFrom"]:
            e.pop("mergedFrom", None)
        changed = True
    owner = next((o["eventId"] for o in store.all() if o["eventId"] != eid
                  and str((o.get("security") or {}).get("bseCode") or "") == code
                  and (o.get("period") or {}).get("end") == (e.get("period") or {}).get("end")), None)
    aliases = store.aliases()
    stale = [k for k, v in aliases.items() if v == eid and k.startswith(f"BSE:{code}|")]
    for k in stale:
        if owner:
            aliases[k] = owner
        else:
            aliases.pop(k, None)
    if stale:
        json_dump_atomic(store._alias_path(), aliases)
        changed = True
    if master is not None:
        for k, v in (master.data.get("securities") or {}).items():
            if isinstance(v, dict) and str(v.get("bseCode") or "") == code and \
                    normalize_symbol(v.get("nseSymbol")) == normalize_symbol(sec.get("nseSymbol")) and \
                    v.get("name") == sec.get("name"):
                for kk in ("bseCode", "bseSymbol", "bseGroup"):
                    v.pop(kk, None)
                for kk in _BSE_IDENTITY_KEYS:
                    if v.get(kk) in wrong_ident.get(kk, set()):
                        v.pop(kk, None)
                v["identityRepaired"] = sec.get("identityRepaired")
                changed = True
    if changed and save:
        store.save(e)
    return changed


def fill_security_identity(event: dict[str, Any], store: EventStore, master: SymbolMaster, ctx: SourceContext) -> dict[str, Any]:
    sec = event.get("security", {})
    symbol = normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))
    if sec.get("isin") and sec.get("bseCode") and sec.get("yahooTicker"):
        # Fully identified securities still need to return the event.  The
        # previous bare `return` produced None and crashed the enrichment loop.
        return store.rekey_and_merge(event)

    if symbol and not sec.get("isin"):
        try:
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.lookup(symbol, event["eventId"])
                rows = payload.get("data") if isinstance(payload, dict) else []
                rec = pick_lookup_row(rows, symbol, sec.get("name"))
                if rec is not None:
                    isin = extract_isin(rec)
                    if isin:
                        sec["isin"] = isin
                    sec["nseSymbol"] = normalize_symbol(first(rec, "symbol", default=symbol)) or symbol
                    sec["name"] = first(rec, "companyName", default=sec.get("name") or symbol)
                    store.record_fetch(event, "NSE_LOOKUP", ok=True, raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "NSE_LOOKUP", ok=False, error=f"{type(exc).__name__}: {exc}")

    if (not sec.get("bseCode") or not sec.get("isin")) and (sec.get("name") or symbol):
        try:
            with BSEAdapter(ctx) as bse:
                payload, raw_ref = bse.lookup(str(sec.get("name") or symbol), event["eventId"])
                bse_name = payload.get("company_name") or payload.get("companyName") if isinstance(payload, dict) else None
                # 2.6.1: BSE lookup is a search too ("Bright Solar" -> BRIGHT
                # OUTDOOR MEDIA 543831); accept it only for the same company.
                if isinstance(payload, dict) and sec.get("name") and bse_name and not same_company(
                        {"name": sec.get("name")}, {"name": bse_name}):
                    store.record_fetch(event, "BSE_LOOKUP", ok=False, raw_ref=raw_ref,
                                       error=f"lookup returned another company: {bse_name}")
                    payload = None
                if isinstance(payload, dict):
                    code = payload.get("bse_code") or payload.get("bseCode")
                    isin = payload.get("isin") or payload.get("ISIN")
                    bsym = payload.get("symbol")
                    if code:
                        sec["bseCode"] = str(code)
                    if isin and not sec.get("isin"):
                        sec["isin"] = str(isin).upper()
                    if bsym:
                        sec["bseSymbol"] = normalize_symbol(bsym)
                    store.record_fetch(event, "BSE_LOOKUP", ok=True, raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "BSE_LOOKUP", ok=False, error=f"{type(exc).__name__}: {exc}")

    if not sec.get("yahooTicker"):
        if sec.get("nseSymbol"):
            sec["yahooTicker"] = f"{normalize_symbol(sec['nseSymbol'])}.NS"
        elif str(sec.get("bseCode") or "").isdigit():
            sec["yahooTicker"] = f"{sec['bseCode']}.BO"
    master.merge(sec)
    return store.rekey_and_merge(event)


# ---------------------------------------------------------------------------
# Financial snapshots: validate per source, apply exactly one atomically
# ---------------------------------------------------------------------------

FINANCIAL_SNAPSHOT_RANK = {
    "NSE_XBRL": 110, "BSE_XBRL": 110,
    "NSE_RESULTS_COMPARISON": 100,
    "BSE_RESULTS_SNAPSHOT": 96,
    "YAHOO_QUARTERLY": 55,
}
# Issues that make a snapshot unusable.
SNAPSHOT_REJECT_ISSUES = ("NO_CORE_VALUES", "PERIOD_MISMATCH", "NEGATIVE_REVENUE", "NO_QUARTER_CONTEXT_FOR_PERIOD", "UNIT_SUSPECT")
# Issues produced by validate_financial_snapshot itself (recomputed every run);
# anything else in a snapshot's issue list came from the parser and is kept.
VALIDATOR_ISSUES = {"NO_CORE_VALUES", "PERIOD_MISMATCH", "NEGATIVE_REVENUE", "ZERO_REVENUE", "EXTREME_REVENUE_YOY",
                    "PAT_EXCEEDS_REVENUE", "UNOFFICIAL_SOURCE", "UNIT_SUSPECT", "CROSS_SOURCE_MISMATCH", "PAT_SWING"}
# Issues that keep the snapshot usable but require a visible review flag.
SNAPSHOT_FLAG_ISSUES = (
    "EXTREME_REVENUE_YOY", "PAT_EXCEEDS_REVENUE", "REVENUE_IS_TOTAL_INCOME",
    "UNOFFICIAL_SOURCE", "CROSS_SOURCE_MISMATCH", "CONFLICTING_FACTS", "NON_INR_UNIT",
    "ZERO_REVENUE",
)
CROSS_SOURCE_TOLERANCE_PCT = 2.0


def validate_financial_snapshot(parsed: dict[str, Any], source: str, period_end: date | None) -> dict[str, Any]:
    meta = parsed.get("_meta") or {}
    issues = list(meta.get("issues") or [])
    rev = safe_num(parsed.get("revenue_cr"))
    pat = safe_num(parsed.get("pat_cr"))
    if rev is None and pat is None:
        issues.append("NO_CORE_VALUES")
    if period_end is not None and meta.get("periodEnd") and meta["periodEnd"] != period_end.isoformat():
        issues.append("PERIOD_MISMATCH")
    if rev is not None and rev < 0:
        issues.append("NEGATIVE_REVENUE")
    if rev == 0:
        issues.append("ZERO_REVENUE")
    rev_yoy = safe_num(parsed.get("revenue_yoy_pct"))
    if rev_yoy is not None and abs(rev_yoy) > 300:
        issues.append("EXTREME_REVENUE_YOY")
    if rev is not None and pat is not None and rev > 0 and pat > 1.5 * rev:
        issues.append("PAT_EXCEEDS_REVENUE")
    # A >20x jump or collapse versus the previous quarter almost always means
    # the company/exchange filed in the wrong unit (₹ instead of ₹ lakh, etc.).
    # Regression: Golkonda Sep-26 revenue 1,399 Cr vs 0.15 Cr in Jun-26.
    rev_qoq = safe_num(parsed.get("revenue_qoq_pct"))
    if rev_qoq is not None and (rev_qoq > 1900 or rev_qoq < -95):
        issues.append("UNIT_SUSPECT")
    if rev_yoy is not None and (rev_yoy > 1900 or rev_yoy < -95):
        issues.append("UNIT_SUSPECT")
    # Profit 50x larger than last quarter's (in either direction) and at least
    # ₹1 Cr: Alstone Sep-26 PAT -106.72 Cr vs +0.07 Cr in Jun-26.
    pat_qoq = safe_num(parsed.get("pat_qoq_pct"))
    if pat_qoq is None:
        # 2.9.4: profit->loss swings publish no ordinary %, but the size of the
        # swing still feeds the unit check.
        pat_qoq = safe_num(meta.get("patQoQRawPct"))
    ref = meta.get("reference") or {}
    fy_rev = safe_num(ref.get("fy_revenue_cr"))
    # 2.6.2: a unit error scales EVERY figure. When revenue is in line with
    # last quarter and with last year's total, a big profit swing is a real
    # swing (LOTUSCHO: revenue -0.6% QoQ, loss -3.8 Cr after a 0.02 Cr profit),
    # so it is flagged, not rejected.
    revenue_sane = (rev is not None and rev_qoq is not None and -60 < rev_qoq < 150
                    and (fy_rev in (None, 0) or rev <= 1.5 * abs(fy_rev)))
    pat_unit_issue = "PAT_SWING" if revenue_sane else "UNIT_SUSPECT"
    if pat is not None and abs(pat) >= 1 and pat_qoq is not None and abs(pat_qoq) > 5000:
        issues.append(pat_unit_issue)
    # One quarter larger than 10x the entire previous financial year.
    for key, value in (("fy_revenue_cr", rev), ("fy_pat_cr", pat)):
        fy = safe_num(ref.get(key))
        if fy not in (None, 0) and value is not None and abs(value) >= 1 and abs(value) > 10 * abs(fy):
            issues.append("UNIT_SUSPECT" if key == "fy_revenue_cr" else pat_unit_issue)
    if FINANCIAL_SNAPSHOT_RANK.get(source, 0) < 90:
        issues.append("UNOFFICIAL_SOURCE")
    issues = sorted(set(issues))
    if any(i.split(":")[0] in SNAPSHOT_REJECT_ISSUES for i in issues):
        status = "REJECTED"
    elif any(i.split(":")[0] in SNAPSHOT_FLAG_ISSUES for i in issues):
        status = "FLAGGED"
    else:
        status = "VERIFIED"
    return {"status": status, "issues": issues}


def store_financial_snapshot(
    event: dict[str, Any], source: str, parsed: dict[str, Any], *, raw_ref: str | None = None, document_url: str | None = None,
) -> dict[str, Any]:
    """Persist one source's complete parse. A rejected/empty parse never
    replaces an earlier usable snapshot from the same source."""
    period_end = parse_date(event.get("period", {}).get("end"))
    validation = validate_financial_snapshot(parsed, source, period_end)
    meta = parsed.get("_meta") or {}
    snapshot = {
        "source": source,
        "fetchedAt": iso_now(),
        "rawRef": raw_ref,
        "documentUrl": document_url,
        "periodEnd": meta.get("periodEnd"),
        "basis": parsed.get("basis") or "UNKNOWN",
        "revenueDefinition": meta.get("revenueDefinition"),
        "concepts": meta.get("concepts"),
        "reference": meta.get("reference"),
        "values": {k: parsed.get(k) for k in FINANCIAL_FIELDS if k != "basis" and parsed.get(k) is not None},
        "validation": validation,
        "parserVersion": SNAPSHOT_PARSER_VERSION,
    }
    snaps = event.setdefault("financialSnapshots", {})
    old = snaps.get(source)
    if validation["status"] == "REJECTED" and isinstance(old, dict) and (old.get("validation") or {}).get("status") != "REJECTED":
        old["lastRejected"] = {"at": snapshot["fetchedAt"], "issues": validation["issues"], "rawRef": raw_ref}
        return old
    snaps[source] = snapshot
    return snapshot


def _snapshot_completeness(snap: dict[str, Any]) -> int:
    v = snap.get("values") or {}
    return sum(v.get(k) is not None for k in ("revenue_cr", "pat_cr", "prior_year_revenue_cr", "prior_year_pat_cr", "eps"))


def apply_financial_snapshots(store: EventStore, event: dict[str, Any]) -> dict[str, Any]:
    """Choose ONE usable snapshot and make it the visible financial record.

    Every visible financial field (revenue, PAT, YoY, QoQ, trend, basis) then
    comes from the same document. A field the winner lacks is shown as —, never
    borrowed from another source, because borrowing is how consolidated revenue
    ended up next to a standalone prior-year figure.
    """
    period_end = event.get("period", {}).get("end")
    snaps = [s for s in (event.get("financialSnapshots") or {}).values() if isinstance(s, dict)]
    # Validation rules evolve: re-check every stored snapshot so a value that
    # passed an older, weaker validator cannot stay visible.
    pe = parse_date(period_end)
    for snap in snaps:
        old_v = snap.get("validation") or {}
        parser_issues = [i for i in old_v.get("issues") or [] if i.split(":")[0] not in VALIDATOR_ISSUES]
        replay = dict(snap.get("values") or {})
        replay["_meta"] = {"periodEnd": snap.get("periodEnd"), "issues": parser_issues, "reference": snap.get("reference")}
        snap["validation"] = validate_financial_snapshot(replay, snap.get("source") or "", pe)
    usable = [
        s for s in snaps
        if (s.get("validation") or {}).get("status") in {"VERIFIED", "FLAGGED"} and s.get("periodEnd") == period_end
    ]
    integrity: dict[str, Any] = {"checkedAt": iso_now(), "snapshotsConsidered": len(snaps)}
    if not usable:
        rejected = sorted({i for s in snaps for i in (s.get("validation") or {}).get("issues") or []
                           if i.split(":")[0] in SNAPSHOT_REJECT_ISSUES})
        integrity.update({"status": "NO_VERIFIED_SNAPSHOT", "selectedSource": None, "issues": rejected or None})
        # Unverified legacy numbers, and numbers from a snapshot that is now
        # rejected, must not masquerade as parsed financials.
        for field in FINANCIAL_FIELDS:
            meta = (event.get("fields") or {}).get(field) or {}
            if meta.get("status") != "OK":
                continue
            if (meta.get("source") in {"V1_MIGRATION", "DERIVED"} or "migrated" in str(meta.get("note") or "")
                    or meta.get("source") in FINANCIAL_SNAPSHOT_RANK):
                store.revoke_field(event, field, "no usable exchange snapshot for this period: " + ",".join(rejected or ["none"]))
        if event.get("state") == "FINANCIALS_PARSED":
            store.force_state(event, "RESULT_FILED", "financial snapshot rejected on re-validation")
        event["financialIntegrity"] = integrity
        return integrity

    winner = max(
        usable,
        key=lambda s: (FINANCIAL_SNAPSHOT_RANK.get(s["source"], 0), _snapshot_completeness(s), str(s.get("fetchedAt") or "")),
    )
    cross = []
    w_rev = safe_num((winner.get("values") or {}).get("revenue_cr"))
    for other in usable:
        if other is winner:
            continue
        o_rev = safe_num((other.get("values") or {}).get("revenue_cr"))
        if w_rev in (None, 0) or o_rev is None:
            continue
        diff = abs(o_rev / w_rev - 1) * 100
        same_basis = winner.get("basis") == other.get("basis") and winner.get("basis") != "UNKNOWN"
        cross.append({"source": other["source"], "basis": other.get("basis"), "revenueCr": o_rev, "diffPct": round2(diff), "sameBasis": same_basis})
        if same_basis and diff > CROSS_SOURCE_TOLERANCE_PCT:
            v = winner.setdefault("validation", {"status": "VERIFIED", "issues": []})
            if "CROSS_SOURCE_MISMATCH" not in v["issues"]:
                v["issues"] = sorted(v["issues"] + ["CROSS_SOURCE_MISMATCH"])
            v["status"] = "FLAGGED"

    values = dict(winner.get("values") or {})
    values["basis"] = winner.get("basis") or "UNKNOWN"
    note = f"snapshot {winner['source']} basis={values['basis']}"
    for field in FINANCIAL_FIELDS:
        value = values.get(field)
        if value is not None:
            store.force_field(event, field, value, source=winner["source"], raw_ref=winner.get("rawRef"), note=note)
        else:
            meta = (event.get("fields") or {}).get(field) or {}
            if meta.get("status") == "OK":
                # 2.9.4: also when an older parse of the SAME source set it
                # (HATHWAYB kept "-350%" after the PAT-model fix re-parse).
                store.revoke_field(event, field, f"not in selected {winner['source']} snapshot"
                                   + (" (cross-source mixing prevented)" if meta.get("source") != winner["source"] else " (re-parsed)"))
    integrity.update({
        "status": (winner.get("validation") or {}).get("status"),
        "issues": (winner.get("validation") or {}).get("issues"),
        "selectedSource": winner["source"],
        "documentUrl": winner.get("documentUrl"),
        "basis": values["basis"],
        "revenueDefinition": winner.get("revenueDefinition"),
        "concepts": winner.get("concepts"),
        "periodEnd": winner.get("periodEnd"),
        "crossCheck": cross,
    })
    event["financialIntegrity"] = integrity
    if store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
        store.set_state(event, "FINANCIALS_PARSED", f"verified {winner['source']} snapshot applied")
    return integrity


def _has_snapshot(event: dict[str, Any], *sources: str) -> bool:
    snaps = event.get("financialSnapshots") or {}
    return any((snaps.get(s) or {}).get("validation", {}).get("status") in {"VERIFIED", "FLAGGED"} for s in sources)


def _exact(parsed: dict[str, Any], key: str) -> float | None:
    """Unrounded value from a parse (meta.exact), else the published one."""
    v = ((parsed.get("_meta") or {}).get("exact") or {}).get(key)
    return safe_num(v) if v is not None else safe_num(parsed.get(key))


def fill_comparatives_from_listing(parsed: dict[str, Any], listing: list[dict[str, Any]], period_end: date,
                                   basis: str | None, fetch_parse) -> dict[str, Any]:
    """NSE's quarterly XBRL often carries only the current period. When the
    year-ago (or previous-quarter) figures are missing from the document, read
    them from that quarter's own XBRL filing - same basis, same revenue/PAT
    concept - instead of leaving YoY blank (engine 2.5.4)."""
    if not parsed or parsed.get("revenue_cr") is None:
        return parsed
    meta = parsed.setdefault("_meta", {})
    concepts = meta.get("concepts") or {}
    basis = basis if basis in {"CONSOLIDATED", "STANDALONE"} else (parsed.get("basis") if parsed.get("basis") in {"CONSOLIDATED", "STANDALONE"} else None)
    for shift, need in ((-4, parsed.get("prior_year_revenue_cr") is None or parsed.get("prior_year_pat_cr") is None),
                        (-1, parsed.get("revenue_qoq_pct") is None)):
        if not need:
            continue
        target = _shift_quarters(period_end, shift)
        row = listing_row(listing, target, basis)
        if row is None:
            continue
        try:
            other = fetch_parse(row["xbrlUrl"], target) or {}
        except Exception as exc:
            meta.setdefault("issues", []).append(f"COMPARATIVE_FETCH_FAILED:{target.isoformat()}:{type(exc).__name__}")
            continue
        oc = (other.get("_meta") or {}).get("concepts") or {}
        other = {**other, **{k: v for k, v in ((other.get("_meta") or {}).get("exact") or {}).items() if v is not None}}
        same_rev = oc.get("revenue") == concepts.get("revenue")
        same_pat = oc.get("pat") == concepts.get("pat")
        rev_o = other.get("revenue_cr") if same_rev else None
        pat_o = other.get("pat_cr") if same_pat else None
        rev_n, pat_n = _exact(parsed, "revenue_cr"), _exact(parsed, "pat_cr")
        if shift == -4:
            if parsed.get("prior_year_revenue_cr") is None and rev_o is not None:
                parsed["prior_year_revenue_cr"] = rev_o
                parsed["revenue_yoy_pct"] = round2(pct_change(rev_n, rev_o))
            if parsed.get("prior_year_pat_cr") is None and pat_o is not None:
                parsed["prior_year_pat_cr"] = pat_o
                trend, yoy = pat_trend(pat_n, pat_o)
                parsed["pat_trend"], parsed["pat_yoy_pct"] = trend, round2(yoy)
        else:
            if rev_o is not None:
                parsed["revenue_qoq_pct"] = round2(pct_change(rev_n, rev_o))
            if pat_o is not None and pat_n is not None:
                parsed["pat_qoq_trend"], qoq = pat_trend(pat_n, pat_o)
                parsed["pat_qoq_pct"] = round2(qoq)
                meta["patQoQRawPct"] = round2(pct_change(pat_n, pat_o))
            meta["prevQuarter"] = {"revenue_cr": rev_o, "pat_cr": pat_o}
        if rev_o is not None or pat_o is not None:
            meta["comparativesFromSameDocument"] = False
            meta.setdefault("comparativeSources", {})[target.isoformat()] = row["xbrlUrl"]
    # 2.7.0 earnings acceleration: last quarter's own YoY = previous quarter vs
    # the quarter a year before it (one more filing, same basis and concept).
    prev = meta.get("prevQuarter") or {}
    if (prev.get("revenue_cr") is not None or prev.get("pat_cr") is not None) and parsed.get("prev_q_revenue_yoy_pct") is None:
        target = _shift_quarters(period_end, -5)
        row = listing_row(listing, target, basis)
        if row is not None:
            try:
                other = fetch_parse(row["xbrlUrl"], target) or {}
            except Exception as exc:
                other = {}
                meta.setdefault("issues", []).append(f"COMPARATIVE_FETCH_FAILED:{target.isoformat()}:{type(exc).__name__}")
            oc = (other.get("_meta") or {}).get("concepts") or {}
            o_rev, o_pat = _exact(other, "revenue_cr"), _exact(other, "pat_cr")
            if oc.get("revenue") == concepts.get("revenue") and o_rev is not None and prev.get("revenue_cr") is not None:
                parsed["prev_q_revenue_yoy_pct"] = round2(pct_change(prev["revenue_cr"], o_rev))
            if oc.get("pat") == concepts.get("pat") and o_pat is not None and prev.get("pat_cr") is not None:
                parsed["prev_q_pat_yoy_pct"] = round2(pat_change_pct(prev["pat_cr"], o_pat))
    return parsed


def enrich_financials(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    if boolish(store.value(event, "results_released")) is not True:
        return
    period_end = parse_date(event.get("period", {}).get("end"))
    if period_end is None:
        return
    sec = event.get("security", {})
    symbol = normalize_symbol(sec.get("nseSymbol"))
    bse_code = str(sec.get("bseCode") or "").strip()
    listing = nse_result_listing(event, store, ctx) if sec.get("nseSymbol") else []
    chosen = select_xbrl_filing(event)
    # 2.6.2: compare like with like. If the year-ago quarter exists only as
    # STANDALONE (GMBREW began consolidated filing in 2026), use the
    # standalone filing for this quarter too, so YoY is not left blank.
    if chosen and listing and chosen.get("basis") == "CONSOLIDATED":
        ya = _shift_quarters(period_end, -4)
        if listing_row(listing, ya, "CONSOLIDATED") is None and listing_row(listing, ya, "STANDALONE") is not None:
            alt = listing_row(listing, period_end, "STANDALONE")
            if alt is not None:
                chosen = next((f for f in event.get("filings") or [] if f.get("xbrlUrl") == alt["xbrlUrl"]),
                              {"xbrlUrl": alt["xbrlUrl"], "basis": "STANDALONE", "source": "NSE_INTEGRATED_FILING"})
    xbrl_url = (chosen or {}).get("xbrlUrl") or store.value(event, "xbrl_url")

    # 1) Exchange XBRL (highest authority). Re-fetch only for a new document.
    if xbrl_url:
        source = "NSE_XBRL" if "nse" in str(xbrl_url).lower() else "BSE_XBRL"
        existing = (event.get("financialSnapshots") or {}).get(source) or {}
        ev_vals = existing.get("values") or {}
        last_try = parse_datetime(((event.get("fetch") or {}).get(source) or {}).get("lastAttempt"))
        rd_fin = parse_date(store.value(event, "result_date"))
        retry_comparatives = (ev_vals.get("revenue_yoy_pct") is None and rd_fin is not None
                              and (now_ist().date() - rd_fin).days <= 10 and
                              (last_try is None or (now_ist() - last_try).total_seconds() > 3 * 3600))
        if (existing.get("documentUrl") != str(xbrl_url) or (existing.get("validation") or {}).get("status") == "REJECTED"
                or existing.get("parserVersion") != XBRL_PARSER_VERSION or retry_comparatives):
            try:
                parsed, raw_ref = XBRLParser(ctx).fetch_parse(str(xbrl_url), event["eventId"], period_end, source)
                fill_comparatives_from_listing(parsed, listing, period_end, (chosen or {}).get("basis"),
                                               lambda url, pe: XBRLParser(ctx).fetch_parse(url, event["eventId"], pe, source)[0])
                snap = store_financial_snapshot(event, source, parsed, raw_ref=raw_ref, document_url=str(xbrl_url))
                snap["parserVersion"] = XBRL_PARSER_VERSION
                ok = (snap.get("validation") or {}).get("status") != "REJECTED"
                store.record_fetch(event, source, ok=ok, raw_ref=raw_ref,
                                   error=None if ok else "XBRL rejected: " + ",".join(snap["validation"]["issues"]))
            except Exception as exc:
                store.record_fetch(event, source, ok=False, error=f"{type(exc).__name__}: {exc}")

    # 2) NSE results comparison: also used as an independent cross-check.
    if symbol and not _has_snapshot(event, "NSE_RESULTS_COMPARISON") and not fetch_cooling_down(event, "NSE_RESULTS_COMPARISON", 24):
        try:
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.results_comparison(symbol, event["eventId"])
            parsed = parse_nse_comparison(payload or {}, period_end)
            if parsed:
                snap = store_financial_snapshot(event, "NSE_RESULTS_COMPARISON", parsed, raw_ref=raw_ref)
                store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=snap["validation"]["status"] != "REJECTED", raw_ref=raw_ref,
                                   error=",".join(snap["validation"]["issues"]) or None)
            else:
                store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=False, error="no quarterly row for event period", raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=False, error=f"{type(exc).__name__}: {exc}")

    # 3) BSE snapshot: essential for BSE-only companies.
    if bse_code and not _has_snapshot(event, "NSE_XBRL", "BSE_XBRL", "NSE_RESULTS_COMPARISON", "BSE_RESULTS_SNAPSHOT"):
        try:
            with BSEAdapter(ctx) as bse:
                payload, raw_ref = bse.results_snapshot(bse_code, event["eventId"], symbol)
            parsed = parse_bse_snapshot(payload or {}, period_end)
            if parsed:
                snap = store_financial_snapshot(event, "BSE_RESULTS_SNAPSHOT", parsed, raw_ref=raw_ref)
                store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=snap["validation"]["status"] != "REJECTED", raw_ref=raw_ref)
            else:
                store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=False, error="snapshot has no column for event quarter", raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=False, error=f"{type(exc).__name__}: {exc}")

    # 4) Yahoo only when no exchange snapshot exists; always FLAGGED.
    if not _has_snapshot(event, *[s for s in FINANCIAL_SNAPSHOT_RANK if s != "YAHOO_QUARTERLY"]):
        data, err = YahooAdapter(ctx).quarterly(event)
        if data:
            data = dict(data)
            data["_meta"] = {"periodEnd": data.pop("statementPeriodEnd", None) or period_end.isoformat(),
                             "revenueDefinition": "YAHOO_TOTAL_REVENUE", "issues": ["BASIS_UNKNOWN"]}
            store_financial_snapshot(event, "YAHOO_QUARTERLY", data)
            store.record_fetch(event, "YAHOO_QUARTERLY", ok=True)
        elif err:
            store.record_fetch(event, "YAHOO_QUARTERLY", ok=False, error=err)

    apply_financial_snapshots(store, event)


# ---------------------------------------------------------------------------
# Exchange meta: sector, valuation, market cap, delivery (engine 2.4)
# ---------------------------------------------------------------------------

def parse_nse_quote(payload: Any) -> dict[str, Any]:
    """Accept both the new getSymbolData shape (metaData/tradeInfo/secInfo/
    priceInfo) and the older quote-equity shape (info/metadata/industryInfo)."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    sec = payload.get("secInfo") or {}
    trade = payload.get("tradeInfo") or {}
    price = payload.get("priceInfo") or {}
    meta = payload.get("metadata") or {}
    ind = payload.get("industryInfo") or {}
    out["exchange_pe"] = safe_num(first(sec, "pdSymbolPe") or first(meta, "pdSymbolPe"))
    spe = safe_num(first(sec, "pdSectorPe") or first(meta, "pdSectorPe"))
    out["sector_pe"] = spe if spe is not None and spe > 0 else None   # 2.9.4: "0" = not available
    mcap = safe_num(first(trade, "totalMarketCap"))
    out["market_cap_cr"] = round2(mcap / 1e7) if mcap else None          # rupees -> crore
    ffmc = safe_num(first(trade, "ffmc"))
    out["ffmc_cr"] = round2(ffmc / 1e7) if ffmc else None
    out["delivery_pct"] = safe_num(first(trade, "deliveryToTradedQuantity") or first(sec, "deliveryTotradedQuantity"))
    wk = price.get("weekHighLow") if isinstance(price.get("weekHighLow"), dict) else {}
    out["week52_high"] = safe_num(first(price, "yearHigh") or first(wk, "max"))
    out["week52_low"] = safe_num(first(price, "yearLow") or first(wk, "min"))
    sector_index = str(first(sec, "pdSectorInd") or first(meta, "pdSectorInd") or "").strip()
    out["identity"] = {
        "basicIndustry": first(sec, "basicIndustry") or first(ind, "basicIndustry"),
        "industry": first(ind, "industry"),
        "exchangeSector": first(ind, "sector"),
        "macroSector": first(ind, "macro"),
        "sectorIndex": sector_index if sector_index and sector_index.upper() not in {"NA", "N/A", "-"} else None,
    }
    return out


def parse_bse_meta(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    def n(key: str) -> float | None:
        v = safe_num(payload.get(key))
        return v if v not in (0,) else None

    return {
        "exchange_pe": n("PE"),
        "pb": n("PB"),
        # 2.9.4: BSE sends "0.00" when it has no figure (TIAANC, GOLKONDA).
        "roe_pct": n("ROE"),
        "exchange_opm_ttm_pct": n("OPM"),
        "identity": {
            "macroSector": payload.get("Sector") or None,
            "exchangeSector": payload.get("IndustryNew") or None,
            "industry": payload.get("IGroup") or None,
            "basicIndustry": payload.get("ISubGroup") or payload.get("Industry") or None,
            "bseGroup": payload.get("Group") or None,
        },
    }


def sector_key(sec: dict[str, Any]) -> str | None:
    """One sector name per company for peer comparison: the exchange 'Sector'
    level (≈22 groups) when known, else basic industry, else Yahoo sector."""
    # 2.5.4: always one of NSE's ~22 sector names (pead_plus.canonical_sector),
    # whichever source label we have; most specific exchange label first.
    return pead_plus.canonical_sector(sec.get("exchangeSector"), sec.get("basicIndustry"), sec.get("industry"),
                                      sec.get("sector"), sec.get("macroSector"))


def enrich_exchange_meta(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    fetch = (event.get("fetch") or {}).get("EXCHANGE_META") or {}
    last = parse_datetime(fetch.get("lastSuccess"))
    if last and (now_ist() - last).total_seconds() < EXCHANGE_META_REFRESH_HOURS * 3600:
        return
    sec = event.setdefault("security", {})
    symbol = normalize_symbol(sec.get("nseSymbol"))
    bse_code = str(sec.get("bseCode") or "").strip()
    if not _extra_budget_ok(int(bool(symbol)) + int(bool(bse_code))):
        return
    got, errors = False, []
    if symbol:
        try:
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.quote(symbol, event["eventId"], series=sec.get("nseSeries"))
            parsed = parse_nse_quote(payload)
            for field, value in parsed.items():
                if field != "identity" and value is not None:
                    store.merge_field(event, field, value, source="NSE_QUOTE", raw_ref=raw_ref)
            for k, v in (parsed.get("identity") or {}).items():
                if v:
                    sec[k] = v
            got = got or bool(parsed)
        except Exception as exc:
            errors.append(f"NSE quote: {type(exc).__name__}: {exc}")
    if bse_code:
        try:
            with BSEAdapter(ctx) as bse:
                payload, raw_ref = bse.meta(bse_code, event["eventId"], symbol)
            parsed = parse_bse_meta(payload)
            for field, value in parsed.items():
                if field != "identity" and value is not None:
                    store.merge_field(event, field, value, source="BSE_META", raw_ref=raw_ref)
            for k, v in (parsed.get("identity") or {}).items():
                if v and not sec.get(k):
                    sec[k] = v
            got = got or bool(parsed)
        except Exception as exc:
            errors.append(f"BSE meta: {type(exc).__name__}: {exc}")
    # The exchange PE is the trailing P/E; keep the legacy field in sync so the
    # existing valuation logic and sorting use exchange data before Yahoo.
    pe = store.value(event, "exchange_pe")
    if pe is not None:
        # 2.9.1: label it with the source that actually supplied exchange_pe
        # (an SME's NSE quote has no P/E, so it came from BSE meta).
        store.merge_field(event, "trailing_pe", pe,
                          source=field_source(event, "exchange_pe") or ("NSE_QUOTE" if symbol else "BSE_META"))
    store.record_fetch(event, "EXCHANGE_META", ok=got, error="; ".join(errors) or None)


_INDEX_CACHE_MEMO: dict[str, Any] = {}


LIVE_QUOTE_MAX = int(os.getenv("LIVE_QUOTE_MAX", "40"))
LIVE_SIGNALS = {"NEAR_ENTRY", "WATCH_BREAKOUT", "WAIT_RECLAIM", "ENTRY_EARLY", "ENTRY_PULLBACK", "ENTRY_BREAKOUT",
                "ENTRY_TRIGGERED", "WAIT_ACCEPTANCE", "WAIT_BOX"}


def market_open_now() -> bool:
    now = now_ist()
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 35)


def refresh_live_quotes(store: EventStore, ctx: SourceContext) -> int:
    """2.7.0: during market hours, read the live NSE price of every declared
    company that has an entry plan (for the 3 pm entry alert and the card's
    'live vs entry' line). Plans themselves still use closing prices."""
    if not market_open_now():
        return 0
    picks = []
    for e in store.all():
        if not dashboard_activity(e) or boolish(store.value(e, "results_released")) is not True:
            continue
        plan = ((e.get("derived") or {}).get("plus") or {}).get("plan") or {}
        sym = normalize_symbol((e.get("security") or {}).get("nseSymbol"))
        if sym and (plan.get("signal") in LIVE_SIGNALS or safe_num(plan.get("entry")) is not None):
            picks.append((e, sym))
    n = 0
    if not picks:
        return 0
    try:
        with NSEAdapter(ctx) as nse:
            for e, sym in picks[:LIVE_QUOTE_MAX]:
                try:
                    payload, raw_ref = nse.quote(sym, e["eventId"], series=(e.get("security") or {}).get("nseSeries"))
                    price = safe_num(first((payload or {}).get("priceInfo") or {}, "lastPrice")) if isinstance(payload, dict) else None
                    if price:
                        e["live"] = {"price": price, "at": iso_now()}
                        store.save(e)
                        n += 1
                except Exception as exc:
                    store.record_fetch(e, "LIVE_QUOTE", ok=False, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"Live quotes skipped: {type(exc).__name__}: {exc}")
    return n


MARKET_INTERNALS_PATH = MASTER_DIR / "market_internals.json"


def parse_fii_dii(payload: Any) -> dict[str, Any] | None:
    """[{category: 'FII/FPI *', date: '09-Oct-2026', buyValue, sellValue, netValue}, {category: 'DII *', ...}]"""
    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        return None
    out: dict[str, Any] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        cat = str(r.get("category") or "").upper()
        net = safe_num(r.get("netValue"))
        d = parse_date(r.get("date"))
        if net is None or d is None:
            continue
        if cat.startswith("FII") or "FPI" in cat:
            out["fiiNetCr"], out["date"] = round2(net), d.isoformat()
        elif cat.startswith("DII"):
            out["diiNetCr"], out["date"] = round2(net), d.isoformat()
    return out if "date" in out else None


def refresh_market_internals(ctx: SourceContext) -> dict[str, Any]:
    """NIFTY 500 advances/declines and FII/DII flows, kept as a 30-day history
    in master/market_internals.json. Failures keep the old file."""
    try:
        cache = json.loads(MARKET_INTERNALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    flows = {r["date"]: r for r in cache.get("flows") or [] if isinstance(r, dict) and r.get("date")}
    try:
        with NSEAdapter(ctx) as nse:
            try:
                ad, _ = nse.market_breadth("NIFTY 500")
                adv, dec = safe_num((ad or {}).get("advances")), safe_num((ad or {}).get("declines"))
                if adv is not None and dec is not None:
                    cache["advanceDecline"] = {"advances": int(adv), "declines": int(dec),
                                               "unchanged": int(safe_num((ad or {}).get("unchanged")) or 0), "at": iso_now()}
            except Exception as exc:
                print(f"Breadth fetch failed: {type(exc).__name__}: {exc}")
            try:
                fd, _ = nse.fii_dii()
                row = parse_fii_dii(fd)
                if row:
                    flows[row["date"]] = {**flows.get(row["date"], {}), **row}
            except Exception as exc:
                print(f"FII/DII fetch failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"Market internals skipped: {type(exc).__name__}: {exc}")
    cache["flows"] = [flows[k] for k in sorted(flows)][-30:]
    json_dump_atomic(MARKET_INTERNALS_PATH, cache)
    return cache


def load_index_cache() -> dict[str, Any]:
    try:
        return json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_index_cache(ctx: SourceContext) -> dict[str, Any]:
    """Nifty 500 daily closes for market regime and relative strength.
    Fetched at most once per day; a failed fetch keeps the old cache."""
    cache = load_index_cache()
    last = (cache.get("dates") or [None])[-1]
    recent = bool(last) and (now_ist().date() - date.fromisoformat(last)).days <= 4
    after_close = now_ist().weekday() < 5 and (now_ist().hour, now_ist().minute) >= (15, 45)
    todays_close_missing = after_close and last and last < now_ist().date().isoformat()
    tried = parse_datetime(cache.get("attemptedAt"))
    if (cache.get("fetchedOn") == now_ist().date().isoformat() and cache.get("closes") and recent
            and not (todays_close_missing and (tried is None or (now_ist() - tried).total_seconds() > 2 * 3600))):
        return cache
    cache["attemptedAt"] = iso_now()
    try:
        end = now_ist().date()
        # 2.5.4: NSE returns at most ~70 sessions per request, so a 330-day
        # request silently stopped in February. Fetch 80-day windows.
        found: dict[str, float] = {}
        with NSEAdapter(ctx) as nse:
            w_end = end
            while w_end > end - timedelta(days=330):
                w_start = max(w_end - timedelta(days=80), end - timedelta(days=330))
                rows, _ = nse.index_history("NIFTY 500", w_start, w_end)
                for r in rows or []:
                    d = parse_date(first(r, "EOD_TIMESTAMP", "HistoricalDate", "date"))
                    c = safe_num(first(r, "EOD_CLOSE_INDEX_VAL", "CLOSE", "close"))
                    if d and c:
                        found[d.isoformat()] = c
                w_end = w_start - timedelta(days=1)
        points = sorted(found.items())
        if points and (end - date.fromisoformat(points[-1][0])).days > 6:
            raise RuntimeError(f"index history ends {points[-1][0]}, not recent")
        if len(points) >= 55:
            cache = {"index": "NIFTY 500", "fetchedOn": end.isoformat(), "attemptedAt": iso_now(),
                     "dates": [p[0] for p in points], "closes": [p[1] for p in points]}
            json_dump_atomic(INDEX_CACHE_PATH, cache)
    except Exception as exc:
        print(f"Index history fetch failed (cache kept): {type(exc).__name__}: {exc}")
    return cache


def enrich_valuation(event: dict[str, Any], store: EventStore, master: SymbolMaster, ctx: SourceContext) -> None:
    # Avoid hammering Yahoo every hour if a recent success exists.
    fetch = (event.get("fetch") or {}).get("YAHOO_FUNDAMENTALS") or {}
    last_success = parse_datetime(fetch.get("lastSuccess"))
    if last_success and (now_ist() - last_success).total_seconds() < 12 * 3600:
        return
    data, ticker, err = YahooAdapter(ctx).fundamentals(event)
    if not data:
        if err:
            store.record_fetch(event, "YAHOO_FUNDAMENTALS", ok=False, error=err)
        return
    for field in ("market_cap_cr", "trailing_pe", "forward_pe", "peg", "roe_pct", "fcf_yield_pct", "last_price"):
        if data.get(field) is not None:
            store.merge_field(event, field, data[field], source="YAHOO_FUNDAMENTALS")
    sec = event.setdefault("security", {})
    if ticker:
        sec["yahooTicker"] = ticker
    for field in ("name", "sector", "industry"):
        if data.get(field) and not sec.get(field):
            sec[field] = data[field]
    master.merge(sec)
    store.record_fetch(event, "YAHOO_FUNDAMENTALS", ok=True)


def fetch_cooling_down(event: dict[str, Any], source: str, hours: float) -> bool:
    """True when the last attempt for `source` failed less than `hours` ago."""
    meta = (event.get("fetch") or {}).get(source) or {}
    last = parse_datetime(meta.get("lastAttempt"))
    return bool(meta.get("status") == "FAILED" and last and (now_ist() - last).total_seconds() < hours * 3600)


NSE_LISTING_REFRESH_HOURS = float(os.getenv("NSE_LISTING_REFRESH_HOURS", "3"))


def nse_result_listing(event: dict[str, Any], store: EventStore, ctx: SourceContext, *, force: bool = False) -> list[dict[str, Any]]:
    """This company's NSE result filings (period, basis, XBRL link, time),
    cached on the event as nseResultListing (engine 2.5.4). Refreshed every
    NSE_LISTING_REFRESH_HOURS until the event quarter's own filing appears."""
    sec = event.get("security") or {}
    symbol = normalize_symbol(sec.get("nseSymbol"))
    cache = event.get("nseResultListing") or {}
    rows = cache.get("rows") or []
    if not symbol:
        return rows
    period_end = (event.get("period") or {}).get("end")
    has_current = any(r.get("periodEnd") == period_end and r.get("xbrlUrl") for r in rows)
    fetched = parse_datetime(cache.get("fetchedAt"))
    max_age = 24 if has_current else NSE_LISTING_REFRESH_HOURS
    if cache.get("v") != 2:
        fetched = None          # 2.6.1: listings cached before Integrated Filing support
    if not force and fetched and (now_ist() - fetched).total_seconds() < max_age * 3600:
        return rows
    if fetch_cooling_down(event, "NSE_RESULT_LISTING", 1) or not _extra_budget_ok(1):
        return rows
    try:
        candidates_raw: list[tuple[dict[str, Any], str | None]] = []
        raw_ref = None
        with NSEAdapter(ctx) as nse:
            try:
                payload, raw_ref = nse.integrated_filings(symbol, event_id=event["eventId"])
                candidates_raw += [(normalize_nse_integrated(i), raw_ref) for i in payload or [] if isinstance(i, dict)]
                store.record_fetch(event, "NSE_INTEGRATED_FILING", ok=True, raw_ref=raw_ref)
            except Exception as exc:
                store.record_fetch(event, "NSE_INTEGRATED_FILING", ok=False, error=f"{type(exc).__name__}: {exc}")
            if not any(c for c, _ in candidates_raw):
                # Pre-2025 quarters and a fallback if the integrated index fails.
                payload, raw_ref = nse.financial_results(None, None, symbol=symbol, event_id=event["eventId"])
                candidates_raw += [(normalize_nse_filing(i), raw_ref) for i in payload or [] if isinstance(i, dict)]
        out = []
        for c, raw_ref in candidates_raw:
            if not c or normalize_symbol(c["security"].get("symbol")) != symbol or not c.get("periodEnd"):
                continue
            ts = c.get("filingTimestamp")
            out.append({"periodEnd": c["periodEnd"].isoformat(), "fromDate": c["fromDate"].isoformat() if c.get("fromDate") else None,
                        "basis": c.get("basis"), "cumulative": c.get("cumulative"), "xbrlUrl": c.get("xbrlUrl"),
                        "filedAt": ts.isoformat() if ts else None})
            if c["periodEnd"].isoformat() == period_end:
                record_filing(event, c, raw_ref)
                # NSE-only companies were never declared (the market-wide NSE
                # listing is broken); the company's own listing declares them.
                if (ts and ts.date() >= c["periodEnd"] and c.get("cumulative") is not True
                        and boolish(store.value(event, "results_released")) is not True):
                    src = c.get("source") or "NSE_FINANCIAL_RESULTS"
                    store.merge_field(event, "results_released", True, source=src, raw_ref=raw_ref)
                    store.merge_field(event, "filing_timestamp", ts.isoformat(), source=src, raw_ref=raw_ref)
                    store.merge_field(event, "result_date", ts.date().isoformat(), source=src, raw_ref=raw_ref)
        out.sort(key=lambda r: (r["periodEnd"], str(r.get("filedAt") or "")), reverse=True)
        event["nseResultListing"] = {"fetchedAt": iso_now(), "v": 2, "rows": out[:24]}
        store.record_fetch(event, "NSE_RESULT_LISTING", ok=True, raw_ref=raw_ref)
        return out[:24]
    except Exception as exc:
        store.record_fetch(event, "NSE_RESULT_LISTING", ok=False, error=f"{type(exc).__name__}: {exc}")
        return rows


def listing_row(rows: list[dict[str, Any]], period_end: date, basis: str | None = None) -> dict[str, Any] | None:
    """Quarterly (non-cumulative) XBRL row for a period; same basis when given."""
    want = period_end.isoformat()
    opts = [r for r in rows if r.get("periodEnd") == want and r.get("xbrlUrl") and r.get("cumulative") is not True
            and (not r.get("fromDate") or 80 <= (period_end - date.fromisoformat(r["fromDate"])).days <= 100)]
    if basis in {"CONSOLIDATED", "STANDALONE"}:
        opts = [r for r in opts if r.get("basis") == basis]
    if not opts:
        return None
    return max(opts, key=lambda r: (r.get("basis") == "CONSOLIDATED", str(r.get("filedAt") or "")))


def enrich_previous_quarter(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    """Exact filing time of the previous quarter's result (for Q1->Q2 buckets),
    from NSE's per-symbol filing list or BSE's per-scrip announcements."""
    if store.value(event, "prev_quarter_result_ts") or fetch_cooling_down(event, "PREV_QUARTER", 24):
        return
    meta = (event.get("fetch") or {}).get("PREV_QUARTER") or {}
    last_ok = parse_datetime(meta.get("lastSuccess"))
    if last_ok and (now_ist() - last_ok).total_seconds() < 24 * 3600:
        return
    period_end = parse_date((event.get("period") or {}).get("end"))
    if period_end is None:
        return
    prev_end = _shift_quarters(period_end, -1)
    if not _extra_budget_ok(1):
        return
    start = datetime.combine(prev_end + timedelta(days=1), dtime(0, 0))
    end = datetime.combine(min(now_ist().date(), prev_end + timedelta(days=80)), dtime(23, 59))
    sec = event.get("security") or {}
    symbol = normalize_symbol(sec.get("nseSymbol"))
    bse_code = str(sec.get("bseCode") or "").strip()
    found: list[tuple[datetime, str, str | None]] = []
    errors = []
    if symbol:
        for r in nse_result_listing(event, store, ctx):
            ts = parse_datetime(r.get("filedAt"))
            if r.get("periodEnd") == prev_end.isoformat() and ts:
                found.append((ts, "NSE_INTEGRATED_FILING", (event.get("fetch") or {}).get("NSE_RESULT_LISTING", {}).get("rawRef")))
        if not found and fetch_cooling_down(event, "NSE_RESULT_LISTING", 1):
            errors.append("NSE: result listing unavailable")
    if not found and bse_code:
        try:
            with BSEAdapter(ctx) as bse:
                rows, raw_ref = bse.scrip_results(bse_code, start, end, event["eventId"])
            for item in rows or []:
                c = normalize_bse_announcement(item) if isinstance(item, dict) else None
                if c and c.get("periodEnd") == prev_end and c.get("periodSource") == "FILING_TEXT" and c.get("filingTimestamp"):
                    found.append((c["filingTimestamp"], "BSE_RESULT_ANNOUNCEMENT", raw_ref))
        except Exception as exc:
            errors.append(f"BSE: {type(exc).__name__}: {exc}")
    if found:
        ts, source, raw_ref = min(found, key=lambda f: f[0])
        store.merge_field(event, "prev_quarter_result_ts", ts.isoformat(), source=source, raw_ref=raw_ref,
                          note=f"previous quarter {prev_end.isoformat()}")
    store.record_fetch(event, "PREV_QUARTER", ok=bool(found) or not errors,
                       error="; ".join(errors) or (None if found else "no previous-quarter filing found"))


def enrich_concall(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    """Concall notice / transcript / audio around the result (engine 2.5).
    Checked every 6 hours while a call may be pending, otherwise daily."""
    if boolish(store.value(event, "results_released")) is not True:
        return
    result_date = parse_date(store.value(event, "result_date"))
    today = now_ist().date()
    if result_date is None or (today - result_date).days > 30:
        return
    current = event.get("concall") or {}
    if current.get("status") == "DONE" and current.get("transcriptUrl"):
        return
    meta = (event.get("fetch") or {}).get("CONCALL") or {}
    last = parse_datetime(meta.get("lastAttempt"))
    # Re-check every 6 h until a transcript/recording proves the call happened
    # (2.5.3: an inferred "held" from an undated notice is re-checked too).
    proven = current.get("transcriptUrl") or current.get("audioUrl")
    gap = 24 if proven else 6
    if last and (now_ist() - last).total_seconds() < gap * 3600:
        return
    sec = event.get("security") or {}
    symbol = normalize_symbol(sec.get("nseSymbol"))
    bse_code = str(sec.get("bseCode") or "").strip()
    if not (symbol or bse_code) or not _extra_budget_ok(1):
        return
    start = datetime.combine(result_date - timedelta(days=20), dtime(0, 0))
    end = datetime.combine(today, dtime(23, 59))
    filings, errors, raw_ref = [], [], None
    try:
        if symbol:
            with NSEAdapter(ctx) as nse:
                rows, raw_ref = nse.announcements(symbol, start, end, event["eventId"])
            for r in rows or []:
                if isinstance(r, dict):
                    filings.append({"filedAt": parse_datetime(first(r, "an_dt", "sort_date", "dt")),
                                    "text": f"{r.get('desc') or ''} {r.get('attchmntText') or ''}",
                                    "url": r.get("attchmntFile")})
        else:
            with BSEAdapter(ctx) as bse:
                rows, raw_ref = bse.scrip_announcements(bse_code, start, end, event["eventId"])
            for r in rows or []:
                if isinstance(r, dict):
                    att = r.get("ATTACHMENTNAME")
                    filings.append({"filedAt": parse_datetime(first(r, "NEWS_DT", "DT_TM")),
                                    "text": f"{r.get('SUBCATNAME') or ''} {r.get('HEADLINE') or ''} {r.get('NEWSSUB') or ''}",
                                    "url": f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}" if att else None})
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    if errors:
        store.record_fetch(event, "CONCALL", ok=False, error="; ".join(errors))
        return
    status = pead_plus.concall_status([f for f in filings if f["filedAt"]], result_date, today)
    status["source"] = "NSE_ANNOUNCEMENTS" if symbol else "BSE_ANNOUNCEMENTS"
    event["concall"] = status
    store.record_fetch(event, "CONCALL", ok=True, raw_ref=raw_ref)


def previous_quarter_event(store: EventStore, event: dict[str, Any]) -> dict[str, Any] | None:
    period_end = parse_date((event.get("period") or {}).get("end"))
    if period_end is None:
        return None
    prev_end = _shift_quarters(period_end, -1)
    sec = event.get("security") or {}
    keys = []
    if sec.get("isin"):
        keys.append(security_key_from_values(sec.get("isin"), None, None, None))
    if sec.get("nseSymbol"):
        keys.append(security_key_from_values(None, sec.get("nseSymbol"), None, None))
    if sec.get("bseCode"):
        keys.append(security_key_from_values(None, None, sec.get("bseCode"), None))
    if sec.get("symbol"):
        keys.append(security_key_from_values(None, None, None, sec.get("symbol")))
    for key in keys:
        prev = store.load(event_id_for(key, prev_end))
        if prev:
            return prev
    return None


def previous_quarter_window_start(store: EventStore, event: dict[str, Any]) -> date | None:
    """First session of the previous quarter's reaction window when that result
    was filed during market hours (2.9.4: GMBREW's Q1, filed 09 Jul 12:04, was
    replayed from 09 Jul's close instead of 08 Jul's)."""
    prev = previous_quarter_event(store, event)
    if prev and boolish(store.value(prev, "results_released")) is True:
        ws = parse_date(store.value(prev, "reaction_window_start"))
        if ws:
            return ws
        ts = parse_datetime(store.value(prev, "filing_timestamp"))
    else:
        ts = parse_datetime(store.value(event, "prev_quarter_result_ts"))
    if ts:
        session, timing = reaction_session(ts, None)
        ws = reaction_window_start(ts, timing, session) if session else None
        return ws if ws and ws != session else None
    return None


def previous_quarter_reaction(store: EventStore, event: dict[str, Any]) -> date | None:
    prev = previous_quarter_event(store, event)
    if prev and boolish(store.value(prev, "results_released")) is True:
        d = parse_date(store.value(prev, "reaction_session")) or parse_date(store.value(prev, "result_date"))
        if d:
            return d
    ts = parse_datetime(store.value(event, "prev_quarter_result_ts"))
    if ts:
        session, _ = reaction_session(ts, None)
        return session
    return None


def enrich_price(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    # Entry/reaction tracking needs fresher prices than valuation/fundamental
    # enrichment. Recently released results are refreshed at most once per hour;
    # upcoming events remain on the lighter 4-hour cadence.
    sec = event.get("security", {})
    filing_ts = parse_datetime(store.value(event, "filing_timestamp"))
    result_date = parse_date(store.value(event, "result_date"))
    released = boolish(store.value(event, "results_released")) is True

    pf = (event.get("fetch") or {}).get("PRICE_HISTORY") or {}
    last_success = parse_datetime(pf.get("lastSuccess"))
    if last_success:
        age_hours = (now_ist() - last_success).total_seconds() / 3600
        recent_released = False
        if released and result_date is not None:
            result_age = (now_ist().date() - result_date).days
            recent_released = 0 <= result_age <= DASHBOARD_RESULT_DAYS
        threshold = RECENT_RESULT_PRICE_REFRESH_HOURS if recent_released else 4
        if age_hours < threshold:
            return
    reaction_date, timing = reaction_session(filing_ts, result_date)
    if reaction_date:
        store.merge_field(event, "reaction_session", reaction_date.isoformat(), source="DERIVED", note=timing)
        store.merge_field(event, "filing_session", timing, source="DERIVED")

    # Upcoming events still benefit from pre-result run-up calculations; use the
    # announced result date as the expected reaction boundary.
    price_boundary = reaction_date or result_date
    window_start = reaction_window_start(filing_ts, timing, reaction_date)
    if window_start and window_start != reaction_date:
        store.merge_field(event, "reaction_window_start", window_start.isoformat(), source="DERIVED", note="intraday filing")
    frame = None
    price_source = None
    raw_ref = None
    errors = []

    # Only a confirmed NSE symbol: a BSE scrip id can be another company's
    # NSE symbol (2.5.4).
    symbol = normalize_symbol(sec.get("nseSymbol"))
    if symbol:
        try:
            start = (price_boundary or now_ist().date()) - timedelta(days=420)
            end = now_ist().date()
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.history(symbol, start, end, event["eventId"])
                if getattr(nse, "last_series", None) == "EQ":
                    sec.pop("nseSeries", None)
                elif getattr(nse, "last_series", None):
                    sec["nseSeries"] = nse.last_series
                frame = nse_history_to_frame(payload)
                if frame is not None and not frame.empty:
                    price_source = "NSE_PRICE"
        except Exception as exc:
            errors.append(f"NSE: {type(exc).__name__}: {exc}")

    bse_code = str(sec.get("bseCode") or "").strip()
    if (frame is None or getattr(frame, "empty", True)) and bse_code:
        try:
            with BSEAdapter(ctx) as bse:
                payload, raw_ref = bse.price_history(bse_code, event["eventId"], symbol)
                frame = bse_history_to_frame(payload)
                if frame is not None and not frame.empty:
                    price_source = "BSE_PRICE"
        except Exception as exc:
            errors.append(f"BSE: {type(exc).__name__}: {exc}")

    if frame is None or getattr(frame, "empty", True):
        yf_frame, ticker, err = YahooAdapter(ctx).history(event)
        frame = normalize_yahoo_frame(yf_frame)
        if frame is not None and not frame.empty:
            price_source = "YAHOO_PRICE"
            if ticker:
                sec["yahooTicker"] = ticker
        elif err:
            errors.append(err)

    if frame is None or getattr(frame, "empty", True):
        store.record_fetch(event, "PRICE_HISTORY", ok=False, error="; ".join(errors) or "no price history")
        return

    metrics = price_metrics(frame, price_boundary, window_start)
    drop_stale_pre_result(store, event, metrics)
    reaction_traded = released and session_closed(reaction_date)
    for field, value in metrics.items():
        if value is None:
            continue
        if field in POST_RESULT_FIELDS and not reaction_traded:
            # Scheduled-only or future sessions must never carry reaction data.
            continue
        store.merge_field(event, field, value, source=price_source or "YAHOO_PRICE", raw_ref=raw_ref)
    append_price_snapshot(event, metrics.get("last_price"), price_source)
    try:
        x = pead_plus.extended_features(
            frame, reaction_date, released=reaction_traded,
            q1_reaction_date=previous_quarter_reaction(store, event), q2_boundary=window_start or price_boundary,
            q1_window_start=previous_quarter_window_start(store, event),
            window_start=window_start,
        )
        if x:
            event["plus"] = {
                "price": {k: v for k, v in x.items() if k != "chart"},
                "chart": x.get("chart"),
                "priceSource": price_source,
                "computedAt": iso_now(),
                "plusVersion": PLUS_VERSION,
                "corporateActions": frame.attrs.get("corporateActions"),
            }
    except Exception as exc:
        store.record_fetch(event, "PLUS_FEATURES", ok=False, error=f"{type(exc).__name__}: {exc}")
    store.record_fetch(event, "PRICE_HISTORY", ok=True, raw_ref=raw_ref)

    if boolish(store.value(event, "results_released")) is True:
        if reaction_date and now_ist().date() < reaction_date:
            store.set_state(event, "REACTION_PENDING", "reaction trading session has not occurred yet")
        elif store.value(event, "result_day_return_pct") is not None or store.value(event, "result_day_rvol") is not None:
            store.set_state(event, "REACTION_READY", "post-result price/volume reaction available")


def enrichment_activity(event: dict[str, Any], today: date | None = None) -> tuple[bool, str]:
    """Select only the live fiscal quarter for hourly network enrichment.

    Older quarters remain permanently stored for quarter memory/backtests, but
    they do not compete with the current result season for NSE/BSE/Yahoo calls.
    """
    today = today or now_ist().date()
    result_date = parse_date(meta_value(event, "result_date"))
    period_end = parse_date(event.get("period", {}).get("end"))

    target = live_reporting_period(today)
    if target is not None:
        if period_end is not None and period_end != target:
            if period_end < target:
                return False, "ARCHIVE_PREVIOUS_QUARTER"
            return False, "DEFER_FUTURE_QUARTER"

        if period_end is None and result_date is not None:
            inferred = expected_period_end(result_date)
            if inferred is not None and inferred != target:
                if inferred < target:
                    return False, "ARCHIVE_PREVIOUS_QUARTER"
                return False, "DEFER_FUTURE_QUARTER"

    if result_date is not None and (result_date - today).days > UPCOMING_DAYS:
        return False, "DEFER_FAR_UPCOMING"

    if result_date is not None:
        age = (today - result_date).days
        if -UPCOMING_DAYS <= age <= ACTIVE_ENRICH_DAYS:
            return True, "ACTIVE_LIVE_QUARTER_RESULT"
        if age > ACTIVE_ENRICH_DAYS:
            return False, "ARCHIVE_OLD_RESULT"

    if period_end is not None and (target is None or period_end == target):
        return True, "ACTIVE_LIVE_QUARTER_PERIOD"

    # Undated legacy rows stay stored but do not consume hourly network calls.
    return False, "DEFER_NO_PERIOD"




def dashboard_activity(event: dict[str, Any], today: date | None = None) -> bool:
    """Current-quarter live dashboard only.

    Q1/Q3/etc. remain in the permanent event store and quarter-memory history,
    but the main live page shows only the active reporting period. This prevents
    the current Q2 FY27 radar from being flooded with Q1 FY27 results.
    """
    today = today or now_ist().date()
    target = live_reporting_period(today)
    period_end = parse_date(event.get("period", {}).get("end"))
    result_date = parse_date(meta_value(event, "result_date"))

    if target is not None:
        if period_end is not None:
            if period_end != target:
                return False
        elif result_date is not None:
            if expected_period_end(result_date) != target:
                return False
        else:
            return False

    if result_date is not None:
        delta = (result_date - today).days
        return -DASHBOARD_RESULT_DAYS <= delta <= DASHBOARD_UPCOMING_DAYS

    # If the result date is not known yet, a correctly identified live-quarter
    # event is still useful as an upcoming/current watch row.
    return period_end == target if target is not None else period_end is not None




def should_enrich(event: dict[str, Any]) -> bool:
    return enrichment_activity(event)[0]


def enrich_events(store: EventStore, master: SymbolMaster, ctx: SourceContext) -> dict[str, Any]:
    all_events = store.all()
    classified = [(event, enrichment_activity(event)) for event in all_events]
    candidates = [event for event, (active, _) in classified if active]
    candidates.sort(key=_enrichment_priority)

    # Hard cap prevents an unexpectedly noisy exchange response from turning an
    # hourly run into a 60-minute historical backfill. Highest-priority released
    # and near-term events win; deferred events remain safely persisted.
    selected = candidates[:MAX_ACTIVE_ENRICH_EVENTS]
    deferred_by_cap = max(0, len(candidates) - len(selected))
    reason_counts: dict[str, int] = {}
    for _, (_, reason) in classified:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    stats = {
        "events": 0, "filed": 0, "financials": 0, "reactionReady": 0,
        "deferredUpcoming": 0, "errors": [],
        "eventsTracked": len(all_events),
        "activeCandidates": len(candidates),
        "selectedForEnrichment": len(selected),
        "deferredByCap": deferred_by_cap,
        "activityReasons": reason_counts,
        "yahooBudget": _YAHOO_BUDGET_USED,
    }
    active = selected
    total = len(active)
    print(
        f"Enrichment plan: tracked={len(all_events)} active={len(candidates)} "
        f"selected={len(active)} deferred_by_cap={deferred_by_cap} "
        f"archived_old_period={reason_counts.get('ARCHIVE_OLD_PERIOD', 0)}",
        flush=True,
    )

    for idx, event in enumerate(active, 1):
        if RUN_GUARD.elapsed() >= RUN_BUDGET_SEC:
            # Highest-priority events went first; the rest wait for the next run.
            RUN_GUARD.budget_hit = True
            stats["deferredByBudget"] = total - idx + 1
            print(f"[run guard] network budget spent; {total - idx + 1} events wait for the next run", flush=True)
            break
        stats["events"] += 1
        original_event = event
        original_event_id = str(event.get("eventId") or "unknown-event")
        try:
            if idx == 1 or idx % 10 == 0 or idx == total:
                print(f"Enrichment progress {idx}/{total}: {original_event_id}", flush=True)

            identified = fill_security_identity(event, store, master, ctx)
            if not isinstance(identified, dict):
                raise RuntimeError(
                    f"fill_security_identity returned {type(identified).__name__}; "
                    "expected event dictionary"
                )
            event = identified

            released = boolish(store.value(event, "results_released")) is True
            if released:
                stats["filed"] += 1

            # Far-away scheduled events are stored permanently now, but heavy
            # price/Yahoo enrichment waits until they are within the actionable
            # PEAD window. This prevents hundreds of unnecessary API calls.
            if not _heavy_enrichment_due(event, store):
                stats["deferredUpcoming"] += 1
                store.save(event)
                continue

            # Core/authoritative work comes first. Valuation is deliberately
            # last because Yahoo info is optional and historically the slowest
            # fallback.
            if released:
                enrich_financials(event, store, ctx)
                if store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
                    stats["financials"] += 1

            enrich_price(event, store, ctx)
            try:
                enrich_exchange_meta(event, store, ctx)
            except Exception as exc:
                store.record_fetch(event, "EXCHANGE_META", ok=False, error=f"{type(exc).__name__}: {exc}")
            try:
                enrich_concall(event, store, ctx)
            except Exception as exc:
                store.record_fetch(event, "CONCALL", ok=False, error=f"{type(exc).__name__}: {exc}")
            try:
                had_prev = store.value(event, "prev_quarter_result_ts")
                enrich_previous_quarter(event, store, ctx)
                if not had_prev and store.value(event, "prev_quarter_result_ts"):
                    # New Q1 date: rebuild the Q1 replay on the next price pass.
                    (event.get("fetch") or {}).get("PRICE_HISTORY", {}).pop("lastSuccess", None)
            except Exception as exc:
                store.record_fetch(event, "PREV_QUARTER", ok=False, error=f"{type(exc).__name__}: {exc}")

            if _valuation_due(event, store):
                enrich_valuation(event, store, master, ctx)

            if STATE_ORDER.get(event.get("state", "DISCOVERED"), 0) >= STATE_ORDER["REACTION_READY"]:
                stats["reactionReady"] += 1
            store.save(event)
        except Exception as exc:
            safe_event = event if isinstance(event, dict) else original_event
            event_id = (
                str(safe_event.get("eventId"))
                if isinstance(safe_event, dict) and safe_event.get("eventId")
                else original_event_id
            )
            stats["errors"].append(f"{event_id}: {type(exc).__name__}: {exc}")
            if isinstance(safe_event, dict):
                try:
                    store.record_fetch(
                        safe_event,
                        "ENGINE",
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    store.save(safe_event)
                except Exception as log_exc:
                    stats["errors"].append(
                        f"{event_id}: error-handler failure: "
                        f"{type(log_exc).__name__}: {log_exc}"
                    )

    master.save()
    _save_yahoo_negative_cache()
    stats["yahooNegativeCached"] = len(_load_yahoo_negative_cache())
    stats["yahooBudget"] = dict(_YAHOO_BUDGET_USED)
    stats["extraCallsUsed"] = _EXTRA_CALLS_USED["n"]
    return stats


# ---------------------------------------------------------------------------
# PEAD scoring and state machine
# ---------------------------------------------------------------------------


def field_source(event: dict[str, Any], field: str) -> str | None:
    meta = event.get("fields", {}).get(field)
    return meta.get("source") if isinstance(meta, dict) else None


def core_completeness(store: EventStore, event: dict[str, Any]) -> tuple[int, int, float]:
    released = boolish(store.value(event, "results_released")) is True
    required = ["market_cap_cr", "pre_result_20d_pct"]
    if released:
        required += ["revenue_cr", "pat_cr", "revenue_yoy_pct", "pat_trend", "result_day_return_pct", "result_day_rvol"]
    else:
        required += ["result_date"]
    have = 0
    for field in required:
        value = store.value(event, field)
        if field == "pat_trend":
            okay = value not in (None, "") or store.value(event, "pat_yoy_pct") is not None
        else:
            okay = value is not None and value != ""
        have += int(okay)
    return have, len(required), round(have / len(required) * 100, 1) if required else 100.0


def screener_links(sec: dict[str, Any]) -> dict[str, str | None]:
    nse = normalize_symbol(sec.get("nseSymbol"))
    bse = str(sec.get("bseCode") or "").strip()
    bse_sym = normalize_symbol(sec.get("bseSymbol") or sec.get("symbol"))
    return {
        "screener": f"https://www.screener.in/company/{nse or bse}/" if (nse or bse) else None,
        "tradingview": (f"https://www.tradingview.com/chart/?symbol=NSE%3A{nse}" if nse
                        else f"https://www.tradingview.com/chart/?symbol=BSE%3A{bse_sym}" if bse_sym else None),
        "nse": f"https://www.nseindia.com/get-quotes/equity?symbol={nse}" if nse else None,
        "bse": f"https://www.bseindia.com/stock-share-price/x/{bse_sym or 'x'}/{bse}/" if bse else None,
        # 2.9.5: TradingView's free embed shows BSE (end-of-day) but not NSE
        # ("This symbol is only available on TradingView"); it needs BSE's own
        # ticker (numeric codes don't resolve), so only a stored BSE symbol.
        "tvEmbed": (f"BSE:{normalize_symbol(sec.get('bseSymbol'))}"
                    if bse and normalize_symbol(sec.get("bseSymbol")) else None),
    }


def build_plus(store: EventStore, event: dict[str, Any], *, released: bool, result_label: str, price_label: str,
               sector_info: dict[str, Any] | None, box_high: Any, last_price: Any, result_ret: Any, rvol: Any,
               result_low: Any, result_high: Any) -> dict[str, Any]:
    """Engine 2.4 analytics for one event (buckets, plan, margins, sector, valuation)."""
    px = (event.get("plus") or {}).get("price") or {}
    sec = event.get("security") or {}
    rev_yoy = store.value(event, "revenue_yoy_pct")
    pat_yoy = store.value(event, "pat_yoy_pct")
    margin = store.value(event, "margin_change_bps")
    strength = pead_plus.earnings_strength(rev_yoy, pat_yoy, store.value(event, "pat_trend"), margin) if released else None

    prev = previous_quarter_event(store, event)
    q1_strength = None
    if prev is not None and boolish(store.value(prev, "results_released")) is True:
        q1_strength = pead_plus.earnings_strength(store.value(prev, "revenue_yoy_pct"), store.value(prev, "pat_yoy_pct"),
                                                  store.value(prev, "pat_trend"), store.value(prev, "margin_change_bps"))
    if q1_strength is None:
        # 2.9.2: no archived Q1 event (or one without YoY): use last quarter's
        # own YoY read from the exchange filings (2.7.0 prev_q_* fields).
        q1_strength = pead_plus.earnings_strength(store.value(event, "prev_q_revenue_yoy_pct"),
                                                  store.value(event, "prev_q_pat_yoy_pct"), None)
    setup = pead_plus.q1_setup(px.get("q1_reaction_return_pct"), px.get("q1_reaction_rvol"), q1_strength)
    bucket = pead_plus.classify_bucket(released=released, q2_strength=strength, setup=setup, sustained=px.get("q1_sustained"),
                                       q1_strength=q1_strength)

    reaction = parse_date(store.value(event, "reaction_session"))
    reaction_traded = released and session_closed(reaction)
    if price_label == "NEGATIVE" or result_label == "LOW QUALITY" or strength in {"WEAK", "AVERAGE"}:
        quality_ok: bool | None = False
    elif strength in {"STRONG", "AVERAGE+"}:
        quality_ok = True
    else:
        quality_ok = None
    # 2.5.3: a stored "held" verdict from an undated notice (pre-2.5.3 rule)
    # is corrected without waiting for the next announcements fetch.
    if event.get("concall"):
        event["concall"] = pead_plus.recheck_concall(event["concall"], parse_date(store.value(event, "result_date")),
                                                     now_ist().date())
    plan = pead_plus.trade_plan(released=released, reaction_traded=reaction_traded, quality_ok=quality_ok, x=px,
                                last_price=last_price, result_return=result_ret, rvol=rvol,
                                result_low=result_low, result_high=result_high, box_high=box_high,
                                concall=event.get("concall"),
                                red_flags=pead_plus.result_red_flags(
                                    rev_yoy=rev_yoy, pat_yoy=pat_yoy, pat_trend=store.value(event, "pat_trend"),
                                    margin_change_bps=margin,
                                    financial_status=(event.get("financialIntegrity") or {}).get("status")))
    ws = parse_date(store.value(event, "reaction_window_start"))
    if plan.get("signal") == "NO_ENTRY" and quality_ok is False and not plan.get("notTrading"):
        # 2.8.0: say exactly why (was one generic sentence for every stock).
        bits = []
        if price_label == "NEGATIVE":
            bits.append(f"the price moved {safe_num(result_ret):+.1f}% on the result" if safe_num(result_ret) is not None
                        else "the price response was negative")
        if strength in {"WEAK", "AVERAGE"}:
            fmt = lambda v: f"{safe_num(v):+.1f}%" if safe_num(v) is not None else "—"
            bits.append(f"earnings {strength.lower()} (revenue {fmt(rev_yoy)}, "
                        f"profit {fmt(pat_yoy)} YoY; strong needs revenue "
                        f">={pead_plus.STRONG_REV_YOY:.0f}% and profit >={pead_plus.STRONG_PAT_YOY:.0f}%)")
        if result_label == "LOW QUALITY":
            bits.append("result quality flagged")
        if bits:
            why = "; ".join(bits)
            plan["why"] = why[0].upper() + why[1:] + "."
    if plan.get("signal") == "WAIT_REACTION" and ws and reaction and ws < reaction:
        plan["why"] = (f"Result came during market hours, so the reaction is measured over {ws.strftime('%d %b')} and "
                       f"{reaction.strftime('%d %b')}. The plan is ready after the {reaction.strftime('%d %b')} close.")
    last_sess = parse_date(px.get("last_session"))
    if last_sess and (now_ist().date() - last_sess).days > 20:
        # 2.6.2: suspended / untraded scrips (TIAANC, GOLKONDA, ALSTONE last
        # traded Apr-May 2026) can never show a reaction; say so plainly.
        plan = {"signal": "NO_ENTRY", "stage": None, "why": f"No trades since {last_sess.strftime('%d %b %Y')}; "
                "the stock is not trading, so there is no reaction to measure.", "notTrading": True}
    event["tradeLog"] = pead_plus.update_trade_log(event.get("tradeLog"), plan, last_price, now_ist().date())
    key = sector_key(sec)
    # 2.7.0 earnings acceleration: this quarter's YoY vs last quarter's YoY.
    prev_rev_yoy = store.value(event, "prev_q_revenue_yoy_pct")
    prev_pat_yoy = store.value(event, "prev_q_pat_yoy_pct")
    if prev is not None and prev_rev_yoy is None and prev_pat_yoy is None:
        prev_rev_yoy, prev_pat_yoy = store.value(prev, "revenue_yoy_pct"), store.value(prev, "pat_yoy_pct")
    growth = pead_plus.earnings_acceleration(rev_yoy, pat_yoy, prev_rev_yoy, prev_pat_yoy,
                                             store.value(event, "pat_trend")) if released else None
    # 2.7.0 relative strength vs NIFTY 500 since the result.
    idx = _INDEX_CACHE_MEMO.get("cache")
    if idx is None:
        idx = _INDEX_CACHE_MEMO["cache"] = load_index_cache()
    idx_last = (idx.get("dates") or [None])[-1]
    rs = (pead_plus.relative_strength(px.get("return_since_result_pct"), px.get("pre_result_session"),
                                      px.get("last_session"), idx.get("dates"), idx.get("closes"))
          if px.get("return_since_result_pct") is not None and idx_last and px.get("last_session")
          and idx_last >= px["last_session"] else None)   # 2.8.0: no stale-index "index 0.0%"
    lv = event.get("live") or {}
    lv_at = parse_datetime(lv.get("at"))
    live = pead_plus.live_entry_status(plan, lv) if lv_at and lv_at.astimezone(IST).date() == now_ist().date() else None
    return {
        "growth": growth,
        "relativeStrength": rs,
        "live": live,
        "strength": strength,
        "q1Strength": q1_strength,
        "q1Setup": setup,
        "bucket": bucket,
        "plan": plan,
        "liquidity": pead_plus.liquidity(store.value(event, "avg_turnover_20d_cr"), last_price),
        "valuation": pead_plus.valuation_view(store.value(event, "trailing_pe"), store.value(event, "sector_pe"),
                                              store.value(event, "roe_pct"), pat_yoy, store.value(event, "pb")),
        "margins": {
            "opm": store.value(event, "opm_pct"), "opmPrevQ": store.value(event, "opm_prev_q_pct"),
            "opmPriorYear": store.value(event, "opm_prior_year_pct"), "changeBps": margin,
            "opmTtm": nonzero(store.value(event, "exchange_opm_ttm_pct")),
            # 2.5.3: margin change falls back to last quarter when the filing
            # has no prior-year column; say which one it is.
            "basis": ("YoY" if store.value(event, "opm_prior_year_pct") is not None
                      else "QoQ" if margin is not None else None),
            "revenueQoQ": store.value(event, "revenue_qoq_pct"),
            "patQoQ": store.value(event, "pat_qoq_pct"),
            "patQoQStatus": store.value(event, "pat_qoq_trend"),
        },
        "sectorKey": key,
        "sector": (sector_info or {}).get(key) if key else None,
        "identity": {k: sec.get(k) for k in ("macroSector", "exchangeSector", "industry", "basicIndustry", "sectorIndex", "bseGroup")},
        "price": px,
        "deliveryPct": store.value(event, "delivery_pct"),
        "concall": event.get("concall"),
        "tradeLog": event.get("tradeLog") or None,
        "links": screener_links(sec),
    }


def score_event(store: EventStore, event: dict[str, Any], sector_info: dict[str, Any] | None = None) -> dict[str, Any]:
    released = boolish(store.value(event, "results_released")) is True
    rev_yoy = safe_num(store.value(event, "revenue_yoy_pct"))
    pat_yoy = safe_num(store.value(event, "pat_yoy_pct"))
    trend = str(store.value(event, "pat_trend") or "").upper() or None
    pre5 = safe_num(store.value(event, "pre_result_5d_pct"))
    pre10 = safe_num(store.value(event, "pre_result_10d_pct"))
    pre20 = safe_num(store.value(event, "pre_result_20d_pct"))
    result_ret = safe_num(store.value(event, "result_day_return_pct"))
    rvol = safe_num(store.value(event, "result_day_rvol"))
    hold5 = boolish(store.value(event, "post_result_hold_5d"))
    hold10 = boolish(store.value(event, "post_result_hold_10d"))
    breakout = boolish(store.value(event, "box_breakout"))
    box_high = safe_num(store.value(event, "box_high"))
    result_low = safe_num(store.value(event, "result_day_low"))
    last_price = safe_num(store.value(event, "last_price"))
    mcap = safe_num(store.value(event, "market_cap_cr"))
    turnover = safe_num(store.value(event, "avg_turnover_20d_cr"))
    trailing_pe = safe_num(store.value(event, "trailing_pe"))
    forward_pe = safe_num(store.value(event, "forward_pe"))
    peg = safe_num(store.value(event, "peg"))
    roe = nonzero(store.value(event, "roe_pct"))   # 2.9.4: BSE 0.00 = missing
    fcf_yield = safe_num(store.value(event, "fcf_yield_pct"))

    reasons: list[str] = []
    risks: list[str] = []
    score = 0

    # Earnings event strength (max ~35)
    if released:
        if rev_yoy is not None:
            if rev_yoy >= 20:
                score += 12
                reasons.append(f"Revenue YoY +{rev_yoy:.1f}%.")
            elif rev_yoy >= REV_YOY_STRONG:
                score += 8
                reasons.append(f"Revenue YoY +{rev_yoy:.1f}%.")
            elif rev_yoy < 0:
                score -= 5
                risks.append(f"Revenue YoY {rev_yoy:.1f}%.")
        if trend == "TURNAROUND":
            score += 18
            reasons.append("PAT turned profitable versus a loss/zero base.")
        elif trend == "PROFIT_GROWTH" and pat_yoy is not None:
            if pat_yoy >= 30:
                score += 18
            elif pat_yoy >= PAT_YOY_STRONG:
                score += 13
            elif pat_yoy >= 0:
                score += 5
            reasons.append(f"PAT YoY +{pat_yoy:.1f}%.")
        elif trend == "LOSS_NARROWING":
            score += 5
            reasons.append("Loss narrowed YoY.")
        elif trend in {"DETERIORATION", "LOSS_WIDENING", "PROFIT_DECLINE"}:
            score -= 10
            risks.append(f"PAT trend: {trend.replace('_', ' ').title()}.")

    # Expectations (max ~15). Missing 20D history is not treated as a zero
    # return. Shorter verified windows may still provide a partial label.
    expectation_has_full_window = pre20 is not None
    if pre20 is not None:
        if pre20 <= LOW_EXPECTATION_RUNUP_PCT:
            score += 15
            reasons.append(f"Low pre-result expectation: 20D move {pre20:+.1f}%.")
            expectation_label = "LOW EXPECTATIONS"
        elif pre20 < PRICED_IN_RUNUP_PCT:
            score += 8
            expectation_label = "PARTLY PRICED"
        else:
            expectation_label = "PRICED IN"
            risks.append(f"20D pre-result run-up {pre20:+.1f}% suggests elevated expectations.")
    elif pre10 is not None or pre5 is not None:
        short_values = [x for x in (pre10, pre5) if x is not None]
        strongest_short = max(short_values) if short_values else None
        if (pre10 is not None and pre10 >= 10) or (pre5 is not None and pre5 >= 7.5):
            expectation_label = "ELEVATED SHORT-TERM"
            if strongest_short is not None:
                risks.append(f"Short-term pre-result run-up reached {strongest_short:+.1f}%; 20D history is still pending.")
        elif short_values and max(short_values) <= LOW_EXPECTATION_RUNUP_PCT:
            score += 5
            expectation_label = "LOW EXPECTATIONS (PARTIAL)"
            reasons.append("Available short-term pre-result history shows limited run-up; 20D history is pending.")
        else:
            expectation_label = "PARTIAL PRICE HISTORY"
    else:
        expectation_label = "AWAITING PRICE HISTORY"

    # Reaction / acceptance (max ~30)
    if result_ret is not None:
        if result_ret >= 5:
            score += 12
            reasons.append(f"Strong reaction: result-session return {result_ret:+.1f}%.")
        elif result_ret >= RESULT_RETURN_CONFIRM:
            score += 8
            reasons.append(f"Positive result-session return {result_ret:+.1f}%.")
        elif result_ret < -3:
            score -= 8
            risks.append(f"Negative result-session return {result_ret:+.1f}%.")
    if rvol is not None:
        if rvol >= 2:
            score += 10
            reasons.append(f"High confirmation volume: {rvol:.2f}x.")
        elif rvol >= RVOL_CONFIRM:
            score += 6
            reasons.append(f"Volume confirmation: {rvol:.2f}x.")
        else:
            risks.append(f"Result-session RVOL only {rvol:.2f}x.")
    if hold5 is True:
        score += 5
        reasons.append("Price held above the result-session low for 5 sessions.")
    if hold10 is True:
        score += 3
        reasons.append("Price held above the result-session low for 10 sessions.")
    if breakout is True:
        score += 8
        reasons.append("Post-result Darvas/box breakout detected.")

    # Tradability / quality modifiers (max ~20)
    if mcap is not None:
        if mcap >= MIN_MCAP_CR:
            score += 8
        else:
            score -= 20
            risks.append(f"Market cap ₹{mcap:,.0f} Cr is below ₹{MIN_MCAP_CR:,.0f} Cr rule.")
    if turnover is not None:
        if turnover >= LIQUIDITY_TURNOVER_CR_MIN:
            score += 5
        elif turnover >= pead_plus.LIQ_TURNOVER_CR:
            pass   # tradeable, just not deep: no bonus, no warning
        else:
            score -= 4
            shown = (f"₹{turnover:.1f} Cr" if turnover >= 0.1
                     else f"₹{turnover * 100:.1f} lakh" if turnover >= 0.01 else "under ₹1 lakh")
            risks.append(f"20D average turnover {shown}/day is thin.")
    if roe is not None and roe >= 15:
        score += 3
    if fcf_yield is not None and fcf_yield > 0:
        score += 2
    if trailing_pe is not None and trailing_pe > 80:
        score -= 3
        risks.append(f"Trailing P/E {trailing_pe:.1f}x is elevated.")

    score = max(0, min(100, int(round(score))))
    have, required, completeness = core_completeness(store, event)

    # Result reality deliberately refuses to label missing data as bad.
    if not released:
        result_label = "AWAITING RESULT"
    else:
        earnings_evidence = int(rev_yoy is not None) + int(trend is not None or pat_yoy is not None)
        bad_trend = trend in {"DETERIORATION", "LOSS_WIDENING"}
        if earnings_evidence < 2:
            result_label = "UNVERIFIED"
        elif bad_trend or (rev_yoy is not None and rev_yoy < 0 and trend == "PROFIT_DECLINE"):
            result_label = "LOW QUALITY"
        elif (rev_yoy is not None and rev_yoy >= REV_YOY_STRONG) and (
            trend == "TURNAROUND" or (trend == "PROFIT_GROWTH" and (pat_yoy or 0) >= PAT_YOY_STRONG)
        ):
            result_label = "GENUINE"
        else:
            result_label = "MIXED"

    if result_ret is None and rvol is None:
        price_label = "UNVERIFIED" if released else "AWAITING RESULT"
    elif (result_ret or 0) >= RESULT_RETURN_CONFIRM and (rvol or 0) >= RVOL_CONFIRM:
        price_label = "CONFIRMED"
    elif result_ret is not None and result_ret < -3:
        price_label = "NEGATIVE"
    else:
        price_label = "MIXED"

    valuation_inputs = [trailing_pe, forward_pe, peg, roe, fcf_yield]
    valuation_input_count = sum(v is not None for v in valuation_inputs)
    exchange_view = pead_plus.valuation_view(trailing_pe, store.value(event, "sector_pe"), roe, pat_yoy, store.value(event, "pb"))
    if valuation_input_count < 3 and exchange_view["label"] not in {"UNVERIFIED"}:
        valuation_label = exchange_view["label"]
    elif valuation_input_count < 3:
        valuation_label = "UNVERIFIED"
    elif trailing_pe is not None and trailing_pe > 80:
        valuation_label = "EXCESSIVE"
    elif (
        peg is not None and 0 < peg <= 1.2
        and roe is not None and roe >= 12
        and (trailing_pe is None or trailing_pe <= 60)
    ):
        valuation_label = "ATTRACTIVE"
    else:
        valuation_label = "FAIR"

    # State and actionable verdict.
    if released and store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
        store.set_state(event, "FINANCIALS_PARSED", "financial statements parsed")
    if released:
        reaction_date = parse_date(store.value(event, "reaction_session"))
        if reaction_date and now_ist().date() < reaction_date:
            store.set_state(event, "REACTION_PENDING", "waiting for reaction session")
        elif result_ret is not None or rvol is not None:
            store.set_state(event, "REACTION_READY", "reaction metrics available")
    if released and result_label != "UNVERIFIED" and price_label != "UNVERIFIED":
        store.set_state(event, "PEAD_SCORED", "core PEAD signals scored")

    high = (
        released
        and result_label == "GENUINE"
        and expectation_has_full_window
        and expectation_label != "PRICED IN"
        and price_label == "CONFIRMED"
        and score >= 70
        and completeness >= 75
        and (mcap is not None and mcap >= MIN_MCAP_CR)
    )
    # Mechanical entry-state model. It identifies a setup/trigger; it never
    # claims the user actually bought the stock.
    mcap_ok = mcap is not None and mcap >= MIN_MCAP_CR
    setup_eligible = (
        released
        and result_label in {"GENUINE", "MIXED"}
        and price_label in {"CONFIRMED", "MIXED"}
        and score >= 55
        and completeness >= 60
        and mcap_ok
    )
    entry_trigger_price = round2(box_high * 1.002) if setup_eligible and box_high is not None else None
    entry_distance_pct = (
        round2(pct_change(last_price, entry_trigger_price))
        if last_price is not None and entry_trigger_price not in (None, 0)
        else None
    )

    if not released:
        entry_signal = "WAIT_RESULT"
    elif result_label == "LOW QUALITY" or price_label == "NEGATIVE":
        entry_signal = "NO_ENTRY"
    elif result_label == "UNVERIFIED" or price_label == "UNVERIFIED":
        entry_signal = "DATA_PENDING"
    elif not setup_eligible:
        entry_signal = "REVIEW_ONLY"
    elif box_high is None:
        entry_signal = "WAIT_BOX"
    elif hold5 is False:
        entry_signal = "WAIT_RECLAIM"
    elif breakout is True and last_price is not None and entry_trigger_price is not None and last_price >= entry_trigger_price:
        entry_signal = "ENTRY_TRIGGERED"
    elif hold5 is True and entry_distance_pct is not None and -2.0 <= entry_distance_pct < 0:
        entry_signal = "NEAR_ENTRY"
    elif hold5 is True:
        entry_signal = "WATCH_BREAKOUT"
    else:
        entry_signal = "WAIT_ACCEPTANCE"

    # Engine 2.4 plan replaces the box-only entry model: early entry on a
    # strong reaction, pullback to the 10/21 EMA, or post-result box breakout,
    # with the SL under the result-day low and TSL on the 21/63 EMA.
    plus = build_plus(store, event, released=released, result_label=result_label, price_label=price_label,
                      sector_info=sector_info, box_high=box_high, last_price=last_price, result_ret=result_ret,
                      rvol=rvol, result_low=result_low, result_high=store.value(event, "result_day_high"))
    plan = plus["plan"]
    entry_signal = plan.get("signal") or entry_signal
    entry_trigger_price = plan.get("entry")
    entry_distance_pct = plan.get("distancePct")
    px = plus["price"]
    bonus = 0
    if px.get("hv_label") in {"HVY", "HVE"}:
        bonus += 5
        reasons.append(f"Highest volume of the {'year' if px['hv_label'] == 'HVY' else 'listed history'} on the result session ({px['hv_label']}).")
    elif px.get("hv_label") == "HVQ":
        bonus += 3
        reasons.append("Highest volume of the quarter on the result session (HVQ).")
    mbps = safe_num(plus["margins"].get("changeBps"))
    if mbps is not None:
        if mbps >= 100:
            bonus += 4
            reasons.append(f"Operating margin expanded {mbps:+.0f} bps.")
        elif mbps <= -200:
            bonus -= 3
            risks.append(f"Operating margin contracted {mbps:+.0f} bps.")
    bcode = (plus.get("bucket") or {}).get("code")
    if bcode in {"CONFIRMATION", "RE_PEAD"}:
        bonus += 6
        reasons.append(f"{plus['bucket']['label']}: {plus['bucket']['why']}")
    elif bcode == "FRESH_PEAD":
        bonus += 4
        reasons.append(f"{plus['bucket']['label']}: {plus['bucket']['why']}")
    tail = ((plus.get("sector") or {}).get("tailwind"))
    if tail == "STRONG":
        bonus += 5
        reasons.append(f"Sector tailwind: {plus['sectorKey']} peers are outperforming.")
    elif tail == "POSITIVE":
        bonus += 2
    elif tail == "WEAK":
        bonus -= 3
        risks.append(f"Sector headwind: {plus['sectorKey']} peers are lagging.")
    if px.get("base_broken"):
        risks.append("Closed below the result-day low: post-result base broken.")
    if plus["liquidity"].get("pass") is False:
        risks.append(plus["liquidity"]["label"] + ".")
    score = max(0, min(100, score + bonus))

    entry_watch = entry_signal in pead_plus.ACTIONABLE
    if entry_watch:
        store.set_state(event, "ENTRY_WATCH", f"PEAD entry state: {entry_signal}")

    if not released:
        verdict = "AWAIT RESULT — WATCHLIST"
    elif high:
        verdict = "HIGH-CONVICTION PEAD CANDIDATE"
    elif result_label == "GENUINE" and expectation_label == "PRICED IN":
        verdict = "GOOD RESULT MAY BE PRICED IN — WAIT"
    elif result_label == "LOW QUALITY":
        verdict = "RESULT QUALITY WEAK — AVOID / REVIEW"
    elif result_label == "UNVERIFIED":
        verdict = "DATA PENDING — RESULT VERIFIED"
    elif score >= 55:
        verdict = "PEAD CANDIDATE — REVIEW ENTRY"
    else:
        verdict = "IN REVIEW — NOT ENOUGH EDGE YET"

    derived = {
        "score": score,
        "completenessPct": completeness,
        "completeness": {"present": have, "required": required},
        "resultReality": result_label,
        "expectationReality": expectation_label,
        "priceResponse": price_label,
        "valuationReality": valuation_label,
        "verdict": verdict,
        "highConviction": high,
        "entryWatch": entry_watch,
        "entrySignal": entry_signal,
        "entryTriggerPrice": entry_trigger_price,
        "entryDistancePct": entry_distance_pct,
        "plus": plus,
        "valuationInputCount": valuation_input_count,
        "expectationFullWindow": expectation_has_full_window,
        "reasons": reasons[:8],
        "risks": risks[:8],
    }
    event["derived"] = derived
    return derived


def _raw_folders_for(event: dict[str, Any], base: Path) -> list[Path]:
    sec = event.get("security") or {}
    prefixes = {safe_filename(event["eventId"]).rsplit("_", 1)[0]}
    for oid in event.get("mergedFrom") or []:
        prefixes.add(safe_filename(oid).rsplit("_", 1)[0])
    for alt in sec.get("altIsins") or []:
        prefixes.add(safe_filename(str(alt).upper()))
    if sec.get("isin"):
        prefixes.add(safe_filename(str(sec["isin"]).upper()))
    if sec.get("nseSymbol") or sec.get("symbol"):
        prefixes.add(safe_filename("NSE:" + normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))))
    if not base.exists():
        return []
    return [f for pre in prefixes for f in base.glob(f"{pre}_*") if f.is_dir()]


def _payload_symbol(payload: Any) -> str | None:
    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else None)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get("chSymbol"):
        return normalize_symbol(rows[0]["chSymbol"])
    return None


def rebuild_plus_from_raw(store: EventStore, event: dict[str, Any], raw_root: Path = RAW_DIR) -> bool:
    """Recompute price analytics from the newest saved price payload (NSE OHLC
    preferred, BSE close-only fallback). Used when no fresh fetch happened, so
    stored events get engine-2.4 features without extra network calls."""
    candidates = []
    for base, kind in ((raw_root / "nse_price", "NSE"), (raw_root / "bse_price", "BSE")):
        for folder in _raw_folders_for(event, base):
            for f in folder.glob("*.json"):
                candidates.append((kind == "NSE", f.stat().st_mtime, f, kind))
    # 2.5.4: pick the payload with the LATEST session (file mtimes are all
    # equal after a git checkout, so "newest file" picked a random one).
    frame, source, best_key = None, None, None
    nse_sym = normalize_symbol((event.get("security") or {}).get("nseSymbol"))
    for is_nse, _, f, kind in candidates:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if kind == "NSE" and _payload_symbol(payload) not in (None, nse_sym):
            continue   # another company's prices (pre-2.5.4 lookup mix-up)
        if kind == "BSE" and not (event.get("security") or {}).get("bseCode"):
            continue   # BSE code removed as another company's (2.6.1)
        fr = nse_history_to_frame(payload) if kind == "NSE" else bse_history_to_frame(payload)
        if fr is None or fr.empty:
            continue
        key = (fr["Date"].iloc[-1], is_nse, len(fr))
        if best_key is None or key > best_key:
            frame, source, best_key = fr, f"{kind}_PRICE", key
    if frame is None or getattr(frame, "empty", True):
        return False
    released = boolish(store.value(event, "results_released")) is True
    reaction = parse_date(store.value(event, "reaction_session"))
    result_date = parse_date(store.value(event, "result_date"))
    wstart = parse_date(store.value(event, "reaction_window_start")) or reaction
    traded = released and session_closed(reaction)
    x = pead_plus.extended_features(frame, reaction, released=traded,
                                    q1_reaction_date=previous_quarter_reaction(store, event),
                                    q1_window_start=previous_quarter_window_start(store, event),
                                    q2_boundary=wstart or result_date, window_start=wstart)
    if not x:
        return False
    event["plus"] = {"price": {k: v for k, v in x.items() if k != "chart"}, "chart": x.get("chart"),
                     "priceSource": source, "computedAt": iso_now(), "replayedFromRaw": True,
                     "plusVersion": PLUS_VERSION, "corporateActions": frame.attrs.get("corporateActions")}
    # Pre-result context (run-up, 52-week distance, turnover, last price) from
    # the same split-adjusted frame.
    m_all = price_metrics(frame, reaction or result_date, wstart)
    drop_stale_pre_result(store, event, m_all)
    for field in ("pre_result_5d_pct", "pre_result_10d_pct", "pre_result_20d_pct", "distance_52w_high_pct",
                  "last_price", "avg_turnover_20d_cr"):
        if m_all.get(field) is not None:
            store.force_field(event, field, m_all[field], source=source, note="replayed from saved price data")
    # OHLC from NSE fixes result-day high/low that were close-only before.
    if traded and source == "NSE_PRICE":
        m = price_metrics(frame, reaction, wstart)
        m.pop("_preResultStale", None)
        for field in ("result_day_low", "result_day_high", "result_day_return_pct", "result_day_rvol",
                      "post_result_hold_5d", "post_result_hold_10d", "box_high", "box_breakout"):
            if m.get(field) is not None:
                store.merge_field(event, field, m[field], source="NSE_PRICE", note="replayed from saved NSE OHLC")
    return True


PLUS_VERSION = "2.9.4"   # bump to rebuild every stored chart/price analytic once


def replay_plus(store: EventStore) -> int:
    n = 0
    for event in store.all():
        plus = event.get("plus") or {}
        if not dashboard_activity(event) or (plus.get("price") and plus.get("plusVersion") == PLUS_VERSION):
            continue
        if rebuild_plus_from_raw(store, event):
            store.save(event)
            n += 1
    return n


def sector_universe(store: EventStore, events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for e in events:
        if not dashboard_activity(e):
            continue
        px = (e.get("plus") or {}).get("price") or {}
        released = boolish(store.value(e, "results_released")) is True
        rows.append({
            "sectorKey": sector_key(e.get("security") or {}),
            "ret63": px.get("ret_63d_pct"),
            "released": released,
            "strength": pead_plus.earnings_strength(store.value(e, "revenue_yoy_pct"), store.value(e, "pat_yoy_pct"),
                                                    store.value(e, "pat_trend"), store.value(e, "margin_change_bps")) if released else None,
            "reaction": store.value(e, "result_day_return_pct"),
            "liquid": pead_plus.liquidity(store.value(e, "avg_turnover_20d_cr"), store.value(e, "last_price")).get("pass"),
        })
    return pead_plus.sector_stats(rows)


def score_all(store: EventStore) -> dict[str, Any]:
    stats = {"scored": 0, "highConviction": 0}
    events = store.all()
    sectors = sector_universe(store, events)
    stats["sectors"] = len(sectors)
    for event in events:
        try:
            d = score_event(store, event, sectors)
            stats["scored"] += 1
            stats["highConviction"] += int(d.get("highConviction") is True)
            store.save(event)
        except Exception:
            traceback.print_exc()
    return stats


# ---------------------------------------------------------------------------
# Publishing / compatibility layer
# ---------------------------------------------------------------------------


def meta_value(event: dict[str, Any], field: str) -> Any:
    meta = event.get("fields", {}).get(field)
    return meta.get("value") if isinstance(meta, dict) and meta.get("status") == "OK" else None


def event_to_data_row(event: dict[str, Any]) -> dict[str, Any]:
    sec = event.get("security", {})
    d = event.get("derived", {})
    period = event.get("period", {})
    released = boolish(meta_value(event, "results_released")) is True
    score8 = round((d.get("score") or 0) / 100 * 8, 1)
    result_date = meta_value(event, "result_date")
    pat_trend_value = meta_value(event, "pat_trend")
    rev_yoy = safe_num(meta_value(event, "revenue_yoy_pct"))
    pat_yoy = safe_num(meta_value(event, "pat_yoy_pct"))
    turnover = safe_num(meta_value(event, "avg_turnover_20d_cr"))
    revenue_cr = safe_num(meta_value(event, "revenue_cr"))
    pat_cr = safe_num(meta_value(event, "pat_cr"))

    if not released:
        earnings_accel = None
    else:
        earnings_accel = (
            rev_yoy is not None
            and rev_yoy >= REV_YOY_STRONG
            and (pat_trend_value == "TURNAROUND" or (pat_trend_value == "PROFIT_GROWTH" and pat_yoy is not None and pat_yoy >= PAT_YOY_STRONG))
        ) if (rev_yoy is not None and (pat_trend_value is not None or pat_yoy is not None)) else None

    earnings_quality = None
    if released and revenue_cr is not None and pat_cr is not None:
        earnings_quality = bool(revenue_cr > 0 and pat_cr > 0 and d.get("resultReality") != "LOW QUALITY")

    price_volume_pass = None
    if d.get("priceResponse") == "CONFIRMED":
        price_volume_pass = True
    elif d.get("priceResponse") == "NEGATIVE":
        price_volume_pass = False

    liquidity_pass = None if turnover is None else turnover >= LIQUIDITY_TURNOVER_CR_MIN
    priced_in = d.get("expectationReality") == "PRICED IN"
    allocation = 30 if d.get("highConviction") else (20 if (d.get("score") or 0) >= 55 else (10 if not released else 0))
    entry_signal = str(d.get("entrySignal") or ("WAIT_RESULT" if not released else "REVIEW_ONLY"))
    entry_trigger = entry_signal == "ENTRY_TRIGGERED" if released else None
    box_high = safe_num(meta_value(event, "box_high"))
    plus = d.get("plus") or {}
    plan = plus.get("plan") or {}
    mechanical_entry = safe_num(plan.get("entry"))
    actionable_entry = entry_signal in pead_plus.ACTIONABLE
    mechanical_sl = round2(plan.get("sl")) if actionable_entry else None
    mechanical_tsl = round2(plan.get("tslSwing")) if actionable_entry else None
    tracked_change = price_trail_change(event)
    price_trail = event.get("priceTrail") if isinstance(event.get("priceTrail"), list) else []

    checks = [
        {"label": "Results released", "value": released, "note": f"Persistent event state: {event.get('state')}"},
        {"label": f"Market cap > ₹{MIN_MCAP_CR:,.0f} Cr", "value": (safe_num(meta_value(event, "market_cap_cr")) >= MIN_MCAP_CR) if safe_num(meta_value(event, "market_cap_cr")) is not None else None, "note": f"₹{safe_num(meta_value(event, 'market_cap_cr')):,.0f} Cr" if safe_num(meta_value(event, "market_cap_cr")) is not None else "Unverified"},
        {"label": "Earnings acceleration", "value": earnings_accel, "note": (f"Revenue YoY {rev_yoy:+.1f}%; PAT {str(pat_trend_value or pat_yoy or 'pending')}" if rev_yoy is not None else "Growth data pending")},
        {"label": "Earnings quality", "value": earnings_quality, "note": (f"Revenue ₹{revenue_cr:.2f} Cr; PAT ₹{pat_cr:.2f} Cr" if revenue_cr is not None and pat_cr is not None else "Current-quarter financials pending")},
        {"label": "Cash flow", "value": None, "note": "Not a blocking PEAD v2 core gate; enrich separately when verified"},
        {"label": "Surprise", "value": None, "note": "No consensus feed; v2 uses verified earnings acceleration as the primary event-strength proxy"},
        {"label": "Post-result price/volume confirmation", "value": price_volume_pass, "note": f"Return {round2(meta_value(event, 'result_day_return_pct'))}; RVOL {round2(meta_value(event, 'result_day_rvol'))}"},
        {"label": "Liquidity", "value": liquidity_pass, "note": f"20D average turnover ₹{turnover:.1f} Cr" if turnover is not None else "Turnover pending"},
    ]

    fetch_errors = [
        f"{src}: {meta.get('error')}"
        for src, meta in (event.get("fetch") or {}).items()
        if isinstance(meta, dict) and meta.get("status") == "FAILED" and meta.get("error")
    ]

    return {
        "eventId": event.get("eventId"),
        "symbol": sec.get("symbol") or sec.get("nseSymbol") or sec.get("bseSymbol") or sec.get("bseCode") or "UNKNOWN",
        "sym": sec.get("symbol") or sec.get("nseSymbol"),
        "name": sec.get("name") or sec.get("symbol") or "Unknown",
        "sector": plus.get("sectorKey") or sec.get("sector") or sec.get("industry") or "—",
        "industry": next((v for v in (sec.get("basicIndustry"), sec.get("industry"), sec.get("sector"))
                          if str(v or "").strip() not in {"", "—", "-"} and v != plus.get("sectorKey")), None),
        "isin": sec.get("isin"),
        "bseCode": sec.get("bseCode"),
        "ticker": sec.get("yahooTicker"),
        "quarter": period.get("quarter") or fiscal_quarter(parse_date(period.get("end"))),
        "earningsPeriod": period.get("quarter") or fiscal_quarter(parse_date(period.get("end"))),
        "resultPeriodEnd": period.get("end"),
        "resultDate": result_date,
        "resultsReleased": released,
        "financialSource": (event.get("financialIntegrity") or {}).get("selectedSource") if released else None,
        "financialBasis": (event.get("financialIntegrity") or {}).get("basis") if released else None,
        "financialStatus": (event.get("financialIntegrity") or {}).get("status") if released else None,
        "financialIssues": (event.get("financialIntegrity") or {}).get("issues") if released else None,
        "revenueDefinition": (event.get("financialIntegrity") or {}).get("revenueDefinition") if released else None,
        "resultReleased": released,
        "peadStatus": event.get("state"),
        "bucket": "Post-results" if released else "Upcoming",
        "eventState": event.get("state"),
        "dataCompletenessPct": d.get("completenessPct"),
        "marketCapCr": round2(meta_value(event, "market_cap_cr")),
        "marketCapPass": (safe_num(meta_value(event, "market_cap_cr")) >= MIN_MCAP_CR) if safe_num(meta_value(event, "market_cap_cr")) is not None else None,
        "price": round2(meta_value(event, "last_price")),
        "lastPrice": round2(meta_value(event, "last_price")),
        "priceTimestamp": latest_price_timestamp(event) or event.get("updatedAt"),
        "priceTrackChangePct": tracked_change,
        "priceTrail": price_trail[-12:],
        "changePct": tracked_change,
        "revenueYoY": round2(meta_value(event, "revenue_yoy_pct")),
        "patYoY": round2(meta_value(event, "pat_yoy_pct")),
        "patYoYTurnaround": pat_trend_value == "TURNAROUND",
        "patYoYStatus": pat_trend_value,
        "patQoQ": round2(meta_value(event, "pat_qoq_pct")),
        "revenueQoQ": round2(meta_value(event, "revenue_qoq_pct")),
        "latestRevenueCr": round2(meta_value(event, "revenue_cr")),
        "latestPatCr": round2(meta_value(event, "pat_cr")),
        "reportedEps": round2(meta_value(event, "eps")),
        "preResult5dPct": round2(meta_value(event, "pre_result_5d_pct")),
        "preResult10dPct": round2(meta_value(event, "pre_result_10d_pct")),
        "preResultRunupPct": round2(meta_value(event, "pre_result_20d_pct")),
        "resultDayReturnPct": round2(meta_value(event, "result_day_return_pct")),
        "relativeVolume": round2(meta_value(event, "result_day_rvol")),
        "distanceFrom52wHighPct": round2(meta_value(event, "distance_52w_high_pct")),
        "avgTurnover20dCr": round2(meta_value(event, "avg_turnover_20d_cr")),
        "resultDayLow": round2(meta_value(event, "result_day_low")),
        "resultDayHigh": round2(meta_value(event, "result_day_high")),
        "holdAboveResultLow5d": meta_value(event, "post_result_hold_5d"),
        "holdAboveResultLow10d": meta_value(event, "post_result_hold_10d"),
        "darvasBoxHigh": round2(meta_value(event, "box_high")),
        "darvasBreakout": meta_value(event, "box_breakout"),
        "filingSession": meta_value(event, "filing_session"),
        "reactionSession": meta_value(event, "reaction_session"),
        # 2.9.2: NSE quotes P/E 0 for loss-makers; publish null, not 0.0.
        "trailingPE": round2(pe_) if (pe_ := safe_num(meta_value(event, "trailing_pe"))) is not None and pe_ > 0 else None,
        "forwardPE": round2(meta_value(event, "forward_pe")),
        "peg": round2(meta_value(event, "peg")),
        "roePct": round2(nonzero(meta_value(event, "roe_pct"))),
        "fcfYieldPct": round2(meta_value(event, "fcf_yield_pct")),
        "score": score8,
        "scoreText": f"{score8:g}/8",
        "checks": checks,
        "knownChecks": sum(c.get("value") is not None for c in checks),
        "earningsAccelerationPass": earnings_accel,
        "revenuePatPass": earnings_accel,
        "earningsEvidence": checks[2]["note"],
        "earningsQualityPass": earnings_quality,
        "qualityEvidence": checks[3]["note"],
        "cashFlowPass": None,
        "cashFlowEvidence": checks[4]["note"],
        "surprisePass": None,
        "surpriseEvidence": checks[5]["note"],
        "priceVolumePass": price_volume_pass,
        "technicalPass": price_volume_pass,
        "priceVolumeEvidence": checks[6]["note"],
        "liquidityPass": liquidity_pass,
        "liquidityEvidence": checks[7]["note"],
        "sectorTailwind": ((plus.get("sector") or {}).get("tailwind")),
        "pricedIn": priced_in,
        "candidateStatus": event.get("state"),
        "allocationPct": allocation,
        "entryTriggerPass": entry_trigger,
        "entryWatchPass": actionable_entry,
        "entrySignal": entry_signal,
        "entryDistancePct": round2(d.get("entryDistancePct")),
        "entry": round2(mechanical_entry) if actionable_entry else None,
        "entryTriggerPrice": round2(mechanical_entry),
        "sl": mechanical_sl,
        "tsl": mechanical_tsl,
        "liveStatus": event.get("state"),
        "liveError": "; ".join(fetch_errors[:3]) if fetch_errors else None,
        "evidence": f"Persistent v2 event · state {event.get('state')} · completeness {d.get('completenessPct', 0):.0f}%",
        "resultEvidence": f"Persistent v2 event · state {event.get('state')} · completeness {d.get('completenessPct', 0):.0f}%",
        "thesis": d.get("reasons") or [],
        "convictionScore": d.get("score"),
        "verdict": d.get("verdict"),
        "resultReality": d.get("resultReality"),
        "expectationReality": d.get("expectationReality"),
        "priceResponse": d.get("priceResponse"),
        "valuationReality": d.get("valuationReality"),
        "reasons": d.get("reasons") or [],
        "risks": d.get("risks") or [],
        "resultSource": field_source(event, "results_released"),
        "resultDataSource": field_source(event, "revenue_cr") or field_source(event, "pat_cr"),
        # 2.9.4: link the document whose figures are shown.
        "resultSourceUrl": (event.get("financialIntegrity") or {}).get("documentUrl") or meta_value(event, "xbrl_url"),
        "resultsEvidence": f"Persistent v2 event · state {event.get('state')} · completeness {d.get('completenessPct', 0):.0f}%",
        "sourceHealth": event.get("fetch") or {},
    }


def event_to_intelligence(event: dict[str, Any]) -> dict[str, Any]:
    row = event_to_data_row(event)
    d = event.get("derived", {})
    pat_display = row.get("patYoY")
    rr_reasons = list(d.get("reasons") or [])
    rr_risks = list(d.get("risks") or [])
    if row.get("patYoYStatus") == "TURNAROUND" and "PAT turned profitable versus a loss/zero base." not in rr_reasons:
        rr_reasons.insert(0, "PAT turned profitable versus a loss/zero base.")

    return {
        "eventId": row["eventId"],
        "symbol": row["symbol"],
        "name": row["name"],
        "sector": row["sector"],
        "quarter": row["quarter"],
        "resultDate": row["resultDate"],
        "resultsReleased": row["resultsReleased"],
        "financialSource": row.get("financialSource"),
        "financialBasis": row.get("financialBasis"),
        "financialStatus": row.get("financialStatus"),
        "financialIssues": row.get("financialIssues"),
        "eventState": row["eventState"],
        "dataCompletenessPct": row["dataCompletenessPct"],
        "baseScoreText": row["scoreText"],
        "convictionScore": row["convictionScore"] or 0,
        "verdict": row["verdict"],
        "resultReality": {"label": row["resultReality"], "reasons": rr_reasons, "risks": rr_risks},
        "expectationReality": {"label": row["expectationReality"], "reasons": [], "risks": []},
        "valuationReality": {
            "label": row["valuationReality"],
            "confidence": "NORMAL" if sum(row.get(k) is not None for k in ("trailingPE", "forwardPE", "peg", "roePct", "fcfYieldPct")) >= 3 else "LIMITED",
            "metrics": {
                "trailingPE": row["trailingPE"],
                "forwardPE": row["forwardPE"],
                "peg": row["peg"],
                "roePct": row["roePct"],
                "fcfYieldPct": row["fcfYieldPct"],
            },
            "reasons": [], "risks": [],
        },
        "priceResponse": {"label": row["priceResponse"], "points": 0},
        "priceContext": {
            "pre5dPct": row["preResult5dPct"],
            "pre10dPct": row["preResult10dPct"],
            "pre20dPct": row["preResultRunupPct"],
            "resultDayPct": row["resultDayReturnPct"],
            "relativeVolume": row["relativeVolume"],
            "distanceFrom52wHighPct": row["distanceFrom52wHighPct"],
            "lastClose": row["price"],
            "historyAvailable": any(row.get(k) is not None for k in ("preResult5dPct", "preResult10dPct", "preResultRunupPct", "resultDayReturnPct")),
            "trackedChangePct": row.get("priceTrackChangePct"),
            "updatedAt": row.get("priceTimestamp"),
            "trail": row.get("priceTrail") or [],
        },
        "fundamentalSnapshot": {
            "revenueYoYCalc": row["revenueYoY"],
            "patYoYCalc": pat_display,
            "patYoYTurnaround": row["patYoYTurnaround"],
            "patYoYStatus": row["patYoYStatus"],
            "revenueCr": row["latestRevenueCr"],
            "patCr": row["latestPatCr"],
            "eps": row["reportedEps"],
        },
        "entry": row["entry"],
        "entryTriggerPrice": row["entryTriggerPrice"],
        "entrySignal": row["entrySignal"],
        "entryDistancePct": row["entryDistancePct"],
        "sl": row["sl"],
        "tsl": row["tsl"],
        "revenueYoY": row["revenueYoY"],
        "patYoY": row["patYoY"],
        "patYoYTurnaround": row["patYoYTurnaround"],
        "patYoYStatus": row["patYoYStatus"],
        "patQoQ": row["patQoQ"],
        "reactionSession": row["reactionSession"],
        "reactionWindowStart": meta_value(event, "reaction_window_start"),
        "filingSession": row["filingSession"],
        "darvasBoxHigh": row["darvasBoxHigh"],
        "darvasBreakout": row["darvasBreakout"],
        "holdAboveResultLow5d": row["holdAboveResultLow5d"],
        "holdAboveResultLow10d": row["holdAboveResultLow10d"],
        "reasons": d.get("reasons") or [],
        "risks": d.get("risks") or [],
        "managementCommentary": None,
        "commentaryVerified": False,
        "industry": row.get("industry"),
        "marketCapCr": row.get("marketCapCr"),
        "avgTurnover20dCr": row.get("avgTurnover20dCr"),
        "plus": {k: v for k, v in (d.get("plus") or {}).items() if k != "price"} | {
            "price": {k: v for k, v in ((d.get("plus") or {}).get("price") or {}).items()},
            "chart": (event.get("plus") or {}).get("chart"),
            "priceSource": (event.get("plus") or {}).get("priceSource"),
        },
    }


def aggregate_health(store: EventStore, discovery_stats: dict[str, Any], enrichment_stats: dict[str, Any]) -> dict[str, Any]:
    events = store.all()
    all_rows = [event_to_data_row(e) for e in events]
    all_declared = [r for r in all_rows if r.get("resultsReleased") is True]

    active_events = [e for e in events if dashboard_activity(e)]
    active_rows = [event_to_data_row(e) for e in active_events]
    declared = [r for r in active_rows if r.get("resultsReleased") is True]
    financial = [r for r in declared if r.get("latestRevenueCr") is not None and r.get("latestPatCr") is not None]
    fin_status: dict[str, int] = {}
    for e in active_events:
        if boolish(meta_value(e, "results_released")) is True:
            st = (e.get("financialIntegrity") or {}).get("status") or "NOT_CHECKED"
            fin_status[st] = fin_status.get(st, 0) + 1
    reaction = [r for r in declared if r.get("resultDayReturnPct") is not None or r.get("relativeVolume") is not None]
    fully_scored = [r for r in declared if (r.get("dataCompletenessPct") or 0) >= 75]

    fetch_today = LOG_DIR / f"fetch_{now_ist().date().isoformat()}.jsonl"
    ok = failed = 0
    by_source: dict[str, dict[str, int]] = {}
    if fetch_today.exists():
        for line in fetch_today.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            src = str(rec.get("source") or "UNKNOWN")
            by_source.setdefault(src, {"ok": 0, "failed": 0})
            if rec.get("status") == "OK":
                ok += 1
                by_source[src]["ok"] += 1
            else:
                failed += 1
                by_source[src]["failed"] += 1

    completeness_avg = (
        round(sum((r.get("dataCompletenessPct") or 0) for r in declared) / len(declared), 1)
        if declared else 0.0
    )
    live_period = live_reporting_period()
    return {
        "generatedAt": iso_now(),
        "engineVersion": ENGINE_VERSION,
        "livePeriodEnd": live_period.isoformat() if live_period else None,
        "liveQuarter": fiscal_quarter(live_period),
        "eventsTracked": len(all_rows),
        "activeDashboardEvents": len(active_rows),
        "archiveEvents": max(0, len(all_rows) - len(active_rows)),
        "activeResultsFiled": len(declared),
        "storedResultsFiled": len(all_declared),
        "resultsFiled": len(declared),
        "financialsParsed": len(financial),
        "financialsVerified": fin_status.get("VERIFIED", 0),
        "financialsFlagged": fin_status.get("FLAGGED", 0),
        "financialsNoSnapshot": fin_status.get("NO_VERIFIED_SNAPSHOT", 0) + fin_status.get("NOT_CHECKED", 0),
        "reactionReady": len(reaction),
        "fullyScored": len(fully_scored),
        "declaredCompletenessPct": completeness_avg,
        "fetchesOk": ok,
        "fetchesFailed": failed,
        "sources": by_source,
        "discovery": discovery_stats,
        "enrichment": enrichment_stats,
    }




def previous_health() -> dict[str, Any]:
    """Health of the last PUBLISHED feed.

    Regression: run_health.json is rewritten even when a run is blocked, so
    reading it first let the next hourly run compare against the blocked run
    and pass. The published data.json is the only valid baseline.
    """
    for path in (DATA_PATH, INTELLIGENCE_PATH, HEALTH_PATH):
        if not path.exists():
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if path == HEALTH_PATH:
                return obj
            if isinstance(obj, dict) and isinstance(obj.get("health"), dict):
                return obj["health"]
        except Exception:
            pass
    return {}


def integrity_signature(revocations: list[dict[str, Any]]) -> str | None:
    """Stable fingerprint of a set of declaration revocations."""
    if not revocations:
        return None
    lines = sorted(
        f"{r.get('eventId')}|{','.join(sorted(x.split(':')[0] for x in r.get('reasons') or []))}" for r in revocations
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def quality_gate(new_health: dict[str, Any], old_health: dict[str, Any], approved_signature: str | None = None) -> tuple[bool, list[str]]:
    reasons = []
    integrity = new_health.get("integrity") or {}
    signature = integrity.get("signature")
    baseline_approved = bool(signature and approved_signature and signature == approved_signature)
    # Folding a duplicate removes a row but no company: add folds back
    # before comparing with the previous publish.
    current_rows = int(new_health.get("activeDashboardEvents") or 0) + int(integrity.get("duplicatesMerged") or 0)
    current_declared = int(new_health.get("resultsFiled") or 0) + int(integrity.get("declaredMerged") or 0)
    old_rows = int(old_health.get("activeDashboardEvents") or old_health.get("eventsTracked") or 0)
    old_declared = int(old_health.get("resultsFiled") or 0)
    current_comp = float(new_health.get("declaredCompletenessPct") or 0)
    old_comp = float(old_health.get("declaredCompletenessPct") or 0)

    new_quarter = str(new_health.get("liveQuarter") or "")
    old_quarter = str(old_health.get("liveQuarter") or "")
    comparable_quarter = bool(new_quarter and old_quarter and new_quarter == old_quarter)

    if current_rows == 0:
        reasons.append("live current-quarter dashboard contains zero rows")

    # Row-count/completeness collapse checks are meaningful only inside the same
    # fiscal reporting quarter. On the first strict-quarter migration (or when
    # the market rolls from Q2 to Q3), a large count drop is intentional.
    if comparable_quarter:
        if old_rows >= 10 and current_rows < old_rows * 0.80 and not baseline_approved:
            msg = f"event count dropped from {old_rows} to {current_rows} (>20%)"
            if signature:
                msg += (f"; the integrity pass made {integrity.get('revoked', 0)} revocations/duplicate merges. "
                        f"Review logs/integrity_{now_ist().date().isoformat()}.json and re-run with "
                        f"--approve-baseline {signature} if they are correct")
            reasons.append(msg)
        if old_declared >= 5 and current_declared < old_declared * 0.80 and not baseline_approved:
            msg = f"declared-result count dropped from {old_declared} to {current_declared} (>20%)"
            if signature:
                msg += (f"; the integrity pass made {integrity.get('revoked', 0)} revocations/duplicate merges. "
                        f"Review logs/integrity_{now_ist().date().isoformat()}.json and re-run with "
                        f"--approve-baseline {signature} if the revocations are correct")
            reasons.append(msg)
        if old_declared >= 5 and old_comp >= 40 and current_comp < max(20, old_comp - 25) and not baseline_approved:
            reasons.append(f"declared completeness collapsed from {old_comp:.1f}% to {current_comp:.1f}%")
    if approved_signature and not baseline_approved:
        reasons.append(
            f"--approve-baseline {approved_signature} does not match this run's integrity signature "
            f"{signature or '(no revocations)'}; nothing was approved"
        )
    return len(reasons) == 0, reasons


def publish(store: EventStore, health: dict[str, Any], *, force: bool = False, approved_signature: str | None = None) -> tuple[bool, list[str]]:
    old = previous_health()
    old.pop("qualityGate", None)
    ok, reasons = quality_gate(health, old, approved_signature)
    health["qualityGate"] = {"passed": ok, "reasons": reasons, "previous": old,
                             "approvedBaseline": approved_signature if ok and approved_signature else None}
    json_dump_atomic(HEALTH_PATH, health)
    if not ok and not force:
        return False, reasons

    stored_events = store.all()
    events = sorted(
        [e for e in stored_events if dashboard_activity(e)],
        key=lambda e: (
            parse_date(meta_value(e, "result_date")) or date.min,
            parse_date(e.get("period", {}).get("end")) or date.min,
            str(e.get("security", {}).get("symbol") or ""),
        ),
        reverse=True,
    )
    rows = [event_to_data_row(e) for e in events]
    items = [event_to_intelligence(e) for e in events]
    sectors = sector_universe(store, stored_events)
    index_cache = load_index_cache()
    as_of = (index_cache.get("dates") or [None])[-1]
    stale = not as_of or (now_ist().date() - date.fromisoformat(as_of)).days > 7
    regime = pead_plus.market_regime(None if stale else index_cache.get("closes"))
    if stale and as_of:
        regime["note"] = f"Index data is stale (last {as_of}); market regime not shown."
    regime["index"] = index_cache.get("index")
    regime["asOf"] = as_of
    timing = pead_plus.entry_timing_scorecard([e.get("tradeLog") for e in stored_events])
    card = pead_plus.scorecard([
        {"q1Return": (it["plus"].get("price") or {}).get("q1_reaction_return_pct"),
         "q1ToQ2": (it["plus"].get("price") or {}).get("q1_return_to_q2_pct"),
         "q1Setup": it["plus"].get("q1Setup")}
        for it in items
    ])
    health["activeDashboardEvents"] = len(events)
    health["archiveEvents"] = max(0, len(stored_events) - len(events))

    data_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "scannerMode": "persistent-event-store-v2",
        "qualificationVersion": f"pead-core-v2-{ENGINE_VERSION}",
        "minMarketCapCr": MIN_MCAP_CR,
        "health": health,
        "livePeriodEnd": health.get("livePeriodEnd"),
        "liveQuarter": health.get("liveQuarter"),
        "stocks": rows,
    }
    counts = {
        "total": len(items),
        "resultsDeclared": sum(x.get("resultsReleased") is True for x in items),
        "genuineResults": sum(x.get("resultReality", {}).get("label") == "GENUINE" for x in items),
        "pricedIn": sum(x.get("expectationReality", {}).get("label") == "PRICED IN" for x in items),
        "highConviction": sum(x.get("verdict") == "HIGH-CONVICTION PEAD CANDIDATE" for x in items),
        "dataPending": sum(x.get("verdict") == "DATA PENDING — RESULT VERIFIED" for x in items),
        "entryWatch": sum(x.get("entrySignal") in pead_plus.ACTIONABLE for x in items),
        "entryTriggered": sum(str(x.get("entrySignal") or "").startswith("ENTRY_") for x in items),
        "liquid": sum((x["plus"].get("liquidity") or {}).get("pass") is True for x in items),
        "buckets": {code: sum(((x["plus"].get("bucket") or {}).get("code") == code) for x in items) for code in pead_plus.BUCKETS},
        "nearEntry": sum(x.get("entrySignal") == "NEAR_ENTRY" for x in items),
    }
    try:
        internals = json.loads(MARKET_INTERNALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        internals = {}
    regime.update(pead_plus.market_internals(items, internals, now_ist().date()))
    pead_plus.peer_context(items, regime)
    try:
        health["selfAudit"] = pead_plus.self_audit(items, regime, health, now_ist().date())
        json_dump_atomic(LOG_DIR / "self_audit.json", health["selfAudit"])
    except Exception as exc:   # the audit must never block publishing
        health["selfAudit"] = {"status": "FAIL", "checks": [{"check": "Self-audit ran", "status": "FAIL", "count": 1,
                                                            "examples": [f"{type(exc).__name__}: {exc}"], "note": ""}]}
    intel_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "version": f"PEAD Intelligence v2 {ENGINE_VERSION}",
        "health": health,
        "livePeriodEnd": health.get("livePeriodEnd"),
        "liveQuarter": health.get("liveQuarter"),
        "counts": counts,
        "regime": regime,
        "sectors": sorted(sectors.values(), key=lambda r: (-(r.get("relativeToMarket") or -999), r["sector"])),
        "scorecard": card | {"timing": timing},
        "thresholds": {
            "liquidityTurnoverCr": pead_plus.LIQ_TURNOVER_CR, "liquidityMinPrice": pead_plus.LIQ_MIN_PRICE,
            "maxRiskPct": pead_plus.MAX_RISK_PCT, "strongRevYoY": pead_plus.STRONG_REV_YOY, "strongPatYoY": pead_plus.STRONG_PAT_YOY,
        },
        "items": items,
    }
    json_dump_atomic(DATA_PATH, data_payload)
    # The dashboard feed is compact (no indentation) to keep it light on mobile.
    json_dump_atomic(INTELLIGENCE_PATH, intel_payload, compact=True)
    return True, []


# ---------------------------------------------------------------------------
# Self tests
# ---------------------------------------------------------------------------


def self_test() -> int:
    failures = []

    # Non-null merge and source precedence.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "events")
        e = store.ensure_event(security={"symbol": "TEST", "nseSymbol": "TEST"}, period_end=date(2026, 9, 30))
        assert store.merge_field(e, "revenue_cr", 100, source="NSE_RESULTS_COMPARISON") is True
        assert store.merge_field(e, "revenue_cr", None, source="YAHOO_QUARTERLY") is False
        assert store.value(e, "revenue_cr") == 100
        assert store.merge_field(e, "revenue_cr", 90, source="YAHOO_QUARTERLY") is False
        assert store.value(e, "revenue_cr") == 100
        assert store.merge_field(e, "revenue_cr", 105, source="NSE_XBRL") is True
        assert store.value(e, "revenue_cr") == 105

    # Regression: a fully identified event must never become None.
    with tempfile.TemporaryDirectory() as td:
        troot = Path(td)
        store = EventStore(troot / "events")
        master = SymbolMaster(troot / "symbols.json")
        e = store.ensure_event(
            security={
                "symbol": "TESTBSE",
                "bseCode": "500001",
                "isin": "INE000A01001",
                "yahooTicker": "500001.BO",
            },
            period_end=date(2026, 9, 30),
        )
        identified = fill_security_identity(e, store, master, ctx=None)
        if not isinstance(identified, dict):
            failures.append("full-identity enrichment returned None")

        # Also verify malformed legacy fetch metadata is repaired rather than
        # crashing the error/fetch logger.
        identified["fetch"] = None
        store.record_fetch(identified, "ENGINE_TEST", ok=False, error="synthetic")
        if not isinstance(identified.get("fetch"), dict):
            failures.append("fetch metadata repair failed")

    # BSE-only identities must never invent an NSE Yahoo ticker.
    # This regression test must be independent of the persistent Yahoo
    # negative-cache.  A real prior 404 for 523343.BO is allowed to suppress
    # the ticker during normal operation, but it must not make the mapping
    # self-test fail.
    bse_event = {
        "eventId": "TESTBSE|2026-09-30",
        "security": {"symbol": "MICROSE", "bseCode": "523343"},
    }
    test_ticker = "523343.BO"
    negative_cache = _load_yahoo_negative_cache()
    saved_negative = negative_cache.pop(test_ticker, None)
    try:
        candidates = YahooAdapter(ctx=None)._ticker_candidates(bse_event)
        if candidates != [test_ticker]:
            failures.append(f"BSE-only Yahoo mapping wrong: {candidates}")
    finally:
        if saved_negative is not None:
            negative_cache[test_ticker] = saved_negative
        else:
            negative_cache.pop(test_ticker, None)

    # Active/archive selection regression: old periods must never consume
    # hourly network enrichment, while current and near-term events do.
    test_today = date(2026, 10, 7)
    old_event = {
        "eventId": "OLD|2021-03-31",
        "state": "RESULT_FILED",
        "period": {"end": "2021-03-31"},
        "fields": {
            "result_date": {"value": "2026-10-06"},
            "results_released": {"value": True},
        },
    }
    live_event = {
        "eventId": "LIVE|2026-09-30",
        "state": "RESULT_FILED",
        "period": {"end": "2026-09-30"},
        "fields": {
            "result_date": {"value": "2026-10-06"},
            "results_released": {"value": True},
        },
    }
    upcoming_event = {
        "eventId": "NEXT|2026-09-30",
        "state": "SCHEDULED",
        "period": {"end": "2026-09-30"},
        "fields": {"result_date": {"value": "2026-10-20"}},
    }
    if enrichment_activity(old_event, test_today)[0] is not False:
        failures.append("old fiscal period was not archived")
    if enrichment_activity(live_event, test_today)[0] is not True:
        failures.append("current filed result was not active")
    if enrichment_activity(upcoming_event, test_today)[0] is not True:
        failures.append("near-term upcoming event was not active")

    # PAT trend edge cases.
    cases = [
        ((55, -12), "TURNAROUND", None),
        ((-5, 10), "DETERIORATION", None),
        ((-8, -20), "LOSS_NARROWING", None),
        ((-20, -8), "LOSS_WIDENING", None),
        ((20, 10), "PROFIT_GROWTH", 100.0),
    ]
    for args, expected_label, expected_pct in cases:
        label, pct = pat_trend(*args)
        if label != expected_label or (expected_pct is not None and round(pct or 0, 1) != expected_pct):
            failures.append(f"PAT trend failed for {args}: got {(label, pct)}")

    # Reaction-session timing.
    dt_after = datetime(2026, 10, 7, 18, 30, tzinfo=IST)
    reaction, basis = reaction_session(dt_after, dt_after.date())
    if not (reaction > dt_after.date() and basis == "AFTER_CLOSE"):
        failures.append("after-close reaction session failed")

    # Fiscal quarter convention.
    if fiscal_quarter(date(2026, 9, 30)) != "Q2 FY27":
        failures.append("fiscal quarter mapping failed")

    # Quality gate protects against collapse.
    ok, reasons = quality_gate(
        {"eventsTracked": 20, "resultsFiled": 3, "declaredCompletenessPct": 60},
        {"eventsTracked": 30, "resultsFiled": 20, "declaredCompletenessPct": 70},
    )
    if ok or not reasons:
        failures.append("quality gate did not block collapse")

    # Valuation requires at least three genuine inputs; one PEG must not label attractive.
    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "events")
        e = store.ensure_event(security={"symbol": "VALTEST", "nseSymbol": "VALTEST"}, period_end=date(2026, 9, 30))
        store.merge_field(e, "results_released", True, source="NSE_FINANCIAL_RESULTS")
        store.merge_field(e, "result_date", now_ist().date().isoformat(), source="NSE_FINANCIAL_RESULTS")
        store.merge_field(e, "revenue_yoy_pct", 25, source="NSE_RESULTS_COMPARISON")
        store.merge_field(e, "pat_trend", "PROFIT_GROWTH", source="DERIVED")
        store.merge_field(e, "pat_yoy_pct", 30, source="DERIVED")
        store.merge_field(e, "peg", 0.4, source="YAHOO_FUNDAMENTALS")
        d = score_event(store, e)
        if d.get("valuationReality") != "UNVERIFIED":
            failures.append("valuation accepted fewer than 3 genuine inputs")

    # Entry signal exposes a watch/trigger level without claiming a user fill.
    with tempfile.TemporaryDirectory() as td:
        store = EventStore(Path(td) / "events")
        e = store.ensure_event(security={"symbol": "ENTRYTEST", "nseSymbol": "ENTRYTEST"}, period_end=date(2026, 9, 30))
        for field, value, source in (
            ("results_released", True, "NSE_FINANCIAL_RESULTS"),
            ("result_date", now_ist().date().isoformat(), "NSE_FINANCIAL_RESULTS"),
            ("revenue_yoy_pct", 25, "NSE_RESULTS_COMPARISON"),
            ("pat_trend", "PROFIT_GROWTH", "DERIVED"),
            ("pat_yoy_pct", 35, "DERIVED"),
            ("pre_result_20d_pct", 4, "NSE_PRICE"),
            ("result_day_return_pct", 5, "NSE_PRICE"),
            ("result_day_rvol", 2, "NSE_PRICE"),
            ("reaction_session", (now_ist().date() - timedelta(days=7)).isoformat(), "DERIVED"),
            ("result_day_low", 95, "NSE_PRICE"),
            ("result_day_high", 98, "NSE_PRICE"),
            ("post_result_hold_5d", True, "NSE_PRICE"),
            ("box_high", 100, "NSE_PRICE"),
            ("box_breakout", False, "NSE_PRICE"),
            ("last_price", 99.5, "NSE_PRICE"),
            ("market_cap_cr", 5000, "YAHOO_FUNDAMENTALS"),
            ("avg_turnover_20d_cr", 10, "NSE_PRICE"),
            ("revenue_cr", 100, "NSE_RESULTS_COMPARISON"),
            ("pat_cr", 15, "NSE_RESULTS_COMPARISON"),
        ):
            store.merge_field(e, field, value, source=source)
        d = score_event(store, e)
        if d.get("entrySignal") != "NEAR_ENTRY":
            failures.append(f"entry watch signal missing: {d.get('entrySignal')}")
        plan = (d.get("plus") or {}).get("plan") or {}
        if plan.get("sl") != 94.05 or plan.get("entry") != 100:
            failures.append(f"plan levels wrong: {plan}")
        if safe_num(d.get("entryTriggerPrice")) is None:
            failures.append("entry trigger price missing")

    # Live-dashboard quarter isolation regression.
    q2_day = date(2026, 10, 7)
    if live_reporting_period(q2_day) != date(2026, 9, 30):
        failures.append("live reporting period should be Q2 FY27 / 2026-09-30")
    q2_event = {"period": {"end": "2026-09-30"}, "fields": {}, "state": "DISCOVERED"}
    q1_event = {"period": {"end": "2026-06-30"}, "fields": {}, "state": "PEAD_SCORED"}
    if not dashboard_activity(q2_event, q2_day):
        failures.append("Q2 FY27 event missing from live dashboard")
    if dashboard_activity(q1_event, q2_day):
        failures.append("Q1 FY27 leaked into Q2 FY27 live dashboard")
    if enrichment_activity(q1_event, q2_day)[0]:
        failures.append("Q1 FY27 leaked into Q2 FY27 hourly enrichment")

    if failures:
        print("SELF TEST FAILED")
        for x in failures:
            print(" -", x)
        return 1
    print("SELF TEST PASSED")
    print("✓ non-null merge")
    print("✓ source precedence")
    print("✓ full-identity event never becomes None")
    print("✓ malformed fetch metadata is repaired")
    print("✓ BSE-only symbols do not invent .NS tickers")
    print("✓ historical quarters are archived from hourly enrichment")
    print("✓ current/near-term events remain active")
    print("✓ PAT turnaround/deterioration logic")
    print("✓ after-hours reaction session")
    print("✓ Q2 FY27 fiscal-quarter mapping")
    print("✓ publish-collapse quality gate")
    print("✓ valuation requires 3 real inputs")
    print("✓ entry watch/trigger signal model")
    print("✓ active dashboard/archive separation")
    print("✓ strict current-quarter dashboard (Q1 archive / Q2 FY27 live)")
    return 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def write_integrity_report(stats: dict[str, Any], signature: str | None, *, dry_run: bool = False) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"integrity_{now_ist().date().isoformat()}{'_dryrun' if dry_run else ''}.json"
    json_dump_atomic(path, {"generatedAt": iso_now(), "signature": signature, "dryRun": dry_run, **stats})
    return path


def print_integrity_summary(stats: dict[str, Any], signature: str | None) -> None:
    print(f"Integrity: merged {stats.get('duplicatesMerged', 0)} duplicate events; checked {stats['checked']} declared events, re-verified {stats['reverified']}, "
          f"promoted {stats.get('promoted', 0)}, revoked {stats['revoked']}, post-result purged {stats['postResultPurged']}, "
          f"snapshots replayed {stats['snapshotsReplayed']}, financial snapshots applied {stats['financialsApplied']}")
    for r in stats.get("revocations", []):
        before = r.get("before") or {}
        print(f"  REVOKE {str(r.get('symbol') or ''):<14} {r['eventId']:<32} "
              f"filed={before.get('filing_timestamp')} reasons={','.join(r.get('reasons') or [])}")
    if signature:
        print(f"Integrity signature: {signature}")


def integrity_report(*, today: date | None = None) -> int:
    """Dry run: apply the integrity pass to a COPY of the event store, print the
    projected live counts and the signature needed to approve the baseline."""
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_events = Path(td) / "events"
        shutil.copytree(EVENTS_DIR, tmp_events)
        store = EventStore(tmp_events)
        before = aggregate_health(store, {}, {})
        stats = integrity_pass(store, today=today)
        score_all(store)
        after = aggregate_health(store, {}, {})
    signature = integrity_signature(stats["revocations"])
    print_integrity_summary(stats, signature)
    published = previous_health()
    print("\nLive-quarter counts       published   store-now   after-repair")
    for key in ("resultsFiled", "financialsParsed", "financialsVerified", "financialsFlagged", "reactionReady", "fullyScored"):
        print(f"  {key:<24}{str(published.get(key, '—')):>10}{str(before.get(key, '—')):>12}{str(after.get(key, '—')):>15}")
    path = write_integrity_report(stats | {"projected": after, "storeBefore": before}, signature, dry_run=True)
    print(f"\nReport: {path.relative_to(ROOT)}")
    if signature:
        print(f"If every revocation above is correct, approve with:  python pead_v2.py --approve-baseline {signature}")
    return 0


def validate_xbrl_file(target: str, period_end: date) -> int:
    """Parse one XBRL instance (local path or URL) and print what was used."""
    if re.match(r"^https?://", target):
        ctx = SourceContext(raw=RawCache(), log=FetchLogger())
        blob, _ = XBRLParser(ctx)._fetch(target, "VALIDATE", "NSE_XBRL" if "nse" in target.lower() else "BSE_XBRL")
    else:
        blob = Path(target).read_bytes()
    docs = XBRLParser(SourceContext(raw=RawCache(), log=FetchLogger()))._documents(blob)
    parsed = parse_xbrl_financials(docs, period_end)
    validation = validate_financial_snapshot(parsed, "NSE_XBRL", period_end)
    print(json.dumps({"parsed": parsed, "validation": validation}, indent=2, default=str))
    return 0 if validation["status"] != "REJECTED" else 3


def validate_financials(limit: int = 30) -> int:
    """Live cross-source check (network): for current-quarter declared events,
    fetch XBRL, NSE comparison and BSE snapshot independently into a COPY of the
    store and report agreement. Nothing is published."""
    import shutil
    import tempfile
    rows = []
    with tempfile.TemporaryDirectory() as td:
        tmp_events = Path(td) / "events"
        shutil.copytree(EVENTS_DIR, tmp_events)
        store = EventStore(tmp_events)
        ctx = SourceContext(raw=RawCache(), log=FetchLogger())
        integrity_pass(store)
        events = [e for e in store.all() if dashboard_activity(e) and boolish(store.value(e, "results_released")) is True][:limit]
        for event in events:
            event["financialSnapshots"] = {}
            try:
                enrich_financials(event, store, ctx)
            except Exception as exc:
                event.setdefault("financialIntegrity", {})["error"] = f"{type(exc).__name__}: {exc}"
            integ = event.get("financialIntegrity") or {}
            for src, snap in (event.get("financialSnapshots") or {}).items():
                v = snap.get("values") or {}
                rows.append({
                    "symbol": event.get("security", {}).get("symbol"), "eventId": event["eventId"], "source": src,
                    "selected": src == integ.get("selectedSource"), "basis": snap.get("basis"),
                    "revenueCr": v.get("revenue_cr"), "patCr": v.get("pat_cr"), "revYoY": v.get("revenue_yoy_pct"),
                    "patTrend": v.get("pat_trend"), "status": (snap.get("validation") or {}).get("status"),
                    "issues": (snap.get("validation") or {}).get("issues"), "concepts": snap.get("concepts"),
                })
            if not event.get("financialSnapshots"):
                rows.append({"symbol": event.get("security", {}).get("symbol"), "eventId": event["eventId"],
                             "source": None, "status": "NO_SNAPSHOT", "fetch": event.get("fetch")})
    path = LOG_DIR / f"financial_validation_{now_ist().date().isoformat()}.json"
    json_dump_atomic(path, {"generatedAt": iso_now(), "rows": rows})
    print(f"{'SYMBOL':<14}{'SOURCE':<24}{'SEL':<5}{'BASIS':<14}{'REV CR':>10}{'PAT CR':>10}{'REV YOY':>9}  STATUS / ISSUES")
    for r in rows:
        print(f"{str(r.get('symbol') or ''):<14}{str(r.get('source') or '—'):<24}{('*' if r.get('selected') else ''):<5}"
              f"{str(r.get('basis') or ''):<14}{str(r.get('revenueCr') if r.get('revenueCr') is not None else '—'):>10}"
              f"{str(r.get('patCr') if r.get('patCr') is not None else '—'):>10}{str(r.get('revYoY') if r.get('revYoY') is not None else '—'):>9}"
              f"  {r.get('status')} {','.join(r.get('issues') or [])}")
    print(f"\nReport: {path.relative_to(ROOT)}")
    return 0


def run(*, skip_network: bool = False, force_publish: bool = False, approved_signature: str | None = None) -> int:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_DIR.mkdir(parents=True, exist_ok=True)

    RUN_GUARD.reset()
    store = EventStore()
    master = SymbolMaster()
    raw = RawCache()
    fetch_log = FetchLogger()
    ctx = SourceContext(raw=raw, log=fetch_log)

    migrated = bootstrap_from_v1(store, master)
    migrated_intel = bootstrap_from_v1_intelligence(store) if migrated else 0
    print(f"Bootstrap migrated {migrated} v1 data rows; intelligence gaps filled on {migrated_intel} events")

    # Pre-discovery integrity pass: deterministic over committed evidence, so
    # its signature is stable between a blocked run and the approving run.
    integrity_stats = integrity_pass(store, master=master)
    signature = integrity_signature(integrity_stats["revocations"])
    print_integrity_summary(integrity_stats, signature)
    write_integrity_report(integrity_stats, signature)
    # Revocations already written to the store by an earlier (blocked) run stay
    # pending until a publish succeeds, so the approval signature is stable even
    # if the event store was persisted between runs.
    pending: dict[str, Any] = {}
    if PENDING_INTEGRITY_PATH.exists():
        try:
            pending = json.loads(PENDING_INTEGRITY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pending = {}
    if pending.get("signature"):
        if signature and signature != pending["signature"]:
            merged = {r["eventId"]: r for r in (pending.get("revocations") or []) + integrity_stats["revocations"]}
            integrity_stats["revocations"] = list(merged.values())
            signature = integrity_signature(integrity_stats["revocations"])
        elif not signature:
            signature = pending["signature"]
            integrity_stats["revocations"] = pending.get("revocations") or []
        integrity_stats["revoked"] = len(integrity_stats["revocations"])

    discovery_stats = {"skipped": True, "errors": []}
    enrichment_stats = {"skipped": True, "errors": []}
    if not skip_network:
        discovery_stats = discover_events(store, master, ctx)
        print("Discovery:", json.dumps(discovery_stats, default=str))
        enrichment_stats = enrich_events(store, master, ctx)
        print("Enrichment:", json.dumps(enrichment_stats, default=str))
        refresh_index_cache(ctx)
        _INDEX_CACHE_MEMO.clear()
        refresh_market_internals(ctx)
        print(f"Live quotes refreshed: {refresh_live_quotes(store, ctx)}")
        post = integrity_pass(store, master=master)
        if post["revoked"] or post["postResultPurged"]:
            print_integrity_summary(post, integrity_signature(post["revocations"]))

    replayed = replay_plus(store)
    print(f"Analytics replayed from saved price files for {replayed} events")
    score_stats = score_all(store)
    print("Scoring:", score_stats)

    health = aggregate_health(store, discovery_stats, enrichment_stats)
    health["bootstrapMigrated"] = migrated
    health["bootstrapIntelligenceFilled"] = migrated_intel
    health["scoreStats"] = score_stats
    health["runGuard"] = RUN_GUARD.summary()
    # Install check (2.6.0): every file must come from the same release.
    plus_v = getattr(pead_plus, "MODULE_VERSION", "missing")
    health["installCheck"] = {"engine": ENGINE_VERSION, "pead_plus": plus_v,
                              "ok": plus_v == ENGINE_VERSION}
    health["integrity"] = {
        "signature": signature,
        "checked": integrity_stats["checked"],
        "reverified": integrity_stats["reverified"],
        # Declaration revocations only; duplicate folds and identity repairs
        # are reported separately (2.5.4) so the health tile is not alarming.
        "revoked": sum(1 for r in integrity_stats["revocations"]
                       if not any(str(x).startswith(("DUPLICATE_MERGED_INTO", "IDENTITY_REPAIRED")) for x in r.get("reasons") or [])),
        "identityRepaired": sum(1 for r in integrity_stats["revocations"]
                                if any(str(x).startswith("IDENTITY_REPAIRED") for x in r.get("reasons") or [])),
        "postResultPurged": integrity_stats["postResultPurged"],
        "financialsApplied": integrity_stats["financialsApplied"],
        # Duplicate folds shrink the row count without losing any company;
        # the quality gate discounts them (2.5.3).
        # Only folds of events that existed before this run (i.e. were in the
        # last published baseline) are added back by the quality gate.
        "duplicatesMerged": int(integrity_stats.get("duplicatesMerged") or 0),
        "declaredMerged": int(integrity_stats.get("declaredMerged") or 0),
    }
    published, reasons = publish(store, health, force=force_publish, approved_signature=approved_signature)
    if published:
        if PENDING_INTEGRITY_PATH.exists():
            PENDING_INTEGRITY_PATH.unlink()
        print("PUBLISHED data.json and intelligence.json")
        print(json.dumps(health, indent=2, default=str))
        return 0
    if signature:
        json_dump_atomic(PENDING_INTEGRITY_PATH, {"signature": signature, "since": iso_now(),
                                                  "revocations": integrity_stats["revocations"]})
    print("PUBLISH BLOCKED — last known good files kept")
    for reason in reasons:
        print(" -", reason)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="PEAD persistent event engine v2")
    parser.add_argument("--self-test", action="store_true", help="run deterministic unit-style checks")
    parser.add_argument("--skip-network", action="store_true", help="bootstrap/score/publish without external fetching")
    parser.add_argument("--force-publish", action="store_true", help="override quality gate (manual recovery only)")
    parser.add_argument("--integrity-report", action="store_true",
                        help="dry run: show which declarations the integrity pass would revoke and the approval signature")
    parser.add_argument("--approve-baseline", metavar="SIGNATURE",
                        help="publish even though declared counts drop, ONLY if the revocation set matches this signature")
    parser.add_argument("--validate-financials", action="store_true",
                        help="network: cross-check XBRL / NSE / BSE financials for live declared events (no publish)")
    parser.add_argument("--validate-xbrl", metavar="PATH_OR_URL", help="parse a single XBRL file/URL and print the result")
    parser.add_argument("--period", metavar="YYYY-MM-DD", help="period end for --validate-xbrl (default: live quarter)")
    parser.add_argument("--limit", type=int, default=30, help="max events for --validate-financials")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.integrity_report:
        return integrity_report()
    if args.validate_xbrl:
        return validate_xbrl_file(args.validate_xbrl, parse_date(args.period) or live_reporting_period())
    if args.validate_financials:
        return validate_financials(args.limit)
    return run(skip_network=args.skip_network, force_publish=args.force_publish, approved_signature=args.approve_baseline)


if __name__ == "__main__":
    raise SystemExit(main())
