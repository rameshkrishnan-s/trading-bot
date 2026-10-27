import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.backtest import analyze
from app.engine import Engine
from app.market import DemoMarket, MarketError, Option, TransientMarketError, Upstox
from app.store import Store
from app.strategy import select_option


class EngineTests(unittest.TestCase):
    def make_engine(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "test.sqlite3"
        store = Store(path)
        self.addCleanup(store.db.close)
        return Engine(Config(db_path=path, strategy="ema", starting_capital=1_000_000,
                             max_premium=30_000, max_loss_per_trade=7500, max_daily_loss=20_000), store)

    def test_demo_opens_only_bounded_positions(self):
        engine = self.make_engine()
        for _ in range(200):
            engine.step()
            self.assertLessEqual(len(engine.store.rows("SELECT id FROM positions WHERE status='OPEN'")), 1)
        positions = engine.store.rows("SELECT * FROM positions")
        self.assertGreaterEqual(len(positions), 1)
        self.assertLessEqual(len(positions), engine.config.max_trades_per_day)
        for position in positions:
            self.assertIn(position["option_type"], ("CE", "PE"))
            self.assertLessEqual(position["entry_price"] * position["initial_qty"], engine.config.max_premium + 20)

    def test_kill_switch_exits_paper_position_and_blocks_entries(self):
        engine = self.make_engine()
        for _ in range(120):
            engine.step()
            if engine._open():
                break
        self.assertIsNotNone(engine._open())
        engine.control("halt")
        engine.step()
        self.assertIsNone(engine._open())
        self.assertEqual(engine.store.get("status"), "HALTED")
        self.assertEqual(engine.store.one("SELECT action FROM orders ORDER BY id DESC LIMIT 1")["action"], "SELL")

    def test_daily_loss_lock(self):
        engine = self.make_engine()
        engine.step()
        self.assertIsNone(engine._open())
        engine.store.set("day_start_equity", engine.config.starting_capital + engine.config.max_daily_loss + 1)
        engine.step()
        self.assertEqual(engine.store.get("status"), "HALTED")
        self.assertTrue(engine.store.get("day_locked"))

    def test_daily_loss_lock_resets_only_on_new_session(self):
        engine = self.make_engine()
        engine.step()
        engine.store.set("day_start_equity", engine.config.starting_capital + engine.config.max_daily_loss + 1)
        engine.step()
        self.assertTrue(engine.store.get("auto_resume_next_session"))
        engine.market.minute = 375
        engine.step()
        self.assertEqual(engine.store.get("status"), "RUNNING")
        self.assertFalse(engine.store.get("day_locked"))

    def test_manual_halt_does_not_auto_resume(self):
        engine = self.make_engine()
        engine.control("halt")
        engine.market.minute = 375
        engine.step()
        self.assertEqual(engine.store.get("status"), "HALTED")
        self.assertFalse(engine.store.get("auto_resume_next_session", False))

    def test_liquidity_filter_rejects_bad_spread(self):
        snapshot = DemoMarket().next()
        option = next(o for o in snapshot.options if o.option_type == "CE" and 0.35 <= abs(o.delta) <= 0.65)
        bad = replace(option, bid=100, ask=110)
        snapshot = replace(snapshot, options=[bad])
        chosen, reason = select_option(snapshot, "CALL", Config())
        self.assertIsNone(chosen)
        self.assertIn("No liquid", reason)

    def test_live_config_requires_explicit_gates(self):
        values = {"BOT_MODE": "live", "STRATEGY": "ema", "UPSTOX_ACCESS_TOKEN": "test", "LIVE_TRADING_ENABLED": "true",
                  "BOT_CONTROL_TOKEN": "secret"}
        with patch.dict("os.environ", values, clear=True):
            with self.assertRaisesRegex(ValueError, "static IP"):
                Config.from_env()
        values["LIVE_STATIC_IP_APPROVED"] = "true"
        with patch.dict("os.environ", values, clear=True):
            self.assertEqual(Config.from_env().mode, "live")

    def test_auto_login_config_requires_complete_credentials(self):
        values = {"BOT_MODE": "paper", "UPSTOX_API_KEY": "key", "UPSTOX_PIN": "123456"}
        with patch.dict("os.environ", values, clear=True):
            with self.assertRaisesRegex(ValueError, "all of"):
                Config.from_env()
        values["UPSTOX_TOTP_SECRET"] = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        with patch.dict("os.environ", values, clear=True):
            self.assertEqual(Config.from_env().mode, "paper")

    def test_replay_report_uses_recorded_bid_ask_and_marks_short_sample(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "captured.sqlite3"
        store = Store(path)
        self.addCleanup(store.db.close)
        demo = DemoMarket()
        for _ in range(50):
            snap = demo.next()
            store.add_snapshot(replace(snap, source="Upstox live data"))
        report = analyze(path)
        self.assertEqual(report["trading_days"], 1)
        self.assertFalse(report["minimum_sample_met"])
        self.assertEqual(len(report["scenarios"]), 3)


class BrokerTests(unittest.TestCase):
    def test_held_expiry_remains_in_snapshot_after_entry_cutoff(self):
        broker = Upstox("test-token")
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        held_expiry = (now.date() + timedelta(days=1)).isoformat()
        entry_expiry = (now.date() + timedelta(days=3)).isoformat()
        contracts = [
            {"instrument_key": "NSE_FO|held", "expiry": held_expiry,
             "trading_symbol": "HELD CE", "lot_size": 75, "tick_size": 5},
            {"instrument_key": "NSE_FO|entry", "expiry": entry_expiry,
             "trading_symbol": "ENTRY CE", "lot_size": 75, "tick_size": 5},
        ]
        requested = []

        def fake_get(path, params=None):
            if path == "/v3/market-quote/quotes":
                return {"NSE_INDEX:Nifty 50": {"last_price": 25000,
                        "last_trade_time": int(now.timestamp() * 1000)},
                        "NSE_INDEX:India VIX": {"last_price": 14}}
            if path == "/v2/option/contract":
                return contracts
            if path == "/v2/option/chain":
                requested.append(params["expiry_date"])
                key = "NSE_FO|held" if params["expiry_date"] == held_expiry else "NSE_FO|entry"
                return [{"strike_price": 25000, "call_options": {"instrument_key": key,
                         "market_data": {"bid_price": 100, "ask_price": 101, "ltp": 100.5,
                                         "oi": 100000, "volume": 20000},
                         "option_greeks": {"delta": 0.5}}}]
            self.fail(f"Unexpected path {path}")

        broker._get = fake_get
        snapshot = broker.snapshot(2, held_expiry)
        self.assertEqual(requested, [entry_expiry, held_expiry])
        self.assertEqual({option.key for option in snapshot.options},
                         {"NSE_FO|entry", "NSE_FO|held"})
        self.assertEqual(next(o.expiry for o in snapshot.options if o.key == "NSE_FO|held"), held_expiry)
        requested.clear()
        held_only = broker.snapshot(10, held_expiry)
        self.assertEqual(requested, [held_expiry])
        self.assertEqual([option.key for option in held_only.options], ["NSE_FO|held"])

    def test_held_snapshot_uses_one_quote_request_without_chain(self):
        broker = Upstox("test-token")
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        expiry = (now.date() + timedelta(days=3)).isoformat()
        broker.contracts_day = now.date()
        broker.contracts = {"NSE_FO|53860": {"instrument_key": "NSE_FO|53860", "expiry": expiry,
                                             "trading_symbol": "NIFTY 22950 CE 03 NOV 26", "instrument_type": "CE",
                                             "strike_price": 22950, "lot_size": 65, "tick_size": 5}}
        requests = []

        def fake_get(path, params=None):
            requests.append((path, params))
            self.assertEqual(path, "/v3/market-quote/quotes")
            return {"NSE_INDEX:Nifty 50": {"instrument_token": "NSE_INDEX|Nifty 50", "last_price": 22760,
                                           "last_trade_time": str(int(now.timestamp() * 1000)),
                                           "prev_close_price": 22700},
                    # Option keys use the trading symbol, not the instrument key.
                    "NSE_FO:NIFTY2611322950CE": {
                        "instrument_token": "NSE_FO|53860", "last_price": 127.5, "oi": 250000, "volume": 90000,
                        "depth": {"buy": [{"price": 127.3, "quantity": 650}, {"price": 0, "quantity": 0}],
                                  "sell": [{"price": 0, "quantity": 0}, {"price": 127.65, "quantity": 325},
                                           {"price": 127.7, "quantity": 65}]}}}

        broker._get = fake_get
        snapshot = broker.snapshot(2, expiry, "NSE_FO|53860")
        self.assertEqual(requests, [("/v3/market-quote/quotes",
                                     {"instrument_key": "NSE_INDEX|Nifty 50,NSE_FO|53860"})])
        self.assertEqual(snapshot.spot, 22760)
        self.assertEqual(snapshot.vix, 0)  # Entry-only VIX is not fetched while holding.
        [option] = snapshot.options
        self.assertEqual((option.key, option.option_type, option.strike, option.lot_size, option.tick_size),
                         ("NSE_FO|53860", "CE", 22950, 65, 0.05))
        self.assertEqual((option.bid, option.ask), (127.3, 127.65))  # Empty depth levels are ignored.
        self.assertTrue(Upstox.held_quote_only)
        stale = lambda path, params=None: {**fake_get(path, params), "NSE_INDEX:Nifty 50": {
            "last_price": 22760, "last_trade_time": str(int((now.timestamp() - 300) * 1000))}}
        broker._get = stale
        with self.assertRaisesRegex(MarketError, "stale"):
            broker.snapshot(2, expiry, "NSE_FO|53860")
        broker._get = fake_get
        with self.assertRaisesRegex(MarketError, "absent"):
            broker.snapshot(2, expiry, "NSE_FO|unknown")

    def test_market_data_outages_are_retryable_but_order_requests_are_not(self):
        broker = Upstox("secret-token")
        failures = {"HTTP 429": lambda: urllib.error.HTTPError("url", 429, "secret-token", {}, io.BytesIO()),
                    "HTTP 503": lambda: urllib.error.HTTPError("url", 503, "secret-token", {}, io.BytesIO()),
                    "connection": lambda: urllib.error.URLError("secret-token unreachable"),
                    "timeout": lambda: TimeoutError("timed out"),
                    "reset": lambda: ConnectionResetError("reset")}
        for path in ("/v3/market-quote/quotes", "/v2/option/chain", "/v2/option/contract",
                     "/v3/historical-candle/intraday/NSE_INDEX%7CNifty%2050/minutes/1"):
            for name, failure in failures.items():
                with self.subTest(path=path, failure=name):
                    with patch("app.market.urllib.request.urlopen", side_effect=failure()):
                        with self.assertRaises(TransientMarketError) as caught:
                            broker._get(path)
                    if name.startswith("HTTP"):
                        self.assertNotIn("secret", str(caught.exception))
        # Order placement/cancel/status and portfolio outcomes must be reconciled, never retried.
        for method, base, path in (("POST", "https://api-hft.upstox.com", "/v3/order/place"),
                                   ("DELETE", "https://api-hft.upstox.com", "/v3/order/cancel"),
                                   ("GET", "https://api.upstox.com", "/v2/order/details"),
                                   ("GET", "https://api.upstox.com", "/v2/portfolio/short-term-positions")):
            for name, failure in failures.items():
                with self.subTest(path=path, failure=name):
                    with patch("app.market.urllib.request.urlopen", side_effect=failure()):
                        with self.assertRaises(MarketError) as caught:
                            broker._request(method, base, path, body={} if method == "POST" else None)
                    self.assertNotIsInstance(caught.exception, TransientMarketError)
        # A rejected market-data request is a configuration problem, not an outage.
        with patch("app.market.urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("url", 400, "bad", {}, io.BytesIO(b'{"errors": []}'))):
            with self.assertRaises(MarketError) as caught:
                broker._get("/v3/market-quote/quotes")
        self.assertNotIsInstance(caught.exception, TransientMarketError)

    def test_automatic_login_outage_is_retryable_but_rejection_is_not(self):
        broker = Upstox("", api_key="api-key", pin="123456", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        with patch("app.market.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(TransientMarketError):
                broker.refresh_token()
        with patch("app.market.urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("url", 401, "denied", {}, io.BytesIO())):
            with self.assertRaises(MarketError) as caught:
                broker.refresh_token()
        self.assertNotIsInstance(caught.exception, TransientMarketError)

    def test_totp_matches_rfc_6238_vector(self):
        self.assertEqual(Upstox.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59), "287082")

    def test_automatic_login_keeps_token_in_memory(self):
        broker = Upstox("", api_key="api-key", pin="123456",
                        totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        response = io.BytesIO(json.dumps({"status": "success", "data": {"access_token": "fresh-token"}}).encode())
        with patch("app.market.urllib.request.urlopen", return_value=response) as opener:
            broker.refresh_token()
        self.assertEqual(broker.token, "fresh-token")
        self.assertIsNotNone(broker.token_expires_at)
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["pin"], "123456")
        self.assertEqual(len(payload["totp"]), 6)

    def test_live_order_is_protected_limit_ioc_and_reads_fill(self):
        broker = Upstox("test-token")
        option = Option("NSE_FO|123", "NIFTY 25000 CE", "CE", 25000, "2026-10-13", 75,
                        0.05, 100, 101, 100.5, 100000, 25000, 0.49)
        calls = []

        def fake_request(method, base, path, params=None, body=None):
            calls.append((method, path, body))
            if method == "POST":
                return {"order_ids": ["12345"]}
            return {"status": "complete", "filled_quantity": 75, "average_price": 101.10}

        broker._request = fake_request
        with patch("app.market.time.sleep", return_value=None):
            filled, price, order_id = broker.place_ioc(option, "BUY", 75, 101.2, "TESTTAG")
        self.assertEqual((filled, price, order_id), (75, 101.10, "12345"))
        self.assertEqual(calls[0][2]["order_type"], "LIMIT")
        self.assertEqual(calls[0][2]["validity"], "IOC")
        self.assertEqual(calls[0][2]["transaction_type"], "BUY")

    def test_bad_fill_halts_reconciliation(self):
        broker = Upstox("test-token")
        option = Option("NSE_FO|123", "NIFTY 25000 CE", "CE", 25000, "2026-10-13", 75,
                        0.05, 100, 101, 100.5, 100000, 25000, 0.49)
        broker._request = lambda method, base, path, params=None, body=None: (
            {"order_ids": ["12345"]} if method == "POST" else
            {"status": "complete", "filled_quantity": 80, "average_price": 101.10})
        with patch("app.market.time.sleep", return_value=None):
            with self.assertRaisesRegex(MarketError, "invalid fill"):
                broker.place_ioc(option, "BUY", 75, 101.2, "TESTTAG")


if __name__ == "__main__":
    unittest.main()
