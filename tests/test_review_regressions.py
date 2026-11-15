"""Offline regressions for configuration, identity, history and bounded writes."""
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import codex_usage as logger
import install


class ReviewRegressions(unittest.TestCase):
    def response(self, account='one', balance='100'):
        return {'_account_key': account, 'rateLimits': {'limitId': 'codex', 'planType': 'pro',
            'credits': {'balance': balance}}}

    def test_account_change_or_unknown_identity_suppresses_deltas(self):
        now = datetime.now(timezone.utc)
        old = logger.make_rows(self.response(), [], now)
        for identity in ('two', ''):
            row = logger.make_rows(self.response(identity, '10'), old, now + timedelta(minutes=10))[0]
            self.assertEqual(row['credit_consumed'], '')
        row = logger.make_rows(self.response('one', '10'), old, now + timedelta(minutes=10))[0]
        self.assertEqual(row['credit_consumed'], '90')

    def test_same_second_samples_remain_separate(self):
        now = datetime.now(timezone.utc)
        old = logger.make_rows(self.response(), [], now)
        new = logger.make_rows(self.response(balance='10'), old, now)
        self.assertNotEqual(old[0]['sample_id'], new[0]['sample_id'])
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'usage.csv').write_bytes(logger.encode_rows(old + new))
            output = io.StringIO()
            with redirect_stdout(output): logger.status(directory)
            self.assertEqual(output.getvalue().count('Bucket:'), 1)
            self.assertIn('Credits: 10', output.getvalue())

    def test_legacy_csv_is_read_and_upgraded_without_losing_rows(self):
        import csv
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with (directory / 'usage.csv').open('w') as handle:
                writer = csv.DictWriter(handle, fieldnames=logger.LEGACY_FIELDS)
                writer.writeheader()
                writer.writerow({'timestamp_utc': '2026-11-08T00:00:00+00:00', 'status': 'ok'})
            rows = logger.load_history(directory)
            self.assertEqual(rows[0]['account_key'], '')
            self.assertEqual(rows[0]['timestamp_utc'], '2026-11-08T00:00:00+00:00')
            (directory / 'usage.csv').write_bytes(logger.encode_rows(rows))
            self.assertEqual(logger.load_history(directory), rows)

    def test_manual_command_loads_config_and_env_can_override(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            codex = home / 'codex'
            codex.write_text('#!/bin/sh\nexit 0\n')
            codex.chmod(0o700)
            with patch.object(install, 'codex_command', return_value=([str(codex)], {})):
                install.install(home, home / 'profile $ with "quotes"', home / 'db')
            with patch.object(logger.Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True):
                _, env = logger.codex_command()
                self.assertEqual(env['CODEX_HOME'], str(home / 'profile $ with "quotes"'))
                self.assertEqual(env['CODEX_SQLITE_HOME'], str(home / 'db'))
                with patch.dict(os.environ, {'CODEX_HOME': '/explicit'}):
                    self.assertEqual(logger.codex_command()[1]['CODEX_HOME'], '/explicit')

    def test_nonexecutable_source_fails_before_install_changes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'src').mkdir()
            (root / 'src/codex_usage.py').write_text('fixture')
            home = root / 'home'
            with patch.object(install, 'ROOT', root), self.assertRaisesRegex(RuntimeError, 'not executable'):
                install.install(home, root / 'profile')
            self.assertFalse(home.exists())

    def test_real_outbound_backpressure_obeys_deadline(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            fake = root / 'codex'
            fake.write_text('#!' + sys.executable + '\nimport sys,json\n'
                'sys.stdin.readline()\nprint(json.dumps({"id":1,"result":{}}), flush=True)\n'
                'for i in range(100000):\n print(json.dumps({"id": "x"*1000+str(i), "method":"unexpected"}), flush=True)\n')
            fake.chmod(0o700)
            script = ('import codex_usage as l,sys,time; l.READ_TIMEOUT=0.3; t=time.monotonic(); '
                '\ntry: l.read_limits(sys.argv[1])\nexcept TimeoutError: pass\nelse: raise AssertionError("expected timeout")\n'
                'assert time.monotonic()-t < 4')
            result = subprocess.run([sys.executable, '-c', script, str(fake)], capture_output=True, text=True, timeout=6)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_failure_emits_fixed_diagnostic(self):
        with tempfile.TemporaryDirectory() as name:
            bad = Path(name) / 'not-a-directory'
            bad.write_text('fixture')
            script = Path(logger.__file__)
            result = subprocess.run([sys.executable, str(script), '--data-dir', str(bad)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('history or configuration operation failed', result.stderr)
            self.assertNotIn(name, result.stderr)
