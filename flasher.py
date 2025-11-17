
#!/usr/bin/env python3

"""
Bulk OTA update for all WLED devices on the network.

Examples:
    python3 bulk_wled_ota.py --dry-run
    python3 bulk_wled_ota.py --firmware my_wled.bin
"""

import argparse
import time
import threading
import requests
import json
from zeroconf import Zeroconf, ServiceBrowser


# ---------------------------------------------------------------------------
# mDNS Listener
# ---------------------------------------------------------------------------

class WLEDListener:
    def __init__(self):
        self.devices = []
        self.lock = threading.Lock()

    def add_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name)
        if info:
            ip = ".".join(map(str, info.addresses[0]))
            with self.lock:
                self.devices.append(ip)
            print(f"Found WLED device: {ip}")

    def update_service(self, *args):
        pass

    def remove_service(self, *args):
        pass


# ---------------------------------------------------------------------------
# Device Discovery
# ---------------------------------------------------------------------------

def discover_wled(timeout):
    print(f"Scanning for WLED devices (mDNS, {timeout}s)...")

    zeroconf = Zeroconf()
    listener = WLEDListener()

    browser = ServiceBrowser(
        zeroconf,
        "_wled._tcp.local.",
        listener
    )

    time.sleep(timeout)
    zeroconf.close()

    return sorted(list(set(listener.devices)))


# ---------------------------------------------------------------------------
# Device Wait & Config
# ---------------------------------------------------------------------------

def wait_for_device(ip, timeout=60, check_interval=2):
    """Wait for device to come back online after reboot."""
    print(f"Waiting for {ip} to come back online (timeout: {timeout}s)...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            r = requests.get(f"http://{ip}", timeout=2)
            if r.status_code == 200:
                print(f"[OK] {ip} is back online")
                # Give device a few more seconds to fully initialize
                print(f"[INFO] Waiting 5s for device to fully initialize...")
                time.sleep(5)
                return True
        except requests.exceptions.RequestException:
            pass

        time.sleep(check_interval)

    print(f"[WARNING] {ip} did not come back online within {timeout}s")
    return False


def configure_device(ip, color_rgb=(200, 100, 50), brightness=16):
    """Send JSON configuration to WLED device."""
    url = f"http://{ip}/json/state"

    config = {
        "on": True,
        "bri": brightness,
        "seg": [{"col": [list(color_rgb)]}]
    }

    print(f"Configuring {ip} with color RGB{color_rgb}, brightness {brightness}...")
    print(f"[DEBUG] Config payload: {json.dumps(config)}")

    try:
        r = requests.post(url, json=config, timeout=10)

        print(f"[DEBUG] Config response status: {r.status_code}")
        print(f"[DEBUG] Config response: {r.text}")

        if r.status_code == 200:
            response_data = r.json() if r.headers.get('content-type') == 'application/json' else {}
            if response_data.get('success'):
                print(f"[OK] {ip} configured successfully")
                return True
            else:
                print(f"[WARNING] {ip} returned 200 but success not confirmed: {r.text}")
                return True  # Still consider it success if we got 200
        else:
            print(f"[ERROR] {ip} config failed with HTTP {r.status_code}: {r.text[:200]}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"[FAIL] {ip} config failed: {e}")
        return False


# ---------------------------------------------------------------------------
# OTA Flash Operation
# ---------------------------------------------------------------------------

def flash_device(ip, firmware_path):
    url = f"http://{ip}/update"
    print(f"Flashing {ip}...")

    try:
        # First verify device is reachable
        try:
            check = requests.get(f"http://{ip}", timeout=5)
            print(f"[INFO] Device {ip} is reachable (HTTP {check.status_code})")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Cannot reach device at {ip}: {e}")
            return False

        # Try newer form field name first ("update" for 0.13-0.15)
        with open(firmware_path, "rb") as fw:
            files = {"update": ("firmware.bin", fw, "application/octet-stream")}
            # Increase timeout for large firmware files
            r = requests.post(url, files=files, timeout=120)

        print(f"[DEBUG] Status code: {r.status_code}")
        print(f"[DEBUG] Response text: {r.text[:200]}")

        # Check for success indicators in response
        if r.status_code == 200:
            response_lower = r.text.lower()
            # WLED typically responds with "Update Success" or similar
            if "success" in response_lower or "update" in response_lower:
                print(f"[OK] {ip} firmware uploaded successfully. Device should reboot.")
                return True
            else:
                print(f"[WARNING] {ip} returned 200 but unexpected response: {r.text[:100]}")
                # Try with alternate field name "file" for 0.16+
                print(f"[INFO] Retrying with alternate form field name...")
                with open(firmware_path, "rb") as fw:
                    files = {"file": ("firmware.bin", fw, "application/octet-stream")}
                    r = requests.post(url, files=files, timeout=120)

                print(f"[DEBUG] Retry status code: {r.status_code}")
                print(f"[DEBUG] Retry response text: {r.text[:200]}")

                if r.status_code == 200 and ("success" in r.text.lower() or "update" in r.text.lower()):
                    print(f"[OK] {ip} firmware uploaded successfully. Device should reboot.")
                    return True
                else:
                    print(f"[ERROR] Both form field names failed")
                    return False
        else:
            print(f"[ERROR] {ip} returned HTTP {r.status_code}: {r.text[:200]}")
            return False

    except requests.exceptions.Timeout:
        print(f"[FAIL] {ip}: Request timed out")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"[FAIL] {ip}: Connection failed - {e}")
        return False
    except FileNotFoundError:
        print(f"[FAIL] Firmware file not found: {firmware_path}")
        return False
    except Exception as e:
        print(f"[FAIL] {ip}: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk OTA update for all WLED devices on the network."
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list devices that would be updated; do not flash."
    )

    parser.add_argument(
        "--firmware",
        default="wled.bin",
        help="Path to WLED firmware .bin file (default: wled.bin)"
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=3,
        help="Time (seconds) to scan for WLED devices using mDNS (default: 3)"
    )

    parser.add_argument(
        "--ip",
        help="Specific IP address to flash (skips mDNS discovery)"
    )

    args = parser.parse_args()

    # If specific IP is provided, use that instead of discovery
    if args.ip:
        devices = [args.ip]
        print(f"Target device: {args.ip}")
    else:
        # Discover devices
        devices = discover_wled(args.timeout)

        print("\nDevices discovered:")
        if devices:
            for ip in devices:
                print(f" - {ip}")
        else:
            print("No WLED devices found.")

    # Dry run ends here
    if args.dry_run:
        print("\n(Dry run) No devices were updated.")
        return

    # No devices -> nothing to update
    if not devices:
        return

    print(f"\nUpdating {len(devices)} device(s) using firmware: {args.firmware}\n")

    success_count = 0
    fail_count = 0

    for ip in devices:
        if flash_device(ip, args.firmware):
            success_count += 1

            # Wait for device to reboot and come back online
            if wait_for_device(ip, timeout=60):
                # Configure device with default settings
                configure_device(ip, color_rgb=(50, 20, 110), brightness=64)
            else:
                print(f"[WARNING] Skipping config for {ip} - device not reachable")
        else:
            fail_count += 1

    print(f"\nDone. Success: {success_count}, Failed: {fail_count}")


if __name__ == "__main__":
    main()
