from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
