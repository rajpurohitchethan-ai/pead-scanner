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
ENGINE_VERSION = "2.1.1"

MIN_MCAP_CR = float(os.getenv("MIN_MCAP_CR", "1000"))
DISCOVERY_LOOKBACK_DAYS = int(os.getenv("DISCOVERY_LOOKBACK_DAYS", "75"))
UPCOMING_DAYS = int(os.getenv("UPCOMING_DAYS", "45"))
EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "550"))
ACTIVE_ENRICH_DAYS = int(os.getenv("ACTIVE_ENRICH_DAYS", "120"))
SOURCE_RETRY_ATTEMPTS = int(os.getenv("SOURCE_RETRY_ATTEMPTS", "3"))
SOURCE_DELAY_SEC = float(os.getenv("SOURCE_DELAY_SEC", "1.25"))

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
    "BSE_RESULT_ANNOUNCEMENT": 98,
    "NSE_RESULTS_COMPARISON": 95,
    "BSE_RESULTS_SNAPSHOT": 92,
    "NSE_PRICE": 95,
    "BSE_PRICE": 95,
    "EXCHANGE_IDENTITY": 95,
    "SCREENER_FALLBACK": 60,
    "YAHOO_QUARTERLY": 55,
    "YAHOO_PRICE": 55,
    "YAHOO_FUNDAMENTALS": 50,
    "V1_MIGRATION": 20,
    "DERIVED": 10,
}

FINANCIAL_FIELDS = {
    "revenue_cr", "pat_cr", "eps", "revenue_yoy_pct", "pat_yoy_pct",
    "revenue_qoq_pct", "pat_qoq_pct", "pat_trend", "basis",
    "prior_year_revenue_cr", "prior_year_pat_cr",
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
        if source in {"NSE_FINANCIAL_RESULTS", "BSE_RESULT_ANNOUNCEMENT"}:
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


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y",
        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S",
        "%b-%y", "%b %Y", "%b-%Y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    if pd is not None:
        try:
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
    else:
        s = str(value).strip()
        dt = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
            "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
        if dt is None and pd is not None:
            try:
                ts = pd.to_datetime(s, errors="coerce", dayfirst=not bool(re.match(r"^\\d{4}-\\d{2}-\\d{2}", s)))
                if not pd.isna(ts):
                    dt = ts.to_pydatetime()
            except Exception:
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
            return d, "INTRADAY"
        return next_trading_day(d), "AFTER_CLOSE"
    if fallback_date is not None:
        # No timestamp = do not pretend the same-day candle is clean. Use the
        # next session and label the timing unknown so the UI can disclose it.
        return next_trading_day(fallback_date), "UNKNOWN_TIME_NEXT_SESSION"
    return None, "UNKNOWN"


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


def json_dump_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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


class EventStore:
    def __init__(self, root: Path = EVENTS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, event_id: str) -> Path:
        return self.root / f"{safe_filename(event_id)}.json"

    def load(self, event_id: str) -> dict[str, Any] | None:
        path = self._path(event_id)
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
        eid = event_id_for(security_key, period_end)
        event = self.load(eid)
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
            return event

        self.merge_security(target, sec)
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
        if old_path.exists():
            old_path.unlink()
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


def with_retry(fn, *, attempts: int = SOURCE_RETRY_ATTEMPTS, base_delay: float = SOURCE_DELAY_SEC):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
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
            payload = with_retry(fn)
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
                source=raw_source, endpoint=endpoint, status="FAILED", event_id=event_id,
                symbol=symbol, error=f"{type(exc).__name__}: {exc}", http_status=_exc_status(exc),
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            raise

    def financial_results(self, from_dt: datetime, to_dt: datetime, symbol: str | None = None, event_id: str | None = None):
        return self._call(
            "financial_results",
            lambda: self.client.financial_results(
                segment="equities", period="quarterly", symbol=symbol,
                from_date=from_dt, to_date=to_dt,
            ) or [],
            event_id=event_id, symbol=symbol,
        )

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

    def history(self, symbol: str, start: date, end: date, event_id: str):
        fn = getattr(self.client, "fetch_equity_historical_data", None)
        if fn is None:
            raise RuntimeError("NSE historical-data method unavailable")
        return self._call(
            "equity_history",
            lambda: fn(symbol=symbol, from_date=start, to_date=end),
            event_id=event_id, symbol=symbol, raw_source="NSE_PRICE",
        )


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
            payload = with_retry(fn)
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
                source=raw_source, endpoint=endpoint, status="FAILED", event_id=event_id,
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

    def price_history(self, code: str, event_id: str, symbol: str | None = None):
        return self._call(
            "price_volume_12m", lambda: self.client.equityPriceVolumeT12M(str(code)),
            event_id=event_id, symbol=symbol, raw_source="BSE_PRICE",
        )


class YahooAdapter:
    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    def _ticker_candidates(self, event: dict[str, Any]) -> list[str]:
        sec = event.get("security", {})
        out = []
        explicit = str(sec.get("yahooTicker") or "").upper().strip()
        if explicit:
            out.append(explicit)
        ns = normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))
        if ns:
            out.append(f"{ns}.NS")
        bc = str(sec.get("bseCode") or "").strip()
        if bc.isdigit():
            out.append(f"{bc}.BO")
        return list(dict.fromkeys(out))

    def history(self, event: dict[str, Any], period: str = "18mo") -> tuple[Any, str | None, str | None]:
        if yf is None:
            return None, None, "yfinance unavailable"
        eid = event["eventId"]
        symbol = event.get("security", {}).get("symbol")
        errors = []
        for ticker in self._ticker_candidates(event):
            started = time.perf_counter()
            try:
                frame = yf.download(
                    ticker, period=period, interval="1d", auto_adjust=False,
                    progress=False, threads=False, timeout=20,
                )
                if frame is None or frame.empty:
                    raise RuntimeError("empty history")
                if pd is not None and isinstance(frame.columns, pd.MultiIndex):
                    try:
                        frame = frame.xs(ticker, axis=1, level=1)
                    except Exception:
                        frame.columns = frame.columns.get_level_values(0)
                self.ctx.log.write(
                    source="YAHOO_PRICE", endpoint="history", status="OK",
                    event_id=eid, symbol=str(symbol or ticker),
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
                return frame, ticker, None
            except Exception as exc:
                errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
                self.ctx.log.write(
                    source="YAHOO_PRICE", endpoint="history", status="FAILED",
                    event_id=eid, symbol=str(symbol or ticker), error=errors[-1],
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
        for ticker in self._ticker_candidates(event):
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
                self.ctx.log.write(
                    source="YAHOO_FUNDAMENTALS", endpoint="info", status="OK",
                    event_id=eid, symbol=str(symbol or ticker),
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    extra={"ticker": ticker},
                )
                return out, ticker, None
            except Exception as exc:
                errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
                self.ctx.log.write(
                    source="YAHOO_FUNDAMENTALS", endpoint="info", status="FAILED",
                    event_id=eid, symbol=str(symbol or ticker), error=errors[-1],
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
        for ticker in self._ticker_candidates(event):
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
                if abs((current_date - period_end).days) > 45:
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
                    "pat_qoq_pct": pct_change(current_pat, prev_pat) if prev_pat not in (None, 0) and prev_pat > 0 else None,
                    "basis": "UNKNOWN",
                    "ticker": ticker,
                    "statement_period": current_date.isoformat(),
                }
                return out, None
            except Exception as exc:
                errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
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
            response = with_retry(lambda: sess.get(url, timeout=25))
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
                source=source, endpoint="xbrl", status="FAILED", event_id=event_id,
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
        facts = []
        contexts: dict[str, dict[str, Any]] = {}
        for doc in self._documents(blob):
            try:
                root = ET.fromstring(doc)
            except Exception:
                continue
            for elem in root.iter():
                local = self._local(elem.tag).lower()
                if local == "context":
                    cid = elem.attrib.get("id")
                    if not cid:
                        continue
                    text = " ".join((x.text or "") for x in elem.iter())
                    starts = [parse_date(x.text) for x in elem.iter() if self._local(x.tag).lower() == "startdate"]
                    ends = [parse_date(x.text) for x in elem.iter() if self._local(x.tag).lower() in {"enddate", "instant"}]
                    contexts[cid] = {
                        "start": next((x for x in starts if x), None),
                        "end": next((x for x in ends if x), None),
                        "text": text.lower(),
                    }
                    continue
                cref = elem.attrib.get("contextRef") or elem.attrib.get("contextref")
                if not cref:
                    continue
                scale = 0
                try:
                    scale = int(elem.attrib.get("scale") or 0)
                except Exception:
                    scale = 0
                value = self._num_text(elem.text, scale)
                if value is None:
                    continue
                facts.append({
                    "name": local,
                    "context": cref,
                    "value": value,
                    "unit": elem.attrib.get("unitRef") or elem.attrib.get("unitref"),
                })

        def candidates(names: list[str], target_end: date):
            scored = []
            for fact in facts:
                name = fact["name"]
                name_score = None
                for idx, wanted in enumerate(names):
                    if wanted == name:
                        name_score = 30 - idx
                        break
                    if wanted in name:
                        name_score = 15 - idx
                        break
                if name_score is None:
                    continue
                ctx = contexts.get(fact["context"], {})
                end = ctx.get("end")
                start = ctx.get("start")
                if end is None or abs((end - target_end).days) > 8:
                    continue
                duration_score = 0
                if start is not None:
                    days = (end - start).days
                    if 75 <= days <= 105:
                        duration_score = 20
                    elif 160 <= days <= 200:
                        duration_score = 4
                    elif days > 250:
                        duration_score = -10
                text = ctx.get("text", "")
                basis_score = 4 if "consolidated" in text else (-2 if "standalone" in text else 0)
                scored.append((name_score + duration_score + basis_score, fact, ctx))
            return sorted(scored, key=lambda x: x[0], reverse=True)

        def choose(names: list[str], target_end: date):
            rows = candidates(names, target_end)
            return rows[0] if rows else None

        current_rev = choose(self.REVENUE_NAMES, period_end)
        current_pat = choose(self.PAT_NAMES, period_end)
        current_eps = choose(self.EPS_NAMES, period_end)
        prior_end = date(period_end.year - 1, period_end.month, period_end.day)
        prior_rev = choose(self.REVENUE_NAMES, prior_end)
        prior_pat = choose(self.PAT_NAMES, prior_end)

        def crore(row):
            if not row:
                return None
            value = row[1]["value"]
            unit = str(row[1].get("unit") or "").upper()
            if "INR" in unit or abs(value) > 1e6:
                return value / 1e7
            return value

        rev_cr = crore(current_rev)
        pat_cr = crore(current_pat)
        prior_rev_cr = crore(prior_rev)
        prior_pat_cr = crore(prior_pat)
        trend, pat_yoy = pat_trend(pat_cr, prior_pat_cr)
        basis_text = ""
        for row in (current_rev, current_pat):
            if row:
                basis_text += " " + str(row[2].get("text") or "")
        basis = "CONSOLIDATED" if "consolidated" in basis_text else ("STANDALONE" if "standalone" in basis_text else "UNKNOWN")

        return {
            "revenue_cr": round2(rev_cr),
            "pat_cr": round2(pat_cr),
            "eps": round2(current_eps[1]["value"]) if current_eps else None,
            "prior_year_revenue_cr": round2(prior_rev_cr),
            "prior_year_pat_cr": round2(prior_pat_cr),
            "revenue_yoy_pct": round2(pct_change(rev_cr, prior_rev_cr)),
            "pat_yoy_pct": round2(pat_yoy),
            "pat_trend": trend,
            "basis": basis,
        }

    def fetch_parse(self, url: str, event_id: str, period_end: date, source: str) -> tuple[dict[str, Any], str]:
        blob, raw_ref = self._fetch(url, event_id, source)
        return self.parse(blob, period_end), raw_ref


# ---------------------------------------------------------------------------
# Discovery normalization
# ---------------------------------------------------------------------------


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
    if period_end is None:
        period_end = expected_period_end(filing_ts.date() if filing_ts else None)
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
    period_end = expected_period_end(event_ts.date())
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


def apply_discovery(store: EventStore, master: SymbolMaster, candidate: dict[str, Any], raw_ref: str | None = None) -> dict[str, Any]:
    security = candidate["security"]
    master.merge(security)
    event = store.ensure_event(security=security, period_end=candidate.get("periodEnd"), quarter=candidate.get("quarter"))
    source = candidate["source"]
    if candidate.get("released"):
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
        if candidate.get("resultDate"):
            store.merge_field(event, "result_date", candidate["resultDate"].isoformat(), source=source, raw_ref=raw_ref)
        if boolish(store.value(event, "results_released")) is not True:
            store.set_state(event, "SCHEDULED", "board meeting/result calendar discovered")
    if candidate.get("xbrlUrl"):
        store.merge_field(event, "xbrl_url", candidate["xbrlUrl"], source=source, raw_ref=raw_ref)
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


def parse_nse_comparison(payload: dict[str, Any], period_end: date) -> dict[str, Any]:
    records = []
    if isinstance(payload, dict):
        records = payload.get("resCmpData") or payload.get("data") or []
    if not isinstance(records, list):
        return {}
    rows = [(record_period_end(r), r) for r in records if isinstance(r, dict)]
    rows = [(d, r) for d, r in rows if d is not None]
    if not rows:
        return {}
    current = min(rows, key=lambda x: abs((x[0] - period_end).days))
    if abs((current[0] - period_end).days) > 45:
        return {}

    def find_near(target: date, max_days: int = 50):
        choices = [(abs((d - target).days), r) for d, r in rows if d != current[0]]
        if not choices:
            return None
        dist, rec = min(choices, key=lambda x: x[0])
        return rec if dist <= max_days else None

    current_rec = current[1]
    prev_candidates = sorted([(d, r) for d, r in rows if d < current[0]], reverse=True)
    prev_rec = prev_candidates[0][1] if prev_candidates else None
    prior_rec = find_near(date(current[0].year - 1, current[0].month, min(current[0].day, 28)))

    revenue_aliases = ("re_total_inc", "total_income", "totalincome", "revenue", "revenue_from_operations")
    pat_aliases = ("re_net_profit", "net_profit", "netprofit", "profit_after_tax", "pat")
    eps_aliases = ("re_basic_eps", "basic_eps", "eps")

    revenue_lakh = metric(current_rec, *revenue_aliases)
    pat_lakh = metric(current_rec, *pat_aliases)
    eps = metric(current_rec, *eps_aliases)
    prev_revenue_lakh = metric(prev_rec or {}, *revenue_aliases)
    prev_pat_lakh = metric(prev_rec or {}, *pat_aliases)
    prior_revenue_lakh = metric(prior_rec or {}, *revenue_aliases)
    prior_pat_lakh = metric(prior_rec or {}, *pat_aliases)

    revenue_cr = revenue_lakh / 100 if revenue_lakh is not None else None
    pat_cr = pat_lakh / 100 if pat_lakh is not None else None
    prior_revenue_cr = prior_revenue_lakh / 100 if prior_revenue_lakh is not None else None
    prior_pat_cr = prior_pat_lakh / 100 if prior_pat_lakh is not None else None
    trend, pat_yoy = pat_trend(pat_cr, prior_pat_cr)

    return {
        "revenue_cr": round2(revenue_cr),
        "pat_cr": round2(pat_cr),
        "eps": round2(eps),
        "prior_year_revenue_cr": round2(prior_revenue_cr),
        "prior_year_pat_cr": round2(prior_pat_cr),
        "revenue_yoy_pct": round2(pct_change(revenue_cr, prior_revenue_cr)),
        "pat_yoy_pct": round2(pat_yoy),
        "pat_trend": trend,
        "revenue_qoq_pct": round2(pct_change(revenue_lakh, prev_revenue_lakh)),
        "pat_qoq_pct": round2(pct_change(pat_lakh, prev_pat_lakh)) if prev_pat_lakh not in (None, 0) and prev_pat_lakh > 0 else None,
        "basis": "CONSOLIDATED" if any("consolid" in str(v).lower() for v in current_rec.values()) else "UNKNOWN",
    }


def parse_bse_snapshot(snapshot: dict[str, Any], period_end: date) -> dict[str, Any]:
    block = snapshot.get("results_in_crores") if isinstance(snapshot, dict) else None
    if not isinstance(block, dict):
        return {}
    fields = block.get("fields") or []
    data = block.get("data") or []
    if len(fields) < 2 or not data:
        return {}
    periods = [str(x).strip() for x in fields[1:]]

    def norm_title(x: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()

    table: dict[str, list[Any]] = {}
    for row in data:
        if not isinstance(row, list) or not row:
            continue
        table[norm_title(str(row[0]))] = row[1:]

    def row_values(*names: str):
        for title, values in table.items():
            if any(norm_title(name) == title or norm_title(name) in title for name in names):
                return values
        return []

    def val(values, idx):
        return safe_num(values[idx]) if idx is not None and 0 <= idx < len(values) else None

    period_dates = [parse_date(p) for p in periods]
    current_idx = None
    for i, d in enumerate(period_dates):
        if d and d.year == period_end.year and d.month == period_end.month:
            current_idx = i
            break
    if current_idx is None:
        current_idx = 0
        latest = period_dates[0] if period_dates else None
        if latest is not None and abs((latest - period_end).days) > 45:
            return {}

    prior_idx = None
    for i, d in enumerate(period_dates):
        if d and d.year == period_end.year - 1 and d.month == period_end.month:
            prior_idx = i
            break
    prev_idx = current_idx + 1 if current_idx + 1 < len(periods) else None

    revs = row_values("Revenue", "Total Income", "Net Sales")
    pats = row_values("Net Profit", "PAT", "Profit After Tax")
    epss = row_values("EPS")
    revenue_cr = val(revs, current_idx)
    pat_cr = val(pats, current_idx)
    prior_revenue_cr = val(revs, prior_idx)
    prior_pat_cr = val(pats, prior_idx)
    prev_revenue_cr = val(revs, prev_idx)
    prev_pat_cr = val(pats, prev_idx)
    trend, pat_yoy = pat_trend(pat_cr, prior_pat_cr)

    return {
        "revenue_cr": round2(revenue_cr),
        "pat_cr": round2(pat_cr),
        "eps": round2(val(epss, current_idx)),
        "prior_year_revenue_cr": round2(prior_revenue_cr),
        "prior_year_pat_cr": round2(prior_pat_cr),
        "revenue_yoy_pct": round2(pct_change(revenue_cr, prior_revenue_cr)),
        "pat_yoy_pct": round2(pat_yoy),
        "pat_trend": trend,
        "revenue_qoq_pct": round2(pct_change(revenue_cr, prev_revenue_cr)),
        "pat_qoq_pct": round2(pct_change(pat_cr, prev_pat_cr)) if prev_pat_cr not in (None, 0) and prev_pat_cr > 0 else None,
        "basis": "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# Price normalization and reaction metrics
# ---------------------------------------------------------------------------


def nse_history_to_frame(records: Any):
    if pd is None or not isinstance(records, list) or not records:
        return None
    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        d = parse_date(first(r, "mTIMESTAMP", "CH_TIMESTAMP", "date", "Date"))
        if d is None:
            continue
        rows.append({
            "Date": pd.Timestamp(d),
            "Open": safe_num(first(r, "CH_OPENING_PRICE", "open", "Open")),
            "High": safe_num(first(r, "CH_TRADE_HIGH_PRICE", "high", "High")),
            "Low": safe_num(first(r, "CH_TRADE_LOW_PRICE", "low", "Low")),
            "Close": safe_num(first(r, "CH_CLOSING_PRICE", "close", "Close")),
            "Volume": safe_num(first(r, "CH_TOT_TRADED_QTY", "volume", "Volume")),
        })
    if not rows:
        return None
    return pd.DataFrame(rows).dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)


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
    return pd.DataFrame(out).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)


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


def price_metrics(frame: Any, reaction_date: date | None) -> dict[str, Any]:
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
    pre_end = reaction_idx if reaction_idx is not None else len(f)

    def pre_move(n: int):
        if pre_end < n + 1:
            return None
        return pct_idx(pre_end - n - 1, pre_end - 1)

    close = pd.to_numeric(f["Close"], errors="coerce")
    latest = safe_num(close.iloc[-1])
    high_52 = safe_num(close.tail(252).max()) if len(close) else None
    out = {
        "pre_result_5d_pct": pre_move(5),
        "pre_result_10d_pct": pre_move(10),
        "pre_result_20d_pct": pre_move(20),
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
            out["avg_turnover_20d_cr"] = round2(turnover.tail(20).mean())

    if reaction_idx is not None and reaction_idx < len(f):
        if reaction_idx > 0:
            out["result_day_return_pct"] = pct_idx(reaction_idx - 1, reaction_idx)
        result_low = safe_num(f.loc[reaction_idx, "Low"])
        result_high = safe_num(f.loc[reaction_idx, "High"])
        if result_low is None:
            result_low = safe_num(f.loc[reaction_idx, "Close"])
        if result_high is None:
            result_high = safe_num(f.loc[reaction_idx, "Close"])
        out["result_day_low"] = round2(result_low)
        out["result_day_high"] = round2(result_high)

        if "Volume" in f.columns:
            rv = safe_num(f.loc[reaction_idx, "Volume"])
            prior = pd.to_numeric(f.loc[max(0, reaction_idx - 20):reaction_idx - 1, "Volume"], errors="coerce").dropna()
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
        box = f.iloc[reaction_idx:min(len(f), reaction_idx + 6)]
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
# Event enrichment
# ---------------------------------------------------------------------------


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
                if isinstance(rows, list) and rows:
                    rec = rows[0]
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


def merge_financials(store: EventStore, event: dict[str, Any], data: dict[str, Any], source: str, raw_ref: str | None = None) -> int:
    changed = 0
    for field in FINANCIAL_FIELDS:
        if field in data and data[field] not in (None, ""):
            changed += int(store.merge_field(event, field, data[field], source=source, raw_ref=raw_ref))
    return changed


def enrich_financials(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    if boolish(store.value(event, "results_released")) is not True:
        return
    period_end = parse_date(event.get("period", {}).get("end"))
    if period_end is None:
        return
    sec = event.get("security", {})
    symbol = normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))
    bse_code = str(sec.get("bseCode") or "").strip()
    xbrl_url = store.value(event, "xbrl_url")

    # 1) XBRL: highest authority when present.
    if xbrl_url:
        source = "NSE_XBRL" if "nse" in str(xbrl_url).lower() or sec.get("nseSymbol") else "BSE_XBRL"
        try:
            parsed, raw_ref = XBRLParser(ctx).fetch_parse(str(xbrl_url), event["eventId"], period_end, source)
            if any(parsed.get(k) is not None for k in ("revenue_cr", "pat_cr", "revenue_yoy_pct", "pat_trend")):
                merge_financials(store, event, parsed, source, raw_ref)
                store.record_fetch(event, source, ok=True, raw_ref=raw_ref)
            else:
                store.record_fetch(event, source, ok=False, error="XBRL parsed but core financial facts were not resolved", raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, source, ok=False, error=f"{type(exc).__name__}: {exc}")

    # 2) NSE results comparison — excellent compact 5-quarter series.
    needs_core = any(store.value(event, k) is None for k in ("revenue_cr", "pat_cr", "prior_year_revenue_cr", "prior_year_pat_cr"))
    if symbol and needs_core:
        try:
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.results_comparison(symbol, event["eventId"])
                parsed = parse_nse_comparison(payload or {}, period_end)
                if parsed:
                    merge_financials(store, event, parsed, "NSE_RESULTS_COMPARISON", raw_ref)
                    store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=True, raw_ref=raw_ref)
                else:
                    store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=False, error="comparison payload did not match event quarter", raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "NSE_RESULTS_COMPARISON", ok=False, error=f"{type(exc).__name__}: {exc}")

    # 3) BSE snapshot — critical for BSE-only companies.
    needs_core = any(store.value(event, k) is None for k in ("revenue_cr", "pat_cr"))
    if bse_code and needs_core:
        try:
            with BSEAdapter(ctx) as bse:
                payload, raw_ref = bse.results_snapshot(bse_code, event["eventId"], symbol)
                parsed = parse_bse_snapshot(payload or {}, period_end)
                if parsed:
                    merge_financials(store, event, parsed, "BSE_RESULTS_SNAPSHOT", raw_ref)
                    store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=True, raw_ref=raw_ref)
                else:
                    store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=False, error="snapshot did not match event quarter", raw_ref=raw_ref)
        except Exception as exc:
            store.record_fetch(event, "BSE_RESULTS_SNAPSHOT", ok=False, error=f"{type(exc).__name__}: {exc}")

    # 4) Yahoo quarterly only fills remaining gaps. It cannot downgrade exchange values.
    needs_core = any(store.value(event, k) is None for k in ("revenue_cr", "pat_cr", "prior_year_revenue_cr", "prior_year_pat_cr"))
    if needs_core:
        data, err = YahooAdapter(ctx).quarterly(event)
        if data:
            merge_financials(store, event, data, "YAHOO_QUARTERLY")
            store.record_fetch(event, "YAHOO_QUARTERLY", ok=True)
        elif err:
            store.record_fetch(event, "YAHOO_QUARTERLY", ok=False, error=err)

    if store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
        # Recompute PAT trend from stored current/prior if prior exists, ensuring
        # legacy negative-base percentages cannot survive as the primary label.
        current_pat = safe_num(store.value(event, "pat_cr"))
        prior_pat = safe_num(store.value(event, "prior_year_pat_cr"))
        trend, yoy = pat_trend(current_pat, prior_pat)
        if trend:
            store.merge_field(event, "pat_trend", trend, source="DERIVED", note="negative/zero-base safe PAT trend")
        if yoy is not None:
            store.merge_field(event, "pat_yoy_pct", round2(yoy), source="DERIVED", note="computed only on positive prior PAT")
        store.set_state(event, "FINANCIALS_PARSED", "current-quarter revenue/PAT available")


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


def enrich_price(event: dict[str, Any], store: EventStore, ctx: SourceContext) -> None:
    # Price history is expensive. Refresh active events at most every 4h before
    # reaction completion and every 12h afterwards.
    pf = (event.get("fetch") or {}).get("PRICE_HISTORY") or {}
    last_success = parse_datetime(pf.get("lastSuccess"))
    if last_success:
        age_hours = (now_ist() - last_success).total_seconds() / 3600
        threshold = 12 if STATE_ORDER.get(event.get("state", "DISCOVERED"), 0) >= STATE_ORDER["REACTION_READY"] else 4
        if age_hours < threshold:
            return
    sec = event.get("security", {})
    filing_ts = parse_datetime(store.value(event, "filing_timestamp"))
    result_date = parse_date(store.value(event, "result_date"))
    reaction_date, timing = reaction_session(filing_ts, result_date)
    if reaction_date:
        store.merge_field(event, "reaction_session", reaction_date.isoformat(), source="DERIVED", note=timing)
        store.merge_field(event, "filing_session", timing, source="DERIVED")

    # Upcoming events still benefit from pre-result run-up calculations; use the
    # announced result date as the expected reaction boundary.
    price_boundary = reaction_date or result_date
    frame = None
    price_source = None
    raw_ref = None
    errors = []

    symbol = normalize_symbol(sec.get("nseSymbol") or sec.get("symbol"))
    if symbol:
        try:
            start = (price_boundary or now_ist().date()) - timedelta(days=420)
            end = now_ist().date()
            with NSEAdapter(ctx) as nse:
                payload, raw_ref = nse.history(symbol, start, end, event["eventId"])
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

    metrics = price_metrics(frame, price_boundary)
    for field, value in metrics.items():
        if value is not None:
            store.merge_field(event, field, value, source=price_source or "YAHOO_PRICE", raw_ref=raw_ref)
    store.record_fetch(event, "PRICE_HISTORY", ok=True, raw_ref=raw_ref)

    if boolish(store.value(event, "results_released")) is True:
        if reaction_date and now_ist().date() < reaction_date:
            store.set_state(event, "REACTION_PENDING", "reaction trading session has not occurred yet")
        elif store.value(event, "result_day_return_pct") is not None or store.value(event, "result_day_rvol") is not None:
            store.set_state(event, "REACTION_READY", "post-result price/volume reaction available")


def should_enrich(event: dict[str, Any]) -> bool:
    result_date = parse_date(meta_value(event, "result_date"))
    period_end = parse_date(event.get("period", {}).get("end"))
    today = now_ist().date()

    # Upcoming/current-quarter events always stay active.
    anchor = result_date or period_end
    if anchor is None:
        return True
    age = (today - anchor).days
    if age < -UPCOMING_DAYS:
        return False
    if age <= ACTIVE_ENRICH_DAYS:
        return True

    # Old events remain on disk forever, but stop consuming network once scored.
    if STATE_ORDER.get(event.get("state", "DISCOVERED"), 0) >= STATE_ORDER["PEAD_SCORED"]:
        return False
    return age <= EVENT_RETENTION_DAYS


def enrich_events(store: EventStore, master: SymbolMaster, ctx: SourceContext) -> dict[str, Any]:
    stats = {"events": 0, "filed": 0, "financials": 0, "reactionReady": 0, "errors": []}
    for event in store.all():
        if not should_enrich(event):
            continue
        stats["events"] += 1
        original_event = event
        original_event_id = str(event.get("eventId") or "unknown-event")
        try:
            identified = fill_security_identity(event, store, master, ctx)
            if not isinstance(identified, dict):
                raise RuntimeError(
                    f"fill_security_identity returned {type(identified).__name__}; "
                    "expected event dictionary"
                )
            event = identified
            enrich_valuation(event, store, master, ctx)
            if boolish(store.value(event, "results_released")) is True:
                stats["filed"] += 1
                enrich_financials(event, store, ctx)
                if store.value(event, "revenue_cr") is not None and store.value(event, "pat_cr") is not None:
                    stats["financials"] += 1
            enrich_price(event, store, ctx)
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


def score_event(store: EventStore, event: dict[str, Any]) -> dict[str, Any]:
    released = boolish(store.value(event, "results_released")) is True
    rev_yoy = safe_num(store.value(event, "revenue_yoy_pct"))
    pat_yoy = safe_num(store.value(event, "pat_yoy_pct"))
    trend = str(store.value(event, "pat_trend") or "").upper() or None
    pre20 = safe_num(store.value(event, "pre_result_20d_pct"))
    result_ret = safe_num(store.value(event, "result_day_return_pct"))
    rvol = safe_num(store.value(event, "result_day_rvol"))
    hold5 = boolish(store.value(event, "post_result_hold_5d"))
    hold10 = boolish(store.value(event, "post_result_hold_10d"))
    breakout = boolish(store.value(event, "box_breakout"))
    mcap = safe_num(store.value(event, "market_cap_cr"))
    turnover = safe_num(store.value(event, "avg_turnover_20d_cr"))
    trailing_pe = safe_num(store.value(event, "trailing_pe"))
    forward_pe = safe_num(store.value(event, "forward_pe"))
    peg = safe_num(store.value(event, "peg"))
    roe = safe_num(store.value(event, "roe_pct"))
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

    # Expectations (max ~15)
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
        else:
            score -= 4
            risks.append(f"20D turnover ₹{turnover:.1f} Cr is thin.")
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

    if trailing_pe is None and forward_pe is None and peg is None:
        valuation_label = "UNVERIFIED"
    elif peg is not None and peg <= 1.2:
        valuation_label = "ATTRACTIVE"
    elif trailing_pe is not None and trailing_pe > 80:
        valuation_label = "EXCESSIVE"
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
        and expectation_label != "PRICED IN"
        and price_label == "CONFIRMED"
        and score >= 70
        and completeness >= 75
        and (mcap is not None and mcap >= MIN_MCAP_CR)
    )
    entry_watch = high and (hold5 is True or breakout is True)
    if entry_watch:
        store.set_state(event, "ENTRY_WATCH", "high-conviction PEAD with post-result acceptance")

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
        "reasons": reasons[:8],
        "risks": risks[:8],
    }
    event["derived"] = derived
    return derived


def score_all(store: EventStore) -> dict[str, Any]:
    stats = {"scored": 0, "highConviction": 0}
    for event in store.all():
        try:
            d = score_event(store, event)
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
    entry_trigger = bool(d.get("entryWatch")) if released else None
    box_high = safe_num(meta_value(event, "box_high"))
    mechanical_entry = round2(box_high * 1.002) if entry_trigger and box_high is not None else None
    mechanical_sl = round2(meta_value(event, "result_day_low"))

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
        "sector": sec.get("sector") or sec.get("industry") or "—",
        "industry": sec.get("industry"),
        "isin": sec.get("isin"),
        "bseCode": sec.get("bseCode"),
        "ticker": sec.get("yahooTicker"),
        "quarter": period.get("quarter") or fiscal_quarter(parse_date(period.get("end"))),
        "earningsPeriod": period.get("quarter") or fiscal_quarter(parse_date(period.get("end"))),
        "resultPeriodEnd": period.get("end"),
        "resultDate": result_date,
        "resultsReleased": released,
        "resultReleased": released,
        "peadStatus": event.get("state"),
        "bucket": "Post-results" if released else "Upcoming",
        "eventState": event.get("state"),
        "dataCompletenessPct": d.get("completenessPct"),
        "marketCapCr": round2(meta_value(event, "market_cap_cr")),
        "marketCapPass": (safe_num(meta_value(event, "market_cap_cr")) >= MIN_MCAP_CR) if safe_num(meta_value(event, "market_cap_cr")) is not None else None,
        "price": round2(meta_value(event, "last_price")),
        "priceTimestamp": event.get("updatedAt"),
        "changePct": None,
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
        "trailingPE": round2(meta_value(event, "trailing_pe")),
        "forwardPE": round2(meta_value(event, "forward_pe")),
        "peg": round2(meta_value(event, "peg")),
        "roePct": round2(meta_value(event, "roe_pct")),
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
        "sectorTailwind": None,
        "pricedIn": priced_in,
        "candidateStatus": event.get("state"),
        "allocationPct": allocation,
        "entryTriggerPass": entry_trigger,
        "entry": mechanical_entry,
        "sl": mechanical_sl,
        "tsl": None,
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
        "resultSourceUrl": meta_value(event, "xbrl_url"),
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
        "eventState": row["eventState"],
        "dataCompletenessPct": row["dataCompletenessPct"],
        "baseScoreText": row["scoreText"],
        "convictionScore": row["convictionScore"] or 0,
        "verdict": row["verdict"],
        "resultReality": {"label": row["resultReality"], "reasons": rr_reasons, "risks": rr_risks},
        "expectationReality": {"label": row["expectationReality"], "reasons": [], "risks": []},
        "valuationReality": {
            "label": row["valuationReality"],
            "confidence": "LIMITED" if all(row.get(k) is None for k in ("trailingPE", "forwardPE", "peg", "roePct")) else "NORMAL",
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
        "entry": None,
        "sl": row["resultDayLow"],
        "tsl": None,
        "revenueYoY": row["revenueYoY"],
        "patYoY": row["patYoY"],
        "patYoYTurnaround": row["patYoYTurnaround"],
        "patYoYStatus": row["patYoYStatus"],
        "patQoQ": row["patQoQ"],
        "reactionSession": row["reactionSession"],
        "filingSession": row["filingSession"],
        "darvasBoxHigh": row["darvasBoxHigh"],
        "darvasBreakout": row["darvasBreakout"],
        "holdAboveResultLow5d": row["holdAboveResultLow5d"],
        "holdAboveResultLow10d": row["holdAboveResultLow10d"],
        "reasons": d.get("reasons") or [],
        "risks": d.get("risks") or [],
        "managementCommentary": None,
        "commentaryVerified": False,
    }


def aggregate_health(store: EventStore, discovery_stats: dict[str, Any], enrichment_stats: dict[str, Any]) -> dict[str, Any]:
    events = store.all()
    rows = [event_to_data_row(e) for e in events]
    declared = [r for r in rows if r.get("resultsReleased") is True]
    financial = [r for r in declared if r.get("latestRevenueCr") is not None and r.get("latestPatCr") is not None]
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

    completeness_avg = round(sum((r.get("dataCompletenessPct") or 0) for r in declared) / len(declared), 1) if declared else 0.0
    return {
        "generatedAt": iso_now(),
        "engineVersion": ENGINE_VERSION,
        "eventsTracked": len(rows),
        "resultsFiled": len(declared),
        "financialsParsed": len(financial),
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
    for path in (HEALTH_PATH, INTELLIGENCE_PATH, DATA_PATH):
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


def quality_gate(new_health: dict[str, Any], old_health: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    current_rows = int(new_health.get("eventsTracked") or 0)
    current_declared = int(new_health.get("resultsFiled") or 0)
    old_rows = int(old_health.get("eventsTracked") or 0)
    old_declared = int(old_health.get("resultsFiled") or 0)
    current_comp = float(new_health.get("declaredCompletenessPct") or 0)
    old_comp = float(old_health.get("declaredCompletenessPct") or 0)

    if current_rows == 0:
        reasons.append("event store contains zero rows")
    if old_rows >= 10 and current_rows < old_rows * 0.80:
        reasons.append(f"event count dropped from {old_rows} to {current_rows} (>20%)")
    if old_declared >= 5 and current_declared < old_declared * 0.80:
        reasons.append(f"declared-result count dropped from {old_declared} to {current_declared} (>20%)")
    if old_declared >= 5 and old_comp >= 40 and current_comp < max(20, old_comp - 25):
        reasons.append(f"declared completeness collapsed from {old_comp:.1f}% to {current_comp:.1f}%")
    return len(reasons) == 0, reasons


def publish(store: EventStore, health: dict[str, Any], *, force: bool = False) -> tuple[bool, list[str]]:
    old = previous_health()
    ok, reasons = quality_gate(health, old)
    health["qualityGate"] = {"passed": ok, "reasons": reasons, "previous": old}
    json_dump_atomic(HEALTH_PATH, health)
    if not ok and not force:
        return False, reasons

    events = sorted(
        store.all(),
        key=lambda e: (
            parse_date(e.get("period", {}).get("end")) or date.min,
            str(e.get("security", {}).get("symbol") or ""),
        ),
        reverse=True,
    )
    rows = [event_to_data_row(e) for e in events]
    items = [event_to_intelligence(e) for e in events]

    data_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "scannerMode": "persistent-event-store-v2",
        "qualificationVersion": f"pead-core-v2-{ENGINE_VERSION}",
        "minMarketCapCr": MIN_MCAP_CR,
        "health": health,
        "stocks": rows,
    }
    counts = {
        "total": len(items),
        "resultsDeclared": sum(x.get("resultsReleased") is True for x in items),
        "genuineResults": sum(x.get("resultReality", {}).get("label") == "GENUINE" for x in items),
        "pricedIn": sum(x.get("expectationReality", {}).get("label") == "PRICED IN" for x in items),
        "highConviction": sum(x.get("verdict") == "HIGH-CONVICTION PEAD CANDIDATE" for x in items),
        "dataPending": sum(x.get("verdict") == "DATA PENDING — RESULT VERIFIED" for x in items),
        "entryWatch": sum(x.get("eventState") == "ENTRY_WATCH" for x in items),
    }
    intel_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_now(),
        "version": f"PEAD Intelligence v2 {ENGINE_VERSION}",
        "health": health,
        "counts": counts,
        "items": items,
    }
    json_dump_atomic(DATA_PATH, data_payload)
    json_dump_atomic(INTELLIGENCE_PATH, intel_payload)
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
    print("✓ PAT turnaround/deterioration logic")
    print("✓ after-hours reaction session")
    print("✓ Q2 FY27 fiscal-quarter mapping")
    print("✓ publish-collapse quality gate")
    return 0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(*, skip_network: bool = False, force_publish: bool = False) -> int:
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_DIR.mkdir(parents=True, exist_ok=True)

    store = EventStore()
    master = SymbolMaster()
    raw = RawCache()
    fetch_log = FetchLogger()
    ctx = SourceContext(raw=raw, log=fetch_log)

    migrated = bootstrap_from_v1(store, master)
    migrated_intel = bootstrap_from_v1_intelligence(store) if migrated else 0
    print(f"Bootstrap migrated {migrated} v1 data rows; intelligence gaps filled on {migrated_intel} events")

    discovery_stats = {"skipped": True, "errors": []}
    enrichment_stats = {"skipped": True, "errors": []}
    if not skip_network:
        discovery_stats = discover_events(store, master, ctx)
        print("Discovery:", json.dumps(discovery_stats, default=str))
        enrichment_stats = enrich_events(store, master, ctx)
        print("Enrichment:", json.dumps(enrichment_stats, default=str))

    score_stats = score_all(store)
    print("Scoring:", score_stats)

    health = aggregate_health(store, discovery_stats, enrichment_stats)
    health["bootstrapMigrated"] = migrated
    health["bootstrapIntelligenceFilled"] = migrated_intel
    health["scoreStats"] = score_stats
    published, reasons = publish(store, health, force=force_publish)
    if published:
        print("PUBLISHED data.json and intelligence.json")
        print(json.dumps(health, indent=2, default=str))
        return 0
    print("PUBLISH BLOCKED — last known good files kept")
    for reason in reasons:
        print(" -", reason)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="PEAD persistent event engine v2")
    parser.add_argument("--self-test", action="store_true", help="run deterministic unit-style checks")
    parser.add_argument("--skip-network", action="store_true", help="bootstrap/score/publish without external fetching")
    parser.add_argument("--force-publish", action="store_true", help="override quality gate (manual recovery only)")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    return run(skip_network=args.skip_network, force_publish=args.force_publish)


if __name__ == "__main__":
    raise SystemExit(main())
