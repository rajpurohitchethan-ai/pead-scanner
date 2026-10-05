import json, datetime as dt, sys
import yfinance as yf
import pandas as pd
import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
now = dt.datetime.now(IST)
today = now.date()
companies = json.load(open("companies.json"))
status = {"yahoo": "ok", "nse_calendar": "not tried"}

def nse_calendar():
    s = requests.Session()
    h = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
    s.get("https://www.nseindia.com", headers=h, timeout=15)
    f = today.strftime("%d-%m-%Y"); t = (today + dt.timedelta(days=45)).strftime("%d-%m-%Y")
    r = s.get(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={f}&to_date={t}", headers=h, timeout=20)
    r.raise_for_status()
    out = {}
    for row in r.json():
        if "result" in (row.get("purpose") or "").lower():
            out.setdefault(row["symbol"], row["meetingdate"])
    return out

try:
    nse = nse_calendar(); status["nse_calendar"] = f"ok ({len(nse)} result meetings)"
except Exception as e:
    nse = {}; status["nse_calendar"] = f"failed: {type(e).__name__}"

def pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None

res = []
for c in companies:
    o = dict(c); sym = c["sym"]
    try:
        tk = yf.Ticker(sym + ".NS")
        h = tk.history(period="1y", auto_adjust=False).dropna(subset=["Close"])
        if h.empty: raise ValueError("no price data")
        h.index = h.index.tz_localize(None) if h.index.tz is None else h.index.tz_convert(IST).tz_localize(None)
        last = h.iloc[-1]; prev = h.iloc[-2]["Close"] if len(h) > 1 else None
        o.update(price=round(float(last["Close"]), 2), chg=pct(last["Close"], prev),
                 asOf=h.index[-1].strftime("%Y-%m-%d"), high52=round(float(h["High"].max()), 2))
        for n in (10, 20, 50, 200):
            o[f"dma{n}"] = round(float(h["Close"].tail(n).mean()), 2) if len(h) >= n else None
        o["aboveDma"] = sum(1 for n in (10, 20, 50, 200) if o[f"dma{n}"] and o["price"] > o[f"dma{n}"])
        o["volRatio"] = round(float(last["Volume"] / h["Volume"].tail(21).iloc[:-1].mean()), 2) if len(h) > 21 else None
        try: o["mcapCr"] = round(tk.fast_info["market_cap"] / 1e7)
        except Exception: o["mcapCr"] = None
        rd = pd.Timestamp(c["resultDate"])
        o["nseDate"] = nse.get(sym)
        if today < rd.date(): o["phase"] = "Upcoming"
        else:
            o["phase"] = "Result day" if today == rd.date() else "Post-results"
            after = h[h.index > rd]; before = h[h.index <= rd]
            if len(after) and len(before):
                ref = before.iloc[-1]["Close"]; day = after.iloc[0]
                o["reaction"] = {"day": after.index[0].strftime("%Y-%m-%d"),
                                 "gapPct": pct(day["Open"], ref), "dayPct": pct(day["Close"], ref),
                                 "sincePct": pct(last["Close"], ref),
                                 "volRatio": round(float(day["Volume"] / h["Volume"].loc[:rd].tail(20).mean()), 2),
                                 "dayHigh": round(float(day["High"]), 2),
                                 "holdingAboveDayHigh": bool(last["Close"] >= day["High"])}
        e = c.get("entry")
        if e: o["distToEntryPct"] = pct(e, o["price"]); o["nearEntry"] = abs(o["distToEntryPct"]) <= 2
        o["error"] = None
    except Exception as ex:
        o["error"] = f"{type(ex).__name__}: {ex}"
    res.append(o)

bad = sum(1 for r in res if r["error"])
if bad: status["yahoo"] = f"{bad} of {len(res)} symbols failed"
if bad == len(res): print("all symbols failed", file=sys.stderr); sys.exit(1)
json.dump({"generatedAt": now.isoformat(), "status": status, "companies": res}, open("data.json", "w"), indent=1)
print("done", status)
