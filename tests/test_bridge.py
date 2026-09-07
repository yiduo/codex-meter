import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.codex_limits import extract_codex_limit
from bridge.server import UsageReader
from bridge.ble_sender import (
    apply_unavailable_limit,
    build_payload,
    cycle_delay,
    limit_is_current,
    load_cached_limit,
    operation_timeout,
    recover_macos_bluetooth,
    save_cached_limit,
)


def token_event(
    timestamp: str, total: int, used_percent: float, limit_id: str = "codex"
) -> str:
    payload = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total - 20,
                    "cached_input_tokens": total // 2,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 10,
                    "total_tokens": total,
                }
            },
            "rate_limits": {
                "limit_id": limit_id,
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10080,
                    "resets_at": 2_000_000_000,
                }
            },
        },
    }
    return json.dumps(payload)


class UsageReaderTest(unittest.TestCase):
    def test_snapshot_uses_latest_limit_and_aggregates_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions" / "2026" / "08" / "06"
            sessions.mkdir(parents=True)
            first = sessions / "one.jsonl"
            second = sessions / "two.jsonl"
            first.write_text(token_event("2026-08-06T01:00:00Z", 100, 25), encoding="utf-8")
            second.write_text(token_event("2026-08-06T02:00:00Z", 250, 42), encoding="utf-8")

            now = datetime(2026, 8, 6, 3, tzinfo=timezone.utc).timestamp()
            result = UsageReader(root).snapshot(now=now)

            self.assertTrue(result["valid"])
            self.assertEqual(result["limit_id"], "codex")
            self.assertEqual(result["used_percent"], 42)
            self.assertEqual(result["session_tokens"], 250)
            self.assertEqual(result["today_tokens"], 350)
            self.assertEqual(result["today_input_tokens"], 310)
            self.assertEqual(result["today_output_tokens"], 20)
            self.assertEqual(result["week_tokens"], 350)
            self.assertEqual(result["window_label"], "WEEKLY")

    def test_today_uses_cumulative_delta_not_session_lifetime_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions" / "2026" / "08" / "05"
            sessions.mkdir(parents=True)
            session = sessions / "continued.jsonl"
            session.write_text(
                token_event("2026-08-05T15:00:00Z", 100, 20)
                + "\n"
                + token_event("2026-08-06T01:00:00Z", 250, 30),
                encoding="utf-8",
            )

            now = datetime(2026, 8, 6, 3, tzinfo=timezone.utc).timestamp()
            result = UsageReader(root).snapshot(now=now)

            self.assertEqual(result["today_tokens"], 150)
            self.assertEqual(result["week_tokens"], 250)

    def test_overall_codex_limit_wins_over_newer_model_specific_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sessions = root / "sessions" / "2026" / "08" / "07"
            sessions.mkdir(parents=True)
            session = sessions / "mixed-limits.jsonl"
            session.write_text(
                token_event("2026-08-07T01:00:00Z", 100, 74)
                + "\n"
                + token_event(
                    "2026-08-07T02:00:00Z", 200, 0, limit_id="codex_bengalfox"
                ),
                encoding="utf-8",
            )

            now = datetime(2026, 8, 7, 3, tzinfo=timezone.utc).timestamp()
            result = UsageReader(root).snapshot(now=now)

            self.assertTrue(result["valid"])
            self.assertEqual(result["used_percent"], 74)

    def test_empty_home_returns_explicit_partial_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = UsageReader(Path(temp_dir)).snapshot(now=1_000)
            self.assertFalse(result["valid"])
            self.assertIn("error", result)

    def test_ble_payload_is_compact_terminated_and_contains_key(self) -> None:
        payload = build_payload(
            {
                "valid": True,
                "limit_id": "codex",
                "used_percent": 42,
                "updated_at": 1234,
            },
            "secret",
        )
        self.assertTrue(payload.endswith(b"\n"))
        decoded = json.loads(payload)
        self.assertEqual(decoded["used_percent"], 42)
        self.assertEqual(decoded["limit_id"], "codex")
        self.assertEqual(decoded["key"], "secret")
        self.assertLess(len(payload), 512)

    def test_cycle_delay_keeps_start_to_start_interval(self) -> None:
        self.assertEqual(cycle_delay(60, 15), 45)
        self.assertEqual(cycle_delay(60, 61), 1)
        self.assertEqual(cycle_delay(1, 0), 5)

    def test_operation_timeout_covers_scan_and_connection(self) -> None:
        self.assertEqual(operation_timeout(15), 35)
        self.assertEqual(operation_timeout(2), 20)

    def test_live_limit_cache_rejects_expired_and_invalid_values(self) -> None:
        current = {
            "valid": True,
            "limit_id": "codex",
            "used_percent": 4,
            "resets_at": 2_000,
        }
        self.assertTrue(limit_is_current(current, now=1_000))
        self.assertFalse(limit_is_current(current, now=2_000))
        self.assertFalse(limit_is_current({**current, "used_percent": 101}, now=1_000))
        self.assertFalse(
            limit_is_current({**current, "limit_id": "codex_bengalfox"}, now=1_000)
        )

    def test_live_limit_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "limit.json"
            current = {
                "valid": True,
                "limit_id": "codex",
                "used_percent": 4.0,
                "window_label": "WEEKLY",
                "resets_at": 2_000,
            }
            save_cached_limit(path, current)
            self.assertEqual(load_cached_limit(path, now=1_000), current)
            self.assertIsNone(load_cached_limit(path, now=2_000))

    def test_unavailable_limit_keeps_token_totals_without_fake_percentage(self) -> None:
        snapshot = {
            "valid": True,
            "limit_id": "codex",
            "used_percent": 83,
            "resets_at": 900,
            "today_tokens": 123,
        }
        apply_unavailable_limit(snapshot)
        self.assertFalse(snapshot["valid"])
        self.assertEqual(snapshot["used_percent"], 0)
        self.assertEqual(snapshot["resets_at"], 0)
        self.assertEqual(snapshot["today_tokens"], 123)

    def test_live_limit_uses_overall_codex_bucket(self) -> None:
        result = extract_codex_limit(
            {
                "rateLimits": {
                    "limitId": "codex_bengalfox",
                    "primary": {"usedPercent": 0},
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 82,
                            "windowDurationMins": 10080,
                            "resetsAt": 2_000_000_000,
                        },
                    }
                },
            }
        )

        self.assertEqual(
            result,
            {
                "valid": True,
                "limit_id": "codex",
                "used_percent": 82.0,
                "window_minutes": 10080,
                "window_label": "WEEKLY",
                "resets_at": 2_000_000_000,
            },
        )

    def test_live_limit_rejects_model_specific_fallback(self) -> None:
        self.assertIsNone(
            extract_codex_limit(
                {
                    "rateLimits": {
                        "limitId": "codex_bengalfox",
                        "primary": {"usedPercent": 0},
                    }
                }
            )
        )

    @patch("bridge.ble_sender.sys.platform", "darwin")
    @patch("bridge.ble_sender.subprocess.run")
    def test_macos_bluetooth_recovery_restarts_user_agent(self, run: Mock) -> None:
        run.return_value.returncode = 0

        self.assertTrue(recover_macos_bluetooth())
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["launchctl", "kickstart", "-k"])
        self.assertTrue(command[3].endswith("/com.apple.bluetoothuserd"))


if __name__ == "__main__":
    unittest.main()
