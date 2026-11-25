# Changelog

All notable changes to asus-fan-max are documented here.

## [1.1.2] - 2026-11-25

### Fixed
- **Hardcoded DriverStore path killed the daemon for weeks (critical)**: `asussci2.inf_amd64_<hash>` directory names change with every ASUS driver update, and the old hash is deleted. A driver update on 2026-09-30 invalidated the DLL path, so the daemon died at startup every 5 seconds — 103,000+ restart cycles and a 50 MB log, while the 85 °C thermal override silently did not exist. Version 1.1.1 already had a DriverStore fallback in `find_dll_path()`, but the deployed copy on the reporting machine was the older hardcoded build; **a fixed repo is not a fixed deployment**.
- **DLL discovery now tries every candidate**: `find_dll_candidates()` returns all `asussci2.inf_amd64_*\ASUSSystemAnalysis\AsusWinIO64.dll` paths plus the live `AsusSAIO` driver path, newest first, deduped case-insensitively. `run_daemon` attempts each in turn and only aborts when every one fails, logging each failure.
- **`--discover` / `--calibrate` no longer abort without a configured DLL**: they resolve a candidate themselves instead of exiting at `main()`'s pre-flight check.

### Added
- **Log rotation**: log files are now capped at 10 MB with 2 rotated backups (`RotatingFileHandler`) instead of growing without limit. A crash loop can no longer fill the disk.
- **Startup failure backoff**: any fatal startup error (not SYSTEM, ATKACPI unavailable, no usable DLL) logs the reason, then sleeps 60 s before exiting. Combined with the looping wrapper this drops a persistent-failure restart loop from ~720 to ~55 attempts per hour.

## [1.1.1] - 2026-09-10

### Fixed
- **Daemon hang via AsusWinIO calls**: the 3s timeout in v1.1.0 only covered `read_temp`. Driver calls made while entering/exiting test mode (`InitializeWinIo`, `Set_FanPwmDuty`, `SetFanTestMode`) could still block forever. Added a watchdog thread: if the main loop produces no heartbeat for 90 seconds, the daemon force-exits and the looping wrapper restarts it.
- **Residual test mode after a force-exit**: if the previous process died (crash or watchdog) while fans were forced, the EC test mode could stay active with fans pinned at max. On startup the daemon now explicitly clears test mode before entering the loop.
- **Wrapper delay unreliable**: the generated batch file used `timeout /nobreak`, which can fail in SYSTEM's console-less session. Switched to `ping` for the restart delay.

## [1.1.0] - 2026-08-27

### Fixed
- **Daemon hang (critical)**: `DeviceIoControl` to the ATKACPI device could block indefinitely after an ACPI hiccup (e.g. power unplug/replug), freezing the whole daemon loop. It stopped responding to commands, stopped reading temperature, and the manual/full-speed tray switches appeared "dead". `read_temp` now runs with a 3-second timeout guard so a stuck call can no longer freeze the loop.
- **Single-sensor failure now protected**: previously the fail-safe only triggered when *both* CPU and GPU sensors were unreadable. Now each sensor is tracked independently — if either one fails for 10 seconds, both fans are forced to max to prevent that chip from overheating unnoticed.
- **Independent fan release**: releasing one fan while the other is still forced now drops it to a safe mid duty (128/255) instead of leaving it stuck at max (test mode is global, so the EC can't take over just one fan).

### Added
- **Override switch**: `disable` / `enable` commands turn the whole feature on/off. When disabled, the daemon takes no action and hands fans fully back to the EC.
- **Manual full-speed control**: `force` / `release` commands run both fans at maximum regardless of temperature (useful as a "jet engine" toggle).
- **Configurable fan index**: `cpu_fan_idx` / `gpu_fan_idx` for models or aftermarket fans where the CPU/GPU index is swapped.
- **Auto-restart wrapper**: the install script now generates a looping batch file, so if the daemon crashes it restarts 5 seconds later.
- **Version tag** exposed as `__version__`.

## [1.0.0] - 2026-07-31

### Added
- Initial release.
- Temperature-triggered fan override (enter test mode at threshold, release back to EC).
- Independent CPU/GPU state machines with hysteresis and sustained-threshold debounce.
- `--calibrate` mode to discover hardware max RPM.
- `--discover` mode to scan ATKACPI endpoints and confirm hardware compatibility.
- `install.ps1` / `uninstall.ps1` for SYSTEM scheduled-task deployment.
- Full risk disclosure in DISCLAIMER.md and hardware compatibility template in COMPATIBILITY.md.