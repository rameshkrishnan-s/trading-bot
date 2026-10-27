"""One regression test per fault found in the 27 October 2026 code review."""
import hmac
import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app import prepare_plan
from app.angel import AngelMarket
from app.backtest import analyze
from app.config import Config
from app.engine import Engine
from app.market import IST, MarketError, Option, TransientMarketError, Upstox, check_quote_time
from app.runtime import engine_locks
from app.server import Handler
from app.store import Store
from app.strategy import breakout_signal
from test_breakout import setup_snapshot


class FaultRegressionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def store(self, path=Path(":memory:")):
        store = Store(path)
        self.addCleanup(store.db.close)
        return store

    def test_demo_mode_refuses_a_paper_or_unlabelled_broker_database(self):
        path = self.root / "paper.sqlite3"
        store = self.store(path)
        Engine(Config(mode="paper", data_provider="angel", db_path=path), store, market=Mock())
        store.add_snapshot(setup_snapshot())
        with self.assertRaisesRegex(ValueError, "different mode"):
            Engine(Config(mode="demo", db_path=path), store)
        self.assertEqual((store.one("SELECT COUNT(*) AS n FROM snapshots")["n"], store.get("mode")), (1, "paper"))
        # Databases from older releases may lack the mode record; recorded broker data still protects them.
        store.execute("DELETE FROM state WHERE key='mode'")
        with self.assertRaisesRegex(ValueError, "different mode"):
            Engine(Config(mode="demo", db_path=path), store)
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM snapshots")["n"], 1)
        demo_path = self.root / "demo.sqlite3"
        demo = self.store(demo_path)
        Engine(Config(mode="demo", db_path=demo_path), demo).step()
        Engine(Config(mode="demo", db_path=demo_path), demo)  # A demo database still resets on restart.
        self.assertEqual(demo.one("SELECT COUNT(*) AS n FROM snapshots")["n"], 0)

    def test_late_published_candle_is_refetched_within_the_minute(self):
        snap = setup_snapshot()  # 09:32; the 09:31 candle is the second confirmation close.
        rows = [[c["ts"], c["open"], c["high"], c["low"], c["close"], 0] for c in snap.candles]
        angel = AngelMarket("k", "c", "p", "s")
        angel.indices, angel.contracts_day = {"spot": "1", "vix": "2"}, snap.ts.date()
        for feed, method, late, published in ((angel, "_post", rows[:-1], rows),
                                              (Upstox("token"), "_get", {"candles": rows[:-1]}, {"candles": rows})):
            with self.subTest(feed=type(feed).__name__), patch.object(feed, method, side_effect=[late, published]) as fetch:
                first = feed.intraday_candles(snap.ts + timedelta(seconds=1))
                self.assertEqual(breakout_signal(replace(snap, candles=first))[0], "WAIT")
                later = snap.ts + timedelta(seconds=16)
                self.assertEqual(breakout_signal(replace(snap, ts=later, candles=feed.intraday_candles(later)))[0], "CALL")
                feed.intraday_candles(snap.ts + timedelta(seconds=31))  # Complete now: served from the cache.
                self.assertEqual(fetch.call_count, 2)

    def test_kill_switch_and_dashboard_do_not_wait_for_a_broker_request(self):
        snap = setup_snapshot()
        inside, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowFeed:
            def snapshot(self, *_):
                inside.set()
                release.wait(5)
                return snap

        engine = Engine(Config(mode="paper", data_provider="angel"), self.store(), market=SlowFeed(), replay=True)
        loop = threading.Thread(target=engine.step)
        loop.start()
        self.assertTrue(inside.wait(2))
        started = time.monotonic()
        engine.view()
        engine.control("halt")
        waited = time.monotonic() - started
        release.set()
        loop.join(5)
        self.assertLess(waited, 1)
        # The in-flight cycle re-reads the halt after its fetch, so the pending breakout is not entered.
        self.assertIsNone(engine._open())
        self.assertEqual(engine.store.get("status"), "HALTED")

    def test_replay_reports_a_halted_scenario_instead_of_crashing(self):
        snap = setup_snapshot()
        atm = Option("ANGEL_NFO|A", "NIFTY 25000 CE", "CE", 25000, "2026-10-20", 75, .05, 100, 100.5, 100.25, 100000, 100000, .5)
        otm = Option("ANGEL_NFO|B", "NIFTY 25100 CE", "CE", 25100, "2026-10-20", 75, .05, 60, 60.4, 60.2, 100000, 100000, .38)
        path = self.root / "replay.sqlite3"
        store = self.store(path)
        Engine(Config(mode="paper", data_provider="angel", db_path=path), store, market=Mock())
        store.add_snapshot(replace(snap, options=[atm, otm]))
        # The recorded run held the ATM call, so its next snapshot quotes only spot and that contract.
        store.add_snapshot(replace(snap, ts=snap.ts + timedelta(seconds=15), spot=25026, vix=0.0, options=[atm], candles=[]))
        scenarios = analyze(path)["scenarios"]
        self.assertIsNone(scenarios[0]["halted_by_error"])
        self.assertEqual(scenarios[0]["open_positions_at_end"], 1)
        # With 25 bps more slippage the ATM risk no longer fits, so the scenario buys a strike never quoted again.
        self.assertIn("no valid bid", scenarios[2]["halted_by_error"])
        self.assertEqual(scenarios[2]["snapshots_replayed"], 2)

    def test_stale_quote_retries_in_paper_without_forcing_an_exit(self):
        now = datetime.now(IST)
        with self.assertRaises(TransientMarketError):
            AngelMarket._fresh({"exchFeedTime": (now - timedelta(minutes=5)).strftime("%d-%b-%Y %H:%M:%S")}, now)
        with self.assertRaises(TransientMarketError):
            check_quote_time(int((now - timedelta(minutes=5)).timestamp() * 1000), now)
        # A clock ahead of the exchange or a missing timestamp is not a feed delay: still fatal.
        for failure in (lambda: AngelMarket._fresh({"exchFeedTime": (now + timedelta(minutes=5)).strftime("%d-%b-%Y %H:%M:%S")}, now),
                        lambda: check_quote_time(None, now)):
            with self.assertRaises(MarketError) as caught:
                failure()
            self.assertNotIsInstance(caught.exception, TransientMarketError)
        snap = setup_snapshot()
        market = Mock()
        market.snapshot.side_effect = [snap, TransientMarketError("Angel quote is stale (over 90 seconds)"),
                                       replace(snap, ts=snap.ts + timedelta(seconds=30))]
        engine = Engine(Config(mode="paper", data_provider="angel"), self.store(), market=market, replay=True)
        engine.poll_once()
        self.assertIsNotNone(engine._open())
        engine.poll_once()
        self.assertEqual(engine.store.get("status"), "RUNNING")
        self.assertFalse(engine.store.get("exit_requested", False))
        engine.poll_once()
        self.assertIsNotNone(engine._open())  # No forced "kill switch" sale once quotes recover.
        self.assertEqual(engine.store.get("last_decision")["signal"], "HOLD")

    def test_closed_market_polls_do_not_repeat_decision_rows(self):
        store = self.store()
        engine = Engine(Config(mode="paper", data_provider="angel"), store, market=Mock())
        with patch("app.engine.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 27, 21, 0, tzinfo=IST)
            for _ in range(4):
                engine.poll_once()
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM decisions")["n"], 1)
        # Each new snapshot is still audited, even when the decision text is unchanged.
        snap = replace(setup_snapshot(), candles=[])
        market = Mock()
        market.intraday_candles.return_value = []
        market.snapshot.side_effect = [snap, replace(snap, ts=snap.ts + timedelta(seconds=15))]
        session_store = self.store()
        engine = Engine(Config(mode="paper", data_provider="angel"), session_store, market=market)
        with patch("app.engine.datetime", wraps=datetime) as clock:
            clock.now.return_value = snap.ts
            engine.poll_once()
            engine.poll_once()
        self.assertEqual(len(session_store.rows("SELECT DISTINCT snapshot_ts FROM decisions")), 2)

    def test_live_halt_resumes_only_after_restart_and_broker_reconciliation(self):
        path = self.root / "live.sqlite3"
        store = self.store(path)
        live = Config(mode="live", strategy="ema", data_provider="upstox", live_enabled=True,
                      static_ip_approved=True, control_token="t", db_path=path)
        broker = Mock()
        broker.positions.return_value = []
        engine = Engine(live, store, market=broker)
        engine.control("halt")
        with self.assertRaisesRegex(ValueError, "process restart"):
            engine.control("start")
        broker.positions.return_value = [{"instrument_token": "NSE_FO|1", "quantity": 75}]
        restarted = Engine(live, store, market=broker)
        with self.assertRaisesRegex(ValueError, "unmanaged F&O position"):
            restarted.control("start")
        self.assertEqual((store.get("status"), store.get("broker_uncertain")), ("HALTED", True))
        broker.positions.return_value = []
        restarted.control("start")
        self.assertEqual((store.get("status"), store.get("broker_uncertain")), ("RUNNING", False))
        restarted.control("halt")
        with self.assertRaisesRegex(ValueError, "process restart"):
            restarted.control("start")  # A new halt in this process needs another restart.

    def test_prepare_plan_refuses_while_the_engine_owns_the_database(self):
        config = Config(mode="paper", data_provider="angel", angel_client_id="regression-client",
                        db_path=self.root / "paper.sqlite3")
        with engine_locks(config), patch.object(prepare_plan, "load_env"), \
                patch.object(prepare_plan.Config, "from_env", return_value=config), \
                patch.object(prepare_plan, "Store") as store, patch.object(prepare_plan, "AngelMarket") as market:
            with self.assertRaisesRegex(RuntimeError, "Another process"):
                prepare_plan.main()
        store.assert_not_called()
        market.assert_not_called()

    def test_backtest_opens_the_recorded_database_read_only(self):
        path = self.root / "older.sqlite3"
        store = self.store(path)
        Engine(Config(mode="paper", data_provider="angel", db_path=path), store, market=Mock())
        store.add_snapshot(setup_snapshot())
        store.execute("DROP TABLE history_candles")  # As recorded by an older release.
        before = store.rows("SELECT name FROM sqlite_master ORDER BY name")
        self.assertEqual(len(analyze(path)["scenarios"]), 3)
        self.assertEqual(store.rows("SELECT name FROM sqlite_master ORDER BY name"), before)

    def test_invalid_trade_limit_tick_oi_expiry_and_port_are_rejected(self):
        for name, value in (("MAX_TRADES_PER_DAY", "0"), ("BROKER_ORDER_TICK", "0"), ("BROKER_ORDER_TICK", "nan"),
                            ("MIN_OPTION_OI", "-1"), ("MIN_DAYS_TO_EXPIRY", "-1"), ("BOT_PORT", "0")):
            with self.subTest(name=name, value=value), patch.dict("os.environ", {name: value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config.from_env().max_trades_per_day, 2)

    def test_snapshot_records_every_observed_option_including_the_held_one(self):
        snap = setup_snapshot()
        held, other = snap.options[0], replace(snap.options[0], key="ANGEL_NFO|2", strike=25100)
        store = self.store()
        store.add_snapshot(replace(snap, options=[held, other]))
        store.add_snapshot(replace(snap, ts=snap.ts + timedelta(seconds=3), options=[held]))  # Held-only poll.
        saved = [[o["key"] for o in json.loads(r["options_json"])]
                 for r in store.rows("SELECT options_json FROM snapshots ORDER BY ts")]
        self.assertEqual(saved, [["ANGEL_NFO|1", "ANGEL_NFO|2"], ["ANGEL_NFO|1"]])

    def test_control_token_uses_constant_time_comparison(self):
        engine = Mock()
        engine.control.return_value = {"status": "PAUSED"}

        def post(token):
            handler = object.__new__(Handler)
            handler.request_version, handler.command, handler.close_connection = "HTTP/1.1", "POST", False
            handler.log_request = Mock()
            handler.config, handler.engine, handler.path = Config(control_token="s3cret"), engine, "/api/control"
            body = b'{"action":"pause"}'
            handler.headers = {"Host": "127.0.0.1:8765", "Content-Type": "application/json",
                               "Content-Length": str(len(body)), **({"X-Control-Token": token} if token else {})}
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.do_POST()
            return handler.wfile.getvalue().split(b" ", 2)[1]

        with patch("app.server.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertEqual([post(token) for token in ("s3cret", "wrong", None, "sécret")],
                             [b"200", b"401", b"401", b"401"])  # Non-ASCII is rejected, not a server error.
        self.assertEqual(compare.call_count, 4)
        engine.control.assert_called_once_with("pause")
