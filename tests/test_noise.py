import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.angel import AngelMarket
from app.backtest import analyze
from app.config import Config
from app.engine import Engine
from app.history import SessionHistory, normalize_history
from app.market import IST, MarketError, Option, Snapshot, Upstox
from app.monitor import assess
from app.session_calendar import previous_trading_day
from app.store import Store
from app.strategy import NOISE_LOOKBACK, noise_exit, noise_sigma, noise_signal

SOURCE = "Angel One live data"
DAY = datetime(2026, 10, 14, tzinfo=IST).date()  # Previous regular session: 2026-10-13.
CE = Option("ANGEL_NFO|1", "NIFTY 25100 CE", "CE", 25100, "2026-10-20", 75, .05, 100, 100.5, 100.25, 100000, 100000, .35)
PE = Option("ANGEL_NFO|2", "NIFTY 24800 PE", "PE", 24800, "2026-10-20", 75, .05, 100, 100.5, 100.25, 100000, 100000, -.35)


def at(hour, minute, second=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST)


def session_rows(day, move):
    """375 candles; every close after the 25,000 open sits `move` (a fraction) away from it."""
    level = 25000 * (1 + move)
    rows = []
    for i in range(375):
        opened = 25000 if i == 0 else level
        rows.append([(at(9, 15, day=day) + timedelta(minutes=i)).isoformat(), opened, max(opened, level),
                     min(opened, level), level])
    return rows


def history_rows(count=NOISE_LOOKBACK, end=DAY):
    """Alternate ±0.4% sessions, so sigma is 0.4% at every minute; the latest closes at 24,900."""
    days, day = [], end
    for _ in range(count):
        day = previous_trading_day(day)
        days.append(day)
    return [row for age, day in enumerate(days) for row in session_rows(day, -.004 if age % 2 == 0 else .004)]


def today_candles(last_close, minutes=30):
    """09:15 onward; the final (09:44 by default) candle closes at `last_close`."""
    candles = []
    for i in range(minutes):
        close = last_close if i == minutes - 1 else 25000
        candles.append({"ts": (at(9, 15) + timedelta(minutes=i)).isoformat(), "open": 25000,
                        "high": max(25000, close), "low": min(25000, close), "close": close})
    return candles


def snapshot(ts, spot, candles, options=(CE, PE)):
    return Snapshot(ts, spot, 14, list(options), SOURCE, 24900, candles)


class NoiseStrategyTests(unittest.TestCase):
    def sessions(self, rows=None, now=at(9, 45, 10)):
        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        history = SessionHistory(store, True)
        market = Mock()
        market.prior_session_candles.return_value = history_rows() if rows is None else rows
        history.prepare(market, SOURCE, now)
        return history.sessions(SOURCE, now.date(), NOISE_LOOKBACK)

    def test_band_follows_source_formula_with_gap_anchors(self):
        history = self.sessions()
        self.assertAlmostEqual(noise_sigma(history, "09:44"), .004)
        signal, reason, metrics = noise_signal(snapshot(at(9, 45, 10), 25050, today_candles(25050)), history)
        self.assertEqual(signal, "WAIT")
        self.assertIn("inside the noise area", reason)
        # Upper uses max(open 25,000, prior close 24,900); lower uses the minimum.
        self.assertEqual((metrics["upper_band"], metrics["lower_band"]), (25100.0, 24800.4))
        self.assertEqual(metrics["previous_close"], 24900)

    def test_call_and_put_only_at_half_hour_checks(self):
        history = self.sessions()
        signal, _, metrics = noise_signal(snapshot(at(9, 45, 10), 25104, today_candles(25103)), history)
        self.assertEqual(signal, "CALL")
        self.assertEqual(metrics["signal_id"], "2026-10-14T09:45:1")
        self.assertEqual(metrics["stop_spot"], 25100.0)
        self.assertEqual(list(metrics["exit_checks"])[0], "10:15")
        self.assertEqual(list(metrics["exit_checks"])[-1], "14:45")
        self.assertEqual(noise_signal(snapshot(at(9, 45, 10), 24795, today_candles(24790)), history)[0], "PUT")
        # Between checks the same breach is not traded.
        signal, reason, _ = noise_signal(snapshot(at(9, 52), 25104, today_candles(25103)), history)
        self.assertEqual((signal, reason), ("WAIT", "Next noise-band check at 10:15 IST"))

    def test_late_candle_is_used_within_grace_and_current_minute_is_ignored(self):
        history = self.sessions()
        signal, reason, _ = noise_signal(snapshot(at(9, 45, 3), 25104, today_candles(25103, 29)), history)
        self.assertEqual(signal, "WAIT")
        self.assertIn("Waiting for the 09:44 candle", reason)
        self.assertEqual(noise_signal(snapshot(at(9, 47), 25104, today_candles(25103)), history)[0], "CALL")
        forming = today_candles(25050) + [{"ts": at(9, 45).isoformat(), "open": 25050, "high": 99999,
                                           "low": 25050, "close": 99999}]
        self.assertEqual(noise_signal(snapshot(at(9, 45, 10), 25104, forming), history)[0], "WAIT")

    def test_missing_history_prior_close_or_open_never_enters(self):
        rows = history_rows()
        cases = {
            "needs 14 prior sessions": self.sessions(history_rows(13)),
            "lacks 2026-10-13": self.sessions([r for r in history_rows(15) if r[0][:10] != "2026-10-13"]),
            "has no 15:29 close": self.sessions([r for r in rows if not r[0].startswith("2026-10-13T15:29")]),
        }
        for expected, history in cases.items():
            with self.subTest(expected):
                signal, reason, _ = noise_signal(snapshot(at(9, 45, 10), 25104, today_candles(25103)), history)
                self.assertEqual(signal, "WAIT")
                self.assertIn(expected, reason)
        history = self.sessions()
        self.assertIn("09:15 opening candle", noise_signal(snapshot(at(9, 45, 10), 25104, today_candles(25103)[1:]), history)[1])
        self.assertIn("faded", noise_signal(snapshot(at(9, 45, 10), 25099, today_candles(25103)), history)[1])

    def test_history_excludes_today_after_close_naive_and_conflicting_rows(self):
        good = history_rows(1)
        bad = [[at(9, 30).isoformat(), 1, 2, 1, 2],  # today
               [at(15, 30, day=DAY - timedelta(days=1)).isoformat(), 1, 2, 1, 2],  # after the regular session
               ["2026-10-13T10:00:00", 1, 2, 1, 2],  # no timezone
               [good[5][0], 25000, 25200, 24900, 25100]]  # conflicts with an existing candle
        clean = normalize_history(good + bad, DAY)
        self.assertEqual(len(clean), 374)
        self.assertNotIn(good[5][0], [c["ts"] for c in clean])

    def test_history_fetch_retries_after_fifteen_minutes_and_once_per_day(self):
        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        history = SessionHistory(store, True)
        market = Mock()
        market.prior_session_candles.side_effect = MarketError("Angel connection failed")
        history.prepare(market, SOURCE, at(9, 15, 5))
        history.prepare(market, SOURCE, at(9, 20))
        self.assertEqual(market.prior_session_candles.call_count, 1)
        self.assertEqual(store.one("SELECT title FROM events ORDER BY id DESC LIMIT 1")["title"], "Prior sessions unavailable")
        market.prior_session_candles.side_effect = None
        market.prior_session_candles.return_value = history_rows()
        history.prepare(market, SOURCE, at(9, 31))
        history.prepare(market, SOURCE, at(11, 0))
        self.assertEqual(market.prior_session_candles.call_count, 2)
        self.assertEqual(len(history.sessions(SOURCE, DAY)), 14)

    def test_band_exit_is_checked_once_per_half_hour(self):
        plan = {"strategy": "noise", "exit_checks": {"10:15": 25100.0, "10:45": 25120.0}}
        self.assertIsNone(noise_exit(plan, 1, at(10, 0), at(9, 59, 57), 25000))  # Between checks.
        self.assertIn("10:15", noise_exit(plan, 1, at(10, 15, 2), at(10, 14, 59), 25090))
        self.assertIsNone(noise_exit(plan, 1, at(10, 15, 5), at(10, 15, 2), 25090))  # Already checked.
        self.assertIsNone(noise_exit(plan, 1, at(10, 15, 2), at(10, 14, 59), 25101))
        # After an outage the latest missed check decides; a restart checks once.
        self.assertIn("10:45", noise_exit(plan, 1, at(10, 50), at(10, 10), 25110))
        self.assertIn("10:45", noise_exit(plan, 1, at(10, 50), None, 25110))
        self.assertIn("10:15", noise_exit(plan, -1, at(10, 15, 2), at(10, 14, 59), 25100))


class NoiseEngineTests(unittest.TestCase):
    def engine(self, snapshots, config=None, rows=None):
        rows = history_rows() if rows is None else rows

        class Feed:
            def snapshot(self, *_):
                return snapshots.pop(0)

            def prior_session_candles(self, now, days=10):
                return [r for r in rows if r[0][:10] < now.date().isoformat()]

        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        return Engine(config or Config(mode="paper", data_provider="angel", strategy="noise"), store,
                      market=Feed(), replay=True)

    def test_enters_at_check_and_band_exits_only_at_next_check(self):
        entry = snapshot(at(9, 45, 10), 25104, today_candles(25103))
        engine = self.engine([entry, replace(entry, ts=at(10, 0), spot=25090, candles=[]),
                              replace(entry, ts=at(10, 15, 2), spot=25090, candles=[])])
        engine.step()
        position = engine._open()
        self.assertEqual(position["option_type"], "CE")
        self.assertEqual(engine.store.one("SELECT note FROM orders")["note"], "Noise-area momentum")
        engine.step()
        self.assertIsNotNone(engine._open())  # Back inside the band, but before the 10:15 check.
        self.assertIn("half-hour noise-band", engine.store.get("last_decision")["reason"])
        engine.step()
        self.assertIsNone(engine._open())
        self.assertEqual(engine.store.get("last_decision")["reason"], "noise band check 10:15: back inside the noise area")
        self.assertEqual(engine.store.get("strategy_version"), "noise-v1")

    def test_rupee_stop_still_works_between_checks(self):
        entry = snapshot(at(9, 45, 10), 25104, today_candles(25103))
        collapsed = replace(CE, bid=90, ask=90.5)
        engine = self.engine([entry, replace(entry, ts=at(10, 0), spot=25101, options=[collapsed], candles=[])])
        engine.step()
        engine.step()
        self.assertIsNone(engine._open())
        self.assertEqual(engine.store.get("last_decision")["reason"], "rupee loss stop")

    def test_extended_entry_is_taken_and_bounded_by_the_rupee_stop(self):
        # As in the source, a check far outside the band still enters; the rupee stop bounds the loss.
        entry = snapshot(at(9, 45, 10), 25160, today_candles(25150))
        engine = self.engine([entry, replace(entry, ts=at(9, 50), spot=25140, options=[replace(CE, bid=93, ask=93.5)],
                                             candles=[])])
        engine.step()
        self.assertEqual(engine.store.get("last_decision")["metrics"]["estimated_stop_loss"], 500)
        self.assertIsNotNone(engine._open())
        engine.step()
        self.assertEqual(engine.store.get("last_decision")["reason"], "rupee loss stop")
        # Fires on the first poll past ₹500; a one-poll jump overshoots it (a threshold, not a guarantee).
        self.assertTrue(-700 < engine.store.one("SELECT realized FROM positions")["realized"] < -500)

    def test_replay_what_if_uses_only_earlier_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "orb.sqlite3"
            store = Store(path)
            Engine(Config(mode="paper", data_provider="angel", db_path=path), store, market=Mock())
            # Today's and tomorrow's saved candles must never become "prior" history.
            poison = [[at(9, 30 + i, day=d).isoformat(), 1, 99999, 1, 99999]
                      for d in (DAY, DAY + timedelta(days=1)) for i in range(3)]
            store.db.executemany("INSERT INTO session_candles VALUES (?,?,?,?,?,?)",
                                 [(SOURCE, *row) for row in history_rows() + poison])
            store.db.commit()
            entry = snapshot(at(9, 45, 10), 25104, today_candles(25103))
            for snap in (entry, replace(entry, ts=at(10, 0), spot=25090, candles=[]),
                         replace(entry, ts=at(10, 15, 2), spot=25090, candles=[])):
                store.add_snapshot(snap)
            store.db.close()
            report = analyze(path, strategy="noise")
        self.assertEqual((report["recorded_strategy"], report["strategy"]), ("orb", "noise"))
        self.assertEqual([s["closed_trades"] for s in report["scenarios"]], [1, 1, 1])

    def test_demo_builds_bands_from_synthetic_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo.sqlite3"
            store = Store(path)
            self.addCleanup(store.db.close)
            engine = Engine(Config(strategy="noise", db_path=path), store)
            for _ in range(60):
                engine.step()
            self.assertEqual(engine.history.available, NOISE_LOOKBACK)
            metrics = store.get("range_context")["metrics"]
            self.assertEqual(metrics["strategy"], "noise")
            self.assertLess(metrics["lower_band"], metrics["upper_band"])
            for _ in range(250):
                engine.step()
                self.assertLessEqual(len(store.rows("SELECT id FROM positions WHERE status='OPEN'")), 1)

    def test_noise_is_paper_or_demo_only_and_needs_its_own_database(self):
        store = Store(Path(":memory:"))
        self.addCleanup(store.db.close)
        with self.assertRaisesRegex(ValueError, "paper/demo validation only"):
            Engine(Config(mode="live", strategy="noise", data_provider="upstox", live_enabled=True,
                          static_ip_approved=True, control_token="t"), store, market=Mock())
        values = {"BOT_MODE": "live", "STRATEGY": "noise", "MARKET_DATA_PROVIDER": "upstox", "UPSTOX_ACCESS_TOKEN": "t",
                  "LIVE_TRADING_ENABLED": "true", "LIVE_STATIC_IP_APPROVED": "true", "BOT_CONTROL_TOKEN": "t"}
        with patch.dict("os.environ", values, clear=True), self.assertRaisesRegex(ValueError, "paper/demo"):
            Config.from_env()
        with patch.dict("os.environ", {"STRATEGY": "noise"}, clear=True):
            self.assertEqual(Config.from_env().strategy, "noise")
        Engine(Config(mode="paper", data_provider="angel"), store, market=Mock())
        with self.assertRaisesRegex(ValueError, "settings changed"):
            Engine(Config(mode="paper", data_provider="angel", strategy="noise"), store, market=Mock())


class NoiseAdapterTests(unittest.TestCase):
    def test_upstox_requests_only_completed_earlier_days(self):
        feed = Upstox("token")
        with patch.object(feed, "_get", return_value={"candles": [["row"]]}) as get:
            self.assertEqual(feed.prior_session_candles(at(9, 20), 28), [["row"]])
        get.assert_called_once_with("/v3/historical-candle/NSE_INDEX%7CNifty%2050/minutes/1/2026-10-13/2026-09-16")

    def test_angel_history_window_is_configurable(self):
        feed = AngelMarket("k", "c", "p", "s")
        feed.indices, feed.contracts_day = {"spot": "1", "vix": "2"}, DAY
        with patch.object(feed, "_post", return_value=[]) as post:
            feed.prior_session_candles(at(9, 20), 28)
        payload = post.call_args[0][1]
        self.assertEqual((payload["fromdate"], payload["todate"]), ("2026-09-16 09:15", "2026-10-13 15:30"))

    def test_monitor_flags_missing_noise_history_while_flat(self):
        health = {"ok": True, "status": "RUNNING", "session": {"known": True, "open": True}, "feed_fresh": True,
                  "open_position": False, "candles_ok": True, "greeks_ok": True, "history_ok": False}
        self.assertEqual(assess(health, at(10, 0))[0], "history_missing")
        self.assertEqual(assess({**health, "history_ok": True}, at(10, 0))[0], "healthy")
