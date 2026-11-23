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


def watch_list() -> dict[str, str]:
    """{ISIN: SYMBOL} for every stock in page.json (falls back to intelligence.json)."""
    for name in ("page.json", "intelligence.json"):
        try:
            data = json.loads((ROOT / name).read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            data = None
    out: dict[str, str] = {}
    for it in (data or {}).get("items") or []:
        isin = str(it.get("eventId") or "").split("|")[0].strip().upper()
        if isin.startswith("IN") and it.get("symbol"):
            out[isin] = str(it["symbol"])
    for pos in (data or {}).get("positions") or []:
        pass  # holdings are already among page.json's items
    return out


def snapshot(watch: dict[str, str], prices: dict[str, dict], now: datetime) -> dict:
    return {"generatedAt": now.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": "UPSTOX", "count": len(prices),
            "prices": {watch[i]: [round(q["price"], 2), q.get("prevClose"), q.get("volume")]
                       for i, q in prices.items() if i in watch}}


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
    print(f"Watching {len(watch)} stocks.")
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
            prices = p.upstox_ltp(list(watch))
            LIVE_PATH.write_text(json.dumps(snapshot(watch, prices, now), separators=(",", ":")) + "\n", encoding="utf-8")
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
