from pathlib import Path
import tempfile
import unittest

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
        with self.assertRaises(ValueError):
            install.env_quote('bad\npath')

    def test_preserves_separate_linux_sqlite_path(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            install.install(home, home / 'profile', home / 'linux-sqlite')
            environment = (home / '.config/codex-usage/environment').read_text()
            self.assertIn('CODEX_SQLITE_HOME="' + str(home / 'linux-sqlite') + '"', environment)


if __name__ == '__main__':
    unittest.main()
