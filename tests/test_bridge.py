import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bridge.server import UsageReader
from bridge.ble_sender import build_payload


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


if __name__ == "__main__":
    unittest.main()
