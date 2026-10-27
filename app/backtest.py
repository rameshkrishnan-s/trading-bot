"""Replay captured real option chains using the production decision engine.

Usage: python3 -m app.backtest data/desk.sqlite3 [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Config, POLICY_FIELDS
from app.engine import Engine
from app.market import Option, Snapshot
from app.store import Store


class ReplayMarket:
    def __init__(self, rows: list[dict], history: list[dict] = ()):
        self.rows = rows
        self.index = 0
        self.history = [[c["ts"], c["open"], c["high"], c["low"], c["close"]] for c in history]

    def prior_session_candles(self, now: datetime, days: int = 10) -> list:
        """Recorded completed candles from strictly earlier dates; never today's or later."""
        first, today = (now - timedelta(days=days)).date().isoformat(), now.date().isoformat()
        return [row for row in self.history if first <= row[0][:10] < today]

    def snapshot(self, _min_days_to_expiry: int, _held_expiry: str | None = None,
                 _held_key: str | None = None) -> Snapshot:
        if self.index >= len(self.rows):
            raise StopIteration
        row = self.rows[self.index]
        self.index += 1
        options = [Option(**o) for o in json.loads(row["options_json"])]
        return Snapshot(datetime.fromisoformat(row["ts"]), float(row["spot"]), float(row["vix"]),
                        options, row["source"], float(row["previous_close"]),
                        json.loads(row.get("candles_json", "[]")), row.get("candle_error", ""), row.get("collection_ms"))


def replay(rows: list[dict], base: Config, slippage_bps: float, history: list[dict] = ()) -> dict:
    store = Store(Path(":memory:"))
    market = ReplayMarket(rows, history)
    config = replace(base, mode="paper", auto_start=True, paper_slippage_bps=base.paper_slippage_bps + slippage_bps,
                     upstox_token="", live_enabled=False, db_path=Path(":memory:"))
    engine = Engine(config, store, market=market, replay=True)
    for _ in rows:
        # Paper-loop supervision: an error halts this scenario (and exits when quoted) instead of
        # aborting the whole report, e.g. a strike that the recorded run never quoted again.
        engine.poll_once()
    halt = store.one("SELECT detail FROM events WHERE title='Engine halted' ORDER BY id LIMIT 1")
    positions = store.rows("SELECT * FROM positions ORDER BY id")
    closed = [p for p in positions if p["status"] == "CLOSED"]
    curve = [float(r["value"]) for r in store.rows("SELECT value FROM equity ORDER BY ts")]
    peak = config.starting_capital
    max_drawdown = 0.0
    for value in curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    wins = sum(1 for p in closed if p["realized"] > 0)
    gross_profit = sum(float(p["realized"]) for p in closed if p["realized"] > 0)
    gross_loss = -sum(float(p["realized"]) for p in closed if p["realized"] < 0)
    fees = store.one("SELECT COALESCE(SUM(fee),0) AS total FROM orders")["total"]
    view = engine.view()
    store.db.close()
    return {
        "extra_slippage_bps_each_side": slippage_bps,
        "closed_trades": len(closed), "open_positions_at_end": sum(p["status"] == "OPEN" for p in positions),
        "win_rate_pct": round(wins / len(closed) * 100, 2) if closed else None,
        "realized_pnl_inr": round(sum(float(p["realized"]) for p in positions), 2),
        "ending_estimated_equity_inr": view["equity"],
        "max_drawdown_pct": round(max_drawdown, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "net_expectancy_per_closed_trade_inr": round(sum(p['realized'] for p in closed) / len(closed), 2) if closed else None,
        "average_win_inr": round(gross_profit / wins, 2) if wins else None,
        "average_loss_inr": round(gross_loss / sum(p['realized'] < 0 for p in closed), 2) if gross_loss else None,
        "estimated_order_charges_inr": round(fees, 2),
        "halted_by_error": halt["detail"] if halt else None,
        "snapshots_replayed": market.index,
    }


def analyze(path: Path, base: Config | None = None, strategy: str | None = None) -> dict:
    if not path.exists():
        raise ValueError(f"Snapshot database not found: {path}")
    # Read-only: a validation report must never migrate or write to the recorded experiment.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = [dict(r) for r in db.execute("SELECT * FROM snapshots ORDER BY ts")] if "snapshots" in tables else []
        stored = db.execute("SELECT value FROM state WHERE key='policy'").fetchone() if "state" in tables else None
        recorded = json.loads(stored[0]) if stored else None
        # Broker-fetched prior sessions plus candles the engine itself saved on earlier days.
        history = [dict(r) for name in ("history_candles", "session_candles") if name in tables
                   for r in db.execute(f"SELECT source,ts,open,high,low,close FROM {name}")]
    finally:
        db.close()
    if not rows:
        raise ValueError("No captured option snapshots found")
    if any(row["source"] not in {"Upstox live data", "Angel One live data"} for row in rows):
        raise ValueError("Only actual broker snapshots can be used for the validation report")
    if len({row["source"] for row in rows}) != 1:
        raise ValueError("Use a single market-data provider per replay database")
    days = sorted({row["ts"][:10] for row in rows})
    base = base or (replace(Config(), **{k: v for k, v in recorded.items() if k in POLICY_FIELDS}) if recorded
                    else Config(strategy="ema", starting_capital=1_000_000, max_premium=30_000,
                                max_loss_per_trade=7500, max_daily_loss=20_000))
    recorded_strategy = base.strategy
    if strategy:
        # What-if: same recorded quotes and risk policy, different entry/exit rules.
        base = replace(base, strategy=strategy)
    history = [c for c in history if c["source"] == rows[0]["source"]]
    scenarios = [replay(rows, base, bps, history) for bps in (0, 10, 25)]
    minutes = len({row["ts"][:16] for row in rows})
    sufficient = len(days) >= 20 and minutes >= 20 * 200
    return {
        "source": f"Recorded {rows[0]['source']} option bid/ask snapshots",
        "first_day": days[0], "last_day": days[-1],
        "strategy": base.strategy, "recorded_strategy": recorded_strategy, "starting_capital": base.starting_capital,
        "cooldown_minutes": base.cooldown_minutes,
        "fee_model": base.paper_fee_model, "execution_latency_simulated": False,
        "trading_days": len(days), "minute_snapshots": minutes, "quote_snapshots": len(rows),
        "base_slippage_bps_each_side": base.paper_slippage_bps,
        "snapshots_without_candles": sum(not json.loads(row.get("candles_json", "[]")) for row in rows),
        "minimum_sample_met": sufficient,
        "scenarios": scenarios,
        "interpretation": "Historical replay is evidence about this sample, not a profit guarantee. "
                          "Forward paper trading and broker sandbox order tests remain required before live use.",
    }


def main():
    parser = argparse.ArgumentParser(description="Replay recorded broker option quotes through the strategy")
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strategy", choices=("orb", "noise", "ema"),
                        help="Replay the recorded quotes under another strategy (what-if comparison)")
    args = parser.parse_args()
    report = analyze(args.database, strategy=args.strategy)
    output = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
        print(f"Wrote {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
