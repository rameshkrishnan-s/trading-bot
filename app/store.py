from __future__ import annotations

import json
from contextlib import contextmanager
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._transaction_depth = 0
        with self.lock:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bars (
                  ts TEXT PRIMARY KEY, spot REAL NOT NULL, vix REAL NOT NULL, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity (
                  ts TEXT PRIMARY KEY, value REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                  ts TEXT PRIMARY KEY, spot REAL NOT NULL, vix REAL NOT NULL,
                  previous_close REAL NOT NULL, source TEXT NOT NULL, options_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                  level TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT NOT NULL,
                  closed_at TEXT, instrument_key TEXT NOT NULL, symbol TEXT NOT NULL,
                  option_type TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL,
                  qty INTEGER NOT NULL, initial_qty INTEGER NOT NULL,
                  entry_price REAL NOT NULL, entry_fee REAL NOT NULL,
                  exit_price REAL, exit_fee REAL DEFAULT 0, realized REAL DEFAULT 0,
                  high_bid REAL NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                  position_id INTEGER, action TEXT NOT NULL, symbol TEXT NOT NULL,
                  qty INTEGER NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
                  status TEXT NOT NULL, broker_order_id TEXT, note TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
                CREATE TABLE IF NOT EXISTS session_candles (
                  source TEXT NOT NULL, ts TEXT NOT NULL, open REAL NOT NULL,
                  high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                  PRIMARY KEY(source,ts)
                );
                CREATE TABLE IF NOT EXISTS history_candles (
                  source TEXT NOT NULL, ts TEXT NOT NULL, open REAL NOT NULL,
                  high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                  PRIMARY KEY(source,ts)
                );
                CREATE TABLE IF NOT EXISTS market_requests (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                  endpoint TEXT NOT NULL, duration_ms REAL NOT NULL,
                  outcome TEXT NOT NULL, error TEXT NOT NULL, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
                  snapshot_ts TEXT, decision_json TEXT NOT NULL, policy_json TEXT NOT NULL
                );
            """)
            for table, columns in {"positions": {"plan_json": "TEXT NOT NULL DEFAULT '{}'"},
                                   "snapshots": {"candles_json": "TEXT NOT NULL DEFAULT '[]'",
                                                 "candle_error": "TEXT NOT NULL DEFAULT ''",
                                                 "collection_ms": "REAL"},
                                   "orders": {"costs_json": "TEXT NOT NULL DEFAULT '{}'",
                                              "execution_json": "TEXT NOT NULL DEFAULT '{}'"}}.items():
                existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                for name, declaration in columns.items():
                    if name not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            self._commit()

    def _commit(self):
        if not self._transaction_depth:
            self.db.commit()

    @contextmanager
    def transaction(self):
        """Serialize writers and commit a complete state change, or roll it all back."""
        with self.lock:
            name = f"unit_{self._transaction_depth}"
            self.db.execute(f"SAVEPOINT {name}")
            self._transaction_depth += 1
            try:
                yield
                self.db.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                self.db.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.db.execute(f"RELEASE SAVEPOINT {name}")
                raise
            finally:
                self._transaction_depth -= 1

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.lock:
            self.db.execute("INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
            self._commit()

    def event(self, level: str, title: str, detail: str, ts: str | None = None):
        with self.lock:
            self.db.execute("INSERT INTO events(ts,level,title,detail) VALUES (?,?,?,?)", (ts or now_iso(), level, title, detail))
            self._commit()

    def add_bar(self, ts: str, spot: float, vix: float, source: str):
        minute = ts[:16] + ":00+05:30"
        with self.lock:
            self.db.execute("INSERT INTO bars(ts,spot,vix,source) VALUES (?,?,?,?) ON CONFLICT(ts) DO UPDATE SET spot=excluded.spot,vix=excluded.vix", (minute, spot, vix, source))
            self._commit()

    def add_equity(self, ts: str, value: float):
        with self.lock:
            self.db.execute("INSERT INTO equity(ts,value) VALUES (?,?) ON CONFLICT(ts) DO UPDATE SET value=excluded.value", (ts, round(value, 2)))
            self._commit()

    def add_snapshot(self, snapshot):
        from dataclasses import asdict
        stamp = snapshot.ts.isoformat()
        # Every observed option is kept, so the near-money chain and any held strike are available to replay.
        with self.lock:
            self.db.execute("""INSERT INTO snapshots(ts,spot,vix,previous_close,source,options_json,candles_json,candle_error,collection_ms)
                              VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET
                              spot=excluded.spot,vix=excluded.vix,previous_close=excluded.previous_close,
                              source=excluded.source,options_json=excluded.options_json,
                              candles_json=excluded.candles_json,candle_error=excluded.candle_error,
                              collection_ms=excluded.collection_ms""",
                            (stamp, snapshot.spot, snapshot.vix, snapshot.previous_close,
                             snapshot.source, json.dumps([asdict(o) for o in snapshot.options], separators=(",", ":")),
                             json.dumps(snapshot.candles, separators=(",", ":")), snapshot.candle_error, snapshot.collection_ms))
            self._commit()

    def merge_candles(self, source, candles, now):
        from app.market import normalize_candles
        valid = normalize_candles([[b['ts'], b['open'], b['high'], b['low'], b['close']]
                                   for b in candles], now)
        with self.lock:
            self.db.executemany("""INSERT INTO session_candles VALUES (?,?,?,?,?,?)
                ON CONFLICT(source,ts) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close""",
                [(source, b['ts'], b['open'], b['high'], b['low'], b['close']) for b in valid])
            self._commit()
        return self.rows("SELECT ts,open,high,low,close FROM session_candles WHERE source=? AND ts>=? AND ts<? ORDER BY ts",
                         (source, now.date().isoformat(), now.replace(second=0, microsecond=0).isoformat()))

    def record_request(self, record):
        self.execute("INSERT INTO market_requests(ts,endpoint,duration_ms,outcome,error,data_json) VALUES (?,?,?,?,?,?)",
                     (record['ts'], record['endpoint'], record['duration_ms'], record['outcome'],
                      record['error'], json.dumps(record['data'], separators=(',', ':'))))

    def rows(self, sql: str, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self._commit()
            return cur.lastrowid

    def clear_demo(self):
        with self.transaction():
            for table in ("state", "bars", "equity", "snapshots", "events", "positions", "orders",
                          "session_candles", "history_candles", "market_requests", "decisions"):
                self.db.execute(f"DELETE FROM {table}")
