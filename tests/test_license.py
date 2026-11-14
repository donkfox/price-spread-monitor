from __future__ import annotations

import csv
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from license_server.core import LicenseError, LicenseStore
from monitor.license_client import (
    LicenseClient,
    LicenseRejected,
    LicenseUnavailable,
)


UTC = timezone.utc


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MutableMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta.total_seconds()


class LicenseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = MutableClock(datetime(2026, 8, 4, 8, 0, tzinfo=UTC))
        self.monotonic = MutableMonotonic()
        self.store = LicenseStore(self.root / "licenses.sqlite3", clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_one_day_trial_code_activates_once(self) -> None:
        code = self.store.create_codes(1, 1, "4.8元体验")[0]
        result = self.store.redeem(code, "device-1234567890123456")

        self.assertIsNotNone(result.token)
        self.assertEqual(result.expires_at, "2026-08-05T08:00:00+00:00")
        with self.assertRaisesRegex(LicenseError, "已经使用"):
            self.store.redeem(code, "other-device-123456789")

    def test_renewal_adds_to_remaining_time_and_requires_original_token(self) -> None:
        first_code = self.store.create_codes(30, 1)[0]
        first = self.store.redeem(first_code, "device-1234567890123456")
        renewal_code = self.store.create_codes(90, 1)[0]

        with self.assertRaisesRegex(LicenseError, "原授权令牌"):
            self.store.redeem(renewal_code, first.device_id, "wrong-token")

        renewed = self.store.redeem(renewal_code, first.device_id, first.token or "")
        self.assertEqual(renewed.expires_at, "2026-12-02T08:00:00+00:00")
        self.assertIsNone(renewed.token)
        self.assertEqual(self.store.check(first.device_id, first.token or "").license_id, first.license_id)

    def test_revoke_and_admin_extension(self) -> None:
        code = self.store.create_codes(30, 1)[0]
        license_result = self.store.redeem(code, "device-1234567890123456")
        self.assertEqual(
            self.store.extend(license_result.license_id, 5),
            "2026-09-08T08:00:00+00:00",
        )
        self.store.set_enabled(license_result.license_id, False)
        with self.assertRaisesRegex(LicenseError, "停用"):
            self.store.check(license_result.device_id, license_result.token or "")

    def test_csv_contains_chain_store_delivery_inventory(self) -> None:
        codes = self.store.create_codes(1, 2)
        output = self.root / "trial.csv"
        self.store.export_codes(output, codes, 1)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["activation_code"] for row in rows], codes)
        self.assertEqual(rows[0]["product_name"], "价差监控 1 天体验")


class LicenseClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = MutableClock(datetime(2026, 8, 4, 8, 0, tzinfo=UTC))
        self.monotonic = MutableMonotonic()
        self.config = {
            "licensing": {
                "enabled": True,
                "server_url": "https://license.example.test",
                "offline_grace_days": 3,
                "verify_interval_hours": 6,
                "state_file": "runtime/license.json",
            }
        }
        self.client = LicenseClient(
            self.config,
            self.root,
            clock=self.clock,
            monotonic=self.monotonic,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_activation_is_bound_locally_and_offline_grace_expires(self) -> None:
        self.client._post = lambda _path, _payload: {
            "ok": True,
            "license_id": 7,
            "device_id": self.client.device_id,
            "token": "server-issued-token",
            "expires_at": "2026-09-03T08:00:00+00:00",
            "server_time": "2026-08-04T08:00:00+00:00",
        }
        active = self.client.activate("ABCD-EFGH-JKLM-NPQR-STUV")
        self.assertTrue(active.can_scan)

        def unavailable(_path, _payload):
            raise LicenseUnavailable("offline")

        self.client._post = unavailable
        self.clock.value += timedelta(days=2)
        self.monotonic.advance(timedelta(days=2))
        grace = self.client.status(force_online=True)
        self.assertTrue(grace.can_scan)
        self.assertTrue(grace.offline)

        self.clock.value += timedelta(days=2)
        self.monotonic.advance(timedelta(days=2))
        expired_grace = self.client.status(force_online=True)
        self.assertFalse(expired_grace.can_scan)
        self.assertEqual(expired_grace.state, "offline_expired")

    def test_server_rejection_cannot_use_offline_grace(self) -> None:
        self.client._post = lambda _path, _payload: {
            "ok": True,
            "license_id": 8,
            "device_id": self.client.device_id,
            "token": "another-server-token",
            "expires_at": "2026-09-03T08:00:00+00:00",
            "server_time": "2026-08-04T08:00:00+00:00",
        }
        self.client.activate("ABCD-EFGH-JKLM-NPQR-STUV")

        def rejected(_path, _payload):
            raise LicenseRejected("该授权已被停用")

        self.client._post = rejected
        status = self.client.status(force_online=True)
        self.assertFalse(status.can_scan)
        self.assertEqual(status.state, "rejected")

    def test_disabled_personal_build_remains_unlocked(self) -> None:
        with patch.dict(os.environ, {"PRICE_MONITOR_COMMERCIAL_BUILD": ""}):
            client = LicenseClient({"licensing": {"enabled": False}}, self.root)
            status = client.status(force_online=True)
        self.assertTrue(status.can_scan)
        self.assertEqual(status.state, "disabled")

    def test_commercial_build_fails_closed_when_config_is_missing(self) -> None:
        with patch.dict(os.environ, {"PRICE_MONITOR_COMMERCIAL_BUILD": "1"}):
            client = LicenseClient({}, self.root)
            status = client.status(force_online=True)
        self.assertFalse(status.can_scan)
        self.assertEqual(status.state, "inactive")

    def test_commercial_build_ignores_replaced_server_and_ca(self) -> None:
        with patch.dict(os.environ, {"PRICE_MONITOR_COMMERCIAL_BUILD": "1"}):
            client = LicenseClient(
                {
                    "licensing": {
                        "enabled": False,
                        "server_url": "https://attacker.invalid",
                        "ca_cert_file": "attacker.pem",
                        "state_file": "attacker-license.json",
                    }
                },
                self.root,
            )
        self.assertEqual(client.server_url, "https://example.com")
        self.assertIsNotNone(client.ca_cert_data)
        self.assertIsNone(client.ca_cert_file)
        self.assertEqual(client.state_file, self.root / "runtime" / "license.json")
        self.assertEqual(client.verify_interval, timedelta(minutes=5))
        self.assertEqual(client.offline_grace, timedelta(minutes=15))

    @unittest.skipUnless(os.name == "nt", "DPAPI is only available on Windows")
    def test_commercial_state_encrypts_token_with_windows_dpapi(self) -> None:
        with patch.dict(os.environ, {"PRICE_MONITOR_COMMERCIAL_BUILD": "1"}):
            client = LicenseClient({}, self.root)
            client._save_state(
                {
                    "device_id": client.device_id,
                    "license_id": 12,
                    "token": "plain-token-must-not-be-written",
                    "expires_at": "2026-09-03T08:00:00+00:00",
                    "last_verified_at": "2026-08-04T08:00:00+00:00",
                }
            )
            disk_text = client.state_file.read_text(encoding="utf-8")
            loaded = client._load_state()

        self.assertNotIn("plain-token-must-not-be-written", disk_text)
        self.assertNotIn('"token"', disk_text)
        self.assertIn('"token_protected"', disk_text)
        self.assertEqual(loaded["token"], "plain-token-must-not-be-written")

    def test_commercial_build_rejects_plaintext_or_damaged_token_state(self) -> None:
        with patch.dict(os.environ, {"PRICE_MONITOR_COMMERCIAL_BUILD": "1"}):
            client = LicenseClient({}, self.root)
            client.state_file.parent.mkdir(parents=True)
            client.state_file.write_text(
                '{"device_id": "'
                + client.device_id
                + '", "token": "stolen-token", "expires_at": "2099-01-01T00:00:00+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(client._load_state(), {})
            client.state_file.write_text(
                '{"device_id": "'
                + client.device_id
                + '", "token_protected": "not-valid-dpapi", "expires_at": "2099-01-01T00:00:00+00:00"}',
                encoding="utf-8",
            )
            self.assertEqual(client._load_state(), {})

    def test_tampered_cached_state_never_allows_a_new_process_to_scan(self) -> None:
        self.client._post = lambda _path, _payload: {
            "ok": True,
            "license_id": 9,
            "device_id": self.client.device_id,
            "token": "trusted-server-token",
            "expires_at": "2026-09-03T08:00:00+00:00",
            "server_time": "2026-08-04T08:00:00+00:00",
        }
        self.client.activate("ABCD-EFGH-JKLM-NPQR-STUV")
        state = self.client._load_state()
        state["expires_at"] = "2099-01-01T00:00:00+00:00"
        state["last_verified_at"] = "2099-01-01T00:00:00+00:00"
        self.client._save_state(state)

        # The running process keeps its in-memory server result, not disk edits.
        current = self.client.status(online=False)
        self.assertTrue(current.can_scan)
        self.assertEqual(current.expires_at, "2026-09-03T08:00:00+00:00")

        fresh = LicenseClient(
            self.config,
            self.root,
            clock=self.clock,
            monotonic=self.monotonic,
        )
        cached = fresh.status(online=False)
        self.assertFalse(cached.can_scan)
        self.assertEqual(cached.state, "verification_required")

        def unavailable(_path, _payload):
            raise LicenseUnavailable("offline")

        fresh._post = unavailable
        denied = fresh.status(force_online=True)
        self.assertFalse(denied.can_scan)
        self.assertEqual(denied.state, "offline_expired")

    def test_server_cannot_authorize_a_different_device(self) -> None:
        self.client._post = lambda _path, _payload: {
            "ok": True,
            "license_id": 10,
            "device_id": "different-device-1234567890",
            "token": "server-issued-token",
            "expires_at": "2026-09-03T08:00:00+00:00",
            "server_time": "2026-08-04T08:00:00+00:00",
        }
        with self.assertRaisesRegex(LicenseRejected, "设备授权不匹配"):
            self.client.activate("ABCD-EFGH-JKLM-NPQR-STUV")

    def test_wall_clock_rollback_cannot_extend_in_memory_expiry(self) -> None:
        self.client._post = lambda _path, _payload: {
            "ok": True,
            "license_id": 11,
            "device_id": self.client.device_id,
            "token": "short-lived-token",
            "expires_at": "2026-08-05T08:00:00+00:00",
            "server_time": "2026-08-04T08:00:00+00:00",
        }
        self.client.activate("ABCD-EFGH-JKLM-NPQR-STUV")
        self.clock.value -= timedelta(days=30)
        self.monotonic.advance(timedelta(days=1, seconds=1))

        expired = self.client.status(online=False)
        self.assertFalse(expired.can_scan)
        self.assertEqual(expired.state, "expired")

    def test_missing_pinned_certificate_is_reported_before_network_access(self) -> None:
        client = LicenseClient(
            {
                "licensing": {
                    "enabled": True,
                    "server_url": "https://example.com",
                    "ca_cert_file": "assets/missing.pem",
                }
            },
            self.root,
        )

        with self.assertRaisesRegex(LicenseUnavailable, "证书文件缺失"):
            client._post("/v1/check", {"device_id": "x" * 64, "token": "y" * 32})


if __name__ == "__main__":
    unittest.main()
