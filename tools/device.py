#!/usr/bin/env python3
"""macOS discovery for flash.sh. Reads device metadata; never flashes or resets."""
import argparse
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from firmware import BOARD_IDS


def bootloader_volumes(root=Path("/Volumes")):
    matches = []
    for volume in sorted(root.iterdir()):
        try:
            info = (volume / "INFO_UF2.TXT").read_text()
        except (OSError, UnicodeError):
            continue
        board = re.search(r"^Board-ID:\s*(\S+)", info, re.M)
        if info.startswith("UF2 Bootloader ") and board and board[1] in BOARD_IDS:
            matches.append(str(volume))
    return matches


def ports_from_registry(registry):
    """Carry the USB parent identity down to its IOSerialBSDClient child."""
    ports = set()

    def walk(node, vendor=None, product=None):
        vendor = node.get("idVendor", vendor)
        product = node.get("idProduct", product)
        port = node.get("IOCalloutDevice")
        if (vendor, product) == (0xC0DE, 0xCAFE) and isinstance(port, str):
            if port.startswith("/dev/cu.usbmodem"):
                ports.add(port)
        for child in node.get("IORegistryEntryChildren", []):
            walk(child, vendor, product)

    for root in [registry] if isinstance(registry, dict) else registry:
        walk(root)
    return sorted(ports)


def application_ports():
    result = subprocess.run(
        ["ioreg", "-a", "-l", "-p", "IOService"],
        capture_output=True, check=True, timeout=10,
    )
    return ports_from_registry(plistlib.loads(result.stdout))


def choose(candidates, requested, kind):
    if requested:
        return requested if requested in candidates else None
    if len(candidates) > 1:
        option = "--volume" if kind == "bootloader" else "--port"
        raise ValueError(f"Multiple {kind} devices: {', '.join(candidates)}. Run ./flash.sh {option} PATH.")
    return candidates[0] if candidates else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("wait-volume", "ports", "wait-port"))
    parser.add_argument("--volume", default="")
    parser.add_argument("--port", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--bootloader-volume", default="")
    args = parser.parse_args()
    if args.command == "ports":
        print("\n".join(application_ports()))
        return
    waiting_for_volume = args.command == "wait-volume"
    deadline = time.monotonic() + (120 if waiting_for_volume else 60)
    excluded = set(args.exclude.splitlines())
    requested = str(Path(args.volume).resolve()) if args.volume else ""
    while time.monotonic() < deadline:
        if waiting_for_volume:
            selected = choose(bootloader_volumes(), requested, "bootloader")
        else:
            ports = application_ports()
            ports = [port for port in ports if port not in excluded]
            if args.bootloader_volume and Path(args.bootloader_volume).exists():
                ports = []
            selected = choose(ports, args.port, "application serial")
        if selected:
            print(selected)
            return
        time.sleep(1)
    if waiting_for_volume:
        raise ValueError("No supported XIAO UF2 volume appeared. Check the cable and double-press RESET, then retry.")
    raise ValueError("Application restart was not confirmed. Check USB connection; if still in bootloader mode after copying, press RESET once. Transfer/startup may have failed; rerun ./flash.sh after checking the board.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
        sys.exit(f"ERROR: {error}")
