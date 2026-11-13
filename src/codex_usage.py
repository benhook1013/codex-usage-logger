#!/usr/bin/python3
"""Bounded local Codex quota history. Uses only read RPCs; never starts a turn."""
import argparse
import hashlib
import re
import sys
import uuid
import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import fcntl
import io
import json
import math
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading
import time

DATA = Path.home() / '.local/share/codex-usage'
MAX_AGE_DAYS = 90
MAX_ROWS = 10000
MAX_BYTES = 10 * 1024 * 1024
FIELDS = ['sample_id', 'account_key', 'timestamp_utc', 'timestamp_local', 'status', 'error', 'limit_id',
          'plan', 'elapsed_minutes', 'sampling_gap',
          'five_hour_left_pct', 'five_hour_reset_utc', 'five_hour_event',
          'five_hour_consumed_pp', 'five_hour_rate_pp_hour',
          'weekly_left_pct', 'weekly_reset_utc', 'weekly_event',
          'weekly_consumed_pp', 'weekly_rate_pp_hour',
          'credit_balance', 'has_credits', 'unlimited_credits',
          'credit_consumed', 'credit_event', 'limit_reached_type', 'response_json']


LEGACY_FIELDS = FIELDS[2:]
READ_TIMEOUT = 45


def utc(dt):
    """Format an aware datetime in UTC with whole-second precision."""
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def number(value):
    """Return a finite Decimal, or None for missing, boolean, or invalid numeric values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


def installed_environment(home=None):
    """Read only the installer's quoted path settings, without executing shell input."""
    path = (Path.home() if home is None else home) / '.config/codex-usage/environment'
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'(CODEX_HOME|CODEX_SQLITE_HOME|CODEX_USAGE_CODEX)="((?:[^"\\]|\\.)*)"', line)
        if not match:
            raise RuntimeError('Invalid installed usage configuration; rerun install.py')
        result[match[1]] = re.sub(r'\\([\\"$`])', r'\1', match[2])
    return result


def codex_command(explicit=None, *, use_installed=True):
    """Choose an explicit, configured, or detected CLI and preserve its shim directory on PATH."""
    configured = installed_environment() if use_installed else {}
    candidate = explicit or os.environ.get('CODEX_USAGE_CODEX') or configured.get('CODEX_USAGE_CODEX') or shutil.which('codex')
    if not candidate:
        candidates = list((Path.home() / '.nvm/versions/node').glob('v*/bin/codex'))
        candidates.sort(key=lambda p: tuple(int(x) for x in p.parents[1].name[1:].split('.')), reverse=True)
        candidate = str(candidates[0]) if candidates else None
    if not candidate:
        raise RuntimeError('Codex executable unavailable; install or repair the Codex CLI')
    # Keep an npm shim's bin directory on PATH so its node interpreter is found.
    # Resolving the symlink would instead select node_modules/.../bin.
    candidate = str(Path(candidate).expanduser().absolute())
    if not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError('Configured Codex executable unavailable; rerun install.py --codex with its current path')
    env = {**configured, **os.environ}
    env['PATH'] = str(Path(candidate).absolute().parent) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    return [candidate, 'app-server', '--listen', 'stdio://', '-c', 'analytics.enabled=false'], env


def read_limits(explicit=None):
    """Initialize a read-only app-server, read quotas, and clean up its process group and pipes."""
    command, env = codex_command(explicit)
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', env=env, cwd=Path.home(),
                         start_new_session=True)
    messages = queue.Queue(maxsize=32)
    stopped = threading.Event()

    def enqueue(value):
        while not stopped.is_set():
            try:
                messages.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def receive():
        """Queue JSON objects from bounded protocol lines; signal EOF or malformed data with None."""
        try:
            while True:
                line = p.stdout.readline(1024 * 1024 + 1)
                if not line or len(line) > 1024 * 1024:
                    enqueue(None)
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    enqueue(None)
                    break
                enqueue(message)
        except (ValueError, OSError):
            enqueue(None)

    threading.Thread(target=receive, daemon=True).start()
    deadline = time.monotonic() + READ_TIMEOUT

    def send(message):
        """Write and flush one JSON message, translating pipe errors into a fixed diagnostic."""
        completed = queue.Queue(maxsize=1)
        def write():
            try:
                p.stdin.write(json.dumps(message) + '\n')
                p.stdin.flush()
                completed.put(None)
            except (OSError, ValueError):
                completed.put(False)
        threading.Thread(target=write, daemon=True).start()
        try:
            outcome = completed.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError('Codex usage read timed out') from None
        if outcome is False:
            raise RuntimeError('Codex app-server exited or returned invalid protocol data') from None

    def request(request_id, method, params=None):
        """Await a matching reply within the shared deadline, rejecting unsupported server requests."""
        message = {'id': request_id, 'method': method}
        if params is not None:
            message['params'] = params
        send(message)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex usage read timed out')
            try:
                reply = messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError('Codex usage read timed out') from None
            if reply is None:
                raise RuntimeError('Codex app-server exited or returned invalid protocol data')
            if reply.get('id') == request_id and 'method' not in reply:
                if 'error' in reply:
                    # Do not copy arbitrary upstream messages or credentials into logs.
                    raise RuntimeError('Codex rejected the usage read; check codex login status')
                if 'result' not in reply:
                    raise RuntimeError('Codex app-server exited or returned invalid protocol data')
                return reply['result']
            if 'id' in reply and 'method' in reply:
                send({'id': reply['id'], 'error': {
                    'code': -32601, 'message': 'Read-only logger does not handle requests'}})

    try:
        request(1, 'initialize', {'clientInfo': {
            'name': 'codex_usage_logger', 'title': 'Local usage logger', 'version': '1.0.0'}})
        send({'method': 'initialized'})
        result = request(2, 'account/rateLimits/read')
        # No credentials are read or stored. Missing identity suppresses cross-sample deltas.
        if isinstance(result, dict) and ('rateLimits' in result or 'rateLimitsByLimitId' in result):
            try:
                account = request(3, 'account/read', {'refreshToken': False}).get('account') or {}
                identity = account.get('accountId') or account.get('id') or account.get('email')
                workspace = account.get('workspaceName')
                if (isinstance(identity, str) and identity.strip()
                        and isinstance(workspace, str) and workspace.strip()):
                    key = json.dumps([account.get('type'), identity, workspace], separators=(',', ':'))
                    result = {**result, '_account_key': hashlib.sha256(key.encode()).hexdigest()}
            except (RuntimeError, TimeoutError, AttributeError):
                pass
        return result
    finally:
        stopped.set()
        # The npm entry point starts a native child. Stop their entire process
        # group so manual invocations cannot leave app-servers behind.
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # The leader can exit while a native child still holds the pipes open.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(timeout=3)
        for stream in (p.stdin, p.stdout):
            try:
                stream.close()
            except OSError:
                pass


def make_rows(result, history, now):
    """Validate quota buckets and derive rows, comparing only matching plans and allowance windows."""
    if not isinstance(result, dict):
        raise ValueError('Invalid usage response')
    account_key = result.get('_account_key') or ''
    # Retain only quota payloads, never top-level account identity or future metadata.
    result = {key: result[key] for key in ('rateLimits', 'rateLimitsByLimitId') if key in result}
    encoded = json.dumps(result, separators=(',', ':'), ensure_ascii=True)
    if len(encoded) > 65536:
        raise ValueError('Usage response exceeded 64 KiB safety limit')
    buckets = result.get('rateLimitsByLimitId') or {}
    if not buckets and isinstance(result.get('rateLimits'), dict):
        bucket = result['rateLimits']
        buckets = {bucket.get('limitId') or 'codex': bucket}
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError('No usage buckets returned; authentication may need attention')
    rows = []
    sample_id = uuid.uuid4().hex
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            raise ValueError('Invalid usage bucket')
        row = dict.fromkeys(FIELDS, '')
        row.update(sample_id=sample_id, account_key=account_key, timestamp_utc=utc(now), timestamp_local=now.astimezone().isoformat(timespec='seconds'),
                   status='ok', limit_id=str(limit_id), plan=bucket.get('planType') or '',
                   limit_reached_type=bucket.get('rateLimitReachedType') or '')
        previous = next((r for r in reversed(history)
                         if r['status'] == 'ok' and r['limit_id'] == str(limit_id)), None)
        elapsed = (now - datetime.fromisoformat(previous['timestamp_utc'])).total_seconds() if previous else None
        comparable = (previous and elapsed > 0 and previous['plan'] == row['plan']
                      and account_key and previous.get('account_key') == account_key)
        if elapsed is not None:
            row['elapsed_minutes'] = round(elapsed / 60, 3)
            row['sampling_gap'] = str(elapsed > 25 * 60).lower()
        windows = [bucket.get('primary'), bucket.get('secondary')]
        if any(window is not None and not isinstance(window, dict) for window in windows):
            raise ValueError('Invalid usage window')
        for name, duration in [('five_hour', 300), ('weekly', 10080)]:
            window = next((w for w in windows if isinstance(w, dict) and w.get('windowDurationMins') == duration), None)
            if not window:
                row[name + '_event'] = 'unavailable'
                continue
            used = number(window.get('usedPercent'))
            if used is None or not 0 <= used <= 100:
                raise ValueError('Invalid usage percentage')
            row[name + '_left_pct'] = str(100 - used)
            reset = window.get('resetsAt')
            if reset is not None:
                try:
                    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset):
                        raise ValueError('Invalid reset timestamp')
                    row[name + '_reset_utc'] = utc(datetime.fromtimestamp(reset, timezone.utc))
                except (OverflowError, OSError, ValueError):
                    raise ValueError('Invalid reset timestamp') from None
            event = 'first_reading'
            old_left = number(previous.get(name + '_left_pct')) if comparable else None
            if old_left is not None:
                old_reset = previous[name + '_reset_utc']
                if old_reset != row[name + '_reset_utc']:
                    event = 'reset_timestamp_changed'
                elif old_reset and now >= datetime.fromisoformat(old_reset):
                    event = 'reset_due_or_passed'
                elif 100 - used > old_left:
                    event = 'allowance_increased'
                elif not old_reset:
                    event = 'reset_unknown'
                else:
                    event = 'same_window'
                    consumed = old_left - (100 - used)
                    row[name + '_consumed_pp'] = str(consumed)
                    row[name + '_rate_pp_hour'] = str(round(consumed * Decimal(3600) / Decimal(str(elapsed)), 4))
            row[name + '_event'] = event
        credit_data = bucket.get('credits')
        if credit_data is None:
            credit_data = {}
        if not isinstance(credit_data, dict):
            raise ValueError('Invalid credits')
        balance = number(credit_data.get('balance'))
        if credit_data.get('balance') is not None and balance is None:
            raise ValueError('Invalid credit balance')
        row['credit_balance'] = str(balance) if balance is not None else ''
        for field, key in [('has_credits', 'hasCredits'), ('unlimited_credits', 'unlimited')]:
            if isinstance(credit_data.get(key), bool):
                row[field] = str(credit_data[key]).lower()
        row['credit_event'] = 'unavailable' if balance is None else 'first_reading'
        old_balance = number(previous.get('credit_balance')) if comparable else None
        if balance is not None and old_balance is not None:
            consumed = old_balance - balance
            row['credit_event'] = 'balance_increased' if consumed < 0 else 'same_or_lower_balance'
            if consumed >= 0:
                row['credit_consumed'] = str(consumed)
        rows.append(row)
    # Keep the quota payloads once per reading, without top-level account identity.
    rows[0]['response_json'] = encoded
    return rows


def encode_rows(rows):
    """Encode history as UTF-8 CSV with the canonical field order and header."""
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode('utf-8')


def retain(rows, now):
    """Keep the newest rows within age, count, and byte limits; return rows and encoded CSV."""
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    rows = [r for r in rows if datetime.fromisoformat(r['timestamp_utc']) >= cutoff][-MAX_ROWS:]
    encoded = encode_rows(rows)
    if len(encoded) > MAX_BYTES:
        lo, hi = 0, len(rows)
        while lo < hi:
            middle = (lo + hi) // 2
            if len(encode_rows(rows[middle:])) > MAX_BYTES:
                lo = middle + 1
            else:
                hi = middle
        rows = rows[lo:]
        encoded = encode_rows(rows)
    return rows, encoded


def load_history(directory):
    """Read bounded CSV history, returning an empty list if absent and rejecting schema drift."""
    path = directory / 'usage.csv'
    if not path.exists():
        return []
    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError('History exceeds its size limit; inspect it before continuing')
    csv.field_size_limit(1024 * 1024)
    with path.open(newline='', encoding='utf-8') as source:
        reader = csv.DictReader(source)
        if reader.fieldnames == LEGACY_FIELDS:
            return [{**dict.fromkeys(FIELDS, ''), **row} for row in reader]
        if reader.fieldnames != FIELDS:
            raise RuntimeError('Unexpected history schema; inspect it before continuing')
        return list(reader)


def sample(directory, explicit=None):
    """Save one reading or sanitized error under a lock, trim history, and return its exit status."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = load_history(directory)
        try:
            response = read_limits(explicit)
            now = datetime.now(timezone.utc)
            rows = make_rows(response, history, now)
            success = True
        except Exception as exc:
            now = datetime.now(timezone.utc)
            # Only local fixed diagnostic messages are persisted, never stderr/auth material.
            message = str(exc) if isinstance(exc, (RuntimeError, ValueError, TimeoutError)) else type(exc).__name__
            row = dict.fromkeys(FIELDS, '')
            row.update(sample_id=uuid.uuid4().hex, timestamp_utc=utc(now), timestamp_local=now.astimezone().isoformat(timespec='seconds'),
                       status='error', error=message[:240])
            rows, success = [row], False
        _, encoded = retain(history + rows, now)
        temporary = directory / '.usage.csv.tmp'
        try:
            with temporary.open('wb') as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, directory / 'usage.csv')
        except BaseException:
            # Preserve the original failure even if best-effort cleanup fails.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        print(f"{'Recorded usage' if success else 'Recorded error'} at {utc(now)}")
        return 0 if success else 1


def status(directory):
    """Print the latest saved reading and retention limits without contacting Codex."""
    history = load_history(directory)
    if not history:
        print('No readings yet.')
        return
    latest_time = history[-1]['timestamp_utc']
    print('Latest reading:', latest_time)
    latest_id = history[-1].get('sample_id')
    for row in [r for r in history if (r.get('sample_id') == latest_id if latest_id else r['timestamp_utc'] == latest_time)]:
        if row['status'] != 'ok':
            print('Error:', row['error'])
            continue
        print('Bucket:', row['limit_id'])
        for name, label in [('weekly', 'Weekly'), ('five_hour', 'Five-hour')]:
            remaining = row[name + '_left_pct']
            print(f"  {label} left: {remaining + '%' if remaining else 'unavailable'}")
            if row[name + '_reset_utc']:
                print('    Resets:', datetime.fromisoformat(row[name + '_reset_utc']).astimezone().isoformat(timespec='seconds'))
        print('  Credits:', row['credit_balance'] or 'unavailable')
    print(f'History: {directory / "usage.csv"} ({len(history)} rows)')
    print('Retention: 90 days, 10,000 rows, 10 MiB maximum; oldest readings removed first.')


def list_history(directory, period='hour', days=7, now=None):
    """Print the last success per local day or hour, retaining error counts and distinct DST hours."""
    now = now or datetime.now(timezone.utc)
    first_day = now.astimezone().date() - timedelta(days=days - 1)
    groups = {}
    for row in sorted(load_history(directory), key=lambda r: datetime.fromisoformat(r['timestamp_utc'])):
        timestamp = datetime.fromisoformat(row['timestamp_utc']).astimezone()
        if timestamp.date() < first_day or timestamp > now:
            continue
        # Include the UTC offset so the repeated hour at daylight-saving end
        # remains two distinct hours. Daily rows span the whole local day.
        label = timestamp.strftime('%Y-%m-%d' if period == 'day' else '%Y-%m-%d %H:00 %z')
        group = groups.setdefault(label, {'buckets': {}, 'errors': 0})
        if row['status'] != 'ok':
            group['errors'] += 1
            continue
        bucket = group['buckets'].setdefault(row['limit_id'], {'count': 0, 'events': set()})
        bucket.update(row=row, time=timestamp.strftime('%H:%M:%S %Z'))
        bucket['count'] += 1
        for window in ['weekly', 'five_hour']:
            event = row[window + '_event']
            if event in ['reset_timestamp_changed', 'reset_due_or_passed', 'allowance_increased']:
                bucket['events'].add(event.replace('_', ' '))
        if row['sampling_gap'] == 'true':
            bucket['events'].add('sampling gap')
    if not groups:
        print(f'No readings in the last {days} local calendar days.')
        return
    table = [['Day (local)' if period == 'day' else 'Hour (local)', 'Bucket', 'Last success',
              'Weekly left', '5h left', 'Credits', 'Readings', 'Errors', 'Events']]
    for label, group in groups.items():
        for limit_id, bucket in sorted(group['buckets'].items()) or [('-', None)]:
            row = bucket['row'] if bucket else {}
            def percentage(key):
                """Render a saved percentage, preserving real zero and marking missing values with a dash."""
                value = row.get(key, '')
                return value + '%' if value else '-'
            table.append([label, limit_id, bucket['time'] if bucket else '-',
                          percentage('weekly_left_pct'), percentage('five_hour_left_pct'),
                          row.get('credit_balance') or '-', str(bucket['count'] if bucket else 0),
                          str(group['errors']), ', '.join(sorted(bucket['events'])) if bucket else '-'])
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for index, row in enumerate(table):
        print('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        if index == 0:
            print('  '.join('-' * width for width in widths))
    print('\nValues are the last successful observation per period, not daily/hourly consumption.')
    print("'-' means unavailable. Errors count failed reads in the period, shared across buckets.")


def history_days(value):
    """Parse a whole-number calendar-day count within the supported retention range."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('days must be a whole number from 1 to 90') from None
    if not 1 <= days <= MAX_AGE_DAYS:
        raise argparse.ArgumentTypeError('days must be a whole number from 1 to 90')
    return days


def main(argv=None):
    """Dispatch validated CLI options to saved-history views or the default scheduled sample."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['list'], help='List saved history without a network request')
    parser.add_argument('--status', action='store_true', help='Show the latest saved reading without a network request')
    parser.add_argument('--data-dir', type=Path, default=DATA)
    parser.add_argument('--codex', help='Explicit Codex executable (normally auto-detected)')
    grouping = parser.add_mutually_exclusive_group()
    grouping.add_argument('--day', action='store_const', const='day', dest='period', help='One summary per local day')
    grouping.add_argument('--hour', action='store_const', const='hour', dest='period', help='One summary per local hour (default)')
    parser.add_argument('--days', type=history_days, help='Local calendar days to list, including today (default: 7; maximum: 90)')
    args = parser.parse_args(argv)
    if args.command == 'list' and args.status:
        parser.error('choose list or --status')
    if args.command != 'list' and (args.period is not None or args.days is not None):
        parser.error('--day, --hour, and --days require the list command')
    if args.command == 'list':
        list_history(args.data_dir, args.period or 'hour', args.days or 7)
        return 0
    if args.status:
        status(args.data_dir)
        return 0
    return sample(args.data_dir, args.codex)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, csv.Error):
        print('codex-usage: history or configuration operation failed; inspect storage permissions and saved history', file=sys.stderr)
        raise SystemExit(1)
