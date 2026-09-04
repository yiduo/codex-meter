"""Read live Codex rate limits from the local Codex app-server."""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO


DEFAULT_CODEX_APP_CLI = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def _window_label(minutes: int) -> str:
    if minutes and minutes % 10080 == 0:
        weeks = minutes // 10080
        return "WEEKLY" if weeks == 1 else f"{weeks} WEEKS"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440} DAY"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} HOUR"
    return "LIMIT"


def resolve_codex_cli(configured: Path | None = None) -> Path | None:
    if configured:
        candidate = configured.expanduser()
        return candidate if candidate.is_file() else None
    environment_path = os.environ.get("CODEX_CLI")
    if environment_path:
        candidate = Path(environment_path).expanduser()
        if candidate.is_file():
            return candidate
    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered)
    if DEFAULT_CODEX_APP_CLI.is_file():
        return DEFAULT_CODEX_APP_CLI
    return None


def extract_codex_limit(result: dict[str, Any]) -> dict[str, Any] | None:
    limits_by_id = result.get("rateLimitsByLimitId") or {}
    limit = limits_by_id.get("codex") or result.get("rateLimits") or {}
    if limit.get("limitId") != "codex":
        return None
    primary = limit.get("primary") or {}
    if "usedPercent" not in primary:
        return None
    window_minutes = int(primary.get("windowDurationMins", 0) or 0)
    return {
        "valid": True,
        "limit_id": "codex",
        "used_percent": float(primary["usedPercent"]),
        "window_minutes": window_minutes,
        "window_label": _window_label(window_minutes),
        "resets_at": int(primary.get("resetsAt", 0) or 0),
    }


def _send(stream: BinaryIO, message: dict[str, Any]) -> None:
    stream.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    stream.flush()


def _receive(
    stream: BinaryIO, response_id: int, deadline: float, buffer: bytearray
) -> dict[str, Any]:
    while True:
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            if not raw_line:
                continue
            message = json.loads(raw_line)
            if message.get("id") == response_id:
                if "error" in message:
                    raise RuntimeError(f"Codex app-server 返回错误：{message['error']}")
                return message
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError("Codex 实时额度接口超时")
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            raise RuntimeError("Codex app-server 提前退出")
        buffer.extend(chunk)


def read_live_codex_limit(codex_cli: Path, timeout: float = 10) -> dict[str, Any] | None:
    process = subprocess.Popen(
        [str(codex_cli), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise RuntimeError("无法连接 Codex app-server")
    deadline = time.monotonic() + max(timeout, 1)
    buffer = bytearray()
    try:
        _send(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-meter", "version": "1.0.0"}
                },
            },
        )
        _receive(process.stdout, 1, deadline, buffer)
        _send(process.stdin, {"method": "initialized"})
        _send(process.stdin, {"id": 2, "method": "account/rateLimits/read"})
        response = _receive(process.stdout, 2, deadline, buffer)
        return extract_codex_limit(response.get("result") or {})
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
