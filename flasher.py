
#!/usr/bin/env python3

"""
Bulk OTA update for WLED devices on the network.

Examples:
    python3 flasher.py --dry-run
    python3 flasher.py --firmware wled.bin
    python3 flasher.py --ip 192.168.1.100 --firmware wled.bin
"""

import argparse
import time
import threading
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def flash_and_configure_device(ip, firmware_path, max_retries=2):
    """Flash device and configure it after reboot."""
    if not flash_device(ip, firmware_path, max_retries):
        return {"ip": ip, "success": False, "configured": False}

    # Wait for device to reboot and come back online
    if wait_for_device(ip, timeout=60):
        # Configure device with default settings
        configured = configure_device(ip, color_rgb=(50, 20, 110), brightness=64)
        return {"ip": ip, "success": True, "configured": configured}
    else:
        print(f"[WARNING] Skipping config for {ip} - device not reachable")
        return {"ip": ip, "success": True, "configured": False}


def flash_device(ip, firmware_path, max_retries=2):
    url = f"http://{ip}/update"
    print(f"[{ip}] Flashing...")

    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"[{ip}] Retry attempt {attempt}/{max_retries}...")
                time.sleep(2)  # Brief pause before retry

            # First verify device is reachable
            try:
                check = requests.get(f"http://{ip}", timeout=5)
                print(f"[{ip}] Device is reachable (HTTP {check.status_code})")
            except requests.exceptions.RequestException as e:
                print(f"[{ip}] ERROR: Cannot reach device: {e}")
                if attempt == max_retries:
                    return False
                continue

            # Try newer form field name first ("update" for 0.13-0.15)
            print(f"[{ip}] Uploading firmware... (this may take 1-2 minutes)")
            try:
                with open(firmware_path, "rb") as fw:
                    files = {"update": ("firmware.bin", fw, "application/octet-stream")}
                    # Use shorter timeout - device may not respond if flashing succeeds
                    r = requests.post(url, files=files, timeout=60)
                print(f"[{ip}] Upload completed, checking response...")

                # Check for success indicators in response
                if r.status_code == 200:
                    response_lower = r.text.lower()
                    # WLED typically responds with "Update Success" or similar
                    # Also accept empty response as device may reboot immediately
                    if "success" in response_lower or "update" in response_lower or len(r.text.strip()) == 0:
                        print(f"[{ip}] OK: Firmware uploaded successfully. Device should reboot.")
                        return True
                    else:
                        print(f"[{ip}] WARNING: Unexpected response, trying alternate form field...")
                        # Try with alternate field name "file" for 0.16+
                        with open(firmware_path, "rb") as fw:
                            files = {"file": ("firmware.bin", fw, "application/octet-stream")}
                            r = requests.post(url, files=files, timeout=60)

                        if r.status_code == 200:
                            print(f"[{ip}] OK: Firmware uploaded successfully. Device should reboot.")
                            return True
                        else:
                            print(f"[{ip}] ERROR: Both form field names failed")
                            if attempt == max_retries:
                                return False
                            continue
                else:
                    print(f"[{ip}] ERROR: HTTP {r.status_code}")
                    if attempt == max_retries:
                        return False
                    continue
            except requests.exceptions.ChunkedEncodingError:
                # Device may close connection immediately after successful upload
                print(f"[{ip}] OK: Connection closed by device (likely successful flash)")
                return True
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                # Check if it's due to device rebooting after successful upload
                error_str = str(e)
                if "RemoteDisconnected" in error_str or "Connection reset" in error_str or "Read timed out" in error_str:
                    print(f"[{ip}] OK: Connection issue during response (likely successful flash)")
                    return True
                else:
                    raise  # Re-raise if it's a different connection error

        except requests.exceptions.Timeout:
            print(f"[{ip}] FAIL: Request timed out on attempt {attempt}/{max_retries}")
            if attempt == max_retries:
                return False
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"[{ip}] FAIL: Connection failed on attempt {attempt}/{max_retries}")
            if attempt == max_retries:
                return False
            continue
        except FileNotFoundError:
            print(f"[{ip}] FAIL: Firmware file not found: {firmware_path}")
            return False
        except Exception as e:
            print(f"[{ip}] FAIL: {type(e).__name__}: {e}")
            if attempt == max_retries:
                return False
            continue

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

    print(f"\nUpdating {len(devices)} device(s) using firmware: {args.firmware}")
    print(f"Processing devices in parallel...\n")

    # Flash all devices in parallel
    with ThreadPoolExecutor(max_workers=len(devices)) as executor:
        # Submit all flash jobs
        future_to_ip = {
            executor.submit(flash_and_configure_device, ip, args.firmware): ip
            for ip in devices
        }

        # Collect results as they complete
        results = []
        for future in as_completed(future_to_ip):
            result = future.result()
            results.append(result)

    # Print summary
    success_count = sum(1 for r in results if r["success"])
    fail_count = len(results) - success_count
    configured_count = sum(1 for r in results if r["configured"])

    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Total devices: {len(devices)}")
    print(f"  Flashed successfully: {success_count}")
    print(f"  Failed: {fail_count}")
    print(f"  Configured: {configured_count}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
