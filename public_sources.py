#!/usr/bin/env python3
"""Best-effort public-data adapters used by the PEAD scanner.

Design rules:
- NSE/BSE remain the authoritative source for result-release verification.
- These adapters only FILL MISSING fields; callers decide precedence.
- No login/session bypass, no CAPTCHA workarounds, no private endpoints.
- Low request rate, short timeouts, in-process cache, normal browser User-Agent.
- Failure of any third-party source must never stop the PEAD pipeline.

Public sources currently supported:
- StockScans public company pages: quarterly numbers + valuation/profitability.
- ScreeningMantis public table: opportunistic technical/earnings-reaction fallback.
- Concall.in public documents page when a company URL/ID is already known.
"""

from __future__ import annotations

import calendar
import math
import re
import time
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PEAD-Radar/1.0"
)
_CACHE: dict[str, str | None] = {}
_LAST_REQUEST_AT: dict[str, float] = {}


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        text = str(value)
        text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "")
        text = text.replace(",", "").replace("%", "").replace("x", "")
        text = text.strip()
        if text in {"", "--", "—", "nan", "NaN", "undefined", "?"}:
            return None
        x = float(text)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = _norm(value)
    for fmt in (
        "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y",
        "%b %Y", "%b-%Y", "%b %y", "%b-%y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt in {"%b %Y", "%b-%Y", "%b %y", "%b-%y"}:
                last_day = calendar.monthrange(dt.year, dt.month)[1]
                return date(dt.year, dt.month, last_day)
            return dt.date()
        except ValueError:
            pass
    return None


def _clean_symbol(row: dict) -> str:
    value = row.get("symbol") or row.get("sym") or row.get("ticker") or ""
    s = str(value).upper().replace("NSE:", "").replace("BSE:", "")
    s = s.replace(".NS", "").replace(".BO", "").strip()
    return s


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1).lower() if m else ""


def _public_get(url: str, timeout: int = 10, min_interval: float = 0.20) -> str | None:
    """Small, polite cached GET. Returns None on any block/error."""
    if url in _CACHE:
        return _CACHE[url]

    host = _host(url)
    last = _LAST_REQUEST_AT.get(host, 0.0)
    wait = min_interval - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)

    try:
        req = Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(3_000_000)
            charset = resp.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
        _LAST_REQUEST_AT[host] = time.monotonic()
        _CACHE[url] = text
        return text
    except Exception:
        _LAST_REQUEST_AT[host] = time.monotonic()
        _CACHE[url] = None
        return None


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self.links: list[tuple[str, str]] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
        elif tag == "a":
            self._link_href = attrs_d.get("href")
            self._link_parts = []

    def handle_data(self, data):
        t = _norm(data)
        if not t:
            return
        self.text_parts.append(t)
        if self._cell_parts is not None:
            self._cell_parts.append(t)
        if self._link_href is not None:
            self._link_parts.append(t)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            self._row.append(_norm(" ".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None
        elif tag == "a" and self._link_href is not None:
            self.links.append((self._link_href, _norm(" ".join(self._link_parts))))
            self._link_href = None
            self._link_parts = []

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)


def _parse_page(html: str) -> _PageParser:
    p = _PageParser()
    try:
        p.feed(html)
    except Exception:
        pass
    return p


def _row_last_num(table: list[list[str]], needles: tuple[str, ...]) -> float | None:
    for row in table:
        label = _norm(row[0] if row else "").lower()
        joined = _norm(" ".join(row)).lower()
        if any(n.lower() in label or n.lower() in joined for n in needles):
            for cell in reversed(row[1:]):
                val = _num(cell)
                if val is not None:
                    return val
    return None


def _find_quarter_table(tables: list[list[list[str]]]) -> list[list[str]] | None:
    for table in tables:
        joined = " ".join(" ".join(r) for r in table).lower()
        if "quarter" in joined and "revenue" in joined and "pat" in joined and "growth yoy" in joined:
            return table
    return None


def _find_ratio_table(tables: list[list[list[str]]]) -> list[list[str]] | None:
    for table in tables:
        joined = " ".join(" ".join(r) for r in table).lower()
        if "price to earnings" in joined and "roe" in joined and "market cap" in joined:
            return table
    return None


def _period_from_quarter_label(label: str | None) -> date | None:
    if not label:
        return None
    return _parse_date(label)


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1.0) * 100.0


def stockscans_for_row(row: dict, timeout: int = 10) -> dict:
    """Fetch public StockScans fundamentals for one company.

    This is a SECONDARY source. It does not prove an official result release.
    """
    symbol = _clean_symbol(row)
    bse_code = _norm(
        row.get("bseCode") or row.get("bse_code") or row.get("scripCode") or row.get("scrip_code")
    )
    candidates: list[str] = []
    if symbol:
        candidates.append(f"https://www.stockscans.in/company/{quote('NSE:' + symbol, safe='')}")
    if bse_code.isdigit():
        candidates.append(f"https://www.stockscans.in/company/{quote('BSE:' + bse_code, safe='')}")

    for url in candidates:
        html = _public_get(url, timeout=timeout)
        if not html:
            continue
        parser = _parse_page(html)
        page_text = parser.text
        # Reject generic/404-ish pages that do not mention the company symbol.
        if symbol and symbol not in page_text.upper():
            continue

        out: dict[str, Any] = {
            "source": "StockScans public company page",
            "sourceUrl": url,
            "symbol": symbol,
        }

        qtable = _find_quarter_table(parser.tables)
        if qtable:
            header = next((r for r in qtable if r and _norm(r[0]).lower().startswith("quarter")), None)
            labels = header[1:] if header and len(header) > 1 else []
            latest_label = labels[-1] if labels else None
            latest_period = _period_from_quarter_label(latest_label)
            out["latestIncomeQuarterEnd"] = latest_period.isoformat() if latest_period else None
            out["quarterLabel"] = latest_label

            def last_row_value(*needles: str) -> float | None:
                return _row_last_num(qtable, tuple(needles))

            out["latestRevenueCr"] = last_row_value("Revenue Revenue Cr")
            out["revenueYoY"] = last_row_value("Revenue Growth YoY", "Growth YoY Revenue")
            out["latestPatCr"] = last_row_value("PAT PAT Cr")
            out["patYoY"] = last_row_value("PAT Growth YoY", "Growth YoY PAT")
            out["reportedEps"] = last_row_value("EPS EPS")
            out["operatingMarginNow"] = last_row_value("OPM OPM%")
            out["netMarginPct"] = last_row_value("NPM NPM%")

            # QoQ from last two quarterly absolute values when available.
            for row_cells in qtable:
                label = _norm(row_cells[0] if row_cells else "").lower()
                nums = [_num(x) for x in row_cells[1:]]
                nums = [x for x in nums if x is not None]
                if len(nums) >= 2 and label.startswith("revenue") and "growth" not in label:
                    out["revenueQoQ"] = _pct_change(nums[-1], nums[-2])
                if len(nums) >= 2 and label.startswith("pat") and "growth" not in label:
                    out["patQoQ"] = _pct_change(nums[-1], nums[-2])

            if out.get("latestRevenueCr") is not None:
                out["latestRevenueLakh"] = out["latestRevenueCr"] * 100.0
            if out.get("latestPatCr") is not None:
                out["latestPatLakh"] = out["latestPatCr"] * 100.0

        rtable = _find_ratio_table(parser.tables)
        if rtable:
            out["marketCapCr"] = _row_last_num(rtable, ("Market Cap Market Capitalization",))
            out["trailingPE"] = _row_last_num(rtable, ("Price To Earnings",))
            out["priceToBook"] = _row_last_num(rtable, ("Price To Book",))
            out["enterpriseToEbitda"] = _row_last_num(rtable, ("EV To EBITDA",))
            out["returnOnEquityPct"] = _row_last_num(rtable, ("ROE ROE%",))
            out["returnOnCapitalEmployedPct"] = _row_last_num(rtable, ("ROCE ROCE%",))

        # Quick-ratio fallback if table structure changes.
        text_flat = _norm(page_text)
        regexes = {
            "marketCapCr": r"Market Capitalization\s*₹?\s*([0-9,.]+)\s*Cr",
            "trailingPE": r"Price To Earnings\s*([0-9,.]+)",
            "priceToSales": r"Price To Sales\s*([0-9,.]+)",
            "revenueTtmCr": r"Revenue\s*₹?\s*([0-9,.]+)\s*Cr",
            "revenueGrowthTtmPct": r"Revenue Growth TTM\s*([+-]?[0-9,.]+)%",
            "patGrowthTtmPct": r"PAT Growth TTM\s*([+-]?[0-9,.]+)%",
        }
        for key, pat in regexes.items():
            if out.get(key) is not None:
                continue
            m = re.search(pat, text_flat, flags=re.I)
            if m:
                out[key] = _num(m.group(1))

        # Capture best document links without downloading PDFs.
        expected = _parse_date(
            row.get("resultPeriodEnd") or row.get("resultDate") or row.get("result_date")
        )
        link_rows: list[tuple[str, str]] = []
        for href, anchor in parser.links:
            if not href:
                continue
            absolute = urljoin(url, href)
            if "/document/" in absolute or "transcript-notes" in absolute:
                link_rows.append((absolute, anchor))

        def choose_link(*tokens: str) -> str | None:
            token_l = [t.lower() for t in tokens]
            for href, anchor in link_rows:
                a = anchor.lower()
                if all(t in a for t in token_l):
                    return href
            return None

        out["quarterlyResultUrl"] = choose_link("quarterly", "result")
        out["investorPresentationUrl"] = choose_link("investor", "presentation")
        out["earningsCallTranscriptUrl"] = choose_link("earnings", "call", "transcript")
        out["transcriptSummaryUrl"] = next(
            (href for href, _ in link_rows if "transcript-notes" in href),
            None,
        )

        # Quarter consistency check. If a tracked period is known, stale public
        # quarterly values must not be used as the current result.
        expected_period = None
        if row.get("resultPeriodEnd"):
            expected_period = _parse_date(row.get("resultPeriodEnd"))
        if expected_period is None and expected is not None:
            # Result date in Jul/Aug -> Jun quarter; Oct/Nov -> Sep, etc.
            month = ((expected.month - 1) // 3) * 3
            if month == 0:
                month = 12
                year = expected.year - 1
            else:
                year = expected.year
            expected_period = date(year, month, calendar.monthrange(year, month)[1])

        latest_period = _parse_date(out.get("latestIncomeQuarterEnd"))
        if expected_period and latest_period:
            out["periodMatchesExpected"] = abs((latest_period - expected_period).days) <= 45
        else:
            out["periodMatchesExpected"] = None
        return out

    return {
        "source": "StockScans public company page",
        "sourceUrl": candidates[0] if candidates else None,
        "symbol": symbol,
        "error": "public page unavailable or company not matched",
    }


def _header_index(headers: list[str], *needles: str) -> int | None:
    for i, h in enumerate(headers):
        low = _norm(h).lower()
        if all(n.lower() in low for n in needles):
            return i
    return None


def _parse_screeningmantis_public(timeout: int = 10) -> dict[str, dict]:
    url = "https://www.screeningmantis.com/"
    html = _public_get(url, timeout=timeout)
    if not html:
        return {}
    parser = _parse_page(html)
    best: dict[str, dict] = {}

    for table in parser.tables:
        header_i = None
        for i, row in enumerate(table):
            joined = " ".join(row).lower()
            if "ticker" in joined and "price" in joined and ("e-day" in joined or "past earn" in joined):
                header_i = i
                break
        if header_i is None:
            continue
        headers = table[header_i]
        idx_ticker = _header_index(headers, "ticker")
        idx_price = _header_index(headers, "price")
        idx_day = _header_index(headers, "1d%")
        idx_rsi = _header_index(headers, "rsi")
        idx_vol = _header_index(headers, "vol", "spike")
        idx_ema20 = _header_index(headers, "20ema")
        idx_52 = _header_index(headers, "52w", "high")
        idx_past = _header_index(headers, "past", "earn")
        idx_pre = _header_index(headers, "-1d%")
        idx_eday = _header_index(headers, "e-day%")
        idx_plus = _header_index(headers, "+1d%")

        if idx_ticker is None:
            continue
        for cells in table[header_i + 1:]:
            if idx_ticker >= len(cells):
                continue
            ticker = re.sub(r"[^A-Z0-9&.-]", "", cells[idx_ticker].upper())
            if not ticker or ticker in {"TICKER", "--"}:
                continue

            def cell(idx):
                return cells[idx] if idx is not None and idx < len(cells) else None

            best[ticker] = {
                "source": "ScreeningMantis public table",
                "sourceUrl": url,
                "price": _num(cell(idx_price)),
                "oneDayPct": _num(cell(idx_day)),
                "rsi": _num(cell(idx_rsi)),
                "volumeSpike": _num(cell(idx_vol)),
                "distanceFrom20EmaPct": _num(cell(idx_ema20)),
                "distanceFrom52wHighPct": _num(cell(idx_52)),
                "pastEarnings": _norm(cell(idx_past)),
                "pre1dPct": _num(cell(idx_pre)),
                "earningsDayPct": _num(cell(idx_eday)),
                "post1dPct": _num(cell(idx_plus)),
                "coverageNote": "Public table may expose only the currently rendered subset of stocks.",
            }
    return best


def _partial_day_month_date(value: str | None, reference: date | None) -> date | None:
    if not value or not reference:
        return None
    s = _norm(value)
    for fmt in ("%d %b", "%d-%b"):
        try:
            dt = datetime.strptime(s, fmt)
            candidate = date(reference.year, dt.month, dt.day)
            # Around New Year, use the closest year.
            variants = [candidate]
            for y in (reference.year - 1, reference.year + 1):
                try:
                    variants.append(date(y, dt.month, dt.day))
                except ValueError:
                    pass
            return min(variants, key=lambda d: abs((d - reference).days))
        except ValueError:
            pass
    return None


def screeningmantis_for_row(row: dict, timeout: int = 10) -> dict:
    symbol = _clean_symbol(row)
    if not symbol:
        return {"source": "ScreeningMantis public table", "error": "missing symbol"}
    all_rows = _parse_screeningmantis_public(timeout=timeout)
    data = dict(all_rows.get(symbol) or {})
    if not data:
        return {
            "source": "ScreeningMantis public table",
            "sourceUrl": "https://www.screeningmantis.com/",
            "symbol": symbol,
            "error": "symbol not present in currently rendered public table",
        }

    data["symbol"] = symbol
    rd = _parse_date(row.get("resultDate") or row.get("result_date") or row.get("earningsDate"))
    past = _partial_day_month_date(data.get("pastEarnings"), rd)
    data["pastEarningsDate"] = past.isoformat() if past else None
    data["resultDateMatched"] = bool(rd and past and abs((past - rd).days) <= 7)
    return data


def concall_for_row(row: dict, timeout: int = 10) -> dict:
    """Read a public Concall.in company documents page when URL/ID is known.

    Concall uses internal numeric company IDs. We deliberately do not scrape a
    search engine to guess IDs. A row can supply `concallUrl`/`concallCompanyId`.
    """
    raw_url = _norm(row.get("concallUrl") or row.get("concall_url"))
    cid = _norm(row.get("concallCompanyId") or row.get("concall_company_id"))
    if raw_url:
        url = raw_url
    elif cid.isdigit():
        url = f"https://concall.in/company/{cid}/updates/documents"
    else:
        return {
            "source": "Concall.in public documents",
            "sourceUrl": None,
            "error": "Concall company URL/ID not mapped",
        }

    html = _public_get(url, timeout=timeout)
    if not html:
        return {
            "source": "Concall.in public documents",
            "sourceUrl": url,
            "error": "public page unavailable",
        }
    parser = _parse_page(html)
    text_flat = _norm(parser.text)
    out: dict[str, Any] = {
        "source": "Concall.in public documents",
        "sourceUrl": url,
        "hasInvestorPresentation": "Investor Presentation" in text_flat,
        "hasConcallTranscript": "Concall Transcript" in text_flat,
        "hasFinancialResults": "Financial Results" in text_flat,
    }
    patterns = {
        "marketCapCr": r"₹\s*([0-9,.]+)\s*Cr\s*Market Cap",
        "trailingPE": r"([0-9,.]+)\s*PE",
        "priceToBook": r"([0-9,.]+)\s*P/B",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text_flat, flags=re.I)
        if m:
            out[key] = _num(m.group(1))

    docs: list[dict[str, str]] = []
    for href, anchor in parser.links:
        a = anchor.lower()
        if any(token in a for token in ("financial results", "investor presentation", "concall transcript", "recording")):
            docs.append({"title": anchor, "url": urljoin(url, href)})
    out["documents"] = docs[:12]
    return out


def combined_public_context(row: dict, include_screeningmantis: bool = True) -> dict:
    stock = stockscans_for_row(row)
    concall = concall_for_row(row)
    mantis = screeningmantis_for_row(row) if include_screeningmantis else {}
    return {
        "stockScans": stock,
        "concall": concall,
        "screeningMantis": mantis,
    }
