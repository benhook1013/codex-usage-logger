"""Regression coverage for saved installer defaults and explicit overrides."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install
from src import codex_usage


class ReinstallDefaultsTests(unittest.TestCase):
    def test_reinstall_preserves_saved_paths_and_cli_outside_discovery(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            profile, database = home / 'profile', home / 'database'
            profile.mkdir()
            database.mkdir()
            executable = home / 'custom-codex'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)
            with patch.dict(os.environ, {'PATH': ''}, clear=True), patch.object(Path, 'home', return_value=home):
                install.install(home, profile, database, str(executable))
                install.install(home)
                self.assertEqual(codex_usage.installed_environment(), {
                    'CODEX_HOME': str(profile), 'CODEX_SQLITE_HOME': str(database),
                    'CODEX_USAGE_CODEX': str(executable)})

    def test_environment_then_explicit_paths_override_saved_defaults(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            for prefix in ['saved', 'environment', 'explicit']:
                (home / (prefix + '-profile')).mkdir()
                (home / (prefix + '-db')).mkdir()
                executable = home / (prefix + '-codex')
                executable.write_text('#!/bin/sh\nexit 0\n')
                executable.chmod(0o700)
            with patch.dict(os.environ, {'PATH': ''}, clear=True):
                install.install(home, home / 'saved-profile', home / 'saved-db', str(home / 'saved-codex'))
            environment = {'CODEX_HOME': str(home / 'environment-profile'),
                           'CODEX_SQLITE_HOME': str(home / 'environment-db'),
                           'CODEX_USAGE_CODEX': str(home / 'environment-codex')}
            with patch.dict(os.environ, environment, clear=True):
                install.install(home)
                self.assertEqual(codex_usage.installed_environment(home), environment)
                install.install(home, home / 'explicit-profile', home / 'explicit-db', str(home / 'explicit-codex'))
            self.assertEqual(codex_usage.installed_environment(home), {
                'CODEX_HOME': str(home / 'explicit-profile'),
                'CODEX_SQLITE_HOME': str(home / 'explicit-db'),
                'CODEX_USAGE_CODEX': str(home / 'explicit-codex')})

    def test_stale_saved_profiles_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(install, 'codex_command', return_value=(['/test/codex'], {})):
                install.install(home, home / 'missing-profile', home / 'missing-db')
                install.install(home)
            saved = codex_usage.installed_environment(home)
            self.assertEqual(saved['CODEX_HOME'], str(home / '.codex'))
            self.assertNotIn('CODEX_SQLITE_HOME', saved)
