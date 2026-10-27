import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import Config
from app.reviewer import Reviewer
from app.store import Store


class AntigravityReviewerTests(unittest.TestCase):
    def reviewer(self):
        store=Store(Path(':memory:'));self.addCleanup(store.db.close)
        with patch('app.reviewer.shutil.which',return_value='/usr/local/bin/agy'):
            return Reviewer(Config(reviewer_provider='antigravity'),store,enabled=True)

    def test_cli_uses_sandbox_plan_and_never_inherits_broker_credentials(self):
        reviewer=self.reviewer()
        process=Mock(returncode=0)
        process.communicate.return_value=(json.dumps({'status':'SUCCESS','response':'WAIT: range incomplete.'}), 'private log')
        with patch.dict('os.environ',{'HOME':'/tmp/test-home','PATH':'/usr/bin','ANGEL_API_KEY':'broker-secret','GEMINI_API_KEY':'model-secret'}), \
             patch('app.reviewer.subprocess.Popen',return_value=process) as popen, \
             patch('app.reviewer.urllib.request.urlopen') as network:
            text=reviewer.generate({'decision':{'signal':'WAIT'}})
        self.assertEqual(text,'WAIT: range incomplete.')
        network.assert_not_called()
        args=popen.call_args.args[0];opts=popen.call_args.kwargs
        self.assertEqual(args[args.index('--mode')+1],'plan')
        self.assertIn('--sandbox',args)
        self.assertNotIn('--dangerously-skip-permissions',args)
        self.assertNotIn('ANGEL_API_KEY',opts['env']);self.assertNotIn('GEMINI_API_KEY',opts['env'])
        self.assertIn('meridian-review-',opts['cwd'])
        self.assertTrue(opts['start_new_session'])
        self.assertFalse(Path(opts['cwd']).exists())

    def test_failed_or_empty_response_is_not_treated_as_a_review(self):
        for output in ('not json', '{}', '{"status":"ERROR","response":"APPROVED"}', '{"status":"SUCCESS","response":""}'):
            reviewer=self.reviewer();process=Mock(returncode=0)
            process.communicate.return_value=(output,'secret')
            with patch('app.reviewer.subprocess.Popen',return_value=process):
                with self.assertRaisesRegex(RuntimeError,'no completed review'):
                    reviewer.generate({})

    def test_timeout_terminates_child_process_group(self):
        reviewer=self.reviewer();process=Mock(pid=123)
        process.communicate.side_effect=[subprocess.TimeoutExpired('agy',55),('','')]
        with patch('app.reviewer.subprocess.Popen',return_value=process),patch('app.reviewer.os.killpg') as kill:
            with self.assertRaisesRegex(RuntimeError,'timed out'):reviewer.generate({})
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0],123)

    def test_missing_cli_is_visible_without_a_gemini_key_requirement(self):
        store=Store(Path(':memory:'));self.addCleanup(store.db.close)
        with patch('app.reviewer.shutil.which',return_value=None):
            reviewer=Reviewer(Config(reviewer_provider='antigravity'),store,enabled=True)
        self.assertEqual(reviewer.view()['status'],'Antigravity CLI missing')
        self.assertNotIn('Gemini',reviewer.view()['status'])

    def test_provider_switch_does_not_reuse_gemini_error(self):
        store=Store(Path(':memory:'));self.addCleanup(store.db.close)
        store.set('reviewer_provider','gemini');store.set('reviewer_status','Gemini key missing')
        with patch('app.reviewer.shutil.which',return_value='/usr/bin/agy'):
            reviewer=Reviewer(Config(reviewer_provider='antigravity'),store,enabled=True)
        self.assertEqual(reviewer.view()['status'],'Waiting for a market snapshot')


if __name__=='__main__':unittest.main()
