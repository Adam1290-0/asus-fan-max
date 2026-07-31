# ⚠️ DISCLAIMER — READ BEFORE USE

## Risk Warning

**This tool operates at kernel driver level and directly controls hardware fan speeds. Use at your own risk.**

### Hardware Risks

- **Fan wear**: Running fans at maximum RPM for extended periods may reduce fan lifespan.
- **Thermal shock**: Rapid fan speed changes could theoretically stress fan bearings.
- **Aftermarket fans**: If you have replaced your stock fans, the duty-to-RPM mapping may differ significantly. Calibrate first.
- **No hardware protection**: This tool bypasses the EC firmware's built-in fan speed limits. Those limits exist for a reason (noise, power, longevity). You are overriding the manufacturer's safety margins.

### Software Risks

- **SYSTEM privileges required**: The daemon runs as `NT AUTHORITY\SYSTEM`. A bug in this code has the same privilege level as the Windows kernel.
- **Driver interaction**: This tool communicates with `AsusSAIO.sys` via `AsusWinIO64.dll`. Incorrect usage could theoretically cause system instability (BSOD).
- **No rollback guarantee**: If the daemon crashes without cleanup, fans may remain at maximum speed until reboot. This is safe (loud but safe), but unexpected.

### Security Notice

- **CVE-2024-55408**: The `AsusSAIO.sys` driver has a known vulnerability (arbitrary physical memory read/write). This is a pre-existing ASUS driver issue, not introduced by this tool. However, be aware that any software communicating with this driver operates in a security-sensitive context.
- **ASUS driver updates**: ASUS has previously broken community fan control tools via driver updates (e.g., ASUS System Control Interface v3.1.41.0). This tool may stop working after driver updates.

### Legal Notice

- **AsusWinIO64.dll** is proprietary ASUS software. This project does NOT distribute it. You must extract it from your own system's DriverStore.
- This tool is provided as-is under the MIT License with NO WARRANTY.
- The authors are not responsible for any hardware damage, data loss, or system instability.
- Using this tool may void your warranty.

### What This Tool Does NOT Do

- Does NOT stop fans (exit from test mode returns control to EC, fans continue running)
- Does NOT modify BIOS/UEFI firmware
- Does NOT modify ASUS drivers or services
- Does NOT require internet access
- Does NOT collect any data

## Safe Usage Guidelines

1. **Always calibrate first** (`--calibrate`) to understand your hardware's actual RPM range.
2. **Start with conservative thresholds** (e.g., 90°C trigger) and lower them gradually.
3. **Monitor the log file** for the first few hours of operation.
4. **Keep the uninstall script handy** (`uninstall.ps1`).
5. **Do not run alongside MyASUS Fan Diagnosis** — both use the same test mode and will conflict.
