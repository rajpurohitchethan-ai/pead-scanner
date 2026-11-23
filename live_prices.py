#!/usr/bin/env python3
"""2.9.59: live prices every LIVE_EVERY_SEC during market hours (09:15-15:30 IST,
trading days), from Upstox with the owner's read-only Analytics Token
(UPSTOX_TOKEN secret; never written or printed). Writes live.json and pushes it.
The stocks are the ones on the page: page.json (liquid declared results and the
owner's holdings). Runs until the close or MAX_MINUTES, whichever comes first;
the next engine run starts it again while the market is open."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pead_v2 as p

ROOT = Path(__file__).resolve().parent
LIVE_PATH = ROOT / "live.json"
IST = ZoneInfo("Asia/Kolkata")
EVERY = int(os.getenv("LIVE_EVERY_SEC", "300"))
MAX_MINUTES = int(os.getenv("LIVE_MAX_MINUTES", "330"))


def market_open(now: datetime) -> bool:
    return p.is_trading_day(now.date()) and (9, 15) <= (now.hour, now.minute) <= (15, 31)


_PAGE: dict = {}


def load_page() -> dict:
    for name in ("page.json", "intelligence.json"):
        try:
            return json.loads((ROOT / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def watch_list() -> dict[str, str]:
    """{SYMBOL: ISIN} for every stock in page.json (falls back to intelligence.json)."""
    data = load_page()
    _PAGE.clear(); _PAGE.update(data or {})
    out: dict[str, str] = {}
    for it in (data or {}).get("items") or []:
        isin = str(it.get("eventId") or "").split("|")[0].strip().upper()
        if it.get("symbol"):
            out[str(it["symbol"]).upper()] = isin if isin.startswith("IN") else ""
    return out


def snapshot(watch: dict[str, str], prices: dict[str, dict], now: datetime, sent: set[str] | None = None) -> dict:
    return {"generatedAt": now.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": "UPSTOX", "count": len(prices),
            "alerts": {"date": now.date().isoformat(), "sent": sorted(sent or [])},
            "prices": {sym: [round(q["price"], 2), q.get("prevClose"), q.get("volume")]
                       for sym, q in prices.items() if sym in watch}}


# ---------------------------------------------------------------------------
# 2.9.60 Instant phone alerts (owner-approved 23 Nov 2026) through ntfy.sh.
# The topic name is the NTFY_TOPIC secret (anyone who knows it can read the
# alerts, so it is long and random and never committed). Each alert fires at
# most once a day; the day's sent keys are kept in live.json so a restarted
# job does not repeat them. Exits are judged on the CLOSE: an intraday alert
# says what the rule does IF the stock closes there.
# ---------------------------------------------------------------------------
NTFY_URL = "https://ntfy.sh/"
ENTRY_SIGNALS = {"ENTRY_EARLY", "ENTRY_PULLBACK", "ENTRY_BREAKOUT", "NEAR_ENTRY", "PULLBACK_ZONE"}


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def inr(v) -> str:
    return f"₹{v:,.2f}"


def alerts_for(page: dict, prices: dict[str, dict]) -> list[tuple[str, str, str, str]]:
    """(key, title, body, priority) for every condition met at these prices."""
    out = []
    held = set()
    for pos in page.get("positions") or []:
        sym = str(pos.get("symbol") or "").upper()
        held.add(sym)
        q = prices.get(sym)
        price = _f(q and q.get("price"))
        e, s0, r1 = _f(pos.get("entryPrice")), _f(pos.get("initialStop")), _f(pos.get("oneR"))
        half, rest = _f(pos.get("halfStop")), _f(pos.get("restStop"))
        if price is None or e is None:
            continue
        pnl = (price / e - 1) * 100
        if pos.get("oneRHit"):
            if rest is not None and price < rest:
                out.append((f"{sym}:rest", f"{sym} below {inr(rest)} (20 EMA / cost)",
                            f"{sym} {inr(price)} ({pnl:+.1f}%) is below {inr(rest)}. If it CLOSES below, the rule says sell everything left.", "high"))
            elif half is not None and price < half and not pos.get("halfHit"):
                out.append((f"{sym}:half", f"{sym} below its 10 EMA {inr(half)}",
                            f"{sym} {inr(price)} ({pnl:+.1f}%) is below the 10 EMA {inr(half)}. If it CLOSES below, the rule says sell half; the rest goes below {inr(rest) if rest else 'cost'}.", "default"))
        else:
            if s0 is not None and price < s0:
                out.append((f"{sym}:stop", f"{sym} below its stop {inr(s0)}",
                            f"{sym} {inr(price)} ({pnl:+.1f}%) is below the stop {inr(s0)}. If it CLOSES below, the rule says exit.", "high"))
            elif r1 is not None and price >= r1:
                out.append((f"{sym}:1r", f"{sym} at +1R {inr(r1)}",
                            f"{sym} {inr(price)} ({pnl:+.1f}%) reached +1R {inr(r1)}. On a CLOSE above it the stop moves to your cost {inr(e)}.", "default"))
    for it in page.get("items") or []:
        sym = str(it.get("symbol") or "").upper()
        plus = it.get("plus") or {}
        plan, grade = plus.get("plan") or {}, plus.get("grade") or {}
        stage2 = ((plus.get("price") or {}).get("stage") or {}).get("stage") == 2
        if sym in held or plan.get("signal") not in ENTRY_SIGNALS:
            continue
        if not (grade.get("tier") == "A" or (grade.get("tier") == "B" and stage2)):
            continue                                   # A grades, or B grades in Stage 2
        if (plus.get("liquidity") or {}).get("pass") is not True:
            continue
        q = prices.get(sym)
        price, entry, sl = _f(q and q.get("price")), _f(plan.get("entry")), _f(plan.get("sl"))
        if price is None or entry is None or sl is None or price < entry:
            continue
        if price > entry * 1.03:
            continue                                   # more than 3% above: do not chase
        risk = (price - sl) / price * 100
        stage = (plus.get("price") or {}).get("stage") or {}
        out.append((f"{sym}:entry", f"{sym} at its trigger {inr(entry)} ({grade.get('tier')} {grade.get('score')})",
                    f"{sym} {inr(price)} is above its trigger {inr(entry)}. Stop {inr(sl)} (risk {risk:.1f}%)"
                    + (", Stage 2" if stage.get("stage") == 2 else "") + ". Buy only 3:15-3:25 pm if it is still above, limit order.",
                    "default"))
    return out


MAX_ENTRY_ALERTS = 5          # a day; trade alerts are never capped


def send_alerts(found, sent: set[str], topic: str | None) -> list[str]:
    """Posts the ones not sent today; returns the keys posted."""
    import requests
    done = []
    entries = sum(1 for k in sent if k.endswith(":entry"))
    for key, title, body, prio in found:
        if key in sent or not topic:
            continue
        if key.endswith(":entry"):
            if entries >= MAX_ENTRY_ALERTS:
                continue
            entries += 1
        try:
            r = requests.post(NTFY_URL + topic, data=body.encode("utf-8"), timeout=15,
                              headers={"Title": title.encode("utf-8"), "Priority": prio, "Tags": "chart_with_upwards_trend"})
            if r.status_code < 300:
                done.append(key)
        except Exception as exc:
            print(f"Alert {key} not sent: {type(exc).__name__}")
    return done


def git(*args: str) -> int:
    return subprocess.run(["git", *args], cwd=ROOT).returncode


def push(now: datetime) -> None:
    git("add", "live.json")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-q", "-m", f"Live prices {now:%H:%M} IST")
    for i in range(5):
        if git("push", "-q") == 0:
            return
        git("pull", "-q", "--rebase", "-X", "theirs", "origin", os.getenv("GITHUB_REF_NAME", "main"))
        time.sleep(3 * (i + 1))
    print("Push failed after 5 attempts.")


def main() -> int:
    if not p.upstox_token():
        print("UPSTOX_TOKEN is not set: no live prices.")
        return 0
    started = time.time()
    watch, loaded = watch_list(), time.time()
    topic = (os.getenv("NTFY_TOPIC") or "").strip() or None
    sent: set[str] = set()
    try:
        prev = json.loads(LIVE_PATH.read_text(encoding="utf-8")).get("alerts") or {}
        if prev.get("date") == datetime.now(IST).date().isoformat():
            sent = set(prev.get("sent") or [])
    except (OSError, ValueError, AttributeError):
        pass
    print(f"Watching {len(watch)} stocks. Phone alerts: {'on' if topic else 'off (NTFY_TOPIC not set)'}.")
    if topic and "hello" not in sent:                       # 2.9.67: one quiet "alerts are running" a day
        held = len((_PAGE or {}).get("positions") or [])
        sent.update(send_alerts([("hello", "PEAD live alerts running",
                                  f"Watching {len(watch)} stocks and {held} open trades every 5 minutes until 3:30 pm. "
                                  "Exit and entry alerts come here.", "low")], sent, topic))
    while True:
        now = datetime.now(IST)
        if not market_open(now):
            print(f"{now:%H:%M} IST: market closed, stopping.")
            return 0
        if time.time() - started > MAX_MINUTES * 60:
            print("Time limit reached; the next engine run starts a new live job.")
            return 0
        if time.time() - loaded > 1800:                    # pick up new results from the engine's runs
            git("pull", "-q", "--rebase", "origin", os.getenv("GITHUB_REF_NAME", "main"))
            watch, loaded = watch_list() or watch, time.time()
        try:
            prices = p.upstox_ltp(list(watch.items()))
            if topic:
                new = send_alerts(alerts_for(_PAGE, prices), sent, topic)
                if new:
                    sent.update(new)
                    print(f"{now:%H:%M} IST: alerts sent: {', '.join(new)}")
            LIVE_PATH.write_text(json.dumps(snapshot(watch, prices, now, sent), separators=(",", ":")) + "\n", encoding="utf-8")
            print(f"{now:%H:%M} IST: {len(prices)} prices")
            push(now)
        except PermissionError as exc:
            print(f"{exc}. Stopping.")
            return 1
        except Exception as exc:
            print(f"{now:%H:%M} IST: {type(exc).__name__}: {exc}")
        time.sleep(max(30, EVERY - (time.time() - started) % EVERY))


if __name__ == "__main__":
    sys.exit(main())
