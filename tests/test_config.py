from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import ConfigError, Settings
from app import main as entry


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.env = {
            "POSTGRES_DSN": "postgresql://test:test@localhost/test",
            "CLOUDFLARE_API_TOKEN": "fake",
            "CLOUDFLARE_ACCOUNT_ID": "fake-account",
            "D1_DATABASE_ID": "fake-database",
        }

    def test_pull_is_configured_as_one_shot_even_without_sync_once(self) -> None:
        with patch.dict(os.environ, self.env, clear=True):
            settings = Settings.from_env(mode_override="pull")
        self.assertEqual(settings.mode, "pull")
        self.assertFalse(settings.once)

    def test_destructive_confirmation_is_required(self) -> None:
        with patch.dict(os.environ, {**self.env, "SYNC_MODE": "pull"}, clear=True), \
             patch("sys.argv", ["app.main"]):
            self.assertEqual(entry.main(), 2)

    def test_invalid_interval_is_rejected(self) -> None:
        with patch.dict(os.environ, {**self.env, "SYNC_INTERVAL_SECONDS": "-1"}, clear=True):
            with self.assertRaises(ConfigError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
