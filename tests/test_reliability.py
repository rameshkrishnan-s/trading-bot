import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.angel import AngelMarket, QUOTES, LOGIN
from app.market import Upstox
from app.config import Config, policy
from app.costs import order_costs
from app.engine import Engine
from app.market import TransientMarketError, MarketError
from app.session_report import report
from app.store import Store
from test_breakout import setup_snapshot


class ReliabilityTests(unittest.TestCase):
    def engine(self, market=None, config=None, path=Path(':memory:')):
        store = Store(path)
        self.addCleanup(store.db.close)
        engine = Engine(config or Config(mode='paper', data_provider='angel'), store,
                        market=market or Mock())
        return engine

    def clock(self, snap):
        clock = patch('app.engine.datetime', wraps=datetime)
        value = clock.start()
        self.addCleanup(clock.stop)
        value.now.return_value = snap.ts

    def test_feed_recovers_without_clearing_manual_halt(self):
        snap = setup_snapshot()
        self.clock(snap)
        market = Mock()
        market.snapshot.side_effect = [TransientMarketError('Connection failed'), snap]
        market.intraday_candles.return_value = snap.candles
        engine = self.engine(market)
        delay = engine.poll_once()
        self.assertGreaterEqual(delay, 15)
        self.assertEqual(engine.store.get('status'), 'RUNNING')
        self.assertEqual(engine.view()['feed']['status'], 'error')
        self.assertIsNotNone(engine.next_retry_at)
        engine.control('halt')
        engine.poll_once()
        self.assertEqual(market.snapshot.call_count, 1)
        self.assertEqual(engine.store.get('status'), 'HALTED')
        self.assertEqual(engine.view()['feed']['status'], 'error')
        engine.control('start')
        engine.poll_once()
        self.assertEqual(engine.last_error, '')
        self.assertIsNone(engine.next_retry_at)
        self.assertIsNotNone(engine.latest)

    def test_upstox_paper_engine_retries_connection_outage_instead_of_halting(self):
        snap = setup_snapshot(); self.clock(snap)
        config = Config(mode='paper', data_provider='upstox', upstox_token='token')
        engine = self.engine(Upstox('token'), config)
        with patch('app.market.urllib.request.urlopen', side_effect=TimeoutError('timed out')), \
                patch('app.engine.random.uniform', return_value=0):
            self.assertEqual(engine.poll_once(), 15)
            self.assertEqual(engine.poll_once(), 30)  # Bounded backoff, as with Angel.
        self.assertEqual(engine.store.get('status'), 'RUNNING')
        self.assertIn('Upstox connection failed', engine.last_error)
        self.assertFalse(engine.store.get('exit_requested', False))

    def test_unexpected_error_still_halts(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.side_effect = ValueError('Invalid payload')
        engine = self.engine(market)
        engine.poll_once()
        self.assertEqual(engine.store.get('status'), 'HALTED')
        engine.poll_once()
        self.assertEqual(engine.last_error, 'Invalid payload')

    def test_open_position_retries_remain_prompt_and_exit_after_recovery(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.return_value = snap
        market.intraday_candles.return_value = snap.candles
        engine = self.engine(market)
        engine.poll_once()
        self.assertIsNotNone(engine._open())
        market.snapshot.side_effect = TransientMarketError('Connection failed')
        for _ in range(5):
            self.assertLessEqual(engine.poll_once(), 16.5)
        self.assertEqual(engine.store.get('status'), 'RUNNING')
        market.snapshot.side_effect = None
        market.snapshot.return_value = replace(snap, ts=snap.ts + timedelta(seconds=30), spot=25000)
        engine.poll_once()
        self.assertIsNone(engine._open())
        self.assertIn('structural stop', engine.store.get('last_decision')['reason'])

    def held_exit(self, fast_held):
        """Simulate an option bid falling 0.5/s after entry; return (seconds held, realized P&L, reason)."""
        snap = setup_snapshot()
        sim = {'t': 0.0}
        clock = patch('app.engine.datetime', wraps=datetime)
        value = clock.start(); self.addCleanup(clock.stop)
        value.now.side_effect = lambda *_: snap.ts + timedelta(seconds=sim['t'])
        monotonic = patch('app.engine.time.monotonic', side_effect=lambda: sim['t'])
        monotonic.start(); self.addCleanup(monotonic.stop)

        class Feed:
            held_quote_only = fast_held
            def intraday_candles(self, _now):
                return snap.candles
            def snapshot(self, *_):
                sim['t'] += 1.2  # One broker round trip, as observed in paper sessions.
                held = sim['t'] - 1.2
                option = replace(snap.options[0], bid=round(100 - .5 * held, 2), ask=round(100.5 - .5 * held, 2))
                # Spot stays between the structural stop and target: only the rupee stop can fire.
                return replace(snap, ts=snap.ts + timedelta(seconds=sim['t']), options=[option])

        engine = self.engine(Feed())
        engine.poll_once()
        self.assertIsNotNone(engine._open())
        entered = sim['t']
        while engine._open() and sim['t'] < 120:
            sim['t'] += engine.poll_once()
        position = engine.store.one('SELECT * FROM positions')
        return sim['t'] - entered, float(position['realized']), engine.store.get('last_decision')['reason']

    def test_fast_held_polling_fires_rupee_stop_sooner(self):
        fast_seconds, fast_pnl, reason = self.held_exit(fast_held=True)
        slow_seconds, slow_pnl, _ = self.held_exit(fast_held=False)
        self.assertEqual(reason, 'rupee loss stop')
        self.assertLess(fast_seconds, slow_seconds)
        self.assertGreater(fast_pnl, slow_pnl)
        # Overshoot past the budget is bounded by one ~3-second cycle (0.5/s x 75 qty x 3s).
        budget = Config(mode='paper', data_provider='angel').max_loss_per_trade
        self.assertGreater(fast_pnl, -budget - 0.5 * 75 * 3.5)

    def test_held_cadence_is_a_fixed_period_and_chain_feeds_keep_15_seconds(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.return_value = snap
        market.intraday_candles.return_value = snap.candles
        engine = self.engine(market)
        engine.poll_once()
        self.assertIsNotNone(engine._open())
        self.assertEqual(engine.poll_once(), 15)  # A Mock feed is not a light held-quote feed.
        engine.fast_held = True
        sim = {'t': 0.0}
        def fetch(*_):
            sim['t'] += 1.0  # The broker round trip counts toward the 3-second period.
            return snap
        market.snapshot.side_effect = fetch
        with patch('app.engine.time.monotonic', side_effect=lambda: sim['t']):
            self.assertAlmostEqual(engine.poll_once(), 2.0)

    def test_backoff_is_bounded_and_uncertain_state_is_never_auto_resumed(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.side_effect = TransientMarketError('Connection failed')
        engine = self.engine(market)
        with patch('app.engine.random.uniform', return_value=0):
            self.assertEqual([engine.poll_once() for _ in range(6)], [15,30,60,120,120,120])
        engine.store.set('broker_uncertain', True)
        engine.poll_once()
        self.assertEqual(engine.store.get('status'), 'HALTED')

    def test_saved_candles_survive_failure_restart_and_next_day(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.return_value = snap
        market.intraday_candles.return_value = snap.candles[:5]
        engine = self.engine(market)
        engine.step()
        self.assertEqual(len(engine.latest.candles), 5)
        # Restart uses persistent candles, not adapter memory.
        resumed = Engine(engine.config, engine.store, market=market)
        market.intraday_candles.side_effect = MarketError('Failed candles')
        resumed.step()
        self.assertEqual(len(resumed.latest.candles), 5)
        self.assertEqual(resumed.store.get('last_decision')['signal'], 'WAIT')
        self.assertIn('saved candles retained', resumed.store.get('last_decision')['reason'])
        self.assertEqual(resumed.view()['health']['candles'], 'error')
        market.intraday_candles.side_effect = None
        market.intraday_candles.return_value = snap.candles
        resumed.step()
        self.assertEqual(len(resumed.latest.candles), 17)
        tomorrow = snap.ts + timedelta(days=1)
        self.assertEqual(engine.store.merge_candles(snap.source, [], tomorrow), [])

    def test_candle_merge_does_not_invent_missing_or_accept_future(self):
        engine = self.engine(); snap = setup_snapshot()
        future = {**snap.candles[-1], 'ts': snap.ts.isoformat()}
        rows = engine.store.merge_candles(snap.source, snap.candles[:5] + [future], snap.ts)
        self.assertEqual(len(rows), 5)
        rows = engine.store.merge_candles(snap.source, snap.candles[6:8], snap.ts)
        self.assertEqual(len(rows), 7)
        self.assertNotIn(snap.candles[5]['ts'], [r['ts'] for r in rows])

    def test_endpoint_telemetry_records_market_data_but_never_login(self):
        feed = AngelMarket('secret-key', 'secret-client', 'secret-pin', 'secret-totp')
        store = Store(Path(':memory:')); self.addCleanup(store.db.close)
        feed.observer = store.record_request
        public = {'fetched': [{'ltp': 100, 'exchFeedTime': '15-Oct-2026 09:30:00'}]}
        with patch.object(feed, '_post_once', return_value={'jwtToken': 'secret-token'}):
            feed._post(LOGIN, {})
        with patch.object(feed, '_post_once', return_value=public):
            feed._post(QUOTES, {})
        with patch.object(feed, '_post_once', side_effect=TransientMarketError('Connection failed')):
            with self.assertRaises(TransientMarketError):
                feed._post(QUOTES, {})
        rows = store.rows('SELECT * FROM market_requests')
        self.assertEqual(len(rows), 2)
        self.assertNotIn('secret', json.dumps(rows))
        self.assertEqual(json.loads(rows[0]['data_json']), public)
        self.assertEqual(rows[1]['outcome'], 'retryable')

    def test_network_errors_are_classified_and_do_not_expose_credentials(self):
        feed = AngelMarket('secret-key', 'secret-client', 'secret-pin', '')
        feed.token = 'secret-token'; feed.session_day = datetime.now().date()
        feed.client_headers = {'X-MACAddress': 'test'}
        for error in (urllib.error.URLError('secret-key'),
                      urllib.error.HTTPError('url', 429, 'secret-key', {}, io.BytesIO()),
                      urllib.error.HTTPError('url', 503, 'secret-key', {}, io.BytesIO())):
            with patch('app.angel.urllib.request.urlopen', side_effect=error), patch('app.angel.time.sleep'):
                with self.assertRaises(TransientMarketError) as caught:
                    feed._post(QUOTES, {})
                self.assertNotIn('secret', str(caught.exception))

    def test_itemized_costs_side_rules_and_legacy_compatibility(self):
        config = Config(data_provider='angel', paper_fee_model='angel_nse_2026_10')
        buy = order_costs(100, 100, config, 'BUY')
        sell = order_costs(100, 100, config, 'SELL')
        self.assertEqual(buy['brokerage'], 20)
        self.assertEqual(buy['exchange'], 3.55)
        self.assertEqual(buy['gst'], 4.24)
        self.assertEqual(buy['stamp_duty'], .3)
        self.assertEqual(buy['stt'], 0)
        self.assertEqual(sell['stt'], 15)
        self.assertEqual(sell['stamp_duty'], 0)
        self.assertEqual(buy['total'], 28.1)
        self.assertEqual(sell['total'], 42.8)
        self.assertEqual(order_costs(100, 100, Config(), 'BUY')['total'], 25)
        self.assertEqual(order_costs(100, 0, config)['total'], 0)

    def test_upstox_profile_matches_statutory_rates_and_excludes_sebi_from_gst(self):
        config = Config(data_provider='upstox', paper_fee_model='upstox_nse_2026_10')
        buy = order_costs(100, 100, config, 'BUY')
        sell = order_costs(100, 100, config, 'SELL')
        self.assertEqual((buy['brokerage'], buy['exchange'], buy['stamp_duty'], buy['stt']), (20, 3.55, .3, 0))
        self.assertEqual((sell['stt'], sell['stamp_duty']), (15, 0))
        self.assertEqual(buy['gst'], 4.24)  # 18% of brokerage + exchange + IPFT.
        self.assertEqual(buy['model'], 'upstox_nse_2026_10')
        # 27 Oct paper trade size: Upstox and Angel differ only by GST on the SEBI fee (under a paisa here).
        angel = Config(data_provider='angel', paper_fee_model='angel_nse_2026_10')
        self.assertAlmostEqual(order_costs(127.71, 65, config)['total'], order_costs(127.71, 65, angel)['total'], delta=.011)
        for provider, model in (('angel', 'upstox_nse_2026_10'), ('upstox', 'angel_nse_2026_10')):
            with self.assertRaisesRegex(ValueError, 'must match'):
                self._from_env(provider, model)
        self.assertEqual(self._from_env('upstox', 'upstox_nse_2026_10').paper_fee_model, 'upstox_nse_2026_10')

    def _from_env(self, provider, model):
        values = {'BOT_MODE': 'paper', 'MARKET_DATA_PROVIDER': provider, 'PAPER_FEE_MODEL': model,
                  'UPSTOX_ACCESS_TOKEN': 'token', 'ANGEL_API_KEY': 'k', 'ANGEL_CLIENT_ID': 'c',
                  'ANGEL_PIN': '1234', 'ANGEL_TOTP_SECRET': 's'}
        with patch.dict('os.environ', values, clear=True):
            return Config.from_env()

    def test_214_premium_matches_user_buy_estimates_within_one_paisa(self):
        config = Config(data_provider='angel', paper_fee_model='angel_nse_2026_10')
        # User's approximate premium; assume 65 units per lot and one order per side.
        # Broker rounding and the approximate input can account for a paisa difference.
        for qty, quoted_buy_cost, expected_sell_cost in ((65, 29.87, 50.31),
                                                       (650, 86.26, 290.73)):
            with self.subTest(qty=qty):
                buy = order_costs(214, qty, config, 'BUY')
                sell = order_costs(214, qty, config, 'SELL')
                self.assertAlmostEqual(buy['total'], quoted_buy_cost, delta=0.011)
                self.assertEqual(buy['premium_turnover'], 214 * qty)
                self.assertEqual(buy['brokerage'], 20)
                self.assertEqual(sell['brokerage'], 20)
                self.assertEqual(sell['total'], expected_sell_cost)
                self.assertGreater(sell['total'], buy['total'])

    def test_new_fee_policy_preserves_old_experiment(self):
        engine = self.engine()
        legacy = policy(engine.config); legacy.pop('paper_fee_model'); legacy.pop('cooldown_minutes')
        engine.store.set('policy', legacy)
        Engine(engine.config, engine.store, market=Mock())
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            Engine(replace(engine.config, paper_fee_model='angel_nse_2026_10'), engine.store, market=Mock())

    def test_all_observed_options_and_cost_details_are_recorded(self):
        snap = setup_snapshot(); self.clock(snap)
        market = Mock(); market.snapshot.return_value = snap
        market.intraday_candles.return_value = snap.candles
        engine = self.engine(market, Config(mode='paper', data_provider='angel', paper_fee_model='angel_nse_2026_10'))
        engine.step()
        order = engine.store.one('SELECT * FROM orders')
        self.assertEqual(json.loads(order['costs_json'])['model'], 'angel_nse_2026_10')
        self.assertFalse(json.loads(order['execution_json'])['latency_simulated'])
        self.assertEqual(order['fee'], json.loads(order['costs_json'])['total'])
        self.assertEqual(engine.store.one('SELECT count(*) AS n FROM decisions')['n'], 1)
        extra = [replace(snap.options[0], key=str(i)) for i in range(45)]
        engine.store.add_snapshot(replace(snap, options=extra))
        self.assertEqual(len(json.loads(engine.store.one('SELECT options_json FROM snapshots')['options_json'])), 45)

    def test_report_handles_zero_trade_day(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.sqlite3'
            store = Store(path); store.add_snapshot(setup_snapshot()); store.db.close()
            data = report(path)
            self.assertEqual(data['completed_trades'], 0)
            self.assertEqual(data['days_without_entries'], ['2026-10-14'])
