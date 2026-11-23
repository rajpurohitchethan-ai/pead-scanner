#!/usr/bin/env python3
"""2.9.74 Telegram alerts with buttons (owner-approved 23 Nov 2026).

Secrets (GitHub): TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (the
owner's chat). Neither is ever written or printed. Without them nothing runs.

Alerts carry buttons:
  "Open card"        a link to the stock on the page
  "Sold half" / "Sold all" / "I bought"   record the trade in master/positions.json
Button taps and two text commands are read with getUpdates (long polling)
by the live job in market hours and by every engine run:
  /fill SYMBOL PRICE   correct the price of the last thing recorded for SYMBOL
  /trades              list the open trades
Only messages from TELEGRAM_CHAT_ID are acted on. Quantities are never asked
for or stored (the repo is public): a part sale is stored as a fraction.

Usage: python telegram_bot.py --poll   (handle waiting taps once, then exit)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
POSITIONS = ROOT / "master" / "positions.json"
STATE = ROOT / "master" / "telegram_state.json"      # only the update offset (no chat ids, no tokens)
PAGE_URL = "https://rajpurohitchethan-ai.github.io/pead-scanner/"
IST = ZoneInfo("Asia/Kolkata")
API = "https://api.telegram.org/bot{token}/{method}"


def configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())


def _call(method: str, payload: dict, timeout: int = 30) -> dict:
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    r = requests.post(API.format(token=token, method=method), json=payload, timeout=timeout)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "status": r.status_code}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def buttons_for(key: str) -> list[list[dict]]:
    """Inline buttons for an alert key such as 'GROWW:half' (see live_prices.alerts_for)."""
    sym, _, kind = key.partition(":")
    row = []
    if sym and kind:
        anchor = f"#t-{sym}" if kind in ("half", "rest", "stop", "1r") else f"#s-{sym}"
        row.append({"text": "Open card", "url": PAGE_URL + anchor})
    if kind == "half":
        row.append({"text": "Sold half", "callback_data": f"sold|{sym}|half"})
    if kind in ("rest", "stop", "half"):
        row.append({"text": "Sold all", "callback_data": f"sold|{sym}|all"})
    if kind == "entry":
        row.append({"text": "I bought", "callback_data": f"bought|{sym}"})
    return [row] if row else []


def send(title: str, body: str, key: str = "") -> bool:
    if not configured():
        return False
    payload = {"chat_id": os.getenv("TELEGRAM_CHAT_ID", "").strip(), "text": f"{title}\n{body}",
               "disable_web_page_preview": True}
    kb = buttons_for(key)
    if kb:
        payload["reply_markup"] = {"inline_keyboard": kb}
    try:
        return bool(_call("sendMessage", payload, timeout=15).get("ok"))
    except Exception as exc:                                   # never stop the live job over a message
        print(f"Telegram send failed: {type(exc).__name__}")
        return False


# ---------------------------------------------------------------------------
# Recording trades (pure functions: tested without the network)
# ---------------------------------------------------------------------------
def _open(doc: dict, sym: str) -> dict | None:
    return next((r for r in doc.get("positions") or [] if str(r.get("symbol")).upper() == sym
                 and str(r.get("status") or "OPEN").upper() == "OPEN"), None)


def apply_action(doc: dict, action: str, price: float | None, today: date, plan: dict | None = None) -> str:
    """Changes positions.json (doc) for one button tap or command; returns the reply text."""
    parts = action.split("|")
    if parts[0] == "sold" and len(parts) == 3:
        sym, what = parts[1].upper(), parts[2]
        pos = _open(doc, sym)
        if not pos:
            return f"{sym}: no open trade recorded."
        if price is None:
            return f"{sym}: no price available right now. Send /fill {sym} PRICE after the sale."
        if what == "half":
            if any(x.get("via") == "telegram" and x.get("date") == today.isoformat() for x in pos.get("partialExits") or []):
                return f"{sym}: a half sale is already recorded today."
            pos.setdefault("partialExits", []).append({"date": today.isoformat(), "price": round(price, 2), "fraction": 0.5,
                                                       "note": "Recorded from Telegram at the live price; /fill corrects it", "via": "telegram"})
            return f"Recorded: {sym} half sold at about ₹{price:,.2f} (live price). Send /fill {sym} PRICE with your fill to correct it."
        pos.update(status="CLOSED", exitDate=today.isoformat(), exitPrice=round(price, 2), exitVia="telegram",
                   exitNote="Recorded from Telegram at the live price" + (" (after a part sale)" if pos.get("partialExits") else ""))
        return f"Recorded: {sym} sold (all) at about ₹{price:,.2f} (live price). Send /fill {sym} PRICE with your fill to correct it."
    if parts[0] == "bought" and len(parts) == 2:
        sym = parts[1].upper()
        if _open(doc, sym):
            return f"{sym} is already an open trade."
        sl = (plan or {}).get("sl")
        if price is None or sl is None or price <= sl:
            return f"{sym}: no live price or stop to record. Tell Claude the buy price."
        doc.setdefault("positions", []).append({"symbol": sym, "entryDate": today.isoformat(), "entryPrice": round(price, 2),
                                                "stop": sl, "status": "OPEN", "entryVia": "telegram",
                                                "setup": f"Recorded from Telegram at the live price; screener stop {sl}. /fill corrects the price"})
        return f"Recorded: bought {sym} at about ₹{price:,.2f}, stop ₹{sl:,.2f}. Send /fill {sym} PRICE with your fill to correct it."
    return "Unknown button."


def apply_fill(doc: dict, sym: str, price: float, today: date) -> tuple[str, bool]:
    """/fill: correct today's Telegram-recorded price for sym (a part sale, a full sale or a buy)."""
    sym = sym.upper()
    rows = [r for r in doc.get("positions") or [] if str(r.get("symbol")).upper() == sym]
    for r in reversed(rows):
        if r.get("exitVia") == "telegram" and r.get("exitDate") == today.isoformat():
            r["exitPrice"], r["exitNote"] = round(price, 2), "Recorded from Telegram; price corrected with /fill"
            return f"{sym}: sale price set to ₹{price:,.2f}.", True
        px = [x for x in r.get("partialExits") or [] if x.get("via") == "telegram" and x.get("date") == today.isoformat()]
        if px:
            px[-1]["price"], px[-1]["note"] = round(price, 2), "Recorded from Telegram; price corrected with /fill"
            return f"{sym}: part-sale price set to ₹{price:,.2f}.", True
        if r.get("entryVia") == "telegram" and r.get("entryDate") == today.isoformat():
            r["entryPrice"] = round(price, 2)
            return f"{sym}: buy price set to ₹{price:,.2f}.", True
    return f"{sym}: nothing recorded from Telegram today to correct.", False


def trades_text(doc: dict) -> str:
    rows = [r for r in doc.get("positions") or [] if str(r.get("status") or "OPEN").upper() == "OPEN"]
    if not rows:
        return "No open trades."
    return "Open trades:\n" + "\n".join(f"{r['symbol']}: bought {r.get('entryDate')} at ₹{r.get('entryPrice')}, stop ₹{r.get('stop')}"
                                        + (" (part sold)" if r.get("partialExits") else "") for r in rows)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
def _prices_from_page() -> tuple[dict, dict]:
    """(last price by symbol, plan by symbol) from live.json and page.json."""
    prices, plans = {}, {}
    try:
        page = json.loads((ROOT / "page.json").read_text(encoding="utf-8"))
        for p_ in page.get("positions") or []:
            if p_.get("last") is not None:
                prices[str(p_["symbol"]).upper()] = float(p_["last"])
        for it in page.get("items") or []:
            sym = str(it.get("symbol") or "").upper()
            plans[sym] = (it.get("plus") or {}).get("plan") or {}
            lc = (it.get("priceContext") or {}).get("lastClose")
            if lc is not None:
                prices.setdefault(sym, float(lc))
    except (OSError, ValueError, TypeError):
        pass
    try:
        live = json.loads((ROOT / "live.json").read_text(encoding="utf-8"))
        for sym, v in (live.get("prices") or {}).items():
            if v and v[0] is not None:
                prices[sym.upper()] = float(v[0])
    except (OSError, ValueError, TypeError):
        pass
    return prices, plans


def poll(wait: int = 0, prices: dict | None = None) -> bool:
    """Handles waiting button taps and commands. Returns True when positions.json changed."""
    if not configured():
        return False
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    res = _call("getUpdates", {"offset": int(state.get("offset") or 0), "timeout": wait,
                               "allowed_updates": ["callback_query", "message"]}, timeout=wait + 15)
    updates = res.get("result") or [] if res.get("ok") else []
    if not updates:
        return False
    doc = json.loads(POSITIONS.read_text(encoding="utf-8"))
    page_prices, plans = _prices_from_page()
    page_prices.update(prices or {})
    today, changed = datetime.now(IST).date(), False
    for u in updates:
        state["offset"] = int(u["update_id"]) + 1
        cq, msg = u.get("callback_query"), u.get("message")
        src = str(((cq or {}).get("message") or {}).get("chat", {}).get("id") or (msg or {}).get("chat", {}).get("id") or "")
        if src != chat:
            continue                                            # not the owner: ignore silently
        if cq:
            data = str(cq.get("data") or "")
            sym = data.split("|")[1].upper() if "|" in data else ""
            before = json.dumps(doc, sort_keys=True)
            reply = apply_action(doc, data, page_prices.get(sym), today, plans.get(sym))
            changed |= json.dumps(doc, sort_keys=True) != before
            try:
                _call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": reply[:190]}, timeout=10)
            except Exception:
                pass
            send("PEAD", reply)
        elif msg:
            text = str(msg.get("text") or "").strip()
            bits = text.split()
            if bits[:1] == ["/fill"] and len(bits) == 3:
                try:
                    reply, did = apply_fill(doc, bits[1], float(bits[2].replace(",", "").lstrip("₹")), today)
                    changed |= did
                except ValueError:
                    reply = "Use: /fill SYMBOL PRICE, for example /fill GROWW 216.73"
                send("PEAD", reply)
            elif bits[:1] == ["/trades"]:
                send("PEAD", trades_text(doc))
            elif bits[:1] in (["/start"], ["/help"]):
                send("PEAD", "Alerts arrive here with buttons. Commands: /trades, /fill SYMBOL PRICE.")
    try:                                                        # confirm at once so another job never sees them again
        _call("getUpdates", {"offset": int(state["offset"]), "timeout": 0}, timeout=15)
    except Exception:
        pass
    STATE.write_text(json.dumps(state) + "\n", encoding="utf-8")
    if changed:
        POSITIONS.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


if __name__ == "__main__":
    if "--poll" in sys.argv and "--outside-market" in sys.argv:
        # the engine run polls only when the live job is not running (market hours on trading days)
        now = datetime.now(IST)
        try:
            import pead_v2
            trading = pead_v2.is_trading_day(now.date())
        except Exception:
            trading = now.weekday() < 5
        if trading and (9, 10) <= (now.hour, now.minute) <= (15, 35):
            print("Telegram: market hours, the live job handles buttons.")
            sys.exit(0)
    if "--poll" in sys.argv:
        print("Telegram:", "positions changed" if poll() else "nothing to record" if configured() else "not configured")
    sys.exit(0)
