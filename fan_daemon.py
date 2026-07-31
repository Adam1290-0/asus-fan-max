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
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import os
import time
import signal
import atexit
import logging
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
    "dll_path": "",  # empty = auto-detect
    "log_file": "",  # empty = stdout only
    "status_file": "",  # empty = disabled
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
        if not self.handle:
            return None
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
            return None
        raw = struct.unpack_from("<i", out_buf.raw, 0)[0]
        temp = raw - 65536
        return temp if 0 <= temp <= 120 else None


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

    cpu_fan = FanState("CPU", 0, CPU_TEMP_ID, config)
    gpu_fan = FanState("GPU", 1, GPU_TEMP_ID, config)

    def write_status(cpu_state, gpu_state):
        if config["status_file"]:
            try:
                Path(config["status_file"]).write_text(f"{cpu_state},{gpu_state}")
            except Exception:
                pass

    def cleanup():
        log.info("Cleanup: releasing fan overrides")
        try:
            ctrl.release_all()
        except Exception as e:
            log.error(f"Cleanup error: {e}")
        reader.close()
        write_status("Off", "Off")
        log.info("=== asus-fan-max stopped ===")

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    log.info("Entering main loop")
    write_status("Normal", "Normal")
    consecutive_errors = 0

    while True:
        cpu_temp = reader.read_temp(CPU_TEMP_ID)
        gpu_temp = reader.read_temp(GPU_TEMP_ID)

        if cpu_temp is None and gpu_temp is None:
            consecutive_errors += 1
            if consecutive_errors >= 10:
                log.warning("10 consecutive read failures, reopening ATKACPI")
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
        duty = config["fan_duty_max"]

        action = cpu_fan.update(cpu_temp)
        if action == "enter":
            log.info(f"CPU fan FORCED (CPU={cpu_temp}C, sustained {config['sustained_count']}s)")
            if not ctrl.force_fan(0, duty):
                log.error("CPU fan force failed")
                cpu_fan.forced = False
            write_status("Forced" if cpu_fan.forced else "Normal", "Forced" if gpu_fan.forced else "Normal")
        elif action == "leave":
            log.info(f"CPU fan released (CPU={cpu_temp}C)")
            if not gpu_fan.forced:
                ctrl.release_all()
            write_status("Normal", "Forced" if gpu_fan.forced else "Normal")

        action = gpu_fan.update(gpu_temp)
        if action == "enter":
            log.info(f"GPU fan FORCED (GPU={gpu_temp}C, sustained {config['sustained_count']}s)")
            if not ctrl.force_fan(1, duty):
                log.error("GPU fan force failed")
                gpu_fan.forced = False
            write_status("Forced" if cpu_fan.forced else "Normal", "Forced" if gpu_fan.forced else "Normal")
        elif action == "leave":
            log.info(f"GPU fan released (GPU={gpu_temp}C)")
            if not cpu_fan.forced:
                ctrl.release_all()
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
