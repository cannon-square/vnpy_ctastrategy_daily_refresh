"""Collect news catalysts for screener tickers: finviz headlines and SEC filings.

Both go into the news table with a category, and share_structure.recent_offering
is set to 1 when an offering headline or filing was published in the last 90
days. Each source is checked at most every CHECK_INTERVAL per ticker, and a
failed check is logged and retried on the next run.

Categories are a keyword/form-based first pass, not a reading of the news:
    headlines -> offering, reverse_split, fda, earnings, merger, contract, other
    filings   -> offering (S-1/S-3/F-1/F-3/S-11 and amendments, 424B
                 prospectuses, 8-K item 3.02), merger (8-K 2.01),
                 earnings (8-K 2.02, 10-Q, 10-K), contract (8-K 1.01), filing
"""
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from finviz.helper_functions.request_functions import USER_AGENT
from lxml import html

import db
from sec import load_filings

FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx?t={ticker}"
# finviz shows news times in US Eastern time
FINVIZ_TZ = ZoneInfo("America/New_York")
FINVIZ_DELAY = 0.5  # seconds between finviz quote pages
CHECK_INTERVAL = timedelta(minutes=15)
OFFERING_LOOKBACK = timedelta(days=90)

# First match wins, so the warning signs come first.
HEADLINE_CATEGORIES = [
    ("offering", r"\b(offering|registered direct|private placement|at[- ]the[- ]market|warrants?|"
                 r"securities purchase agreement|priced|pricing of|dilut)"),
    ("reverse_split", r"reverse (stock )?split"),
    ("fda", r"\bfda\b|\bphase (1|2|3|i{1,3})\b|clinical|orphan drug|breakthrough (therapy )?designation|510\(k\)"),
    ("earnings", r"earnings|financial results|\bquarter|\bq[1-4]\b|fiscal|revenue|guidance"),
    ("merger", r"merger|acqui(re|sition)|buyout|business combination"),
    ("contract", r"contract|agreement|partnership|partners with|purchase order|\border\b|award|collaborat"),
]
HEADLINE_PATTERNS = [(category, re.compile(pattern, re.IGNORECASE)) for category, pattern in HEADLINE_CATEGORIES]

OFFERING_FORMS = {"S-1", "S-3", "F-1", "F-3", "S-11", "S-1MEF", "S-3MEF", "F-1MEF", "F-3MEF"}
# 8-K item -> category, highest priority first
FILING_ITEMS = [("3.02", "offering"), ("2.01", "merger"), ("2.02", "earnings"), ("1.01", "contract")]


@dataclass
class NewsItem:
    published_at: datetime  # timezone-aware
    headline: str
    url: str
    source: str
    category: str


def categorize_headline(headline: str) -> str:
    for category, pattern in HEADLINE_PATTERNS:
        if pattern.search(headline):
            return category
    return "other"


def categorize_filing(form: str, items: str) -> str:
    base = form.split("/")[0]
    if base in OFFERING_FORMS or base.startswith("424B"):
        return "offering"
    if base == "8-K":
        filed_items = set(items.split(","))
        for item, category in FILING_ITEMS:
            if item in filed_items:
                return category
    if base in {"10-Q", "10-K"}:
        return "earnings"
    return "filing"


def parse_finviz_news(page: str, today: date) -> list[NewsItem]:
    """Read the news table on a finviz quote page.

    A row's time is "Today 08:30AM", "Oct-20-26 08:30AM", or just "08:30AM"
    for another story on the same date as the row above.
    """
    items: list[NewsItem] = []
    current: date | None = None
    for row in html.fromstring(page).cssselect("table#news-table tr"):
        cells = row.cssselect("td")
        links = row.cssselect("a.tab-link-news")
        if not cells or not links:
            continue
        stamp = cells[0].text_content().split()
        if len(stamp) == 2:
            current = today if stamp[0] == "Today" else datetime.strptime(stamp[0], "%b-%d-%y").date()
        if current is None or not stamp:
            continue
        clock = datetime.strptime(stamp[-1], "%I:%M%p").time()
        headline = links[0].text_content().strip()
        source = row.cssselect(".news-link-right span")
        items.append(NewsItem(
            published_at=datetime.combine(current, clock, tzinfo=FINVIZ_TZ),
            headline=headline,
            url=urljoin("https://finviz.com/", links[0].get("href", "")),
            source=source[0].text_content().strip().strip("()") if source else "finviz",
            category=categorize_headline(headline),
        ))
    return items


def fetch_finviz_news(ticker: str) -> list[NewsItem]:
    response = requests.get(FINVIZ_QUOTE_URL.format(ticker=ticker), headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return parse_finviz_news(response.text, datetime.now(FINVIZ_TZ).date())


def filing_news(filings: list[dict[str, str]], since: datetime) -> list[NewsItem]:
    """SEC filings accepted since the given time, as news items."""
    items: list[NewsItem] = []
    for filing in filings:
        # The SEC reports acceptance times in UTC
        accepted = datetime.fromisoformat(filing["accepted"].replace("Z", "+00:00"))
        if accepted.tzinfo is None:
            accepted = accepted.replace(tzinfo=timezone.utc)
        if accepted < since:
            continue
        items.append(NewsItem(
            published_at=accepted,
            headline=f"{filing['form']}: {filing['description']}",
            url=filing["url"],
            source="SEC EDGAR",
            category=categorize_filing(filing["form"], filing["items"]),
        ))
    return items


def due_sources(conn: sqlite3.Connection, symbol_id: int, has_cik: bool, now: datetime) -> list[str]:
    sources = ["finviz", "sec"] if has_cik else ["finviz"]
    checked = {
        row["source"]: datetime.strptime(row["checked_at"], db.TIME_FORMAT)
        for row in conn.execute("SELECT source, checked_at FROM source_checks WHERE symbol_id = ?", (symbol_id,))
    }
    local_now = now.replace(tzinfo=None)
    return [s for s in sources if s not in checked or local_now - checked[s] >= CHECK_INTERVAL]


def save_news(conn: sqlite3.Connection, symbol_id: int, items: list[NewsItem]) -> int:
    added = 0
    for item in items:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO news (symbol_id, published_at, headline, url, source, category)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                symbol_id, item.published_at.astimezone(db.TIMEZONE).strftime(db.TIME_FORMAT),
                item.headline, item.url, item.source, item.category,
            ),
        )
        added += cursor.rowcount
    return added


def update_recent_offering(conn: sqlite3.Connection, symbol_id: int, now: datetime) -> None:
    since = (now - OFFERING_LOOKBACK).strftime(db.TIME_FORMAT)
    flag = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM news WHERE symbol_id = ? AND category = 'offering' AND published_at >= ?)",
        (symbol_id, since),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO share_structure (symbol_id, as_of, recent_offering) VALUES (?, ?, ?)
        ON CONFLICT (symbol_id, as_of) DO UPDATE SET recent_offering = excluded.recent_offering
        """,
        (symbol_id, now.date().isoformat(), flag),
    )


def update_catalysts(listings: list[tuple[int, str, int | None]]) -> None:
    """Fetch due news for each (symbol_id, ticker, cik) and refresh recent_offering."""
    now = db.now()
    checked = added = 0
    fetched_finviz = False
    for symbol_id, ticker, cik in listings:
        with db.connect() as conn:
            sources = due_sources(conn, symbol_id, cik is not None, now)

        results: dict[str, list[NewsItem]] = {}
        for source in sources:
            try:
                if source == "finviz":
                    if fetched_finviz:
                        time.sleep(FINVIZ_DELAY)
                    fetched_finviz = True
                    results[source] = fetch_finviz_news(ticker)
                elif cik is not None:
                    results[source] = filing_news(load_filings(cik), now - OFFERING_LOOKBACK)
            except (requests.RequestException, ValueError, KeyError, IndexError) as error:
                print(f"news: {source} check for {ticker} failed: {error}", file=sys.stderr)

        with db.connect() as conn:
            for source, items in results.items():
                added += save_news(conn, symbol_id, items)
                conn.execute(
                    """
                    INSERT INTO source_checks (symbol_id, source, checked_at) VALUES (?, ?, ?)
                    ON CONFLICT (symbol_id, source) DO UPDATE SET checked_at = excluded.checked_at
                    """,
                    (symbol_id, source, now.strftime(db.TIME_FORMAT)),
                )
                checked += 1
            update_recent_offering(conn, symbol_id, now)

    print(f"news: {checked} checks, {added} new items", file=sys.stderr)
