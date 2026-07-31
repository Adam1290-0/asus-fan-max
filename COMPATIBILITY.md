# Hardware Compatibility Database

This is a community-maintained list of tested hardware configurations.
If you verify this tool works on your laptop, please open a PR adding your entry.

## How to Add Your Entry

1. Run `python fan_daemon.py --calibrate` as SYSTEM
2. Record the output
3. Add a row to the table below

## Compatibility Table

| Laptop Model | CPU | GPU | Fans | Stock Max RPM (CPU/GPU) | Aftermarket Fan? | Duty=255 RPM | Notes |
|---|---|---|---|---|---|---|---|
| ROG Zephyrus G14 (GA401) | Ryzen 9 | RTX 3060 | 2 | ~4000 / ~4000 | Yes (high-speed) | 7200 / 7400 | Original author's config |
| *(add yours here)* | | | | | | | |

## Known Limitations

- **Fan count varies**: Some models have 2 fans, some have 3 (with a mid/VRM fan). Use `--calibrate` to detect.
- **RPM is hardware-specific**: The same duty=255 produces different RPM on different fan models. Stock fans may max out lower than aftermarket fans.
- **ATKACPI RPM readings are unreliable during test mode**: Use the daemon's own RPM readings (via AsusSAIO) or listen/feel the airflow. Do not trust GHelper/HWiNFO readings during forced mode.
- **Driver version sensitivity**: If ASUS updates `AsusSAIO.sys`, the DLL path may change. Re-run `--calibrate` after driver updates.

## Fan Index Mapping

The tool assumes:
- Fan 0 = CPU fan
- Fan 1 = GPU fan

This mapping is based on observed behavior but may differ on some models. If your GPU fan spins up when CPU is hot (or vice versa), swap the indices in the source code.

## ASUS Driver Versions Tested

| Driver Version | AsusSAIO.sys SHA-256 (first 16) | Status |
|---|---|---|
| asussci2.inf 3642f6b2eb6cd5d1 | D826A1022E992273 | ✅ Working |
| asussci2.inf cfd6e6fcf484217d | D235644AB09517CE | Untested (staged, not active) |
