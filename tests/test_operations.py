import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

from app.backup import copy_database
from app.config import Config
from app.daily_report import daily
from app.engine import Engine
from app.market import IST
from app.monitor import assess, notify_change, telegram
from app.preflight import check
from app.runtime import engine_locks, file_lock
from app.session_calendar import session
from app.store import Store
from app.session_report import report
from test_breakout import setup_snapshot


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / 'paper.sqlite3')
        self.addCleanup(self.store.db.close)
        self.config = Config(mode='paper', data_provider='angel', db_path=self.root / 'paper.sqlite3')
        self.engine = Engine(self.config, self.store, market=Mock(), replay=True)
        self.snap = setup_snapshot()

    def test_entry_rollback_and_restart_does_not_consume_signal_or_count(self):
        original = self.store.execute
        def fail_order(sql, args=()):
            if sql.startswith('INSERT INTO orders'):
                raise sqlite3.OperationalError('injected disk failure')
            return original(sql, args)
        with patch.object(self.store, 'execute', side_effect=fail_order):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine._enter(self.snap.options[0], self.snap, {'signal_id': 'one'})
        resumed = Engine(self.config, self.store, market=Mock(), replay=True)
        self.assertIsNone(resumed._open())
        self.assertEqual(self.store.rows('SELECT * FROM orders'), [])
        self.assertIsNone(self.store.get('last_breakout_id'))
        resumed._enter(self.snap.options[0], self.snap, {'signal_id': 'one'})
        self.assertEqual(self.store.get('last_breakout_id'), 'one')
        self.assertEqual(len(self.store.rows('SELECT * FROM positions')), 1)

    def test_exit_failure_rolls_back_pnl_position_and_order(self):
        self.engine._enter(self.snap.options[0], self.snap)
        before = self.engine._open()
        with patch.object(self.store, 'event', side_effect=sqlite3.OperationalError('injected failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.engine._exit(before, self.snap.options[0], self.snap, 'test')
        self.assertEqual(self.engine._open(), before)
        self.assertEqual(len(self.store.rows('SELECT * FROM orders')), 1)
        self.engine._exit(before, self.snap.options[0], self.snap, 'test')
        self.assertIsNone(self.engine._open())
        self.assertEqual(len(self.store.rows('SELECT * FROM orders')), 2)

    def test_rollover_failure_does_not_reset_risk_partway(self):
        self.store.set('session_day', '2026-10-13')
        self.store.set('day_locked', True)
        with patch.object(self.store, 'event', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.engine._roll_day(self.snap)
        self.assertEqual(self.store.get('session_day'), '2026-10-13')
        self.assertTrue(self.store.get('day_locked'))

    def test_process_crash_discards_uncommitted_transaction(self):
        self.store.set('survives', True)
        script = '''
import os, sys
from pathlib import Path
from app.store import Store
s=Store(Path(sys.argv[1]))
with s.transaction():
 s.set('survives', False)
 s.set('partial_trade', True)
 os._exit(7)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.config.db_path)], check=False)
        self.assertEqual(result.returncode, 7)
        self.assertTrue(self.store.get('survives'))
        self.assertIsNone(self.store.get('partial_trade'))

    def test_backup_wal_and_restore_preserve_state_and_refuse_overwrite(self):
        self.engine._enter(self.snap.options[0], self.snap, {'signal_id': 'backup'})
        self.store.set('day_locked', True)
        backup = copy_database(self.config.db_path, self.root / 'backup.sqlite3')
        restored = copy_database(backup, self.root / 'restored.sqlite3')
        db = Store(restored)
        try:
            self.assertTrue(db.get('day_locked'))
            self.assertEqual(db.get('last_breakout_id'), 'backup')
            self.assertEqual(len(db.rows('SELECT * FROM orders')), 1)
            self.assertEqual(db.one("SELECT status FROM positions")['status'], 'OPEN')
        finally:
            db.db.close()
        with self.assertRaises(ValueError):
            copy_database(backup, restored)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_account_lock_blocks_different_db_and_port_then_releases(self):
        other = replace(self.config, db_path=self.root/'other.sqlite3', port=8766)
        with engine_locks(self.config):
            with self.assertRaises(RuntimeError):
                with engine_locks(other):
                    self.fail('duplicate account accepted')
        with engine_locks(other):
            pass

    def test_lock_is_enforced_between_processes(self):
        path = self.root/'shared.lock'
        with file_lock(path):
            result = subprocess.run([sys.executable, '-c',
                'from app.runtime import file_lock; import sys\nwith file_lock(sys.argv[1]): pass', str(path)],
                capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'Another process', result.stderr)

    def test_calendar_holidays_special_session_and_unknown_year(self):
        for date in ('2026-10-20', '2026-11-08', '2026-11-10'):
            self.assertFalse(session(datetime.fromisoformat(date+'T10:00:00+05:30'))['open'])
        self.assertTrue(session(datetime(2026,10,15,9,15,tzinfo=IST))['open'])
        self.assertFalse(session(datetime(2026,10,15,9,14,tzinfo=IST))['open'])
        self.assertFalse(session(datetime(2027,1,4,10,tzinfo=IST))['known'])

    def test_monitor_deduplicates_and_does_not_ack_failed_delivery(self):
        state = self.root/'monitor.json'
        send = Mock()
        self.assertFalse(notify_change('healthy', 'ok', state, send))
        notify_change('feed_stale', 'stale', state, send)
        notify_change('feed_stale', 'stale', state, send)
        self.assertEqual(send.call_count, 1)
        with self.assertRaises(ValueError):
            notify_change('exit_overdue', 'exit', state, Mock(side_effect=ValueError('failed')))
        self.assertEqual(json.loads(state.read_text())['key'], 'feed_stale')
        notify_change('healthy', 'recovered', state, send)
        self.assertEqual(send.call_count, 2)

    def test_monitor_closed_market_never_calls_stale_data_fresh(self):
        now = datetime(2026,10,20,10,tzinfo=IST)
        health = {'ok':True,'status':'RUNNING','session':session(now),'feed_fresh':False}
        self.assertEqual(assess(health, now)[0], 'healthy')
        self.assertFalse(health['feed_fresh'])
        health['exit_overdue'] = True
        self.assertEqual(assess(health, now)[0], 'exit_overdue')

    def test_telegram_failure_does_not_expose_token_or_response(self):
        with patch('app.monitor.urllib.request.urlopen', side_effect=OSError('private-token')):
            with self.assertRaisesRegex(ValueError, 'Telegram delivery failed') as error:
                telegram('test', 'private-token', 'private-chat')
        self.assertNotIn('private', str(error.exception))
        with patch('app.monitor.urllib.request.urlopen', return_value=io.BytesIO(b'{"ok":true}')) as request:
            telegram('test', 'token', 'chat')
        self.assertEqual(json.loads(request.call_args.args[0].data)['text'], 'test')

    def test_cloud_preflight_rejects_mixed_fee_history(self):
        result = check(replace(self.config, paper_fee_model='angel_nse_2026_10'), cloud=True)
        self.assertFalse(result['ok'])
        self.assertIn('Database policy differs from configuration', result['errors'])
        self.assertNotIn('angel_api_key', json.dumps(result))

    def test_cloud_preflight_accepts_upstox_paper_with_read_only_token(self):
        config = replace(self.config, data_provider='upstox', paper_fee_model='upstox_nse_2026_10',
                         upstox_token='analytics', control_token='control', reviewer_enabled=False,
                         db_path=self.root / 'upstox.sqlite3')
        result = check(config, cloud=True)
        self.assertEqual(result['errors'], [])
        self.assertNotIn('analytics', json.dumps(result))
        legacy = check(replace(config, paper_fee_model='legacy'), cloud=True)
        self.assertIn('Cloud trial requires the itemized fee profile for upstox', legacy['errors'])
        seeded = check(replace(config, upstox_api_key='key', upstox_pin='123456', upstox_totp_secret='seed'), cloud=True)
        self.assertTrue(any('read-only UPSTOX_ACCESS_TOKEN' in e for e in seeded['errors']))
        self.assertIn('Database data_provider differs from configuration',
                      check(replace(config, db_path=self.config.db_path, paper_fee_model='legacy'))['errors'])

    def test_reports_distinguish_no_observations_and_net_closed_results(self):
        empty = daily(self.config.db_path, self.snap.ts.date().isoformat())
        self.assertEqual(empty['snapshots'], 0)
        self.assertEqual(empty['completed_trades'], 0)
        self.engine._enter(self.snap.options[0], self.snap)
        self.engine._exit(self.engine._open(), self.snap.options[0], self.snap, 'test')
        result = report(self.config.db_path)
        self.assertLess(result['performance']['net_expectancy_inr'], 0)
        self.assertEqual(result['performance']['longest_losing_streak'], 1)
        self.assertEqual(result['performance']['closed_trade_drawdown_inr'], -result['net_closed_pnl_inr'])
        summary = daily(self.config.db_path, self.snap.ts.date().isoformat())
        self.assertEqual(summary['completed_trades'], 1)
        self.assertEqual(summary['net_closed_pnl_inr'], result['net_closed_pnl_inr'])

    def test_loop_health_distinguishes_liveness_from_feed_readiness(self):
        self.engine.thread = Mock()
        self.engine.thread.is_alive.return_value = True
        with patch('app.engine.datetime', wraps=datetime) as clock:
            clock.now.return_value = self.snap.ts
            self.engine.last_cycle_at = self.snap.ts
            health = self.engine.health()
            self.assertTrue(health['ok'])
            self.assertFalse(health['ready'])
            self.engine.latest = self.snap
            self.assertTrue(self.engine.health()['ready'])
            self.engine.thread.is_alive.return_value = False
            self.assertFalse(self.engine.health()['ok'])


if __name__ == '__main__':
    unittest.main()
