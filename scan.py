#!/usr/bin/env python3

from __future__ import annotations

import json
import math
import os
import sys
import traceback

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
OUT = BASE / "data.json"

MIN_MCAP_CR = 1000.0

# Discover upcoming result meetings this far forward.
UPCOMING_DAYS = 21

# Pick up newly declared/filed results from this many days back.
RECENT_DAYS = 10

# Keep recent post-result stocks on the radar even after discovery moves on.
KEEP_DAYS = 30


def now_iso():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def first(d, *keys, default=None):

    if not isinstance(d, dict):
        return default

    for key in keys:

        value = d.get(key)

        if value not in (None, ""):
            return value

    return default


def fnum(value):

    try:

        x = float(
            str(value)
            .replace(",", "")
            .replace("%", "")
            .strip()
        )

        return (
            x
            if math.isfinite(x)
            else None
        )

    except Exception:
        return None


def pdate(value):

    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    value = str(value).strip()

    formats = (
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%b-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    )

    for fmt in formats:

        try:
            return datetime.strptime(
                value[:20],
                fmt
            ).date()

        except Exception:
            pass

    try:

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        ).date()

    except Exception:
        return None


def iso(value):

    d = pdate(value)

    return (
        d.isoformat()
        if d
        else None
    )


def clean_symbol(value):

    symbol = (
        str(value or "")
        .strip()
        .upper()
    )

    if symbol.endswith(".NS"):
        symbol = symbol[:-3]

    return symbol


def get_method(obj, *names):

    for name in names:

        fn = getattr(
            obj,
            name,
            None
        )

        if callable(fn):
            return fn

    raise AttributeError(
        "/".join(names)
    )


def safe_call(
    label,
    fn,
    *args,
    **kwargs
):

    try:

        return fn(
            *args,
            **kwargs
        )

    except Exception as exc:

        print(
            f"[WARN] {label}: {exc}"
        )

        return None


def extract_symbol(row):

    return clean_symbol(
        first(
            row,
            "symbol",
            "SYMBOL",
            "sm_symbol",
            "smSymbol",
            "nseSymbol",
        )
    )


def extract_company(
    row,
    symbol
):

    return str(
        first(
            row,
            "companyName",
            "company",
            "sm_name",
            "smName",
            "name",
            default=symbol,
        )
    )


def combined_text(row):

    keys = (
        "purpose",
        "bmPurpose",
        "subject",
        "description",
        "desc",
        "remarks",
        "relatingTo",
    )

    return " ".join(
        str(
            row.get(
                key,
                ""
            )
        )
        for key in keys
    ).lower()


def is_result_meeting(row):

    text = combined_text(row)

    return (
        "financial result" in text
        or
        "quarterly result" in text
        or
        "results" in text
    )


# ---------------------------------------------------------
# Previous radar
# ---------------------------------------------------------

def previous_rows():

    if not OUT.exists():
        return {}

    try:

        payload = json.loads(
            OUT.read_text(
                encoding="utf-8"
            )
        )

        rows = (
            payload.get(
                "companies"
            )
            or
            payload.get(
                "stocks"
            )
            or
            []
        )

        result = {}

        for row in rows:

            if not isinstance(
                row,
                dict
            ):
                continue

            symbol = clean_symbol(
                first(
                    row,
                    "symbol",
                    "sym",
                )
            )

            if symbol:
                result[symbol] = row

        return result

    except Exception:
        return {}


# ---------------------------------------------------------
# LIVE DISCOVERY
# ---------------------------------------------------------

def discover(nse):

    today = (
        datetime.now(
            timezone.utc
        ).date()
    )

    recent_start = (
        today
        - timedelta(
            days=RECENT_DAYS
        )
    )

    upcoming_end = (
        today
        + timedelta(
            days=UPCOMING_DAYS
        )
    )

    candidates = {}

    # ---------------------------------------------
    # UPCOMING RESULTS
    # ---------------------------------------------

    board_fn = get_method(
        nse,
        "board_meetings",
        "boardMeetings",
    )

    board_rows = (
        safe_call(
            "NSE board meetings",
            board_fn,
            index="equities",
            from_date=datetime.combine(
                today,
                datetime.min.time()
            ),
            to_date=datetime.combine(
                upcoming_end,
                datetime.max.time()
            ),
        )
        or
        []
    )

    for row in board_rows:

        if not isinstance(
            row,
            dict
        ):
            continue

        if not is_result_meeting(
            row
        ):
            continue

        symbol = extract_symbol(
            row
        )

        if not symbol:
            continue

        result_date = iso(
            first(
                row,
                "meetingDate",
                "bmDate",
                "date",
                "meeting_date",
            )
        )

        candidates[
            symbol
        ] = {

            "symbol":
                symbol,

            "sym":
                symbol,

            "name":
                extract_company(
                    row,
                    symbol
                ),

            "resultDate":
                result_date,

            "result_date":
                result_date,

            "quarter":
                str(
                    first(
                        row,
                        "relatingTo",
                        "quarter",
                        default=
                        "Upcoming result",
                    )
                ),

            "bucket":
                "Upcoming",

            "discoverySource":
                "NSE board meetings",
        }

    # ---------------------------------------------
    # NEWLY DECLARED RESULTS
    # ---------------------------------------------

    results_fn = get_method(
        nse,
        "financial_results",
        "financialResults",
    )

    result_rows = (
        safe_call(
            "NSE financial results",
            results_fn,
            segment="equities",
            period="quarterly",

            from_date=
                datetime.combine(
                    recent_start,
                    datetime.min.time()
                ),

            to_date=
                datetime.combine(
                    today,
                    datetime.max.time()
                ),
        )
        or
        []
    )

    for row in result_rows:

        if not isinstance(
            row,
            dict
        ):
            continue

        symbol = extract_symbol(
            row
        )

        if not symbol:
            continue

        result_date = (
            iso(
                first(
                    row,
                    "broadcastDate",
                    "broadcastDateTime",
                    "filingDate",
                    "date",
                )
            )
            or
            today.isoformat()
        )

        candidates[
            symbol
        ] = {

            **candidates.get(
                symbol,
                {}
            ),

            "symbol":
                symbol,

            "sym":
                symbol,

            "name":
                extract_company(
                    row,
                    symbol
                ),

            "resultDate":
                result_date,

            "result_date":
                result_date,

            "quarter":
                str(
                    first(
                        row,
                        "relatingTo",
                        "toDate",
                        "periodEnded",
                        default="Quarterly",
                    )
                ),

            "bucket":
                "Post-results",

            "discoverySource":
                "NSE financial results",
        }

    return (
        candidates,
        len(board_rows),
        len(result_rows),
    )


# ---------------------------------------------------------
# Keep recent tracked stocks
# ---------------------------------------------------------

def carry_forward(
    candidates,
    old
):

    cutoff = (
        datetime.now(
            timezone.utc
        ).date()
        -
        timedelta(
            days=KEEP_DAYS
        )
    )

    kept = 0

    for symbol, row in old.items():

        if symbol in candidates:
            continue

        result_date = pdate(
            first(
                row,
                "resultDate",
                "result_date",
            )
        )

        status = str(
            first(
                row,
                "bucket",
                "peadStatus",
                "stage",
                default="",
            )
        ).lower()

        keep = (

            (
                result_date
                and
                result_date >= cutoff
            )

            or

            "qualified" in status

            or

            "caution" in status

            or

            "review" in status
        )

        if not keep:
            continue

        candidates[
            symbol
        ] = {

            **row,

            "symbol":
                symbol,

            "sym":
                symbol,
        }

        kept += 1

    return kept


# ---------------------------------------------------------
# LIVE NSE MARKET DATA
# ---------------------------------------------------------

def unpack_detailed(payload):

    if not isinstance(
        payload,
        dict
    ):
        return {}

    rows = payload.get(
        "equityResponse"
    )

    if (
        isinstance(
            rows,
            list
        )
        and
        rows
        and
        isinstance(
            rows[0],
            dict
        )
    ):
        return rows[0]

    return payload


def enrich_market(
    nse,
    row
):

    symbol = row[
        "symbol"
    ]

    detailed_fn = get_method(
        nse,
        "get_detailed_scrip_data",
        "getDetailedScripData",
    )

    payload = safe_call(
        f"{symbol} quote",
        detailed_fn,
        symbol,
    )

    output = dict(
        row
    )

    output.update({

        "liveStatus":
            "unavailable",

        "liveError":
            None,

        "priceTimestamp":
            now_iso(),
    })

    if not payload:

        output[
            "liveError"
        ] = (
            "NSE quote unavailable"
        )

        return output

    data = unpack_detailed(
        payload
    )

    order = (
        data.get(
            "orderBook"
        )
        or
        {}
    )

    meta = (
        data.get(
            "metaData"
        )
        or
        data.get(
            "metadata"
        )
        or
        {}
    )

    trade = (
        data.get(
            "tradeInfo"
        )
        or
        {}
    )

    sec = (
        data.get(
            "secInfo"
        )
        or
        {}
    )

    price = (
        fnum(
            first(
                order,
                "lastPrice"
            )
        )
        or
        fnum(
            first(
                trade,
                "lastPrice"
            )
        )
        or
        fnum(
            first(
                meta,
                "closePrice"
            )
        )
    )

    previous_close = fnum(
        first(
            meta,
            "previousClose",
            "basePrice",
        )
    )

    change_pct = fnum(
        first(
            meta,
            "pChange"
        )
    )

    if (
        change_pct is None
        and
        price is not None
        and
        previous_close not in (
            None,
            0
        )
    ):

        change_pct = round(
            (
                price
                -
                previous_close
            )
            /
            previous_close
            *
            100,
            2,
        )

    # NSE returns totalMarketCap
    # in rupees.
    total_market_cap = fnum(
        first(
            trade,
            "totalMarketCap"
        )
    )

    market_cap_cr = (

        round(
            total_market_cap
            /
            10_000_000,
            2
        )

        if
        total_market_cap
        is not None

        else
        None
    )

    output.update({

        "name":
            str(
                first(
                    meta,
                    "companyName",
                    default=
                    output.get(
                        "name",
                        symbol
                    ),
                )
            ),

        "sector":
            str(
                first(
                    sec,
                    "sector",
                    "basicIndustry",
                    "industryInfo",
                    default="—",
                )
            ),

        "industry":
            str(
                first(
                    sec,
                    "industryInfo",
                    "basicIndustry",
                    default="—",
                )
            ),

        "price":
            price,

        "lastPrice":
            price,

        "previousClose":
            previous_close,

        "changePct":
            change_pct,

        "marketCapCr":
            market_cap_cr,

        "marketCapPass":
            (
                market_cap_cr
                >=
                MIN_MCAP_CR

                if
                market_cap_cr
                is not None

                else
                None
            ),

        "volume":
            fnum(
                first(
                    trade,
                    "totalTradedVolume",
                    "quantitytraded",
                )
            ),

        "deliveryPct":
            fnum(
                first(
                    trade,
                    "deliveryToTradedQuantity",
                )
            ),

        "indexList":
            sec.get(
                "indexList"
            )
            or
            [],

        "liveStatus":
            (
                "ok"
                if price is not None
                else
                "unavailable"
            ),

        "liveError":
            (
                None
                if price is not None
                else
                "Price missing"
            ),

        "priceTimestamp":
            first(
                payload,
                "lastUpdateTime",
                default=now_iso(),
            ),
    })

    return output


# ---------------------------------------------------------
# Result comparison
# ---------------------------------------------------------

def growth(
    current,
    old
):

    if (
        current is None
        or
        old in (
            None,
            0
        )
    ):
        return None

    return round(
        (
            current
            -
            old
        )
        /
        abs(old)
        *
        100,
        2,
    )


def enrich_results(
    nse,
    row
):

    output = dict(
        row
    )

    # Results have not happened yet.
    if (
        str(
            output.get(
                "bucket",
                ""
            )
        ).lower()
        ==
        "upcoming"
    ):
        return output

    compare_fn = get_method(
        nse,
        "results_comparison",
        "resultsComparison",
    )

    payload = (
        safe_call(
            output[
                "symbol"
            ]
            +
            " results",

            compare_fn,

            output[
                "symbol"
            ],
        )
        or
        {}
    )

    rows = (
        payload.get(
            "resCmpData"
        )
        if
        isinstance(
            payload,
            dict
        )
        else
        None
    )

    if (
        not isinstance(
            rows,
            list
        )
        or
        not rows
    ):
        return output

    def value(
        result_row,
        *keys
    ):

        return fnum(
            first(
                result_row,
                *keys
            )
        )

    latest = (
        rows[0]
        if
        isinstance(
            rows[0],
            dict
        )
        else
        {}
    )

    year_ago = (

        rows[4]

        if
        len(rows) >= 5
        and
        isinstance(
            rows[4],
            dict
        )

        else
        None
    )

    previous = (

        rows[1]

        if
        len(rows) >= 2
        and
        isinstance(
            rows[1],
            dict
        )

        else
        None
    )

    revenue = value(
        latest,
        "re_total_inc",
        "totalIncome",
        "revenue",
        "total_revenue",
    )

    pat = value(
        latest,
        "re_net_profit",
        "netProfit",
        "pat",
        "profitAfterTax",
    )

    revenue_yoy = (
        growth(
            revenue,
            value(
                year_ago,
                "re_total_inc",
                "totalIncome",
                "revenue",
                "total_revenue",
            )
        )
        if year_ago
        else None
    )

    pat_yoy = (
        growth(
            pat,
            value(
                year_ago,
                "re_net_profit",
                "netProfit",
                "pat",
                "profitAfterTax",
            )
        )
        if year_ago
        else None
    )

    revenue_qoq = (
        growth(
            revenue,
            value(
                previous,
                "re_total_inc",
                "totalIncome",
                "revenue",
                "total_revenue",
            )
        )
        if previous
        else None
    )

    pat_qoq = (
        growth(
            pat,
            value(
                previous,
                "re_net_profit",
                "netProfit",
                "pat",
                "profitAfterTax",
            )
        )
        if previous
        else None
    )

    output.update({

        "latestRevenueLakh":
            revenue,

        "latestPatLakh":
            pat,

        "revenueYoY":
            revenue_yoy,

        "patYoY":
            pat_yoy,

        "revenueQoQ":
            revenue_qoq,

        "patQoQ":
            pat_qoq,

        "revenueP
