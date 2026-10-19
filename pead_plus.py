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
MODULE_VERSION = "2.9.25"   # must equal pead_v2.ENGINE_VERSION (install check)
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
    vol = tail["Volume"].tolist() if "Volume" in tail else []
    vols = [int(v) if v is not None and v == v else None for v in vol]      # 2.9.23: volume bars
    return {"from": tail["Date"].iloc[0].date().isoformat(), "to": tail["Date"].iloc[-1].date().isoformat(),
            "close": closes, "ema21": ema21, "reactionIndex": marker, "volume": vols}


def extended_features(frame: Any, reaction_date: date | None, *, released: bool,
                      q1_reaction_date: date | None = None, q2_boundary: date | None = None,
                      window_start: date | None = None, q1_window_start: date | None = None) -> dict[str, Any]:
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
        # 2.9.21: the previous session, to recognise a breakout day out of the pullback zone
        "prev_close": _r2(close.iloc[-2]) if len(close) >= 2 else None,
        "prev_ema10": _ema(close.iloc[:-1], 10) if len(close) > 11 else None,
        "prev_ema21": _ema(close.iloc[:-1], 21) if len(close) > 22 else None,
        "ret_5d_pct": _r2(_pct(last, close.iloc[-6])) if len(f) > 6 else None,          # 2.9.23
        "d5_from": f["Date"].iloc[-6].date().isoformat() if len(f) > 6 else None,
        "d21_from": f["Date"].iloc[-22].date().isoformat() if len(f) > 22 else None,
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
    if ri is not None and (f["Date"].iloc[ri].date() - reaction_date).days > 5:
        ri = None                         # 2.9.6: reaction session missing from the data
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
    # 2.9.4: an intraday Q1 filing has a two-session window, like Q2.
    qw = qi
    if qi is not None and q1_window_start is not None and q1_reaction_date is not None and q1_window_start < q1_reaction_date:
        w = _idx_on_or_after(f, q1_window_start)
        qw = w if w is not None and w <= qi else qi
    # 2.9.6: no Q1 replay across a hole in the price history (FAIRCHEMOR,
    # TIGERLOGS: the "Q1 reaction" was measured from a close months earlier).
    if qi is not None and qw is not None and qw > 0:
        d0, d1 = f["Date"].iloc[qw - 1], f["Date"].iloc[qi]
        q1_date = q1_reaction_date
        if q1_date is not None and (d1.date() - q1_date).days > 5:
            qi = None                     # first session found is far after the real reaction day
        elif (d1 - d0).days > 10 + (qi - qw + 1) * 3:
            qi = None
    if qi is not None and qw is not None and qw > 0:
        q_pre = _num(close.iloc[qw - 1])
        q_close = _num(close.iloc[qi])
        vol_prior = f["Volume"].iloc[max(0, qw - 20):qw].dropna()
        win_vol = f["Volume"].iloc[qw:qi + 1].dropna()
        q_vol = _num(win_vol.max()) if not win_vol.empty else None
        q_rvol = (q_vol / vol_prior.mean()) if len(vol_prior) >= 10 and vol_prior.mean() > 0 and q_vol else None
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
    out.update(favour_signals(f))                  # 2.9.20
    out["pressure"] = selling_pressure(f)          # 2.9.15
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


def classify_bucket(*, released: bool, q2_strength: str | None, setup: bool | None, sustained: bool | None,
                    q1_strength: str | None = "UNSPECIFIED") -> dict[str, str] | None:
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
    # 2.9.2: "Average Q1" only when Q1's numbers are known (POONAWALLA's Q1
    # was unknown to the engine, yet the card said "Average Q1").
    if code == "FRESH_PEAD" and q1_strength is None:
        why = "Q1 numbers not verified and no Q1 price setup; Q2 earnings pivot."
    return {"code": code, "label": label, "why": why}


# ---------------------------------------------------------------------------
# Trade plan
# ---------------------------------------------------------------------------

ACTIONABLE = {"ENTRY_EARLY", "ENTRY_PULLBACK", "ENTRY_BREAKOUT", "ENTRY_TRIGGERED", "NEAR_ENTRY", "PULLBACK_ZONE"}


BOX_SESSIONS = 5               # post-result box = reaction day + 5 sessions (pead_v2.price_metrics)


def _prev_in_pullback_zone(x: dict[str, Any]) -> bool:
    pc, e10, e21 = _num(x.get("prev_close")), _num(x.get("prev_ema10")), _num(x.get("prev_ema21"))
    if pc is None:
        return False
    band = 1 + PULLBACK_BAND_PCT / 100
    in21 = e21 is not None and e21 * 0.995 <= pc <= e21 * band
    in10 = e10 is not None and e10 * 0.995 <= pc <= e10 * band and (x.get("sessions_since_reaction") or 0) >= 4
    return in21 or in10


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
        # 2.9.11: "above" the result-day high; a close AT the high (HCLTECH 13 Oct,
        # close 1326 = high 1326) is near entry, triggered on a move above it.
        signal = "ENTRY_EARLY" if last > trigger else "NEAR_ENTRY"
        why = "Strong reaction on high volume and the gap is holding: early entry above the result-day high."
        plan["style"] = "EARLY"
    elif _num(box_high) is not None and last > _num(box_high):
        # 2.9.17: strictly ABOVE the box high, like the early entry (2.9.11).
        # VIJIFIN 15 Oct closed AT its one-session box high (locked circuit).
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
    elif x.get("reclaimed_prev_high") and _num(x.get("day_before_high")) is not None and _prev_in_pullback_zone(x):
        # 2.9.21: yesterday sat in the pullback zone and today closed above
        # yesterday's high. A strong breakout day closes well clear of the
        # EMAs, so the zone test above misses it (PREMIERPOL 16 Oct: 111.51
        # vs 10 EMA 105.25 -> "no entry pattern yet").
        trigger, signal = _num(x.get("day_before_high")), "ENTRY_PULLBACK"
        why = "Pulled back to the 10/21 EMA, then closed above the pullback day's high."
        plan["style"] = "PULLBACK"
    elif ema21 is not None and last > ema21 * (1 + EXTENDED_ABOVE_EMA21_PCT / 100):
        return done("EXTENDED", f"More than {EXTENDED_ABOVE_EMA21_PCT:.0f}% above the 21 EMA: wait for a pullback.")
    elif _num(box_high) is not None and _pct(_num(box_high), last) is not None and _pct(_num(box_high), last) <= NEAR_TRIGGER_PCT:
        trigger, signal = _num(box_high), "NEAR_ENTRY"
        why = "Within 3% of the post-result box high."
        if sessions is not None and sessions < BOX_SESSIONS:
            # 2.9.21: the box is the reaction day + 5 sessions; until then a new
            # high raises the trigger (GROWW 200.80 -> 204.00 on 16 Oct).
            why += (f" The box is still forming (session {sessions + 1} of {BOX_SESSIONS + 1}): "
                    "a new high raises the trigger.")
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
    # 2.9.4: exchanges send 0 for "not available" (NSE sector P/E for RALLIS,
    # BSE ROE for TIAANC); missing is not zero.
    spe = spe if spe is not None and spe > 0 else None
    roe_ = None if roe_ == 0 else roe_
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
    # 2.9.2: a P/E of 0 / negative is "loss-making", never a number to show.
    return {"label": label, "pe": _r2(pe_) if pe_ is not None and pe_ > 0 else None, "sectorPe": _r2(spe), "peVsSector": rel, "peg": peg, "roe": _r2(roe_), "pb": _r2(pb),
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
        # 2.9.3 (owner): results of peers failing the liquidity test (untraded
        # shells) do not count towards how the sector's results came out.
        declared = [r for r in rows if r.get("released") and r.get("liquid") is not False]
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
                "ret63dPct": _r2(_pct(last, closes[-64])) if len(closes) >= 64 else None,
                "note": ("Index above its 50 DMA (200 DMA needs more history)." if label == "RISK-ON"
                         else "Index below its 50 DMA (200 DMA needs more history); size down.")}
    dma200 = sum(closes[-200:]) / 200
    ret20 = _pct(last, closes[-21])
    ret63 = _pct(last, closes[-64]) if len(closes) >= 64 else None
    if last >= dma50 and last >= dma200:
        label = "RISK-ON"
    elif last < dma50 and last < dma200:
        label = "RISK-OFF"
    else:
        label = "MIXED"
    return {"label": label, "last": _r2(last), "dma50": _r2(dma50), "dma200": _r2(dma200), "ret20dPct": _r2(ret20),
            "ret63dPct": _r2(ret63),
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


RUNUP_BUCKETS = (("Below 0% (cool / pullback)", None, 0.0), ("0% to 10% (tight base)", 0.0, 10.0),
                 ("10% to 20% (warm)", 10.0, 20.0), ("Above 20% (extended)", 20.0, None))


def runup_scorecard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """2.9.19: this quarter's declared results grouped by their 20-day run-up
    into the result. rows: {runup20, reaction, sinceResult, sessions, liquid}.
    Drift = close-to-close after the reaction session ((1 + since result) /
    (1 + reaction) - 1), only once at least one session has traded after it.
    Illiquid names and rows without a run-up or reaction are left out."""
    out = []
    for label, lo, hi in RUNUP_BUCKETS:
        reac, drift = [], []
        for r in rows:
            ru, rx = _num(r.get("runup20")), _num(r.get("reaction"))
            if ru is None or rx is None or r.get("liquid") is False or rx <= -100:
                continue
            if (lo is not None and ru < lo) or (hi is not None and ru >= hi):
                continue
            reac.append(rx)
            since, n = _num(r.get("sinceResult")), _num(r.get("sessions"))
            if since is not None and n is not None and n >= 1:
                drift.append(((1 + since / 100) / (1 + rx / 100) - 1) * 100)
        out.append({"group": label, "n": len(reac), "avgReaction": _r2(sum(reac) / len(reac)) if reac else None,
                    "nDrift": len(drift), "avgDrift": _r2(sum(drift) / len(drift)) if drift else None,
                    "medianDrift": _r2(median(drift)) if drift else None,
                    "winRate": _r2(sum(d > 0 for d in drift) / len(drift) * 100) if drift else None})
    return {"metric": "20-day run-up into the result vs the reaction and the drift after it (this quarter, liquid names)",
            "rows": out}


# ---------------------------------------------------------------------------
# Concalls (engine 2.5): "enter after the results AND the call"
# ---------------------------------------------------------------------------

import re as _re
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

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


def call_date_from_url(url: Any, filed_on: date) -> date | None:
    """2.9.6: the call date is often only in the attachment's file name
    (CRAMC "Investor_Meet_October_12_2026.pdf", "InvestorMeet_28092026.pdf").
    Only a date after the filing day counts (it is then the meeting date)."""
    if not url:
        return None
    name = str(url).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    d = call_date_from_text(_re.sub(r"[_\-]+", " ", name), filed_on)
    if d is None:
        for tok in _re.findall(r"(?<!\d)(\d{8})(?!\d)", name):
            try:
                d = date(int(tok[4:]), int(tok[2:4]), int(tok[:2]))
                break
            except ValueError:
                continue
    if d is None or d <= filed_on or (d - filed_on).days > 60:
        return None
    return d


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
            url_d = call_date_from_url(f.get("url"), filed_d)
            if url_d and result_date and url_d < result_date:
                continue   # 2.9.6: a meeting before the result (CRAMC 28 Sep investor meet)
            d = url_d or call_date_from_text(f.get("text") or "", filed_d)
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
        n = max(notices, key=lambda r: r["filedOn"])   # 2.9.6: exchanges list newest first
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
    triggered = sig.startswith("ENTRY_") and stage in {"STARTER", "FULL"} and plan.get("entry") is not None
    # 2.9.17: plans use closing prices, so every run on one day reaches the same
    # verdict. An entry recorded TODAY that this run no longer triggers came
    # from a superseded verdict (VIJIFIN's at-the-high "breakout"): withdraw it.
    for key in ("starter", "full"):
        t = log.get(key)
        if t and t.get("date") == today.isoformat() and not triggered:
            log.pop(key)
    if triggered:
        key = stage.lower()
        if key not in log:
            log[key] = {"date": today.isoformat(), "entry": plan["entry"], "sl": plan["sl"], "signal": sig, "minSeen": last}
    for key in ("starter", "full"):
        t = log.get(key)
        if not t or last is None:
            continue
        t["minSeen"] = min(x for x in (t.get("minSeen"), last) if x is not None)
        t["maxSeen"] = max(x for x in (t.get("maxSeen"), t.get("entry"), last) if x is not None)
        t["last"] = last
        if not t.get("stopped") and t.get("sl") is not None and t["minSeen"] <= t["sl"]:
            t["stopped"], t["stoppedOn"] = True, today.isoformat()
        exit_px = t["sl"] if t.get("stopped") else last
        t["returnPct"] = _r2(_pct(exit_px, t["entry"]))
    return log


def hold_triggered_entry(plan: dict[str, Any], log: dict[str, Any] | None, last_price: Any, x: dict[str, Any],
                         concall: dict[str, Any] | None, today: date) -> dict[str, Any]:
    """2.9.21: once an entry has triggered (trade log) and its stop has not
    been hit, the plan keeps that entry instead of hunting for a new pattern
    (HCLTECH's 13 Oct early entry turned into "risk too wide, wait for a
    tighter base" on 16 Oct). A fresh ENTRY_* signal today still wins.
    Stop: the entry's own stop until a close at +1R, then cost / 21 EMA
    (swing) and cost / 63 EMA (position), as in the plan."""
    last = _num(last_price)
    if str(plan.get("signal") or "").startswith("ENTRY_") or not log or last is None:
        return plan
    t = None
    for key in ("full", "starter"):
        c = log.get(key)
        if c and not c.get("stopped") and _num(c.get("entry")) is not None and _num(c.get("sl")) is not None:
            t, stage = c, key.upper()
            break
    if t is None:
        return plan
    entry, sl = _num(t["entry"]), _num(t["sl"])
    if last <= sl or entry <= sl:
        return plan
    per_r = entry - sl
    best = max(x_ for x_ in (_num(t.get("maxSeen")), last, entry) if x_ is not None)
    one_r = best >= entry + per_r
    e21, e63 = _num(x.get("ema21")), _num(x.get("ema63"))
    tsl_s = _r2(max(entry, e21) if e21 is not None else entry) if one_r else sl
    tsl_p = _r2(max(entry, e63) if e63 is not None else entry) if one_r else sl
    try:
        on = date.fromisoformat(str(t.get("date"))[:10]).strftime("%d %b")
    except ValueError:
        on = str(t.get("date"))
    kind = {"ENTRY_EARLY": "early", "ENTRY_PULLBACK": "pullback", "ENTRY_BREAKOUT": "breakout"}.get(t.get("signal"), "")
    where = ("above the entry" if last >= entry else f"below the entry ({_pct(last, entry):+.1f}%) but above the stop")
    why = (f"{'Starter (1/3) ' if stage == 'STARTER' else ''}{kind + ' ' if kind else ''}entry triggered on {on} above "
           f"{entry:,.2f}; now {where}. Hold while the close stays above "
           + (f"the trail {tsl_s:,.2f} (+1R reached)." if one_r else f"the stop {sl:,.2f}; +1R at {entry + per_r:,.2f}."))
    if stage == "STARTER" and (concall or {}).get("status") == "DONE":
        why += " Concall done: the rest can be added on the next entry signal."
    elif stage == "STARTER":
        why += " Add the rest only if the concall confirms."
    held = dict(plan)
    held.update({"signal": "ENTRY_TRIGGERED", "why": why, "entry": _r2(entry), "sl": _r2(sl),
                 "riskPct": _r2(_pct(entry, sl)), "tslSwing": tsl_s, "tslPosition": tsl_p,
                 "rNow": _r2((last - entry) / per_r), "distancePct": _r2(_pct(entry, last)),
                 "stage": stage, "sizeFraction": STARTER_FRACTION if stage == "STARTER" else 1.0,
                 "triggeredOn": t.get("date"), "triggeredBy": t.get("signal"), "oneRHit": one_r})
    return held


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
    # 2.9.4: a run that ran out of time or lost a source still publishes; say so.
    rg = health.get("runGuard") or {}
    guard_bad = ([f"stopped after {rg.get('budgetMin')} min"] if rg.get("budgetHit") else []) + \
        [f"{h} switched off ({why})" for h, why in (rg.get("sourcesDown") or {}).items()]
    add("Last run finished in time with every source answering", guard_bad,
        note="Data already stored is kept; skipped companies are refreshed next hour.")
    loss_states = {"TURNAROUND", "DETERIORATION", "LOSS_NARROWING", "LOSS_WIDENING", "LOSS_FLAT"}
    loss_pct = [it.get("symbol") for it in items
                if (it.get("patYoY") is not None and it.get("patYoYStatus") in loss_states)
                or ((((it.get("plus") or {}).get("margins") or {}).get("patQoQ") is not None)
                    and (((it.get("plus") or {}).get("margins") or {}).get("patQoQStatus") in loss_states))]
    add("No profit % shown across a loss", loss_pct, fail=True)
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
    no_fin_old, no_fin_new, no_yoy, half, suspect = [], [], [], [], []
    for it in declared:
        rd = d(it.get("resultDate"))
        age = (today - rd).days if rd else 0
        fs = it.get("fundamentalSnapshot") or {}
        if fs.get("revenueCr") is None:
            if it.get("halfYearOnly"):
                half.append(it["symbol"])
                continue
            if set(it.get("financialIssues") or []) & {"UNIT_SUSPECT", "EXTREME_REVENUE_YOY", "NEGATIVE_REVENUE", "PERIOD_MISMATCH"}:
                suspect.append(it["symbol"])   # 2.9.6: read, then withheld on purpose
                continue
            (no_fin_old if age >= 2 else no_fin_new).append(it["symbol"])
        elif it.get("revenueYoY") is None and it.get("patYoYStatus") is None:
            no_yoy.append(it["symbol"])
    # 2.9.11: red only for stocks that could be trade candidates; unpriced or
    # illiquid scrips (TYPHOON: BSE XT group, no price history) are a warning.
    def candidate(sym):
        it = by_sym.get(sym) or {}
        pl = it.get("plus") or {}
        return bool(pl.get("price")) and (pl.get("liquidity") or {}).get("pass") is not False
    by_sym = {it.get("symbol"): it for it in declared}
    minor_fin = [x for x in no_fin_old if not candidate(x)]
    no_fin_old = [x for x in no_fin_old if candidate(x)]
    add("Declared results have revenue/profit (filed 2+ days ago)", no_fin_old, fail=True,
        note="Exchange filing not parsed; check the NSE_XBRL / BSE_RESULTS_SNAPSHOT fetch errors.")
    add("Illiquid or unpriced results not parsed (2+ days)", minor_fin,
        note="Not trade candidates (no price history or below the liquidity test); filled in when the exchange posts the table.")
    add("Declared results have revenue/profit (filed in last 2 days)", no_fin_new,
        note="Normal for a few hours after filing; the engine re-checks every run.")
    add("Declared results withheld as suspect figures", suspect,
        note="Read from the filing but held back (looks like a unit error or an impossible jump); check the filing.")
    checks.append({"check": "Declared results with only half-year figures", "status": "OK", "count": len(half),
                   "examples": half[:8], "note": "The filing has no quarter-only numbers; nothing to parse."})
    add("Declared results have year-on-year figures", no_yoy,
        note="Shown as quarter-on-quarter until the year-ago figures are found.")

    overdue = []
    for it in declared:
        rs = d(it.get("reactionSession"))
        if rs and rs < today and (it.get("priceContext") or {}).get("resultDayPct") is None:
            overdue.append(it["symbol"])
    add("Reaction measured once the session has closed", [x for x in overdue if candidate(x)], fail=True)
    add("Reaction not measurable (no price history or illiquid)", [x for x in overdue if not candidate(x)],
        note="No usable prices for the reaction session; not a trade candidate.")

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
    # 2.9.23: an operating margin outside +/-100% (AURUM 8150%, DLF 193%) is a unit
    # or other-income artefact; it must be hidden (n/m), never ranked.
    odd += [it["symbol"] for it in items if any(abs(_num(((it.get("plus") or {}).get("margins") or {}).get(k)) or 0) > MARGIN_LIMIT_PCT
                                                for k in ("opm", "opmTtm"))]
    add("No implausible values (>40% reaction, >1000% revenue growth, margin beyond 100%)", odd, note="Usually a split or a unit error in the filing.")

    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else ("WARN" if any(c["status"] == "WARN" for c in checks) else "OK")
    return {"status": status, "checkedOn": today.isoformat(), "checks": checks}



MARGIN_LIMIT_PCT = 100.0


def plausible_margin(value: Any) -> tuple[float | None, str | None]:
    """2.9.23: (margin, note). Beyond +/-100% the figure is not meaningful
    (other income, unit or denominator mismatch): returns (None, 'n/m ...')."""
    v = _num(value)
    if v is None:
        return None, None
    if abs(v) > MARGIN_LIMIT_PCT:
        return None, f"n/m (exchange figure {v:,.1f}%: other income or a unit mismatch)"
    return v, None


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


def live_quote_current(live_at: Any, last_session: str | None, now: Any) -> bool:
    """2.9.13: an intraday quote is the latest price only until the day's close
    is stored. It must be from today (IST); after 15:45 IST, a stored session
    on or after the quote's day supersedes it (HCLTECH 14 Oct: a 15:19 quote
    1,327.80 was shown at 20:21 next to the 1,327.00 close)."""
    if not live_at or now is None:
        return False
    try:
        at = live_at if isinstance(live_at, _dt) else _dt.fromisoformat(str(live_at).replace("Z", "+00:00"))
        if at.tzinfo is not None and now.tzinfo is not None:
            at = at.astimezone(now.tzinfo)
    except (TypeError, ValueError):
        return False
    qd = at.date()
    if qd != now.date():
        return False
    closed = (now.hour, now.minute) >= (15, 45)
    return not (closed and last_session and str(last_session)[:10] >= qd.isoformat())


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


def _liquid_peer(m: dict[str, Any]) -> bool:
    """2.9.3 (owner's decision): a peer that fails the liquidity test (₹1 Cr/day,
    ₹20) is not evidence of how the sector's results are received."""
    return ((m.get("plus") or {}).get("liquidity") or {}).get("pass") is not False


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
        reported = [m for m in members if m.get("resultsReleased") and _liquid_peer(m)
                    and _num((m.get("priceContext") or {}).get("resultDayPct")) is not None]
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
        try:   # 2.9.6: untraded scrips (last session months ago) are not market breadth
            if (today - date.fromisoformat(str(px.get("last_session"))[:10])).days > 20:
                continue
        except ValueError:
            pass
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
    reported = [m for m in members if m.get("resultsReleased") and (m is it or _liquid_peer(m))]
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


# ---------------------------------------------------------------------------
# 2.9.8: QoQ / YoY / YTD lens and the sector checklist (after @SureshKBN's
# "When to use QoQ, YoY and YTD" and 5-point sector checklist). Display only:
# conviction score and entry rules are unchanged.
# ---------------------------------------------------------------------------
_LOSS_STATES = {"TURNAROUND", "DETERIORATION", "LOSS_NARROWING", "LOSS_WIDENING", "LOSS_FLAT"}


def growth_lens(rev_qoq: Any, rev_yoy: Any, rev_ytd: Any, pat_qoq: Any, pat_yoy: Any, pat_ytd: Any,
                pat_yoy_status: Any = None) -> dict[str, Any] | None:
    """QoQ detects the change, YoY confirms the growth, YTD validates execution.
    Returns the three readings per line plus plain-language notes."""
    rq, ry, rt, pq, py, pt = (_num(v) for v in (rev_qoq, rev_yoy, rev_ytd, pat_qoq, pat_yoy, pat_ytd))
    if all(v is None for v in (rq, ry, rt, pq, py, pt)):
        return None
    notes: list[str] = []
    for name, q, y, t in (("Revenue", rq, ry, rt), ("Profit", pq, py, pt)):
        if y is not None and y > 100 and q is not None and q < 25:
            notes.append(f"{name} {y:+.0f}% vs last year is mostly a low base: only {q:+.1f}% vs last quarter.")
        elif y is not None and t is not None:
            if y >= t + 5:
                notes.append(f"{name} growth is speeding up: this quarter {y:+.1f}% vs {t:+.1f}% for the year so far.")
            elif y <= t - 5:
                notes.append(f"{name} growth is slowing: this quarter {y:+.1f}% vs {t:+.1f}% for the year so far.")
    if rq is not None and pq is not None:
        if rq > 0 and pq > 0:
            notes.append("Improving now: revenue and profit both up on last quarter.")
        elif rq < 0 and pq < 0:
            notes.append("Both revenue and profit fell from last quarter (check if the business is seasonal).")
    if ry is not None and rt is not None and py is not None and pt is not None and min(ry, rt, py, pt) > 0 \
            and abs(ry - rt) < 5 and abs(py - pt) < 5 and py <= 100:
        notes.append("Consistent: this quarter's growth matches the year so far.")
    return {"revenue": {"qoq": rq, "yoy": ry, "ytd": rt}, "profit": {"qoq": pq, "yoy": py, "ytd": pt},
            "profitStatus": pat_yoy_status if pat_yoy_status in _LOSS_STATES else None, "notes": notes[:3]}


def _median(vals: list[float]) -> float | None:
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


def sector_checklists(items: list[dict[str, Any]], sectors: dict[str, dict[str, Any]], today: date) -> None:
    """Five questions per sector: demand, investment & orders, earnings, peer
    confirmation, market strength. Uses liquid companies that have reported
    this quarter; with fewer than 3 a question is 'not enough reports yet'.
    Orders/capex have no free source and are left to the reader. Adds
    sectors[name]['checklist'] and, per declared company, plus.execution
    (its own growth against the sector's median)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        key = (it.get("plus") or {}).get("sectorKey")
        if key:
            by.setdefault(key, []).append(it)

    def liquid(it):
        return ((it.get("plus") or {}).get("liquidity") or {}).get("pass") is not False

    def grew(x, status):
        return (x is not None and x > 0) or status == "TURNAROUND"

    for name, members in by.items():
        rep = [it for it in members if it.get("resultsReleased") and liquid(it) and _num(it.get("revenueYoY")) is not None]
        n = len(rep)
        checks: list[dict[str, Any]] = []
        few = f"only {n} liquid {'company has' if n == 1 else 'companies have'} reported"

        # 1 Demand: is revenue growth accelerating across the sector?
        pairs = [(_num(it.get("revenueYoY")), _num(((it.get("plus") or {}).get("growth") or {}).get("prevRevenueYoY")))
                 for it in rep]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        if len(pairs) >= 3:
            now, before = _median([a for a, _ in pairs]), _median([b for _, b in pairs])
            d = now - before
            st = "ok" if d >= 2 else "bad" if d <= -2 else "warn"
            checks.append({"key": "demand", "label": "Demand", "status": st,
                           "note": f"Median revenue growth {now:+.1f}% vs {before:+.1f}% last quarter ({len(pairs)} companies)."})
        else:
            checks.append({"key": "demand", "label": "Demand", "status": "na", "note": f"Not enough reports yet ({few})."})

        # 2 Investment and orders: no reliable free data.
        checks.append({"key": "orders", "label": "Orders & capex", "status": "na",
                       "note": "No free source for order books or capex; read the results presentations and concalls."})

        # 3 Earnings: revenue, profit and margins improving?
        if n >= 3:
            both = sum(_num(it.get("revenueYoY")) > 0 and grew(_num(it.get("patYoY")), it.get("patYoYStatus")) for it in rep)
            mg = [_num(((it.get("plus") or {}).get("margins") or {}).get("changeBps")) for it in rep]
            mg = [m for m in mg if m is not None]
            share = both / n
            st = "ok" if share >= 0.6 and (not mg or sum(m > 0 for m in mg) >= len(mg) / 2) else "bad" if share <= 0.4 else "warn"
            checks.append({"key": "earnings", "label": "Earnings", "status": st,
                           "note": f"{both} of {n} grew revenue and profit"
                                   + (f"; margins up at {sum(m > 0 for m in mg)} of {len(mg)}." if mg else ".")})
        else:
            checks.append({"key": "earnings", "label": "Earnings", "status": "na", "note": f"Not enough reports yet ({few})."})

        # 4 Peer confirmation: several companies showing the same trend?
        if n >= 3:
            up = sum(_num(it.get("revenueYoY")) > 0 for it in rep)
            st = "ok" if up >= max(3, 2 * n / 3) else "bad" if (n - up) >= max(3, 2 * n / 3) else "warn"
            checks.append({"key": "peers", "label": "Peer confirmation", "status": st,
                           "note": f"{up} of {n} reported higher revenue than last year."})
        else:
            checks.append({"key": "peers", "label": "Peer confirmation", "status": "na", "note": f"Not enough reports yet ({few})."})

        # 5 Market strength: money flowing in and stocks showing relative strength?
        rel = _num((sectors.get(name) or {}).get("relativeToMarket"))
        above = tot = 0
        for it in members:
            px = (it.get("plus") or {}).get("price") or {}
            last, e63 = _num((it.get("priceContext") or {}).get("lastClose")), _num(px.get("ema63"))
            try:
                fresh = (today - date.fromisoformat(str(px.get("last_session"))[:10])).days <= 20
            except ValueError:
                fresh = False
            if last is not None and e63 is not None and fresh:
                tot += 1
                above += last > e63
        if rel is None or tot < 4:
            checks.append({"key": "market", "label": "Market strength", "status": "na", "note": "Too few priced stocks."})
        else:
            share = above / tot * 100
            st = "ok" if rel > 0 and share >= 50 else "bad" if rel < 0 and share < 50 else "warn"
            checks.append({"key": "market", "label": "Market strength", "status": st,
                           "note": f"3-month move {rel:+.1f}% vs market; {share:.0f}% of {tot} stocks above their 63-day average."})

        measured = [c for c in checks if c["status"] != "na"]
        cl = {"score": sum(c["status"] == "ok" for c in measured), "measured": len(measured), "of": 5,
              "reported": n, "checks": checks,
              "medianRevenueYoY": _r2(_median([_num(it.get("revenueYoY")) for it in rep])),
              "medianProfitYoY": _r2(_median([_num(it.get("patYoY")) for it in rep if it.get("patYoYStatus") not in _LOSS_STATES]))}
        if name in sectors:
            sectors[name]["checklist"] = cl

        # Company execution vs the sector (tailwind is not execution).
        for it in members:
            if not it.get("resultsReleased"):
                continue
            ry, py = _num(it.get("revenueYoY")), _num(it.get("patYoY"))
            mr, mp = cl["medianRevenueYoY"], cl["medianProfitYoY"]
            peers = [x for x in rep if x is not it]
            if len(peers) < 2 or (ry is None and py is None):
                ex = {"label": None, "note": "Not enough sector peers have reported to judge execution."}
            else:
                beats = [(ry - mr) if ry is not None and mr is not None else None,
                         (py - mp) if py is not None and mp is not None and it.get("patYoYStatus") not in _LOSS_STATES else None]
                b = [x for x in beats if x is not None]
                lab = "AHEAD" if b and all(x > 0 for x in b) else "BEHIND" if b and all(x < 0 for x in b) else "MIXED"
                bits = []
                if beats[0] is not None:
                    bits.append(f"revenue {ry:+.1f}% vs sector median {mr:+.1f}%")
                if beats[1] is not None:
                    bits.append(f"profit {py:+.1f}% vs {mp:+.1f}%")
                ex = {"label": lab, "note": "; ".join(bits) + "."}
            ex["sectorScore"] = f"{cl['score']}/{cl['measured']}" if cl["measured"] else None
            it.setdefault("plus", {})["execution"] = ex


# ---------------------------------------------------------------------------
# 2.9.12: open positions (My trades tab). Same rules as the trade plan:
# stop = 1% under the result-day low until +1R, then cost, then trail the
# 21 EMA (swing) / 63 EMA (position). A stop never moves down. Close basis.
# ---------------------------------------------------------------------------

FAVOUR_LABELS = {"IN_FAVOUR": "In favour", "NEUTRAL": "Neutral", "OUT_OF_FAVOUR": "Out of favour (fundamentals fine)",
                 "DETERIORATING": "Fundamentals deteriorating", "NOT_ENOUGH": "Not enough data"}


def sector_favour(items: list[dict[str, Any]], sectors: dict[str, dict[str, Any]], regime: dict[str, Any] | None,
                  today: date) -> None:
    """2.9.20, after @SureshKBN's "How to know when a sector is out of favour".
    Five price signals per sector (liquid stocks with prices <= 20 days old):
    1 relative weakness (median 1M / 3M move vs NIFTY 500), 2 good results,
    poor reaction (STRONG reporters whose result-day move was <= 0), 3 failed
    breakouts (new 3-month high in the last 20 sessions, now > 5% under the
    peak), 4 participation (52-week highs now vs the 20 sessions before; share
    above the 63-day EMA), 5 valuation compression proxy (median profit growth
    > 10% while the median 3-month move is negative; real P/E history has no
    free source). Fundamentals from the sector checklist (median revenue /
    profit YoY). Status: OUT_OF_FAVOUR (prices weak, fundamentals fine or
    unknown) / DETERIORATING (prices and results weak) when two signals are
    bad, or three are bad/warn including relative strength; IN_FAVOUR when
    relative strength is ok and at most one other warns; else NEUTRAL.
    Adds sectors[name]["favour"] and plus.favour per company. Display only."""
    reg = regime or {}
    idx1, idx3 = _num(reg.get("ret20dPct")), _num(reg.get("ret63dPct"))
    by: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        key = (it.get("plus") or {}).get("sectorKey")
        if key:
            by.setdefault(key, []).append(it)

    def fresh(it):
        px = (it.get("plus") or {}).get("price") or {}
        try:
            return (today - date.fromisoformat(str(px.get("last_session"))[:10])).days <= 20
        except ValueError:
            return False

    def liquid(it):
        return ((it.get("plus") or {}).get("liquidity") or {}).get("pass") is not False

    for name, members in by.items():
        priced = [it for it in members if liquid(it) and fresh(it)]
        px = lambda it: (it.get("plus") or {}).get("price") or {}
        sig: list[dict[str, Any]] = []

        # 1 Relative weakness
        m1 = _median([_num(px(it).get("ret_21d_pct")) for it in priced])
        m3 = _median([_num(px(it).get("ret_63d_pct")) for it in priced])
        if len(priced) >= 4 and m1 is not None and m3 is not None and idx1 is not None:
            r1 = m1 - idx1
            r3 = (m3 - idx3) if idx3 is not None else None
            lag = [x for x in (r1, r3) if x is not None]
            st = "bad" if all(x <= -3 for x in lag) else "warn" if any(x <= -2 for x in lag) else "ok"
            sig.append({"key": "relative", "label": "Relative strength", "status": st,
                        "note": f"Median move 1M {m1:+.1f}% vs NIFTY 500 {idx1:+.1f}%"
                                + (f"; 3M {m3:+.1f}% vs {idx3:+.1f}%." if r3 is not None else ".")})
        else:
            sig.append({"key": "relative", "label": "Relative strength", "status": "na", "note": "Too few priced stocks or no index data."})

        # 2 Good results, poor reaction
        strong = [it for it in members if it.get("resultsReleased") and liquid(it)
                  and (it.get("plus") or {}).get("strength") == "STRONG"
                  and _num((it.get("priceContext") or {}).get("resultDayPct")) is not None]
        if len(strong) >= 3:
            weak = [it for it in strong if _num(it["priceContext"]["resultDayPct"]) <= 0]
            sh = len(weak) / len(strong)
            st = "bad" if sh >= 0.5 else "warn" if sh >= 0.34 else "ok"
            sig.append({"key": "reaction", "label": "Reaction to good results", "status": st,
                        "note": f"{len(weak)} of {len(strong)} companies with strong results fell or stayed flat on the result day"
                                + (f" ({', '.join(str(it.get('symbol')) for it in weak[:4])})." if weak else ".")})
        else:
            sig.append({"key": "reaction", "label": "Reaction to good results", "status": "na",
                        "note": f"Only {len(strong)} liquid compan{'y has' if len(strong) == 1 else 'ies have'} strong results with a measured reaction."})

        # 3 Failed breakouts
        bos = [it for it in priced if px(it).get("breakout_recent")]
        if len(bos) >= 3:
            failed = [it for it in bos if px(it).get("breakout_failed")]
            sh = len(failed) / len(bos)
            st = "bad" if sh >= 0.5 else "warn" if sh >= 0.34 else "ok"
            sig.append({"key": "breakouts", "label": "Breakouts holding", "status": st,
                        "note": f"{len(failed)} of {len(bos)} stocks that made a new 3-month high in the last 20 sessions are now more than 5% below their peak."})
        else:
            sig.append({"key": "breakouts", "label": "Breakouts holding", "status": "na",
                        "note": f"Only {len(bos)} new 3-month high{'s' if len(bos) != 1 else ''} in the last 20 sessions."})

        # 4 Participation
        long_hist = [it for it in priced if px(it).get("nh52_recent") is not None]
        above = [it for it in priced if _num(px(it).get("ema63")) is not None
                 and _num((it.get("priceContext") or {}).get("lastClose")) is not None]
        if len(above) >= 4:
            share = sum(_num(it["priceContext"]["lastClose"]) > _num(px(it)["ema63"]) for it in above) / len(above) * 100
            now = sum(bool(px(it).get("nh52_recent")) for it in long_hist)
            before = sum(bool(px(it).get("nh52_prior")) for it in long_hist)
            shrinking = before >= 3 and now < before * 0.5
            st = "bad" if shrinking or share < 30 else "warn" if (before >= 3 and now < before) or share < 50 else "ok"
            sig.append({"key": "participation", "label": "Participation", "status": st,
                        "note": f"{now} at 52-week highs in the last 20 sessions vs {before} in the 20 before; "
                                f"{share:.0f}% of {len(above)} above their 63-day average."})
        else:
            sig.append({"key": "participation", "label": "Participation", "status": "na", "note": "Too few priced stocks."})

        # 5 Valuation compression (proxy)
        cl = (sectors.get(name) or {}).get("checklist") or {}
        mp, mr = _num(cl.get("medianProfitYoY")), _num(cl.get("medianRevenueYoY"))
        reported = cl.get("reported") or 0
        if reported >= 3 and mp is not None and m3 is not None:
            st = "bad" if mp > 10 and m3 <= -8 else "warn" if mp > 10 and m3 < 0 else "ok"
            sig.append({"key": "valuation", "label": "Paying for growth", "status": st,
                        "note": f"Median profit growth {mp:+.1f}% while the median 3-month move is {m3:+.1f}%"
                                + (" (investors paying less for the same growth)." if st != "ok" else ".")
                                + " Rough proxy: no free P/E history."})
        else:
            sig.append({"key": "valuation", "label": "Paying for growth", "status": "na",
                        "note": "Not enough reports yet. Rough proxy only: no free P/E history."})

        # Fundamentals
        if reported >= 3 and mr is not None and mp is not None:
            fund = "weak" if (mr < 0 or mp < 0) else "ok"
            fnote = f"Median revenue {mr:+.1f}%, profit {mp:+.1f}% YoY ({reported} liquid reporters)."
        else:
            fund, fnote = "na", "Results not known yet (fewer than 3 liquid reporters)."

        measured = [x for x in sig if x["status"] != "na"]
        bad = sum(x["status"] == "bad" for x in measured)
        warn = sum(x["status"] == "warn" for x in measured)
        rel_st = sig[0]["status"]
        if len(measured) < 2:
            status = "NOT_ENOUGH"
        elif bad >= 2 or (bad + warn >= 3 and rel_st in ("warn", "bad")):
            # Relative weakness has to agree unless two signals are clearly bad.
            status = "DETERIORATING" if fund == "weak" else "OUT_OF_FAVOUR"
        elif bad == 0 and warn <= 1 and rel_st == "ok":
            status = "IN_FAVOUR" if fund != "weak" else "NEUTRAL"
        else:
            status = "NEUTRAL"
        if status == "OUT_OF_FAVOUR":
            note = ("Prices are losing favour while results still grow: sentiment, not fundamentals; can turn into an opportunity later."
                    if fund == "ok" else "Prices are losing favour; results not known yet.")
        elif status == "DETERIORATING":
            note = "Prices and results are both weakening: higher risk."
        elif status == "IN_FAVOUR":
            note = "No sign of the sector losing favour."
        elif status == "NOT_ENOUGH":
            note = "Not enough data to judge."
        else:
            note = "Some signs of fading interest; watch how stocks react to good news."
        fav = {"status": status, "label": FAVOUR_LABELS[status], "note": note, "bad": bad, "warn": warn,
               "measured": len(measured), "fundamentals": fund, "fundamentalsNote": fnote, "signals": sig}
        if name in sectors:
            sectors[name]["favour"] = fav
        for it in members:
            it.setdefault("plus", {})["favour"] = {"status": status, "label": FAVOUR_LABELS[status], "note": note,
                                                   "sector": name, "bad": bad, "warn": warn, "measured": len(measured)}

def position_view(pos: dict[str, Any], item: dict[str, Any] | None, state: dict[str, Any] | None,
                  today: date) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (view for the page, updated state to persist)."""
    st = dict(state or {})
    entry = _num(pos.get("entryPrice"))
    it = item or {}
    pl = it.get("plus") or {}
    px = pl.get("price") or {}
    plan = pl.get("plan") or {}
    low = _num(it.get("resultDayLow"))
    stop0 = _num(pos.get("stop")) or _num(plan.get("sl")) or (_r2(low * (1 - SL_BUFFER_PCT / 100)) if low else None)
    close = _num((it.get("priceContext") or {}).get("lastClose"))
    live = pl.get("live") or {}
    last = _num(live.get("price")) or close
    view: dict[str, Any] = {"symbol": pos.get("symbol"), "entryDate": pos.get("entryDate"), "entryPrice": entry,
                            "setup": pos.get("setup"), "initialStop": stop0, "resultDayLow": low,
                            "last": last, "lastClose": close, "live": bool(live.get("price")), "at": live.get("at"),
                            "ema21": _num(px.get("ema21")), "ema63": _num(px.get("ema63")), "status": "NO_DATA",
                            "note": "Waiting for prices."}
    if entry is None or stop0 is None or entry <= stop0:
        view["note"] = "Entry or stop missing."
        return view, st
    risk = entry - stop0
    one_r = entry + risk
    sess = str(px.get("last_session") or "")[:10]
    if close is not None and sess and sess >= str(pos.get("entryDate") or ""):   # only closes after buying
        st["peakClose"] = max(_num(st.get("peakClose")) or close, close)
    one_r_hit = bool(st.get("oneRHit")) or ((_num(st.get("peakClose")) or 0) >= one_r)
    st["oneRHit"] = one_r_hit
    swing = position = stop0
    if one_r_hit:
        e21, e63 = view["ema21"], view["ema63"]
        swing = max(entry, e21) if e21 is not None else entry
        position = max(entry, e63) if e63 is not None else entry
    # Never lower a stop that was already raised.
    swing = max(swing, _num(st.get("swingStop")) or swing)
    position = max(position, _num(st.get("positionStop")) or position)
    st["swingStop"], st["positionStop"] = _r2(swing), _r2(position)
    r_now = (last - entry) / risk if last is not None else None
    view.update({"riskPerShare": _r2(risk), "riskPct": _r2(risk / entry * 100), "oneR": _r2(one_r),
                 "twoR": _r2(entry + 2 * risk), "swingStop": _r2(swing), "positionStop": _r2(position),
                 "rNow": _r2(r_now), "pnlPct": _r2(_pct(last, entry)) if last is not None else None,
                 "peakClose": _r2(_num(st.get("peakClose"))), "oneRHit": one_r_hit})
    if last is None:
        view["note"] = "No price for this stock yet."
    elif close is not None and close < swing:
        view["status"], view["note"] = "EXIT_SWING", (
            f"Closed at {close:,.2f}, below the {'swing trail' if one_r_hit else 'stop'} {swing:,.2f}: the plan says exit"
            + (" the swing part (position part trails the 63 EMA)." if one_r_hit and close >= position else "."))
        if close < position:
            view["status"] = "EXIT"
    elif one_r_hit:
        view["status"], view["note"] = "TRAILING", (f"+1R reached: stop at cost or better. Trail swing {swing:,.2f} "
                                                    f"(21 EMA) / position {position:,.2f} (63 EMA), on a closing basis.")
    elif last is not None and last < entry:
        view["status"], view["note"] = "BELOW_ENTRY", (f"Below entry; the stop {stop0:,.2f} (1% under the result-day "
                                                       f"low {low:,.2f}) stands until +1R at {one_r:,.2f}." if low else
                                                       f"Below entry; stop {stop0:,.2f} stands until +1R at {one_r:,.2f}.")
    else:
        view["status"], view["note"] = "HOLD", (f"Hold. Stop {stop0:,.2f} until +1R at {one_r:,.2f}; then move the stop "
                                                f"to cost and trail the 21 / 63 EMA.")
    return view, st


# ---------------------------------------------------------------------------
# 2.9.15 Holdings watch: exchange filings, deals, selling pressure, global
# markets and headlines for the owner's open trades. Display only: no entry
# or exit rule reads any of this.
# ---------------------------------------------------------------------------

# (kind, tone, pattern) - first match wins. Tone is keyword-based and only a
# hint; the filing itself is the source.
_ANN_RULES = (
    # 2.9.19: corrections first (SEJALLTD "Corrigendum To Standalone Financial
    # Results" was a plain results tag), then exchange surveillance queries
    # (NSE "Spurt in Volume" text says "The Exchange, in order to ensure..."
    # and was tagged ORDER / pos for PREMIERPOL).
    ("CORRECTION", "warn", r"corrigendum|\berrat(?:a|um)\b|restat(?:ed|ement)|revised (?:financial )?results|"
                           r"(?:qualified|modified|adverse) (?:audit )?opinion"),
    ("PLEDGE", "neg", r"\bpledg|encumbr|invocation"),
    ("PLEDGE", "pos", r"release of (?:pledge|encumbrance)|revocation of pledge"),
    ("INSIDER", "info", r"\bSAST\b|Regulation 29|Reg(?:ulation)?\.? ?7\(2\)|Regulation 31|insider trading|\bPIT\b|"
                        r"acquisition of shares|disposal of shares|change in (?:promoter )?shareholding"),
    ("DEAL", "info", r"bulk deal|block deal"),
    ("LEGAL", "neg", r"tax demand|show cause|penalt|litigation|\bGST\b|income tax|search (?:and|&) seizure|\bsurvey\b|\bfine\b|"
                     r"adjudicat|arbitrat|\bSEBI order|court|tribunal|\bNCLT\b"),
    ("QUERY", "info", r"spurt in (?:volume|price)|significant increase in (?:volume|price)|"
                      r"(?:price|volume) movement|movement in (?:the )?(?:price|volume)|clarification"),
    ("ORDER", "pos", r"(?<!in )\border(?:s)?\b(?! to\b| of the| dated| passed| under| of hon| from the (?:hon|court|tribunal))|"
                     r"\bcontract\b|letter of (?:intent|award)|\bLoI\b|\bLoA\b|bagging|awarded|\bwins?\b|purchase order"),
    ("RATING", "info", r"credit rating|\brating\b"),
    ("MGMT", "neg", r"resign|cessation|step(?:s|ped)? down"),
    ("CORP", "info", r"buy-?back|dividend|bonus|split|merger|amalgamat|acqui|demerger|scheme of|fund ?rais|"
                     r"preferential|\bQIP\b|rights issue|capacity|commission|plant|expansion|joint venture|"
                     r"\bJV\b|agreement|MoU|partnership|launch"),
    ("MGMT", "info", r"appoint|re-?designat|change in (?:key )?management|\bKMP\b|director"),
    ("RESULT", "info", r"financial result|outcome of board|earnings call|analyst|investor (?:meet|presentation)|"
                       r"conference call|transcript|press release|recording"),
)
_ANN_RX = [(k, t, _re.compile(p, _re.I)) for k, t, p in _ANN_RULES]


def classify_announcement(text: str) -> tuple[str, str]:
    """(kind, tone) for an exchange announcement: ORDER / DEAL / INSIDER /
    PLEDGE / RATING / LEGAL / MGMT / CORP / RESULT / OTHER; tone pos / neg / info."""
    t = _re.sub(r"\s+", " ", text or "")
    # Release of a pledge reads as "pledge" too: check it first.
    if _re.search(r"release of (?:pledge|encumbrance)|revocation of pledge", t, _re.I):
        return "PLEDGE", "pos"
    for kind, tone, rx in _ANN_RX:
        if rx.search(t):
            return kind, tone
    return "OTHER", "info"


def holding_filings(rows: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    """rows: {filedAt (datetime), text, url} -> newest first, classified,
    trading-window / routine compliance filings dropped."""
    out = []
    routine = _re.compile(r"trading window|loss of share certificate|duplicate share|newspaper (?:publication|advertisement)|"
                          r"certificate under regulation 74|regulation 74\(5\)|compliance certificate|"
                          r"\bESOP\b|allotment of .*(?:esop|employee)|copy of newspaper", _re.I)
    for r in rows or []:
        at = r.get("filedAt")
        text = _re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
        if at is None or not text or routine.search(text):
            continue
        kind, tone = classify_announcement(text)
        out.append({"at": at.isoformat() if hasattr(at, "isoformat") else str(at), "kind": kind, "tone": tone,
                    "text": text[:280], "url": r.get("url")})
    out.sort(key=lambda x: x["at"], reverse=True)
    seen, uniq = set(), []
    for x in out:
        k = (x["at"][:10], x["text"][:80])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


def parse_deal_rows(rows: list[dict[str, Any]], kind: str, symbols: set[str]) -> list[dict[str, Any]]:
    """NSE bulk / block / short-selling report rows for the given symbols."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("BD_SYMBOL") or r.get("SS_SYMBOL") or r.get("symbol") or "").strip().upper()
        if sym not in symbols:
            continue
        raw_d = str(r.get("BD_DT_DATE") or r.get("SS_DATE") or r.get("date") or "")
        try:
            d = _dt.strptime(raw_d.title(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        if kind == "SHORT":
            out.append({"symbol": sym, "date": d, "kind": kind, "qty": _num(r.get("SS_QTY"))})
        else:
            out.append({"symbol": sym, "date": d, "kind": kind,
                        "client": str(r.get("BD_CLIENT_NAME") or "").strip() or None,
                        "side": str(r.get("BD_BUY_SELL") or "").strip().upper() or None,
                        "qty": _num(r.get("BD_QTY_TRD")), "price": _num(r.get("BD_TP_WATP"))})
    return out


def favour_signals(f: Any) -> dict[str, Any]:
    """2.9.20 per-stock inputs for the sector favour check (closing prices):
    breakout_recent: a close above the previous 63 sessions' highest close
      within the last 20 sessions; breakout_failed: since then the last close
      is > 5% below the highest close after that breakout.
    nh52_recent / nh52_prior: a close at a 52-week (252-session) high in the
      last 20 sessions / in the 20 before those (needs >= 160 sessions)."""
    out: dict[str, Any] = {"breakout_recent": None, "breakout_failed": None, "nh52_recent": None, "nh52_prior": None}
    if f is None or len(f) < 84:
        return out
    c = [_num(x) for x in f["Close"].tolist()]
    n = len(c)
    bo = None
    for i in range(max(63, n - 20), n):
        prior = [x for x in c[i - 63:i] if x is not None]
        if c[i] is not None and prior and c[i] > max(prior):
            bo = i
            break
    out["breakout_recent"] = bo is not None
    if bo is not None:
        peak = max(x for x in c[bo:] if x is not None)
        out["breakout_failed"] = bool(c[-1] is not None and c[-1] < peak * 0.95)
    if n >= 160:
        def at_high(i):
            w = [x for x in c[max(0, i - 251):i + 1] if x is not None]
            return c[i] is not None and w and c[i] >= max(w)
        out["nh52_recent"] = any(at_high(i) for i in range(n - 20, n))
        out["nh52_prior"] = any(at_high(i) for i in range(n - 40, n - 20))
    return out


def selling_pressure(frame: Any, lookback: int = 25) -> dict[str, Any] | None:
    """Volume behaviour over the last `lookback` sessions:
    distribution day = close down >= 0.2% on volume >= 1.2x the prior 20-session
    average; accumulation day = close up >= 0.2% on such volume; up/down volume ratio
    over 20 sessions; where the last close sits in its day's range.
    Label: HEAVY_SELLING / SELLING / NEUTRAL / ACCUMULATION (screener heuristic)."""
    f = _clean(frame)
    if f is None or len(f) < 22 or not f["Volume"].notna().any():
        return None
    close, vol = f["Close"], f["Volume"]
    chg = close.pct_change() * 100
    vup = vol >= vol.shift(1).rolling(20, min_periods=10).mean() * 1.2   # heavy volume vs the prior 20 sessions
    tail = slice(len(f) - lookback, len(f)) if len(f) > lookback else slice(1, len(f))
    dist = int(((chg <= -0.2) & vup).iloc[tail].sum())
    acc = int(((chg >= 0.2) & vup).iloc[tail].sum())
    last20 = f.iloc[-20:]
    c20 = chg.iloc[-20:]
    upv = float(last20["Volume"][c20 > 0].sum())
    dnv = float(last20["Volume"][c20 < 0].sum())
    udr = _r2(upv / dnv) if dnv > 0 else None
    hi, lo, last = _num(f["High"].iloc[-1]), _num(f["Low"].iloc[-1]), _num(close.iloc[-1])
    cir = _r2((last - lo) / (hi - lo) * 100) if hi is not None and lo is not None and last is not None and hi > lo else None
    avg20 = _num(vol.iloc[-21:-1].mean())
    last_rvol = _r2(_num(vol.iloc[-1]) / avg20) if avg20 else None
    if dist >= 5 and dist >= acc + 3 and (udr is None or udr < 0.8):
        label = "HEAVY_SELLING"
    elif dist >= 3 and dist > acc and (udr is None or udr < 1.0):
        label = "SELLING"
    elif acc >= dist + 2 and (udr or 0) >= 1.3:
        label = "ACCUMULATION"
    else:
        label = "NEUTRAL"
    return {"label": label, "distributionDays": dist, "accumulationDays": acc, "sessions": min(lookback, len(f) - 1),
            "upDownVolume": udr, "lastCloseInRangePct": cir, "lastDownOnVolume": bool(chg.iloc[-1] <= -0.2 and (last_rvol or 0) >= 1.5),
            "lastRvol": last_rvol, "asOf": f["Date"].iloc[-1].date().isoformat()}


# Global context per sector: listed peers / commodities whose overnight move
# tends to show up in Indian prices. Tickers are Yahoo symbols.
GLOBAL_MARKETS = (
    ("^GSPC", "S&P 500", "index"), ("^IXIC", "Nasdaq", "index"), ("^N225", "Nikkei 225", "index"),
    ("^HSI", "Hang Seng", "index"), ("^VIX", "US VIX", "risk"), ("^TNX", "US 10Y yield", "rates"),
    ("DX-Y.NYB", "Dollar index", "fx"), ("USDINR=X", "USD/INR", "fx"), ("BZ=F", "Brent crude", "commodity"),
    ("GC=F", "Gold", "commodity"),
)
SECTOR_GLOBAL_PEERS = {
    "Information Technology": (("ACN", "Accenture"), ("CTSH", "Cognizant"), ("EPAM", "EPAM"), ("INFY", "Infosys ADR"),
                               ("WIT", "Wipro ADR"), ("IBM", "IBM")),
    # 2.9.19: Capital Goods without a known industry gets broad industrials;
    # the power-equipment names moved to the electrical-equipment industry group.
    "Capital Goods": (("XLI", "US industrials ETF"), ("CAT", "Caterpillar"), ("ABBN.SW", "ABB")),
    "Power": (("GEV", "GE Vernova"), ("NEE", "NextEra"), ("ENR.DE", "Siemens Energy"), ("HG=F", "Copper")),
    "Healthcare": (("XLV", "US healthcare ETF"), ("TEVA", "Teva"), ("VTRS", "Viatris"), ("DRREDDY.NS", "Dr Reddy's")),
    "Metals & Mining": (("HG=F", "Copper"), ("ALI=F", "Aluminium"), ("FCX", "Freeport"), ("BHP", "BHP"), ("VALE", "Vale")),
    "Oil, Gas & Consumable Fuels": (("BZ=F", "Brent crude"), ("NG=F", "Natural gas"), ("XOM", "Exxon"), ("SHEL", "Shell")),
    "Chemicals": (("DOW", "Dow"), ("LYB", "LyondellBasell"), ("BZ=F", "Brent crude")),
    "Automobile and Auto Components": (("TM", "Toyota"), ("GM", "GM"), ("F", "Ford"), ("APTV", "Aptiv")),
    "Financial Services": (("XLF", "US financials ETF"), ("HDB", "HDFC Bank ADR"), ("IBN", "ICICI Bank ADR")),
    "Fast Moving Consumer Goods": (("XLP", "US staples ETF"), ("UL", "Unilever"), ("NSRGY", "Nestle")),
    "Textiles": (("CT=F", "Cotton"), ("XRT", "US retail ETF")),
    "Consumer Durables": (("XLY", "US discretionary ETF"), ("GC=F", "Gold")),
    "Telecommunication": (("VZ", "Verizon"), ("T", "AT&T")),
}
# Macro headline searches per sector (Yahoo news search); the market-wide one is always added.
SECTOR_MACRO_QUERY = {      # short keyword searches (Yahoo search matches few words), tried in order
    "Information Technology": ("H-1B visa", "IT services tariff", "Accenture"),
    "Capital Goods": ("India capex", "industrial production India"),
    "Power": ("power grid", "GE Vernova"),
    "Healthcare": ("pharma tariff", "FDA warning letter"),
    "Metals & Mining": ("steel tariff", "copper price", "aluminium price"),
    "Oil, Gas & Consumable Fuels": ("OPEC", "crude oil price"),
    "Chemicals": ("anti-dumping duty", "chemical prices China"),
    "Automobile and Auto Components": ("auto tariffs", "EV sales"),
    "Textiles": ("textile tariff", "cotton prices"),
    "Financial Services": ("RBI policy", "Indian banks"),
}
MARKET_MACRO_QUERY = ("Trump tariff", "India stocks", "FII selling", "Federal Reserve")


# 2.9.19: peers by NSE basic industry before the broad sector. NSE puts
# Premier Polyfilm (Plastic Products - Industrial) and Sejal Glass (Glass -
# Industrial) in Capital Goods, whose list was all power-grid names, and
# Groww (Stockbroking & Allied) in Financial Services, whose list is banks.
# (pattern on the industry label, group, global peers, headline searches);
# first match wins; no match -> the sector lists above.
INDUSTRY_PEER_GROUPS = (
    (r"stock ?brok|exchange and data|depositor|clearing|asset management|financial products distributor|"
     r"fintech|financial technology|wealth|capital market",
     "Capital markets", (("HOOD", "Robinhood"), ("SCHW", "Charles Schwab"), ("IBKR", "Interactive Brokers"),
                         ("ICE", "ICE (exchanges)")), ("SEBI", "India IPO", "demat accounts")),
    (r"insurance", "Insurance", (("MET", "MetLife"), ("PRU", "Prudential"), ("AIG", "AIG")), ("IRDAI", "insurance India")),
    (r"heavy electrical|electrical equipment|cables|transformer|switchgear|power equipment",
     "Electrical equipment", (("GEV", "GE Vernova"), ("ETN", "Eaton"), ("HUBB", "Hubbell"), ("ENR.DE", "Siemens Energy"),
                              ("SU.PA", "Schneider"), ("HG=F", "Copper")),
     ("transformer shortage", "grid equipment", "GE Vernova")),
    (r"plastic|packaging|polymer|films",
     "Plastics & packaging", (("AMCR", "Amcor"), ("DOW", "Dow"), ("LYB", "LyondellBasell"), ("BZ=F", "Brent crude")),
     ("PVC prices", "polymer prices")),
    (r"glass|ceramic|sanitaryware|plywood|laminate|other construction materials",
     "Glass & building products", (("SGO.PA", "Saint-Gobain"), ("OC", "Owens Corning"), ("APOG", "Apogee")),
     ("India real estate", "housing demand India")),
    (r"aerospace|defen[cs]e|ship ?building",
     "Aerospace & defence", (("ITA", "US aerospace & defense ETF"), ("LMT", "Lockheed Martin"), ("RTX", "RTX")),
     ("defence order", "defense budget")),
    (r"iron & steel|\bsteel\b|castings|forgings",
     "Steel", (("HG=F", "Copper"), ("ALI=F", "Aluminium"), ("NUE", "Nucor"), ("MT", "ArcelorMittal")),
     ("steel tariff", "steel prices")),
)
_INDUSTRY_PEER_RX = [(_re.compile(p, _re.I), g, peers, q) for p, g, peers, q in INDUSTRY_PEER_GROUPS]


def peer_group_for(sector: str | None, industry: str | None = None) -> tuple[str | None, tuple, tuple]:
    """(group label, global peers, headline searches): industry group first,
    else the sector's lists (label = sector), else nothing."""
    ind = str(industry or "")
    if ind:
        for rx, g, peers, q in _INDUSTRY_PEER_RX:
            if rx.search(ind):
                return g, peers, q
    if sector and (sector in SECTOR_GLOBAL_PEERS or sector in SECTOR_MACRO_QUERY):
        return sector, SECTOR_GLOBAL_PEERS.get(sector, ()), SECTOR_MACRO_QUERY.get(sector, ())
    return None, (), ()


def global_peers_for(sector: str | None, industry: str | None = None) -> tuple[tuple[str, str], ...]:
    return peer_group_for(sector, industry)[1]


def macro_queries_for(sector: str | None, industry: str | None = None) -> tuple[str, ...]:
    return peer_group_for(sector, industry)[2]


def summarize_series(closes: list[tuple[str, float]]) -> dict[str, Any] | None:
    """[(iso date, close)] oldest first -> last, 1-session and 5-session % change."""
    pts = [(d, _num(c)) for d, c in closes if _num(c) is not None]
    if not pts:
        return None
    d, last = pts[-1]
    prev = pts[-2][1] if len(pts) >= 2 else None
    prev5 = pts[-6][1] if len(pts) >= 6 else None
    return {"date": d, "last": _r2(last), "chg1dPct": _r2(_pct(last, prev)) if prev else None,
            "chg5dPct": _r2(_pct(last, prev5)) if prev5 else None}


def normalize_news(items: Any, limit: int = 6) -> list[dict[str, Any]]:
    """Yahoo news items (old flat and new nested 'content' shapes) -> headline,
    publisher, time, link. Titles and links only; never article text."""
    out = []
    for n in items or []:
        if not isinstance(n, dict):
            continue
        c = n.get("content") if isinstance(n.get("content"), dict) else n
        title = str(c.get("title") or "").strip()
        url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None) \
            or ((c.get("clickThroughUrl") or {}).get("url") if isinstance(c.get("clickThroughUrl"), dict) else None) \
            or c.get("link") or c.get("url")
        pub = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        at = c.get("pubDate") or c.get("displayTime") or c.get("at")
        if at is None and _num(c.get("providerPublishTime")):
            at = _dt.fromtimestamp(int(_num(c["providerPublishTime"])), _tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not title or not url or not str(url).startswith("http"):
            continue
        out.append({"title": title[:200], "publisher": pub, "at": str(at) if at else None, "url": url})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    seen, uniq = set(), []
    for x in out:
        k = x["title"].lower()[:70]
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq[:limit]


def balanced_news(per_source: list[list[dict[str, Any]]], limit: int = 6) -> list[dict[str, Any]]:
    """Up to 2 newest headlines from each source first (so one busy source
    cannot crowd out the rest), then the remainder; deduplicated, at most
    `limit`, shown newest first."""
    lists = [normalize_news(lst, limit=20) for lst in per_source]
    order = [n for lst in lists for n in lst[:2]] + [n for lst in lists for n in lst[2:]]
    seen, out = set(), []
    for n in order:
        k = n["title"].lower()[:70]
        if k not in seen:
            seen.add(k)
            out.append(n)
        if len(out) >= limit:
            break
    return sorted(out, key=lambda x: x["at"] or "", reverse=True)


def holding_watch_view(symbol: str, watch: dict[str, Any] | None, deals: list[dict[str, Any]],
                       pressure: dict[str, Any] | None, delivery_pct: Any, global_rows: dict[str, Any],
                       sector: str | None, today: date, industry: str | None = None) -> dict[str, Any]:
    """What the My trades card shows under 'News & pressure'."""
    w = watch or {}
    mine = [d for d in deals or [] if d.get("symbol") == symbol]
    mine.sort(key=lambda d: d["date"], reverse=True)
    peers = []
    group = peer_group_for(sector, industry)[0]
    for t, name in global_peers_for(sector, industry):
        g = global_rows.get(t)
        if g:
            peers.append({"ticker": t, "name": name, **g})
    sells = [d for d in mine if d.get("side") == "SELL" or d.get("kind") == "SHORT"]
    flags = []
    if pressure and pressure.get("label") in ("SELLING", "HEAVY_SELLING"):
        flags.append(f"{pressure['distributionDays']} distribution days in {pressure['sessions']} sessions")
    if pressure and pressure.get("lastDownOnVolume"):
        flags.append(f"last session fell on {pressure['lastRvol']}x volume")
    if any(d.get("kind") in ("BULK", "BLOCK") and d.get("side") == "SELL" for d in mine):
        flags.append("bulk/block selling filed")
    neg = [f for f in w.get("filings") or [] if classify_announcement(f.get("text") or "")[1] == "neg"]
    if neg:
        flags.append(f"{len(neg)} negative-sounding filing(s)")
    if any(classify_announcement(f.get("text") or "")[0] == "CORRECTION" for f in w.get("filings") or []):
        flags.append("correction / corrigendum filed")
    peer_moves = [p["chg1dPct"] for p in peers if p.get("chg1dPct") is not None]
    peer_avg = _r2(sum(peer_moves) / len(peer_moves)) if peer_moves else None
    if peer_avg is not None and peer_avg <= -1.5:
        flags.append(f"global peers {peer_avg:+.1f}% last session")
    # 2.9.19: re-label stored filings with the current rules (tags were fixed
    # at fetch time, so the old "Order" tag on a volume query stayed 2 h).
    filings = [{**f, **dict(zip(("kind", "tone"), classify_announcement(f.get("text") or "")))}
               for f in w.get("filings") or [] if isinstance(f, dict)]
    return {"filings": filings, "filingsAt": w.get("filingsAt"), "peerGroup": group,
            "deals": mine[:8], "dealsSelling": len(sells), "pressure": pressure,
            "deliveryPct": _r2(_num(delivery_pct)), "peers": peers, "peerAvg1dPct": peer_avg,
            "news": w.get("news") or [], "sectorNews": w.get("sectorNews") or [], "newsAt": w.get("newsAt"),
            "flags": flags}


# ---------------------------------------------------------------------------
# 2.9.23 Relative strength vs the stock's sector index (1 week / 1 month),
# the "RS vs sector index" table. Display only.
# ---------------------------------------------------------------------------

SECTOR_INDEX_FOR = {   # canonical sector -> NSE index when the quote gives none (or only NIFTY 500)
    "Information Technology": "NIFTY IT", "Financial Services": "NIFTY FINANCIAL SERVICES",
    "Automobile and Auto Components": "NIFTY AUTO", "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Healthcare": "NIFTY PHARMA", "Metals & Mining": "NIFTY METAL", "Realty": "NIFTY REALTY",
    "Media, Entertainment & Publication": "NIFTY MEDIA", "Oil, Gas & Consumable Fuels": "NIFTY OIL & GAS",
    "Power": "NIFTY ENERGY", "Construction": "NIFTY INFRASTRUCTURE", "Construction Materials": "NIFTY INFRASTRUCTURE",
    "Consumer Durables": "NIFTY CONSUMER DURABLES", "Chemicals": "NIFTY CHEMICALS", "Capital Goods": "NIFTY INDIA MANUFACTURING",
    "Consumer Services": "NIFTY INDIA CONSUMPTION", "Services": "NIFTY SERVICES SECTOR",
    "Telecommunication": "NIFTY 500",      # no telecom index: compare with the market
}


def sector_index_for(identity: dict[str, Any] | None, sector: str | None) -> str:
    ident = identity or {}
    si = str(ident.get("sectorIndex") or "").strip().upper()
    if si and si != "NIFTY 500":
        return si
    ind = f"{ident.get('industry') or ''} {ident.get('basicIndustry') or ''}".lower()
    if "bank" in ind:
        return "NIFTY BANK"
    return SECTOR_INDEX_FOR.get(sector or "", "NIFTY 500")


def _index_close_on_or_before(series: dict[str, Any], day: str | None) -> float | None:
    dates, closes = series.get("dates") or [], series.get("closes") or []
    best = None
    for d, c in zip(dates, closes):
        if day and d <= day:
            best = c
    return _num(best)


def rs_vs_sector_index(px: dict[str, Any], index: str, series: dict[str, Any] | None) -> dict[str, Any] | None:
    """Stock return over its last 5 / 21 sessions minus the sector index's
    return over the same dates (close-to-close)."""
    if not series or not series.get("dates"):
        return None
    end = str(px.get("last_session") or "")[:10]
    s1, s21 = _num(px.get("ret_5d_pct")), _num(px.get("ret_21d_pct"))
    i_end = _index_close_on_or_before(series, end)
    if not end or i_end is None or end > series["dates"][-1] and (date.fromisoformat(end) - date.fromisoformat(series["dates"][-1])).days > 4:
        return None
    out = {"index": index, "asOf": min(end, series["dates"][-1])}
    for key, sret, frm in (("1w", s1, px.get("d5_from")), ("1m", s21, px.get("d21_from"))):
        i0 = _index_close_on_or_before(series, frm)
        ir = _pct(i_end, i0) if i0 else None
        out[f"stock{key}"] = sret
        out[f"index{key}"] = _r2(ir)
        out[f"rs{key}"] = _r2(sret - ir) if sret is not None and ir is not None else None
    if out["rs1w"] is None and out["rs1m"] is None:
        return None
    return out


def index_returns(series: dict[str, Any] | None) -> dict[str, Any] | None:
    closes = [c for c in ((series or {}).get("closes") or []) if _num(c) is not None]
    if len(closes) < 6:
        return None
    return {"ret1w": _r2(_pct(closes[-1], closes[-6])), "ret1m": _r2(_pct(closes[-1], closes[-22])) if len(closes) >= 22 else None,
            "asOf": (series.get("dates") or [None])[-1]}


def apply_sector_index_rs(items: list[dict[str, Any]], sectors: dict[str, dict[str, Any]],
                          series_by_index: dict[str, dict[str, Any]]) -> None:
    """plus.rsSector per company; sectors[name]['index'] = the index most of
    its members use, with that index's 1W / 1M return."""
    use: dict[str, dict[str, int]] = {}
    for it in items:
        pl = it.get("plus") or {}
        idx = sector_index_for(pl.get("identity"), pl.get("sectorKey"))
        ser = series_by_index.get(idx)
        if ser is None and idx != "NIFTY 500":
            idx, ser = "NIFTY 500", series_by_index.get("NIFTY 500")
        rs = rs_vs_sector_index(pl.get("price") or {}, idx, ser)
        if rs:
            pl["rsSector"] = rs
        if pl.get("sectorKey") and ser is not None:
            use.setdefault(pl["sectorKey"], {}).setdefault(idx, 0)
            use[pl["sectorKey"]][idx] += 1
    for name, counts in use.items():
        idx = max(counts, key=counts.get)
        r = index_returns(series_by_index.get(idx))
        if name in sectors and r:
            sectors[name]["index"] = {"name": idx, **r}


# ---------------------------------------------------------------------------
# 2.9.25 Radar grade: best to worst for the owner who takes only a few PEAD
# trades. Display only: it orders the Radar and explains why; no entry or
# exit rule reads it.
# ---------------------------------------------------------------------------

RADAR_TIERS = (("A", "Best: take on the trigger"), ("B", "Good: wait for the trigger"),
               ("C", "Watch only"), ("D", "Weak: skip for now"))
FRESH_ENTRY = {"ENTRY_EARLY", "ENTRY_PULLBACK", "ENTRY_BREAKOUT"}
READY = {"NEAR_ENTRY", "PULLBACK_ZONE"}


def radar_grade(item: dict[str, Any], *, min_mcap_cr: float = 1000.0, last_price: Any = None) -> dict[str, Any]:
    """Score 0-100 and tier A-D with the reasons for and against."""
    pl = item.get("plus") or {}
    plan = pl.get("plan") or {}
    px = pl.get("price") or {}
    pc = item.get("priceContext") or {}
    sig = str(plan.get("signal") or "")
    last = _num(last_price) or _num(pc.get("lastClose"))
    entry, sl = _num(plan.get("entry")), _num(plan.get("sl"))
    pros: list[str] = []
    cons: list[str] = []
    score = 0.0

    # 1 How ready the setup is
    if sig in FRESH_ENTRY:
        score += 30; pros.append("entry triggered")
    elif sig in READY:
        score += 22; pros.append("close to its trigger")
    elif sig == "ENTRY_TRIGGERED":
        score += 12; cons.append("entry triggered earlier: buying now is late")
    elif sig == "WATCH":
        score += 8; cons.append("no entry pattern yet")
    elif sig == "WAIT_CONCALL":
        score += 6; cons.append("waiting for the concall")
    elif sig == "EXTENDED":
        score += 4; cons.append("extended above the 21 EMA")
    elif sig == "RISK_TOO_WIDE":
        cons.append("stop too far away")

    # 2 Earnings quality
    st = pl.get("strength")
    if st == "STRONG":
        score += 20; pros.append("strong earnings")
    elif st == "AVERAGE+":
        score += 8; cons.append("earnings only average")
    bucket = (pl.get("bucket") or {}).get("code")
    if bucket in ("CONFIRMATION", "RE_PEAD"):
        score += 8; pros.append("second strong quarter")
    elif bucket == "FRESH_PEAD":
        score += 4

    # 3 Risk from today's price to the stop
    risk_now = None
    if sl is not None and last is not None and last > sl:
        basis = max(last, entry) if entry is not None else last
        risk_now = (basis - sl) / basis * 100
        if risk_now <= 5:
            score += 15; pros.append(f"tight risk {risk_now:.1f}%")
        elif risk_now <= 8:
            score += 8
        elif risk_now <= 10:
            score += 2; cons.append(f"risk {risk_now:.1f}%")
        else:
            score -= 5; cons.append(f"risk {risk_now:.1f}% from here")

    # 4 Reaction on the result
    ret, vol = _num(pc.get("resultDayPct")), _num(pc.get("relativeVolume"))
    if ret is not None and vol is not None and ret >= 4 and vol >= 2:
        score += 5; pros.append(f"reaction {ret:+.1f}% on {vol:.1f}x volume")
    elif ret is not None and ret < 0:
        score -= 5; cons.append(f"fell {abs(ret):.1f}% on the result")

    # 5 Volume behaviour since
    pr = (px.get("pressure") or {}).get("label")
    if pr == "ACCUMULATION":
        score += 6; pros.append("accumulation")
    elif pr == "SELLING":
        score -= 8; cons.append("selling pressure")
    elif pr == "HEAVY_SELLING":
        score -= 15; cons.append("heavy selling")

    # 6 Strength vs its own sector index
    rs = _num((pl.get("rsSector") or {}).get("rs1m"))
    if rs is not None:
        if rs >= 3:
            score += 6; pros.append(f"beats its sector index by {rs:.1f} pp")
        elif rs <= -3:
            score -= 6; cons.append(f"lags its sector index by {abs(rs):.1f} pp")

    # 7 Sector favour
    fav = (pl.get("favour") or {}).get("status")
    if fav == "IN_FAVOUR":
        score += 6; pros.append("sector in favour")
    elif fav == "OUT_OF_FAVOUR":
        score -= 4; cons.append("sector out of favour")
    elif fav == "DETERIORATING":
        score -= 10; cons.append("sector deteriorating")

    # 8 Red flags, concall, size
    flags = plan.get("redFlags") or []
    if flags:
        score -= min(20, 10 * len(flags)); cons.append("; ".join(flags))
    if plan.get("stage") == "STARTER":
        score -= 3; cons.append("concall pending: 1/3 size only")
    mcap = _num(item.get("marketCapCr"))
    small = mcap is None or mcap < min_mcap_cr
    if small:
        score -= 15; cons.append("below the market-cap minimum" if mcap is not None else "market cap unknown")

    score = max(0.0, min(100.0, score))
    actionable = sig in FRESH_ENTRY or sig in READY
    if actionable and score >= 70 and not flags and not small and (risk_now is None or risk_now <= 8):
        tier = "A"
    elif score >= 55 and not small:
        tier = "B"
    elif score >= 40:
        tier = "C"
    else:
        tier = "D"
    return {"score": round(score), "tier": tier, "label": dict(RADAR_TIERS)[tier], "riskNowPct": _r2(risk_now),
            "pros": pros[:5], "cons": cons[:5]}
