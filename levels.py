"""Fill daily_levels.prev_high and premarket_high from vnpy's datafeed (Massive).

prev_high       -> the previous trading day's regular-session high, from daily
                   bars. Saved only when that bar's close matches the prev_close
                   finviz reported, so the bar is known to be from the right day.
premarket_high  -> the highest price from 04:00 to 09:29 ET, from minute bars.
                   Filled during the pre-market when the datafeed plan includes
                   same-day data (refreshed every REFRESH_INTERVAL); otherwise
                   filled the next day, for review.

At most CALLS_PER_RUN datafeed requests are made per run (Massive's free plan
allows 5 a minute and runs are 5 minutes apart); work is done in priority order:
today's prev_high by rank, today's premarket_high, then earlier days, newest
first. A level that can't be found within GIVE_UP_AFTER is left empty.
"""
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed
from vnpy.trader.object import BarData, HistoryRequest

import db

MARKET_TZ = ZoneInfo("America/New_York")
PREMARKET_START = time(4, 0)
MARKET_OPEN = time(9, 30)
CALLS_PER_RUN = 4
REFRESH_INTERVAL = timedelta(minutes=15)
PREV_CLOSE_TOLERANCE = 0.01  # 1%
GIVE_UP_AFTER = timedelta(days=5)
STATE_FILE = "datafeed_state.json"


class BudgetExhausted(Exception):
    pass


class NotAuthorized(Exception):
    pass


class FeedError(Exception):
    pass


@dataclass
class Task:
    kind: str  # "prev_high" or "premarket_high"
    symbol_id: int
    symbol: str
    exchange: str
    trade_date: date
    prev_close: float | None


class Feed:
    """The vnpy datafeed with a per-run request budget and error detection."""

    def __init__(self, datafeed: BaseDatafeed, budget: int) -> None:
        self.datafeed = datafeed
        self.remaining = budget
        self.calls = 0
        self.ready = False

    def spend(self) -> None:
        if self.remaining <= 0:
            raise BudgetExhausted
        self.remaining -= 1
        self.calls += 1

    def bars(self, symbol: str, exchange: str, interval: Interval, start: datetime, end: datetime) -> list[BarData]:
        """Bars between two market-time datetimes; bar times are returned in market time."""
        messages: list[str] = []
        if not self.ready:
            self.spend()  # init checks the API key with one request
            if not self.datafeed.init(messages.append):
                raise FeedError("; ".join(messages) or "datafeed init failed")
            self.ready = True
        self.spend()
        try:
            vt_exchange = Exchange(exchange)
        except ValueError:
            vt_exchange = Exchange.SMART  # Massive looks bars up by symbol only
        # The datafeed works in naive system-local time.
        request = HistoryRequest(
            symbol=symbol,
            exchange=vt_exchange,
            start=start.astimezone().replace(tzinfo=None),
            end=end.astimezone().replace(tzinfo=None),
            interval=interval,
        )
        bars = self.datafeed.query_bar_history(request, messages.append)
        if any("NOT_AUTHORIZED" in m for m in messages):
            raise NotAuthorized(messages[-1])
        if messages:
            raise FeedError(messages[-1])
        for bar in bars:
            bar.datetime = bar.datetime.replace(tzinfo=None).astimezone(MARKET_TZ)
        return bars


def market_time(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=MARKET_TZ)


def load_state() -> dict[str, str]:
    path = db.DB_PATH.parent / STATE_FILE
    try:
        state: dict[str, str] = json.loads(path.read_text())
        return state
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, str]) -> None:
    path = db.DB_PATH.parent / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def mark(conn: sqlite3.Connection, symbol_id: int, source: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT INTO source_checks (symbol_id, source, checked_at) VALUES (?, ?, ?)
        ON CONFLICT (symbol_id, source) DO UPDATE SET checked_at = excluded.checked_at
        """,
        (symbol_id, source, now.strftime(db.TIME_FORMAT)),
    )


def pending_tasks(conn: sqlite3.Connection, symbol_ids: list[int], now: datetime, same_day: bool) -> list[Task]:
    """Levels still to fill, in priority order. Markers in source_checks:
    "premarket_high:DATE" = final (may be empty: no pre-market trades),
    "premarket_high" = last live refresh of today's pre-market high.
    Rows older than GIVE_UP_AFTER are no longer tried.
    """
    today = now.astimezone(MARKET_TZ).date()
    rows = conn.execute(
        """
        SELECT d.symbol_id, s.symbol, s.exchange, d.trade_date, d.prev_close, d.prev_high, d.premarket_high
        FROM daily_levels d JOIN symbols s ON s.id = d.symbol_id
        WHERE d.trade_date >= ?
        ORDER BY d.trade_date DESC, d.symbol_id
        """,
        ((today - GIVE_UP_AFTER).isoformat(),),
    ).fetchall()
    checks = {
        (row["symbol_id"], row["source"]): datetime.strptime(row["checked_at"], db.TIME_FORMAT)
        for row in conn.execute("SELECT symbol_id, source, checked_at FROM source_checks WHERE source LIKE '%high%'")
    }
    rank = {symbol_id: i for i, symbol_id in enumerate(symbol_ids)}
    local_now = now.replace(tzinfo=None)

    def task(kind: str, row: sqlite3.Row) -> Task:
        return Task(kind, row["symbol_id"], row["symbol"], row["exchange"],
                    date.fromisoformat(row["trade_date"]), row["prev_close"])

    def finished(kind: str, row: sqlite3.Row) -> bool:
        return (row["symbol_id"], f"{kind}:{row['trade_date']}") in checks

    todays = sorted((r for r in rows if r["trade_date"] == today.isoformat() and r["symbol_id"] in rank),
                    key=lambda r: rank[r["symbol_id"]])
    earlier = [r for r in rows if r["trade_date"] < today.isoformat()]

    tasks = [task("prev_high", r) for r in todays if r["prev_high"] is None]
    if same_day and now.astimezone(MARKET_TZ).time() >= PREMARKET_START:
        stale = [
            r for r in todays
            if not finished("premarket_high", r)
            and local_now - checks.get((r["symbol_id"], "premarket_high"), datetime.min) >= REFRESH_INTERVAL
        ]
        tasks += [task("premarket_high", r) for r in stale]
    tasks += [task("prev_high", r) for r in earlier if r["prev_high"] is None]
    tasks += [task("premarket_high", r) for r in earlier if r["premarket_high"] is None and not finished("premarket_high", r)]
    return tasks


def run_task(feed: Feed, conn: sqlite3.Connection, task: Task, now: datetime) -> bool:
    """Do one task; return True if a level was saved."""
    day = task.trade_date

    if task.kind == "prev_high":
        bars = feed.bars(task.symbol, task.exchange, Interval.DAILY,
                         market_time(day - timedelta(days=14), time(0)), market_time(day - timedelta(days=1), time(23, 59)))
        prior = [b for b in bars if b.datetime.date() < day]
        bar = prior[-1] if prior else None
        matches = bar is not None and (
            task.prev_close is None
            or abs(bar.close_price - task.prev_close) <= PREV_CLOSE_TOLERANCE * task.prev_close
        )
        if bar is not None and matches:
            conn.execute("UPDATE daily_levels SET prev_high = ? WHERE symbol_id = ? AND trade_date = ?",
                         (bar.high_price, task.symbol_id, day.isoformat()))
            return True
        # The daily bar may not be published yet; retried on later runs.
        return False

    bars = feed.bars(task.symbol, task.exchange, Interval.MINUTE,
                     market_time(day, PREMARKET_START), market_time(day, MARKET_OPEN))
    highs = [b.high_price for b in bars if PREMARKET_START <= b.datetime.time() < MARKET_OPEN and b.datetime.date() == day]
    if highs:
        conn.execute("UPDATE daily_levels SET premarket_high = ? WHERE symbol_id = ? AND trade_date = ?",
                     (max(highs), task.symbol_id, day.isoformat()))
    is_today = day == now.astimezone(MARKET_TZ).date()
    if is_today:
        mark(conn, task.symbol_id, "premarket_high", now)
    # Final once the pre-market is over; no bars means no pre-market trades.
    if not is_today or now.astimezone(MARKET_TZ).time() >= MARKET_OPEN:
        mark(conn, task.symbol_id, f"premarket_high:{day.isoformat()}", now)
    return bool(highs)


def update_levels(symbol_ids: list[int], budget: int = CALLS_PER_RUN) -> None:
    """Fill missing levels for today's symbols (in rank order) and earlier days."""
    datafeed = get_datafeed()
    if type(datafeed) is BaseDatafeed:
        print("levels: no datafeed configured (datafeed.name), skipped", file=sys.stderr)
        return

    now = db.now()
    today = now.astimezone(MARKET_TZ).date().isoformat()
    state = load_state()
    same_day = state.get("no_same_day_data") != today
    feed = Feed(datafeed, budget)
    filled: dict[str, int] = {"prev_high": 0, "premarket_high": 0}

    with db.connect() as conn:
        tasks = pending_tasks(conn, symbol_ids, now, same_day)

    attempted = 0
    for task in tasks:
        if task.kind == "premarket_high" and task.trade_date.isoformat() == today and not same_day:
            attempted += 1
            continue
        try:
            with db.connect() as conn:
                filled[task.kind] += run_task(feed, conn, task, now)
            attempted += 1
        except BudgetExhausted:
            break
        except NotAuthorized as error:
            attempted += 1
            if task.trade_date.isoformat() == today:
                same_day = False
                save_state({**state, "no_same_day_data": today})
                print("levels: datafeed plan has no same-day data; "
                      "today's premarket_high will be filled tomorrow", file=sys.stderr)
            else:
                print(f"levels: {task.kind} for {task.symbol} {task.trade_date} not authorized: {error}", file=sys.stderr)
        except FeedError as error:
            # Usually a rate limit or a bad API key; further calls would fail too.
            print(f"levels: {task.kind} for {task.symbol} {task.trade_date} failed: {error}", file=sys.stderr)
            break

    print(f"levels: {feed.calls} datafeed calls, filled prev_high {filled['prev_high']}, "
          f"premarket_high {filled['premarket_high']}, {len(tasks) - attempted} left for later runs", file=sys.stderr)
