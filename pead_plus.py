"""PEAD analytics layer (engine 2.4).

Pure functions on top of the persistent event store in pead_v2.py:

* extended price features  - EMAs, highest-volume-on-earnings flags, return
  since result, base integrity, forward outcomes, Q1 reaction replay, chart
* earnings strength + margin expansion
* Q1 -> Q2 PEAD buckets     - Confirmation / Re-PEAD / Fresh PEAD (Suresh K
  framework: "build the buckets, wait for results, follow earnings + price")
* trade plan                - early entry, pullback entry, box breakout,
  SL at the result base, TSL on 21/63 EMA, risk % and R-multiple
* liquidity, valuation view, sector tailwind (peer momentum + peer results),
  market regime and an outcome scorecard

Nothing here fetches data and nothing invents a value: every function returns
None when its inputs are missing, so the dashboard shows "—".
"""
from __future__ import annotations

import math
from datetime import date
from statistics import median
from typing import Any

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

# Thresholds (kept together so they are easy to tune after the scorecard).
LIQ_TURNOVER_CR = 1.0          # ₹ Cr average daily traded value (20D)
LIQ_MIN_PRICE = 20.0           # ₹
STRONG_REV_YOY = 15.0
STRONG_PAT_YOY = 20.0
Q1_SETUP_RETURN = 4.0          # result-session return that made Q1 a "setup"
Q1_SETUP_RVOL = 1.5
EARLY_ENTRY_SESSIONS = 2       # sessions after the reaction day for an early entry
EARLY_MIN_RETURN = 4.0
EARLY_MIN_RVOL = 2.0
PULLBACK_BAND_PCT = 3.0        # price within this % above the 10/21 EMA
NEAR_TRIGGER_PCT = 3.0
EXTENDED_ABOVE_EMA21_PCT = 15.0
MAX_RISK_PCT = 10.0
SL_BUFFER_PCT = 1.0            # SL sits 1% under the result-day low / base


def _num(x: Any) -> float | None:
    try:
        if x is None or isinstance(x, bool):
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r2(x: Any) -> float | None:
    v = _num(x)
    return round(v, 2) if v is not None else None


def _pct(new: Any, old: Any) -> float | None:
    a, b = _num(new), _num(old)
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


# ---------------------------------------------------------------------------
# Price features
# ---------------------------------------------------------------------------

def _clean(frame: Any):
    if pd is None or frame is None or getattr(frame, "empty", True):
        return None
    f = frame.copy()
    f["Date"] = pd.to_datetime(f["Date"], errors="coerce")
    try:
        f["Date"] = f["Date"].dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c not in f.columns:
            f[c] = None
        f[c] = pd.to_numeric(f[c], errors="coerce")
    f = f.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    return None if f.empty else f


def _idx_on_or_after(f, d: date | None) -> int | None:
    if d is None:
        return None
    hits = f.index[f["Date"].dt.date >= d].tolist()
    return hits[0] if hits else None


def _ema(series, n: int) -> float | None:
    s = series.dropna()
    if len(s) < max(5, n // 2):
        return None
    return _r2(s.ewm(span=n, adjust=False).mean().iloc[-1])


def _reaction_volume_flags(f, i: int) -> dict[str, Any]:
    vol = f["Volume"]
    v = _num(vol.iloc[i])
    out = {"hvq": None, "hvy": None, "hve": None, "hv_label": None}
    if v is None or v <= 0:
        return out

    def highest(lookback: int | None) -> bool | None:
        start = 0 if lookback is None else max(0, i - lookback)
        window = vol.iloc[start:i].dropna()
        need = 40 if lookback is None else min(lookback, 40)
        if len(window) < need:
            return None
        return bool(v >= window.max())

    out["hvq"] = highest(63)
    out["hvy"] = highest(250)
    # "Ever" needs a long history; with ~1 year of data it equals HVY, so only
    # claim HVE when more than 300 sessions precede the reaction day.
    out["hve"] = highest(None) if i > 300 else None
    out["hv_label"] = "HVE" if out["hve"] else "HVY" if out["hvy"] else "HVQ" if out["hvq"] else None
    return out


def _chart(f, reaction_idx: int | None, points: int = 75) -> dict[str, Any]:
    tail = f.tail(points)
    closes = [_r2(x) for x in tail["Close"].tolist()]
    ema21 = f["Close"].ewm(span=21, adjust=False).mean().tail(points).round(2).tolist()
    marker = None
    if reaction_idx is not None and reaction_idx >= len(f) - points:
        marker = reaction_idx - (len(f) - len(tail))
    return {"from": tail["Date"].iloc[0].date().isoformat(), "to": tail["Date"].iloc[-1].date().isoformat(),
            "close": closes, "ema21": ema21, "reactionIndex": marker}


def extended_features(frame: Any, reaction_date: date | None, *, released: bool,
                      q1_reaction_date: date | None = None, q2_boundary: date | None = None) -> dict[str, Any]:
    """Price-derived PEAD features. `reaction_date` is the Q2 reaction session
    (only used when the result is released and that session has traded);
    `q1_reaction_date` replays the previous quarter's reaction on the same chart."""
    f = _clean(frame)
    if f is None:
        return {}
    close = f["Close"]
    last = _num(close.iloc[-1])
    out: dict[str, Any] = {
        "ema10": _ema(close, 10), "ema21": _ema(close, 21), "ema63": _ema(close, 63),
        "ret_21d_pct": _r2(_pct(last, close.iloc[-22])) if len(f) > 22 else None,
        "ret_63d_pct": _r2(_pct(last, close.iloc[-64])) if len(f) > 64 else None,
        "ret_126d_pct": _r2(_pct(last, close.iloc[-127])) if len(f) > 127 else None,
        "high_52w": _r2(close.tail(252).max()),
        "low_52w": _r2(close.tail(252).min()),
        "last_session": f["Date"].iloc[-1].date().isoformat(),
    }
    has_hl = f["High"].notna().sum() > len(f) * 0.8 and f["Low"].notna().sum() > len(f) * 0.8
    if has_hl and len(f) > 15:
        prev = close.shift(1)
        tr = pd.concat([(f["High"] - f["Low"]), (f["High"] - prev).abs(), (f["Low"] - prev).abs()], axis=1).max(axis=1)
        out["atr14"] = _r2(tr.tail(14).mean())
    elif len(f) > 15:
        out["atr14"] = _r2(close.diff().abs().tail(14).mean() * 1.25)   # close-only proxy (BSE data)

    ri = _idx_on_or_after(f, reaction_date) if released else None
    chart_marker = ri
    if ri is not None and ri > 0:
        pre_close = _num(close.iloc[ri - 1])
        r_close = _num(close.iloc[ri])
        r_open = _num(f["Open"].iloc[ri])
        r_low = _num(f["Low"].iloc[ri]) or r_close
        r_high = _num(f["High"].iloc[ri]) or r_close
        after = f.iloc[ri:]
        out.update(_reaction_volume_flags(f, ri))
        out.update({
            "reaction_close": _r2(r_close),
            "reaction_open": _r2(r_open),
            "gap_pct": _r2(_pct(r_open, pre_close)) if r_open is not None else None,
            "gap_held": (bool(r_close >= r_open) if r_open is not None and r_close is not None else None),
            "close_in_range_pct": (_r2((r_close - r_low) / (r_high - r_low) * 100)
                                   if r_close is not None and r_high is not None and r_low is not None and r_high > r_low else None),
            "sessions_since_reaction": int(len(f) - 1 - ri),
            "return_since_result_pct": _r2(_pct(last, pre_close)),
            "post_result_high": _r2(after["High"].max() if after["High"].notna().any() else after["Close"].max()),
            "post_result_low": _r2(after["Low"].min() if after["Low"].notna().any() else after["Close"].min()),
            "prev_session_high": _r2(f["High"].iloc[-1] if _num(f["High"].iloc[-1]) is not None else close.iloc[-1]),
        })
        if len(f) >= 2:
            h2 = _num(f["High"].iloc[-2]) or _num(close.iloc[-2])
            out["day_before_high"] = _r2(h2)
            out["reclaimed_prev_high"] = bool(last > h2) if h2 is not None and last is not None else None
        out["drawdown_from_post_high_pct"] = _r2(_pct(last, out["post_result_high"]))
        # Base integrity: has any later session closed below the result-day low?
        later = f.iloc[ri + 1:]
        out["base_broken"] = bool((later["Close"] < r_low * (1 - SL_BUFFER_PCT / 100)).any()) if not later.empty else False
        for n in (5, 20, 60):
            j = ri + n
            out[f"fwd_{n}d_pct"] = _r2(_pct(close.iloc[j], pre_close)) if j < len(f) else None

    # Previous quarter (Q1) reaction replayed on this chart.
    qi = _idx_on_or_after(f, q1_reaction_date)
    if qi is not None and qi > 0:
        q_pre = _num(close.iloc[qi - 1])
        q_close = _num(close.iloc[qi])
        vol_prior = f["Volume"].iloc[max(0, qi - 20):qi].dropna()
        q_rvol = (_num(f["Volume"].iloc[qi]) / vol_prior.mean()) if len(vol_prior) >= 10 and vol_prior.mean() > 0 and _num(f["Volume"].iloc[qi]) else None
        # Sustain test is measured up to the Q2 result (or today when Q2 is pending).
        end_i = (_idx_on_or_after(f, q2_boundary) or len(f)) - 1
        end_i = max(qi, min(end_i, len(f) - 1))
        seg = close.iloc[qi:end_i + 1]
        out.update({
            "q1_reaction_date": f["Date"].iloc[qi].date().isoformat(),
            "q1_reaction_return_pct": _r2(_pct(q_close, q_pre)),
            "q1_reaction_rvol": _r2(q_rvol),
            "q1_return_to_q2_pct": _r2(_pct(close.iloc[end_i], q_pre)),
            "q1_max_gain_pct": _r2(_pct(seg.max(), q_pre)),
            "q1_sustained": bool(_num(close.iloc[end_i]) >= (q_close or 0)) if q_close else None,
        })
    out["chart"] = _chart(f, chart_marker)
    return out


# ---------------------------------------------------------------------------
# Earnings, margins, buckets
# ---------------------------------------------------------------------------

def earnings_strength(rev_yoy: Any, pat_yoy: Any, pat_trend: Any, margin_change_bps: Any = None) -> str | None:
    rev, pat, mbps = _num(rev_yoy), _num(pat_yoy), _num(margin_change_bps)
    trend = str(pat_trend or "").upper() or None
    if rev is None and pat is None and trend is None:
        return None
    pat_strong = trend == "TURNAROUND" or (pat is not None and pat >= STRONG_PAT_YOY)
    rev_strong = rev is not None and rev >= STRONG_REV_YOY
    weak = (trend in {"DETERIORATION", "LOSS_WIDENING", "PROFIT_DECLINE"}) or (rev is not None and rev < 0 and (pat is None or pat < 0))
    if weak:
        return "WEAK"
    if pat_strong and (rev_strong or (mbps is not None and mbps >= 100)):
        return "STRONG"
    if pat_strong or rev_strong:
        return "AVERAGE+"
    return "AVERAGE"


BUCKETS = {
    "CONFIRMATION": ("Confirmation", "Strong Q1 that held, confirmed again in Q2."),
    "RE_PEAD": ("Re-PEAD", "Q1 setup faded; Q2 confirms it was not a one-off."),
    "FRESH_PEAD": ("Fresh PEAD", "Average Q1, sudden Q2 earnings pivot."),
    "NO_CONFIRMATION": ("Not confirmed", "Q2 numbers did not confirm."),
    "WATCH_CONFIRM": ("Watch: Q1 held", "Q1 setup sustained. Needs Q2 confirmation."),
    "WATCH_REPEAD": ("Watch: re-PEAD", "Q1 setup faded. Strong Q2 could restart the drift."),
    "WATCH_FRESH": ("Watch: fresh", "No Q1 setup. Only a sudden Q2 pivot counts."),
    "PENDING": ("Q2 data pending", "Q2 numbers not verified yet."),
}


def q1_setup(q1_return: Any, q1_rvol: Any, q1_strength: str | None = None) -> bool | None:
    r, v = _num(q1_return), _num(q1_rvol)
    if q1_strength == "STRONG":
        return True
    if r is None:
        return None
    return bool(r >= Q1_SETUP_RETURN and (v is None or v >= Q1_SETUP_RVOL))


def classify_bucket(*, released: bool, q2_strength: str | None, setup: bool | None, sustained: bool | None) -> dict[str, str] | None:
    if not released:
        if setup is None:
            return None
        code = "WATCH_FRESH" if not setup else ("WATCH_CONFIRM" if sustained else "WATCH_REPEAD")
    elif q2_strength is None:
        code = "PENDING"
    elif q2_strength in {"WEAK", "AVERAGE"}:
        code = "NO_CONFIRMATION"
    elif setup is None:
        code = "FRESH_PEAD" if q2_strength == "STRONG" else "PENDING"
    elif setup and sustained:
        code = "CONFIRMATION"
    elif setup:
        code = "RE_PEAD"
    else:
        code = "FRESH_PEAD" if q2_strength == "STRONG" else "NO_CONFIRMATION"
    label, why = BUCKETS[code]
    return {"code": code, "label": label, "why": why}


# ---------------------------------------------------------------------------
# Trade plan
# ---------------------------------------------------------------------------

ACTIONABLE = {"ENTRY_EARLY", "ENTRY_PULLBACK", "ENTRY_BREAKOUT", "NEAR_ENTRY", "PULLBACK_ZONE"}


def trade_plan(*, released: bool, reaction_traded: bool, quality_ok: bool | None, x: dict[str, Any],
               last_price: Any, result_return: Any, rvol: Any, result_low: Any, result_high: Any,
               box_high: Any = None) -> dict[str, Any]:
    """Mechanical plan for one event. Levels are published only when the
    signal is actionable; otherwise the plan explains what it is waiting for."""
    last = _num(last_price)
    plan: dict[str, Any] = {"signal": None, "why": None, "entry": None, "sl": None, "riskPct": None,
                            "tslSwing": None, "tslPosition": None, "rNow": None, "style": None}

    def done(signal: str, why: str) -> dict[str, Any]:
        plan["signal"], plan["why"] = signal, why
        return plan

    if not released:
        return done("WAIT_RESULT", "Results not declared yet.")
    if not reaction_traded:
        return done("WAIT_REACTION", "Waiting for the first trading session after the result.")
    if last is None or _num(result_low) is None:
        return done("DATA_PENDING", "Price history for the reaction session is not available yet.")
    if quality_ok is False:
        return done("NO_ENTRY", "Earnings weak/average or the price response was negative.")
    if quality_ok is None:
        return done("DATA_PENDING", "Earnings growth not verified yet: entry levels stay hidden until YoY numbers are available.")
    if x.get("base_broken"):
        return done("NO_ENTRY", "Price closed below the result-day low: the post-result base is broken.")

    sl = _r2(_num(result_low) * (1 - SL_BUFFER_PCT / 100))
    ema10, ema21, ema63 = _num(x.get("ema10")), _num(x.get("ema21")), _num(x.get("ema63"))
    sessions = x.get("sessions_since_reaction")
    ret, vol = _num(result_return), _num(rvol)
    r_close = _num(x.get("reaction_close"))
    trigger = None

    if sessions is not None and sessions <= EARLY_ENTRY_SESSIONS and (ret or 0) >= EARLY_MIN_RETURN and (vol or 0) >= EARLY_MIN_RVOL \
            and r_close is not None and last >= r_close * 0.99:
        trigger = _num(result_high) or r_close
        signal = "ENTRY_EARLY" if last >= trigger else "NEAR_ENTRY"
        why = "Strong reaction on high volume and the gap is holding: early entry above the result-day high."
        plan["style"] = "EARLY"
    elif _num(box_high) is not None and last >= _num(box_high):
        trigger, signal = _num(box_high), "ENTRY_BREAKOUT"
        why = "Closed above the post-result box high."
        plan["style"] = "BREAKOUT"
    elif ema21 is not None and ema21 * 0.995 <= last <= ema21 * (1 + PULLBACK_BAND_PCT / 100) \
            or (ema10 is not None and ema10 * 0.995 <= last <= ema10 * (1 + PULLBACK_BAND_PCT / 100) and (sessions or 0) >= 3):
        if x.get("reclaimed_prev_high") and _num(x.get("day_before_high")) is not None:
            trigger, signal = _num(x.get("day_before_high")), "ENTRY_PULLBACK"
            why = "Pullback to the 10/21 EMA held and price closed above the previous session high."
        else:
            trigger = max(_num(x.get("prev_session_high")) or last * 1.01, last)
            signal = "PULLBACK_ZONE"
            why = "Pulled back to the 10/21 EMA while the result base holds: enter on a move above today's high."
        plan["style"] = "PULLBACK"
    elif ema21 is not None and last > ema21 * (1 + EXTENDED_ABOVE_EMA21_PCT / 100):
        return done("EXTENDED", f"More than {EXTENDED_ABOVE_EMA21_PCT:.0f}% above the 21 EMA: wait for a pullback.")
    elif _num(box_high) is not None and _pct(_num(box_high), last) is not None and _pct(_num(box_high), last) <= NEAR_TRIGGER_PCT:
        trigger, signal = _num(box_high), "NEAR_ENTRY"
        why = "Within 3% of the post-result box high."
        plan["style"] = "BREAKOUT"
    else:
        return done("WATCH", "Base intact; no entry pattern yet (early, pullback or breakout).")

    entry = _r2(trigger)
    risk = _pct(entry, sl)
    plan.update({"entry": entry, "sl": sl, "riskPct": _r2(risk)})
    if risk is not None and risk > MAX_RISK_PCT:
        plan.update({"entry": None, "sl": None})
        return done("RISK_TOO_WIDE", f"Stop under the result-day low is {risk:.1f}% away (max {MAX_RISK_PCT:.0f}%). Wait for a tighter base.")
    per_r = entry - sl if entry is not None and sl is not None else None
    if per_r and per_r > 0:
        plan["rNow"] = _r2((last - entry) / per_r) if last >= entry else None
        # Until the trade is +1R the initial SL stands; after +1R the stop moves
        # to cost and then trails the 21 EMA (swing) or 63 EMA (position).
        if last >= entry + per_r:
            plan["tslSwing"] = _r2(max(entry, ema21)) if ema21 is not None else entry
            plan["tslPosition"] = _r2(max(entry, ema63)) if ema63 is not None else entry
        else:
            plan["tslSwing"] = plan["tslPosition"] = sl
    plan["distancePct"] = _r2(_pct(entry, last))
    return done(signal, why)


def liquidity(turnover_cr: Any, price: Any) -> dict[str, Any]:
    t, p = _num(turnover_cr), _num(price)
    if t is None and p is None:
        return {"pass": None, "label": "—"}
    ok = (t is None or t >= LIQ_TURNOVER_CR) and (p is None or p >= LIQ_MIN_PRICE)
    if t is not None and t < LIQ_TURNOVER_CR:
        label = "Illiquid: " + (f"₹{t:.2f} Cr/day" if t >= 0.01 else "under ₹1 lakh/day")
    elif p is not None and p < LIQ_MIN_PRICE:
        label = f"Penny price ₹{p:.2f}"
    else:
        label = f"₹{t:.1f} Cr/day" if t is not None else "OK"
    return {"pass": bool(ok), "label": label}


def valuation_view(pe: Any, sector_pe: Any, roe: Any, pat_yoy: Any, pb: Any = None) -> dict[str, Any]:
    pe_, spe, roe_, g = _num(pe), _num(sector_pe), _num(roe), _num(pat_yoy)
    peg = _r2(pe_ / g) if pe_ and pe_ > 0 and g and g > 0 else None
    rel = _r2(pe_ / spe) if pe_ and pe_ > 0 and spe and spe > 0 else None
    if pe_ is None or pe_ <= 0:
        label = "LOSS-MAKING" if pe_ is not None else "UNVERIFIED"
    elif pe_ > 80 or (rel is not None and rel > 2):
        label = "EXPENSIVE"
    elif (rel is not None and rel <= 0.8) or (peg is not None and peg <= 1 and (roe_ is None or roe_ >= 12)):
        label = "ATTRACTIVE"
    else:
        label = "FAIR"
    return {"label": label, "pe": _r2(pe_), "sectorPe": _r2(spe), "peVsSector": rel, "peg": peg, "roe": _r2(roe_), "pb": _r2(pb)}


# ---------------------------------------------------------------------------
# Cross-sectional: sectors, regime, scorecard
# ---------------------------------------------------------------------------

def sector_stats(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Peer-based tailwind: momentum of tracked peers vs the whole universe,
    and how peers' Q2 results came out so far."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        key = it.get("sectorKey")
        if key:
            groups.setdefault(key, []).append(it)
    all_mom = [m for it in items if (m := _num(it.get("ret63"))) is not None]
    base = median(all_mom) if all_mom else None
    out = {}
    for key, rows in groups.items():
        mom = [m for r in rows if (m := _num(r.get("ret63"))) is not None]
        declared = [r for r in rows if r.get("released")]
        strong = [r for r in declared if r.get("strength") in {"STRONG", "AVERAGE+"}]
        reacts = [v for r in declared if (v := _num(r.get("reaction"))) is not None]
        med = median(mom) if len(mom) >= 3 else None
        rs = _r2(med - base) if med is not None and base is not None else None
        beat_share = len(strong) / len(declared) if len(declared) >= 2 else None
        score = 0
        if rs is not None:
            score += 2 if rs >= 8 else 1 if rs >= 3 else -1 if rs <= -5 else 0
        if beat_share is not None:
            score += 1 if beat_share >= 0.6 else -1 if beat_share <= 0.3 else 0
        label = None if rs is None and beat_share is None else ("STRONG" if score >= 2 else "POSITIVE" if score == 1 else "WEAK" if score < 0 else "NEUTRAL")
        out[key] = {
            "sector": key, "stocks": len(rows), "momentum63dMedian": _r2(med), "relativeToMarket": rs,
            "declared": len(declared), "strongResults": len(strong),
            "avgReaction": _r2(sum(reacts) / len(reacts)) if reacts else None,
            "tailwind": label,
        }
    return out


def market_regime(index_closes: list[float] | None) -> dict[str, Any]:
    closes = [c for c in (index_closes or []) if _num(c) is not None]
    if len(closes) < 55:
        return {"label": None, "note": "Index history unavailable"}
    last = closes[-1]
    dma50 = sum(closes[-50:]) / 50
    dma200 = sum(closes[-200:]) / len(closes[-200:])
    ret20 = _pct(last, closes[-21])
    if last >= dma50 and last >= dma200:
        label = "RISK-ON"
    elif last < dma50 and last < dma200:
        label = "RISK-OFF"
    else:
        label = "MIXED"
    return {"label": label, "last": _r2(last), "dma50": _r2(dma50), "dma200": _r2(dma200), "ret20dPct": _r2(ret20),
            "note": {"RISK-ON": "Index above 50 & 200 DMA: PEAD tends to work best.",
                     "MIXED": "Index between its 50 and 200 DMA: be selective.",
                     "RISK-OFF": "Index below 50 & 200 DMA: PEAD drifts fail more often; size down."}[label]}


def scorecard(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Forward returns of past setups, grouped by Q1 setup quality."""
    groups: dict[str, list[float]] = {"Q1 setup (≥4% on volume)": [], "No Q1 setup": []}
    for it in items:
        r, ret = it.get("q1Return"), it.get("q1ToQ2")
        if _num(ret) is None or _num(r) is None:
            continue
        key = "Q1 setup (≥4% on volume)" if it.get("q1Setup") else "No Q1 setup"
        groups[key].append(float(ret))
    rows = []
    for k, vals in groups.items():
        if not vals:
            rows.append({"group": k, "n": 0, "avg": None, "median": None, "winRate": None})
            continue
        rows.append({"group": k, "n": len(vals), "avg": _r2(sum(vals) / len(vals)), "median": _r2(median(vals)),
                     "winRate": _r2(sum(v > 0 for v in vals) / len(vals) * 100)})
    return {"metric": "Return from the day before the Q1 result to the Q2 result (or today)", "rows": rows}
