"""Tests for the finviz screener script, its database and the SEC company lookup.

finviz and the SEC are faked, so these run offline. Set SCREENER_LIVE_TESTS=1
to also check the real finviz page and SEC file.
"""
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import db
import save_screener_results as ssr
import sec


def make_row(ticker: str, exchange: str = "NASD", price: float = 2.5, gap: float = 20.0) -> dict[str, Any]:
    return {
        "text": {"ticker": ticker, "exchange": exchange},
        "numbers": {
            "price": price, "gap": gap, "change": 15.0, "relativeVolume": 8.0,
            "volume": 1_000_000, "sharesFloat": 4.5, "prevClose": 2.0,
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


def finviz_page(rows: list[dict[str, Any]], url: str, page_size: int = 2) -> FakeResponse:
    start: int = int(parse_qs(urlparse(url).query)["r"][0]) - 1
    data: dict[str, Any] = {"totalCount": len(rows), "rows": rows[start:start + page_size]}
    return FakeResponse(f"<html><script data-ssr-props>{json.dumps({'data': data})}</script></html>")


@pytest.fixture
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Point the database and SEC cache at tmp_path and fake both websites."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screener.db")
    monkeypatch.setattr(sec, "CACHE_PATH", tmp_path / "sec_company_tickers.json")
    state: dict[str, Any] = {"rows": ROWS, "sec_online": True, "sec_calls": 0}

    def fake_get(url: str, **kwargs: Any) -> FakeResponse:
        if url == sec.TICKERS_URL:
            state["sec_calls"] += 1
            if not state["sec_online"]:
                raise requests.ConnectionError("offline")
            return FakeResponse(json.dumps(SEC_FILE))
        return finviz_page(state["rows"], url)

    monkeypatch.setattr(requests, "get", fake_get)
    yield state


def query(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_scrape_rows_reads_every_page(offline: dict[str, Any]) -> None:
    rows = ssr.scrape_rows(ssr.url)
    assert [r["text"]["ticker"] for r in rows] == ["AAA", "BBB", "CCC"]


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
    assert query("SELECT DISTINCT float_shares FROM share_structure") == [(4_500_000,)]
    assert query("SELECT DISTINCT prev_close FROM daily_levels") == [(2.0,)]
    assert query("PRAGMA foreign_key_check") == []


def test_main_links_companies_by_cik(offline: dict[str, Any]) -> None:
    ssr.main()

    assert query(
        "SELECT s.symbol, c.cik, c.name FROM symbols s LEFT JOIN companies c ON c.id = s.company_id ORDER BY s.symbol"
    ) == [("AAA", 111, "AAA Corp"), ("BBB", 222, "BBB Inc"), ("CCC", None, None)]


def test_identical_rerun_is_skipped(offline: dict[str, Any]) -> None:
    ssr.main()
    ssr.main()

    assert query("SELECT COUNT(*) FROM scan_runs") == [(1,)]
    assert query("SELECT COUNT(*) FROM symbols") == [(3,)]


@pytest.mark.parametrize("changed", [ROWS[::-1], ROWS[:2], ROWS + [make_row("DDD")]])
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
    assert query("SELECT run_at, criteria FROM scan_runs") == [("2026-01-02 08:00:00", "crit")]
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

    rows = ssr.scrape_rows(ssr.url)
    for row in rows:
        assert row["text"]["exchange"] in ssr.EXCHANGES
        assert {"price", "gap", "change", "relativeVolume", "volume", "sharesFloat", "prevClose"} <= set(row["numbers"])

    cik_map = sec.load_cik_map()
    assert len(cik_map) > 5000
