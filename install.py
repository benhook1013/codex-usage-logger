#!/usr/bin/env python3
"""Link this checkout into a Linux user account and enable the usage timer."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def env_quote(value):
    if any(c in value for c in '\n\r\0'):
        raise ValueError('Environment paths cannot contain newlines or null bytes')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def install(home, codex_home, sqlite_home=None):
    value = 'CODEX_HOME=' + env_quote(str(codex_home)) + '\n'
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
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path,
                        default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))),
                        help='Existing Codex profile; defaults to CODEX_HOME or ~/.codex')
    parser.add_argument('--sqlite-home', type=Path,
                        default=os.environ.get('CODEX_SQLITE_HOME'),
                        help='Existing Linux Codex database directory; defaults to CODEX_SQLITE_HOME when set')
    args = parser.parse_args()
    install(Path.home(), args.codex_home.expanduser().resolve(),
            args.sqlite_home.expanduser().resolve() if args.sqlite_home else None)
    env = os.environ.copy()
    # Let systemctl use the user manager's private socket; desktop-inherited
    # session-bus addresses can be stale inside WSL.
    env.pop('DBUS_SESSION_BUS_ADDRESS', None)
    for command in [
        ['systemctl', '--user', 'daemon-reload'],
        ['systemctl', '--user', 'enable', '--now', 'codex-usage.timer'],
        ['systemctl', '--user', 'start', 'codex-usage.service'],
    ]:
        subprocess.run(command, check=True, env=env)
    print('Enabled: readings every 15 minutes, plus startup.')
    print('Live source:', ROOT / 'src/codex_usage.py')
    print('View history: ~/.local/bin/codex-usage --status')


if __name__ == '__main__':
    main()
