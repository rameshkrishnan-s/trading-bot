from __future__ import annotations

import math
import json
from contextlib import nullcontext
from dataclasses import replace
from functools import wraps
import threading
import time
import random
import uuid
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo

from app.config import Config, policy
from app.costs import order_costs
from app.angel import AngelMarket
from app.market import DemoMarket, MarketError, TransientMarketError, Option, Snapshot, Upstox
from app.store import Store
from app.history import SessionHistory
from app.session_calendar import session
from app.strategy import (CANDLE_STRATEGIES, NOISE_LOOKBACK, entry_window, exit_window, paper_fee, select_option,
                          trend_signal, breakout_signal, noise_signal, option_risk, plan_exit)


IST = ZoneInfo("Asia/Kolkata")


def atomic_paper_change(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        # A database rollback cannot undo an external live order.
        if self.config.mode == "live":
            return method(self, *args, **kwargs)
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return run


class Engine:
    def __init__(self, config: Config, store: Store, market=None, replay: bool = False):
        if config.strategy in CANDLE_STRATEGIES and config.mode == "live":
            raise ValueError("Breakout adaptation is available for paper/demo validation only")
        self.config = config
        self.store = store
        self.lock = threading.RLock()
        if config.mode == "live" and config.data_provider != "upstox":
            raise ValueError("Angel integration supports paper trading only")
        self.market = market if market is not None else (DemoMarket() if config.mode == "demo" else (
            AngelMarket(config.angel_api_key, config.angel_client_id, config.angel_pin, config.angel_totp_secret,
                        refresh_minutes=config.angel_session_refresh_minutes)
            if config.data_provider == "angel" else Upstox(
                config.upstox_token, config.algo_name, config.upstox_api_key, config.upstox_pin, config.upstox_totp_secret)))
        self.replay = replay
        self.latest: Snapshot | None = None
        self.last_error = self.store.get("feed_error", "")
        self.feed_failures = 0
        self.next_retry_at = None
        self.stop_event = threading.Event()
        self.last_cycle_at = datetime.now(IST)
        self.decision_key = None
        if config.mode == "demo":
            # Ownership check before clearing: a paper or live history is never demo scratch data.
            broker_rows = self.store.one("""SELECT (SELECT COUNT(*) FROM snapshots WHERE source != 'Synthetic demo')
                                            + (SELECT COUNT(*) FROM positions WHERE source != 'demo') AS n""")["n"]
            if self.store.get("mode") not in (None, "demo") or broker_rows:
                raise ValueError("Database belongs to a different mode; use a separate BOT_DB_PATH")
            self.store.clear_demo()
            for _ in range(65):
                seed = self.market.next()
                self.store.add_bar(seed.ts.isoformat(), seed.spot, seed.vix, seed.source)
            self.latest = seed
        recorded = self.store.get("policy")
        if recorded:
            # Older databases used a fixed 30-minute cooldown.
            recorded = {"cooldown_minutes": 30, "paper_fee_model": "legacy", **recorded}
        if not replay and recorded and recorded != policy(config):
            raise ValueError("Strategy or capital/risk settings changed; use a separate BOT_DB_PATH to preserve history")
        if not replay and not recorded and config.strategy in CANDLE_STRATEGIES and self.store.one("SELECT COUNT(*) AS n FROM snapshots")["n"]:
            raise ValueError("Legacy observations cannot be treated as ORB candles; use a separate BOT_DB_PATH")
        old_mode = self.store.get("mode")
        if old_mode and old_mode != config.mode:
            raise ValueError("Database belongs to a different mode; use a separate BOT_DB_PATH")
        old_provider = self.store.get("data_provider")
        if old_provider and old_provider != config.data_provider:
            raise ValueError("Database belongs to a different market-data provider; use a separate BOT_DB_PATH")
        self.store.set("mode", config.mode)
        self.store.set("data_provider", config.data_provider)
        self.store.set("policy", policy(config))
        self.store.set("strategy_version", {"orb": "orb-v1", "noise": "noise-v1"}.get(config.strategy, "ema-v1"))
        if self.store.get("status") is None:
            self.store.set("status", "RUNNING" if config.auto_start else "PAUSED")
        # A live halt can only be cleared by a later process, once broker positions reconcile.
        self.inherited_halt = self.store.get("status") == "HALTED"
        self.store.set("last_decision", self.store.get("last_decision", {"signal": "WAIT", "reason": "Waiting for a market snapshot", "metrics": {}}))
        self.store.event("info", "Desk initialized", f"{config.mode.upper()} mode · NIFTY 50 options · one position maximum",
                         self.latest.ts.isoformat() if self.latest else None)
        from app.reviewer import Reviewer
        self.reviewer = Reviewer(config, store, enabled=config.reviewer_enabled and not replay and config.mode == "paper")
        from app.context import MarketContext
        self.context = MarketContext(store, self.reviewer, enabled=config.reviewer_enabled and
                                     config.mode == "paper" and config.data_provider == "angel" and not replay)
        # Strategy input, unlike the advisory context above: prior sessions define the noise area.
        self.history = SessionHistory(store, enabled=config.strategy == "noise")
        if isinstance(self.market, AngelMarket):
            self.market.observer = self.store.record_request
        # Only feeds whose held snapshot is spot plus the held option are polled quickly.
        self.fast_held = getattr(self.market, "held_quote_only", False) is True
        self.thread = threading.Thread(target=self._run, daemon=True, name="options-engine")

    def start(self):
        self.thread.start()

    def shutdown(self):
        self.stop_event.set()
        self.thread.join()  # Keep the account lock until the bounded in-flight request finishes.
        self.reviewer.shutdown()

    def poll_once(self):
        """One supervisor cycle; only explicitly retryable paper-feed errors recover."""
        delay = 2 if self.config.mode == "demo" else 15
        started = time.monotonic()
        try:
            self.step()
            if self.config.mode != "demo" and self.fast_held and self._open():
                # Fixed cycle period: collection time counts toward the interval.
                delay = max(0.0, self.config.held_poll_seconds - (time.monotonic() - started))
        except TransientMarketError as exc:
            if self.config.mode != "paper" or self.store.get("broker_uncertain", False):
                self._halt_error(exc)
            else:
                self.last_error = str(exc)[:300]
                self.store.set("feed_error", self.last_error)
                self.feed_failures += 1
                delay = min(120, 15 * 2 ** min(self.feed_failures - 1, 3))
                if self._open():
                    # Do not extend held-position monitoring gaps.
                    delay = max(5.0, self.config.held_poll_seconds) if self.fast_held else 15
                delay += random.uniform(0, delay * .1)
                self.next_retry_at = (datetime.now(IST) + timedelta(seconds=delay)).isoformat()
                self.store.event("warning", "Feed retry scheduled", self.last_error + f"; retry in {delay:.0f}s")
        except Exception as exc:
            self._halt_error(exc)
        self.last_cycle_at = datetime.now(IST)
        return delay

    @atomic_paper_change
    def _halt_error(self, exc):
        self.last_error = str(exc)[:300]
        self.store.set("feed_error", self.last_error)
        self.store.set("status", "HALTED")
        self.store.set("auto_resume_next_session", False)
        if not self.store.get("broker_uncertain", False):
            self.store.set("exit_requested", True)
        self.store.event("error", "Engine halted", self.last_error)

    def _run(self):
        while not self.stop_event.is_set():
            self.stop_event.wait(self.poll_once())

    def control(self, action: str) -> dict:
        # Flag changes need only the store lock, so pause and the kill switch never wait for a broker
        # request in progress; the loop re-reads them after its fetch. A demo reset replaces the market
        # and a live resume queries the broker, so both wait for the loop.
        loop = self.lock if action == "reset_demo" or (action == "start" and self.config.mode == "live") else nullcontext()
        with loop:
            if action == "start" and self.config.mode == "live" and self.store.get("status") == "HALTED":
                self._resume_live()
            with self.store.transaction():
                return self._apply_control(action)

    def _resume_live(self):
        if not self.inherited_halt:
            raise ValueError("Live halt requires broker reconciliation and process restart")
        try:
            self._live_reconcile(self._open())
        except MarketError as exc:
            raise ValueError(str(exc)) from None
        self.store.set("broker_uncertain", False)
        self.inherited_halt = False

    def _apply_control(self, action: str) -> dict:
        event_time = self.latest.ts.isoformat() if self.config.mode == "demo" and self.latest else None
        if action == "start":
            self.store.set("status", "RUNNING")
            self.store.set("exit_requested", False)
            self.store.set("auto_resume_next_session", False)
            self.store.event("info", "Strategy started", "Monitoring NIFTY 50 for eligible entries", event_time)
        elif action == "pause":
            self.store.set("status", "PAUSED")
            self.store.event("info", "Entries paused", "Open positions remain under risk management", event_time)
        elif action == "halt":
            self.store.set("status", "HALTED")
            self.store.set("exit_requested", True)
            self.store.set("auto_resume_next_session", False)
            self.store.event("warning", "Kill switch engaged", "New entries blocked; exit will be attempted when a fresh quote is available", event_time)
        elif action == "reset_demo" and self.config.mode == "demo":
            self.store.clear_demo()
            self.market = DemoMarket()
            for _ in range(65):
                self.latest = self.market.next()
                self.store.add_bar(self.latest.ts.isoformat(), self.latest.spot, self.latest.vix, self.latest.source)
            self.store.set("mode", "demo")
            self.store.set("status", "RUNNING")
            self.store.set("last_decision", {"signal": "WAIT", "reason": "Demo tape reset", "metrics": {}})
            self.store.event("info", "Demo reset", "Synthetic tape restarted from its opening level", self.latest.ts.isoformat())
        else:
            raise ValueError("Unknown or unavailable action")
        return {"status": self.store.get("status")}

    def _open(self):
        return self.store.one("SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC LIMIT 1")

    def _totals(self, snapshot: Snapshot | None = None) -> tuple[float, float, float]:
        row = self.store.one("SELECT COALESCE(SUM(realized),0) AS realized FROM positions")
        realized = float(row["realized"] if row else 0)
        position = self._open()
        unrealized = 0.0
        if position and snapshot:
            option = next((o for o in snapshot.options if o.key == position["instrument_key"]), None)
            if option:
                fee = paper_fee(option.bid, position["qty"], self.config, "SELL")
                unrealized = (option.bid - position["entry_price"]) * position["qty"] - position["entry_fee"] - fee
        return realized, unrealized, self.config.starting_capital + realized + unrealized

    @atomic_paper_change
    def _roll_day(self, snapshot: Snapshot):
        day = snapshot.ts.date().isoformat()
        if self.store.get("session_day") != day:
            realized, unrealized, equity = self._totals(snapshot)
            self.store.set("session_day", day)
            self.store.set("day_start_equity", equity)
            self.store.set("day_start_realized", realized)
            self.store.set("day_locked", False)
            if self.store.get("auto_resume_next_session", False) and not self.store.get("broker_uncertain", False):
                self.store.set("status", "RUNNING")
                self.store.set("exit_requested", False)
                self.store.set("auto_resume_next_session", False)
                self.store.event("info", "Daily lock cleared", "New session; strategy resumed under a fresh daily risk budget", snapshot.ts.isoformat())
            self.store.event("info", "New session", f"{day} · daily risk budget reset", snapshot.ts.isoformat())

    @atomic_paper_change
    def _lock_day(self):
        self.store.set("status", "HALTED")
        self.store.set("day_locked", True)
        self.store.set("auto_resume_next_session", True)

    def _daily_pnl(self, snapshot: Snapshot) -> float:
        _, _, equity = self._totals(snapshot)
        return equity - float(self.store.get("day_start_equity", self.config.starting_capital))

    def _live_reconcile(self, position=None):
        if self.config.mode != "live":
            return
        broker_positions = self.market.positions()
        fno = [p for p in broker_positions if str(p.get("instrument_token", "")).startswith("NSE_FO|") and int(p.get("quantity") or 0) != 0]
        if position:
            matching = [p for p in fno if p.get("instrument_token") == position["instrument_key"]]
            broker_qty = sum(int(p.get("quantity") or 0) for p in matching)
            if broker_qty != position["qty"] or len(fno) != len(matching):
                self.store.set("broker_uncertain", True)
                raise MarketError("Broker and local F&O positions differ; manual reconciliation required")
        elif fno:
            self.store.set("broker_uncertain", True)
            raise MarketError("Broker account has an unmanaged F&O position; manual reconciliation required")

    def _execute(self, option: Option, side: str, qty: int) -> tuple[int, float, float, str]:
        if self.config.mode != "live":
            slippage = self.config.paper_slippage_bps / 10_000
            price = (option.ask * (1 + slippage)) if side == "BUY" else (option.bid * (1 - slippage))
            price = round(price, 2)
            return qty, price, paper_fee(price, qty, self.config, side), "paper"
        raw = option.ask * 1.002 if side == "BUY" else option.bid * 0.998
        tick = option.tick_size or self.config.order_tick
        price = (math.ceil(raw / tick) if side == "BUY" else math.floor(raw / tick)) * tick
        price = round(price, 2)
        if price <= 0:
            raise MarketError("Invalid limit price")
        if side == "BUY" and price * qty > self.config.max_premium:
            raise MarketError("Protected buy limit exceeds the premium cap")
        tag = "OD" + uuid.uuid4().hex[:14].upper()
        try:
            filled, average, broker_id = self.market.place_ioc(option, side, qty, price, tag)
        except MarketError:
            self.store.set("broker_uncertain", True)
            raise
        return filled, average, paper_fee(average, filled, self.config, side) if filled else 0.0, broker_id

    def _execution_record(self, option, snapshot, side):
        return json.dumps({"snapshot_ts": snapshot.ts.isoformat(), "recorded_at": datetime.now(IST).isoformat(),
                           "bid": option.bid, "ask": option.ask, "side": side,
                           "collection_ms": snapshot.collection_ms,
                           "slippage_bps": self.config.paper_slippage_bps if self.config.mode != "live" else None,
                           "latency_simulated": False, "fill_model": "observed_quote" if self.config.mode != "live" else "broker"})

    @atomic_paper_change
    def _enter(self, option: Option, snapshot: Snapshot, plan: dict | None = None):
        self._live_reconcile()
        filled, price, fee, broker_id = self._execute(option, "BUY", option.lot_size)
        if not filled:
            self.store.event("warning", "Entry unfilled", f"{option.symbol} · limit IOC received no fill", snapshot.ts.isoformat())
            return
        position_id = self.store.execute(
            """INSERT INTO positions(opened_at,instrument_key,symbol,option_type,expiry,strike,qty,initial_qty,
               entry_price,entry_fee,high_bid,status,source,plan_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snapshot.ts.isoformat(), option.key, option.symbol, option.option_type, option.expiry,
             option.strike, filled, filled, price, fee, option.bid, "OPEN", self.config.mode, json.dumps(plan or {})),
        )
        note = ("Noise-area momentum" if plan.get("strategy") == "noise" else "Opening-range breakout") if plan else "Trend entry"
        self.store.execute("INSERT INTO orders(ts,position_id,action,symbol,qty,price,fee,status,broker_order_id,note,costs_json,execution_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (snapshot.ts.isoformat(), position_id, "BUY", option.symbol, filled, price, fee, "FILLED", broker_id, note,
                            json.dumps(order_costs(price, filled, self.config, "BUY")),
                            self._execution_record(option, snapshot, "BUY")))
        if plan and plan.get("signal_id"):
            self.store.set("last_breakout_id", plan["signal_id"])
        self.store.event("trade", "Position opened", f"BUY {filled} {option.symbol} @ ₹{price:.2f}", snapshot.ts.isoformat())

    @atomic_paper_change
    def _exit(self, position: dict, option: Option, snapshot: Snapshot, reason: str):
        self._live_reconcile(position)
        filled, price, fee, broker_id = self._execute(option, "SELL", position["qty"])
        if not filled:
            self.store.event("warning", "Exit unfilled", f"{position['symbol']} · retrying on next fresh quote", snapshot.ts.isoformat())
            return
        entry_fee_alloc = position["entry_fee"] * filled / position["qty"]
        pnl = (price - position["entry_price"]) * filled - entry_fee_alloc - fee
        remaining = position["qty"] - filled
        self.store.execute("""UPDATE positions SET qty=?,entry_fee=?,realized=realized+?,exit_price=?,exit_fee=exit_fee+?,
                             closed_at=?,status=? WHERE id=?""",
                           (remaining, round(position["entry_fee"] - entry_fee_alloc, 2), round(pnl, 2), price, fee,
                            snapshot.ts.isoformat() if remaining == 0 else None, "CLOSED" if remaining == 0 else "OPEN", position["id"]))
        self.store.execute("INSERT INTO orders(ts,position_id,action,symbol,qty,price,fee,status,broker_order_id,note,costs_json,execution_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (snapshot.ts.isoformat(), position["id"], "SELL", position["symbol"], filled, price, fee, "FILLED", broker_id, reason, json.dumps(order_costs(price, filled, self.config, "SELL")),
                            self._execution_record(option, snapshot, "SELL")))
        self.store.event("trade", "Position reduced" if remaining else "Position closed",
                         f"SELL {filled} {position['symbol']} @ ₹{price:.2f} · {reason} · P&L ₹{pnl:+,.0f}", snapshot.ts.isoformat())

    def _set_decision(self, signal: str, reason: str, metrics: dict | None = None, option: Option | None = None):
        if metrics is None and self.latest and self.config.strategy in CANDLE_STRATEGIES:
            context = self.store.get("range_context", {})
            if context.get("day") == self.latest.ts.date().isoformat():
                metrics = context.get("metrics", {})
        new = {"signal": signal, "reason": reason, "metrics": metrics or {},
               "candidate": {"symbol": option.symbol, "ask": option.ask, "spread_pct": round(option.spread_pct, 2),
                             "oi": option.oi, "delta": option.delta, "expiry": option.expiry} if option else None}
        old = self.store.get("last_decision", {})
        if old.get("signal") != signal or old.get("reason") != reason:
            self.store.event("signal", f"Decision: {signal}", reason,
                             self.latest.ts.isoformat() if self.latest else None)
        self.store.set("last_decision", new)
        # One audit row per decision per snapshot: an unchanged decision without new data (for example
        # every closed-market poll) is not written again.
        key = (self.latest.ts.isoformat() if self.latest else None, signal, reason)
        if not self.replay and key != self.decision_key:
            self.decision_key = key
            self.store.execute("INSERT INTO decisions(recorded_at,snapshot_ts,decision_json,policy_json) VALUES (?,?,?,?)",
                               (datetime.now(IST).isoformat(), key[0], json.dumps(new), json.dumps(policy(self.config))))
        if self.latest:
            self.reviewer.submit(self.latest, new)

    def step(self):
        with self.lock:
            status = self.store.get("status", "PAUSED")
            position = self._open()
            now = datetime.now(IST)
            if not position and status == "RUNNING" and session(now)["trading_day"] and clock_time(8) <= now.time() < clock_time(14, 45):
                # History is fetched only while flat, at most once per session (retry failures after 15 min).
                # The model runs on the advisory worker and is never awaited by execution.
                self.context.prepare(self.market, now)
            if self.config.mode == "live" and self.store.get("broker_uncertain", False):
                self._set_decision("BLOCKED", "Broker order or position state is uncertain; manual reconciliation required")
                return
            if status == "PAUSED" and not position:
                return
            if status == "HALTED" and not position and not self.store.get("auto_resume_next_session", False):
                return
            if self.config.mode != "demo" and not self.replay:
                now = datetime.now(IST)
                if not session(now)["open"]:
                    if position and now.time() > clock_time(15, 30) and status != "HALTED":
                        self.store.set("status", "HALTED")
                        self.store.set("exit_requested", True)
                        self.store.event("error", "Position past close", "Broker position remains open after 15:30 IST; reconcile immediately")
                    self._set_decision("WAIT", session(now)["reason"])
                    return
            collection_started = time.monotonic()
            # Half-hour noise-band exits are evaluated on the first snapshot after each check time.
            previous_ts = self.latest.ts if self.latest else None
            snapshot = self.market.next() if self.config.mode == "demo" else self.market.snapshot(
                self.config.min_days_to_expiry, position["expiry"] if position else None,
                position["instrument_key"] if position else None)
            # Persist complete candles independently of a single failed request.
            # Cached candles remain visible, but a failed request blocks new entries.
            if self.config.strategy in CANDLE_STRATEGIES and not self.replay and self.config.mode != "demo" and not position:
                candle_error = ""
                try:
                    candles = self.market.intraday_candles(snapshot.ts)
                except (MarketError, AttributeError):
                    candles = []
                    candle_error = "Broker candles unavailable; saved candles retained, entries blocked"
                candles = self.store.merge_candles(snapshot.source, candles, snapshot.ts)
                snapshot = replace(snapshot, candles=candles, candle_error=candle_error)
            if not self.replay:
                snapshot = replace(snapshot, collection_ms=round((time.monotonic() - collection_started) * 1000, 2))
            if self.last_error:
                self.store.event("info", "Quote feed recovered", "New market snapshot received; candle and entry checks remain separate")
            self.last_error = ""
            self.store.set("feed_error", "")
            self.feed_failures = 0
            self.next_retry_at = None
            self.latest = snapshot
            self.store.add_bar(snapshot.ts.isoformat(), snapshot.spot, snapshot.vix, snapshot.source)
            if not self.replay:
                self.store.add_snapshot(snapshot)
            self._roll_day(snapshot)
            status = self.store.get("status")
            position = self._open()
            assessment = None
            if self.config.strategy in CANDLE_STRATEGIES and not position:
                if self.config.strategy == "noise":
                    # One prior-session request per day, made only while flat so exits never wait on it.
                    self.history.prepare(self.market, snapshot.source, snapshot.ts)
                    assessment = noise_signal(snapshot, self.history.sessions(snapshot.source, snapshot.ts.date(), NOISE_LOOKBACK))
                else:
                    assessment = breakout_signal(snapshot)
                self.store.set("range_context", {"day": snapshot.ts.date().isoformat(), "metrics": assessment[2]})
            if position:
                option = next((o for o in snapshot.options if o.key == position["instrument_key"]), None)
                if not option or option.bid <= 0:
                    raise MarketError("Open position has no valid bid; entries halted")
                high = max(float(position["high_bid"]), option.bid)
                if high > position["high_bid"]:
                    self.store.execute("UPDATE positions SET high_bid=? WHERE id=?", (high, position["id"]))
                drawdown = self._daily_pnl(snapshot)
                stop = max(position["entry_price"] * 0.75, high * 0.80 if high > position["entry_price"] * 1.2 else 0)
                plan = json.loads(position.get("plan_json") or "{}")
                direction = 1 if position["option_type"] == "CE" else -1
                projected_exit = round(option.bid * (1 - self.config.paper_slippage_bps / 10000), 2)
                projected_fee = paper_fee(projected_exit, position["qty"], self.config, "SELL")
                projected_pnl = (projected_exit - position["entry_price"]) * position["qty"] - position["entry_fee"] - projected_fee + position["realized"]
                reason = None
                if self.store.get("exit_requested"):
                    reason = "kill switch"
                elif drawdown <= -self.config.max_daily_loss:
                    self._lock_day()
                    reason = "daily loss limit"
                elif plan and projected_pnl <= -plan["risk_budget"]:
                    reason = "rupee loss stop"
                elif plan:
                    reason = plan_exit(plan, direction, snapshot, previous_ts)
                elif option.bid <= stop:
                    reason = "premium stop"
                elif option.bid >= position["entry_price"] * 1.4:
                    reason = "profit target"
                if not reason and exit_window(snapshot.ts):
                    reason = "intraday exit"
                if reason:
                    self._exit(position, option, snapshot, reason)
                    self._set_decision("EXIT", reason)
                elif plan.get("strategy") == "noise":
                    self._set_decision("HOLD", "Monitoring half-hour noise-band checks, rupee loss stop and session exit", plan)
                else:
                    self._set_decision("HOLD", "Monitoring structural stop, rupee loss stop, target and session exit", plan)
            else:
                realized, unrealized, equity = self._totals(snapshot)
                day_pnl = self._daily_pnl(snapshot)
                if day_pnl <= -self.config.max_daily_loss:
                    self._lock_day()
                    self._set_decision("BLOCKED", "Daily loss limit reached")
                elif status != "RUNNING":
                    self._set_decision("WAIT", "New entries are paused")
                elif snapshot.candle_error:
                    self._set_decision("WAIT", snapshot.candle_error)
                elif not entry_window(snapshot.ts) or (self.config.strategy == "orb" and snapshot.ts.time() < clock_time(9, 30)):
                    self._set_decision("WAIT", "Outside the entry window (noise checks 09:45–14:15 IST)" if self.config.strategy == "noise"
                                       else "Outside the entry window (ORB 09:30–14:45 IST)")
                elif self.store.get("day_locked", False):
                    self._set_decision("BLOCKED", "Daily risk lock is active")
                else:
                    today_orders = self.store.one("SELECT COUNT(*) AS n FROM positions WHERE substr(opened_at,1,10)=?", (snapshot.ts.date().isoformat(),))["n"]
                    if today_orders >= self.config.max_trades_per_day:
                        self._set_decision("WAIT", "Daily trade count reached")
                    else:
                        last = self.store.one("SELECT closed_at FROM positions WHERE closed_at IS NOT NULL ORDER BY id DESC LIMIT 1")
                        if last and (snapshot.ts - datetime.fromisoformat(last["closed_at"])).total_seconds() < self.config.cooldown_minutes * 60:
                            self._set_decision("WAIT", f"{self.config.cooldown_minutes}-minute cooldown after last exit")
                        else:
                            bars = self.store.rows("SELECT spot FROM bars WHERE substr(ts,1,10)=? ORDER BY ts DESC LIMIT 35", (snapshot.ts.date().isoformat(),))
                            spots = [float(r["spot"]) for r in reversed(bars)]
                            signal, reason, metrics = (assessment if self.config.strategy in CANDLE_STRATEGIES
                                                       else trend_signal(spots, snapshot.vix))
                            if signal in {"CALL", "PUT"}:
                                risk_budget = max(0, min(self.config.max_loss_per_trade, equity * .02,
                                                         self.config.max_daily_loss + min(0, day_pnl)))
                                consumed = self.store.get("last_breakout_id") == metrics.get("signal_id") if metrics.get("signal_id") else False
                                option, selection = select_option(snapshot, signal, self.config, metrics, equity, risk_budget)
                                if consumed:
                                    option, selection = None, "This breakout has already been traded"
                                metrics["risk_budget"] = round(risk_budget, 2)
                                if option:
                                    if self.config.strategy in CANDLE_STRATEGIES:
                                        # A noise trade's planned stop is the rupee threshold itself.
                                        metrics["estimated_stop_loss"] = (round(risk_budget, 2) if self.config.strategy == "noise"
                                                                          else option_risk(option, snapshot, metrics, self.config))
                                    self._enter(option, snapshot, metrics if self.config.strategy in CANDLE_STRATEGIES else None)
                                    self._set_decision(signal, reason, metrics, option)
                                else:
                                    self._set_decision("WAIT", selection, metrics)
                            else:
                                self._set_decision(signal, reason, metrics)
            _, _, equity = self._totals(snapshot)
            self.store.add_equity(snapshot.ts.isoformat(), equity)

    def health(self):
        # No engine lock: a slow provider must not hide an unresponsive loop.
        now = datetime.now(IST)
        market_session = session(now)
        age = (now - self.latest.ts).total_seconds() if self.latest else None
        cycle_age = (now - self.last_cycle_at).total_seconds()
        alive = self.thread.is_alive() and cycle_age <= 240
        position = self._open()
        fresh = age is not None and -10 <= age <= 90 and not self.last_error
        status = self.store.get("status")
        history_ok = not self.history.enabled or self.history.available >= NOISE_LOOKBACK
        entry_data = bool(self.latest and self.latest.candles and not self.latest.candle_error
                          and any(.35 <= abs(o.delta) <= .65 for o in self.latest.options)) and history_ok
        ready = alive and status == 'RUNNING' and market_session['open'] and fresh and (bool(position) or entry_data)
        return {"ok": alive, "mode": self.config.mode, "status": status,
                "engine_alive": self.thread.is_alive(), "cycle_age_seconds": round(cycle_age, 1),
                "session": market_session, "feed_fresh": fresh,
                "snapshot_age_seconds": round(age, 1) if age is not None else None,
                "ready": ready, "open_position": bool(position),
                "exit_overdue": bool(position) and (not market_session['open'] or now.time() >= clock_time(15, 16)),
                "candles_ok": bool(self.latest and self.latest.candles and not self.latest.candle_error),
                "greeks_ok": bool(self.latest and any(.35 <= abs(o.delta) <= .65 for o in self.latest.options)),
                "history_ok": history_ok,
                "fee_model": self.config.paper_fee_model}

    def view(self) -> dict:
        # Store lock only: a consistent database read that never waits for a broker request in progress.
        with self.store.lock:
            snapshot = self.latest
            realized, unrealized, equity = self._totals(snapshot)
            position = self._open()
            if position:
                position["plan"] = json.loads(position.get("plan_json") or "{}")
            day_pnl = self._daily_pnl(snapshot) if snapshot else 0
            bars = self.store.rows("SELECT * FROM bars ORDER BY ts DESC LIMIT 110")
            curve = self.store.rows("SELECT * FROM equity ORDER BY ts DESC LIMIT 110")
            recent = self.store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 25")
            orders = self.store.rows("SELECT * FROM orders ORDER BY id DESC LIMIT 30")
            positions = self.store.rows("SELECT * FROM positions ORDER BY id DESC LIMIT 15")
            # Read-only dashboard context; never changes the strategy or execution state.
            now = datetime.now(IST)
            session_time = snapshot.ts if self.config.mode == "demo" and snapshot else now
            day = session_time.date().isoformat()
            day_started = self.store.get("session_day") == day
            trades_today = self.store.one(
                "SELECT COUNT(*) AS n FROM positions WHERE substr(opened_at,1,10)=?", (day,))["n"]
            last_exit = self.store.one("SELECT closed_at FROM positions WHERE closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 1")
            cooldown_until = (datetime.fromisoformat(last_exit["closed_at"]) + timedelta(minutes=self.config.cooldown_minutes)
                              if last_exit and self.config.cooldown_minutes else None)
            context = self.store.get("range_context", {})
            range_metrics = context.get("metrics", {}) if context.get("day") == day else {}
            age = (now - snapshot.ts).total_seconds() if snapshot else None
            feed_status = ("synthetic" if self.config.mode == "demo" else "error" if self.last_error else
                           "missing" if not snapshot else "stale" if age > 90 or age < -10 else "fresh")
            remaining = max(0, self.config.max_daily_loss + min(0, day_pnl)) if day_started else None
            chain = []
            if snapshot:
                near = sorted(snapshot.options, key=lambda o: (abs(o.strike - snapshot.spot), o.strike))[:16]
                chain = [{"symbol": o.symbol, "strike": o.strike, "type": o.option_type,
                          "bid": o.bid, "ask": o.ask, "spread_pct": round(o.spread_pct, 2),
                          "delta": o.delta, "oi": o.oi, "expiry": o.expiry,
                          "lot_size": o.lot_size} for o in sorted(near, key=lambda x: (x.strike, x.option_type))]
            return {
                "mode": self.config.mode, "status": self.store.get("status"),
                "data_provider": self.config.data_provider,
                "strategy": self.config.strategy, "reviewer": self.reviewer.view(),
                "planning": self.context.view(), "history": self.history.view(),
                "session": {"date": day, "time": session_time.isoformat(), "day_started": day_started,
                            "calendar": session(now) if self.config.mode != "demo" else None,
                            "trades_taken": trades_today,
                            "cooldown_until": cooldown_until.isoformat() if cooldown_until and cooldown_until > session_time else None,
                            "daily_remaining": remaining,
                            "entry_risk_budget": max(0, min(self.config.max_loss_per_trade, equity * .02, remaining)) if remaining is not None else None},
                "feed": {"status": feed_status, "snapshot_age_seconds": round(age, 1) if age is not None and self.config.mode != "demo" else None,
                         "contracts_with_delta": sum(.35 <= abs(o.delta) <= .65 for o in snapshot.options) if snapshot else 0},
                "health": {
                    "quotes": feed_status,
                    "candles": "not_checked_while_holding" if position else (
                        "error" if snapshot and snapshot.candle_error else "missing" if not snapshot or not snapshot.candles else
                        "stale" if age is not None and age > 90 else "available"),
                    "candle_error": snapshot.candle_error if snapshot else "",
                    "saved_candles": len(snapshot.candles) if snapshot else 0,
                    "greeks": "not_required_while_holding" if position else "unavailable" if self.last_error else "stale" if age is not None and age > 90 else (
                        "available" if snapshot and any(.35 <= abs(o.delta) <= .65 for o in snapshot.options) else "missing"),
                    "collection_ms": snapshot.collection_ms if snapshot else None,
                    "retry_count": self.feed_failures, "next_retry_at": self.next_retry_at,
                    "execution_latency": "Not simulated; paper fills use the observed bid/ask plus fixed slippage"
                },
                "range": range_metrics,
                "server_time": datetime.now(IST).isoformat(timespec="seconds"),
                "market": {"spot": snapshot.spot, "vix": snapshot.vix, "timestamp": snapshot.ts.isoformat(),
                           "previous_close": snapshot.previous_close, "source": snapshot.source} if snapshot else None,
                "capital": self.config.starting_capital, "equity": round(equity, 2),
                "realized": round(realized, 2), "unrealized": round(unrealized, 2),
                "day_pnl": round(day_pnl, 2), "day_locked": self.store.get("day_locked", False),
                "decision": self.store.get("last_decision"), "position": position,
                "positions": positions, "orders": orders, "events": recent,
                "bars": list(reversed(bars)), "curve": list(reversed(curve)), "chain": chain,
                "cost_model": {"name": self.config.paper_fee_model, "brokerage_per_order": self.config.paper_fee_per_order, "slippage_bps": self.config.paper_slippage_bps, "execution_latency_simulated": False},
                "risk": {"max_premium": self.config.max_premium,
                         "max_loss_per_trade": self.config.max_loss_per_trade,
                         "max_daily_loss": self.config.max_daily_loss,
                         "max_trades_per_day": self.config.max_trades_per_day,
                         "max_spread_pct": self.config.max_spread_pct,
                         "min_days_to_expiry": self.config.min_days_to_expiry,
                         "min_option_oi": self.config.min_option_oi},
                "error": self.last_error,
            }
