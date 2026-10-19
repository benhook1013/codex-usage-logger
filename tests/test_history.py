from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_usage as logger


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.zone = patch.dict(os.environ, {'TZ': 'Pacific/Auckland'})
        self.zone.start()
        time.tzset()
        self.addCleanup(self.restore_zone)
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def restore_zone(self):
        self.zone.stop()
        time.tzset()

    def row(self, timestamp, weekly='70', status='ok', bucket='codex', event='same_window'):
        row = dict.fromkeys(logger.FIELDS, '')
        row.update(timestamp_utc=timestamp, status=status, limit_id=bucket,
                   weekly_left_pct=weekly if status == 'ok' else '',
                   credit_balance='123.456789' if status == 'ok' else '', weekly_event=event)
        return row

    def render(self, rows, period='hour', days=7):
        output = io.StringIO()
        with patch.object(logger, 'load_history', return_value=rows), redirect_stdout(output):
            logger.list_history(Path('/unused'), period, days, self.now)
        return output.getvalue()

    def test_hourly_uses_latest_success_not_error_or_average(self):
        rows = [self.row('2026-10-19T09:00:00+00:00', '70'),
                self.row('2026-10-19T09:15:00+00:00', '60', event='reset_timestamp_changed'),
                self.row('2026-10-19T09:30:00+00:00', status='error')]
        output = self.render(rows)
        lines = output.splitlines()
        data = next(line for line in lines if line.startswith('2026-10-19'))
        self.assertIn('22:00 +1300', data)
        self.assertIn('22:15:00 NZDT', data)
        self.assertIn('60%', data)
        self.assertNotIn('70%', data)
        self.assertIn('123.456789', data)
        self.assertIn('reset timestamp changed', data)
        self.assertRegex(data, r'\s2\s+1\s')

    def test_daily_groups_by_local_day_and_filters_calendar_days(self):
        rows = [self.row('2026-10-19T10:00:00+00:00', '70'),
                self.row('2026-10-19T11:00:00+00:00', '50'),
                self.row('2026-10-19T12:00:00+00:00', '40')]
        output = self.render(rows, 'day', 2)
        data = [line for line in output.splitlines() if line.startswith('2026-')]
        self.assertEqual(len(data), 1)
        self.assertTrue(data[0].startswith('2026-10-20'))
        self.assertIn('40%', data[0])
        self.assertNotIn('2026-10-19', output)
        self.assertIn('No readings', self.render(rows, 'day', 1))

    def test_error_only_period_is_visible_without_fake_zero(self):
        output = self.render([self.row('2026-10-19T09:00:00+00:00', status='error')])
        data = next(line for line in output.splitlines() if line.startswith('2026-'))
        self.assertNotIn('%', data)
        self.assertRegex(data, r'\s0\s+1\s')

    def test_zero_allowance_and_multiple_buckets_remain_distinct(self):
        output = self.render([self.row('2026-10-19T09:00:00+00:00', '0'),
                              self.row('2026-10-19T09:00:00+00:00', '75', bucket='other')])
        data = [line for line in output.splitlines() if line.startswith('2026-')]
        self.assertEqual(len(data), 2)
        self.assertIn('0%', data[0])
        self.assertIn('other', data[1])
        self.assertIn('75%', data[1])

    def test_daylight_saving_repeated_hours_are_distinct(self):
        self.now = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        rows = [self.row('2026-04-04T13:15:00+00:00', '80'),
                self.row('2026-04-04T14:15:00+00:00', '70')]
        output = self.render(rows)
        self.assertIn('2026-04-05 02:00 +1300', output)
        self.assertIn('2026-04-05 02:00 +1200', output)

    def test_cli_listing_never_samples_or_writes(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'usage.csv'
            path.write_bytes(logger.encode_rows([self.row('2026-10-19T09:00:00+00:00')]))
            before = path.read_bytes()
            with patch.object(logger, 'sample', side_effect=AssertionError('Must not sample')), redirect_stdout(io.StringIO()):
                self.assertEqual(logger.main(['list', '--day', '--days', '90', '--data-dir', name]), 0)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_history_options_do_not_accidentally_sample(self):
        with patch.object(logger, 'sample', side_effect=AssertionError('Must not sample')), redirect_stderr(io.StringIO()):
            for args in [['--day'], ['list', '--day', '--hour'], ['list', '--days', '0'],
                         ['list', '--days', '91'], ['list', '--status']]:
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    logger.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_default_invocation_still_samples_for_the_service(self):
        with patch.object(logger, 'sample', return_value=0) as sample:
            self.assertEqual(logger.main([]), 0)
            sample.assert_called_once_with(logger.DATA, None)


if __name__ == '__main__':
    unittest.main()
