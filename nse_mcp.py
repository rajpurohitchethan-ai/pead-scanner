#!/usr/bin/env python3

import asyncio
import json
import re
import sys
from datetime import date, timedelta
from typing import Any

from mcp import Client


CM_URL = "https://mcp.nseindia.in/cmmkt/mcp"
BHAV_URL = "https://mcp.nseindia.in/bhavcopy/cm/mcp"


def _schema(tool) -> dict:
    return (
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {}
    )


def _tool_text(tool) -> str:
    return " ".join(
        str(x or "")
        for x in (
            getattr(tool, "name", ""),
            getattr(tool, "title", ""),
            getattr(tool, "description", ""),
        )
    ).lower()


async def _list_all_tools(client: Client):
    out = []
    cursor = None
    while True:
        page = await client.list_tools(cursor=cursor)
        out.extend(page.tools)
        cursor = getattr(page, "next_cursor", None)
        if cursor is None:
            break
    return out


def _score_tool(tool, positives: dict[str, int], negatives: dict[str, int]) -> int:
    text = _tool_text(tool)
    score = 0
    for token, weight in positives.items():
        if token in text:
            score += weight
    for token, weight in negatives.items():
        if token in text:
            score -= weight
    return score


def _pick_quote_tool(tools):
    positives = {
        "individual": 10,
        "quote": 10,
        "stock": 4,
        "equity": 3,
        "live": 3,
        "price": 2,
    }
    negatives = {
        "bulk": 12,
        "all equity": 12,
        "listing": 10,
        "list ": 8,
        "gainer": 10,
        "loser": 10,
        "index": 8,
        "bond": 10,
        "sme": 8,
    }
    ranked = sorted(
        tools,
        key=lambda t: _score_tool(t, positives, negatives),
        reverse=True,
    )
    return ranked[0] if ranked and _score_tool(ranked[0], positives, negatives) > 0 else None


def _pick_history_tool(tools):
    positives = {
        "stock price history": 16,
        "price history": 12,
        "history": 9,
        "historical": 9,
        "ohlcv": 12,
        "stock": 3,
        "price": 2,
        "volume": 1,
    }
    negatives = {
        "bulk": 8,
        "gainer": 8,
        "loser": 8,
        "compare": 6,
        "52-week": 5,
        "52 week": 5,
        "breadth": 6,
        "search": 8,
    }
    ranked = sorted(
        tools,
        key=lambda t: _score_tool(t, positives, negatives),
        reverse=True,
    )
    return ranked[0] if ranked and _score_tool(ranked[0], positives, negatives) > 0 else None


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _coerce_for_schema(spec: dict, value: Any):
    typ = spec.get("type")
    if typ == "array":
        return value if isinstance(value, list) else [value]
    if typ == "integer":
        try:
            return int(value)
        except Exception:
            return value
    if typ == "number":
        try:
            return float(value)
        except Exception:
            return value
    if typ == "boolean":
        return bool(value)
    return str(value) if value is not None else value


def _build_args(tool, *, symbol: str, start_date: date | None = None, end_date: date | None = None) -> dict:
    schema = _schema(tool)
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args: dict[str, Any] = {}

    for name, spec in props.items():
        lname = _norm_key(name)
        desc = str(spec.get("description") or "").lower()
        enum = spec.get("enum") or []
        default = spec.get("default", None)

        value = None
        matched = False

        if any(x in lname for x in ("symbol", "ticker", "security", "stockcode", "stocksymbol", "scrip")):
            value = symbol
            matched = True
        elif lname in {"stock", "equity"} and "symbol" in desc:
            value = symbol
            matched = True
        elif ("from" in lname or "start" in lname) and "date" in lname and start_date is not None:
            value = start_date.isoformat()
            matched = True
        elif ("to" in lname or "end" in lname) and "date" in lname and end_date is not None:
            value = end_date.isoformat()
            matched = True
        elif lname in {"datefrom", "fromdate", "startdate"} and start_date is not None:
            value = start_date.isoformat()
            matched = True
        elif lname in {"dateto", "todate", "enddate"} and end_date is not None:
            value = end_date.isoformat()
            matched = True
        elif "series" in lname:
            value = "EQ" if (not enum or "EQ" in enum) else enum[0]
            matched = True
        elif "segment" in lname:
            preferred = ["CM", "EQUITY", "equities", "cash"]
            value = next((v for v in preferred if v in enum), enum[0] if enum else "CM")
            matched = True
        elif ("day" in lname or "lookback" in lname) and start_date is not None and end_date is not None:
            value = max(1, (end_date - start_date).days)
            matched = True
        elif "limit" in lname:
            value = 500
            matched = True

        if matched:
            args[name] = _coerce_for_schema(spec, value)
        elif name in required:
            if default is not None:
                args[name] = default
            elif enum:
                args[name] = enum[0]
            elif spec.get("type") == "boolean":
                args[name] = False
            elif spec.get("type") == "integer":
                args[name] = 500

    return args


def _result_object(result):
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        raw = text.strip()
        for candidate in (
            raw,
            re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S),
        ):
            try:
                return json.loads(candidate)
            except Exception:
                pass

    return None


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_dicts(item)


def _first_value(d: dict, aliases: set[str]):
    for key, value in d.items():
        if _norm_key(key) in aliases and value not in (None, ""):
            return value
    return None


def _number(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


SYMBOL_KEYS = {
    "symbol", "ticker", "tckrsymb", "securitysymbol", "stocksymbol", "scripcode"
}
PRICE_KEYS = {
    "lastprice", "ltp", "lasttradedprice", "price", "close", "closingprice", "clsp"
}
PREV_KEYS = {
    "previousclose", "prevclose", "previousclosingprice", "prevclosingprice"
}
CHANGE_KEYS = {
    "pchange", "changepct", "percentchange", "percentagechange", "changepercent"
}
TIME_KEYS = {
    "lastupdatetime", "timestamp", "time", "tradetime", "updatedat", "asof"
}


def normalize_quote(obj, symbol: str) -> dict | None:
    if obj is None:
        return None

    best = None
    best_score = -1

    for d in _walk_dicts(obj):
        keys = {_norm_key(k) for k in d.keys()}
        score = 0
        if keys & PRICE_KEYS:
            score += 10
        if keys & SYMBOL_KEYS:
            score += 3
        if keys & PREV_KEYS:
            score += 2
        if keys & CHANGE_KEYS:
            score += 2

        sym = _first_value(d, SYMBOL_KEYS)
        if sym and str(sym).upper().replace(".NS", "") == symbol.upper().replace(".NS", ""):
            score += 5

        if score > best_score:
            best = d
            best_score = score

    if not best or best_score < 10:
        return None

    price = _number(_first_value(best, PRICE_KEYS))
    prev = _number(_first_value(best, PREV_KEYS))
    pct = _number(_first_value(best, CHANGE_KEYS))
    ts = _first_value(best, TIME_KEYS)

    if pct is None and price is not None and prev not in (None, 0):
        pct = round((price / prev - 1) * 100, 2)

    return {
        "price": price,
        "previousClose": prev,
        "changePct": pct,
        "priceTimestamp": str(ts) if ts is not None else None,
        "source": "NSE MCP CM Market",
    }


DATE_KEYS = {
    "date", "tradedate", "traddt", "timestamp", "ch_timestamp", "tradingdate"
}
OPEN_KEYS = {
    "open", "openprice", "openingprice", "opnpric", "ch_opening_price"
}
HIGH_KEYS = {
    "high", "highprice", "tradehighprice", "hghpric", "ch_trade_high_price"
}
LOW_KEYS = {
    "low", "lowprice", "tradelowprice", "lwpric", "ch_trade_low_price"
}
CLOSE_KEYS = {
    "close", "closeprice", "closingprice", "clspric", "ch_closing_price", "lastprice"
}
VOLUME_KEYS = {
    "volume", "tradedvolume", "totaltradedvolume", "ttltradgvol", "tottrdqty", "ch_tot_traded_qty"
}


def _candidate_record_lists(obj):
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[: min(5, len(obj))]):
        yield obj
    if isinstance(obj, dict):
        vals = list(obj.values())
        if vals and all(isinstance(x, dict) for x in vals[: min(5, len(vals))]):
            yield vals
        for value in vals:
            yield from _candidate_record_lists(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _candidate_record_lists(item)


def normalize_history(obj, symbol: str) -> list[dict]:
    best = None
    best_score = -1

    for records in _candidate_record_lists(obj):
        if not records:
            continue
        sample = records[: min(10, len(records))]
        score = 0
        for d in sample:
            keys = {_norm_key(k) for k in d.keys()}
            score += 5 if keys & DATE_KEYS else 0
            score += 4 if keys & CLOSE_KEYS else 0
            score += 2 if keys & OPEN_KEYS else 0
            score += 2 if keys & HIGH_KEYS else 0
            score += 2 if keys & LOW_KEYS else 0
            score += 2 if keys & VOLUME_KEYS else 0
        score += min(len(records), 50) / 10
        if score > best_score:
            best = records
            best_score = score

    if not best:
        return []

    out = []
    for d in best:
        sym = _first_value(d, SYMBOL_KEYS)
        if sym:
            normalized_sym = str(sym).upper().replace(".NS", "")
            if normalized_sym != symbol.upper().replace(".NS", ""):
                continue

        dt = _first_value(d, DATE_KEYS)
        close = _number(_first_value(d, CLOSE_KEYS))
        if dt in (None, "") or close is None:
            continue

        out.append(
            {
                "Date": str(dt),
                "Open": _number(_first_value(d, OPEN_KEYS)),
                "High": _number(_first_value(d, HIGH_KEYS)),
                "Low": _number(_first_value(d, LOW_KEYS)),
                "Close": close,
                "Volume": _number(_first_value(d, VOLUME_KEYS)),
            }
        )

    return out


async def probe_servers() -> dict:
    result = {}
    for label, url in (("cm", CM_URL), ("bhavcopy", BHAV_URL)):
        try:
            async with Client(url) as client:
                tools = await _list_all_tools(client)
                result[label] = [
                    {
                        "name": getattr(t, "name", ""),
                        "title": getattr(t, "title", None),
                        "description": getattr(t, "description", None),
                        "input_schema": _schema(t),
                    }
                    for t in tools
                ]
        except Exception as exc:
            result[label] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


async def fetch_nse_market_layer(rows: list[dict], lookback_days: int = 430, concurrency: int = 4) -> dict:
    symbols = sorted(
        {
            str(row.get("symbol") or row.get("sym") or "").upper().replace(".NS", "")
            for row in rows
            if row.get("symbol") or row.get("sym")
        }
    )

    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    quotes: dict[str, dict] = {}
    histories: dict[str, list[dict]] = {}
    errors: list[str] = []
    meta: dict[str, Any] = {
        "cmUrl": CM_URL,
        "bhavcopyUrl": BHAV_URL,
        "quoteTool": None,
        "historyTool": None,
        "symbolsRequested": len(symbols),
    }

    try:
        async with Client(CM_URL) as cm_client, Client(BHAV_URL) as bhav_client:
            cm_tools = await _list_all_tools(cm_client)
            bhav_tools = await _list_all_tools(bhav_client)

            quote_tool = _pick_quote_tool(cm_tools)
            history_tool = _pick_history_tool(bhav_tools)

            meta["cmTools"] = [getattr(t, "name", "") for t in cm_tools]
            meta["bhavcopyTools"] = [getattr(t, "name", "") for t in bhav_tools]
            meta["quoteTool"] = getattr(quote_tool, "name", None) if quote_tool else None
            meta["historyTool"] = getattr(history_tool, "name", None) if history_tool else None

            if quote_tool is None:
                errors.append("Could not identify an individual-stock quote tool on CM Market MCP.")
            if history_tool is None:
                errors.append("Could not identify a stock-price-history tool on Bhavcopy MCP.")

            sem = asyncio.Semaphore(max(1, concurrency))

            async def one_symbol(sym: str):
                async with sem:
                    if quote_tool is not None:
                        try:
                            args = _build_args(quote_tool, symbol=sym)
                            result = await cm_client.call_tool(quote_tool.name, args)
                            if getattr(result, "is_error", False):
                                errors.append(f"{sym} quote tool returned is_error")
                            else:
                                q = normalize_quote(_result_object(result), sym)
                                if q:
                                    quotes[sym] = q
                                else:
                                    errors.append(f"{sym} quote response could not be normalized")
                        except Exception as exc:
                            errors.append(f"{sym} quote: {type(exc).__name__}: {exc}")

                    if history_tool is not None:
                        try:
                            args = _build_args(
                                history_tool,
                                symbol=sym,
                                start_date=start_date,
                                end_date=end_date,
                            )
                            result = await bhav_client.call_tool(history_tool.name, args)
                            if getattr(result, "is_error", False):
                                errors.append(f"{sym} history tool returned is_error")
                            else:
                                h = normalize_history(_result_object(result), sym)
                                if h:
                                    histories[sym] = h
                                else:
                                    errors.append(f"{sym} history response could not be normalized")
                        except Exception as exc:
                            errors.append(f"{sym} history: {type(exc).__name__}: {exc}")

            await asyncio.gather(*(one_symbol(sym) for sym in symbols))

    except Exception as exc:
        errors.append(f"MCP connection: {type(exc).__name__}: {exc}")

    meta["quotesReceived"] = len(quotes)
    meta["historiesReceived"] = len(histories)
    meta["errorCount"] = len(errors)

    return {
        "quotes": quotes,
        "histories": histories,
        "errors": errors[:100],
        "meta": meta,
    }


def main():
    if "--probe" not in sys.argv:
        print("Use: python nse_mcp.py --probe")
        return

    result = asyncio.run(probe_servers())
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
