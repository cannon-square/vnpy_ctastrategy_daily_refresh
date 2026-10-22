"""Run a finviz screen and save the matching tickers to data/screener.db.

Each run writes:
    scan_runs       -> one row: run time and the finviz filter string used
    companies       -> one row per new company, keyed by its SEC CIK
    symbols         -> one row per new listing (symbol, exchange, company), e.g. ("WORX", "NASDAQ", 3)
    scan_results    -> one row per ticker: rank, price, gap %, change %, rel volume, volume
    share_structure -> today's float for each ticker
    daily_levels    -> today's previous close for each ticker

A run whose tickers (in rank order) match the previous run is not saved.
Prints the vnpy vt_symbols (SYMBOL.EXCHANGE) joined with "," for the
portfolio strategy vt_symbols field.
"""
import json
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from finviz.helper_functions.request_functions import USER_AGENT
from lxml import html

from db import connect, get_company_id, get_symbol_id, init_db
from sec import load_cik_map

# Custom view (v=152) columns: 0 No., 1 Ticker, 2 Company, 129 Exchange,
# 25 Float, 65 Price, 61 Gap, 64 Rel Volume, 67 Volume
url = "https://finviz.com/screener.ashx?v=152&f=ta_gap_u4,sh_curvol_o100,sh_relvol_o2,sh_price_1to20,sh_float_u50&c=0,1,2,129,25,65,61,64,67&o=-gap"

# finviz exchange code -> exchange name
EXCHANGES = {"NASD": "NASDAQ", "NYSE": "NYSE", "AMEX": "AMEX", "CBOE": "CBOE"}


def scrape_rows(screen_url: str) -> list[dict[str, Any]]:
    """Read the rows finviz embeds as JSON in its screener page, in screen order.

    The finviz-api 2.0.0 Screener can't parse the current finviz layout (it
    reports NoResults even when there are matches), so the page is read directly.
    """
    rows: list[dict[str, Any]] = []
    while True:
        page_url = f"{screen_url}&r={len(rows) + 1}"
        response = requests.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        response.raise_for_status()
        props = html.fromstring(response.text).cssselect("script[data-ssr-props]")
        data = json.loads(props[0].text)["data"]
        page = data["rows"]
        rows.extend(page)
        if not page or len(rows) >= data["totalCount"]:
            return rows


def main() -> None:
    criteria = parse_qs(urlparse(url).query)["f"][0]
    rows = scrape_rows(url)
    tickers: list[tuple[str, str]] = []
    for row in rows:
        code: str = row["text"]["exchange"]
        tickers.append((row["text"]["ticker"], EXCHANGES.get(code, code)))
    today = date.today().isoformat()
    cik_map = load_cik_map()

    init_db()
    with connect() as conn:
        previous = conn.execute(
            """
            SELECT s.symbol, s.exchange
            FROM scan_results r
            JOIN symbols s ON s.id = r.symbol_id
            WHERE r.run_id = (SELECT MAX(id) FROM scan_runs WHERE criteria = ?)
            ORDER BY r.rank
            """,
            (criteria,),
        ).fetchall()

        run_id = None
        if [(p["symbol"], p["exchange"]) for p in previous] != tickers:
            run_id = conn.execute(
                "INSERT INTO scan_runs (criteria) VALUES (?)", (criteria,)
            ).lastrowid

        for rank, (row, (symbol, exchange)) in enumerate(zip(rows, tickers, strict=True), start=1):
            numbers = row["numbers"]
            company_id = None
            if symbol in cik_map:
                cik, name = cik_map[symbol]
                company_id = get_company_id(conn, cik, name)
            symbol_id = get_symbol_id(conn, symbol, exchange, company_id)
            if run_id is not None:
                conn.execute(
                    """
                    INSERT INTO scan_results
                        (run_id, symbol_id, rank, price, gap_pct, change_pct, rel_volume, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, symbol_id, rank, numbers.get("price"), numbers.get("gap"),
                        numbers.get("change"), numbers.get("relativeVolume"), numbers.get("volume"),
                    ),
                )
            # Daily values are refreshed on every run, even when the run itself is skipped.
            # finviz reports float in millions of shares
            if numbers.get("sharesFloat") is not None:
                conn.execute(
                    """
                    INSERT INTO share_structure (symbol_id, as_of, float_shares)
                    VALUES (?, ?, ?)
                    ON CONFLICT (symbol_id, as_of) DO UPDATE SET float_shares = excluded.float_shares
                    """,
                    (symbol_id, today, round(numbers["sharesFloat"] * 1_000_000)),
                )
            if numbers.get("prevClose") is not None:
                conn.execute(
                    """
                    INSERT INTO daily_levels (symbol_id, trade_date, prev_close)
                    VALUES (?, ?, ?)
                    ON CONFLICT (symbol_id, trade_date) DO UPDATE SET prev_close = excluded.prev_close
                    """,
                    (symbol_id, today, numbers["prevClose"]),
                )

    print(",".join(f"{symbol}.{exchange}" for symbol, exchange in tickers))


if __name__ == "__main__":
    main()
