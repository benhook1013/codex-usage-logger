"""Workspace-aware comparison identity through a synthetic account RPC."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

import codex_usage as logger


class WorkspaceIdentityTests(unittest.TestCase):
    def read_response(self, workspace, balance='100'):
        account = {'type': 'chatgpt', 'email': 'fixture@example.invalid'}
        if workspace is not None:
            account['workspaceName'] = workspace
        usage = {'rateLimits': {'limitId': 'codex', 'planType': 'pro',
                               'credits': {'balance': balance},
                               'primary': {'windowDurationMins': 300, 'usedPercent': 20,
                                           'resetsAt': 1900000000}}}
        with tempfile.TemporaryDirectory() as name:
            executable = Path(name) / 'codex'
            executable.write_text('#!' + sys.executable + '\nimport sys,json\n'
                'responses = ' + repr({1: {}, 2: usage, 3: {'account': account}}) + '\n'
                'for line in sys.stdin:\n'
                ' request = json.loads(line)\n'
                ' if "id" in request:\n'
                '  print(json.dumps({"id": request["id"], "result": responses[request["id"]]}), flush=True)\n')
            executable.chmod(0o700)
            return logger.read_limits(str(executable))

    def test_workspace_changes_suppress_credit_and_allowance_deltas(self):
        now = datetime.now(timezone.utc)
        original = self.read_response('Workspace A')
        history = logger.make_rows(original, [], now)
        for workspace in ('Workspace B', None, '', '  '):
            with self.subTest(workspace=workspace):
                current = self.read_response(workspace, '10')
                self.assertNotEqual(current.get('_account_key'), original['_account_key'])
                row = logger.make_rows(current, history, now + timedelta(minutes=10))[0]
                self.assertEqual(row['credit_consumed'], '')
                self.assertEqual(row['five_hour_consumed_pp'], '')
                self.assertNotIn('Workspace', row['response_json'])
        same = self.read_response('Workspace A', '10')
        self.assertEqual(same['_account_key'], original['_account_key'])
        self.assertEqual(logger.make_rows(same, history, now + timedelta(minutes=10))[0]['credit_consumed'], '90')

    def test_missing_or_invalid_workspace_never_produces_comparison_key(self):
        for workspace in (None, '', ' ', 42, []):
            with self.subTest(workspace=workspace):
                self.assertNotIn('_account_key', self.read_response(workspace))
