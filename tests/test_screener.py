"""Tests for the finviz screener script, its database and the SEC company lookup.

finviz and the SEC are faked, so these run offline. Set SCREENER_LIVE_TESTS=1
to also check the real finviz page and SEC file.
"""
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
import requests

import catalysts
import db
import save_screener_results as ssr
import sec


def make_row(ticker: str, exchange: str = "NASD", price: float = 2.5, gap: float = 20.0) -> dict[str, Any]:
    return {
        "text": {"ticker": ticker, "exchange": exchange},
        "numbers": {
            "price": price, "gap": gap, "change": 15.0, "relativeVolume": 8.0,
            "volume": 1_000_000, "sharesFloat": 4.5, "prevClose": 2.0,
            "sharesOutstanding2": 6.25, "shortInterestShare": 12.5, "shortInterestRatio": 1.75,
        },
    }


ROWS: list[dict[str, Any]] = [make_row("AAA"), make_row("BBB", "NYSE"), make_row("CCC", "AMEX")]
SEC_FILE: dict[str, Any] = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [[111, "AAA Corp", "AAA", "Nasdaq"], [222, "BBB Inc", "BBB", "NYSE"]],
}


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text: str = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return json.loads(self.text)


def finviz_page(rows: list[dict[str, Any]], url: str, page_size: int = 2) -> FakeResponse:
    start: int = int(parse_qs(urlparse(url).query)["r"][0]) - 1
    data: dict[str, Any] = {"totalCount": len(rows), "rows": rows[start:start + page_size]}
    return FakeResponse(f"<html><script data-ssr-props>{json.dumps({'data': data})}</script></html>")


def news_page(rows: list[tuple[str, str, str, str]]) -> str:
    """A finviz quote page news table from (time cell, headline, href, source) rows."""
    trs = "".join(
        f'<tr><td>{stamp}</td><td><div class="news-link-left"><a class="tab-link-news" href="{href}">{headline}</a>'
        f'</div><div class="news-link-right"><span>({source})</span></div></td></tr>'
        for stamp, headline, href, source in rows
    )
    return f'<html><table id="news-table">{trs}</table></html>'


def submissions(filings: list[tuple[str, datetime, str]]) -> dict[str, Any]:
    """An SEC submissions file from (form, accepted UTC, 8-K items) filings."""
    return {"filings": {"recent": {
        "form": [f[0] for f in filings],
        "acceptanceDateTime": [f[1].strftime("%Y-%m-%dT%H:%M:%S.000Z") for f in filings],
        "items": [f[2] for f in filings],
        "primaryDocDescription": [f"{f[0]} doc" for f in filings],
        "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(len(filings))],
        "primaryDocument": [f"doc{i}.htm" for i in range(len(filings))],
    }}}


@pytest.fixture
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Point the database and SEC cache at tmp_path and fake finviz and the SEC."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screener.db")
    monkeypatch.setattr(sec, "CACHE_PATH", tmp_path / "sec_company_tickers.json")
    monkeypatch.setattr(catalysts, "FINVIZ_DELAY", 0)
    state: dict[str, Any] = {
        "rows": ROWS, "sec_online": True, "sec_calls": 0,
        "news": {}, "news_calls": 0, "news_error": False,
        "filings": {}, "filings_calls": 0,
    }

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        if url == sec.TICKERS_URL:
            state["sec_calls"] += 1
            if not state["sec_online"]:
                raise requests.ConnectionError("offline")
            return FakeResponse(json.dumps(SEC_FILE))
        if url.startswith("https://data.sec.gov/submissions/"):
            state["filings_calls"] += 1
            if not state["sec_online"]:
                raise requests.ConnectionError("offline")
            cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
            return FakeResponse(json.dumps(submissions(state["filings"].get(cik, []))))
        if "/quote.ashx" in url:
            state["news_calls"] += 1
            if state["news_error"]:
                raise requests.HTTPError("429 Too Many Requests")
            ticker = parse_qs(urlparse(url).query)["t"][0]
            return FakeResponse(news_page(state["news"].get(ticker, [])))
        return finviz_page(state["rows"], url)

    monkeypatch.setattr(requests, "get", fake_get)
    yield state


def query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def utc_to_local(text: str) -> str:
    utc = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_scrape_rows_reads_every_page(offline: dict[str, Any]) -> None:
    rows = ssr.scrape_rows(ssr.SCREENS["premarket"])
    assert [r["text"]["ticker"] for r in rows] == ["AAA", "BBB", "CCC"]


# finviz ignores unknown filter codes instead of failing, so pin them here.
KNOWN_FILTERS = {"ta_gap_u4", "ta_change_u10", "sh_curvol_o100", "sh_relvol_o2", "sh_price_1to20", "sh_float_u50"}


@pytest.mark.parametrize("screen, order", [("premarket", "-gap"), ("momentum", "-change")])
def test_screen_urls(screen: str, order: str) -> None:
    query = parse_qs(urlparse(ssr.SCREENS[screen]).query)
    assert set(query["f"][0].split(",")) <= KNOWN_FILTERS
    assert query["o"] == [order]
    assert query["c"] == [ssr.COLUMNS]


def test_premarket_criteria_unchanged() -> None:
    # Runs are compared by criteria; changing it would split the premarket history.
    criteria = parse_qs(urlparse(ssr.SCREENS["premarket"]).query)["f"][0]
    assert criteria == "ta_gap_u4,sh_curvol_o100,sh_relvol_o2,sh_price_1to20,sh_float_u50"


def test_screens_are_saved_and_compared_separately(offline: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    ssr.main("premarket")
    ssr.main("momentum")
    ssr.main("momentum")

    assert query("SELECT id, screen FROM scan_runs ORDER BY id") == [(1, "premarket"), (2, "momentum")]
    assert "momentum: skipped: unchanged since run 2" in capsys.readouterr().err
    # Both screens share symbols and companies.
    assert query("SELECT COUNT(*) FROM symbols") == [(3,)]

    offline["rows"] = ROWS[:1]
    ssr.main("momentum")
    assert db.latest_vt_symbols("momentum") == ["AAA.NASDAQ"]
    assert db.latest_vt_symbols() == ["AAA.NASDAQ", "BBB.NYSE", "CCC.AMEX"]


def test_adds_short_ratio_to_existing_share_structure(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE share_structure (symbol_id INTEGER NOT NULL, as_of DATE NOT NULL, float_shares INTEGER,
            shares_outstanding INTEGER, short_float_pct REAL, recent_offering INTEGER, PRIMARY KEY (symbol_id, as_of));
        INSERT INTO share_structure (symbol_id, as_of, float_shares) VALUES (1, '2026-10-21', 100);
        PRAGMA user_version = 2;
        """
    )
    conn.close()

    db.init_db()

    assert query("SELECT float_shares, short_ratio FROM share_structure") == [(100, None)]
    assert query("PRAGMA user_version") == [(3,)]


def test_adds_screen_to_existing_runs(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, run_at DATETIME NOT NULL, criteria TEXT NOT NULL);
        INSERT INTO scan_runs (run_at, criteria) VALUES ('2026-10-21 19:36:03', 'crit');
        PRAGMA user_version = 1;
        """
    )
    conn.close()

    db.init_db()

    assert query("SELECT run_at, screen FROM scan_runs") == [("2026-10-21 19:36:03", "premarket")]
    assert query("PRAGMA user_version") == [(db.SCHEMA_VERSION,)]


def test_main_saves_run_results_and_daily_values(offline: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    ssr.main()

    assert capsys.readouterr().out.strip() == "AAA.NASDAQ,BBB.NYSE,CCC.AMEX"
    assert query("SELECT COUNT(*) FROM scan_runs") == [(1,)]
    assert query(
        "SELECT s.symbol, r.rank, r.price, r.gap_pct, r.change_pct, r.rel_volume, r.volume "
        "FROM scan_results r JOIN symbols s ON s.id = r.symbol_id ORDER BY r.rank"
    ) == [
        ("AAA", 1, 2.5, 20.0, 15.0, 8.0, 1_000_000),
        ("BBB", 2, 2.5, 20.0, 15.0, 8.0, 1_000_000),
        ("CCC", 3, 2.5, 20.0, 15.0, 8.0, 1_000_000),
    ]
    assert query(
        "SELECT DISTINCT float_shares, shares_outstanding, short_float_pct, short_ratio FROM share_structure"
    ) == [(4_500_000, 6_250_000, 12.5, 1.75)]
    assert query("SELECT DISTINCT prev_close FROM daily_levels") == [(2.0,)]
    assert query("PRAGMA foreign_key_check") == []


def test_run_at_is_local_time(offline: dict[str, Any]) -> None:
    before = datetime.now().replace(microsecond=0)
    ssr.main()
    after = datetime.now()

    run_at = datetime.strptime(query("SELECT run_at FROM scan_runs")[0][0], "%Y-%m-%d %H:%M:%S")
    assert before <= run_at <= after


def test_utc_run_times_converted_once(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, run_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, criteria TEXT NOT NULL);
        INSERT INTO scan_runs (run_at, criteria) VALUES ('2026-07-01 12:30:00', 'crit');
        """
    )
    conn.close()

    db.init_db()
    db.init_db()

    assert query("SELECT run_at FROM scan_runs") == [(utc_to_local("2026-07-01 12:30:00"),)]
    assert query("PRAGMA user_version") == [(db.SCHEMA_VERSION,)]


def test_times_use_timezone_setting(offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tokyo = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr(db, "TIMEZONE", tokyo)
    before = datetime.now(tokyo).replace(microsecond=0, tzinfo=None)
    ssr.main()
    after = datetime.now(tokyo).replace(tzinfo=None)

    run_at = datetime.strptime(query("SELECT run_at FROM scan_runs")[0][0], "%Y-%m-%d %H:%M:%S")
    assert before <= run_at <= after
    assert query("SELECT DISTINCT as_of FROM share_structure") == [(run_at.date().isoformat(),)]
    assert query("SELECT DISTINCT trade_date FROM daily_levels") == [(run_at.date().isoformat(),)]


@pytest.mark.parametrize("utc, local", [
    ("2026-07-01 12:00:00", "2026-07-01 08:00:00"),  # EDT, UTC-4
    ("2026-01-15 12:00:00", "2026-01-15 07:00:00"),  # EST, UTC-5
    ("2026-11-01 05:30:00", "2026-11-01 01:30:00"),  # last 1:30 EDT before the switch
    ("2026-11-01 06:30:00", "2026-11-01 01:30:00"),  # repeated 1:30, now EST
    ("2026-03-08 07:30:00", "2026-03-08 03:30:00"),  # 2:00-3:00 is skipped in spring
])
def test_utc_to_local_applies_daylight_saving(monkeypatch: pytest.MonkeyPatch, utc: str, local: str) -> None:
    monkeypatch.setattr(db, "TIMEZONE", ZoneInfo("America/New_York"))
    assert db.utc_to_local(utc) == local


def test_new_database_column_default_is_local(offline: dict[str, Any]) -> None:
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT INTO scan_runs (criteria) VALUES ('crit')")

    run_at = datetime.strptime(query("SELECT run_at FROM scan_runs")[0][0], "%Y-%m-%d %H:%M:%S")
    assert abs(run_at - datetime.now()) < timedelta(seconds=5)


def test_missing_share_value_keeps_earlier_one(offline: dict[str, Any]) -> None:
    ssr.main()
    row = make_row("AAA", price=2.6)
    del row["numbers"]["shortInterestShare"]
    row["numbers"]["sharesOutstanding2"] = 7.0
    offline["rows"] = [row]
    ssr.main()

    assert query(
        "SELECT float_shares, shares_outstanding, short_float_pct FROM share_structure s "
        "JOIN symbols y ON y.id = s.symbol_id WHERE y.symbol = 'AAA'"
    ) == [(4_500_000, 7_000_000, 12.5)]


def test_main_links_companies_by_cik(offline: dict[str, Any]) -> None:
    ssr.main()

    assert query(
        "SELECT s.symbol, c.cik, c.name FROM symbols s LEFT JOIN companies c ON c.id = s.company_id ORDER BY s.symbol"
    ) == [("AAA", 111, "AAA Corp"), ("BBB", 222, "BBB Inc"), ("CCC", None, None)]


def test_identical_rerun_is_skipped(offline: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    ssr.main()
    assert "saved run 1: 3 tickers" in capsys.readouterr().err
    ssr.main()
    assert "skipped: unchanged since run 1 (3 tickers)" in capsys.readouterr().err

    assert query("SELECT COUNT(*) FROM scan_runs") == [(1,)]
    assert query("SELECT COUNT(*) FROM symbols") == [(3,)]


@pytest.mark.parametrize("changed", [
    ROWS[::-1],
    ROWS[:2],
    ROWS + [make_row("DDD")],
    [make_row("AAA", price=2.6), ROWS[1], ROWS[2]],
    [make_row("AAA", gap=25.0), ROWS[1], ROWS[2]],
], ids=["reordered", "dropped", "added", "price moved", "gap moved"])
def test_changed_list_saves_new_run(offline: dict[str, Any], changed: list[dict[str, Any]]) -> None:
    ssr.main()
    offline["rows"] = changed
    ssr.main()

    assert query("SELECT COUNT(*) FROM scan_runs") == [(2,)]
    expected = [f"{r['text']['ticker']}.{ssr.EXCHANGES[r['text']['exchange']]}" for r in changed]
    assert db.latest_vt_symbols() == expected


def test_ticker_reuse_and_rename(offline: dict[str, Any]) -> None:
    db.init_db()
    with db.connect() as conn:
        olox = db.get_symbol_id(conn, "OLOX", "NASDAQ", db.get_company_id(conn, 1, "Olenox"))
        assert db.get_symbol_id(conn, "OLOX", "NASDAQ", db.get_company_id(conn, 1, "Olenox")) == olox

        reused = db.get_symbol_id(conn, "OLOX", "NASDAQ", db.get_company_id(conn, 2, "Other Co"))
        renamed = db.get_symbol_id(conn, "OLX2", "NASDAQ", db.get_company_id(conn, 1, "Olenox"))
        assert len({olox, reused, renamed}) == 3

        # Without a CIK, the most recent listing of the ticker is reused.
        assert db.get_symbol_id(conn, "OLOX", "NASDAQ") == reused

    assert query("SELECT COUNT(*) FROM symbols s JOIN companies c ON c.id = s.company_id WHERE c.cik = 1") == [(2,)]


def test_listing_without_company_is_linked_later(offline: dict[str, Any]) -> None:
    db.init_db()
    with db.connect() as conn:
        unlinked = db.get_symbol_id(conn, "AAA", "NASDAQ")
        assert db.get_symbol_id(conn, "AAA", "NASDAQ", db.get_company_id(conn, 111, "AAA Corp")) == unlinked

    assert query("SELECT company_id IS NOT NULL FROM symbols") == [(1,)]


def test_migrates_legacy_json_table(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE screener (id INTEGER PRIMARY KEY AUTOINCREMENT, watchlist TEXT NOT NULL,
            time_added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, criteria TEXT, exch TEXT, tickers TEXT);
        INSERT INTO screener (watchlist, time_added, criteria, exch, tickers)
            VALUES ('["AAA","BBB"]', '2026-01-02 08:00:00', 'crit', '["NASDAQ","NYSE"]', '["AAA.NASDAQ","BBB.NYSE"]');
        """
    )
    conn.close()

    db.init_db()

    assert query("SELECT name FROM sqlite_master WHERE name = 'screener'") == []
    assert query("SELECT run_at, criteria FROM scan_runs") == [(utc_to_local("2026-01-02 08:00:00"), "crit")]
    assert db.latest_vt_symbols() == ["AAA.NASDAQ", "BBB.NYSE"]


def test_migrates_legacy_rows_without_exch(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE screener (id INTEGER PRIMARY KEY AUTOINCREMENT, watchlist TEXT NOT NULL,
            time_added DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, criteria TEXT, exch TEXT, tickers TEXT);
        INSERT INTO screener (watchlist, criteria, tickers) VALUES ('["AAA","BBB"]', 'crit', '["AAA.NASDAQ","BBB.NYSE"]');
        INSERT INTO screener (watchlist, criteria) VALUES ('["CCC"]', 'crit');
        """
    )
    conn.close()

    db.init_db()

    assert query(
        "SELECT r.run_id, s.symbol, s.exchange FROM scan_results r JOIN symbols s ON s.id = r.symbol_id ORDER BY r.run_id, r.rank"
    ) == [(1, "AAA", "NASDAQ"), (1, "BBB", "NYSE"), (2, "CCC", "")]


def test_adds_company_id_to_existing_symbols(offline: dict[str, Any]) -> None:
    conn = sqlite3.connect(db.DB_PATH)
    conn.executescript(
        """
        CREATE TABLE symbols (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, exchange TEXT NOT NULL, UNIQUE (symbol, exchange));
        CREATE TABLE scan_runs (id INTEGER PRIMARY KEY, run_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, criteria TEXT NOT NULL);
        CREATE TABLE scan_results (run_id INTEGER NOT NULL REFERENCES scan_runs(id), symbol_id INTEGER NOT NULL REFERENCES symbols(id),
            rank INTEGER NOT NULL, price REAL, gap_pct REAL, change_pct REAL, rel_volume REAL, volume INTEGER,
            PRIMARY KEY (run_id, symbol_id));
        INSERT INTO symbols VALUES (7, 'AAA', 'NASDAQ');
        INSERT INTO scan_runs (id, criteria) VALUES (1, 'crit');
        INSERT INTO scan_results (run_id, symbol_id, rank) VALUES (1, 7, 1);
        """
    )
    conn.close()

    db.init_db()

    assert "company_id" in {row[1] for row in query("PRAGMA table_info(symbols)")}
    assert query("SELECT id, symbol, company_id FROM symbols") == [(7, "AAA", None)]
    assert query("PRAGMA foreign_key_check") == []


def test_sec_map_downloads_once_and_caches(offline: dict[str, Any]) -> None:
    assert sec.load_cik_map() == {"AAA": (111, "AAA Corp"), "BBB": (222, "BBB Inc")}
    sec.load_cik_map()

    assert offline["sec_calls"] == 1
    assert sec.CACHE_PATH.exists()


def test_sec_offline_uses_stale_cache(offline: dict[str, Any]) -> None:
    sec.load_cik_map()
    old = time.time() - 2 * sec.CACHE_MAX_AGE
    os.utime(sec.CACHE_PATH, (old, old))
    offline["sec_online"] = False

    assert sec.load_cik_map() == {"AAA": (111, "AAA Corp"), "BBB": (222, "BBB Inc")}
    assert offline["sec_calls"] == 2


def test_screener_saves_without_sec(offline: dict[str, Any]) -> None:
    offline["sec_online"] = False

    assert sec.load_cik_map() == {}
    ssr.main()
    assert query("SELECT COUNT(*) FROM scan_results") == [(3,)]
    assert query("SELECT COUNT(*) FROM symbols WHERE company_id IS NULL") == [(3,)]


@pytest.mark.skipif(os.environ.get("SCREENER_LIVE_TESTS") != "1", reason="set SCREENER_LIVE_TESTS=1 to hit finviz and the SEC")
def test_live_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sec, "CACHE_PATH", tmp_path / "sec_company_tickers.json")

    for url in ssr.SCREENS.values():
        rows = ssr.scrape_rows(url)
        for row in rows:
            assert row["text"]["exchange"] in ssr.EXCHANGES
            assert {
                "price", "gap", "change", "relativeVolume", "volume", "sharesFloat", "prevClose",
                "sharesOutstanding2", "shortInterestShare", "shortInterestRatio",
            } <= set(row["numbers"])

    cik_map = sec.load_cik_map()
    assert len(cik_map) > 5000

    # AAPL always has recent news and filings.
    news = catalysts.fetch_finviz_news("AAPL")
    assert len(news) > 10
    assert all(item.headline and item.url.startswith("http") for item in news)
    filings = sec.load_filings(cik_map["AAPL"][0])
    assert filings and {"form", "accepted", "items", "description", "url"} <= set(filings[0])


# News catalysts --------------------------------------------------------------

def local(when: datetime) -> str:
    return when.astimezone(db.TIMEZONE).strftime(db.TIME_FORMAT)


def test_parse_finviz_news_time_formats() -> None:
    page = news_page([
        ("Today 08:30AM", "Acme Announces FDA Approval", "/news/1/acme", "ACCESSWIRE"),
        ("07:05AM", "Acme Prices $5M Registered Direct Offering", "https://example.com/2", "GlobeNewswire"),
        ("Oct-20-26 04:15PM", "Acme Reports Third Quarter Results", "https://example.com/3", "Business Wire"),
        ("09:00AM", "Acme Signs Supply Agreement", "https://example.com/4", "PR Newswire"),
    ])
    items = catalysts.parse_finviz_news(page, today=datetime(2026, 10, 21).date())

    eastern = ZoneInfo("America/New_York")
    assert [(i.published_at, i.category, i.source) for i in items] == [
        (datetime(2026, 10, 21, 8, 30, tzinfo=eastern), "fda", "ACCESSWIRE"),
        (datetime(2026, 10, 21, 7, 5, tzinfo=eastern), "offering", "GlobeNewswire"),
        (datetime(2026, 10, 20, 16, 15, tzinfo=eastern), "earnings", "Business Wire"),
        (datetime(2026, 10, 20, 9, 0, tzinfo=eastern), "contract", "PR Newswire"),
    ]
    assert items[0].url == "https://finviz.com/news/1/acme"


@pytest.mark.parametrize("headline, category", [
    ("XYZ Announces Pricing of $10 Million Public Offering", "offering"),
    ("XYZ Enters Into Securities Purchase Agreement for Private Placement", "offering"),
    ("XYZ Announces 1-for-20 Reverse Stock Split", "reverse_split"),
    ("XYZ Receives FDA Clearance", "fda"),
    ("XYZ Phase 2 Trial Meets Primary Endpoint", "fda"),
    ("XYZ Reports Q3 2026 Financial Results", "earnings"),
    ("XYZ to Acquire ABC in All-Stock Merger", "merger"),
    ("XYZ Wins $3M Government Contract", "contract"),
    ("XYZ to Present at Investor Conference", "other"),
])
def test_categorize_headline(headline: str, category: str) -> None:
    assert catalysts.categorize_headline(headline) == category


@pytest.mark.parametrize("form, items, category", [
    ("424B5", "", "offering"), ("S-1/A", "", "offering"), ("F-3", "", "offering"),
    ("8-K", "1.01,3.02,9.01", "offering"), ("8-K", "2.01,9.01", "merger"),
    ("8-K", "2.02,9.01", "earnings"), ("8-K", "1.01,9.01", "contract"),
    ("10-Q", "", "earnings"), ("S-8", "", "filing"), ("8-K", "5.07", "filing"),
])
def test_categorize_filing(form: str, items: str, category: str) -> None:
    assert catalysts.categorize_filing(form, items) == category


def test_main_saves_news_and_flags_recent_offering(offline: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    offline["news"] = {"CCC": [("Oct-20-26 08:30AM", "CCC Announces Reverse Stock Split", "https://example.com/ccc", "PRN")]}
    offline["filings"] = {
        111: [("424B5", now - timedelta(days=10), ""), ("8-K", now - timedelta(days=12), "2.02,9.01")],
        222: [("424B5", now - timedelta(days=200), "")],  # outside the 90-day window
    }
    ssr.main()

    assert query("SELECT s.symbol, n.published_at, n.headline, n.source, n.category FROM news n "
                 "JOIN symbols s ON s.id = n.symbol_id ORDER BY s.symbol, n.published_at") == [
        ("AAA", local(now - timedelta(days=12)), "8-K: 8-K doc", "SEC EDGAR", "earnings"),
        ("AAA", local(now - timedelta(days=10)), "424B5: 424B5 doc", "SEC EDGAR", "offering"),
        ("CCC", local(datetime(2026, 10, 20, 8, 30, tzinfo=ZoneInfo("America/New_York"))),
         "CCC Announces Reverse Stock Split", "PRN", "reverse_split"),
    ]
    assert query("SELECT url FROM news WHERE category = 'offering'") == [
        ("https://www.sec.gov/Archives/edgar/data/111/000000000026000000/doc0.htm",)
    ]
    assert query("SELECT s.symbol, x.recent_offering FROM share_structure x JOIN symbols s ON s.id = x.symbol_id "
                 "ORDER BY s.symbol") == [("AAA", 1), ("BBB", 0), ("CCC", 0)]


def test_news_checks_are_throttled(offline: dict[str, Any]) -> None:
    ssr.main()
    assert (offline["news_calls"], offline["filings_calls"]) == (3, 2)  # CCC has no CIK

    offline["rows"] = ROWS[::-1]  # a changed list, so the run is saved
    ssr.main()
    assert (offline["news_calls"], offline["filings_calls"]) == (3, 2)

    with db.connect() as conn:
        stale = (db.now() - catalysts.CHECK_INTERVAL).strftime(db.TIME_FORMAT)
        conn.execute("UPDATE source_checks SET checked_at = ?", (stale,))
    ssr.main()
    assert (offline["news_calls"], offline["filings_calls"]) == (6, 4)


def test_duplicate_news_is_not_saved_twice(offline: dict[str, Any]) -> None:
    offline["news"] = {"AAA": [("Today 08:30AM", "AAA Wins Contract", "https://example.com/a", "PRN")]}
    ssr.main()
    with db.connect() as conn:
        conn.execute("DELETE FROM source_checks")
    ssr.main()

    assert query("SELECT COUNT(*) FROM news") == [(1,)]


def test_news_failure_keeps_run_and_retries(offline: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    offline["news_error"] = True
    ssr.main()

    err = capsys.readouterr().err
    assert "news: finviz check for AAA failed: 429 Too Many Requests" in err
    assert query("SELECT COUNT(*) FROM scan_results") == [(3,)]
    assert query("SELECT DISTINCT source FROM source_checks") == [("sec",)]

    offline["news_error"] = False
    calls = offline["news_calls"]
    offline["rows"] = ROWS[::-1]
    ssr.main()
    assert offline["news_calls"] == calls + 3
