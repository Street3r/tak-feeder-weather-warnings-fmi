# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the shared helpers (same file in every fork)."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from tak_feeder_weather_warnings_fmi import common


class EnvTest(unittest.TestCase):
    def test_legacy_aliases(self) -> None:
        env = {"CLIENT_CERT": "/c/cert.pem", "REMOTE_SERVER_URL": "ssl://tak:8089",
               "REMOTE_SSL_USER_KEY": "/c/key.pem"}
        tak = common.TakSettings.from_env(env)
        self.assertEqual(tak.cot_url, "ssl://tak:8089")
        self.assertEqual(tak.tls_client_cert, "/c/cert.pem")
        self.assertEqual(tak.tls_client_key, "/c/key.pem")

    def test_new_name_wins(self) -> None:
        env = {"COT_URL": "ssl://new:8089", "REMOTE_SERVER_URL": "ssl://old:8089"}
        self.assertEqual(common.TakSettings.from_env(env).cot_url, "ssl://new:8089")

    def test_false_flags_not_passed_to_pytak(self) -> None:
        cfg = common.TakSettings.from_env({"PYTAK_TLS_DONT_VERIFY": "0"}).pytak_config()
        self.assertNotIn("PYTAK_TLS_DONT_VERIFY", cfg)
        cfg = common.TakSettings.from_env({"PYTAK_TLS_DONT_VERIFY": "1"}).pytak_config()
        self.assertEqual(cfg["PYTAK_TLS_DONT_VERIFY"], "1")

    def test_numbers_and_lists(self) -> None:
        env = {"A": "2.5", "B": "x", "L": " a, ,b "}
        self.assertEqual(common.env_float("A", 1.0, environ=env), 2.5)
        self.assertEqual(common.env_float("B", 1.0, environ=env), 1.0)
        self.assertEqual(common.env_list("L", environ=env), ["a", "b"])


class HealthTest(unittest.TestCase):
    def test_flag_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "unhealthy")
            health = common.Health(path=path, max_errors=2, max_silence=0)
            health.error()
            self.assertFalse(os.path.exists(path))
            health.error()
            self.assertTrue(os.path.exists(path))
            health.ok()
            self.assertFalse(os.path.exists(path))

    def test_silence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "unhealthy")
            health = common.Health(path=path, max_errors=3, max_silence=10)
            health.last_data -= 11
            health.check_silence()
            self.assertTrue(os.path.exists(path))


class CotTest(unittest.TestCase):
    def test_event(self) -> None:
        now = datetime(2026, 10, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)
        xml = common.cot_event("u1", "a-f-G", 60.1, 24.9, "CALL", "text", stale=60,
                               course=370.0, speed=None, now=now)
        root = ET.fromstring(xml)
        self.assertEqual(root.get("time"), "2026-10-13T12:00:00.123Z")
        self.assertEqual(root.get("stale"), "2026-10-13T12:01:00.123Z")
        self.assertEqual(root.find("detail/contact").get("callsign"), "CALL")
        self.assertEqual(root.find("detail/track").get("course"), "10.0")
        self.assertEqual(root.find("detail/track").get("speed"), "0.00")
        self.assertEqual(root.find("detail/remarks").text, "text")

    def test_remarks(self) -> None:
        text = common.remarks_text(["a", None, "b"], "Source: x", ("HSL", "#bus", ""))
        self.assertEqual(text, "a\nb\nSource: x\n#HSL #bus")

    def test_as_float(self) -> None:
        self.assertIsNone(common.as_float(None))
        self.assertEqual(common.as_float(None, 0.0), 0.0)
        self.assertEqual(common.as_float("nan", 1.0), 1.0)
        self.assertEqual(common.as_float("2"), 2.0)


if __name__ == "__main__":
    unittest.main()
