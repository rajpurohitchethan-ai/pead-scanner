#!/usr/bin/env python3

import json
import math
import sys
import time
import warnings
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf


BASE = Path(__file__).resolve().parent
DATA = BASE / "data.json"

IST = ZoneInfo("Asia/Kolkata")

# PEAD thresholds
REV_YOY_MIN = 10.0
PAT_YOY_MIN = 15.0
PAT_QOQ_FLOOR = -10.0

PRICED_IN_RUNUP_PCT = 15.0

RVOL_MIN = 1.20
EPS_SURPRISE_MIN = 5.0
MARKET_SURPRISE_PROXY_MIN = 3.0

LIQUIDITY_TURNOVER_CR_MIN = 5.0
CASHFLOW_TO_PAT_MIN = 0.50


SECTOR_PROXIES = {
    "bank": "^NSEBANK",
    "private bank": "^NSEBANK",
    "public bank": "^NSEBANK",
    "financial": "^CNXFIN",
    "nbfc": "^CNXFIN",
    "finance": "^CNXFIN",
    "wealth": "^CNXFIN",
    "asset management": "^CNXFIN",
    "insurance": "^CNXFIN",
    "it services": "^CNXIT",
    "software": "^CNXIT",
    "technology": "^CNXIT",
    "auto": "^CNXAUTO",
    "tyre": "^CNXAUTO",
    "pharma": "^CNXPHARMA",
    "healthcare": "^CNXPHARMA",
    "fmcg": "^CNXFMCG",
    "food": "^CNXFMCG",
    "consumer staples": "^CNXFMCG",
    "steel": "^CNXMETAL",
    "metal": "^CNXMETAL",
    "mining": "^CNXMETAL",
    "real estate": "^CNXREALTY",
    "realty": "^CNXREALTY",
    "cement": "^CNXINFRA",
    "construction": "^CNXINFRA",
    "infrastructure": "^CNXINFRA",
    "energy": "^CNXENERGY",
    "oil": "^CNXENERGY",
    "gas": "^CNXENERGY",
    "media": "^CNXMEDIA",
}


def num(v):
    if v in (None, ""):
        return None

    try:
        x = float(
            str(v)
            .replace(",", "")
            .replace("%", "")
            .strip()
        )
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def bool_value(v):
    if v is True or v is False:
        return v

    if v is None:
        return None

    s = str(v).strip().lower()

    if s in {
        "true",
        "yes",
        "pass",
        "passed",
        "qualified",
        "satisfied",
        "ok",
    }:
        return True

    if s in {
        "false",
        "no",
        "fail",
        "failed",
        "not satisfied",
    }:
        return False

    return None


def round2(v):
    x = num(v)
    return round(x, 2) if x is not None else None


def parse_date(v):
    if not v:
        return None

    if isinstance(v, datetime):
        return v.date()

    if isinstance(v, date):
        return v

    s = str(v).strip()

    formats = (
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(
            s.replace("Z", "+00:00")
        ).date()
    except ValueError:
        return None


def load_data():
    if not DATA.exists():
        raise RuntimeError(
            "data.json missing; run scan.py first"
        )

    payload = json.loads(
        DATA.read_text(encoding="utf-8")
    )

    if payload.get("scannerMode") != "live-discovery":
        raise RuntimeError(
            "Expected scannerMode=live-discovery"
        )

    rows = (
        payload.get("stocks")
        or payload.get("companies")
        or []
    )

    if not rows:
        raise RuntimeError(
            "No live-discovery stocks found"
        )

    return payload, rows


def yahoo_ticker(row):
    s = str(
        row.get("symbol")
        or row.get("sym")
        or ""
    ).strip().upper()

    if not s:
        return None

    if s.endswith(".NS"):
        return s

    return f"{s}.NS"


def history_for(batch, ticker):
    if batch is None or batch.empty or not ticker:
        return pd.DataFrame()

    try:
        if isinstance(batch.columns, pd.MultiIndex):
            level0 = batch.columns.get_level_values(0)
            level1 = batch.columns.get_level_values(1)

            if ticker in level0:
                h = batch[ticker].copy()
            elif ticker in level1:
                h = batch.xs(
                    ticker,
                    axis=1,
                    level=1
                ).copy()
            else:
                return pd.DataFrame()
        else:
            h = batch.copy()

        if not h.empty and "Close" in h:
            h = h[h["Close"].notna()]

        return h.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def technicals(h):
    if h.empty or "Close" not in h:
        return {}

    close = h["Close"].astype(float)

    out = {
        "lastClose": round2(close.iloc[-1]),
        "ma10": (
            round2(
                close.ewm(
                    span=10,
                    adjust=False
                ).mean().iloc[-1]
            )
            if len(close) >= 10
            else None
        ),
        "ma20": (
            round2(
                close.ewm(
                    span=20,
                    adjust=False
                ).mean().iloc[-1]
            )
            if len(close) >= 20
            else None
        ),
        "ma50": (
            round2(
                close.rolling(50).mean().iloc[-1]
            )
            if len(close) >= 50
            else None
        ),
        "ma200": (
            round2(
                close.rolling(200).mean().iloc[-1]
            )
            if len(close) >= 200
            else None
        ),
    }

    if "Volume" in h and len(h) >= 21:
        volume = h["Volume"].astype(float)

        average_volume = float(
            volume.iloc[-21:-1].mean()
        )

        out["relativeVolume"] = (
            round2(
                float(volume.iloc[-1])
                / average_volume
            )
            if average_volume > 0
            else None
        )

        turnover = (
            (close.tail(20) * volume.tail(20)).mean()
            / 10_000_000
        )

        out["avgTurnover20dCr"] = round2(turnover)
    else:
        out["relativeVolume"] = None
        out["avgTurnover20dCr"] = None

    return out


def result_metrics(h, result_date):
    out = {
        "preResultRunupPct": None,
        "resultDayReturnPct": None,
        "resultDayHigh": None,
        "resultDayLow": None,
        "preResult20dHigh": None,
        "pricedIn": False,
    }

    if (
        h.empty
        or result_date is None
        or "Close" not in h
    ):
        return out

    result_index = None

    for i, idx in enumerate(h.index):
        if pd.Timestamp(idx).date() >= result_date:
            result_index = i
            break

    end_index = len(h) if result_index is None else result_index
    pre = h.iloc[:end_index]

    if len(pre) >= 20:
        start_close = num(pre["Close"].iloc[-20])
        end_close = num(pre["Close"].iloc[-1])

        if (
            start_close not in (None, 0)
            and end_close is not None
        ):
            runup = (
                (end_close / start_close) - 1
            ) * 100

            out["preResultRunupPct"] = round2(runup)

            out["pricedIn"] = (
                runup > PRICED_IN_RUNUP_PCT
            )

        if "High" in pre:
            out["preResult20dHigh"] = round2(
                pre["High"].tail(20).max()
            )

    if result_index is not None and result_index < len(h):
        result_row = h.iloc[result_index]

        previous_close = (
            num(h.iloc[result_index - 1]["Close"])
            if result_index > 0
            else None
        )

        result_close = num(
            result_row.get("Close")
        )

        if (
            previous_close not in (None, 0)
            and result_close is not None
        ):
            out["resultDayReturnPct"] = round2(
                (
                    result_close
                    / previous_close
                    - 1
                ) * 100
            )

        if "High" in h:
            out["resultDayHigh"] = round2(
                result_row.get("High")
            )

        if "Low" in h:
            out["resultDayLow"] = round2(
                result_row.get("Low")
            )

    return out


def sector_proxy(row):
    text = (
        f"{row.get('sector', '')} "
        f"{row.get('industry', '')} "
    ).lower()

    for key, value in SECTOR_PROXIES.items():
        if key in text:
            return value

    return "^NSEI"


def sector_tailwind(h):
    if (
        h.empty
        or "Close" not in h
        or len(h) < 200
    ):
        return None, {}

    close = h["Close"].astype(float)

    last = float(close.iloc[-1])

    ma10 = float(
        close.ewm(
            span=10,
            adjust=False
        ).mean().iloc[-1]
    )

    ma20 = float(
        close.ewm(
            span=20,
            adjust=False
        ).mean().iloc[-1]
    )

    ma50 = float(
        close.rolling(50).mean().iloc[-1]
    )

    ma200 = float(
        close.rolling(200).mean().iloc[-1]
    )

    passed = all(
        last > value
        for value in (
            ma10,
            ma20,
            ma50,
            ma200
        )
    )

    return passed, {
        "sectorClose": round2(last),
        "sectorMa10": round2(ma10),
        "sectorMa20": round2(ma20),
        "sectorMa50": round2(ma50),
        "sectorMa200": round2(ma200),
    }


def is_financial(row):
    text = (
        f"{row.get('sector', '')} "
        f"{row.get('industry', '')}"
    ).lower()

    return any(
        x in text
        for x in (
            "bank",
            "financial",
            "finance",
            "nbfc",
            "insurance",
            "asset management",
            "wealth",
        )
    )


def statement_row(df, names):
    if (
        df is None
        or getattr(df, "empty", True)
    ):
        return None

    normalized = {
        str(index).strip().lower(): index
        for index in df.index
    }

    for name in names:
        key = name.lower()

        if key in normalized:
            return normalized[key]

    for low, original in normalized.items():
        if any(
            name.lower() in low
            for name in names
        ):
            return original

    return None


def cashflow_gate(ticker, row):
    # Operating cash flow is not comparable for banks/NBFCs.
    if is_financial(row):
        pat_yoy = num(
            row.get("patYoY")
        )

        quality = bool_value(
            row.get("earningsQualityPass")
        )

        passed = (
            quality is True
            and pat_yoy is not None
            and pat_yoy > 0
        )

        return passed, {
            "cashFlowMethod":
                "financial-sector proxy",

            "cashFlowEvidence":
                (
                    "CFO not comparable for "
                    "lenders/financials; "
                    "positive earnings quality "
                    "+ PAT YoY growth used"
                ),
        }

    try:
        cashflow = ticker.quarterly_cash_flow

        operating_row = statement_row(
            cashflow,
            [
                "Operating Cash Flow",
                "Total Cash From Operating Activities",
                "Cash Flow From Continuing Operating Activities",
            ]
        )

        income_row = statement_row(
            cashflow,
            [
                "Net Income From Continuing Operations",
                "Net Income",
            ]
        )

        if operating_row is None:
            return None, {
                "cashFlowMethod":
                    "yfinance quarterly cash flow",

                "cashFlowEvidence":
                    "Operating cash flow unavailable",
            }

        operating_series = (
            cashflow
            .loc[operating_row]
            .dropna()
        )

        income_series = (
            cashflow
            .loc[income_row]
            .dropna()
            if income_row is not None
            else pd.Series(dtype=float)
        )

        operating_cashflow = (
            num(operating_series.iloc[0])
            if not operating_series.empty
            else None
        )

        net_income = (
            num(income_series.iloc[0])
            if not income_series.empty
            else None
        )

        if operating_cashflow is None:
            return None, {
                "cashFlowMethod":
                    "yfinance quarterly cash flow",

                "cashFlowEvidence":
                    "Latest operating cash flow unavailable",
            }

        ratio = (
            operating_cashflow
            / abs(net_income)
            if net_income not in (None, 0)
            else None
        )

        passed = (
            operating_cashflow > 0
            and (
                ratio is None
                or ratio >= CASHFLOW_TO_PAT_MIN
            )
        )

        return passed, {
            "operatingCashFlow":
                round2(operating_cashflow),

            "cashFlowToNetIncome":
                round2(ratio),

            "cashFlowMethod":
                "yfinance quarterly cash flow",

            "cashFlowEvidence":
                (
                    "OCF positive"
                    +
                    (
                        f"; OCF/net income {ratio:.2f}x"
                        if ratio is not None
                        else ""
                    )
                ),
        }

    except Exception as exc:
        return None, {
            "cashFlowMethod":
                "yfinance quarterly cash flow",

            "cashFlowEvidence":
                (
                    "Unavailable: "
                    f"{type(exc).__name__}"
                ),
        }


def eps_surprise(ticker, result_date):
    try:
        earnings = ticker.get_earnings_dates(
            limit=12
        )

        if (
            earnings is None
            or earnings.empty
            or result_date is None
        ):
            return None

        matches = []

        for index, row in earnings.iterrows():
            distance = abs(
                (
                    pd.Timestamp(index).date()
                    - result_date
                ).days
            )

            if distance <= 4:
                matches.append(
                    (distance, row)
                )

        if not matches:
            return None

        row = sorted(
            matches,
            key=lambda x: x[0]
        )[0][1]

        for column in (
            "Surprise(%)",
            "Surprise %",
            "Surprise",
        ):
            if (
                column in row.index
                and num(row.get(column)) is not None
            ):
                return num(
                    row.get(column)
                )

        estimate = num(
            row.get("EPS Estimate")
        )

        reported = num(
            row.get("Reported EPS")
        )

        if (
            estimate not in (None, 0)
            and reported is not None
        ):
            return (
                reported
                / estimate
                - 1
            ) * 100

        return None

    except Exception:
        return None


def build_checks(row):
    market_cap = num(
        row.get("marketCapCr")
    )

    return [
        {
            "label": "Results released",
            "value": row.get("resultsReleased"),
            "note": row.get(
                "resultsEvidence",
                ""
            ),
        },
        {
            "label": "Market cap > ₹1,000 Cr",
            "value": row.get("marketCapPass"),
            "note": (
                f"₹{market_cap:,.0f} Cr"
                if market_cap is not None
                else "Unverified"
            ),
        },
        {
            "label": "Earnings acceleration",
            "value": row.get(
                "earningsAccelerationPass"
            ),
            "note": row.get(
                "earningsEvidence",
                ""
            ),
        },
        {
            "label": "Earnings quality",
            "value": row.get(
                "earningsQualityPass"
            ),
            "note": row.get(
                "qualityEvidence",
                ""
            ),
        },
        {
            "label": "Cash flow",
            "value": row.get(
                "cashFlowPass"
            ),
            "note": row.get(
                "cashFlowEvidence",
                ""
            ),
        },
        {
            "label": "Surprise",
            "value": row.get(
                "surprisePass"
            ),
            "note": row.get(
                "surpriseEvidence",
                ""
            ),
        },
        {
            "label":
                "Post-result price/volume confirmation",
            "value": row.get(
                "priceVolumePass"
            ),
            "note": row.get(
                "priceVolumeEvidence",
                ""
            ),
        },
        {
            "label": "Liquidity",
            "value": row.get(
                "liquidityPass"
            ),
            "note": row.get(
                "liquidityEvidence",
                ""
            ),
        },
    ]


def qualify(
    row,
    stock_history,
    sector_history
):
    out = dict(row)

    today = datetime.now(
        IST
    ).date()

    result_date = parse_date(
        out.get("resultDate")
        or out.get("result_date")
    )

    source = str(
        out.get("discoverySource")
        or ""
    ).lower()

    old_bucket = str(
        out.get("bucket")
        or out.get("peadStatus")
        or ""
    ).lower()

    results_released = (
        "financial results" in source
        or old_bucket in {
            "post-results",
            "in review",
            "qualified",
            "caution",
        }
    )

    if (
        result_date is not None
        and result_date > today
    ):
        results_released = False

    out[
        "resultsReleased"
    ] = results_released

    out[
        "resultsEvidence"
    ] = (
        "NSE financial-results filing detected"
        if "financial results" in source
        else (
            "Post-result live record"
            if results_released
            else "Awaiting result filing"
        )
    )

    out.update(
        technicals(
            stock_history
        )
    )

    out.update(
        result_metrics(
            stock_history,
            result_date
        )
    )

    revenue_yoy = num(
        out.get(
            "revenueYoY"
        )
    )

    pat_yoy = num(
        out.get(
            "patYoY"
        )
    )

    pat_qoq = num(
        out.get(
            "patQoQ"
        )
    )

    if (
        not results_released
        or revenue_yoy is None
        or pat_yoy is None
    ):
        earnings_pass = None

    else:
        earnings_pass = (
            revenue_yoy >= REV_YOY_MIN
            and pat_yoy >= PAT_YOY_MIN
            and (
                pat_qoq is None
                or pat_qoq >= PAT_QOQ_FLOOR
            )
        )

    out[
        "earningsAccelerationPass"
    ] = earnings_pass

    # Frontend compatibility
    out[
        "revenuePatPass"
    ] = earnings_pass

    out[
        "earningsEvidence"
    ] = (
        (
            f"Revenue YoY {revenue_yoy:.1f}%, "
            f"PAT YoY {pat_yoy:.1f}%"
            +
            (
                f", PAT QoQ {pat_qoq:.1f}%"
                if pat_qoq is not None
                else ""
            )
        )
        if (
            revenue_yoy is not None
            and pat_yoy is not None
        )
        else "Growth data unavailable"
    )

    latest_revenue = num(
        out.get(
            "latestRevenueLakh"
        )
    )

    latest_pat = num(
        out.get(
            "latestPatLakh"
        )
    )

    margin = (
        latest_pat
        / latest_revenue
        * 100
        if (
            latest_revenue not in (None, 0)
            and latest_pat is not None
        )
        else None
    )

    if not results_released:
        quality_pass = None

    elif (
        latest_revenue is not None
        and latest_pat is not None
    ):
        quality_pass = (
            latest_revenue > 0
            and latest_pat > 0
            and (
                margin is None
                or margin >= 3
            )
        )

    else:
        quality_pass = bool_value(
            out.get(
                "earningsQualityPass"
            )
        )

    out[
        "earningsQualityPass"
    ] = quality_pass

    out[
        "netMarginPct"
    ] = round2(
        margin
    )

    out[
        "qualityEvidence"
    ] = (
        f"PAT positive; net margin {margin:.1f}%"
        if margin is not None
        else (
            "Positive revenue/PAT required"
            if results_released
            else "Awaiting results"
        )
    )

    sector_pass, sector_data = (
        sector_tailwind(
            sector_history
        )
    )

    out[
        "sectorTailwind"
    ] = sector_pass

    out.update(
        sector_data
    )

    turnover = num(
        out.get(
            "avgTurnover20dCr"
        )
    )

    out[
        "liquidityPass"
    ] = (
        turnover >= LIQUIDITY_TURNOVER_CR_MIN
        if turnover is not None
        else None
    )

    out[
        "liquidityEvidence"
    ] = (
        f"20d avg traded value ₹{turnover:.1f} Cr"
        if turnover is not None
        else "20d turnover unavailable"
    )

    if not results_released:
        out[
            "cashFlowPass"
        ] = None

        out[
            "cashFlowEvidence"
        ] = "Awaiting results"

        out[
            "surprisePass"
        ] = None

        out[
            "surprisePct"
        ] = None

        out[
            "surpriseEvidence"
        ] = "Awaiting results"

        out[
            "priceVolumePass"
        ] = None

        out[
            "priceVolumeEvidence"
        ] = (
            "Awaiting post-result confirmation"
        )

    else:
        ticker_symbol = yahoo_ticker(
            out
        )

        ticker = (
            yf.Ticker(
                ticker_symbol
            )
            if ticker_symbol
            else None
        )

        if ticker is not None:
            cash_pass, cash_data = (
                cashflow_gate(
                    ticker,
                    out
                )
            )
        else:
            cash_pass, cash_data = (
                None,
                {
                    "cashFlowEvidence":
                        "Ticker unavailable",
                    "cashFlowMethod":
                        "unavailable",
                },
            )

        out[
            "cashFlowPass"
        ] = cash_pass

        out.update(
            cash_data
        )

        surprise = (
            eps_surprise(
                ticker,
                result_date
            )
            if ticker is not None
            else None
        )

        result_return = num(
            out.get(
                "resultDayReturnPct"
            )
        )

        rvol = num(
            out.get(
                "relativeVolume"
            )
        )

        if surprise is not None:
            surprise_pass = (
                surprise >= EPS_SURPRISE_MIN
            )

            surprise_text = (
                f"Reported EPS surprise "
                f"{surprise:.1f}%"
            )

            surprise_method = (
                "reported EPS surprise"
            )

        elif result_return is not None:
            surprise_pass = (
                result_return
                >= MARKET_SURPRISE_PROXY_MIN
                and (
                    rvol is None
                    or rvol >= RVOL_MIN
                )
            )

            surprise_text = (
                f"Market-reaction proxy: "
                f"result-day {result_return:.1f}%"
                +
                (
                    f", RVOL {rvol:.2f}x"
                    if rvol is not None
                    else ""
                )
            )

            surprise_method = (
                "market-reaction proxy"
            )

        else:
            surprise_pass = None

            surprise_text = (
                "EPS surprise and "
                "result-day reaction unavailable"
            )

            surprise_method = (
                "unavailable"
            )

        out[
            "surprisePass"
        ] = surprise_pass

        out[
            "surprisePct"
        ] = round2(
            surprise
        )

        out[
            "surpriseMethod"
        ] = surprise_method

        out[
            "surpriseEvidence"
        ] = surprise_text

        last_close = (
            num(
                out.get(
                    "lastClose"
                )
            )
            or
            num(
                out.get(
                    "price"
                )
            )
        )

        ma10 = num(
            out.get(
                "ma10"
            )
        )

        if None in (
            last_close,
            ma10,
            result_return,
            rvol,
        ):
            price_volume_pass = None

        else:
            price_volume_pass = (
                last_close > ma10
                and rvol >= RVOL_MIN
                and result_return >= 2
            )

        out[
            "priceVolumePass"
        ] = price_volume_pass

        out[
            "priceVolumeEvidence"
        ] = (
            (
                f"Close ₹{last_close:.2f} "
                f"vs EMA10 ₹{ma10:.2f}; "
                f"RVOL {rvol:.2f}x; "
                f"result-day {result_return:.1f}%"
            )
            if None not in (
                last_close,
                ma10,
                rvol,
                result_return,
            )
            else "Post-result evidence incomplete"
        )

    checklist = build_checks(
        out
    )

    values = [
        item["value"]
        for item in checklist
    ]

    out[
        "checks"
    ] = checklist

    out[
        "score"
    ] = sum(
        value is True
        for value in values
    )

    out[
        "knownChecks"
    ] = sum(
        value is not None
        for value in values
    )

    out[
        "scoreText"
    ] = f'{out["score"]}/8'

    all_eight = all(
        value is True
        for value in values
    )

    priced_in = (
        out.get(
            "pricedIn"
        )
        is True
    )

    if priced_in:
        bucket = "Caution"

    elif not results_released:
        bucket = "Upcoming"

    elif all_eight:
        bucket = "Qualified"

    else:
        bucket = "Post-results"

    out[
        "bucket"
    ] = bucket

    out[
        "peadStatus"
    ] = bucket

    out[
        "stage"
    ] = bucket

    out[
        "entry"
    ] = None

    out[
        "sl"
    ] = None

    out[
        "tsl"
    ] = None

    out[
        "entryTriggerPass"
    ] = None

    out[
        "candidateStatus"
    ] = None

    out[
        "allocationPct"
    ] = None

    if bucket == "Qualified":
        highs = [
            value

            for value in (
                num(
                    out.get(
                        "resultDayHigh"
                    )
                ),

                num(
                    out.get(
                        "preResult20dHigh"
                    )
                ),

                (
                    num(
                        stock_history[
                            "High"
                        ]
                        .tail(5)
                        .max()
                    )
                    if (
                        not stock_history.empty
                        and "High"
                        in stock_history
                    )
                    else None
                ),
            )

            if value is not None
        ]

        if highs:
            entry = (
                max(
                    highs
                )
                * 1.002
            )

            out[
                "entry"
            ] = round2(
                entry
            )

            current = (
                num(
                    out.get(
                        "price"
                    )
                )
                or
                num(
                    out.get(
                        "lastClose"
                    )
                )
            )

            out[
                "entryTriggerPass"
            ] = (
                current is not None
                and current >= entry
            )

        lows = [
            value

            for value in (
                num(
                    out.get(
                        "resultDayLow"
                    )
                ),

                num(
                    out.get(
                        "ma20"
                    )
                ),
            )

            if value is not None
        ]

        if lows:
            out[
                "sl"
            ] = round2(
                min(
                    lows
                )
                * 0.995
            )

        out[
            "tsl"
        ] = round2(
            out.get(
                "ma10"
            )
        )

        out[
            "candidateStatus"
        ] = (
            "Entry Triggered"
            if out.get(
                "entryTriggerPass"
            )
            else "Potential Candidate"
        )

        rvol = num(
            out.get(
                "relativeVolume"
            )
        )

        if (
            out.get(
                "entryTriggerPass"
            )
            and out.get(
                "sectorTailwind"
            )
            is True
            and rvol is not None
            and rvol >= 2
        ):
            out[
                "allocationPct"
            ] = 30

        elif out.get(
            "entryTriggerPass"
        ):
            out[
                "allocationPct"
            ] = 20

        else:
            out[
                "allocationPct"
            ] = 10

    out[
        "qualificationVersion"
    ] = "pead-v1.0"

    return out


def main():
    warnings.filterwarnings(
        "ignore"
    )

    payload, rows = load_data()

    stocks = [
        ticker

        for ticker in (
            yahoo_ticker(
                row
            )
            for row in rows
        )

        if ticker
    ]

    proxies = sorted(
        {
            sector_proxy(
                row
            )
            for row in rows
        }
    )

    tickers = sorted(
        set(
            stocks
            + proxies
        )
    )

    print(
        f"Downloading history for "
        f"{len(tickers)} stocks/indices..."
    )

    history = yf.download(
        tickers=tickers,
        period="14mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        threads=True,
        progress=False,
        timeout=20,
    )

    output = []

    counts = {
        "Upcoming": 0,
        "Post-results": 0,
        "Caution": 0,
        "Qualified": 0,
    }

    for index, row in enumerate(
        rows,
        1
    ):
        stock_symbol = (
            row.get(
                "symbol"
            )
            or
            row.get(
                "sym"
            )
            or
            "UNKNOWN"
        )

        print(
            f"[{index}/{len(rows)}] "
            f"{stock_symbol}"
        )

        try:
            qualified = qualify(
                row,

                history_for(
                    history,
                    yahoo_ticker(
                        row
                    )
                ),

                history_for(
                    history,
                    sector_proxy(
                        row
                    )
                )
            )

        except Exception as exc:
            qualified = dict(
                row
            )

            qualified[
                "qualificationError"
            ] = (
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            qualified[
                "bucket"
            ] = (
                "Upcoming"
                if str(
                    row.get(
                        "bucket",
                        ""
                    )
                ).lower()
                ==
                "upcoming"
                else
                "Post-results"
            )

            qualified[
                "peadStatus"
            ] = qualified[
                "bucket"
            ]

            qualified[
                "stage"
            ] = qualified[
                "bucket"
            ]

        output.append(
            qualified
        )

        bucket = qualified.get(
            "bucket",
            "Post-results"
        )

        counts[
            bucket
        ] = (
            counts.get(
                bucket,
                0
            )
            + 1
        )

        if qualified.get(
            "resultsReleased"
        ):
            time.sleep(
                0.15
            )

    payload[
        "stocks"
    ] = output

    payload[
        "companies"
    ] = output

    payload[
        "qualificationVersion"
    ] = "pead-v1.0"

    payload[
        "bucketCounts"
    ] = counts

    payload[
        "qualifiedCount"
    ] = counts.get(
        "Qualified",
        0
    )

    payload[
        "cautionCount"
    ] = counts.get(
        "Caution",
        0
    )

    payload[
        "postResultCount"
    ] = counts.get(
        "Post-results",
        0
    )

    payload[
        "upcomingCount"
    ] = counts.get(
        "Upcoming",
        0
    )

    payload[
        "qualificationRules"
    ] = {
        "revenueYoYMin":
            REV_YOY_MIN,

        "patYoYMin":
            PAT_YOY_MIN,

        "patQoQFloor":
            PAT_QOQ_FLOOR,

        "pricedInRunupPct":
            PRICED_IN_RUNUP_PCT,

        "relativeVolumeMin":
            RVOL_MIN,

        "liquidity20dTurnoverCrMin":
            LIQUIDITY_TURNOVER_CR_MIN,

        "epsSurpriseMin":
            EPS_SURPRISE_MIN,

        "marketSurpriseProxyMin":
            MARKET_SURPRISE_PROXY_MIN,
    }

    temp = DATA.with_suffix(
        ".json.qualify.tmp"
    )

    temp.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False
        )
        +
        "\n",
        encoding="utf-8",
    )

    temp.replace(
        DATA
    )

    print(
        "PEAD QUALIFICATION COMPLETE"
    )

    print(
        json.dumps(
            counts,
            indent=2
        )
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            "QUALIFIER FAILED: "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        sys.exit(1)
