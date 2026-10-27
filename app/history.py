"""Completed prior-session NIFTY one-minute candles for multi-day strategy inputs.

Only regular-session candles strictly before the requested day are kept. Gaps are not
filled and conflicting duplicates are dropped. Failures leave entries waiting; they never
change risk settings or an open position.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, time as wall_time

from app.market import IST, MarketError

# Calendar days per request: about 19 sessions, within the one-month Angel/Upstox 1-minute limit.
HISTORY_DAYS = 28
RETRY = timedelta(minutes=15)


def normalize_history(rows, day) -> list[dict]:
    candles, conflicts = {}, set()
    for row in rows or []:
        try:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo is None:
                continue
            ts = ts.astimezone(IST)
            if ts.date() >= day or ts.second or ts.microsecond or not wall_time(9, 15) <= ts.time() < wall_time(15, 30):
                continue
            o, h, l, c = map(float, row[1:5])
            if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
                continue
            stamp = ts.isoformat()
            candle = {"ts": stamp, "open": o, "high": h, "low": l, "close": c}
            if stamp in candles and candles[stamp] != candle:
                conflicts.add(stamp)
            candles[stamp] = candle
        except (TypeError, ValueError, IndexError):
            continue
    for stamp in conflicts:
        candles.pop(stamp, None)
    return [candles[k] for k in sorted(candles)]


class SessionHistory:
    """Fetches prior sessions once per day while flat; a failed fetch retries after 15 minutes."""

    def __init__(self, store, enabled: bool):
        self.store, self.enabled = store, enabled
        self.last_attempt = None
        self.available = 0
        self.cache = None
        self.status = "Waiting for prior sessions" if enabled else "Not used by this strategy"

    def prepare(self, market, source: str, now: datetime):
        if not self.enabled or self.store.get("history_day") == {"source": source, "day": now.date().isoformat()}:
            return
        # Snapshot time, not wall time, so replay retries follow the recorded session.
        if self.last_attempt is not None and timedelta(0) <= now - self.last_attempt < RETRY:
            return
        self.last_attempt = now
        try:
            candles = normalize_history(market.prior_session_candles(now, HISTORY_DAYS), now.date())
        except Exception as exc:
            detail = str(exc)[:200] if isinstance(exc, MarketError) else type(exc).__name__
            self.status = "Prior sessions unavailable; retry after 15 minutes. Entries wait for history."
            self.store.event("warning", "Prior sessions unavailable", detail, now.isoformat())
            return
        with self.store.transaction():
            self.store.db.executemany("""INSERT INTO history_candles VALUES (?,?,?,?,?,?)
                ON CONFLICT(source,ts) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close""",
                [(source, c["ts"], c["open"], c["high"], c["low"], c["close"]) for c in candles])
            self.store.set("history_day", {"source": source, "day": now.date().isoformat()})
        self.cache = None
        self.status = f"{len({c['ts'][:10] for c in candles})} prior sessions received"

    def sessions(self, source: str, day, count: int = 14) -> list[dict]:
        """Latest `count` sessions before `day` that have a 09:15 open, oldest first."""
        key = (source, day.isoformat(), count)
        if self.cache and self.cache[0] == key:
            return self.cache[1]
        rows = self.store.rows("SELECT ts,open,close FROM history_candles WHERE source=? AND ts>=? AND ts<? ORDER BY ts",
                               (source, (day - timedelta(days=HISTORY_DAYS + 7)).isoformat(), day.isoformat()))
        grouped = {}
        for row in rows:
            grouped.setdefault(row["ts"][:10], {})[row["ts"][11:16]] = row
        result = [{"date": date, "open": minutes["09:15"]["open"],
                   "closes": {minute: row["close"] for minute, row in minutes.items()},
                   "last_close": minutes["15:29"]["close"] if "15:29" in minutes else None}
                  for date, minutes in sorted(grouped.items()) if "09:15" in minutes][-count:]
        self.cache = (key, result)
        self.available = len(result)
        return result

    def view(self):
        return {"enabled": self.enabled, "status": self.status, "sessions": self.available}
