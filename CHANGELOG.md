# Changelog

All notable changes to CodexMeter are documented in this file.

## Unreleased

### Fixed

- Read the live overall Codex percentage from `account/rateLimits/read`, falling back to session logs only when unavailable
- Keep BLE attempts on a true start-to-start 60-second cadence instead of adding scan time to the interval
- Bound CoreBluetooth operations with an overall timeout so a stalled scan cannot freeze synchronization indefinitely
- Automatically restart the per-user macOS Bluetooth agent when CoreBluetooth becomes unavailable
- Add timestamps to bridge logs for easier stale-data diagnosis

## [1.0.0] - 2026-08-14

Initial stable release.

### Highlights

- M5StickS3 dashboard for remaining Codex capacity and token usage
- BLE synchronization from local Codex session logs every 60 seconds
- Today, seven-day, session, input, cached input, output, and reasoning counters
- Four-way automatic display rotation with portrait and landscape layouts
- Motion-aware dimming and full brightness while externally powered
- macOS LaunchAgent template for automatic startup and recovery
- Overall-limit filtering, official-reset handling, stale-data status, and missing-data state
- English and Simplified Chinese documentation

[1.0.0]: https://github.com/yiduo/codex-meter/releases/tag/v1.0.0
