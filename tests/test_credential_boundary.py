from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_monitor.credentials import credential_path


class CredentialBoundaryTests(unittest.TestCase):
    def test_systemd_credential_precedes_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            systemd = Path(temp) / "telegram.env"
            systemd.write_text("TOKEN=test\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": temp}, clear=False):
                self.assertEqual(
                    systemd,
                    credential_path("telegram.env", "/legacy/telegram.env"),
                )

    def test_missing_systemd_credential_uses_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": temp}, clear=False):
                self.assertEqual(
                    Path("/legacy/telegram.env"),
                    credential_path("telegram.env", "/legacy/telegram.env"),
                )

    def test_monitor_unit_uses_native_config_and_vault_telegram(self) -> None:
        unit = (
            Path(__file__).resolve().parents[1]
            / "systemd"
            / "codex-usage-monitor.service"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "LoadCredential=codex-usage-monitor.env:/home/daniele/.config/codex-usage-monitor/codex-usage-monitor.env",
            unit,
        )
        self.assertNotIn("LoadCredential=telegram.env:", unit)
        self.assertIn("ExecStartPre=/home/daniele/.local/bin/bws-credential --ref FEDORA_TELEGRAM_ENV", unit)
        self.assertIn("EnvironmentFile=-%t/bitwarden-env/FEDORA_TELEGRAM_ENV--37f94f7e982d.env", unit)
        self.assertNotIn("Environment=TELEGRAM_NOTIFY_CONFIG=", unit)

    def test_human_log_unit_uses_vault_telegram(self) -> None:
        unit = (Path(__file__).resolve().parents[1] / "systemd" / "codex-human-log.service").read_text()
        self.assertNotIn("LoadCredential=telegram.env:", unit)
        self.assertIn("ExecStartPre=/home/daniele/.local/bin/bws-credential --ref FEDORA_TELEGRAM_ENV", unit)
        self.assertIn("EnvironmentFile=-%t/bitwarden-env/FEDORA_TELEGRAM_ENV--37f94f7e982d.env", unit)


if __name__ == "__main__":
    unittest.main()
