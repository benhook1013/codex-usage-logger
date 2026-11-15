#!/usr/bin/env python3
"""Link this checkout into a Linux user account and enable the usage timer."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from src.codex_usage import codex_command, installed_environment

ROOT = Path(__file__).resolve().parent


def env_quote(value):
    """Quote a path for systemd EnvironmentFile syntax, rejecting line breaks and nulls."""
    if any(c in value for c in '\n\r\0'):
        raise ValueError('Environment paths cannot contain newlines or null bytes')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def saved_defaults(home):
    """Recover usable defaults without letting malformed saved configuration block repair."""
    try:
        saved = installed_environment(home)
    except (RuntimeError, UnicodeError):
        return {}
    valid = {}
    for key, value in saved.items():
        path = Path(value).expanduser()
        if not value or not path.is_absolute():
            continue
        if key == 'CODEX_USAGE_CODEX':
            usable = path.is_file() and os.access(path, os.X_OK)
        else:
            usable = path.is_dir()
        if usable:
            valid[key] = str(path)
    return valid


def install(home, codex_home=None, sqlite_home=None, codex=None):
    """Link this checkout and atomically save private profile and CLI paths; preserve regular files."""
    if not os.access(ROOT / 'src/codex_usage.py', os.X_OK):
        raise RuntimeError('Logger source is not executable; restore permission with chmod +x src/codex_usage.py')
    # A reinstall must be able to repair corrupt or stale saved settings.
    saved = saved_defaults(home)
    selected = codex or os.environ.get('CODEX_USAGE_CODEX') or saved.get('CODEX_USAGE_CODEX')
    command, _ = codex_command(selected, use_installed=False)
    codex_home = Path(codex_home or os.environ.get('CODEX_HOME') or
                      saved.get('CODEX_HOME') or home / '.codex').expanduser().resolve()
    sqlite_home = sqlite_home or os.environ.get('CODEX_SQLITE_HOME') or saved.get('CODEX_SQLITE_HOME')
    if sqlite_home is not None:
        sqlite_home = Path(sqlite_home).expanduser().resolve()
    value = 'CODEX_HOME=' + env_quote(str(codex_home)) + '\n'
    value += 'CODEX_USAGE_CODEX=' + env_quote(command[0]) + '\n'
    if sqlite_home is not None:
        value += 'CODEX_SQLITE_HOME=' + env_quote(str(sqlite_home)) + '\n'
    links = {
        home / '.local/bin/codex-usage': ROOT / 'src/codex_usage.py',
        home / '.config/systemd/user/codex-usage.service': ROOT / 'systemd/codex-usage.service',
        home / '.config/systemd/user/codex-usage.timer': ROOT / 'systemd/codex-usage.timer',
    }
    for destination in links:
        if destination.exists() and not destination.is_symlink():
            raise RuntimeError(f'Refusing to overwrite existing regular file: {destination}')
    for destination, source in links.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        destination.symlink_to(source)
    config = home / '.config/codex-usage'
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = config / 'environment'
    # This file contains a local profile path, not credentials.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=config,
                                         prefix='.environment.', delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, environment)
        directory_fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    """Enable the timer and attempt a bounded first read, reporting read failures without undoing setup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path,
                        help='Existing Codex profile; defaults to CODEX_HOME, saved profile, or ~/.codex')
    parser.add_argument('--sqlite-home', type=Path,
                        help='Existing Linux Codex database directory; defaults to CODEX_SQLITE_HOME or saved path')
    parser.add_argument('--codex', help='Codex executable to preserve for scheduled reads (normally auto-detected)')
    args = parser.parse_args(argv)
    install(Path.home(), args.codex_home, args.sqlite_home, args.codex)
    env = os.environ.copy()
    # Let systemctl use the user manager's private socket; desktop-inherited
    # session-bus addresses can be stale inside WSL.
    env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    for command in [
        ['systemctl', '--user', 'daemon-reload'],
        ['systemctl', '--user', 'enable', '--now', 'codex-usage.timer'],
    ]:
        subprocess.run(command, check=True, env=env, timeout=60)
    try:
        first_failed = subprocess.run(['systemctl', '--user', 'start', 'codex-usage.service'],
                                      check=False, env=env, timeout=90).returncode != 0
    except subprocess.TimeoutExpired:
        first_failed = True
    if first_failed:
        print('Installed and timer enabled, but the first reading failed or timed out. '
              'Check: ~/.local/bin/codex-usage --status and codex login status')
    print('Enabled: readings every 15 minutes, plus startup.')
    print('Live source:', ROOT / 'src/codex_usage.py')
    print('View history: ~/.local/bin/codex-usage --status')


if __name__ == '__main__':
    main()
