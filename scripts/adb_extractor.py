#!/usr/bin/env python3
"""Helper script to detect connected Android device/emulator over ADB,
install the Momcozy APK, and extract the Tuya local_key.
"""

from pathlib import Path
import re
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from momcozy.config import ConfigStore, DEFAULT_CONFIG_PATH

ADB_HOST = "host.lima.internal:5037"
APK_PARTS = [
    "/tmp/xapk/com.lute.momcozy.apk",
    "/tmp/xapk/config.arm64_v8a.apk",
    "/tmp/xapk/rn.apk",
]
PKG_NAME = "com.lute.momcozy"


def run_adb(*args: str, check: bool = False) -> str:
    cmd = ["adb", "-H", "host.lima.internal", "-P", "5037"] + list(args)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"ADB error: {res.stderr.strip()}")
    return res.stdout.strip()


def get_devices() -> list[str]:
    out = run_adb("devices")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def wait_for_device(timeout: int = 120) -> str:
    print(f"Waiting for Android device / emulator to appear on ADB ({ADB_HOST})...")
    start = time.time()
    while time.time() - start < timeout:
        devs = get_devices()
        if devs:
            print(f"Detected device: {devs[0]}")
            return devs[0]
        time.sleep(2)
    raise TimeoutError("No Android device detected within timeout.")


def install_app(device_id: str) -> None:
    print(f"Checking if {PKG_NAME} is installed on {device_id}...")
    packages = run_adb("-s", device_id, "shell", "pm", "list", "packages")
    if PKG_NAME in packages:
        print(f"{PKG_NAME} is already installed.")
        return

    print("Installing Momcozy APK onto device...")
    cmd = ["adb", "-H", "host.lima.internal", "-P", "5037", "-s", device_id, "install-multiple"] + APK_PARTS
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"install-multiple failed: {res.stderr}, trying single APK...")
        cmd_single = ["adb", "-H", "host.lima.internal", "-P", "5037", "-s", device_id, "install", "-r", APK_PARTS[0]]
        subprocess.run(cmd_single, check=True)
    print("Installation successful.")


def scan_for_key(device_id: str) -> str | None:
    print("Searching for localKey in app storage...")
    cmd = 'su -c "grep -rn \'localKey\' /data/data/com.lute.momcozy/ 2>/dev/null"'
    out = run_adb("-s", device_id, "shell", cmd)
    if not out:
        cmd_run_as = 'run-as com.lute.momcozy grep -rn "localKey" /data/data/com.lute.momcozy/'
        out = run_adb("-s", device_id, "shell", cmd_run_as)

    if out:
        matches = re.findall(r'localKey[\'"]?\s*[:=]\s*[\'"]?([a-zA-Z0-9]{16})[\'"]?', out)
        if matches:
            return matches[0]
        print("Raw grep output:", out[:500])
    return None


def main() -> None:
    try:
        device_id = wait_for_device(timeout=10)
    except TimeoutError:
        print("No active device found on ADB server.")
        sys.exit(1)

    install_app(device_id)
    key = scan_for_key(device_id)
    if key:
        print(f"\nFOUND LOCAL KEY: {key}")
        if DEFAULT_CONFIG_PATH.exists():
            ConfigStore.update(DEFAULT_CONFIG_PATH, {"local_key": key})
            print(f"Saved local_key to {DEFAULT_CONFIG_PATH}")
    else:
        print("localKey not found yet. Make sure you log into Momcozy in the app so it downloads device data.")


if __name__ == "__main__":
    main()
