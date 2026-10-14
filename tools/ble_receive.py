#!/usr/bin/env python3
"""Listen for XIAO AHT10 broadcasts on macOS 12.3+; no pairing required."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import struct
import sys

COMPANY_ID = 0xFFFF
FRAME = struct.Struct("<4sBBIIhHI")
ERRORS = {1: "i2c", 2: "sensor_busy", 3: "not_calibrated", 4: "sample_timeout"}


def decode(payload):
    if len(payload) != FRAME.size:
        return None
    magic, version, status, boot, sequence, temperature, humidity, sleep = FRAME.unpack(payload)
    if magic != b"AHT1" or version != 1 or status not in (0, *ERRORS):
        return None
    if not 1 <= sleep <= 604800:
        return None
    if status == 0 and not (-5000 <= temperature <= 15000 and 0 <= humidity <= 10000):
        return None
    return dict(boot_id=f"{boot:08x}", sequence=sequence, sleep_seconds=sleep,
                temperature_c=temperature / 100 if status == 0 else None,
                humidity_percent=humidity / 100 if status == 0 else None,
                error=ERRORS.get(status))


class Receiver:
    def __init__(self, device=None):
        self.device = device
        self.previous = {}

    def accept(self, address, payload, rssi):
        if self.device and address.lower() != self.device.lower():
            return None
        reading = decode(payload)
        if reading is None:
            return None
        identity = reading["boot_id"], reading["sequence"]
        previous = self.previous.get(address)
        if previous == identity:
            return None
        missed = None
        if previous and previous[0] == identity[0]:
            delta = (identity[1] - previous[1]) & 0xFFFFFFFF
            if delta >= 0x80000000:  # Ignore a stale/out-of-order callback.
                return None
            missed = delta - 1
        self.previous[address] = identity
        return dict(time=datetime.now(timezone.utc).isoformat(), device=address,
                    rssi=rssi, missed_since_previous=missed, **reading)


async def listen(args):
    try:
        from bleak import BleakScanner
    except ImportError as error:
        raise RuntimeError("Install receiver dependencies: .venv/bin/python -m pip install -r tools/requirements.txt") from error
    receiver = Receiver(args.device)
    done = asyncio.Event()
    count = 0

    def callback(device, advertisement):
        nonlocal count
        payload = advertisement.manufacturer_data.get(COMPANY_ID)
        if payload is None:
            return
        reading = receiver.accept(device.address, payload, advertisement.rssi)
        if reading is None:
            return
        if args.json:
            print(json.dumps(reading), flush=True)
        else:
            value = (f"ERROR: {reading['error']}" if reading["error"] else
                     f"{reading['temperature_c']:.2f} °C | {reading['humidity_percent']:.2f} %")
            gap = reading["missed_since_previous"]
            print(f"{reading['time']} {device.address} #{reading['sequence']} "
                  f"{value} | RSSI {advertisement.rssi} dBm | sleep {reading['sleep_seconds']} s"
                  + (f" | missed {gap}" if gap else ""), flush=True)
        count += 1
        if args.count and count >= args.count:
            done.set()

    print("Listening for AHT10 broadcasts; keep this Mac awake. Ctrl-C stops.", file=sys.stderr)
    # CoreBluetooth does active scanning; our non-scannable beacon requires no
    # scan response. No name/UUID filter: all payload data is in manufacturer AD.
    async with BleakScanner(detection_callback=callback):
        try:
            await asyncio.wait_for(done.wait(), timeout=args.timeout)
        except asyncio.TimeoutError as error:
            raise RuntimeError(f"Listening timed out after {args.timeout:g}s; received {count} reading(s).") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Filter to the Mac's device UUID printed by this receiver")
    parser.add_argument("--json", action="store_true", help="One JSON record per sample")
    parser.add_argument("--count", type=int, default=0, help="Stop after N readings (default: forever)")
    parser.add_argument("--timeout", type=float, help="Fail if the requested count is not reached in this many seconds")
    args = parser.parse_args()
    if args.count < 0 or (args.timeout is not None and not 0 < args.timeout < float('inf')):
        parser.error("count must be nonnegative; timeout must be positive and finite")
    try:
        asyncio.run(listen(args))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
