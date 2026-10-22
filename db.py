import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "screener.db"

SCHEMA = """
-- One row per company, keyed by its permanent SEC CIK
CREATE TABLE IF NOT EXISTS companies (
    id   INTEGER PRIMARY KEY,
    cik  INTEGER NOT NULL UNIQUE,
    name TEXT
);

-- One row per listing: a ticker on an exchange, linked to its company.
-- A ticker reused by another company gets a new row; a company that changes
-- ticker keeps its company_id. company_id is NULL when the SEC has no CIK.
CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY,
    symbol     TEXT NOT NULL,
    exchange   TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    UNIQUE (symbol, exchange, company_id)
);

-- One row per screener run
CREATE TABLE IF NOT EXISTS scan_runs (
    id       INTEGER PRIMARY KEY,
    run_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    criteria TEXT NOT NULL
);

-- One row per ticker per run, with its numbers at that moment
CREATE TABLE IF NOT EXISTS scan_results (
    run_id     INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
    symbol_id  INTEGER NOT NULL REFERENCES symbols(id),
    rank       INTEGER NOT NULL,
    price      REAL,
    gap_pct    REAL,
    change_pct REAL,
    rel_volume REAL,
    volume     INTEGER,
    PRIMARY KEY (run_id, symbol_id)
);

-- News catalyst
CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY,
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id),
    published_at DATETIME,
    headline     TEXT NOT NULL,
    url          TEXT,
    source       TEXT,
    category     TEXT,
    UNIQUE (symbol_id, url)
);

-- Float and dilution, one snapshot per day
CREATE TABLE IF NOT EXISTS share_structure (
    symbol_id          INTEGER NOT NULL REFERENCES symbols(id),
    as_of              DATE NOT NULL,
    float_shares       INTEGER,
    shares_outstanding INTEGER,
    short_float_pct    REAL,
    recent_offering    INTEGER,
    PRIMARY KEY (symbol_id, as_of)
);

-- Chart levels for the trading day
CREATE TABLE IF NOT EXISTS daily_levels (
    symbol_id       INTEGER NOT NULL REFERENCES symbols(id),
    trade_date      DATE NOT NULL,
    prev_close      REAL,
    prev_high       REAL,
    premarket_high  REAL,
    resistance_note TEXT,
    PRIMARY KEY (symbol_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_symbols_company ON symbols (company_id);
CREATE INDEX IF NOT EXISTS idx_scan_results_symbol ON scan_results (symbol_id);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news (symbol_id, published_at);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open the database; commit on success, roll back on error, and always close."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def get_company_id(conn: sqlite3.Connection, cik: int, name: str | None) -> int:
    conn.execute(
        """
        INSERT INTO companies (cik, name) VALUES (?, ?)
        ON CONFLICT (cik) DO UPDATE SET name = COALESCE(excluded.name, companies.name)
        """,
        (cik, name),
    )
    row = conn.execute("SELECT id FROM companies WHERE cik = ?", (cik,)).fetchone()
    return int(row["id"])


def get_symbol_id(
    conn: sqlite3.Connection, symbol: str, exchange: str, company_id: int | None = None
) -> int:
    """Return the symbols row for this listing, creating it if needed.

    With a company_id, a listing saved before its company was known is linked to
    that company. Without one, the most recent listing of the ticker is reused.
    """
    if company_id is not None:
        row = conn.execute(
            "SELECT id FROM symbols WHERE symbol = ? AND exchange = ? AND company_id = ?",
            (symbol, exchange, company_id),
        ).fetchone()
        if row:
            return int(row["id"])
        row = conn.execute(
            "SELECT id FROM symbols WHERE symbol = ? AND exchange = ? AND company_id IS NULL",
            (symbol, exchange),
        ).fetchone()
        if row:
            conn.execute("UPDATE symbols SET company_id = ? WHERE id = ?", (company_id, row["id"]))
            return int(row["id"])
    else:
        row = conn.execute(
            "SELECT id FROM symbols WHERE symbol = ? AND exchange = ? ORDER BY id DESC LIMIT 1",
            (symbol, exchange),
        ).fetchone()
        if row:
            return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO symbols (symbol, exchange, company_id) VALUES (?, ?, ?)",
        (symbol, exchange, company_id),
    )
    return int(cursor.lastrowid or 0)


def add_company_to_symbols(conn: sqlite3.Connection) -> None:
    """Rebuild a symbols table created before companies existed, adding company_id."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(symbols)")}
    if "company_id" in columns:
        return
    conn.commit()
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE symbols_new (
            id         INTEGER PRIMARY KEY,
            symbol     TEXT NOT NULL,
            exchange   TEXT NOT NULL,
            company_id INTEGER REFERENCES companies(id),
            UNIQUE (symbol, exchange, company_id)
        );
        INSERT INTO symbols_new (id, symbol, exchange) SELECT id, symbol, exchange FROM symbols;
        DROP TABLE symbols;
        ALTER TABLE symbols_new RENAME TO symbols;
        CREATE INDEX IF NOT EXISTS idx_symbols_company ON symbols (company_id);
        COMMIT;
        PRAGMA foreign_keys = ON;
        """
    )


def migrate_legacy(conn: sqlite3.Connection) -> None:
    """Move rows from the old one-table JSON layout into the new tables, then drop it."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'screener'"
    ).fetchone()
    if not exists:
        return
    for row in conn.execute("SELECT * FROM screener ORDER BY id").fetchall():
        symbols = json.loads(row["watchlist"])
        # exch was added after the table was created; older rows only have
        # SYMBOL.EXCHANGE in tickers, or no exchange at all.
        if row["exch"]:
            exchanges = json.loads(row["exch"])
        elif row["tickers"]:
            exchanges = [vt_symbol.rpartition(".")[2] for vt_symbol in json.loads(row["tickers"])]
        else:
            exchanges = []
        exchanges += [""] * (len(symbols) - len(exchanges))
        run_id = conn.execute(
            "INSERT INTO scan_runs (run_at, criteria) VALUES (?, ?)",
            (row["time_added"], row["criteria"] or ""),
        ).lastrowid
        for rank, (symbol, exchange) in enumerate(zip(symbols, exchanges[:len(symbols)], strict=True), start=1):
            conn.execute(
                "INSERT INTO scan_results (run_id, symbol_id, rank) VALUES (?, ?, ?)",
                (run_id, get_symbol_id(conn, symbol, exchange), rank),
            )
    conn.execute("DROP TABLE screener")


def init_db() -> None:
    with connect() as conn:
        # An existing symbols table must gain company_id before SCHEMA indexes it.
        has_symbols = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'symbols'"
        ).fetchone()
        if has_symbols:
            add_company_to_symbols(conn)
        conn.executescript(SCHEMA)
        migrate_legacy(conn)


def latest_vt_symbols() -> list[str]:
    """vnpy vt_symbols (SYMBOL.EXCHANGE) from the most recent run, in rank order."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.symbol || '.' || s.exchange AS vt_symbol
            FROM scan_results r
            JOIN symbols s ON s.id = r.symbol_id
            WHERE r.run_id = (SELECT MAX(id) FROM scan_runs)
            ORDER BY r.rank
            """
        ).fetchall()
    return [row["vt_symbol"] for row in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
