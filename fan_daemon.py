"""
asus-fan-max: SYSTEM-level fan override daemon for ASUS laptops.

Bypasses EC firmware speed limits via AsusWinIO64.dll + AsusSAIO.sys driver
to achieve physical maximum fan RPM when thermal thresholds are exceeded.

Usage:
    python fan_daemon.py                  # Run daemon (requires SYSTEM)
    python fan_daemon.py --calibrate      # Discover fans and measure max RPM
    python fan_daemon.py --status         # Print current status
    python fan_daemon.py --config <path>  # Use custom config file

Requirements:
    - Windows 10/11 x64
    - ASUS laptop with AsusSAIO.sys driver loaded
    - Must run as NT AUTHORITY\\SYSTEM
    - Python 3.10+

WARNING: See DISCLAIMER.md before use. This tool operates at kernel driver level.
"""
__version__ = "1.1.0"
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import os
import time
import signal
import atexit
import logging
import threading
import argparse
import json
from pathlib import Path
from datetime import datetime

# --- Default Configuration ---
DEFAULT_CONFIG = {
    "forced_threshold_c": 80,
    "normal_threshold_c": 70,
    "sustained_count": 3,
    "poll_interval_sec": 1,
    "fan_duty_max": 255,
    "release_duty": 128,  # mid duty for a fan released while its sibling is still forced
    "cpu_fan_idx": 0,     # AsusWinIO fan index for CPU (0 on most models; 1 if swapped)
    "gpu_fan_idx": 1,     # AsusWinIO fan index for GPU (1 on most models; 0 if swapped)
    "dll_path": "",  # empty = auto-detect
    "log_file": "",  # empty = stdout only
    "status_file": "",  # empty = disabled
    "cmd_file": "",  # empty = command file disabled (no GUI manual control)
}


def find_dll_path():
    """Auto-detect AsusWinIO64.dll from the active AsusSAIO driver."""
    import subprocess
    try:
        out = subprocess.check_output(["sc.exe", "qc", "AsusSAIO"], text=True, stderr=subprocess.DEVNULL)
        for line in out.split("\n"):
            if "BINARY_PATH_NAME" in line:
                # Extract path from \SystemRoot\System32\DriverStore\...
                path = line.split(":", 1)[1].strip()
                path = path.replace("\\SystemRoot\\", "C:\\Windows\\")
                path = path.replace("\\??\\", "")
                dll_dir = os.path.dirname(path)
                dll_path = os.path.join(dll_dir, "AsusWinIO64.dll")
                if os.path.exists(dll_path):
                    return dll_path
    except Exception:
        pass

    # Fallback: search DriverStore
    driver_store = r"C:\Windows\System32\DriverStore\FileRepository"
    if os.path.isdir(driver_store):
        for entry in os.listdir(driver_store):
            if entry.startswith("asussci2.inf_amd64_"):
                candidate = os.path.join(driver_store, entry, "ASUSSystemAnalysis", "AsusWinIO64.dll")
                if os.path.exists(candidate):
                    return candidate
    return None


def load_config(config_path=None):
    """Load configuration from JSON file, falling back to defaults."""
    config = DEFAULT_CONFIG.copy()
    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            user_config = json.load(f)
            config.update(user_config)
    if not config["dll_path"]:
        config["dll_path"] = find_dll_path()
    return config


# --- ATKACPI Temperature Reader ---
ATKACPI_PATH = bytes([
    0x5c, 0x00, 0x5c, 0x00, 0x2e, 0x00, 0x5c, 0x00,
    0x41, 0x00, 0x54, 0x00, 0x4b, 0x00, 0x41, 0x00,
    0x43, 0x00, 0x50, 0x00, 0x49, 0x00, 0x00, 0x00
])
CPU_TEMP_ID = 0x00120094
GPU_TEMP_ID = 0x00120097
CONTROL_CODE = 0x0022240C


class AtkAcpiReader:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.restype = wt.HANDLE
        self.kernel32.CreateFileW.argtypes = [
            ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p,
            wt.DWORD, wt.DWORD, wt.HANDLE
        ]
        self.kernel32.DeviceIoControl.restype = wt.BOOL
        self.kernel32.CloseHandle.restype = wt.BOOL
        self.handle = None

    def open(self):
        h = self.kernel32.CreateFileW(ATKACPI_PATH, 0xC0000000, 0, None, 3, 0, None)
        if not h or h == wt.HANDLE(-1).value:
            raise OSError(f"Cannot open ATKACPI, error={ctypes.get_last_error()}")
        self.handle = h

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(wt.HANDLE(self.handle))
            self.handle = None

    def read_temp(self, device_id):
        """Read temperature in Celsius with a timeout guard.

        Returns None on failure or timeout. The timeout guard prevents a stuck
        DeviceIoControl (e.g. after an ACPI device hiccup) from freezing the
        whole daemon loop."""
        if not self.handle:
            return None
        result = {}

        def _raw():
            try:
                in_data = b"DSTS" + struct.pack("<I", 8) + struct.pack("<I", device_id) + b"\x00" * 4
                in_buf = ctypes.create_string_buffer(in_data, 16)
                out_buf = ctypes.create_string_buffer(1024)
                written = wt.DWORD(0)
                ok = self.kernel32.DeviceIoControl(
                    wt.HANDLE(self.handle), wt.DWORD(CONTROL_CODE),
                    in_buf, wt.DWORD(16), out_buf, wt.DWORD(1024),
                    ctypes.byref(written), None
                )
                if not ok:
                    result["val"] = None
                else:
                    raw = struct.unpack_from("<i", out_buf.raw, 0)[0]
                    temp = raw - 65536
                    result["val"] = temp if 0 <= temp <= 120 else None
            except Exception:
                result["val"] = None

        t = threading.Thread(target=_raw, daemon=True)
        t.start()
        t.join(3.0)  # 3s timeout
        if t.is_alive():
            # DeviceIoControl is stuck; the caller will detect repeated
            # failures and reopen ATKACPI, which releases the stuck call.
            return None
        return result.get("val")


# --- AsusWinIO Fan Controller ---
class AsusWinIoController:
    def __init__(self, dll_path):
        self.dll_path = dll_path
        self.dll = None
        self.initialized = False
        self.test_mode_active = False

    def load(self):
        dll_dir = os.path.dirname(self.dll_path)
        os.add_dll_directory(dll_dir)
        self.dll = ctypes.WinDLL(self.dll_path)

    def ensure_initialized(self):
        if self.initialized:
            return True
        fn = self.dll.InitializeWinIo
        fn.restype = ctypes.c_int
        fn.argtypes = []
        self.initialized = (fn() != 0)
        return self.initialized

    def shutdown(self):
        if self.initialized and self.dll:
            if self.test_mode_active:
                self._set_test_mode(0)
            fn = self.dll.ShutdownWinIo
            fn.restype = ctypes.c_int
            fn.argtypes = []
            fn()
            self.initialized = False
            self.test_mode_active = False

    def _set_test_mode(self, val):
        fn = self.dll.HealthyTable_SetFanTestMode
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_int]
        r = fn(val)
        self.test_mode_active = (val != 0 and r == 1)
        return r

    def _set_duty(self, fan_idx, duty):
        fn = self.dll.Set_FanPwmDuty
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_int, ctypes.c_int]
        return fn(fan_idx, duty)

    def get_fan_counts(self):
        if not self.ensure_initialized():
            return 0
        fn = self.dll.HealthyTable_FanCounts
        fn.restype = ctypes.c_int
        fn.argtypes = []
        return fn()

    def get_fan_rpm(self, idx):
        if not self.initialized:
            return -1
        fn = self.dll.HealthyTable_FanRPM
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_int]
        return fn(idx)

    def force_fan(self, fan_idx, duty):
        if not self.ensure_initialized():
            return False
        if not self.test_mode_active:
            if self._set_test_mode(1) != 1:
                return False
        return self._set_duty(fan_idx, duty) == 1

    def release_all(self):
        if self.initialized and self.test_mode_active:
            self._set_test_mode(0)
        self.shutdown()

    def release_fan(self, fan_idx, other_fan_forced, release_duty):
        """Release a fan. Test mode is global, so when the sibling is still
        forced we cannot hand this fan back to the EC; drop it to a safe mid
        duty instead so it slows down without stopping."""
        if not self.initialized:
            return True
        if not other_fan_forced:
            self._set_test_mode(0)
            self.shutdown()
        else:
            self._set_duty(fan_idx, release_duty)
        return True


# --- Per-fan State Machine ---
class FanState:
    def __init__(self, name, fan_idx, temp_device_id, config):
        self.name = name
        self.fan_idx = fan_idx
        self.temp_device_id = temp_device_id
        self.config = config
        self.forced = False
        self.hot_count = 0
        self.cool_count = 0

    def update(self, temp):
        if temp is None:
            return None
        if temp >= self.config["forced_threshold_c"]:
            self.hot_count += 1
            self.cool_count = 0
        elif temp <= self.config["normal_threshold_c"]:
            self.cool_count += 1
            self.hot_count = 0
        else:
            self.hot_count = 0
            self.cool_count = 0

        if not self.forced and self.hot_count >= self.config["sustained_count"]:
            self.forced = True
            self.hot_count = 0
            return "enter"
        if self.forced and self.cool_count >= self.config["sustained_count"]:
            self.forced = False
            self.cool_count = 0
            return "leave"
        return None


# --- Calibration Mode ---
def calibrate(config):
    """Discover fans and measure max RPM at duty=255."""
    print("=== Fan Calibration ===")
    print(f"DLL: {config['dll_path']}")

    ctrl = AsusWinIoController(config["dll_path"])
    ctrl.load()
    if not ctrl.ensure_initialized():
        print("ERROR: InitializeWinIo failed. Are you running as SYSTEM?")
        return

    fan_count = ctrl.get_fan_counts()
    print(f"Fan count: {fan_count}")

    print("\nEntering test mode, ramping to duty=255...")
    ctrl._set_test_mode(1)
    for i in range(fan_count):
        ctrl._set_duty(i, 255)

    print("Waiting 8 seconds for fans to reach max speed...")
    time.sleep(8)

    print("\nResults:")
    print(f"{'Fan':<6} {'RPM':<8}")
    print("-" * 14)
    for i in range(fan_count):
        rpm = ctrl.get_fan_rpm(i)
        print(f"  {i:<4} {rpm:<8}")

    print("\nExiting test mode...")
    ctrl.release_all()
    print("Done. EC has resumed automatic control.")
    print("\nNOTE: RPM values are hardware-specific (fan model, laptop model).")
    print("Record these values for your COMPATIBILITY.md entry.")


# --- Discovery Mode ---
def discover(config):
    """Scan ATKACPI endpoints and identify hardware configuration."""
    print("=== Hardware Discovery ===")
    print("Scanning ATKACPI device IDs for sensors and fans...\n")

    reader = AtkAcpiReader()
    try:
        reader.open()
    except OSError as e:
        print(f"ERROR: Cannot open ATKACPI: {e}")
        return

    # Scan temperature sensors (0x0012xxxx range)
    print("--- Temperature Sensors ---")
    print(f"{'Device ID':<12} {'Value':<8} {'Interpretation'}")
    print("-" * 45)
    temp_sensors = []
    for low in range(0x80, 0xB0):
        dev_id = 0x00120000 | low
        raw = reader.read_temp(dev_id)
        if raw is not None and 15 <= raw <= 110:
            label = ""
            if dev_id == 0x00120094:
                label = "CPU (known)"
            elif dev_id == 0x00120097:
                label = "GPU (known)"
            elif 20 <= raw <= 90:
                label = "possible sensor"
            if label:
                print(f"  0x{dev_id:08X}  {raw:<8} {label}")
                temp_sensors.append((dev_id, raw, label))

    # Scan fan RPM endpoints (0x0011xxxx range)
    print("\n--- Fan RPM Endpoints (ATKACPI DSTS) ---")
    print(f"{'Device ID':<12} {'Raw':<10} {'RPM':<8}")
    print("-" * 35)
    fan_endpoints = []
    for low in range(0x10, 0x40):
        dev_id = 0x00110000 | low
        in_data = b"DSTS" + struct.pack("<I", 8) + struct.pack("<I", dev_id) + b"\x00" * 4
        in_buf = ctypes.create_string_buffer(in_data, 16)
        out_buf = ctypes.create_string_buffer(1024)
        written = wt.DWORD(0)
        ok = reader.kernel32.DeviceIoControl(
            wt.HANDLE(reader.handle), wt.DWORD(CONTROL_CODE),
            in_buf, wt.DWORD(16), out_buf, wt.DWORD(1024),
            ctypes.byref(written), None
        )
        if ok:
            raw = struct.unpack_from("<i", out_buf.raw, 0)[0]
            adj = raw - 65536
            rpm = (adj & 0xFFFF) * 100
            if 0 < rpm < 15000:
                label = ""
                if dev_id == 0x00110013:
                    label = "CPU fan (known)"
                elif dev_id == 0x00110014:
                    label = "GPU fan (known)"
                elif dev_id == 0x00110031:
                    label = "Mid fan (known)"
                print(f"  0x{dev_id:08X}  0x{raw & 0xFFFFFFFF:08X}  {rpm:<8} {label}")
                fan_endpoints.append((dev_id, rpm, label))

    reader.close()

    # AsusWinIO fan discovery
    print("\n--- AsusWinIO Fan Indices ---")
    ctrl = AsusWinIoController(config["dll_path"])
    try:
        ctrl.load()
        if not ctrl.ensure_initialized():
            print("  InitializeWinIo failed (need SYSTEM privileges)")
            print("  Run as SYSTEM for full discovery.")
        else:
            fan_count = ctrl.get_fan_counts()
            print(f"  FanCounts = {fan_count}")
            print(f"  {'Index':<8} {'RPM':<8}")
            print("  " + "-" * 16)
            for i in range(min(fan_count + 2, 6)):
                rpm = ctrl.get_fan_rpm(i)
                print(f"  {i:<8} {rpm:<8}")
            ctrl.shutdown()
    except Exception as e:
        print(f"  AsusWinIO not available: {e}")

    # Summary
    print("\n=== Discovery Summary ===")
    print(f"  Temperature sensors found: {len(temp_sensors)}")
    print(f"  Fan RPM endpoints found: {len(fan_endpoints)}")
    print(f"\n  Standard mapping (works on most ASUS laptops):")
    print(f"    CPU temp: 0x00120094")
    print(f"    GPU temp: 0x00120097")
    print(f"    CPU fan (AsusWinIO index): 0")
    print(f"    GPU fan (AsusWinIO index): 1")
    print(f"\n  If your fans respond to the wrong index, swap them in config.")
    print(f"  Run --calibrate as SYSTEM to verify fan RPM at duty=255.")


# --- Daemon Mode ---
def run_daemon(config):
    log = logging.getLogger("asus-fan-max")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if config["log_file"]:
        fh = logging.FileHandler(config["log_file"])
        fh.setFormatter(fmt)
        log.addHandler(fh)

    log.info("=== asus-fan-max daemon starting ===")
    log.info(f"Thresholds: forced>={config['forced_threshold_c']}C (x{config['sustained_count']}), "
             f"normal<={config['normal_threshold_c']}C (x{config['sustained_count']})")
    log.info(f"Poll: {config['poll_interval_sec']}s, duty: {config['fan_duty_max']}")
    log.info(f"DLL: {config['dll_path']}")

    whoami = os.popen("whoami").read().strip()
    log.info(f"Running as: {whoami}")
    if "system" not in whoami.lower():
        log.error("NOT running as SYSTEM! This tool requires SYSTEM privileges.")
        log.error("Use: schtasks /Create /TN \"asus-fan-max\" /TR \"<path>\" /SC ONLOGON /RU SYSTEM /F")
        sys.exit(1)

    reader = AtkAcpiReader()
    try:
        reader.open()
    except OSError as e:
        log.error(f"Cannot open ATKACPI: {e}")
        sys.exit(1)

    ctrl = AsusWinIoController(config["dll_path"])
    try:
        ctrl.load()
    except Exception as e:
        log.error(f"Cannot load DLL: {e}")
        sys.exit(1)

    cpu_fan = FanState("CPU", config["cpu_fan_idx"], CPU_TEMP_ID, config)
    gpu_fan = FanState("GPU", config["gpu_fan_idx"], GPU_TEMP_ID, config)

    def write_status(cpu_state, gpu_state):
        if config["status_file"]:
            en = "on" if enabled else "off"
            try:
                Path(config["status_file"]).write_text(f"{cpu_state},{gpu_state},{en}")
            except Exception:
                pass

    def cleanup():
        log.info("Cleanup: releasing fan overrides")
        try:
            ctrl.release_all()
        except Exception as e:
            log.error(f"Cleanup error: {e}")
        reader.close()
        try:
            if config["status_file"]:
                Path(config["status_file"]).write_text("Off,Off,off")
        except Exception:
            pass
        log.info("=== asus-fan-max stopped ===")

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    log.info("Entering main loop")
    consecutive_errors = 0
    cpu_read_errors = 0
    gpu_read_errors = 0
    manual_mode = False
    enabled = True
    fail_safe_active = False
    cmd_file = config["cmd_file"]
    duty = config["fan_duty_max"]
    release_duty = config["release_duty"]
    cpu_idx = config["cpu_fan_idx"]
    gpu_idx = config["gpu_fan_idx"]

    def check_command():
        """Handle override commands from an external controller."""
        nonlocal manual_mode, enabled, fail_safe_active
        if not cmd_file:
            return
        try:
            p = Path(cmd_file)
            if p.exists():
                cmd = p.read_text().strip().lower()
                p.unlink()
                if cmd == "force" and not manual_mode:
                    manual_mode = True
                    log.info("MANUAL MODE ON (user command)")
                    ctrl.force_fan(cpu_idx, duty)
                    ctrl.force_fan(gpu_idx, duty)
                    write_status("Manual", "Manual")
                elif cmd == "release" and manual_mode:
                    manual_mode = False
                    log.info("MANUAL MODE OFF (user command)")
                    ctrl.release_all()
                    cpu_fan.forced = False
                    gpu_fan.forced = False
                    write_status("Normal" if enabled else "Off", "Normal" if enabled else "Off")
                elif cmd == "disable":
                    enabled = False
                    manual_mode = False
                    fail_safe_active = False
                    log.info("FAN OVERRIDE DISABLED (all override off)")
                    ctrl.release_all()
                    cpu_fan.forced = False
                    gpu_fan.forced = False
                    write_status("Off", "Off")
                elif cmd == "enable":
                    enabled = True
                    log.info("FAN OVERRIDE ENABLED (auto mode on)")
                    write_status("Normal", "Normal")
        except Exception:
            pass

    # Clear any stale command file so a reboot never re-applies an old command.
    if cmd_file:
        try:
            p = Path(cmd_file)
            if p.exists():
                p.unlink()
                log.info("Cleared stale command file on startup")
        except Exception:
            pass

    write_status("Normal", "Normal")

    while True:
        check_command()

        if manual_mode:
            time.sleep(config["poll_interval_sec"])
            continue

        if not enabled:
            time.sleep(config["poll_interval_sec"])
            continue

        cpu_temp = reader.read_temp(CPU_TEMP_ID)
        gpu_temp = reader.read_temp(GPU_TEMP_ID)

        # Per-sensor failure tracking: a single dead sensor must not go
        # unnoticed or that chip could overheat while the daemon looks healthy.
        cpu_read_errors = 0 if cpu_temp is not None else cpu_read_errors + 1
        gpu_read_errors = 0 if gpu_temp is not None else gpu_read_errors + 1
        sensor_dead = cpu_read_errors >= 10 or gpu_read_errors >= 10

        if cpu_temp is None and gpu_temp is None:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                if not fail_safe_active:
                    fail_safe_active = True
                    log.error("FAIL-SAFE: temperature unreadable for 10s, forcing fans to max")
                    ctrl.force_fan(cpu_idx, duty)
                    ctrl.force_fan(gpu_idx, duty)
                    write_status("Forced", "Forced")
                reader.close()
                time.sleep(1)
                try:
                    reader.open()
                    consecutive_errors = 0
                except OSError:
                    pass
            time.sleep(config["poll_interval_sec"])
            continue

        consecutive_errors = 0

        # One sensor dead while the other still reads: force both fans anyway.
        if sensor_dead and not fail_safe_active:
            fail_safe_active = True
            log.error(
                f"FAIL-SAFE: sensor stuck (cpu_err={cpu_read_errors}, gpu_err={gpu_read_errors}), forcing fans to max"
            )
            ctrl.force_fan(cpu_idx, duty)
            ctrl.force_fan(gpu_idx, duty)
            cpu_fan.forced = True
            gpu_fan.forced = True
            write_status("Forced", "Forced")
            reader.close()
            time.sleep(1)
            try:
                reader.open()
            except OSError:
                pass
            time.sleep(config["poll_interval_sec"])
            continue

        # Exit fail-safe once temperatures are readable again
        if fail_safe_active and not sensor_dead:
            fail_safe_active = False
            cpu_hot = cpu_temp is not None and cpu_temp >= config["forced_threshold_c"]
            gpu_hot = gpu_temp is not None and gpu_temp >= config["forced_threshold_c"]
            if not cpu_hot and not gpu_hot:
                log.info("FAIL-SAFE released (temperatures readable again)")
                ctrl.release_all()
                cpu_fan.forced = False
                gpu_fan.forced = False
                write_status("Normal", "Normal")
            else:
                if cpu_hot:
                    cpu_fan.forced = True
                if gpu_hot:
                    gpu_fan.forced = True
                write_status(
                    "Forced" if cpu_fan.forced else "Normal",
                    "Forced" if gpu_fan.forced else "Normal",
                )

        action = cpu_fan.update(cpu_temp)
        if action == "enter":
            log.info(f"CPU fan FORCED (CPU={cpu_temp}C, sustained {config['sustained_count']}s)")
            if not ctrl.force_fan(cpu_idx, duty):
                log.error("CPU fan force failed")
                cpu_fan.forced = False
            write_status("Forced" if cpu_fan.forced else "Normal", "Forced" if gpu_fan.forced else "Normal")
        elif action == "leave":
            log.info(f"CPU fan released (CPU={cpu_temp}C)")
            ctrl.release_fan(cpu_idx, gpu_fan.forced, release_duty)
            write_status("Normal", "Forced" if gpu_fan.forced else "Normal")

        action = gpu_fan.update(gpu_temp)
        if action == "enter":
            log.info(f"GPU fan FORCED (GPU={gpu_temp}C, sustained {config['sustained_count']}s)")
            if not ctrl.force_fan(gpu_idx, duty):
                log.error("GPU fan force failed")
                gpu_fan.forced = False
            write_status("Forced" if cpu_fan.forced else "Normal", "Forced" if gpu_fan.forced else "Normal")
        elif action == "leave":
            log.info(f"GPU fan released (GPU={gpu_temp}C)")
            ctrl.release_fan(gpu_idx, cpu_fan.forced, release_duty)
            write_status("Forced" if cpu_fan.forced else "Normal", "Normal")

        time.sleep(config["poll_interval_sec"])


# --- Entry Point ---
def main():
    parser = argparse.ArgumentParser(description="ASUS fan max-speed override daemon")
    parser.add_argument("--calibrate", action="store_true", help="Discover fans and measure max RPM")
    parser.add_argument("--discover", action="store_true", help="Scan ATKACPI endpoints and identify hardware")
    parser.add_argument("--config", type=str, default=None, help="Path to config JSON file")
    parser.add_argument("--status", action="store_true", help="Print status file content and exit")
    args = parser.parse_args()

    config_path = args.config
    if not config_path:
        # Look for config next to this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, "config.json")
        if os.path.exists(candidate):
            config_path = candidate

    config = load_config(config_path)

    if not config["dll_path"]:
        print("ERROR: Cannot find AsusWinIO64.dll. Set 'dll_path' in config.json")
        sys.exit(1)

    if args.status:
        if config["status_file"] and os.path.exists(config["status_file"]):
            print(Path(config["status_file"]).read_text())
        else:
            print("No status file configured or not found")
        return

    if args.calibrate:
        calibrate(config)
    elif args.discover:
        discover(config)
    else:
        run_daemon(config)


if __name__ == "__main__":
    main()
