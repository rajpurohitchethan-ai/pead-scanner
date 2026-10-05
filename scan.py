g#!/usr/bin/env python3

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
UPCOMING_DAYS = 21
RECENT_DAYS = 10
KEEP_DAYS = 30


def now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def first(
    d: dict | None,
    *keys: str,
    default=None
):
    if not isinstance(d, dict):
        return default

    for key in keys:
        value = d.get(key)

        if value not in (None, ""):
            return value

    return default


def fnum(value: Any) -> float | None:
    if value in (None, ""):
        return None

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


def parse_date(
    value: Any
) -> date | None:

    if value in (None, ""):
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    for fmt in (
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%b-%Y %H:%M",
    ):
        try:
            return datetime.strptime(
                text,
                fmt
            ).date()

        except ValueError:
            pass

    try:
        return datetime.fromisoformat(
            text.replace(
                "Z",
                "+00:00"
            )
        ).date()

    except Exception:
        return None


def iso_date(
    value: Any
) -> str | None:

    d = parse_date(
        value
    )

    return (
        d.isoformat()
        if d
        else None
    )


def clean_symbol(
    value: Any
) -> str:

    symbol = (
        str(value or "")
        .strip()
        .upper()
    )

    if symbol.endswith(
        ".NS"
    ):
        symbol = symbol[:-3]

    return symbol


def get_method(
    obj: Any,
    *names: str
):
    for name in names:

        fn = getattr(
            obj,
            name,
            None
        )

        if callable(fn):
            return fn

    raise AttributeError(
        "Missing method: "
        +
        " / ".join(names)
    )


def safe_call(
    label: str,
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


# ---------------------------------------------------------
# Previous radar
# ---------------------------------------------------------

def previous_rows() -> dict[str, dict]:

    if not OUT.exists():
        return {}

    try:

        payload = json.loads(
            OUT.read_text(
                encoding="utf-8"
            )
        )

        rows = (
            payload.get("stocks")
            or
            payload.get("companies")
            or
            []
        )

        result: dict[str, dict] = {}

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
                    "ticker"
                )
            )

            if symbol:
                result[symbol] = row

        return result

    except Exception as exc:

        print(
            "[WARN] Could not read "
            f"previous data.json: {exc}"
        )

        return {}


# ---------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------

def result_meeting(
    row: dict
) -> bool:

    text = str(
        first(
            row,
            "bm_purpose",
            "bmPurpose",
            "purpose",
            "description",
            "bm_desc",
            default=""
        )
    ).lower()

    return (
        "financial result"
        in text
        or
        "quarterly result"
        in text
        or
        "results"
        in text
    )


def board_symbol(
    row: dict
) -> str:

    return clean_symbol(
        first(
            row,
            "bm_symbol",
            "symbol",
            "SYMBOL",
            "sm_symbol"
        )
    )


def financial_symbol(
    row: dict
) -> str:

    return clean_symbol(
        first(
            row,
            "symbol",
            "SYMBOL",
            "sm_symbol"
        )
    )


# ---------------------------------------------------------
# LIVE DISCOVERY
# ---------------------------------------------------------

def discover(
    nse
) -> tuple[
    dict[str, dict],
    int,
    int
]:

    today = (
        datetime.now(
            timezone.utc
        ).date()
    )

    recent_start = (
        today
        -
        timedelta(
            days=RECENT_DAYS
        )
    )

    upcoming_end = (
        today
        +
        timedelta(
            days=UPCOMING_DAYS
        )
    )

    candidates: dict[
        str,
        dict
    ] = {}

    # ---------------------------------------------
    # UPCOMING RESULTS
    # ---------------------------------------------

    board_fn = get_method(
        nse,
        "board_meetings",
        "boardMeetings"
    )

    board_rows = (
        safe_call(
            "NSE board meetings",
            board_fn,

            index="equities",

            from_date=
                datetime.combine(
                    today,
                    datetime.min.time()
                ),

            to_date=
                datetime.combine(
                    upcoming_end,
                    datetime.max.time()
                )
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

        if not result_meeting(
            row
        ):
            continue

        symbol = board_symbol(
            row
        )

        if not symbol:
            continue

        result_date = iso_date(
            first(
                row,
                "bm_date",
                "meetingDate",
                "date"
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
                str(
                    first(
                        row,
                        "sm_name",
                        "companyName",
                        "name",
                        default=symbol
                    )
                ),

            "sector":
                str(
                    first(
                        row,
                        "sm_indusrty",
                        "industry",
                        default="—"
                    )
                ),

            "resultDate":
                result_date,

            "result_date":
                result_date,

            "quarter":
                "Upcoming result",

            "bucket":
                "Upcoming",

            "peadStatus":
                "Upcoming",

            "discoverySource":
                "NSE board meetings",
        }

    # ---------------------------------------------
    # NEWLY DECLARED RESULTS
    # ---------------------------------------------

    results_fn = get_method(
        nse,
        "financial_results"
    )

    result_rows = (
        safe_call(
            "NSE financial results",
            results_fn,

            segment=
                "equities",

            period=
                "quarterly",

            from_date=
                datetime.combine(
                    recent_start,
                    datetime.min.time()
                ),

            to_date=
                datetime.combine(
                    today,
                    datetime.max.time()
                )
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

        symbol = financial_symbol(
            row
        )

        if not symbol:
            continue

        result_date = (
            iso_date(
                first(
                    row,
                    "broadCastDate",
                    "broadcastDate",
                    "filingDate"
                )
            )
            or
            today.isoformat()
        )

        existing = candidates.get(
            symbol,
            {}
        )

        candidates[
            symbol
        ] = {

            **existing,

            "symbol":
                symbol,

            "sym":
                symbol,

            "name":
                str(
                    first(
                        row,
                        "companyName",
                        "company",
                        "name",
                        default=
                            existing.get(
                                "name",
                                symbol
                            )
                    )
                ),

            "sector":
                str(
                    first(
                        row,
                        "industry",
                        default=
                            existing.get(
                                "sector",
                                "—"
                            )
                    )
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
                        "period",
                        default=
                            "Quarterly"
                    )
                ),

            "bucket":
                "Post-results",

            "peadStatus":
                "Post-results",

            "discoverySource":
                "NSE financial results",
        }

    return (
        candidates,
        len(board_rows),
        len(result_rows)
    )


# ---------------------------------------------------------
# Keep recent tracked stocks
# ---------------------------------------------------------

def carry_forward(
    candidates: dict[str, dict],
    old: dict[str, dict]
) -> int:

    today = (
        datetime.now(
            timezone.utc
        ).date()
    )

    cutoff = (
        today
        -
        timedelta(
            days=KEEP_DAYS
        )
    )

    kept = 0

    for symbol, row in old.items():

        if symbol in candidates:
            continue

        result_date = parse_date(
            first(
                row,
                "resultDate",
                "result_date"
            )
        )

        status = str(
            first(
                row,
                "bucket",
                "peadStatus",
                "stage",
                default=""
            )
        ).lower()

        recent_post_result = bool(
            result_date
            and
            cutoff
            <=
            result_date
            <=
            today
        )

        special_status = any(
            word in status
            for word in (
                "qualified",
                "caution",
                "review"
            )
        )

        if not (
            recent_post_result
            or
            special_status
        ):
            continue

        candidates[
            symbol
        ] = {

            **row,

            "symbol":
                symbol,

            "sym":
                symbol,

            "discoverySource":
                row.get(
                    "discoverySource",
                    "carry-forward"
                ),
        }

        kept += 1

    return kept


# ---------------------------------------------------------
# NSE detailed quote
# ---------------------------------------------------------

def unpack_detailed(
    payload: Any
) -> dict:

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
    row: dict
) -> dict:

    symbol = row[
        "symbol"
    ]

    detailed_fn = get_method(
        nse,
        "get_detailed_scrip_data",
        "getDetailedScripData"
    )

    payload = safe_call(
        f"{symbol} detailed quote",
        detailed_fn,
        symbol
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
            "basePrice"
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
        previous_close
        not in (
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
            2
        )

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
                        )
                )
            ),

        "sector":
            str(
                first(
                    sec,
                    "sector",
                    "basicIndustry",
                    "industryInfo",
                    default=
                        output.get(
                            "sector",
                            "—"
                        )
                )
            ),

        "industry":
            str(
                first(
                    sec,
                    "industryInfo",
                    "basicIndustry",
                    default="—"
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
                    "quantitytraded"
                )
            ),

        "deliveryPct":
            fnum(
                first(
                    trade,
                    "deliveryToTradedQuantity"
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
                if
                price
                is not None
                else
                "unavailable"
            ),

        "liveError":
            (
                None
                if
                price
                is not None
                else
                "Price missing"
            ),

        "priceTimestamp":
            str(
                first(
                    data,
                    "lastUpdateTime",
                    default=
                        now_iso()
                )
            ),
    })

    return output


# ---------------------------------------------------------
# Results comparison
# ---------------------------------------------------------

def growth(
    current: float | None,
    old: float | None
) -> float | None:

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
        2
    )


def row_value(
    row: dict | None,
    *keys: str
) -> float | None:

    return (
        fnum(
            first(
                row,
                *keys
            )
        )
        if isinstance(
            row,
            dict
        )
        else
        None
    )


def result_row_date(
    row: dict
) -> date:

    return (
        parse_date(
            first(
                row,
                "re_to_dt",
                "re_create_dt"
            )
        )
        or
        date.min
    )

def enrich_results(nse, row: dict) -> dict:
    output = dict(row)

    if str(output.get("bucket", "")).lower() == "upcoming":
        return output

    try:
        compare_fn = get_method(
            nse,
            "results_comparison",
            "resultsComparison",
        )
    except Exception:
        return output

    payload = safe_call(
        f'{output.get("symbol", "UNKNOWN")} results comparison',
        compare_fn,
        output.get("symbol"),
    ) or {}

    rows = (
        payload.get("resCmpData")
        if isinstance(payload, dict)
        else None
    )

    if not isinstance(rows, list) or not rows:
        return output

    rows = [
        item
        for item in rows
        if isinstance(item, dict)
    ]

    if not rows:
        return output

    def item_date(item: dict):
        return (
            parse_date(
                first(
                    item,
                    "re_to_dt",
                    "re_create_dt",
                    "toDate",
                    "periodEnded",
                )
            )
            or date.min
        )

    rows.sort(
        key=item_date,
        reverse=True
    )

    latest = rows[0]
    previous = (
        rows[1]
        if len(rows) >= 2
        else None
    )

    year_ago = (
        rows[4]
        if len(rows) >= 5
        else None
    )

    def value(item, *keys):
        if not isinstance(item, dict):
            return None

        return fnum(
            first(
                item,
                *keys
            )
        )

    revenue = value(
        latest,
        "re_total_inc",
        "re_net_sale",
        "totalIncome",
        "revenue",
    )

    pat = value(
        latest,
        "re_net_profit",
        "re_proloss_ord_act",
        "netProfit",
        "pat",
    )

    revenue_qoq = (
        growth(
            revenue,
            value(
                previous,
                "re_total_inc",
                "re_net_sale",
                "totalIncome",
                "revenue",
            ),
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
                "re_proloss_ord_act",
                "netProfit",
                "pat",
            ),
        )
        if previous
        else None
    )

    revenue_yoy = (
        growth(
            revenue,
            value(
                year_ago,
                "re_total_inc",
                "re_net_sale",
                "totalIncome",
                "revenue",
            ),
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
                "re_proloss_ord_act",
                "netProfit",
                "pat",
            ),
        )
        if year_ago
        else None
    )

    output.update(
        {
            "latestRevenueLakh": revenue,
            "latestPatLakh": pat,
            "revenueYoY": revenue_yoy,
            "patYoY": pat_yoy,
            "revenueQoQ": revenue_qoq,
            "patQoQ": pat_qoq,

            "revenuePatPass": (
                revenue_yoy > 0
                and pat_yoy > 0
                if (
                    revenue_yoy is not None
                    and pat_yoy is not None
                )
                else None
            ),

            "earningsQualityPass": (
                revenue > 0
                and pat > 0
                if (
                    revenue is not None
                    and pat is not None
                )
                else None
            ),
        }
    )

    return output

