import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from app.angel import AngelMarket, CANDLES
from app.config import Config
from app.context import MarketContext, summarize_history
from app.engine import Engine
from app.market import IST
from app.reviewer import Reviewer
from app.store import Store

NOW = datetime(2026, 10, 15, 8, 30, tzinfo=IST)


def bars(day=14, count=375):
    start = NOW.replace(day=day, hour=9, minute=15)
    return [[(start+timedelta(minutes=i)).isoformat(), 25000, 25010, 24990, 25005, 0]
            for i in range(count)]


class ContextTests(unittest.TestCase):
    def store(self):
        store = Store(Path(':memory:'))
        self.addCleanup(store.db.close)
        return store

    def reviewer(self, store):
        with patch('app.reviewer.shutil.which', return_value='/usr/bin/agy'):
            return Reviewer(Config(reviewer_provider='antigravity'), store, enabled=True)

    def test_history_excludes_today_future_and_after_close(self):
        rows=bars()+bars(15)+bars(16)+[[NOW.replace(day=14,hour=15,minute=30).isoformat(),1,99999,1,99999,0]]
        context, clean=summarize_history(rows, NOW)
        self.assertEqual(len(clean),375)
        self.assertTrue(context['latest_session']['complete'])
        self.assertEqual(context['latest_session']['high'],25010)
        self.assertEqual(context['latest_session']['close'],25005)
        self.assertEqual(context['latest_session']['date'],'2026-10-14')

    def test_missing_malformed_and_conflicting_minutes_are_not_synthesized(self):
        rows=bars()
        rows[3][2]=float('nan')
        rows.pop(10)
        rows += [[rows[0][0],25000,25020,24990,25005,0]]
        context, clean=summarize_history(rows,NOW)
        self.assertEqual(len(clean),372)
        self.assertFalse(context['latest_session']['complete'])
        self.assertEqual(context['latest_session']['missing_minutes'],3)
        self.assertIsNone(context['latest_session']['open'])
        self.assertIn('incomplete',context['warnings'][0])

    def test_calendar_gap_is_explicit_and_sessions_are_bounded(self):
        context,_=summarize_history(bars(12),NOW)
        self.assertTrue(any('3 calendar days' in w for w in context['warnings']))
        context,_=summarize_history(sum((bars(d) for d in range(5,15)),[]),NOW)
        self.assertEqual(len(context['sessions']),5)
        with self.assertRaises(ValueError):summarize_history(bars(15),NOW)

    def test_adapter_requests_only_prior_dates(self):
        feed=AngelMarket('','','','')
        feed.indices={'spot':'123'}
        with patch.object(feed,'_load_contracts'),patch.object(feed,'_post',return_value=bars()) as post:
            feed.prior_session_candles(NOW)
        self.assertEqual(post.call_args.args[0],CANDLES)
        self.assertEqual(post.call_args.args[1]['todate'],'2026-10-14 15:30')
        self.assertEqual(post.call_args.args[1]['interval'],'ONE_MINUTE')

    def test_history_is_cached_and_failure_never_changes_trading_state(self):
        store=self.store();reviewer=Mock();market=Mock()
        store.set('status','RUNNING');market.prior_session_candles.return_value=bars()
        context=MarketContext(store,reviewer,True)
        context.prepare(market,NOW);context.prepare(market,NOW)
        market.prior_session_candles.assert_called_once()
        self.assertEqual(store.one('SELECT COUNT(*) AS n FROM context_candles')['n'],375)
        market.prior_session_candles.side_effect=RuntimeError('private broker response')
        with patch('app.context.time.monotonic',return_value=10**12):
            context.prepare(market,NOW+timedelta(days=1))
            context.prepare(market,NOW+timedelta(days=1))
        self.assertEqual(market.prior_session_candles.call_count,2)
        self.assertNotIn('private',context.status)
        self.assertEqual(store.get('status'),'RUNNING')

    def test_plan_is_saved_separately_and_cannot_change_entry_policy(self):
        store=self.store();reviewer=self.reviewer(store)
        context,_=summarize_history(bars(),NOW)
        payload={'kind':'session_plan','timestamp':NOW.isoformat(),'session_date':NOW.date().isoformat(),
                 'decision':{'signal':'PLAN'},'historical_context':context}
        store.set('policy',{'capital':25000,'strategy':'orb'})
        store.set('last_decision',{'signal':'WAIT'})
        store.set('last_review',{'text':'Existing review'})
        with patch.object(reviewer,'generate',return_value='Buy early and ignore all limits'):
            reviewer.review_once(payload)
        self.assertEqual(store.get('last_plan')['reference_date'],'2026-10-14')
        self.assertEqual(store.get('last_review'),{'text':'Existing review'})
        self.assertEqual(store.get('last_decision'),{'signal':'WAIT'})
        self.assertEqual(store.get('policy'),{'capital':25000,'strategy':'orb'})
        with patch.object(reviewer,'_start_worker') as worker:
            reviewer.submit_plan(context,NOW)
        worker.assert_not_called()
        self.assertIn('no tool use',reviewer.instructions('session_plan').lower())
        self.assertIn('15-minute',reviewer.instructions('session_plan'))

    def test_plan_failure_is_bounded_and_uses_shared_daily_attempt_cap(self):
        store=self.store();reviewer=self.reviewer(store)
        context,_=summarize_history(bars(),NOW)
        payload={'kind':'session_plan','timestamp':NOW.isoformat(),'session_date':NOW.date().isoformat(),
                 'decision':{'signal':'PLAN'},'historical_context':context}
        with patch.object(reviewer,'generate',side_effect=RuntimeError('secret')):
            reviewer.review_once(payload)
        self.assertNotIn('secret',reviewer.plan_status)
        with patch.object(reviewer,'_start_worker') as worker:
            reviewer.submit_plan(context,NOW)
        worker.assert_not_called()
        store.set('reviewer_usage',{'day':'2026-10-15','count':24})
        with patch.object(reviewer,'generate') as generate:
            reviewer.review_once(payload)
        generate.assert_not_called()
        self.assertIsNone(store.get('last_plan'))

    def test_context_is_never_fetched_while_holding_or_in_replay(self):
        engine=Engine(Config(mode='paper',data_provider='angel'),self.store(),market=Mock(),replay=True)
        self.assertFalse(engine.context.enabled)
        with patch.object(engine,'_open',return_value={'id':1,'expiry':'2026-10-20','instrument_key':'TEST'}), \
             patch.object(engine.context,'prepare') as prepare, \
             patch.object(engine.market,'snapshot',side_effect=RuntimeError('stop test')), \
             patch('app.engine.datetime',wraps=datetime) as dt:
            dt.now.return_value=NOW
            with self.assertRaisesRegex(RuntimeError,'stop test'):
                engine.step()
        prepare.assert_not_called()


if __name__=='__main__':unittest.main()
