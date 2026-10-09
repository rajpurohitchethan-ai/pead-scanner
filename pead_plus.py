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
MODULE_VERSION = "2.9.1"   # must equal pead_v2.ENGINE_VERSION (install check)
STARTER_FRACTION = 1 / 3       # position size taken before the concall


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
                      q1_reaction_date: date | None = None, q2_boundary: date | None = None,
                      window_start: date | None = None) -> dict[str, Any]:
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
    # Intraday filings: the reaction window starts on the filing day.
    wi = ri
    if ri is not None and window_start is not None and reaction_date is not None and window_start < reaction_date:
        w = _idx_on_or_after(f, window_start)
        wi = w if w is not None and w <= ri else ri
    chart_marker = wi
    if ri is not None and wi is not None and wi > 0:
        pre_close = _num(close.iloc[wi - 1])
        r_close = _num(close.iloc[ri])
        r_open = _num(f["Open"].iloc[wi])
        win = f.iloc[wi:ri + 1]
        r_low = _num(win["Low"].min()) if win["Low"].notna().any() else _num(win["Close"].min())
        r_high = _num(win["High"].max()) if win["High"].notna().any() else _num(win["Close"].max())
        r_low = r_low or r_close
        r_high = r_high or r_close
        after = f.iloc[wi:]
        vol_day = int(win["Volume"].idxmax()) if win["Volume"].notna().any() else ri
        out.update(_reaction_volume_flags(f, vol_day))
        out["reaction_window_sessions"] = int(ri - wi + 1)
        out.update({
            "reaction_close": _r2(r_close),
            "reaction_open": _r2(r_open),
            "gap_pct": _r2(_pct(r_open, pre_close)) if r_open is not None else None,
            "gap_held": (bool(r_close >= r_open) if r_open is not None and r_close is not None else None),
            "close_in_range_pct": (_r2((r_close - r_low) / (r_high - r_low) * 100)
                                   if r_close is not None and r_high is not None and r_low is not None and r_high > r_low else None),
            "sessions_since_reaction": int(len(f) - 1 - ri),
            "return_since_result_pct": _r2(_pct(last, pre_close)),
            "pre_result_session": f["Date"].iloc[wi - 1].date().isoformat(),
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
               box_high: Any = None, concall: dict[str, Any] | None = None,
               red_flags: list[str] | None = None) -> dict[str, Any]:
    """Mechanical plan for one event. Levels are published only when the
    signal is actionable; otherwise the plan explains what it is waiting for.

    Concall handling (hybrid): while a call is pending, clean numbers allow a
    STARTER position (1/3 size) on a valid pattern; numbers with red flags
    wait for the call. After the call (or when no call is held) the plan is
    the FULL position."""
    last = _num(last_price)
    plan: dict[str, Any] = {"signal": None, "why": None, "entry": None, "sl": None, "riskPct": None,
                            "tslSwing": None, "tslPosition": None, "rNow": None, "style": None,
                            "stage": None, "sizeFraction": None, "redFlags": list(red_flags or [])}

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
    pending = call_pending(concall)
    when = None
    if pending:
        when = concall.get("callDate")
        try:
            when = date.fromisoformat(when).strftime("%d %b") if when else None
        except ValueError:
            pass
        if red_flags:
            return done("WAIT_CONCALL", (f"Concall on {when}: " if when else "Concall announced: ")
                        + "the numbers need explaining (" + "; ".join(red_flags) + "), so wait for the call.")
    plan["stage"] = "STARTER" if pending else "FULL"
    plan["sizeFraction"] = STARTER_FRACTION if pending else 1.0

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
    if pending:
        why = (f"Starter position (1/3 size) before the concall{' on ' + when if when else ''}; add the rest only if the call confirms. "
               + why)
    elif concall and concall.get("status") == "DONE":
        why = "Concall done: full position allowed. " + why
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
    # 2.5.3: ROE above 100% or P/B above 50 means a near-zero book value
    # (ONIXSOLAR: P/B 327, ROE 1,448%). The ratio is real but meaningless.
    pb_ = _num(pb)
    neg_book = (pb_ is not None and pb_ < 0) or (roe_ is not None and roe_ < -100)
    tiny_book = neg_book or (roe_ is not None and roe_ > 100)
    return {"label": label, "pe": _r2(pe_), "sectorPe": _r2(spe), "peVsSector": rel, "peg": peg, "roe": _r2(roe_), "pb": _r2(pb),
            "tinyBook": tiny_book, "bookNote": ("negative book value" if neg_book else "tiny book value") if tiny_book else None}


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
    if len(closes) < 200:
        # Not enough history for a 200 DMA (was silently averaging ~70 days).
        ret20 = _pct(last, closes[-21])
        label = "RISK-ON" if last >= dma50 else "RISK-OFF"
        return {"label": label, "last": _r2(last), "dma50": _r2(dma50), "dma200": None, "ret20dPct": _r2(ret20),
                "note": ("Index above its 50 DMA (200 DMA needs more history)." if label == "RISK-ON"
                         else "Index below its 50 DMA (200 DMA needs more history); size down.")}
    dma200 = sum(closes[-200:]) / 200
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


# ---------------------------------------------------------------------------
# Concalls (engine 2.5): "enter after the results AND the call"
# ---------------------------------------------------------------------------

import re as _re
from datetime import datetime as _dt, timedelta as _td

_CALL_WORDS = _re.compile(r"(con(ference)?[\s.\-]*call|earnings?\s+call|analysts?\s*/?\s*(institutional\s+)?investors?\s+meet|investor\s+call|analyst\s+meet|investors?\s+meet)", _re.I)
_TRANSCRIPT = _re.compile(r"transcript", _re.I)
_AUDIO = _re.compile(r"audio|recording|webcast\s+link|video\s+recording", _re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def classify_call_filing(text: str) -> str | None:
    """NOTICE (call scheduled), TRANSCRIPT, AUDIO, or None for other filings."""
    t = text or ""
    if not _CALL_WORDS.search(t) and not _TRANSCRIPT.search(t):
        return None
    if _TRANSCRIPT.search(t):
        return "TRANSCRIPT"
    if _AUDIO.search(t):
        return "AUDIO"
    return "NOTICE"


def call_date_from_text(text: str, filed_on: date) -> date | None:
    """First date in the filing text that falls 0-21 days after it was filed
    (the scheduled call date). Returns None rather than guessing."""
    t = _re.sub(r"\s+", " ", text or "")
    found = []

    def add(y, m, d):
        try:
            y, m, d = int(y), int(m), int(d)
            y = y + 2000 if y < 100 else y
            c = date(y, m, d)
        except (TypeError, ValueError):
            return
        if filed_on <= c <= filed_on + _td(days=21):
            found.append(c)

    for d, m, y in _re.findall(r"(?<!\d)(\d{1,2})[./\-](\d{1,2})[./\-](\d{2,4})(?!\d)", t):
        add(y, m, d)
    for d, mon, y in _re.findall(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-,]*(?:of\s+)?([A-Za-z]{3,9})[\s,.\-']*(\d{4})", t):
        if mon[:3].lower() in _MONTHS:
            add(y, _MONTHS[mon[:3].lower()], d)
    for mon, d, y in _re.findall(r"([A-Za-z]{3,9})[\s.\-]*(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})", t):
        if mon[:3].lower() in _MONTHS:
            add(y, _MONTHS[mon[:3].lower()], d)
    # A letter usually starts with its own date ("October 1, 2026"); prefer a
    # later date (the call) over the filing date itself.
    later = [c for c in found if c > filed_on]
    return min(later) if later else (min(found) if found else None)


def concall_status(filings: list[dict[str, Any]], result_date: date | None, today: date) -> dict[str, Any]:
    """filings: [{"filedAt": datetime|date, "text": str, "url": str|None}] from
    NSE/BSE announcements around the result. Only filings from 20 days before
    the result onwards count."""
    notices, transcripts, audios = [], [], []
    start = (result_date - _td(days=20)) if result_date else None
    for f in filings or []:
        filed = f.get("filedAt")
        filed_d = filed.date() if isinstance(filed, _dt) else filed
        if not isinstance(filed_d, date) or (start and filed_d < start):
            continue
        kind = classify_call_filing(f.get("text") or "")
        rec = {"filedOn": filed_d.isoformat(), "url": f.get("url"), "callDate": None}
        if kind == "NOTICE":
            d = call_date_from_text(f.get("text") or "", filed_d)
            # A "call date" before the result day is almost always the letter
            # date or another date in the text, not the call: treat as undated.
            if d and result_date and d < result_date:
                d = None
            rec["callDate"] = d.isoformat() if d else None
            notices.append(rec)
        elif kind == "TRANSCRIPT":
            transcripts.append(rec)
        elif kind == "AUDIO":
            audios.append(rec)
    out: dict[str, Any] = {"status": "NONE_FOUND", "callDate": None, "noticeUrl": None,
                           "transcriptUrl": transcripts[-1]["url"] if transcripts else None,
                           "audioUrl": audios[-1]["url"] if audios else None, "checkedOn": today.isoformat()}
    if transcripts or audios:
        out["status"] = "DONE"
    dated = [n for n in notices if n["callDate"]]
    if dated:
        n = max(dated, key=lambda r: r["callDate"])
        out.update({"callDate": n["callDate"], "noticeUrl": n["url"]})
        if out["status"] != "DONE":
            out["status"] = "SCHEDULED" if date.fromisoformat(n["callDate"]) >= today else "DONE"
    elif notices and out["status"] != "DONE":
        n = notices[-1]
        out["noticeUrl"] = n["url"]
        # Date not readable from the text. Companies usually file the notice a
        # week or more before the result and hold the call on or just after
        # the result day, so stay pending until 5 days after the notice AND
        # 2 days after the result, whichever is later (engine 2.5.3).
        until = date.fromisoformat(n["filedOn"]) + _td(days=5)
        if result_date:
            until = max(until, result_date + _td(days=2))
        out["status"] = "SCHEDULED" if until >= today else "DONE"
        out["pendingUntil"] = until.isoformat()
    return out


def recheck_concall(concall: dict[str, Any], result_date: date | None, today: date) -> dict[str, Any]:
    """An undated notice with no transcript/recording cannot prove the call
    happened; keep it pending until 2 days after the result."""
    c = dict(concall or {})
    if (c.get("status") == "DONE" and not c.get("callDate") and not c.get("transcriptUrl") and not c.get("audioUrl")
            and c.get("noticeUrl") and result_date):
        until = max(result_date + _td(days=2), date.fromisoformat(c["pendingUntil"]) if c.get("pendingUntil") else result_date)
        if until >= today:
            c.update({"status": "SCHEDULED", "pendingUntil": until.isoformat()})
    return c


def call_pending(concall: dict[str, Any] | None) -> bool:
    return bool(concall and concall.get("status") == "SCHEDULED")


def result_red_flags(*, rev_yoy: Any, pat_yoy: Any, pat_trend: Any, margin_change_bps: Any,
                     financial_status: str | None = None) -> list[str]:
    """Signs that profit may not be repeatable; any one sends the plan to
    WAIT_CONCALL when a call is pending."""
    rev, pat, mb = _num(rev_yoy), _num(pat_yoy), _num(margin_change_bps)
    flags = []
    if str(pat_trend or "").upper() == "TURNAROUND":
        flags.append("turnaround from a loss")
    if pat is not None and pat >= 50 and (rev is None or rev < 10):
        flags.append("profit growth without revenue growth")
    if mb is not None and mb >= 500 and (rev is None or rev < 10):
        flags.append("margin jump without revenue growth")
    if financial_status == "FLAGGED":
        flags.append("figures flagged for review")
    return flags


def update_trade_log(log: dict[str, Any] | None, plan: dict[str, Any], last_price: Any, today: date) -> dict[str, Any]:
    """Record the first triggered STARTER and FULL entries of an event and
    follow them (lowest price seen, stop hits, current return) so the
    scorecard can compare entering on the result with entering after the call."""
    log = dict(log or {})
    last = _num(last_price)
    sig = str(plan.get("signal") or "")
    stage = plan.get("stage")
    if sig.startswith("ENTRY_") and stage in {"STARTER", "FULL"} and plan.get("entry") is not None:
        key = stage.lower()
        if key not in log:
            log[key] = {"date": today.isoformat(), "entry": plan["entry"], "sl": plan["sl"], "signal": sig, "minSeen": last}
    for key in ("starter", "full"):
        t = log.get(key)
        if not t or last is None:
            continue
        t["minSeen"] = min(x for x in (t.get("minSeen"), last) if x is not None)
        t["last"] = last
        if not t.get("stopped") and t.get("sl") is not None and t["minSeen"] <= t["sl"]:
            t["stopped"], t["stoppedOn"] = True, today.isoformat()
        exit_px = t["sl"] if t.get("stopped") else last
        t["returnPct"] = _r2(_pct(exit_px, t["entry"]))
    return log


def entry_timing_scorecard(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, label in (("starter", "Entered on the result (before concall)"), ("full", "Entered after the concall / no call")):
        trades = [lg[key] for lg in logs if lg and lg.get(key) and _num(lg[key].get("returnPct")) is not None]
        rets = [float(t["returnPct"]) for t in trades]
        rows.append({"group": label, "n": len(rets),
                     "avg": _r2(sum(rets) / len(rets)) if rets else None,
                     "median": _r2(median(rets)) if rets else None,
                     "winRate": _r2(sum(r > 0 for r in rets) / len(rets) * 100) if rets else None,
                     "stopped": sum(bool(t.get("stopped")) for t in trades)})
    return rows


# ---------------------------------------------------------------------------
# Corporate actions (2.5.4): exchange price history is NOT split/bonus
# adjusted. CORDELIA's 1:10 split looked like a -90% crash (fake 52-week
# distance, broken EMAs and returns).
# ---------------------------------------------------------------------------

_SPLIT_FACTORS = (1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4, 1 / 5, 2 / 5, 1 / 10, 1 / 20, 1 / 25, 1 / 50, 1 / 100, 4 / 5, 3 / 5)


def adjust_corporate_actions(frame):
    """Detect overnight price steps that match a split/bonus ratio and divide
    all earlier prices by it (volumes multiplied). A step counts only when the
    close drops by >=35% to within 6% of a standard ratio AND the whole new
    session trades below the old close (gap, not an intraday collapse)."""
    if frame is None or getattr(frame, "empty", True) or len(frame) < 2:
        return frame
    f = frame.sort_values("Date").reset_index(drop=True).copy()
    closes = f["Close"].astype(float).tolist()
    highs = f["High"].tolist() if "High" in f else [None] * len(f)
    factor_after = [1.0] * len(f)
    events = []
    for i in range(1, len(f)):
        prev, cur = closes[i - 1], closes[i]
        if not prev or not cur or prev <= 0:
            continue
        r = cur / prev
        if r > 0.65:
            continue
        hi = highs[i]
        if hi is not None and hi == hi and float(hi) > prev * 0.8:
            continue
        best = min(_SPLIT_FACTORS, key=lambda k: abs(r / k - 1))
        if abs(r / best - 1) <= 0.06:
            events.append((i, best))
    if not events:
        return f
    mult = [1.0] * len(f)
    for i, k in events:
        for j in range(i):
            mult[j] *= k
    for col in ("Open", "High", "Low", "Close"):
        if col in f:
            f[col] = [None if v is None or v != v else v * m for v, m in zip(f[col].tolist(), mult)]
    if "Volume" in f:
        f["Volume"] = [None if v is None or v != v else v / m for v, m in zip(f["Volume"].tolist(), mult)]
    f.attrs["corporateActions"] = [{"index": i, "factor": round(k, 4), "date": str(f["Date"].iloc[i])[:10]} for i, k in events]
    return f


# ---------------------------------------------------------------------------
# One sector taxonomy (2.5.4): NSE's ~22 "Sector" groups. Exchange, legacy
# BSE industry and Yahoo labels were mixed ("Finance" vs "Financial
# Services", "Computers - Software" vs "Information Technology").
# ---------------------------------------------------------------------------

NSE_SECTORS = (
    "Automobile and Auto Components", "Capital Goods", "Chemicals", "Construction", "Construction Materials",
    "Consumer Durables", "Consumer Services", "Diversified", "Fast Moving Consumer Goods", "Financial Services",
    "Forest Materials", "Healthcare", "Information Technology", "Media, Entertainment & Publication",
    "Metals & Mining", "Oil, Gas & Consumable Fuels", "Power", "Realty", "Services", "Telecommunication",
    "Textiles", "Utilities",
)
_SECTOR_RULES = (
    # (keywords, sector) - first match wins; order matters
    (("telecom",), "Telecommunication"),
    (("industrial gas",), "Chemicals"),
    (("bank", "financ", "nbfc", "insurance", "capital market", "asset management", "stockbrok", "fintech",
      "investment", "broking", "credit", "lending", "wealth", "exchange", "depositor"), "Financial Services"),
    (("software", "computers", "it -", "it services", "information technology", "technology", "it enabled",
      "data processing"), "Information Technology"),
    (("pharma", "health", "hospital", "medical", "diagnost", "drug", "biotech"), "Healthcare"),
    (("tyre", "auto", "2 and 3 wheel", "4 wheel", "tractor", "commercial vehicle"), "Automobile and Auto Components"),
    (("cement", "construction material", "ceramic", "granite", "sanitary", "glass"), "Construction Materials"),
    (("real estate", "realty", "residential", "commercial projects"), "Realty"),
    (("construction", "infrastructure", "civil", "engineering & construction", "epc"), "Construction"),
    (("refiner", "oil", "gas", "lubricant", "coal", "petroleum", "energy"), "Oil, Gas & Consumable Fuels"),
    (("power", "electric utilit", "solar", "renewable"), "Power"),
    (("utilit", "water supply"), "Utilities"),
    (("steel", "metal", "alumin", "copper", "zinc", "mining", "ferrous", "iron", "ferro alloy"), "Metals & Mining"),
    (("paint", "durable", "airconditioner", "air conditioner", "gems", "jewel", "watch", "household appliance",
      "electronics - consumer", "consumer electronics", "luxury", "footwear", "furniture", "plywood"), "Consumer Durables"),
    (("fertili", "pesticide", "agrochem", "chemical", "plastic", "petrochem", "industrial gas", "dyes", "pigment",
      "basic materials", "specialty"), "Chemicals"),
    (("brew", "distill", "beverage", "food", "tea", "coffee", "sugar", "personal care", "personal product",
      "fmcg", "fast moving", "consumer defensive", "tobacco", "cigarette", "dairy", "edible oil", "household product",
      "agricultur", "packaged"), "Fast Moving Consumer Goods"),
    (("textile", "apparel", "yarn", "garment", "fabric", "cotton", "spinning"), "Textiles"),
    (("media", "entertainment", "printing", "publishing", "broadcast", "film", "advertis"), "Media, Entertainment & Publication"),
    (("hotel", "leisure", "retail", "tour", "travel", "education", "amusement", "restaurant", "consumer services",
      "e-commerce", "consumer cyclical", "recreation"), "Consumer Services"),
    (("electrical", "cable", "diesel engine", "compressor", "pump", "electrode", "industrial product",
      "industrial manufactur", "capital goods", "engineering", "ship build", "electronics - industrial",
      "defence", "aerospace", "machinery", "bearing", "industrials", "heavy electrical", "castings", "forgings"), "Capital Goods"),
    (("shipping", "logistic", "trading", "commercial service", "distributor", "transport", "rental", "airline",
      "aviation", "ports", "port &", "port services", "courier", "services"), "Services"),
    (("paper", "forest", "wood", "timber"), "Forest Materials"),
    (("diversified",), "Diversified"),
)


def canonical_sector(*labels) -> str | None:
    """Map exchange / legacy BSE / Yahoo labels to one NSE sector name."""
    for label in labels:
        v = str(label or "").strip()
        if not v or v in {"—", "-", "NA", "Miscellaneous", "Others"}:
            continue
        for s in NSE_SECTORS:
            if v.lower() == s.lower():
                return s
        low = v.lower()
        for keys, sector in _SECTOR_RULES:
            if any(_re.search(r"(?<![a-z])" + _re.escape(k), low) for k in keys):
                return sector
    return None


# ---------------------------------------------------------------------------
# Self-audit (2.6.1): the engine checks its own published output every run, so
# problems show up on the Data health tab (and logs/self_audit.json) instead
# of being found later on a screenshot.
# ---------------------------------------------------------------------------

def self_audit(items: list[dict[str, Any]], regime: dict[str, Any], health: dict[str, Any], today: date) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, bad: list[str], *, fail: bool = False, note: str = "", warn_if_any: bool = True):
        status = "OK" if not bad else ("FAIL" if fail else ("WARN" if warn_if_any else "OK"))
        checks.append({"check": name, "status": status, "count": len(bad), "examples": bad[:8], "note": note})

    def d(v):
        try:
            return date.fromisoformat(str(v)[:10])
        except Exception:
            return None

    ic = health.get("installCheck") or {}
    add("All files from the same release", [] if ic.get("ok", True) else [f"pead_plus {ic.get('pead_plus')} vs engine {ic.get('engine')}"], fail=True)
    as_of = d(regime.get("asOf"))
    add("Market index data is current", [] if as_of and (today - as_of).days <= 4 else [str(regime.get("asOf"))], fail=True)

    seen: dict[tuple, list[str]] = {}
    for it in items:
        key = (it.get("symbol"), _re.sub(r"[^a-z0-9]", "", str(it.get("name") or "").lower().replace("limited", "").replace("ltd", "")))
        seen.setdefault(key, []).append(it.get("eventId"))
    add("Each company listed once", [k[0] for k, v in seen.items() if len(v) > 1], fail=True)

    future_reaction = [it["symbol"] for it in items if not it.get("resultsReleased")
                       and (it.get("priceContext") or {}).get("resultDayPct") is not None]
    add("No reaction data before a result", future_reaction, fail=True)

    def not_trading(it):
        last = d((((it.get("plus") or {}).get("price") or {}).get("last_session")))
        return last is not None and (today - last).days > 20
    idle = [it["symbol"] for it in items if it.get("resultsReleased") and not_trading(it)]
    checks.append({"check": "Declared results of stocks that are not trading", "status": "OK", "count": len(idle),
                   "examples": idle[:8], "note": "No trades in 20+ days: no reaction or entry is possible."})
    declared = [it for it in items if it.get("resultsReleased") and not not_trading(it)]
    no_fin_old, no_fin_new, no_yoy = [], [], []
    for it in declared:
        rd = d(it.get("resultDate"))
        age = (today - rd).days if rd else 0
        fs = it.get("fundamentalSnapshot") or {}
        if fs.get("revenueCr") is None:
            (no_fin_old if age >= 2 else no_fin_new).append(it["symbol"])
        elif it.get("revenueYoY") is None and it.get("patYoYStatus") is None:
            no_yoy.append(it["symbol"])
    add("Declared results have revenue/profit (filed 2+ days ago)", no_fin_old, fail=True,
        note="Exchange filing not parsed; check the NSE_XBRL / BSE_RESULTS_SNAPSHOT fetch errors.")
    add("Declared results have revenue/profit (filed in last 2 days)", no_fin_new,
        note="Normal for a few hours after filing; the engine re-checks every run.")
    add("Declared results have year-on-year figures", no_yoy,
        note="Shown as quarter-on-quarter until the year-ago figures are found.")

    overdue = []
    for it in declared:
        rs = d(it.get("reactionSession"))
        if rs and rs < today and (it.get("priceContext") or {}).get("resultDayPct") is None:
            overdue.append(it["symbol"])
    add("Reaction measured once the session has closed", overdue, fail=True)

    no_price = [it["symbol"] for it in items if not (it.get("plus") or {}).get("price")]
    add("Every company has price history", no_price, note="Filled in rotation, new companies first.")
    stale = []
    for it in items:
        rd = d(it.get("resultDate"))
        last = d(((it.get("plus") or {}).get("price") or {}).get("last_session"))
        if rd and 0 <= (rd - today).days <= 7 and last and (today - last).days > 4:
            stale.append(it["symbol"])
    add("Prices fresh for companies reporting within 7 days", stale)
    no_sector = [it["symbol"] for it in items if not (it.get("plus") or {}).get("sectorKey")]
    share = len(no_sector) / max(1, len(items))
    checks.append({"check": "Sector known", "status": "OK" if share <= 0.10 else "WARN", "count": len(no_sector),
                   "examples": no_sector[:8], "note": f"{(1 - share) * 100:.0f}% known; exchange data is fetched in rotation."})

    odd = [it["symbol"] for it in items if (_num((it.get("priceContext") or {}).get("resultDayPct")) or 0) and abs(_num(it["priceContext"]["resultDayPct"])) > 40]
    odd += [it["symbol"] for it in declared if (_num(it.get("revenueYoY")) or 0) > 1000]
    add("No implausible values (>40% reaction, >1000% revenue growth)", odd, note="Usually a split or a unit error in the filing.")

    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else ("WARN" if any(c["status"] == "WARN" for c in checks) else "OK")
    return {"status": status, "checkedOn": today.isoformat(), "checks": checks}



# ---------------------------------------------------------------------------
# Engine 2.7.0: earnings acceleration, relative strength, live entry status
# ---------------------------------------------------------------------------

ACCEL_PP = 5.0   # percentage-point change in YoY growth that counts as speeding up / slowing down


def earnings_acceleration(rev_yoy: Any, pat_yoy: Any, prev_rev_yoy: Any, prev_pat_yoy: Any,
                          pat_trend: Any = None) -> dict[str, Any]:
    """Is YoY growth faster this quarter than last quarter? Uses revenue and
    profit growth; profit counts only when both quarters have an ordinary YoY
    (no turnaround / loss quarters)."""
    r, p, pr, pp = _num(rev_yoy), _num(pat_yoy), _num(prev_rev_yoy), _num(prev_pat_yoy)
    rd = _r2(r - pr) if r is not None and pr is not None else None
    pdl = _r2(p - pp) if p is not None and pp is not None and str(pat_trend or "").upper() != "TURNAROUND" else None
    deltas = [d for d in (rd, pdl) if d is not None]
    if not deltas:
        return {"label": None, "revenueDeltaPp": rd, "profitDeltaPp": pdl, "prevRevenueYoY": pr, "prevProfitYoY": pp}
    growing = (r is None or r > 0) and (p is None or p > 0)
    if all(d >= ACCEL_PP for d in deltas) and growing:
        label = "ACCELERATING"
    elif all(d <= -ACCEL_PP for d in deltas):
        label = "DECELERATING"
    else:
        label = "STEADY"
    return {"label": label, "revenueDeltaPp": rd, "profitDeltaPp": pdl, "prevRevenueYoY": pr, "prevProfitYoY": pp}


def relative_strength(stock_return_pct: Any, start: Any, end: Any, dates: list[str] | None,
                      closes: list[float] | None) -> dict[str, Any]:
    """Stock return since the result vs NIFTY 500 over the same sessions
    (close before the result -> latest close)."""
    sr = _num(stock_return_pct)
    if sr is None or not start or not end or not dates or not closes:
        return {"indexReturnPct": None, "relativePct": None}
    idx = {d: c for d, c in zip(dates, closes)}
    s0 = max((d for d in idx if d <= str(start)), default=None)
    s1 = max((d for d in idx if d <= str(end)), default=None)
    if not s0 or not s1 or s1 < s0:
        return {"indexReturnPct": None, "relativePct": None}
    ir = _pct(idx[s1], idx[s0])
    return {"indexReturnPct": _r2(ir), "relativePct": _r2(sr - ir) if ir is not None else None, "indexAsOf": s1}


NEAR_ENTRY_PCT = 2.0


def live_entry_status(plan: dict[str, Any] | None, live: dict[str, Any] | None) -> dict[str, Any] | None:
    """Intraday price against the plan's entry trigger (for the 3 pm alert)."""
    if not live or _num(live.get("price")) is None:
        return None
    price = _num(live["price"])
    entry = _num((plan or {}).get("entry"))
    out = {"price": _r2(price), "at": live.get("at"), "entry": _r2(entry), "distancePct": None, "state": None}
    if entry:
        dist = (price - entry) / entry * 100
        out["distancePct"] = _r2(dist)
        out["state"] = "ABOVE_TRIGGER" if dist >= 0 else ("NEAR_TRIGGER" if dist >= -NEAR_ENTRY_PCT else "BELOW_TRIGGER")
    return out


# ---------------------------------------------------------------------------
# Engine 2.8.0: peers ("who moved, who lags") and the top-down checklist
# (market -> sector -> peers -> trend -> momentum -> valuation).
# ---------------------------------------------------------------------------

MIN_PEERS = 4


def peer_context(items: list[dict[str, Any]], regime: dict[str, Any] | None) -> None:
    """Adds plus.peers and plus.topDown to every item (in place). Uses only
    published fields: sector, 3-month return, reactions of peers that already
    reported, EMAs, relative strength and valuation."""
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        key = (it.get("plus") or {}).get("sectorKey")
        if key:
            by_sector.setdefault(key, []).append(it)

    def ret63(it):
        return _num(((it.get("plus") or {}).get("price") or {}).get("ret_63d_pct"))

    for key, members in by_sector.items():
        ranked = sorted([m for m in members if ret63(m) is not None], key=ret63, reverse=True)
        med = median([ret63(m) for m in ranked]) if ranked else None
        reported = [m for m in members if m.get("resultsReleased") and _num((m.get("priceContext") or {}).get("resultDayPct")) is not None]
        reported.sort(key=lambda m: str(m.get("resultDate") or ""), reverse=True)
        for it in members:
            plus = it.setdefault("plus", {})
            r = ret63(it)
            role, rank = None, None
            if r is not None and len(ranked) >= MIN_PEERS:
                rank = next(i for i, m in enumerate(ranked) if m is it) + 1
                third = len(ranked) / 3
                role = "LEADER" if rank <= third else ("LAGGARD" if rank > len(ranked) - third else "MIDDLE")
            others = [m for m in reported if m is not it]
            reactions = [_num(m["priceContext"]["resultDayPct"]) for m in others]
            plus["peerFundamentals"] = peer_fundamentals(it, members)
            plus["peers"] = {
                "sector": key, "count": len(ranked), "rank": rank, "role": role,
                "ret63": _r2(r), "sectorMedian63": _r2(med),
                "leaders": [{"symbol": m.get("symbol"), "ret63": _r2(ret63(m))} for m in ranked[:3] if m is not it],
                "laggards": [{"symbol": m.get("symbol"), "ret63": _r2(ret63(m))} for m in ranked[-3:][::-1] if m is not it],
                "reported": [{"symbol": m.get("symbol"), "reaction": _r2(_num(m["priceContext"]["resultDayPct"])),
                              "date": m.get("resultDate"), "strength": (m.get("plus") or {}).get("strength")} for m in others[:4]],
                "reportedCount": len(others),
                "reportedAvgReaction": _r2(sum(reactions) / len(reactions)) if reactions else None,
            }
    for it in items:
        it.setdefault("plus", {})["topDown"] = top_down(it, regime)


def top_down(it: dict[str, Any], regime: dict[str, Any] | None) -> dict[str, Any]:
    plus = it.get("plus") or {}
    px = plus.get("price") or {}
    checks = []

    def add(key, label, status, note):
        checks.append({"key": key, "label": label, "status": status, "note": note})

    lab = (regime or {}).get("label")
    extra = []
    br = ((regime or {}).get("breadth") or {}).get("above63Pct")
    if br is not None:
        extra.append(f"{br:.0f}% of tracked stocks above their 63-day average")
    fl = (regime or {}).get("flows") or {}
    if fl.get("fiiStreak"):
        extra.append(f"FIIs net {'buyers' if fl['fiiStreak'] > 0 else 'sellers'} {abs(fl['fiiStreak'])} day(s) running")
    add("market", "Market", {"RISK-ON": "ok", "MIXED": "warn", "RISK-OFF": "bad"}.get(lab, "na"),
        {"RISK-ON": "NIFTY 500 above its 50 & 200 DMA", "MIXED": "NIFTY 500 between its 50 & 200 DMA",
         "RISK-OFF": "NIFTY 500 below its 50 & 200 DMA"}.get(lab, "Index data pending") + ("; " + "; ".join(extra) if extra else ""))
    tail = (plus.get("sector") or {}).get("tailwind")
    add("sector", "Sector", {"STRONG": "ok", "POSITIVE": "ok", "NEUTRAL": "warn", "WEAK": "bad"}.get(tail, "na"),
        f"Sector tailwind {tail.lower()}" if tail else "Sector not known yet")
    pe = plus.get("peers") or {}
    role, avg = pe.get("role"), _num(pe.get("reportedAvgReaction"))
    if role is None:
        add("peers", "Peers", "na", "Not enough tracked peers")
    elif avg is not None and avg < 0:
        add("peers", "Peers", "bad", f"Peers that reported fell {avg:+.1f}% on average")
    elif role == "LAGGARD":
        add("peers", "Peers", "ok", f"Laggard ({pe.get('rank')} of {pe.get('count')}): room to catch up")
    elif role == "LEADER":
        add("peers", "Peers", "warn", f"Leader ({pe.get('rank')} of {pe.get('count')}): already moved")
    else:
        add("peers", "Peers", "ok" if avg is not None and avg > 0 else "warn",
            f"Middle of the pack ({pe.get('rank')} of {pe.get('count')})")
    last = _num((it.get("priceContext") or {}).get("lastClose"))
    e21, e63 = _num(px.get("ema21")), _num(px.get("ema63"))
    if last is None or e21 is None or e63 is None:
        add("trend", "Trend", "na", "Price history pending")
    elif last > e21 > e63:
        add("trend", "Trend", "ok", "Uptrend: price above 21 and 63 EMA")
    elif last < e21 < e63:
        add("trend", "Trend", "bad", "Downtrend: price below 21 and 63 EMA")
    else:
        add("trend", "Trend", "warn", "Sideways: price between its averages")
    rs = _num((plus.get("relativeStrength") or {}).get("relativePct"))
    r21 = _num(px.get("ret_21d_pct"))
    if rs is not None:
        add("momentum", "Momentum", "ok" if rs > 0 else "bad", f"{rs:+.1f}% vs NIFTY 500 since the result")
    elif r21 is not None:
        add("momentum", "Momentum", "ok" if r21 > 0 else ("bad" if r21 < -5 else "warn"), f"{r21:+.1f}% over 1 month")
    else:
        add("momentum", "Momentum", "na", "Price history pending")
    v = plus.get("valuation") or {}
    vl = v.get("label")
    add("valuation", "Valuation", {"ATTRACTIVE": "ok", "FAIR": "ok", "EXPENSIVE": "bad", "LOSS-MAKING": "bad"}.get(vl, "na"),
        {"ATTRACTIVE": "Cheap vs sector / growth", "FAIR": "Reasonable vs sector",
         "EXPENSIVE": "Expensive vs sector", "LOSS-MAKING": "Loss-making"}.get(vl, "P/E not available"))
    ok = sum(c["status"] == "ok" for c in checks)
    known = sum(c["status"] != "na" for c in checks)
    return {"checks": checks, "passed": ok, "known": known}



# ---------------------------------------------------------------------------
# Engine 2.9.0: market internals (breadth, FII/DII) and peer fundamentals
# ---------------------------------------------------------------------------

def market_internals(items: list[dict[str, Any]], internals: dict[str, Any] | None, today: date) -> dict[str, Any]:
    """Breadth from the tracked universe (share of stocks above their 21 / 63
    day EMA), NIFTY 500 advances/declines today, and FII/DII cash flows."""
    above21 = above63 = n = 0
    for it in items:
        px = (it.get("plus") or {}).get("price") or {}
        last = _num((it.get("priceContext") or {}).get("lastClose"))
        e21, e63 = _num(px.get("ema21")), _num(px.get("ema63"))
        if last is None or e21 is None or e63 is None:
            continue
        n += 1
        above21 += last > e21
        above63 += last > e63
    out: dict[str, Any] = {"breadth": {"n": n, "above21Pct": _r2(above21 / n * 100) if n else None,
                                       "above63Pct": _r2(above63 / n * 100) if n else None}}
    internals = internals or {}
    ad = internals.get("advanceDecline") or {}
    at = str(ad.get("at") or "")[:10]
    if ad and at and (today - date.fromisoformat(at)).days <= 3:
        tot = (ad.get("advances") or 0) + (ad.get("declines") or 0)
        out["advanceDecline"] = {**ad, "advancePct": _r2(ad["advances"] / tot * 100) if tot else None}
    flows = [f for f in internals.get("flows") or [] if isinstance(f, dict) and f.get("date")]
    flows.sort(key=lambda f: f["date"])
    if flows and (today - date.fromisoformat(flows[-1]["date"])).days <= 5:
        last5 = flows[-5:]
        fii = [_num(f.get("fiiNetCr")) for f in flows if _num(f.get("fiiNetCr")) is not None]
        streak = 0
        if fii:
            sign = 1 if fii[-1] > 0 else -1
            for v in reversed(fii):
                if (v > 0) == (sign > 0) and v != 0:
                    streak += 1
                else:
                    break
            streak *= sign
        out["flows"] = {"date": flows[-1]["date"], "fiiNetCr": flows[-1].get("fiiNetCr"), "diiNetCr": flows[-1].get("diiNetCr"),
                        "fii5dCr": _r2(sum(_num(f.get("fiiNetCr")) or 0 for f in last5)),
                        "dii5dCr": _r2(sum(_num(f.get("diiNetCr")) or 0 for f in last5)),
                        "days": len(last5), "fiiStreak": streak}
    return out


def _rank_block(value: Any, values: list[float], higher_is_better: bool = True) -> dict[str, Any] | None:
    v = _num(value)
    vals = [x for x in values if x is not None]
    if v is None or len(vals) < 3:
        return None
    ordered = sorted(vals, reverse=higher_is_better)
    return {"value": _r2(v), "median": _r2(median(vals)), "rank": ordered.index(v) + 1, "of": len(vals)}


def peer_fundamentals(it: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    """This company's growth, margin and ROE against its sector peers.
    Growth compares companies that have reported THIS quarter; margin (TTM
    operating margin) and ROE compare every peer with exchange data."""
    def g(m, key):
        return _num(m.get(key))
    reported = [m for m in members if m.get("resultsReleased")]
    def opm(m):
        mg = (m.get("plus") or {}).get("margins") or {}
        return _num(mg.get("opmTtm")) if _num(mg.get("opmTtm")) is not None else _num(mg.get("opm"))
    def roe(m):
        v = (m.get("plus") or {}).get("valuation") or {}
        return None if v.get("tinyBook") else _num(v.get("roe"))
    out = {
        "revenueYoY": _rank_block(g(it, "revenueYoY"), [g(m, "revenueYoY") for m in reported]) if it.get("resultsReleased") else None,
        "patYoY": _rank_block(g(it, "patYoY"), [g(m, "patYoY") for m in reported]) if it.get("resultsReleased") else None,
        "margin": _rank_block(opm(it), [opm(m) for m in members]),
        "roe": _rank_block(roe(it), [roe(m) for m in members]),
    }
    return {k: v for k, v in out.items() if v is not None}
