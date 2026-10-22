"""Run a finviz screen and save the matching tickers to data/screener.db.

Usage: save_screener_results.py [premarket|momentum]   (default: premarket)
    premarket -> gap-up screen for 07:00-09:30 ET, sorted by gap
    momentum  -> up-on-the-day screen for after the open, sorted by % change

Each run writes:
    scan_runs       -> one row: run time, screen name and the finviz filter string used
    companies       -> one row per new company, keyed by its SEC CIK
    symbols         -> one row per new listing (symbol, exchange, company), e.g. ("WORX", "NASDAQ", 3)
    scan_results    -> one row per ticker: rank, price, gap %, change %, rel volume, volume
    share_structure -> today's float, shares outstanding, short float % and short ratio for each ticker
    daily_levels    -> today's previous close for each ticker

A run whose tickers (in rank order) and numbers all match the previous run of
the same screen is not saved. Prints the vnpy vt_symbols (SYMBOL.EXCHANGE) joined with "," for the
portfolio strategy vt_symbols field, and whether the run was saved or skipped
to stderr.
"""
import argparse
import json
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from finviz.helper_functions.request_functions import USER_AGENT
from lxml import html

from db import connect, get_company_id, get_symbol_id, init_db, now, now_text
from sec import load_cik_map

# Custom view (v=152) columns: 0 No., 1 Ticker, 2 Company, 129 Exchange,
# 25 Float, 65 Price, 61 Gap, 64 Rel Volume, 67 Volume, 24 Shares Outstanding,
# 30 Short Float, 31 Short Ratio
COLUMNS = "0,1,2,129,25,65,61,64,67,24,30,31"

# Screen name -> finviz URL. finviz silently ignores filter codes it doesn't
# recognize, so a typo here widens the screen instead of raising an error.
SCREENS = {
    # Gapped up 4%+, current volume 100K+, rel volume 2x+, $1-$20, float under 50M
    "premarket": f"https://finviz.com/screener.ashx?v=152&f=ta_gap_u4,sh_curvol_o100,sh_relvol_o2,sh_price_1to20,sh_float_u50&c={COLUMNS}&o=-gap",
    # Up 10%+ on the day (gapped or not), same volume, price and float limits
    "momentum": f"https://finviz.com/screener.ashx?v=152&f=ta_change_u10,sh_curvol_o100,sh_relvol_o2,sh_price_1to20,sh_float_u50&c={COLUMNS}&o=-change",
}

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


def to_shares(millions: float | None) -> int | None:
    return None if millions is None else round(millions * 1_000_000)


def main(screen: str = "premarket") -> None:
    url = SCREENS[screen]
    criteria = parse_qs(urlparse(url).query)["f"][0]
    rows = scrape_rows(url)
    tickers: list[tuple[str, str]] = []
    for row in rows:
        code: str = row["text"]["exchange"]
        tickers.append((row["text"]["ticker"], EXCHANGES.get(code, code)))
    today = now().date().isoformat()
    cik_map = load_cik_map()

    snapshot = [
        (
            symbol, exchange, row["numbers"].get("price"), row["numbers"].get("gap"),
            row["numbers"].get("change"), row["numbers"].get("relativeVolume"), row["numbers"].get("volume"),
        )
        for row, (symbol, exchange) in zip(rows, tickers, strict=True)
    ]

    init_db()
    with connect() as conn:
        previous_run = conn.execute(
            "SELECT MAX(id) FROM scan_runs WHERE screen = ? AND criteria = ?", (screen, criteria)
        ).fetchone()[0]
        previous = conn.execute(
            """
            SELECT s.symbol, s.exchange, r.price, r.gap_pct, r.change_pct, r.rel_volume, r.volume
            FROM scan_results r
            JOIN symbols s ON s.id = r.symbol_id
            WHERE r.run_id = ?
            ORDER BY r.rank
            """,
            (previous_run,),
        ).fetchall()

        run_id = None
        if [tuple(p) for p in previous] != snapshot:
            # Set run_at here: the column default is UTC in older databases and
            # always ignores the screener.timezone setting.
            run_id = conn.execute(
                "INSERT INTO scan_runs (run_at, screen, criteria) VALUES (?, ?, ?)",
                (now_text(), screen, criteria),
            ).lastrowid
            print(f"{screen}: saved run {run_id}: {len(tickers)} tickers", file=sys.stderr)
        else:
            print(f"{screen}: skipped: unchanged since run {previous_run} ({len(tickers)} tickers)", file=sys.stderr)

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
            # finviz reports float and shares outstanding in millions of shares,
            # short float (shortInterestShare) as a percent, and short ratio
            # (shortInterestRatio) in days to cover.
            float_shares = to_shares(numbers.get("sharesFloat"))
            shares_outstanding = to_shares(numbers.get("sharesOutstanding2"))
            short_float_pct = numbers.get("shortInterestShare")
            short_ratio = numbers.get("shortInterestRatio")
            if (float_shares, shares_outstanding, short_float_pct, short_ratio) != (None, None, None, None):
                conn.execute(
                    """
                    INSERT INTO share_structure
                        (symbol_id, as_of, float_shares, shares_outstanding, short_float_pct, short_ratio)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (symbol_id, as_of) DO UPDATE SET
                        float_shares       = COALESCE(excluded.float_shares, float_shares),
                        shares_outstanding = COALESCE(excluded.shares_outstanding, shares_outstanding),
                        short_float_pct    = COALESCE(excluded.short_float_pct, short_float_pct),
                        short_ratio        = COALESCE(excluded.short_ratio, short_ratio)
                    """,
                    (symbol_id, today, float_shares, shares_outstanding, short_float_pct, short_ratio),
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
    parser = argparse.ArgumentParser(description="Save a finviz screen to data/screener.db.")
    parser.add_argument("screen", nargs="?", default="premarket", choices=sorted(SCREENS))
    main(parser.parse_args().screen)
