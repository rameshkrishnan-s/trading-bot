"""Historical price context for advisory plans only. Never produces entry signals."""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, time as wall_time

from app.market import IST


def summarize_history(rows, now: datetime):
    if not isinstance(rows, list):
        raise ValueError("Historical response is not a candle list")
    candles, conflicts = {}, set()
    for row in rows:
        try:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo is None:
                continue
            ts = ts.astimezone(IST)
            if not now.date() - timedelta(days=10) <= ts.date() < now.date():
                continue
            if ts.second or ts.microsecond or not wall_time(9, 15) <= ts.time() < wall_time(15, 30):
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
    groups = {}
    for stamp in sorted(candles):
        groups.setdefault(stamp[:10], []).append(candles[stamp])
    sessions = []
    for day, bars in sorted(groups.items())[-5:]:
        high, low = max(b['high'] for b in bars), min(b['low'] for b in bars)
        opening = bars[0]['open'] if bars[0]['ts'][11:16] == '09:15' else None
        closing = bars[-1]['close'] if bars[-1]['ts'][11:16] == '15:29' else None
        sessions.append({"date": day, "open": opening, "high": high, "low": low, "close": closing,
                         "range_points": round(high-low, 2), "candle_count": len(bars),
                         "missing_minutes": 375-len(bars), "complete": len(bars) == 375,
                         "open_to_close_pct": round((closing/opening-1)*100, 3) if closing and opening else None})
    if not sessions:
        raise ValueError("No usable completed prior-session candles")
    latest = sessions[-1]
    age = (now.date() - datetime.fromisoformat(latest['date']).date()).days
    warnings = []
    if not latest['complete']:
        warnings.append("Latest observed session is incomplete; its high/low cover available candles only.")
    if age > 1:
        warnings.append(f"Latest available session is {age} calendar days old. Holidays and missing sessions are not independently verified.")
    warnings.append("Regular-session coverage assumes 09:15–15:30 IST; special sessions may differ.")
    context = {"session_date": now.date().isoformat(), "fetched_at": now.isoformat(),
               "source": "Angel One historical NIFTY candles", "interval": "ONE_MINUTE",
               "latest_session": latest, "sessions": sessions,
               "observed_high": max(s['high'] for s in sessions),
               "observed_low": min(s['low'] for s in sessions),
               "warnings": warnings,
               "limitations": "Price history only. No news, overnight/global markets, futures volume, order flow or reliable direction probabilities. Today is excluded."}
    return context, [candles[k] for k in sorted(candles)]


class MarketContext:
    def __init__(self, store, reviewer, enabled):
        self.store, self.reviewer, self.enabled = store, reviewer, enabled
        self.last_attempt = None
        self.status = "Waiting for history" if enabled else "Historical planning is available in Angel paper mode"
        store.execute("""CREATE TABLE IF NOT EXISTS context_candles (
            ts TEXT PRIMARY KEY, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL)""")

    def prepare(self, market, now):
        if not self.enabled:
            return
        context = self.store.get('market_context')
        if not context or context.get('session_date') != now.date().isoformat():
            if self.last_attempt is not None and time.monotonic() - self.last_attempt < 900:
                return
            self.last_attempt = time.monotonic()
            self.status = "Fetching completed NIFTY sessions"
            try:
                context, candles = summarize_history(market.prior_session_candles(now), now)
                with self.store.lock:
                    self.store.db.executemany(
                        "INSERT INTO context_candles VALUES (:ts,:open,:high,:low,:close) ON CONFLICT(ts) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close", candles)
                    self.store.db.commit()
                self.store.set('market_context', context)
                self.store.event('info', 'Historical context prepared',
                                 f"{len(context['sessions'])} observed sessions; latest {context['latest_session']['date']}; advisory only")
            except Exception:
                self.status = "History unavailable; retry after 15 minutes. Trading rules are unaffected."
                return
        self.status = "History available" if context['latest_session']['complete'] else "History available with missing candles"
        self.reviewer.submit_plan(context, now)

    def view(self):
        return {"enabled": self.enabled, "status": self.status,
                "context": self.store.get('market_context') if self.enabled else None}
