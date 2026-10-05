#!/usr/bin/env python3

import json
import math
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data.json"
CACHE = BASE / ".nse-cache"

MIN_MCAP_CR = 1000.0
UPCOMING_DAYS = 30
RECENT_DAYS = 14
KEEP_POST_DAYS = 30


def now_iso():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def first(obj, *keys, default=None):
    if not isinstance(obj, dict):
        return default

    for key in keys:
        value = obj.get(key)

        if value not in (None, ""):
            return value

    return default


def number(value):
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

    except (TypeError, ValueError):
        return None


def symbol(value):
    s = str(
        value or ""
    ).strip().upper()

    if s.endswith(".NS"):
        return s[:-3]

    return s


def parse_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    formats = (
        "%d-%b-%Y",
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%b-%Y %H:%M",
    )

    for fmt in formats:
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

    except ValueError:
        return None


def iso_date(value):
    d = parse_date(value)

    return (
        d.isoformat()
        if d
        else None
    )


def method(obj, *names):
    for name in names:
        fn = getattr(
            obj,
            name,
            None
        )

        if callable(fn):
            return fn

    raise AttributeError(
        "Missing NSE method: "
        +
        " / ".join(names)
    )


def safe(
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
            f"[WARN] {label}: "
            f"{type(exc).__name__}: {exc}"
        )

        return None


# ---------------------------------------------------------
# Previous LIVE scan only
# ---------------------------------------------------------

def previous_live():
    if not DATA.exists():
        return {}

    try:
        payload = json.loads(
            DATA.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {}

    # IMPORTANT:
    # Do not carry forward the old manually seeded 21-stock file.
    if payload.get(
        "scannerMode"
    ) != "live-discovery":
        return {}

    out = {}

    rows = (
        payload.get("stocks")
        or
        payload.get("companies")
        or
        []
    )

    for row in rows:
        if not isinstance(
            row,
            dict
        ):
            continue

        s = symbol(
            first(
                row,
                "symbol",
                "sym",
                "ticker"
            )
        )

        if s:
            out[s] = row

    return out


# ---------------------------------------------------------
# LIVE DISCOVERY
# ---------------------------------------------------------

def is_result_meeting(row):
    text = " ".join(
        str(
            first(
                row,
                key,
                default=""
            )
        )
        for key in (
            "bm_purpose",
            "bmPurpose",
            "purpose",
            "bm_desc",
            "description"
        )
    ).lower()

    return "result" in text


def discover(nse):
    today = (
        datetime.now(
            timezone.utc
        ).date()
    )

    recent = (
        today
        -
        timedelta(
            days=RECENT_DAYS
        )
    )

    future = (
        today
        +
        timedelta(
            days=UPCOMING_DAYS
        )
    )

    found = {}

    # -----------------------------------------------------
    # Upcoming board meetings / result dates
    # -----------------------------------------------------

    board_fn = method(
        nse,
        "board_meetings",
        "boardMeetings"
    )

    board_rows = (
        safe(
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
                    future,
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

        s = symbol(
            first(
                row,
                "bm_symbol",
                "symbol",
                "SYMBOL",
                "sm_symbol"
            )
        )

        if not s:
            continue

        d = iso_date(
            first(
                row,
                "bm_date",
                "meetingDate",
                "date"
            )
        )

        found[s] = {

            "symbol":
                s,

            "sym":
                s,

            "name":
                str(
                    first(
                        row,
                        "sm_name",
                        "companyName",
                        "name",
                        default=s
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
                d,

            "result_date":
                d,

            "quarter":
                "Upcoming result",

            "bucket":
                "Upcoming",

            "peadStatus":
                "Upcoming",

            "discoverySource":
                "NSE board meetings",
        }

    # -----------------------------------------------------
    # Newly declared quarterly results
    # -----------------------------------------------------

    results_fn = method(
        nse,
        "financial_results"
    )

    result_rows = (
        safe(
            "NSE financial results",
            results_fn,

            segment="equities",

            period="quarterly",

            from_date=
                datetime.combine(
                    recent,
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

        s = symbol(
            first(
                row,
                "symbol",
                "SYMBOL",
                "sm_symbol"
            )
        )

        if not s:
            continue

        d = (
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

        old = found.get(
            s,
            {}
        )

        # A filed result overrides an upcoming record.
        found[s] = {

            **old,

            "symbol":
                s,

            "sym":
                s,

            "name":
                str(
                    first(
                        row,
                        "companyName",
                        "company",
                        default=
                            old.get(
                                "name",
                                s
                            )
                    )
                ),

            "sector":
                str(
                    first(
                        row,
                        "industry",
                        default=
                            old.get(
                                "sector",
                                "—"
                            )
                    )
                ),

            "resultDate":
                d,

            "result_date":
                d,

            "quarter":
                str(
                    first(
                        row,
                        "relatingTo",
                        "toDate",
                        default="Quarterly"
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
        found,
        len(board_rows),
        len(result_rows)
    )


# ---------------------------------------------------------
# Carry forward recent post-result LIVE candidates
# ---------------------------------------------------------

def carry_forward(
    found,
    old_rows
):

    today = (
        datetime.now(
            timezone.utc
        ).date()
    )

    cutoff = (
        today
        -
        timedelta(
            days=KEEP_POST_DAYS
        )
    )

    count = 0

    for s, row in old_rows.items():

        if s in found:
            continue

        d = parse_date(
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

        recent_post = (
            d is not None
            and
            cutoff <= d <= today
            and
            "upcoming"
            not in status
        )

        special = any(
            x in status
            for x in (
                "qualified",
                "caution",
                "review"
            )
        )

        if (
            recent_post
            or
            special
        ):

            found[s] = {

                **row,

                "symbol":
                    s,

                "sym":
                    s,

                "discoverySource":
                    row.get(
                        "discoverySource",
                        "live carry-forward"
                    ),
            }

            count += 1

    return count


# ---------------------------------------------------------
# LIVE QUOTE + MARKET CAP
# ---------------------------------------------------------

def detailed_payload(payload):

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


def quote(
    nse,
    row
):

    s = row[
        "symbol"
    ]

    fn = method(
        nse,
        "get_detailed_scrip_data",
        "getDetailedScripData"
    )

    payload = safe(
        f"{s} quote",
        fn,
        s
    )

    out = dict(
        row
    )

    out.update({

        "liveStatus":
            "unavailable",

        "liveError":
            "NSE quote unavailable",

        "priceTimestamp":
            now_iso(),
    })

    if not payload:
        return out

    data = detailed_payload(
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
        number(
            first(
                order,
                "lastPrice"
            )
        )
        or
        number(
            first(
                trade,
                "lastPrice"
            )
        )
        or
        number(
            first(
                meta,
                "closePrice"
            )
        )
    )

    prev = number(
        first(
            meta,
            "previousClose",
            "basePrice"
        )
    )

    pct = number(
        first(
            meta,
            "pChange"
        )
    )

    if (
        pct is None
        and
        price is not None
        and
        prev not in (
            None,
            0
        )
    ):
        pct = round(
            (
                price
                -
                prev
            )
            /
            prev
            *
            100,
            2
        )

    raw_mcap = number(
        first(
            trade,
            "totalMarketCap"
        )
    )

    mcap = (
        round(
            raw_mcap
            /
            10_000_000,
            2
        )
        if
        raw_mcap is not None
        else
        None
    )

    out.update({

        "name":
            str(
                first(
                    meta,
                    "companyName",
                    default=
                        out.get(
                            "name",
                            s
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
                        out.get(
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
            prev,

        "changePct":
            pct,

        "marketCapCr":
            mcap,

        "marketCapPass":
            (
                mcap
                >=
                MIN_MCAP_CR
                if
                mcap is not None
                else
                None
            ),

        "volume":
            number(
                first(
                    trade,
                    "totalTradedVolume",
                    "quantitytraded"
                )
            ),

        "deliveryPct":
            number(
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
                price is not None
                else
                "unavailable"
            ),

        "liveError":
            (
                None
                if
                price is not None
                else
                "Price missing in NSE response"
            ),

        # This fixes the earlier 12:00 AM problem.
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

    return out


# ---------------------------------------------------------
# FINANCIAL RESULTS
# ---------------------------------------------------------

def growth(
    current,
    old
):

    a = number(
        current
    )

    b = number(
        old
    )

    if (
        a is None
        or
        b in (
            None,
            0
        )
    ):
        return None

    return round(
        (
            a
            -
            b
        )
        /
        abs(b)
        *
        100,
        2
    )


def enrich_results(
    nse,
    row
):

    out = dict(
        row
    )

    if (
        str(
            out.get(
                "bucket",
                ""
            )
        ).lower()
        ==
        "upcoming"
    ):
        return out

    fn = method(
        nse,
        "results_comparison",
        "resultsComparison"
    )

    payload = (
        safe(
            (
                f"{out['symbol']} "
                "results comparison"
            ),
            fn,
            out[
                "symbol"
            ]
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

    if not isinstance(
        rows,
        list
    ):
        return out

    rows = [
        x
        for x in rows
        if isinstance(
            x,
            dict
        )
    ]

    rows.sort(
        key=
            lambda x:
            parse_date(
                first(
                    x,
                    "re_to_dt",
                    "re_create_dt"
                )
            )
            or
            date.min,

        reverse=True,
    )

    if not rows:
        return out

    latest = rows[0]

    previous = (
        rows[1]
        if
        len(rows) > 1
        else
        None
    )

    year_ago = (
        rows[4]
        if
        len(rows) > 4
        else
        None
    )

    def metric(
        item,
        *keys
    ):
        if not isinstance(
            item,
            dict
        ):
            return None

        return number(
            first(
                item,
                *keys
            )
        )

    revenue = metric(
        latest,
        "re_total_inc",
        "re_net_sale"
    )

    pat = metric(
        latest,
        "re_net_profit",
        "re_proloss_ord_act"
    )

    revenue_qoq = growth(
        revenue,
        metric(
            previous,
            "re_total_inc",
            "re_net_sale"
        )
    )

    pat_qoq = growth(
        pat,
        metric(
            previous,
            "re_net_profit",
            "re_proloss_ord_act"
        )
    )

    revenue_yoy = growth(
        revenue,
        metric(
            year_ago,
            "re_total_inc",
            "re_net_sale"
        )
    )

    pat_yoy = growth(
        pat,
        metric(
            year_ago,
            "re_net_profit",
            "re_proloss_ord_act"
        )
    )

    out.update({

        "latestRevenueLakh":
            revenue,

        "latestPatLakh":
            pat,

        "revenueQoQ":
            revenue_qoq,

        "patQoQ":
            pat_qoq,

        "revenueYoY":
            revenue_yoy,

        "patYoY":
            pat_yoy,

        "revenuePatPass":
            (
                revenue_yoy > 0
                and
                pat_yoy > 0

                if
                revenue_yoy is not None
                and
                pat_yoy is not None

                else
                None
            ),

        "earningsQualityPass":
            (
                revenue > 0
                and
                pat > 0

                if
                revenue is not None
                and
                pat is not None

                else
                None
            ),
    })

    return out


# ---------------------------------------------------------
# PEAD GATES
# ---------------------------------------------------------

def finalize(row):

    out = dict(
        row
    )

    out.setdefault(
        "cashFlowPass",
        None
    )

    out.setdefault(
        "priceVolumePass",
        None
    )

    out.setdefault(
        "sectorTailwind",
        None
    )

    out.setdefault(
        "entryTriggerPass",
        None
    )

    out.setdefault(
        "entry",
        None
    )

    out.setdefault(
        "sl",
        None
    )

    out.setdefault(
        "tsl",
        None
    )

    checks = [

        (
            "Market cap > ₹1,000 Cr",
            out.get(
                "marketCapPass"
            )
        ),

        (
            "Revenue / PAT acceleration",
            out.get(
                "revenuePatPass"
            )
        ),

        (
            "Earnings quality",
            out.get(
                "earningsQualityPass"
            )
        ),

        (
            "Cash flow / surprise",
            out.get(
                "cashFlowPass"
            )
        ),

        (
            "Price / volume confirmation",
            out.get(
                "priceVolumePass"
            )
        ),

        (
            "Sector tailwind",
            out.get(
                "sectorTailwind"
            )
        ),

        (
            "Entry trigger defined",
            out.get(
                "entryTriggerPass"
            )
        ),

        (
            "Stop loss defined",
            (
                True
                if
                out.get(
                    "sl"
                )
                is not None
                else
                None
            )
        ),
    ]

    out[
        "checks"
    ] = [

        {
            "label":
                label,

            "value":
                value
        }

        for label, value
        in checks
    ]

    out[
        "score"
    ] = sum(
        value is True
        for _, value
        in checks
    )

    out[
        "scoreText"
    ] = (
        f'{out["score"]}/8'
    )

    out[
        "knownChecks"
    ] = sum(
        value is not None
        for _, value
        in checks
    )

    if all(
        value is True
        for _, value
        in checks
    ):
        out[
            "bucket"
        ] = "Qualified"

    out[
        "peadStatus"
    ] = out.get(
        "bucket",
        "In Review"
    )

    out[
        "stage"
    ] = out[
        "peadStatus"
    ]

    out[
        "scanTime"
    ] = now_iso()

    return out


# ---------------------------------------------------------
# NSE CLIENT
# ---------------------------------------------------------

def nse_client():

    from nse import NSE

    CACHE.mkdir(
        parents=True,
        exist_ok=True
    )

    # Current NSE library
    try:
        return NSE(
            download_folder=
                str(CACHE),

            timeout=20
        )

    except TypeError:
        pass

    # Compatibility with older NSE versions
    try:
        return NSE(
            download_folder=
                str(CACHE),

            server=True
        )

    except TypeError:
        return NSE(
            download_folder=
                str(CACHE)
        )


# ---------------------------------------------------------
# JSON WRITER
# ---------------------------------------------------------

def write_json(payload):

    temp = DATA.with_suffix(
        ".json.tmp"
    )

    temp.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False
        )
        +
        "\n",

        encoding="utf-8"
    )

    os.replace(
        temp,
        DATA
    )


# ---------------------------------------------------------
# MAIN SCAN
# ---------------------------------------------------------

def main():

    old_rows = previous_live()

    errors = []
    published = []

    rejected = 0
    unknown_mcap = 0

    with nse_client() as nse:

        (
            found,
            board_count,
            result_count
        ) = discover(
            nse
        )

        carried = carry_forward(
            found,
            old_rows
        )

        print(
            "======================================"
        )

        print(
            "PEAD LIVE DISCOVERY SCANNER"
        )

        print(
            "======================================"
        )

        print(
            f"Board meeting rows: {board_count}"
        )

        print(
            f"Financial result rows: {result_count}"
        )

        print(
            f"Unique live candidates: {len(found)}"
        )

        print(
            f"Carried from prior LIVE scans: {carried}"
        )

        if not found:

            raise RuntimeError(
                "NSE live discovery returned "
                "0 candidates. Existing "
                "data.json was NOT overwritten."
            )

        symbols = sorted(
            found
        )

        for i, s in enumerate(
            symbols,
            1
        ):

            print(
                f"[{i}/{len(symbols)}] {s}"
            )

            try:

                row = quote(
                    nse,
                    found[s]
                )

                mcap = number(
                    row.get(
                        "marketCapCr"
                    )
                )

                if (
                    mcap is not None
                    and
                    mcap
                    <
                    MIN_MCAP_CR
                ):

                    rejected += 1

                    print(
                        "  excluded: "
                        f"₹{mcap:,.0f} Cr"
                    )

                    continue

                if mcap is None:
                    unknown_mcap += 1

                row = enrich_results(
                    nse,
                    row
                )

                published.append(
                    finalize(
                        row
                    )
                )

            except Exception as exc:

                msg = (
                    f"{s}: "
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                errors.append(
                    msg
                )

                print(
                    f"  [WARN] {msg}"
                )

                if s in old_rows:

                    fallback = dict(
                        old_rows[s]
                    )

                    fallback[
                        "liveStatus"
                    ] = "unavailable"

                    fallback[
                        "liveError"
                    ] = msg

                    published.append(
                        finalize(
                            fallback
                        )
                    )

    if not published:

        raise RuntimeError(
            "Candidates were found but "
            "0 stocks could be published. "
            "Existing data.json was NOT overwritten."
        )

    stamp = now_iso()

    payload = {

        "generatedAt":
            stamp,

        "last_scan":
            stamp,

        "lastScanAt":
            stamp,

        "scannerMode":
            "live-discovery",

        "source":
            (
                "NSE board meetings + "
                "NSE financial results + "
                "NSE detailed quotes"
            ),

        "minMarketCapCr":
            MIN_MCAP_CR,

        "upcomingWindowDays":
            UPCOMING_DAYS,

        "recentResultWindowDays":
            RECENT_DAYS,

        "keepPostResultDays":
            KEEP_POST_DAYS,

        "boardMeetingRows":
            board_count,

        "financialResultRows":
            result_count,

        "discoveredCandidateCount":
            len(found),

        "carriedForwardCount":
            carried,

        "scanCount":
            len(published),

        "stockCount":
            len(published),

        "marketCapRejectedCount":
            rejected,

        "marketCapUnavailableCount":
            unknown_mcap,

        "errorCount":
            len(errors),

        "errors":
            errors[:50],

        # Frontend compatibility
        "companies":
            published,

        "stocks":
            published,
    }

    write_json(
        payload
    )

    print()

    print(
        "======================================"
    )

    print(
        "LIVE SCAN COMPLETE"
    )

    print(
        "======================================"
    )

    print(
        f"Published: {len(published)}"
    )

    print(
        f"Below ₹1,000 Cr: {rejected}"
    )

    print(
        f"Unknown market cap: {unknown_mcap}"
    )

    print(
        f"Errors: {len(errors)}"
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            f"SCANNER FAILED: {exc}"
        )

        traceback.print_exc()

        sys.exit(1)
