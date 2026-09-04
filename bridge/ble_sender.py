#!/usr/bin/env python3
"""Continuously push local Codex usage counters to a StickS3 over BLE."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from bridge.codex_limits import read_live_codex_limit, resolve_codex_cli
    from bridge.server import UsageReader
except ModuleNotFoundError:  # 兼容 `python bridge/ble_sender.py` 直接运行。
    from codex_limits import read_live_codex_limit, resolve_codex_cli
    from server import UsageReader


SERVICE_UUID = "7d6a1000-8f3b-4b6d-9f6a-4d3558438d01"
DATA_UUID = "7d6a1001-8f3b-4b6d-9f6a-4d3558438d01"
STATUS_UUID = "7d6a1002-8f3b-4b6d-9f6a-4d3558438d01"
LOCK_PATH = Path("/tmp/codex-meter-ble-sender.lock")
PAYLOAD_FIELDS = (
    "valid",
    "limit_id",
    "used_percent",
    "window_label",
    "resets_at",
    "session_tokens",
    "today_tokens",
    "today_input_tokens",
    "today_cached_input_tokens",
    "today_output_tokens",
    "today_reasoning_output_tokens",
    "week_tokens",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "updated_at",
)


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def operation_timeout(connection_timeout: float) -> float:
    """Allow one scan and one connection, but never hang indefinitely."""
    return max(connection_timeout * 2 + 5, 20)


def cycle_delay(interval: float, elapsed: float) -> float:
    """Keep attempts on a start-to-start cadence with a short failure backoff."""
    return max(max(interval, 5) - elapsed, 1)


def recover_macos_bluetooth() -> bool:
    """Restart the per-user Bluetooth agent after CoreBluetooth becomes unavailable."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            [
                "launchctl",
                "kickstart",
                "-k",
                f"gui/{os.getuid()}/com.apple.bluetoothuserd",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def acquire_sender_lock():
    lock = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.seek(0)
        owner = lock.read().strip() or "unknown"
        lock.close()
        raise RuntimeError(f"蓝牙同步已在运行（PID {owner}）") from exc
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def build_payload(snapshot: dict[str, Any], shared_key: str = "") -> bytes:
    payload = {name: snapshot.get(name, False if name == "valid" else 0) for name in PAYLOAD_FIELDS}
    payload["window_label"] = snapshot.get("window_label", "LIMIT")
    if shared_key:
        payload["key"] = shared_key
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


async def find_device(name: str, timeout: float):
    from bleak import BleakScanner

    def matches(device, advertisement_data) -> bool:
        advertised_name = advertisement_data.local_name or device.name
        # CoreBluetooth 不保证每次扫描都在 advertisement_data 中返回 128 位服务 UUID。
        # 发现阶段按唯一设备名匹配；连接后的特征 UUID与设备确认负责最终校验。
        return advertised_name == name

    return await BleakScanner.find_device_by_filter(matches, timeout=timeout)


async def push_snapshot(
    snapshot: dict[str, Any], device_name: str, shared_key: str, timeout: float, chunk_size: int
) -> None:
    from bleak import BleakClient

    device = await find_device(device_name, timeout)
    if device is None:
        raise RuntimeError(f"未找到蓝牙设备 {device_name!r}")
    payload = build_payload(snapshot, shared_key)
    async with BleakClient(device, timeout=timeout) as client:
        for offset in range(0, len(payload), chunk_size):
            await client.write_gatt_char(DATA_UUID, payload[offset : offset + chunk_size], response=True)
        await asyncio.sleep(0.2)
        acknowledgement = bytes(await client.read_gatt_char(STATUS_UUID)).decode(errors="replace")
    expected = str(int(snapshot.get("updated_at", 0) or 0))
    if acknowledgement != expected:
        raise RuntimeError(f"设备未确认数据：期望 {expected}，收到 {acknowledgement!r}")


def parse_args() -> argparse.Namespace:
    default_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="CodexMeter", help="StickS3 BLE 广播名称")
    parser.add_argument("--codex-home", type=Path, default=default_home)
    parser.add_argument("--codex-cli", type=Path, help="Codex CLI 路径（默认自动查找）")
    parser.add_argument("--no-live-limits", action="store_true", help="只使用本地日志额度")
    parser.add_argument("--live-limit-timeout", type=float, default=10)
    parser.add_argument("--shared-key", default=os.environ.get("CODEX_BLE_KEY", ""))
    parser.add_argument("--interval", type=float, default=60, help="同步间隔秒数")
    parser.add_argument("--timeout", type=float, default=15, help="扫描和连接超时秒数")
    parser.add_argument("--chunk-size", type=int, default=128, choices=range(20, 181))
    parser.add_argument("--once", action="store_true", help="成功同步一次后退出")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    from bleak.exc import BleakBluetoothNotAvailableError

    reader = UsageReader(args.codex_home.expanduser())
    codex_cli = None if args.no_live_limits else resolve_codex_cli(args.codex_cli)
    live_limit_error_logged = False
    loop = asyncio.get_running_loop()
    log(
        f"百分比来源：Codex 实时接口（{codex_cli}）"
        if codex_cli
        else "百分比来源：本地 token_count 日志（实时接口不可用）"
    )
    log(f"等待蓝牙设备：{args.device}")
    while True:
        cycle_started = loop.time()
        snapshot = reader.snapshot()
        if codex_cli:
            try:
                live_limit = await asyncio.to_thread(
                    read_live_codex_limit, codex_cli, args.live_limit_timeout
                )
                if live_limit:
                    snapshot.update(live_limit)
                live_limit_error_logged = False
            except Exception as exc:
                if not live_limit_error_logged:
                    log(f"实时额度读取失败，暂用本地日志：{exc}")
                    live_limit_error_logged = True
        try:
            await asyncio.wait_for(
                push_snapshot(
                    snapshot,
                    args.device,
                    args.shared_key,
                    args.timeout,
                    args.chunk_size,
                ),
                timeout=operation_timeout(args.timeout),
            )
            log(
                f"同步成功：已用 {snapshot.get('used_percent', 0):.0f}%，"
                f"剩余 {100 - snapshot.get('used_percent', 0):.0f}% / "
                f"{snapshot.get('today_tokens', 0):,} today tokens"
            )
            if args.once:
                return
        except (TimeoutError, BleakBluetoothNotAvailableError) as exc:
            recovered = recover_macos_bluetooth()
            reason = "蓝牙操作超时" if isinstance(exc, TimeoutError) else str(exc)
            suffix = "；已重启 macOS 蓝牙代理" if recovered else ""
            log(f"同步失败：{reason}{suffix}")
        except Exception as exc:
            log(f"同步失败：{exc}")
        elapsed = loop.time() - cycle_started
        await asyncio.sleep(cycle_delay(args.interval, elapsed))


def main() -> None:
    args = parse_args()
    try:
        lock = acquire_sender_lock()
    except RuntimeError as exc:
        log(str(exc))
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log("停止蓝牙同步")
    finally:
        lock.close()


if __name__ == "__main__":
    main()
