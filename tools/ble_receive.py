#!/usr/bin/env python3
"""Save and acknowledge XIAO AHT10 readings on macOS 12.3+; no pairing required."""
import argparse
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import struct
import sys

SERVICE_UUID = "a8170001-9c67-4f88-9b44-7d52e2b16a10"
SAMPLE_UUID = "a8170002-9c67-4f88-9b44-7d52e2b16a10"
ACK_UUID = "a8170003-9c67-4f88-9b44-7d52e2b16a10"
STREAM_UUID = "a8170004-9c67-4f88-9b44-7d52e2b16a10"
LINK_UUID = "a8170005-9c67-4f88-9b44-7d52e2b16a10"
FRAME = struct.Struct("<2sBBIIhHI")
ERRORS = {1: "i2c", 2: "sensor_busy", 3: "not_calibrated", 4: "sample_timeout"}
# Mac-side waits only: the board independently enforces its mode's deadlines.
CONNECT_TIMEOUT = 60.0
SESSION_TIMEOUT = 12.0
DISCONNECT_TIMEOUT = 3.0
RETRY_MIN = 0.25
RETRY_MAX = 4.0
RSSI_MAX_AGE = 2.0
DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "artifacts/readings.sqlite3"


class ReceiptError(RuntimeError):
    """A local receipt failure: stop instead of acknowledging or retrying BLE."""


@dataclass
class Peer:
    device: object
    rssi: int
    seen_at: float

    def recent_rssi(self, now):
        # A queued connection may beat the next advertisement callback. Do not
        # report the previous wake's RSSI as a fresh measurement or fetch it by RF.
        return self.rssi if now - self.seen_at <= RSSI_MAX_AGE else None


async def disconnect(client, device):
    """Also cancel a pending CoreBluetooth request when connect was interrupted.

    Bleak 3.0.2's public macOS disconnect skips the Connecting state, and its
    connect cancellation only cancels CoreBluetooth on TimeoutError. Isolate
    the backend-specific workaround here; BLEDevice.details supplies these
    objects in the pinned backend. Await OS completion before another attempt.
    """
    if sys.platform == "darwin":
        peripheral, manager = device.details
        if peripheral.state() in (1, 3):  # Connecting or Disconnecting
            await asyncio.wait_for(manager.disconnect(peripheral), DISCONNECT_TIMEOUT)
            return
    await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)


def decode(payload):
    if len(payload) != FRAME.size:
        return None
    magic, version, status, boot, sequence, temperature, humidity, sleep = FRAME.unpack(payload)
    if magic != b"AH" or version != 2 or status not in (0, *ERRORS):
        return None
    if not 1 <= sleep <= 604800:
        return None
    if status == 0 and not (-5000 <= temperature <= 15000 and 0 <= humidity <= 10000):
        return None
    if status and (temperature or humidity):
        return None
    return dict(boot_id=f"{boot:08x}", sequence=sequence, sleep_seconds=sleep,
                temperature_c=temperature / 100 if status == 0 else None,
                humidity_percent=humidity / 100 if status == 0 else None,
                error=ERRORS.get(status))


class Journal:
    """Durable, idempotent receipt. ACK is permitted only after store returns."""
    def __init__(self, path):
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=1)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA fullfsync=ON")  # F_FULLFSYNC on macOS.
        self.db.execute("""CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY,
            device TEXT NOT NULL, boot_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            payload BLOB NOT NULL, record TEXT NOT NULL,
            UNIQUE(device, boot_id, sequence))""")
        # Avoid sorting all of a device's history on every receipt as it grows.
        self.db.execute("CREATE INDEX IF NOT EXISTS readings_device_order ON readings(device, id DESC)")
        self.db.commit()

    def close(self):
        self.db.close()

    def store(self, address, payload, rssi):
        payload = bytes(payload)
        reading = decode(payload)
        if reading is None:
            raise ValueError("Invalid AHT2 reading; refusing to acknowledge")
        address = address.lower()
        key = address, reading["boot_id"], reading["sequence"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT payload, record FROM readings WHERE device=? AND boot_id=? AND sequence=?", key
            ).fetchone()
            if existing:
                if existing[0] != payload:
                    raise ValueError("Same reading identity has different data; refusing to acknowledge")
                return json.loads(existing[1]), False
            previous = self.db.execute(
                "SELECT boot_id, sequence FROM readings WHERE device=? ORDER BY id DESC LIMIT 1",
                (address,),
            ).fetchone()
            missed = None
            if previous and previous[0] == reading["boot_id"]:
                delta = (reading["sequence"] - previous[1]) & 0xFFFFFFFF
                if 0 < delta < 0x80000000:
                    missed = delta - 1
            record = dict(time=datetime.now(timezone.utc).isoformat(), device=address,
                          rssi=rssi, missed_since_previous=missed, **reading)
            self.db.execute("INSERT INTO readings (device, boot_id, sequence, payload, record) VALUES (?, ?, ?, ?, ?)",
                            (*key, payload, json.dumps(record)))
        # Exiting the transaction successfully is the acknowledgment boundary.
        return record, True


async def save_and_ack(client, journal, address, payload, rssi, emit):
    try:
        record, fresh = journal.store(address, payload, rssi)
    except (sqlite3.Error, OSError, ValueError) as error:
        raise ReceiptError(str(error)) from error
    try:
        await client.write_gatt_char(ACK_UUID, payload[4:12], response=True)
    finally:
        # Even a lost ATT response leaves a durable receipt. Output follows ACK
        # submission, so terminal I/O never precedes the durability boundary.
        if fresh:
            emit(record)
    return record


async def drain(client, journal, address, rssi, emit, stopped):
    """Read oldest, persist, ACK exact identity, repeat within the board's window.

    A lost ACK/reply is harmless: the next session stores idempotently and ACKs
    again. Terminal output is secondary; the SQLite journal is authoritative.
    """
    while client.is_connected and not stopped():
        payload = bytes(await client.read_gatt_char(SAMPLE_UUID))
        if payload == bytes(FRAME.size):  # Empty queue sentinel, never a reading.
            return
        await save_and_ack(client, journal, address, payload, rssi, emit)


async def stream(client, journal, address, rssi, emit, stopped, disconnected, verbose=False):
    """Stop-and-wait notifications: bounded memory, no reads/polling when idle.

    A single consumer serializes SQLite commit and ACK. Subscription may deliver
    the first frame before start_notify returns. Disconnect/shutdown cancels all
    waits; the caller owns bounded connection teardown (which removes the CCCD).
    """
    packets = asyncio.Queue(maxsize=1)
    overflow = False

    def notified(_, payload):
        nonlocal overflow
        if packets.full():
            overflow = True
        else:
            packets.put_nowait(bytes(payload))

    await asyncio.wait_for(client.start_notify(STREAM_UUID, notified), SESSION_TIMEOUT)
    idle_timeout = SESSION_TIMEOUT
    previous_link = None
    lost = asyncio.create_task(disconnected.wait())
    try:
        while client.is_connected and not stopped():
            incoming = asyncio.create_task(packets.get())
            try:
                ready, _ = await asyncio.wait((incoming, lost), timeout=idle_timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if lost in ready:
                    return
                if incoming not in ready:
                    raise TimeoutError("Persistent board stopped sending readings")
                if overflow:
                    raise ReceiptError("Unexpected notification overflow; refusing to ACK")
                record = await asyncio.wait_for(
                    save_and_ack(client, journal, address, incoming.result(), rssi(), emit),
                    SESSION_TIMEOUT,
                )
                idle_timeout = record["sleep_seconds"] + SESSION_TIMEOUT
                # Optional diagnostics only. Normal streaming has no extra ATT
                # reads; these report actual central-selected values, not wishes.
                if verbose and not stopped():
                    link = bytes(await asyncio.wait_for(client.read_gatt_char(LINK_UUID), SESSION_TIMEOUT))
                    if link != previous_link:
                        interval, latency, supervision = struct.unpack("<HHH", link)
                        print(f"{address}: BLE interval {interval * 1.25:g} ms, "
                              f"peripheral latency {latency}, supervision {supervision * 10} ms", file=sys.stderr)
                        previous_link = link
            finally:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
    finally:
        lost.cancel()
        await asyncio.gather(lost, return_exceptions=True)


async def listen(args):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise RuntimeError("Install receiver dependencies: .venv/bin/python -m pip install -r tools/requirements.txt") from error
    journal = Journal(args.database)
    done = asyncio.Event()
    count = 0
    active = {}
    peers = {}
    fatal = []
    loop = asyncio.get_running_loop()

    def output(reading):
        if args.json:
            print(json.dumps(reading), flush=True)
        else:
            value = (f"ERROR: {reading['error']}" if reading["error"] else
                     f"{reading['temperature_c']:.2f} °C | {reading['humidity_percent']:.2f} %")
            gap = reading["missed_since_previous"]
            rssi = "unknown" if reading["rssi"] is None else f"{reading['rssi']} dBm"
            print(f"{reading['time']} {reading['device']} #{reading['sequence']} "
                  f"{value} | RSSI {rssi} | sleep {reading['sleep_seconds']} s"
                  + (f" | missed {gap}" if gap else ""), flush=True)

    async def receive(peer):
        address = peer.device.address.lower()
        retry_delay = RETRY_MIN
        connection_number = 0
        # One worker owns all connection attempts for this device. The scanner
        # can refresh peer.device, but cannot launch a competing connection.
        while not done.is_set():
            device = peer.device
            records = []
            progress = False
            persistent = False
            connected_at = None
            end_reason = "receiver stopping"
            disconnected = asyncio.Event()
            client = BleakClient(device, services=[SERVICE_UUID], timeout=CONNECT_TIMEOUT,
                                 disconnected_callback=lambda _, event=disconnected: event.set())

            def saved(record):
                nonlocal count, progress
                progress = True
                if args.verbose:
                    print(f"{address}: connection #{connection_number}, sample #{record['sequence']}, "
                          f"session age {loop.time() - connected_at:.3f}s", file=sys.stderr)
                if persistent:
                    try:
                        output(record)
                    except OSError as error:
                        raise ReceiptError(str(error)) from error
                else:
                    records.append(record)
                count += 1
                if args.count and count >= args.count:
                    done.set()

            try:
                # Queue immediately after the preceding disconnect, while the
                # board sleeps. BLEDevice avoids an implicit discovery scan.
                # The extra outer margin bounds service discovery/timeout cleanup.
                await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT + 5)
                connected_at = loop.time()
                connection_number += 1
                persistent = client.services.get_characteristic(STREAM_UUID) is not None
                if args.verbose:
                    mode = "persistent-notify" if persistent else "read/ACK (notification characteristic absent)"
                    print(f"{address}: connection #{connection_number} ready; receiver mode={mode}", file=sys.stderr)
                if persistent:
                    await stream(client, journal, address, lambda: peer.recent_rssi(loop.time()),
                                 saved, done.is_set, disconnected, args.verbose)
                else:
                    await asyncio.wait_for(
                        drain(client, journal, address, peer.recent_rssi(loop.time()), saved, done.is_set),
                        timeout=SESSION_TIMEOUT,
                    )
                if not done.is_set():
                    end_reason = ("peer disconnected" if disconnected.is_set() or not client.is_connected
                                  else "stream ended" if persistent else "read queue drained")
            except ReceiptError as error:
                end_reason = f"receipt failure: {error}"
                fatal.append(error)
                done.set()
            except asyncio.CancelledError:
                end_reason = "receiver cancelled/stopped"
                raise
            except Exception as error:
                end_reason = f"{type(error).__name__}: {error}"
                # A disconnect after ACK, an unavailable board, adapter errors,
                # and connect timeouts all leave committed receipts intact.
                if args.verbose and not done.is_set():
                    print(f"{address}: session ended ({error}); retrying remembered device.", file=sys.stderr)
            finally:
                if args.verbose and connected_at is not None:
                    print(f"{address}: connection #{connection_number} session ended after "
                          f"{loop.time() - connected_at:.3f}s ({end_reason})", file=sys.stderr)
                try:
                    cleanup = asyncio.create_task(disconnect(client, device))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # --count/--timeout/Ctrl-C may arrive during teardown.
                        # Finish the bounded OS cancellation before exiting.
                        await cleanup
                        raise
                except Exception as error:
                    # Do not overlap a new request with an unconfirmed teardown.
                    fatal.append(RuntimeError(f"{address}: Bluetooth cleanup failed: {error}"))
                    done.set()
                finally:
                    # Print after teardown, including shutdown during disconnect.
                    try:
                        for record in records:
                            output(record)
                    except OSError as error:
                        fatal.append(error)
                        done.set()

            if done.is_set():
                break
            if progress:
                retry_delay = RETRY_MIN
                # No intentional pause after a saved sample: queue the next
                # connection before this board wakes, even after a lost ACK reply.
            else:
                # Includes empty sessions and immediate adapter errors. Long
                # connection waits handle ordinary sleeps; this prevents spinning.
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, RETRY_MAX)

    def worker_done(task):
        # Never leave a worker exception unobserved or silently stop receiving.
        if not task.cancelled() and (error := task.exception()) is not None:
            fatal.append(error)
            done.set()

    def callback(device, advertisement):
        address = device.address.lower()
        if done.is_set() or (args.device and address != args.device.lower()):
            return
        if SERVICE_UUID not in [uuid.lower() for uuid in advertisement.service_uuids]:
            return
        if address in peers:
            peer = peers[address]
            peer.device, peer.rssi, peer.seen_at = device, advertisement.rssi, loop.time()
            return
        peer = peers[address] = Peer(device, advertisement.rssi, loop.time())
        task = active[address] = asyncio.create_task(receive(peer))
        task.add_done_callback(worker_done)

    print(f"Saving acknowledged AHT10 readings to {args.database}; automatically selecting read/ACK or persistent notifications. Keep this Mac awake. Ctrl-C stops.", file=sys.stderr)
    try:
        async with BleakScanner(detection_callback=callback, service_uuids=[SERVICE_UUID]):
            try:
                await asyncio.wait_for(done.wait(), timeout=args.timeout)
            except asyncio.TimeoutError as error:
                raise RuntimeError(f"Listening timed out after {args.timeout:g}s; saved {count} new reading(s).") from error
            finally:
                done.set()  # Reject late scan callbacks while workers tear down.
                tasks = list(active.values())
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if fatal:
                raise fatal[0]
    finally:
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Filter to the Mac's device UUID printed by this receiver")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="Durable SQLite receipt journal")
    parser.add_argument("--verbose", action="store_true", help="Log connection failures and actual persistent link parameters (extra reads)")
    parser.add_argument("--json", action="store_true", help="One JSON record per newly saved sample")
    parser.add_argument("--count", type=int, default=0, help="Stop after N newly saved readings (default: forever)")
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
