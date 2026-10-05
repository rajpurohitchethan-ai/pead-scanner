import json, datetime as dt, sys
import yfinance as yf
import pandas as pd
import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
now = dt.datetime.now(IST); today = now.date()
manual = json.load(open("companies.json"))
status = {"yahoo": "ok", "nse": {}}
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
     "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9",
     "Referer": "https://www.nseindia.com/"}
MIN_MCAP_CR = 500
MAX_AUTO = 70

def pdate(x):
    for f in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try: return dt.datetime.strptime(str(x).strip(), f).date()
        except Exception: pass

def rows_of(r):
    j = r.json()
    return j.get("data", []) if isinstance(j, dict) else j

def session():
    s = requests.Session()
    s.get("https://www.nseindia.com", headers=H, timeout=20)
    s.get("https://www.nseindia.com/companies-listing/corporate-filings-event-calendar", headers=H, timeout=20)
    return s

def discover(s):
    f = (today - dt.timedelta(days=10)).strftime("%d-%m-%Y")
    t = (today + dt.timedelta(days=21)).strftime("%d-%m-%Y")
    out = {}
    for name in ("event-calendar", "corporate-board-meetings"):
        url = f"https://www.nseindia.com/api/{name}?index=equities&from_date={f}&to_date={t}"
        try:
            r = s.get(url, headers=H, timeout=25); n = 0
            status["nse"][name] = f"HTTP {r.status_code}"
            for row in rows_of(r):
                p = ((row.get("purpose") or "") + " " + (row.get("bm_desc") or "")).lower()
                if "result" not in p: continue
                d = pdate(row.get("date") or row.get("meetingdate") or row.get("bm_date"))
                if row.get("symbol") and d:
                    out.setdefault(row["symbol"], {"date": d.isoformat(), "name": row.get("company") or row.get("sm_name") or row["symbol"]}); n += 1
            status["nse"][name] += f", {n} result rows"
        except Exception as e:
            status["nse"][name] = f"failed: {type(e).__name__}"
    return out

def fin(s, sym):
    r = s.get(f"https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}&period=Quarterly", headers=H, timeout=25)
    rows = rows_of(r)
    def num(row, keys):
        for k in keys:
            try: return float(str(row.get(k)).replace(",", ""))
            except Exception: pass
    def qd(row):
        for k in ("toDate", "qe_Date", "to_date"):
            if row.get(k): return pdate(row[k])
    rows = [x for x in rows if qd(x)]
    rows.sort(key=qd, reverse=True)
    if not rows: return None
    a = rows[0]
    prev = next((x for x in rows[1:] if abs((qd(a) - qd(x)).days - 365) <= 20), None)
    INC = ("income", "totalIncome", "re_total_inc", "re_income_from_ops")
    PAT = ("proLossAftTax", "reProLossAftTax", "netProLossForPeriod", "re_net_profit", "re_pro_loss_aft_tax")
    out = {"quarter": qd(a).isoformat()}
    for key, ks in (("incomeYoY", INC), ("patYoY", PAT)):
        x, y = num(a, ks), (num(prev, ks) if prev else None)
        if x is not None and y and y > 0: out[key] = round((x / y - 1) * 100, 1)
    return out if len(out) > 1 else None

def pct(a, b): return round((a / b - 1) * 100, 2) if b else None

try:
    s = session(); disc = discover(s)
except Exception as e:
    s = None; disc = {}; status["nse"]["session"] = f"failed: {type(e).__name__}"

names = {c["sym"] for c in manual}
universe = [dict(c) for c in manual]
for sym, v in list(disc.items())[:400]:
    if sym in names or len([u for u in universe if u.get("auto")]) >= MAX_AUTO: continue
    universe.append({"sym": sym, "name": v["name"], "sector": "Auto-discovered", "resultDate": v["date"], "quarter": "",
                     "bucket": "Unclassified", "entry": None, "sl": None, "note": "", "auto": True})

res = []
for o in universe:
    sym = o["sym"]
    try:
        tk = yf.Ticker(sym + ".NS")
        h = tk.history(period="1y", auto_adjust=False).dropna(subset=["Close"])
        if h.empty: raise ValueError("no price data")
        h.index = h.index.tz_localize(None) if h.index.tz is None else h.index.tz_convert(IST).tz_localize(None)
        try: o["mcapCr"] = round(tk.fast_info["market_cap"] / 1e7)
        except Exception: o["mcapCr"] = None
        if o.get("auto") and (o["mcapCr"] or 0) < MIN_MCAP_CR: continue
        last = h.iloc[-1]; prev = h.iloc[-2]["Close"] if len(h) > 1 else None
        o.update(price=round(float(last["Close"]), 2), chg=pct(last["Close"], prev),
                 asOf=h.index[-1].strftime("%Y-%m-%d"), high52=round(float(h["High"].max()), 2))
        for n in (10, 20, 50, 200):
            o[f"dma{n}"] = round(float(h["Close"].tail(n).mean()), 2) if len(h) >= n else None
        o["aboveDma"] = sum(1 for n in (10, 20, 50, 200) if o[f"dma{n}"] and o["price"] > o[f"dma{n}"])
        o["volRatio"] = round(float(last["Volume"] / h["Volume"].tail(21).iloc[:-1].mean()), 2) if len(h) > 21 else None
        rd = pd.Timestamp(o["resultDate"]); o["nseDate"] = disc.get(sym, {}).get("date")
        if today < rd.date(): o["phase"] = "Upcoming"
        else:
            o["phase"] = "Result day" if today == rd.date() else "Post-results"
            after = h[h.index > rd]; before = h[h.index <= rd]
            if len(after) and len(before):
                ref = before.iloc[-1]["Close"]; day = after.iloc[0]
                vr = round(float(day["Volume"] / h["Volume"].loc[:rd].tail(20).mean()), 2)
                gap, dp = pct(day["Open"], ref), pct(day["Close"], ref)
                o["reaction"] = {"day": after.index[0].strftime("%Y-%m-%d"), "gapPct": gap, "dayPct": dp,
                                 "sincePct": pct(last["Close"], ref), "volRatio": vr,
                                 "dayHigh": round(float(day["High"]), 2), "dayLow": round(float(day["Low"]), 2),
                                 "holdingAboveDayHigh": bool(last["Close"] >= day["High"])}
                o["signal"] = ("Strong reaction" if ((dp or 0) >= 4 or (gap or 0) >= 3) and vr >= 2
                               else "Weak reaction" if (dp or 0) <= -4 and vr >= 2 else "Muted reaction")
            if s is not None and (today - rd.date()).days <= 14:
                try: o["results"] = fin(s, sym)
                except Exception as e: status["nse"]["results"] = f"failed: {type(e).__name__}"
        base = h[h.index <= rd] if today >= rd.date() else h
        o["preRunup"] = pct(base["Close"].iloc[-1], base["Close"].iloc[-21]) if len(base) > 21 else None
        o["avgTradedCr"] = round(float((h["Close"] * h["Volume"]).tail(20).mean() / 1e7), 1)
        e = o.get("entry")
        if e: o["distToEntryPct"] = pct(e, o["price"]); o["nearEntry"] = abs(o["distToEntryPct"]) <= 2
        o["error"] = None
    except Exception as ex:
        o["error"] = f"{type(ex).__name__}: {ex}"
    res.append(o)

bad = sum(1 for r in res if r["error"])
if bad: status["yahoo"] = f"{bad} of {len(res)} symbols failed"
status["discovered"] = f"{sum(1 for r in res if r.get('auto'))} auto-added"
if res and bad == len(res): print("all symbols failed", file=sys.stderr); sys.exit(1)
json.dump({"generatedAt": now.isoformat(), "status": status, "companies": res}, open("data.json", "w"), indent=1)
print("done", status)
