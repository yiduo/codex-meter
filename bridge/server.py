#!/usr/bin/env python3
"""Expose local Codex usage counters as a tiny LAN-only JSON API."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass
class SessionSummary:
    timestamp: float = 0
    tokens: dict[str, int] = field(default_factory=dict)
    today_tokens: dict[str, int] = field(default_factory=dict)
    week_tokens: dict[str, int] = field(default_factory=dict)
    rate_limits: dict[str, Any] | None = None
    rate_limits_timestamp: float = 0


class UsageReader:
    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home
        self._cache: dict[Path, tuple[int, int, str, SessionSummary]] = {}
        self._lock = threading.Lock()

    def _paths(self) -> list[Path]:
        paths = list((self.codex_home / "sessions").glob("**/*.jsonl"))
        paths.extend((self.codex_home / "archived_sessions").glob("*.jsonl"))
        return paths

    @staticmethod
    def _parse_timestamp(value: Any, fallback: float) -> float:
        if not isinstance(value, str):
            return fallback
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return fallback

    @staticmethod
    def _reverse_lines(path: Path):
        """Yield JSONL records from newest to oldest without loading the file."""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            remainder = b""
            while position > 0:
                read_size = min(65536, position)
                position -= read_size
                handle.seek(position)
                parts = (handle.read(read_size) + remainder).split(b"\n")
                remainder = parts[0]
                yield from (line for line in reversed(parts[1:]) if line)
            if remainder:
                yield remainder

    @classmethod
    def _read_file(cls, path: Path, today_start: float, week_start: float) -> SessionSummary:
        summary = SessionSummary(timestamp=path.stat().st_mtime)
        found_tokens = False
        recent_events: list[tuple[float, dict[str, int]]] = []
        week_baseline = {name: 0 for name in TOKEN_FIELDS}
        found_week_baseline = False

        for raw_line in cls._reverse_lines(path):
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            payload = record.get("payload", {})
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            totals = info.get("total_token_usage") or {}
            current = {name: int(totals.get(name, 0) or 0) for name in TOKEN_FIELDS}
            timestamp = cls._parse_timestamp(record.get("timestamp"), summary.timestamp)

            if not found_tokens:
                summary.tokens = current
                summary.timestamp = timestamp
                found_tokens = True
            rate_limits = payload.get("rate_limits") or {}
            # `token_count` 也可能携带某个模型的独立额度（如 codex_bengalfox）。
            # 仪表盘展示的是总体 Codex 周额度，只接受 limit_id=codex；旧日志没有
            # limit_id 时仍按总体额度兼容处理。
            limit_id = rate_limits.get("limit_id")
            if summary.rate_limits is None and rate_limits and limit_id in (None, "codex"):
                summary.rate_limits = rate_limits
                summary.rate_limits_timestamp = timestamp

            if timestamp >= week_start:
                recent_events.append((timestamp, current))
            elif not found_week_baseline:
                week_baseline = current
                found_week_baseline = True

            if found_tokens and summary.rate_limits is not None and found_week_baseline:
                break

        summary.today_tokens = {name: 0 for name in TOKEN_FIELDS}
        summary.week_tokens = {name: 0 for name in TOKEN_FIELDS}
        previous = week_baseline
        for timestamp, current in reversed(recent_events):
            for name in TOKEN_FIELDS:
                change = current[name] - previous[name]
                change = change if change >= 0 else current[name]
                summary.week_tokens[name] += change
                if timestamp >= today_start:
                    summary.today_tokens[name] += change
            previous = current
        return summary

    def _summaries(
        self, today_start: float, week_start: float, date_key: str
    ) -> list[SessionSummary]:
        paths = self._paths()
        active = set(paths)
        for cached_path in set(self._cache) - active:
            del self._cache[cached_path]
        summaries: list[SessionSummary] = []
        for path in paths:
            try:
                stat = path.stat()
                cached = self._cache.get(path)
                signature = (stat.st_mtime_ns, stat.st_size, date_key)
                if cached is None or cached[:3] != signature:
                    self._cache[path] = (
                        *signature,
                        self._read_file(path, today_start, week_start),
                    )
                summaries.append(self._cache[path][3])
            except (FileNotFoundError, OSError):
                continue
        return [summary for summary in summaries if summary.tokens]

    @staticmethod
    def _window_label(minutes: int) -> str:
        if minutes and minutes % 10080 == 0:
            weeks = minutes // 10080
            return "WEEKLY" if weeks == 1 else f"{weeks} WEEKS"
        if minutes and minutes % 1440 == 0:
            return f"{minutes // 1440} DAY"
        if minutes and minutes % 60 == 0:
            return f"{minutes // 60} HOUR"
        return "LIMIT"

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            now = now or time.time()
            local_now = datetime.fromtimestamp(now).astimezone()
            today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            # 今天加前 6 个完整自然日，和设备上的 “7 DAYS” 文案一致。
            week_start = today_start - timedelta(days=6).total_seconds()
            summaries = self._summaries(
                today_start, week_start, local_now.date().isoformat()
            )
            if not summaries:
                return {
                    "valid": False,
                    "error": "No Codex token_count events found",
                    "updated_at": int(now),
                }

            latest = max(summaries, key=lambda item: item.timestamp)
            today_breakdown = {
                name: sum(item.today_tokens.get(name, 0) for item in summaries)
                for name in TOKEN_FIELDS
            }
            today_tokens = today_breakdown["total_tokens"]
            week_tokens = sum(item.week_tokens.get("total_tokens", 0) for item in summaries)

            rate_summary = max(
                (item for item in summaries if item.rate_limits),
                key=lambda item: item.rate_limits_timestamp,
                default=None,
            )
            primary = ((rate_summary.rate_limits or {}).get("primary") or {}) if rate_summary else {}
            window_minutes = int(primary.get("window_minutes", 0) or 0)
            has_limit = "used_percent" in primary

            result: dict[str, Any] = {
                "valid": has_limit,
                "limit_id": (rate_summary.rate_limits or {}).get("limit_id", "codex")
                if rate_summary
                else "",
                "used_percent": float(primary.get("used_percent", 0) or 0),
                "window_minutes": window_minutes,
                "window_label": self._window_label(window_minutes),
                "resets_at": int(primary.get("resets_at", 0) or 0),
                "session_tokens": latest.tokens.get("total_tokens", 0),
                "today_tokens": today_tokens,
                "today_input_tokens": today_breakdown["input_tokens"],
                "today_cached_input_tokens": today_breakdown["cached_input_tokens"],
                "today_output_tokens": today_breakdown["output_tokens"],
                "today_reasoning_output_tokens": today_breakdown["reasoning_output_tokens"],
                "week_tokens": week_tokens,
                "updated_at": int(now),
            }
            result.update({name: latest.tokens.get(name, 0) for name in TOKEN_FIELDS})
            return result


class DashboardHandler(BaseHTTPRequestHandler):
    reader: UsageReader
    api_key: str

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/api/usage":
            self._json(404, {"error": "not found"})
            return
        if self.api_key and not hmac.compare_digest(
            self.headers.get("X-Dashboard-Key", ""), self.api_key
        ):
            self._json(401, {"error": "unauthorized"})
            return
        self._json(200, self.reader.snapshot())

    def log_message(self, message: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {message % args}")


def make_server(host: str, port: int, reader: UsageReader, api_key: str) -> ThreadingHTTPServer:
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"reader": reader, "api_key": api_key},
    )
    return ThreadingHTTPServer((host, port), handler)


def parse_args() -> argparse.Namespace:
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all LAN interfaces)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--codex-home", type=Path, default=default_home)
    parser.add_argument("--api-key", default=os.environ.get("CODEX_DASHBOARD_KEY", ""))
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reader = UsageReader(args.codex_home.expanduser())
    if args.once:
        print(json.dumps(reader.snapshot(), ensure_ascii=False, indent=2))
        return
    server = make_server(args.host, args.port, reader, args.api_key)
    print(f"Codex dashboard bridge: http://{args.host}:{args.port}/api/usage")
    print(f"Reading token counters from: {reader.codex_home}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bridge")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
