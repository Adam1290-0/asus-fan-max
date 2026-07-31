# asus-fan-max

> ⚠️ **USE AT YOUR OWN RISK.** This tool bypasses manufacturer-imposed hardware limits via kernel-level driver access. It may void your warranty, reduce fan lifespan, or cause system instability. See [DISCLAIMER.md](DISCLAIMER.md) for full details. By using this software, you accept all responsibility for any consequences.

**Your ASUS fan can do 7200+ RPM. The EC firmware locks it at ~4000. This tool unlocks it.**

A Windows daemon that bypasses the EC (Embedded Controller) firmware fan speed limit on ASUS laptops by communicating directly with the `AsusSAIO.sys` kernel driver — the same path used by MyASUS's built-in "Fan Diagnosis" feature.

```
Normal operation:  EC firmware curve → fan capped at ~4000 RPM
With asus-fan-max: Direct driver control → fan at physical maximum (7000-8000 RPM)
```

## Why This Exists

ASUS laptops have a hard fan speed limit baked into the EC firmware. No matter what fan curve you set (via GHelper, atrofac, Armoury Crate, or any ATKACPI-based tool), the CPU fan won't exceed ~4000 RPM. The only official way to reach full speed is MyASUS's "Fan Diagnosis" test — but you can't leave it running permanently.

This tool automates that exact mechanism: it enters the hardware test mode when your CPU/GPU gets hot, runs fans at physical maximum, and releases control back to the EC when temperatures drop.

## How It Works

```
┌─────────────────────────────────────────────────────┐
│  asus-fan-max daemon (NT AUTHORITY\SYSTEM)          │
│                                                     │
│  Every 1s: read CPU/GPU temp via ATKACPI            │
│                                                     │
│  CPU ≥ 80°C for 3s → AsusWinIO64.dll:              │
│    SetFanTestMode(1) + Set_FanPwmDuty(0, 255)       │
│                                                     │
│  CPU ≤ 70°C for 3s → AsusWinIO64.dll:              │
│    SetFanTestMode(0) → EC resumes normal curve      │
│                                                     │
│  GPU: independent state machine, same logic         │
└─────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  AsusSAIO.sys kernel driver → EC registers          │
│  (bypasses EC firmware curve interpreter)           │
└─────────────────────────────────────────────────────┘
```

**Coexists with GHelper / atrofac / Armoury Crate** — they continue managing the normal fan curve. This tool only intervenes during thermal emergencies.

## Quick Start

### Prerequisites

- Windows 10/11 x64
- ASUS laptop (ROG / TUF / Zephyrus / Strix — any model where MyASUS Fan Diagnosis works)
- Python 3.10+
- Administrator access (for installation only)

### Install

```powershell
# 1. Clone or download this repo
git clone https://github.com/Adam1290-0/asus-fan-max.git
cd asus-fan-max

# 2. Copy config and adjust thresholds if needed
copy config.example.json config.json

# 3. Discover your hardware (no admin needed for ATKACPI scan)
python fan_daemon.py --discover

# 4. Run calibration (as Admin) to verify max RPM
Start-Process python -Verb RunAs -ArgumentList "fan_daemon.py --calibrate"

# 5. Install as a SYSTEM service (auto-start on logon)
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

## Configuration

Edit `config.json`:

```json
{
    "forced_threshold_c": 80,
    "normal_threshold_c": 70,
    "sustained_count": 3,
    "poll_interval_sec": 1,
    "fan_duty_max": 255,
    "dll_path": "",
    "log_file": "asus-fan-max.log",
    "status_file": "asus-fan-max-status.txt"
}
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `forced_threshold_c` | 80 | Temperature (°C) that triggers max fan speed |
| `normal_threshold_c` | 70 | Temperature (°C) that releases back to EC control |
| `sustained_count` | 3 | Seconds the threshold must be held before acting |
| `poll_interval_sec` | 1 | Temperature polling interval |
| `fan_duty_max` | 255 | PWM duty cycle (0-255). 255 = physical maximum |
| `dll_path` | auto | Path to AsusWinIO64.dll (auto-detected if empty) |
| `log_file` | asus-fan-max.log | Log file path (empty = stdout only) |
| `status_file` | asus-fan-max-status.txt | Status file for external tools to read |

## Calibration

**Always calibrate before first use.** Different laptop models and fan hardware produce different RPM at the same duty cycle.

```powershell
# Run as Administrator:
Start-Process python -Verb RunAs -ArgumentList "fan_daemon.py --calibrate"
```

Example output:
```
=== Fan Calibration ===
Fan count: 2

Results:
Fan    RPM
--------------
  0    7284
  1    7419

NOTE: RPM values are hardware-specific (fan model, laptop model).
```

If your RPM values are significantly different from the examples in [COMPATIBILITY.md](COMPATIBILITY.md), that's normal — it depends on your specific fan hardware.

## Safety

- ✅ **Fans never stop**: Exiting test mode returns control to EC, fans continue running on the normal curve
- ✅ **Hysteresis**: 10°C gap between trigger (80°C) and release (70°C) prevents rapid cycling
- ✅ **Sustained threshold**: Must stay hot for 3 consecutive seconds (no spike triggers)
- ✅ **Crash-safe**: `atexit` handler releases test mode; worst case = fans stay loud until reboot
- ⚠️ **See [DISCLAIMER.md](DISCLAIMER.md) for full risk disclosure**

## Relationship to Other Tools

| Tool | What it does | Relationship |
|------|-------------|--------------|
| **GHelper** | Fan curves, performance modes, GPU control | Complementary — manages normal operation |
| **atrofac** | Fan curves via ATKACPI | Complementary — its curve runs when we're not forcing |
| **Armoury Crate** | Official ASUS control center | Independent — usually doesn't control fans on newer models |
| **MyASUS Fan Diagnosis** | One-time fan health test | Same underlying mechanism, but manual and temporary |
| **NBFC** | Fan control via EC writes | Different approach, Linux-focused |

**This tool does NOT replace GHelper or atrofac.** It adds a thermal emergency layer on top of whatever normal fan control you already use.

## Technical Details

See [docs/architecture.md](docs/architecture.md) for the full technical deep-dive including:
- ATKACPI vs AsusSAIO control paths
- EC firmware speed limiting mechanism
- AsusWinIO64.dll API reference
- Why ATKACPI RPM readings are unreliable during test mode

## ⚠️ Disclaimer

This tool bypasses manufacturer-imposed fan speed limits. See [DISCLAIMER.md](DISCLAIMER.md) for full details including hardware risks, security notes (CVE-2024-55408), and legal notice.

**TL;DR**: Your fans will be louder. Your fans might wear faster. ASUS doesn't support this. Use at your own risk.

## License

MIT — do whatever you want with the code. AsusWinIO64.dll is ASUS's property and is NOT included.

## Acknowledgments

This project stands on the shoulders of the ASUS laptop community:

- **[GHelper](https://github.com/seerge/g-helper)** (14K+ ⭐) — The definitive open-source Armoury Crate alternative. Its source code (`AsusACPI.cs`) was instrumental in understanding the ATKACPI device interface, fan curve format, and EC communication protocol. GHelper handles your daily fan curves and performance modes; this tool adds the emergency full-speed layer on top.

- **[atrofac](https://github.com/cronosun/atrofac)** — A clean, minimal fan curve tool whose Rust implementation of the ATKACPI DEVS/DSTS interface provided a second reference for understanding the EC protocol and validating our findings.

- **[Karmel0x/AsusFanControl](https://github.com/Karmel0x/AsusFanControl)** — Early pioneer of the AsusWinIO64.dll approach. Their work confirmed that the MyASUS Fan Diagnosis library could be used outside of MyASUS, which inspired this project's direction.

- **The ASUS ROG/TUF community** — Hundreds of GitHub issues, Reddit threads, and forum posts documenting the fan speed limitation problem. You made it clear this tool was needed.

This project is an independent community effort and is **not affiliated with, endorsed by, or supported by ASUSTeK Computer Inc.**
