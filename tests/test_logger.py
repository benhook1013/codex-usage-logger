from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import codex_usage as logger


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
        self.reset = int((self.now + timedelta(days=3)).timestamp())
        self.response = {'rateLimitsByLimitId': {'codex': {
            'planType': 'prolite', 'primary': {'usedPercent': 25,
            'windowDurationMins': 10080, 'resetsAt': self.reset},
            'secondary': None, 'credits': {'hasCredits': True,
            'unlimited': False, 'balance': '123.456789'}}}}

    def rows(self, response=None, history=None, now=None):
        return logger.make_rows(response or self.response, history or [], now or self.now)

    def test_weekly_primary_and_missing_five_hour(self):
        row = self.rows()[0]
        self.assertEqual(row['weekly_left_pct'], '75')
        self.assertEqual(row['five_hour_left_pct'], '')
        self.assertEqual(row['five_hour_event'], 'unavailable')

    def test_same_window_rate_and_exact_credit_delta(self):
        history = self.rows()
        self.response['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 38
        self.response['rateLimitsByLimitId']['codex']['credits']['balance'] = '123.456788'
        row = self.rows(history=history, now=self.now + timedelta(minutes=30))[0]
        self.assertEqual(row['weekly_consumed_pp'], '13')
        self.assertEqual(row['weekly_rate_pp_hour'], '26.0000')
        self.assertEqual(row['credit_consumed'], '0.000001')

    def test_changed_reset_and_allowance_increase_suppress_rate(self):
        history = self.rows()
        self.response['rateLimitsByLimitId']['codex']['primary']['resetsAt'] += 100
        row = self.rows(history=history, now=self.now + timedelta(minutes=15))[0]
        self.assertEqual(row['weekly_event'], 'reset_timestamp_changed')
        self.assertEqual(row['weekly_rate_pp_hour'], '')
        self.response['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = self.reset
        self.response['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 10
        row = self.rows(history=history, now=self.now + timedelta(minutes=15))[0]
        self.assertEqual(row['weekly_event'], 'allowance_increased')
        self.assertEqual(row['weekly_rate_pp_hour'], '')

    def test_reset_elapsed_suppresses_rate_even_if_timestamp_stale(self):
        row = self.rows(history=self.rows(), now=self.now + timedelta(days=4))[0]
        self.assertEqual(row['weekly_event'], 'reset_due_or_passed')
        self.assertEqual(row['weekly_rate_pp_hour'], '')
        self.assertEqual(row['sampling_gap'], 'true')

    def test_error_reading_is_not_zero_and_does_not_replace_last_success(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with patch.object(logger, 'read_limits', side_effect=TimeoutError('Usage read timed out')):
                self.assertEqual(logger.sample(directory), 1)
            row = logger.load_history(directory)[0]
            self.assertEqual(row['status'], 'error')
            self.assertEqual(row['weekly_left_pct'], '')
            self.assertEqual(row['credit_balance'], '')
        history = self.rows()
        error = dict.fromkeys(logger.FIELDS, '')
        error.update(timestamp_utc=logger.utc(self.now + timedelta(minutes=15)), status='error')
        history.append(error)
        row = self.rows(history=history, now=self.now + timedelta(minutes=30))[0]
        self.assertEqual(row['weekly_consumed_pp'], '0')
        self.assertEqual(row['elapsed_minutes'], 30)

    def test_real_zero_balance_and_zero_allowance_preserved(self):
        bucket = self.response['rateLimitsByLimitId']['codex']
        bucket['primary']['usedPercent'] = 100
        bucket['credits']['balance'] = '0'
        row = self.rows()[0]
        self.assertEqual(row['weekly_left_pct'], '0')
        self.assertEqual(row['credit_balance'], '0')
        bucket['credits'] = None
        self.assertEqual(self.rows()[0]['credit_balance'], '')

    def test_credit_topup_is_not_negative_consumption(self):
        history = self.rows()
        self.response['rateLimitsByLimitId']['codex']['credits']['balance'] = '200'
        row = self.rows(history=history, now=self.now + timedelta(minutes=15))[0]
        self.assertEqual(row['credit_event'], 'balance_increased')
        self.assertEqual(row['credit_consumed'], '')

    def test_retention_age_rows_and_bytes(self):
        row = self.rows()[0]
        old = dict(row, timestamp_utc=logger.utc(self.now - timedelta(days=91)))
        rows, encoded = logger.retain([old, row], self.now)
        self.assertEqual(rows, [row])
        with patch.object(logger, 'MAX_ROWS', 2):
            self.assertEqual(len(logger.retain([row] * 5, self.now)[0]), 2)
        one_size = len(logger.encode_rows([row]))
        with patch.object(logger, 'MAX_BYTES', one_size):
            rows, encoded = logger.retain([row] * 4, self.now)
            self.assertEqual(len(rows), 1)
            self.assertLessEqual(len(encoded), one_size)

    def test_multibucket_unknown_windows_preserved(self):
        self.response['rateLimitsByLimitId']['other'] = {'primary': {
            'usedPercent': 9, 'windowDurationMins': 60, 'resetsAt': self.reset}}
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['weekly_left_pct'], '')
        self.assertEqual(json.loads(rows[0]['response_json']), self.response)

    def test_fallback_single_bucket_and_five_hour_secondary(self):
        bucket = self.response['rateLimitsByLimitId']['codex']
        bucket['secondary'] = {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': self.reset}
        row = self.rows(response={'rateLimits': bucket})[0]
        self.assertEqual(row['five_hour_left_pct'], '88')

    def test_invalid_response_records_no_success(self):
        for bad in [None, {}, {'rateLimits': {'primary': {'usedPercent': 'NaN', 'windowDurationMins': 10080}}}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                logger.make_rows(bad, [], self.now)

    def test_malformed_credits_record_fixed_error_instead_of_success_or_zero(self):
        bucket = self.response['rateLimitsByLimitId']['codex']
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for bad in ['private upstream text', ['bad'], 1, True, [], '', 0, False]:
                with self.subTest(credits=bad):
                    bucket['credits'] = bad
                    with patch.object(logger, 'read_limits', return_value=self.response):
                        self.assertEqual(logger.sample(directory), 1)
                    row = logger.load_history(directory)[-1]
                    self.assertEqual(row['status'], 'error')
                    self.assertEqual(row['error'], 'Invalid credits')
                    self.assertEqual(row['weekly_left_pct'], '')
                    self.assertEqual(row['credit_balance'], '')
            for unavailable in [None, {}]:
                bucket['credits'] = unavailable
                self.assertEqual(self.rows()[0]['credit_balance'], '')
            del bucket['credits']
            self.assertEqual(self.rows()[0]['credit_balance'], '')

    def test_malformed_protocol_replies_record_fixed_error_during_initialize_or_read(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            fake = directory / 'codex'
            for method, request_id in [('initialize', 1), ('account/rateLimits/read', 2)]:
                for bad in [[], ['private upstream text'], 'private upstream text', 1, True, None, {'id': request_id}]:
                    with self.subTest(method=method, reply=bad):
                        fake.write_text('''#!/usr/bin/python3
import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if request.get('method') == ''' + repr(method) + ''':
        print(''' + repr(json.dumps(bad)) + ''', flush=True)
    elif 'id' in request:
        print(json.dumps({'id':request['id'], 'result':{}}), flush=True)
''')
                        fake.chmod(0o700)
                        self.assertEqual(logger.sample(directory, str(fake)), 1)
                        row = logger.load_history(directory)[-1]
                        self.assertEqual(row['status'], 'error')
                        self.assertEqual(row['error'], 'Codex app-server exited or returned invalid protocol data')
                        self.assertEqual(row['weekly_left_pct'], '')
                        self.assertEqual(row['credit_balance'], '')

    def test_protocol_client_only_initializes_and_reads_usage(self):
        with tempfile.TemporaryDirectory() as name:
            fake = Path(name) / 'codex'
            fake.write_text('''#!/usr/bin/python3
import sys,json
expected=['initialize','initialized','account/rateLimits/read']
for method in expected:
    request=json.loads(sys.stdin.readline())
    if request['method'] != method: sys.exit(3)
    if method == 'initialize':
        # Server request ids live in a separate namespace from client ids.
        print(json.dumps({'id': request['id'], 'method': 'unexpected/request'}), flush=True)
        rejected=json.loads(sys.stdin.readline())
        if rejected.get('error', {}).get('code') != -32601: sys.exit(4)
        print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
    if method == 'account/rateLimits/read':
        print(json.dumps({'id': request['id'], 'result': {'test': True}}), flush=True)
sys.stdin.read()
''')
            fake.chmod(0o700)
            self.assertEqual(logger.read_limits(str(fake)), {'test': True})

    def test_configured_cli_works_with_service_path_and_preserves_shim_directory(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / 'package/bin/cli.py'
            target.parent.mkdir(parents=True)
            target.write_text('''#!/usr/bin/python3
import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if 'id' in request:
        print(json.dumps({'id':request['id'], 'result':{'test':True}}), flush=True)
''')
            target.chmod(0o700)
            shim = root / '.volta/bin/codex'
            shim.parent.mkdir(parents=True)
            shim.symlink_to(target)
            with patch.dict(os.environ, {'PATH': '/usr/bin:/bin', 'CODEX_USAGE_CODEX': str(shim)}, clear=True):
                command, env = logger.codex_command()
                self.assertEqual(command[0], str(shim))
                self.assertEqual(env['PATH'].split(os.pathsep)[0], str(shim.parent))
                self.assertEqual(logger.read_limits(), {'test': True})
                self.assertEqual(logger.codex_command(str(target))[0][0], str(target))
                shim.unlink()
                with self.assertRaisesRegex(RuntimeError, 'Configured Codex executable unavailable'):
                    logger.codex_command()

    def test_pipe_close_errors_preserve_result_or_original_failure_and_close_both(self):
        for rejected in [False, True]:
            with self.subTest(rejected=rejected):
                process = MagicMock()
                first = {'id': 1, 'error': {'message': 'private upstream message'}} if rejected else {'id': 1, 'result': {}}
                replies = [first] if rejected else [first, {'id': 2, 'result': {'test': True}}]
                process.stdout.readline.side_effect = [json.dumps(reply) + '\n' for reply in replies] + ['']
                process.stdin.close.side_effect = BrokenPipeError('cleanup error')
                process.stdout.close.side_effect = OSError('other cleanup error')
                with patch.object(logger, 'codex_command', return_value=(['codex'], {})), \
                        patch.object(logger.subprocess, 'Popen', return_value=process), \
                        patch.object(logger.os, 'killpg'):
                    if rejected:
                        with self.assertRaisesRegex(RuntimeError, 'Codex rejected the usage read'):
                            logger.read_limits()
                    else:
                        self.assertEqual(logger.read_limits(), {'test': True})
                process.stdin.close.assert_called_once()
                process.stdout.close.assert_called_once()

    def test_successful_sample_roundtrip_and_atomic_temp_reused(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with patch.object(logger, 'read_limits', return_value=self.response):
                self.assertEqual(logger.sample(directory), 0)
                self.assertEqual(logger.sample(directory), 0)
            self.assertEqual(len(logger.load_history(directory)), 2)
            self.assertFalse((directory / '.usage.csv.tmp').exists())
            self.assertLessEqual((directory / 'usage.csv').stat().st_size, logger.MAX_BYTES)

    def test_history_directory_is_synced_after_replacement(self):
        sync = os.fsync
        synced = []
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)

            def observe_sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    self.assertEqual(os.fstat(fd).st_ino, directory.stat().st_ino)
                    self.assertEqual(len(logger.load_history(directory)), 1)
                    self.assertFalse((directory / '.usage.csv.tmp').exists())
                    synced.append('directory')
                else:
                    synced.append('file')
                sync(fd)

            with patch.object(logger, 'read_limits', return_value=self.response), \
                    patch.object(logger.os, 'fsync', side_effect=observe_sync):
                self.assertEqual(logger.sample(directory), 0)
            self.assertEqual(synced, ['file', 'directory'])

    def test_failed_directory_sync_closes_descriptor_and_reports_failure(self):
        sync = os.fsync
        descriptors = []
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)

            def fail_directory_sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    descriptors.append(fd)
                    raise OSError('injected directory sync failure')
                sync(fd)

            with patch.object(logger, 'read_limits', return_value=self.response), \
                    patch.object(logger.os, 'fsync', side_effect=fail_directory_sync), \
                    self.assertRaisesRegex(OSError, 'directory sync failure'):
                logger.sample(directory)
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
            self.assertEqual(len(logger.load_history(directory)), 1)


if __name__ == '__main__':
    unittest.main()
