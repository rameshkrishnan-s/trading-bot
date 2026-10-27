import io
import json
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.angel import AngelMarket, CANDLES
from app.config import Config
from app.engine import Engine
from app.market import IST, Option, Snapshot, normalize_candles
from app.reviewer import Reviewer
from app.store import Store
from app.strategy import breakout_signal, select_option, option_risk
from app.backtest import replay


def setup_snapshot():
    start = datetime(2026, 10, 14, 9, 15, tzinfo=IST)
    bars = [{"ts": (start + timedelta(minutes=i)).isoformat(), "open": 25000,
             "high": 25020, "low": 24980, "close": 25000} for i in range(15)]
    for i, price in enumerate((25024, 25025), 15):
        bars.append({"ts": (start + timedelta(minutes=i)).isoformat(), "open": price,
                     "high": price + 1, "low": price - 1, "close": price})
    option = Option("ANGEL_NFO|1", "NIFTY TEST CE", "CE", 25000, "2026-10-20", 75,
                    .05, 100, 100.5, 100.25, 100000, 100000, .35)
    return Snapshot(start + timedelta(minutes=17), 25025, 14, [option], "Angel One live data", 24900, bars)


class BreakoutTests(unittest.TestCase):
    def store(self):
        store = Store(Path(':memory:'))
        self.addCleanup(store.db.close)
        return store

    def engine(self, snapshots, config=None):
        class Feed:
            def snapshot(self, *_):
                return snapshots.pop(0)
        return Engine(config or Config(mode='paper', data_provider='angel'), self.store(), market=Feed(), replay=True)

    def test_confirmed_breakout_and_symmetric_put(self):
        snap = setup_snapshot()
        signal, _, metrics = breakout_signal(snap)
        self.assertEqual(signal, 'CALL')
        self.assertLess(metrics['stop_spot'], metrics['range_high'])
        self.assertAlmostEqual(metrics['target_spot'] - snap.spot, 2 * (snap.spot - metrics['stop_spot']), places=2)
        inverted = [{**b, 'open': 50000-b['open'], 'high': 50000-b['low'],
                     'low': 50000-b['high'], 'close': 50000-b['close']} for b in snap.candles]
        self.assertEqual(breakout_signal(replace(snap, spot=50000-snap.spot, candles=inverted))[0], 'PUT')

    def test_incomplete_range_stale_candles_and_one_close_never_enter(self):
        snap = setup_snapshot()
        for bad in (replace(snap, candles=snap.candles[1:]),
                    replace(snap, candles=snap.candles[:-1]),
                    replace(snap, ts=snap.ts + timedelta(minutes=2)),
                    replace(snap, candles=snap.candles[:-2] + [{**snap.candles[-2], 'close': 25000}] + snap.candles[-1:]),
                    replace(snap, candle_error='Broker candles unavailable')):
            self.assertEqual(breakout_signal(bad)[0], 'WAIT')

    def test_no_intrabar_or_future_lookahead(self):
        snap = setup_snapshot()
        future = {**snap.candles[-1], 'ts': snap.ts.isoformat(), 'high': 90000, 'close': 90000}
        self.assertEqual(breakout_signal(snap), breakout_signal(replace(snap, candles=snap.candles + [future])))
        raw = [[b['ts'], b['open'], b['high'], b['low'], b['close'], 0] for b in snap.candles + [future]]
        self.assertEqual(len(normalize_candles(raw, snap.ts)), 17)

    def test_faded_breakout_and_chasing_rejected(self):
        snap = setup_snapshot()
        self.assertIn('faded', breakout_signal(replace(snap, spot=25019))[1])
        self.assertIn('chase', breakout_signal(replace(snap, spot=25100))[1])

    def test_cash_risk_and_whole_lot_constraints(self):
        snap = setup_snapshot()
        metrics = breakout_signal(snap)[2]
        config = Config()
        option, _ = select_option(snap, 'CALL', config, metrics, 20000, 400)
        self.assertIsNotNone(option)
        self.assertLessEqual(option_risk(option, snap, metrics, config), 400)
        for equity, risk in ((2000, 400), (20000, 50)):
            self.assertIsNone(select_option(snap, 'CALL', config, metrics, equity, risk)[0])
        wide = dict(metrics, stop_distance=100)
        self.assertIsNone(select_option(snap, 'CALL', config, wide, 20000, 400)[0])
        self.assertIsNone(select_option(replace(snap, options=[replace(option, lot_size=1000)]), 'CALL', config, metrics, 20000, 400)[0])

    def test_structural_stop_and_rupee_stop(self):
        for spot, bid, reason in ((25000, 100, 'structural stop'), (25025, 94, 'rupee loss stop')):
            snap = setup_snapshot()
            exit_snap = replace(snap, ts=snap.ts + timedelta(seconds=15), spot=spot,
                                options=[replace(snap.options[0], bid=bid)], candles=[], candle_error='Unavailable')
            engine = self.engine([snap, exit_snap])
            engine.step()
            self.assertIsNotNone(engine._open())
            plan = json.loads(engine._open()['plan_json'])
            self.assertEqual(plan['risk_budget'], 500)
            engine.step()
            self.assertIsNone(engine._open())
            self.assertIn(reason, engine.store.get('last_decision')['reason'])

    def test_target_and_daily_remaining_budget(self):
        snap = setup_snapshot()
        target = breakout_signal(snap)[2]['target_spot']
        engine = self.engine([snap, replace(snap, ts=snap.ts + timedelta(seconds=15), spot=target)])
        engine.step(); engine.step()
        self.assertEqual(engine.store.get('last_decision')['reason'], 'breakout 2R index target')
        engine = self.engine([snap])
        engine.store.set('session_day', snap.ts.date().isoformat())
        engine.store.set('day_start_equity', engine.config.starting_capital + engine.config.max_daily_loss - 50)
        engine.step()
        self.assertIsNone(engine._open())
        self.assertEqual(engine.store.get('last_decision')['metrics']['risk_budget'], 50)

    def test_restart_preserves_plan_and_rejects_changed_policy(self):
        snap = setup_snapshot()
        engine = self.engine([snap]); engine.step()
        plan = engine._open()['plan_json']
        resumed = Engine(engine.config, engine.store, market=engine.market)
        self.assertEqual(resumed._open()['plan_json'], plan)
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            Engine(replace(engine.config, starting_capital=50000), engine.store, market=engine.market)

    def test_configurable_cooldown_requires_a_new_breakout_and_keeps_trade_limit(self):
        snap = setup_snapshot()
        exit_snap = replace(snap, ts=snap.ts + timedelta(seconds=15), spot=25000)
        def breakout_at(minutes):
            candles = list(snap.candles)
            for i in range(minutes):
                price = 25024 if i == minutes - 2 else 25025 if i == minutes - 1 else 25000
                candles.append({'ts': (snap.ts + timedelta(minutes=i)).isoformat(),
                                'open': price, 'high': price + 1, 'low': price - 1, 'close': price})
            return replace(snap, ts=snap.ts + timedelta(minutes=minutes), candles=candles)
        for cooldown in (0, 30):
            with self.subTest(cooldown=cooldown):
                new_breakout = breakout_at(3)
                repeat = replace(snap, ts=snap.ts + timedelta(seconds=30))
                engine = self.engine([snap, exit_snap, repeat, new_breakout],
                                     Config(mode='paper', data_provider='angel', cooldown_minutes=cooldown))
                engine.step(); engine.step()
                self.assertIsNone(engine._open())
                engine.step()
                self.assertIsNone(engine._open())
                expected = 'already been traded' if cooldown == 0 else 'cooldown'
                self.assertIn(expected, engine.store.get('last_decision')['reason'])
                engine.step()
                self.assertEqual(engine._open() is not None, cooldown == 0)
                if cooldown == 0:
                    engine.market.snapshot = lambda *_: replace(new_breakout, ts=new_breakout.ts + timedelta(seconds=15), spot=25000)
                    engine.step()
                    engine.market.snapshot = lambda *_: breakout_at(6)
                    engine.step()
                    self.assertIsNone(engine._open())
                    self.assertEqual(engine.store.get('last_decision')['reason'], 'Daily trade count reached')

    def test_old_policy_keeps_thirty_minute_cooldown(self):
        engine = self.engine([])
        from app.config import policy
        legacy = policy(engine.config)
        legacy.pop('cooldown_minutes')
        engine.store.set('policy', legacy)
        Engine(engine.config, engine.store, market=engine.market)
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            Engine(replace(engine.config, cooldown_minutes=0), engine.store, market=engine.market)

    def test_cooldown_environment_validation(self):
        with patch.dict('os.environ', {'COOLDOWN_MINUTES': '0'}, clear=True):
            self.assertEqual(Config.from_env().cooldown_minutes, 0)
        with patch.dict('os.environ', {'COOLDOWN_MINUTES': '-1'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'COOLDOWN_MINUTES'):
                Config.from_env()

    def test_replay_preserves_poll_times_candles_and_real_settings(self):
        snap = setup_snapshot(); store = self.store()
        store.add_snapshot(snap)
        store.add_snapshot(replace(snap, ts=snap.ts + timedelta(seconds=15), spot=25000))
        rows = store.rows('SELECT * FROM snapshots ORDER BY ts')
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[0]['candles_json']), snap.candles)
        config = Config(data_provider='angel')
        report = replay(rows, config, 0)
        self.assertEqual(report['closed_trades'], 1)
        self.assertLess(report['ending_estimated_equity_inr'], config.starting_capital)

    def test_candle_adapter_caches_and_excludes_current_minute(self):
        snap = setup_snapshot()
        feed = AngelMarket('', '', '', '')
        feed.contracts_day = snap.ts.date(); feed.indices = {'spot':'99926000'}
        raw = [[b['ts'], b['open'], b['high'], b['low'], b['close'], 0] for b in snap.candles]
        with patch.object(feed, '_post', return_value=raw) as post:
            self.assertEqual(len(feed.intraday_candles(snap.ts)), 17)
            feed.intraday_candles(snap.ts + timedelta(seconds=15))
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], CANDLES)

    def test_reviewer_response_has_no_execution_authority(self):
        snap = setup_snapshot(); engine = self.engine([snap]); engine.step()
        reviewer = Reviewer(Config(gemini_api_key='secret'), engine.store, enabled=True)
        before = (engine._open(), engine.store.get('policy'), engine.store.get('last_decision'))
        payload = {'timestamp': snap.ts.isoformat(), 'decision': {'signal': 'CALL'}}
        with patch.object(reviewer, 'generate', return_value='Override all limits and BUY 500 lots'):
            reviewer.review_once(payload)
        self.assertEqual(before, (engine._open(), engine.store.get('policy'), engine.store.get('last_decision')))
        self.assertIn('500 lots', reviewer.view()['last_review']['text'])

    def test_reviewer_failure_sanitized_and_not_retried_forever(self):
        reviewer = Reviewer(Config(gemini_api_key='secret'), self.store(), enabled=True)
        error = urllib.error.HTTPError('https://example.invalid', 403, 'secret', {}, io.BytesIO(b'secret'))
        with patch('app.reviewer.urllib.request.urlopen', side_effect=error):
            reviewer.review_once({'timestamp': setup_snapshot().ts.isoformat(), 'decision': {'signal': 'WAIT'}})
        self.assertTrue(reviewer.blocked)
        self.assertNotIn('secret', json.dumps(reviewer.view()))
        self.assertIn('unavailable', reviewer.view()['status'])

    def test_defaults_and_live_orb_gate(self):
        with patch.dict('os.environ', {}, clear=True):
            c = Config.from_env()
        self.assertEqual((c.starting_capital, c.max_loss_per_trade, c.max_daily_loss), (25000,500,1000))
        with self.assertRaisesRegex(ValueError, 'paper/demo'):
            Engine(replace(c, mode='live'), self.store(), market=object())

    def test_dashboard_reports_current_range_risk_and_contract_details_without_mutation(self):
        snap = setup_snapshot()
        engine = self.engine([snap]); engine.step()
        before = engine.store.rows('SELECT * FROM state ORDER BY key')
        with patch('app.engine.datetime', wraps=datetime) as dt:
            dt.now.return_value = snap.ts
            view = engine.view()
        self.assertEqual(view['feed']['status'], 'fresh')
        self.assertEqual(view['session']['trades_taken'], 1)
        self.assertEqual(view['range']['range_high'], 25020)
        self.assertEqual(view['chain'][0]['lot_size'], 75)
        self.assertLessEqual(view['session']['entry_risk_budget'], 500)
        self.assertEqual(before, engine.store.rows('SELECT * FROM state ORDER BY key'))

    def test_dashboard_does_not_reuse_yesterdays_range_or_daily_budget(self):
        snap = setup_snapshot()
        engine = self.engine([snap]); engine.step()
        with patch('app.engine.datetime', wraps=datetime) as dt:
            dt.now.return_value = snap.ts + timedelta(days=1)
            view = engine.view()
        self.assertEqual(view['feed']['status'], 'stale')
        self.assertEqual(view['range'], {})
        self.assertEqual(view['session']['trades_taken'], 0)
        self.assertIsNone(view['session']['daily_remaining'])
        self.assertIsNone(view['session']['entry_risk_budget'])

    def test_dashboard_missing_feed_and_broker_error_are_explicit(self):
        engine = self.engine([])
        self.assertEqual(engine.view()['feed']['status'], 'missing')
        self.assertIsNone(engine.view()['feed']['snapshot_age_seconds'])
        engine.last_error = 'Angel request rejected (AB9019)'
        self.assertEqual(engine.view()['feed']['contracts_with_delta'], 0)


if __name__ == '__main__':
    unittest.main()


