from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import uptime_kuma_push as kuma


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps({"ok": True}).encode()


class KumaPushTests(unittest.TestCase):
    def test_build_push_url_replaces_ui_defaults_without_leaking_extra_ping(self) -> None:
        url = kuma.build_push_url(
            "https://kuma.example/api/push/secret?status=down&msg=old&ping=123",
            message="cycle ok",
        )
        self.assertIn("status=up", url)
        self.assertIn("msg=cycle+ok", url)
        self.assertNotIn("ping=", url)

    def test_invalid_url_is_rejected(self) -> None:
        with self.assertRaises(kuma.KumaPushError):
            kuma.build_push_url("https://kuma.example/status")

    def test_missing_url_is_optional_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(0, kuma.main([]))

    def test_push_requires_kuma_ok_response(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=_Response()):
            kuma.push("https://kuma.example/api/push/secret", message="ok")


if __name__ == "__main__":
    unittest.main()
