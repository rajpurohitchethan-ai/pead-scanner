#!/usr/bin/env python3
"""US PEAD scanner (S&P 1500). Separate from the Indian engine: its own data
(us/), its own workflow (us_scan.yml) and its own page (us/index.html).

Sources (probed from GitHub's runners on 12 Nov 2026):
- Universe: Wikipedia's S&P 500 / 400 / 600 lists (symbol, name, GICS sector).
- Results: Nasdaq's earnings calendar (date, EPS, consensus, surprise, market cap).
- Prices and quarterly revenue / net income: Yahoo (yfinance).
SEC EDGAR refuses GitHub's runners (HTTP 403), so it is not used.

The trade rules are the Indian page's own, imported from pead_plus: earnings
strength, the plan (entry, stop 1% under the result-day low), the Radar grade
and the strong-hands check. Missing values stay null; nothing is estimated.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
import pead_plus  # noqa: E402  (the shared rules)

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None
try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

US_VERSION = "us-0.1.0"
NY = ZoneInfo("America/New_York")
WINDOW_DAYS = 75                 # results of the last 75 days (one season)
MIN_MCAP_USD_M = 300.0           # below this the grade is marked small (S&P 600 floor region)
CHART_SESSIONS = 60
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
WIKI_UA = "pead-scanner (rajpurohitchethan-ai@users.noreply.github.com)"
WIKI_LISTS = (("S&P 500", "List_of_S%26P_500_companies"), ("S&P 400", "List_of_S%26P_400_companies"),
              ("S&P 600", "List_of_S%26P_600_companies"))
GICS = ("Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials",
        "Health Care", "Industrials", "Information Technology", "Materials", "Real Estate", "Utilities")

UNIVERSE_PATH = ROOT / "universe.json"
CAL_DIR = ROOT / "store" / "calendar"
FUND_PATH = ROOT / "store" / "fundamentals.json"
LOG_PATH = ROOT / "store" / "trade_log.json"
DATA_PATH = ROOT / "data.json"


# --------------------------------------------------------------------------
# Pure helpers (unit tested)
# --------------------------------------------------------------------------

def num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def r2(v: Any) -> float | None:
    x = num(v)
    return None if x is None else round(x, 2)


def money(text: Any) -> float | None:
    """'$4.10' -> 4.1, '($0.09)' -> -0.09, '$277,622,751,000' -> 277622751000.0, 'N/A' / '' -> None."""
    s = str(text or "").strip()
    if not s or s.upper() in {"N/A", "NA", "--"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[,$()\s]", "", s)
    x = num(s)
    return None if x is None else (-x if neg else x)


def sym_key(symbol: Any) -> str:
    """BRK.B, BRK-B and BRK/B are one stock."""
    return re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())


def yahoo_symbol(symbol: str) -> str:
    return symbol.upper().replace(".", "-").replace("/", "-")


_SYM_RX = re.compile(r"\{\{\s*(?:nyse|nasdaq|bats|cboe|amex|nyse american|nyse arca)[a-z ]*\|\s*([A-Za-z.\-]+)", re.I)
_LINK_RX = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")


def parse_wiki_constituents(wikitext: str, index_name: str) -> list[dict[str, Any]]:
    """Rows of the constituents table: symbol (exchange template), company (first link after it), GICS sector."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in re.split(r"\n\|-", wikitext or ""):
        m = _SYM_RX.search(row)
        if not m:
            continue
        symbol = m.group(1).upper()
        if sym_key(symbol) in seen:
            continue
        rest = row[m.end():]
        name_m = _LINK_RX.search(rest)
        sector = next((g for g in GICS if re.search(r"(?<![A-Za-z])" + re.escape(g) + r"(?![A-Za-z])", rest)), None)
        seen.add(sym_key(symbol))
        out.append({"symbol": symbol, "name": name_m.group(1).strip() if name_m else symbol, "sector": sector,
                    "index": index_name})
    return out


def parse_calendar(payload: dict[str, Any], day: date, universe_keys: set[str]) -> list[dict[str, Any]]:
    """Nasdaq earnings-calendar rows for one date, universe stocks only."""
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        key = sym_key(r.get("symbol"))
        if key not in universe_keys:
            continue
        t = str(r.get("time") or "")
        out.append({"key": key, "date": day.isoformat(),
                    "time": "pre" if "pre-market" in t else "post" if "after-hours" in t else None,
                    "eps": money(r.get("eps")), "epsForecast": money(r.get("epsForecast")),
                    "surprisePct": num(str(r.get("surprise") or "").replace(",", "")),
                    "marketCap": money(r.get("marketCap")), "fiscalQuarter": r.get("fiscalQuarterEnding") or None})
    return out


def pick_reaction_index(f: Any, day: date, time_hint: str | None) -> int | None:
    """Index of the session that traded on the result. Before the open -> that
    day; after the close -> the next session. When the calendar gives no time,
    the one of the two with the higher volume against its prior 20 sessions."""
    days = [d.date() for d in f["Date"]]
    on = next((i for i, d in enumerate(days) if d >= day), None)
    if on is None:
        return None
    if days[on] > day:                       # result on a non-trading day: first session after it
        return on
    nxt = on + 1 if on + 1 < len(days) else None
    if time_hint == "pre":
        return on
    if time_hint == "post":
        return nxt

    def rvol(i: int | None) -> float | None:
        if i is None or i < 5:
            return None
        base = f["Volume"].iloc[max(0, on - 20):on].mean()      # same base for both: sessions before the result
        v = num(f["Volume"].iloc[i])
        return v / base if v is not None and base and base == base else None

    a, b = rvol(on), rvol(nxt)
    if nxt is None:
        return on if (a or 0) >= 1.5 else None   # too early to tell: the next session has not traded
    if a is None or b is None:
        return on
    return on if a >= b else nxt


def yoy_from_quarters(quarters: dict[str, dict[str, Any]], result_day: date) -> dict[str, Any]:
    """Revenue / net income of the reported quarter against the same quarter a
    year earlier. `quarters` maps a quarter-end date to {"rev", "ni"}."""
    ends = sorted((date.fromisoformat(k), v) for k, v in (quarters or {}).items())
    cur = next(((d, v) for d, v in reversed(ends) if d <= result_day and (result_day - d).days <= 120), None)
    if not cur:
        return {}
    prior = next(((d, v) for d, v in ends if abs((cur[0] - d).days - 365) <= 25), None)
    out: dict[str, Any] = {"quarterEnd": cur[0].isoformat(), "revenue": num(cur[1].get("rev")),
                           "netIncome": num(cur[1].get("ni")), "revYoY": None, "niYoY": None, "niTrend": None}
    if prior:
        rev, rev0 = num(cur[1].get("rev")), num(prior[1].get("rev"))
        if rev is not None and rev0 and rev0 > 0:
            out["revYoY"] = r2((rev / rev0 - 1) * 100)
        out["niTrend"], yoy = profit_trend(num(cur[1].get("ni")), num(prior[1].get("ni")))
        out["niYoY"] = r2(yoy)
    return out


def profit_trend(current: float | None, prior: float | None) -> tuple[str | None, float | None]:
    """Same classes as the Indian engine's pat_trend: no percentages on a zero or negative base."""
    if current is None or prior is None:
        return None, None
    if prior <= 0 < current:
        return "TURNAROUND", None
    if prior > 0 and current < 0:
        return "DETERIORATION", None
    if prior < 0 and current < 0:
        return ("LOSS_NARROWING" if abs(current) < abs(prior) else "LOSS_WIDENING" if abs(current) > abs(prior) else "LOSS_FLAT"), None
    if prior == 0:
        return ("TURNAROUND" if current > 0 else "DETERIORATION" if current < 0 else "FLAT"), None
    yoy = (current / prior - 1) * 100
    return ("PROFIT_GROWTH" if yoy >= 0 else "PROFIT_DECLINE"), yoy


def build_item(meta: dict[str, Any], ev: dict[str, Any], frame: Any, fund: dict[str, Any], *, today: date,
               index_frame: Any = None, trade_log: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One stock's row for the page. Returns (item, updated trade log)."""
    item: dict[str, Any] = {
        "symbol": meta["symbol"], "name": meta.get("name"), "sector": meta.get("sector"), "index": meta.get("index"),
        "resultDate": ev["date"], "resultTime": ev.get("time"), "eps": ev.get("eps"), "epsForecast": ev.get("epsForecast"),
        "surprisePct": ev.get("surprisePct"), "fiscalQuarter": ev.get("fiscalQuarter"),
        "marketCapUsdM": r2(ev["marketCap"] / 1e6) if ev.get("marketCap") else None,
        "revenue": fund.get("revenue"), "netIncome": fund.get("netIncome"), "revYoY": fund.get("revYoY"),
        "niYoY": fund.get("niYoY"), "niTrend": fund.get("niTrend"), "quarterEnd": fund.get("quarterEnd"),
        "strength": None, "reaction": None, "plan": None, "grade": None, "hands": None, "chart": None,
        "last": None, "lastDate": None,
    }
    strength = pead_plus.earnings_strength(fund.get("revYoY"), fund.get("niYoY"), fund.get("niTrend"))
    item["strength"] = strength
    f = pead_plus._clean(frame) if frame is not None else None
    if f is None or len(f) < 30:
        item["plan"] = {"signal": "DATA_PENDING", "why": "Price history not available yet.", "entry": None, "sl": None}
        return item, trade_log
    ri = pick_reaction_index(f, date.fromisoformat(ev["date"]), ev.get("time"))
    close = f["Close"]
    last = num(close.iloc[-1])
    item["last"], item["lastDate"] = r2(last), f["Date"].iloc[-1].date().isoformat()
    hi52 = num(close.tail(252).max())
    d52 = r2((last / hi52 - 1) * 100) if last and hi52 else None
    item["distanceFrom52wHighPct"] = d52
    if ri is None or ri < 1:
        item["plan"] = {"signal": "WAIT_REACTION", "why": "Waiting for the first trading session after the result.",
                        "entry": None, "sl": None}
        return item, trade_log
    rdate = f["Date"].iloc[ri].date()
    pre = num(close.iloc[ri - 1])
    ret = (num(close.iloc[ri]) / pre - 1) * 100 if pre else None
    base_vol = f["Volume"].iloc[max(0, ri - 20):ri].mean()
    rvol = num(f["Volume"].iloc[ri]) / base_vol if base_vol and base_vol == base_vol and num(f["Volume"].iloc[ri]) else None
    low, high = num(f["Low"].iloc[ri]), num(f["High"].iloc[ri])
    box = f["High"].iloc[ri:min(len(f), ri + 6)].dropna()
    box_high = num(box.max()) if len(box) else None
    pre20 = (pre / num(close.iloc[ri - 21]) - 1) * 100 if ri >= 21 and pre and num(close.iloc[ri - 21]) else None
    x = pead_plus.extended_features(f, rdate, released=True) or {}
    item["reaction"] = {"date": rdate.isoformat(), "pct": r2(ret), "rvol": r2(rvol), "low": r2(low), "high": r2(high),
                        "closeInRangePct": x.get("close_in_range_pct"), "inferred": ev.get("time") is None,
                        "sinceClosePct": r2((last / num(close.iloc[ri]) - 1) * 100) if last and num(close.iloc[ri]) else None}
    price_neg = ret is not None and ret < -3
    quality_ok: bool | None
    if price_neg or strength in {"WEAK", "AVERAGE"}:
        quality_ok = False
    elif strength in {"STRONG", "AVERAGE+"}:
        quality_ok = True
    else:
        quality_ok = None
    flags = pead_plus.result_red_flags(rev_yoy=fund.get("revYoY"), pat_yoy=fund.get("niYoY"), pat_trend=fund.get("niTrend"),
                                       margin_change_bps=None)
    plan = pead_plus.trade_plan(released=True, reaction_traded=True, quality_ok=quality_ok, x=x, last_price=last,
                                result_return=ret, rvol=rvol, result_low=low, result_high=high, box_high=box_high,
                                concall=None, red_flags=flags)
    log = pead_plus.update_trade_log(trade_log, plan, last, today)
    if not plan.get("notTrading"):
        plan = pead_plus.hold_triggered_entry(plan, log, last, x, None, today)
    item["plan"] = {k: plan.get(k) for k in ("signal", "why", "entry", "sl", "riskPct", "stage", "sizeFraction", "redFlags")}

    rs = None
    if index_frame is not None and pre:
        idx = index_frame.set_index(index_frame["Date"].dt.date)["Close"]
        d0, d1 = f["Date"].iloc[ri - 1].date(), f["Date"].iloc[-1].date()
        if d0 in idx.index and d1 in idx.index and num(idx[d0]):
            ir = (num(idx[d1]) / num(idx[d0]) - 1) * 100
            sr = (last / pre - 1) * 100
            rs = {"stockPct": r2(sr), "indexPct": r2(ir), "relativePct": r2(sr - ir)}
    item["rs"] = rs
    hands = pead_plus.hands_with_rs(x.get("hands"), rs)
    if hands:
        for c in hands.get("checks", []):
            c["note"] = str(c.get("note") or "").replace("NIFTY 500", "S&P 500")
    item["hands"] = hands
    pc = {"lastClose": last, "resultDayPct": ret, "relativeVolume": rvol, "distanceFrom52wHighPct": d52, "pre20dPct": pre20}
    item["grade"] = pead_plus.radar_grade(
        {"marketCapCr": item["marketCapUsdM"], "priceContext": pc,
         "plus": {"plan": plan, "price": x, "strength": strength}},
        min_mcap_cr=MIN_MCAP_USD_M, last_price=last)
    item["pre20dPct"] = r2(pre20)
    tail = f.tail(CHART_SESSIONS)
    ema21 = close.ewm(span=21, adjust=False).mean().tail(CHART_SESSIONS)
    item["chart"] = {"from": tail["Date"].iloc[0].date().isoformat(), "to": item["lastDate"],
                     "close": [r2(v) for v in tail["Close"]], "ema21": [r2(v) for v in ema21],
                     "reactionIndex": ri - (len(f) - len(tail)) if ri >= len(f) - len(tail) else None}
    return item, log


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def http_get(url: str, ua: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json, text/plain, */*",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def curl_get(url: str, ua: str, timeout: int = 30) -> bytes:
    """Nasdaq's API answers curl but refuses Python's urllib (HTTP 403 from
    GitHub's runners, 12 Nov 2026), so the calendar is fetched with curl."""
    import subprocess
    res = subprocess.run(["curl", "-sS", "--fail", "--compressed", "-m", str(timeout), "-H", f"User-Agent: {ua}",
                          "-H", "Accept: application/json, text/plain, */*", "-H", "Accept-Language: en-US,en;q=0.9", url],
                         capture_output=True, timeout=timeout + 10)
    if res.returncode != 0:
        raise RuntimeError(f"curl exit {res.returncode}: {res.stderr.decode('utf-8', 'replace').strip()[:120]}")
    return res.stdout


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj: Any, *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compact else None,
                               indent=None if compact else 1) + "\n", encoding="utf-8")


def refresh_universe(today: date) -> list[dict[str, Any]]:
    """Weekly from Wikipedia. A list that comes back far too short keeps the stored one."""
    stored = load_json(UNIVERSE_PATH, {})
    if stored.get("stocks") and (today - date.fromisoformat(stored.get("refreshed", "2000-01-01"))).days < 7:
        return stored["stocks"]
    stocks: list[dict[str, Any]] = []
    for index_name, title in WIKI_LISTS:
        expect = int(index_name.split()[-1])
        try:
            text = http_get(f"https://en.wikipedia.org/w/index.php?title={title}&action=raw", WIKI_UA).decode("utf-8", "replace")
            rows = parse_wiki_constituents(text, index_name)
        except Exception as exc:
            print(f"Universe {index_name}: {type(exc).__name__}: {exc}")
            rows = []
        if len(rows) < expect * 0.9:
            print(f"Universe {index_name}: parsed {len(rows)} (expected about {expect}); keeping the stored list")
            rows = [s for s in stored.get("stocks", []) if s.get("index") == index_name]
        stocks.extend(rows)
    seen: set[str] = set()
    stocks = [s for s in stocks if not (sym_key(s["symbol"]) in seen or seen.add(sym_key(s["symbol"])))]
    if len(stocks) >= 1000:
        save_json(UNIVERSE_PATH, {"refreshed": today.isoformat(), "count": len(stocks), "stocks": stocks})
    return stocks


def refresh_calendar(today: date, universe_keys: set[str]) -> dict[str, dict[str, Any]]:
    """Latest result per stock in the window. A past date's file is final once it is 3 days old."""
    latest: dict[str, dict[str, Any]] = {}
    fetched = 0
    for back in range(WINDOW_DAYS, -1, -1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        path = CAL_DIR / f"{day.isoformat()}.json"
        stored = load_json(path, None)
        if stored is None or not stored.get("final"):
            try:
                payload = json.loads(curl_get(f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}", BROWSER_UA))
                rows = parse_calendar(payload, day, universe_keys)
                old = {r["key"]: r for r in (stored or {}).get("rows", [])}
                for r in rows:                      # the calendar drops the time once the day has passed: keep it
                    if r["time"] is None and old.get(r["key"], {}).get("time"):
                        r["time"] = old[r["key"]]["time"]
                stored = {"date": day.isoformat(), "final": back > 3, "rows": rows}
                save_json(path, stored, compact=True)
                fetched += 1
                time.sleep(0.4)
            except Exception as exc:
                print(f"Calendar {day}: {type(exc).__name__}: {exc}")
                stored = stored or {"rows": []}
        for r in stored.get("rows", []):
            if r.get("eps") is None and r.get("surprisePct") is None and day >= today:
                continue                            # scheduled, not reported yet
            latest[r["key"]] = r
    print(f"Calendar: {fetched} dates fetched, {len(latest)} universe stocks reported in the last {WINDOW_DAYS} days")
    return latest


def download_prices(symbols: list[str]) -> dict[str, Any]:
    """Daily bars (1 year) per Yahoo symbol; today's unfinished session is dropped."""
    out: dict[str, Any] = {}
    if yf is None or pd is None:
        return out
    now = datetime.now(NY)
    session_open = now.weekday() < 5 and now.hour * 60 + now.minute < 16 * 60 + 10
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        try:
            data = yf.download(batch, period="1y", interval="1d", auto_adjust=False, progress=False, threads=True,
                               group_by="ticker", timeout=30)
        except Exception as exc:
            print(f"Prices batch {i}: {type(exc).__name__}: {exc}")
            continue
        for s in batch:
            try:
                df = data[s] if isinstance(data.columns, pd.MultiIndex) else data
                df = df.dropna(subset=["Close"]).reset_index().rename(columns={"index": "Date"})
                if session_open and len(df) and df["Date"].iloc[-1].date() == now.date():
                    df = df.iloc[:-1]
                if len(df) >= 30:
                    out[s] = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
            except Exception:
                continue
        time.sleep(1.0)
    print(f"Prices: {len(out)} of {len(symbols)} symbols")
    return out


def fetch_quarters(ysym: str) -> dict[str, dict[str, Any]]:
    stmt = yf.Ticker(ysym).quarterly_income_stmt
    out: dict[str, dict[str, Any]] = {}
    if stmt is None or getattr(stmt, "empty", True):
        return out
    for col in stmt.columns:
        rev = stmt.at["Total Revenue", col] if "Total Revenue" in stmt.index else None
        ni = stmt.at["Net Income", col] if "Net Income" in stmt.index else None
        if num(rev) is None and num(ni) is None:
            continue
        out[pd.Timestamp(col).date().isoformat()] = {"rev": num(rev), "ni": num(ni)}
    return out


def refresh_fundamentals(events: dict[str, dict[str, Any]], ysyms: dict[str, str], today: date, budget: int) -> dict[str, Any]:
    """Quarterly revenue / net income per stock. A stock whose reported quarter is
    not in Yahoo yet is retried once a day; newest results first, `budget` a run."""
    store = load_json(FUND_PATH, {})
    if yf is None:
        return store
    order = sorted(events, key=lambda k: events[k]["date"], reverse=True)
    done = 0
    for key in order:
        if done >= budget:
            break
        rec = store.get(key) or {}
        have = yoy_from_quarters(rec.get("quarters") or {}, date.fromisoformat(events[key]["date"]))
        current = have.get("quarterEnd") and (date.fromisoformat(events[key]["date"]) - date.fromisoformat(have["quarterEnd"])).days <= 75
        if current and (have.get("revYoY") is not None or have.get("niTrend")):
            continue
        if rec.get("tried") == today.isoformat():
            continue
        try:
            q = fetch_quarters(ysyms[key])
            rec = {"tried": today.isoformat(), "quarters": {**(rec.get("quarters") or {}), **q}}
        except Exception as exc:
            rec = {**rec, "tried": today.isoformat(), "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        store[key] = rec
        done += 1
        time.sleep(0.35)
    save_json(FUND_PATH, store, compact=True)
    print(f"Fundamentals: {done} stocks fetched this run")
    return store


def run(budget: int) -> int:
    today = datetime.now(NY).date()
    universe = refresh_universe(today)
    if len(universe) < 1000:
        print(f"Universe has only {len(universe)} stocks: not publishing.")
        return 1
    meta = {sym_key(s["symbol"]): s for s in universe}
    events = refresh_calendar(today, set(meta))
    ysyms = {k: yahoo_symbol(meta[k]["symbol"]) for k in events}
    prices = download_prices(sorted(set(ysyms.values())) + ["^GSPC"])
    index_frame = pead_plus._clean(prices.get("^GSPC")) if prices.get("^GSPC") is not None else None
    funds = refresh_fundamentals(events, ysyms, today, budget)
    logs = load_json(LOG_PATH, {})
    items = []
    for key, ev in events.items():
        fund = yoy_from_quarters((funds.get(key) or {}).get("quarters") or {}, date.fromisoformat(ev["date"]))
        if fund.get("quarterEnd") and (date.fromisoformat(ev["date"]) - date.fromisoformat(fund["quarterEnd"])).days > 75:
            fund = {}                                 # Yahoo still shows the previous quarter: not this result
        log_key = f"{key}:{ev['date']}"
        try:
            item, log = build_item(meta[key], ev, prices.get(ysyms[key]), fund, today=today, index_frame=index_frame,
                                   trade_log=logs.get(log_key))
        except Exception as exc:
            print(f"{key}: {type(exc).__name__}: {exc}")
            continue
        if log:
            logs[log_key] = log
        items.append(item)
    priced = sum(1 for i in items if i.get("reaction"))
    if len(items) < 20 or priced < len(items) * 0.5:
        print(f"Only {priced} of {len(items)} results have prices: keeping the published file.")
        return 1
    regime = pead_plus.market_regime([num(v) for v in index_frame["Close"]]) if index_frame is not None else {"label": None}
    save_json(LOG_PATH, {k: v for k, v in logs.items() if k.split(":")[1] >= (today - timedelta(days=WINDOW_DAYS + 30)).isoformat()},
              compact=True)
    save_json(DATA_PATH, {"version": US_VERSION, "generatedAt": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
                          "asOfSession": max((i["lastDate"] for i in items if i.get("lastDate")), default=None),
                          "universe": len(universe), "windowDays": WINDOW_DAYS, "regime": regime,
                          "counts": {"results": len(items), "priced": priced,
                                     "withGrowth": sum(1 for i in items if i.get("strength"))},
                          "items": items}, compact=True)
    print(f"Published {len(items)} results ({priced} with a reaction).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fundamentals-budget", type=int, default=500)
    sys.exit(run(ap.parse_args().fundamentals_budget))
