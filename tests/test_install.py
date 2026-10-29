from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallTests(unittest.TestCase):
    def test_installs_links_to_checkout_without_copying_source(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            install.install(home, home / 'Codex Profile')
            for destination, source in [
                ('.local/bin/codex-usage', 'src/codex_usage.py'),
                ('.config/systemd/user/codex-usage.service', 'systemd/codex-usage.service'),
                ('.config/systemd/user/codex-usage.timer', 'systemd/codex-usage.timer'),
            ]:
                self.assertTrue((home / destination).is_symlink())
                self.assertEqual((home / destination).resolve(), install.ROOT / source)
            environment = home / '.config/codex-usage/environment'
            self.assertIn('Codex Profile', environment.read_text())
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            install.install(home, home / 'Codex Profile')

    def test_existing_regular_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            path = home / '.local/bin/codex-usage'
            path.parent.mkdir(parents=True)
            path.write_text('Existing unrelated command')
            with self.assertRaises(RuntimeError):
                install.install(home, home / '.codex')
            self.assertEqual(path.read_text(), 'Existing unrelated command')

    def test_environment_value_quoting(self):
        self.assertEqual(install.env_quote('a "b" \\ c'), '"a \\"b\\" \\\\ c"')
        self.assertEqual(install.env_quote('$ `profile`'), '"\\$ `profile`"')
        with self.assertRaises(ValueError):
            install.env_quote('bad\npath')

    def test_failed_environment_updates_preserve_existing_file_and_clean_up(self):
        factory = tempfile.NamedTemporaryFile
        for failure in ['write', 'flush', 'replace']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                install.install(home, home / 'old-profile', home / 'old-sqlite')
                config = home / '.config/codex-usage'
                environment = config / 'environment'
                original = environment.read_bytes()

                def temporary_file(*args, **kwargs):
                    output = factory(*args, **kwargs)
                    write = output.write

                    def failed_write(value):
                        write(value[:5])
                        raise OSError('injected partial write failure')

                    def failed_flush():
                        raise OSError('injected flush failure')

                    if failure == 'write':
                        output.write = failed_write
                    elif failure == 'flush':
                        output.flush = failed_flush
                    return output

                if failure == 'replace':
                    failing_operation = patch.object(install.os, 'replace', side_effect=OSError('injected replace failure'))
                else:
                    failing_operation = patch.object(install.tempfile, 'NamedTemporaryFile', side_effect=temporary_file)
                with failing_operation, self.assertRaises(OSError):
                    install.install(home, home / 'new-profile', home / 'new-sqlite')
                self.assertEqual(environment.read_bytes(), original)
                self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(config.glob('.environment.*')), [])

    def test_environment_replacement_is_private_and_complete(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            install.install(home, home / 'old-profile')
            environment = home / '.config/codex-usage/environment'
            environment.chmod(0o644)
            install.install(home, home / 'new $ `profile`', home / 'new-sqlite')
            self.assertEqual(environment.read_text(),
                             'CODEX_HOME="' + str(home / 'new \\$ `profile`') + '"\n'
                             'CODEX_SQLITE_HOME="' + str(home / 'new-sqlite') + '"\n')
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(environment.parent.glob('.environment.*')), [])

    def test_preserves_separate_linux_sqlite_path(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            install.install(home, home / 'profile', home / 'linux-sqlite')
            environment = (home / '.config/codex-usage/environment').read_text()
            self.assertIn('CODEX_SQLITE_HOME="' + str(home / 'linux-sqlite') + '"', environment)

    def test_environment_directory_is_synced_after_replacement(self):
        sync = os.fsync
        synced = []
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            environment = home / '.config/codex-usage/environment'

            def observe_sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    self.assertIn(str(home / 'profile'), environment.read_text())
                    self.assertEqual(os.fstat(fd).st_ino, environment.parent.stat().st_ino)
                    synced.append('directory')
                else:
                    synced.append('file')
                sync(fd)

            with patch.object(install.os, 'fsync', side_effect=observe_sync):
                install.install(home, home / 'profile')
            self.assertEqual(synced, ['file', 'directory'])

    def test_initial_read_failure_preserves_installation_and_shows_guidance(self):
        for returncode in [0, 1]:
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as name:
                home = Path(name)
                output = io.StringIO()
                results = [subprocess.CompletedProcess([], code) for code in [0, 0, returncode]]
                with patch.object(Path, 'home', return_value=home), \
                        patch.object(install.subprocess, 'run', side_effect=results) as run, \
                        redirect_stdout(output):
                    install.main(['--codex-home', str(home / 'profile')])
                self.assertTrue((home / '.local/bin/codex-usage').is_symlink())
                self.assertTrue((home / '.config/codex-usage/environment').exists())
                calls = run.call_args_list
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[0].args[0][-1], 'daemon-reload')
                self.assertTrue(calls[0].kwargs['check'])
                self.assertEqual(calls[0].kwargs['timeout'], 60)
                self.assertEqual(calls[1].args[0][-1], 'codex-usage.timer')
                self.assertTrue(calls[1].kwargs['check'])
                self.assertEqual(calls[1].kwargs['timeout'], 60)
                self.assertEqual(calls[2].args[0][-1], 'codex-usage.service')
                self.assertFalse(calls[2].kwargs['check'])
                self.assertEqual(calls[2].kwargs['timeout'], 90)
                self.assertIn('Enabled: readings every 15 minutes', output.getvalue())
                self.assertEqual('first reading failed' in output.getvalue(), returncode != 0)
                if returncode:
                    self.assertIn('codex-usage --status', output.getvalue())
                    self.assertIn('codex login status', output.getvalue())

    def test_initial_read_timeout_preserves_installation_and_shows_guidance(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            output = io.StringIO()
            results = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                       subprocess.TimeoutExpired(['systemctl', '--user', 'start', 'codex-usage.service'], 90)]
            with patch.object(Path, 'home', return_value=home), \
                    patch.object(install.subprocess, 'run', side_effect=results) as run, \
                    redirect_stdout(output):
                install.main(['--codex-home', str(home / 'profile')])
            self.assertEqual(run.call_args.kwargs['timeout'], 90)
            self.assertTrue((home / '.local/bin/codex-usage').is_symlink())
            self.assertTrue((home / '.config/codex-usage/environment').exists())
            self.assertIn('failed or timed out', output.getvalue())
            self.assertIn('codex-usage --status', output.getvalue())
            self.assertIn('codex login status', output.getvalue())
            self.assertIn('Enabled: readings every 15 minutes', output.getvalue())

    def test_service_setup_errors_still_stop_before_sampling(self):
        for failed_step in [0, 1]:
            for error in [subprocess.CalledProcessError(1, ['systemctl']),
                          subprocess.TimeoutExpired(['systemctl'], 60)]:
                with self.subTest(failed_step=failed_step, error=type(error).__name__), \
                        tempfile.TemporaryDirectory() as name:
                    home = Path(name)
                    results = [subprocess.CompletedProcess([], 0)] * failed_step
                    results.append(error)
                    with patch.object(Path, 'home', return_value=home), \
                            patch.object(install.subprocess, 'run', side_effect=results) as run, \
                            redirect_stdout(io.StringIO()), self.assertRaises(type(error)):
                        install.main(['--codex-home', str(home / 'profile')])
                    self.assertEqual(run.call_count, failed_step + 1)


if __name__ == '__main__':
    unittest.main()
