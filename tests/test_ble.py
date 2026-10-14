"""Protocol, durable receipt, and fault-path tests; no Bluetooth hardware."""
import asyncio
import argparse
import sys
from types import SimpleNamespace
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from tools.ble_receive import ACK_UUID, FRAME, Journal, SAMPLE_UUID, SERVICE_UUID, decode, drain, listen, disconnect, Peer, ReceiptError, CONNECT_TIMEOUT, STREAM_UUID, LINK_UUID, stream


def frame(boot=0x12345678, sequence=42, status=0, temperature=-1234, humidity=5678, sleep=60):
    return FRAME.pack(b"AH", 2, status, boot, sequence, temperature, humidity, sleep)


class BleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "readings.sqlite3"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)

    def test_rust_wire_fixture(self):
        data = bytes.fromhex("41480200785634122a0000002efb2e163c000000")
        self.assertEqual(frame(), data)
        result = decode(data)
        self.assertEqual(result["temperature_c"], -12.34)
        self.assertEqual(result["humidity_percent"], 56.78)
        self.assertEqual(result["sleep_seconds"], 60)

    def test_invalid_packets_and_error_samples(self):
        for data in (b"", frame()[:-1], frame() + b"x", b"XX" + frame()[2:],
                     frame(status=5), frame(humidity=10001), frame(temperature=-5001),
                     frame()[:2] + b"\x01" + frame()[3:], frame(sleep=0), frame(sleep=604801),
                     frame(status=1)):
            self.assertIsNone(decode(data))
            with self.assertRaises(ValueError):
                self.journal.store("A", data, -60)
        for status in (1, 2, 3, 4):
            result = decode(frame(status=status, temperature=0, humidity=0))
            self.assertIsNone(result["temperature_c"])
            self.assertIsNone(result["humidity_percent"])
            self.assertIsNotNone(result["error"])

    def test_duplicates_gaps_reboot_wrap_and_devices(self):
        def save(address="A", **kwargs):
            return self.journal.store(address, frame(**kwargs), -60)
        self.assertTrue(save()[1])
        self.assertFalse(save("a")[1])
        self.assertTrue(save("B")[1])
        self.assertEqual(save(sequence=45)[0]["missed_since_previous"], 2)
        self.assertFalse(save(sequence=42)[1])
        self.assertIsNone(save(boot=1, sequence=0)[0]["missed_since_previous"])
        save("C", sequence=0xFFFFFFFF)
        self.assertEqual(save("C", sequence=0)[0]["missed_since_previous"], 0)
        with self.assertRaisesRegex(ValueError, "different data"):
            save(boot=1, sequence=0, humidity=100)

    def test_restart_deduplicates_and_preserves_gap_tracking(self):
        self.journal.store("A", frame(), -60)
        reopened = Journal(self.path)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.store("a", frame(), -55)[1])
        self.assertEqual(reopened.store("a", frame(sequence=44), -55)[0]["missed_since_previous"], 1)
        self.assertEqual(reopened.db.execute("SELECT count(*) FROM readings").fetchone()[0], 2)


class Client:
    def __init__(self, packets, write=None):
        self.is_connected = True
        self.services = SimpleNamespace(get_characteristic=lambda _: None)
        self.packets = iter(packets)
        self.writes = []
        self.write = write

    async def read_gatt_char(self, uuid):
        assert uuid == SAMPLE_UUID
        return next(self.packets, bytes(FRAME.size))

    async def write_gatt_char(self, uuid, payload, response):
        assert uuid == ACK_UUID and response
        self.writes.append(payload)
        if self.write:
            self.write(payload)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "readings.sqlite3"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.records = []

    async def receive(self, client):
        await drain(client, self.journal, "A", -60, self.records.append, lambda: False)

    async def test_commit_precedes_ack_and_duplicate_is_reacknowledged(self):
        def verify_committed(_):
            other = Journal(self.path)
            try:
                self.assertEqual(other.db.execute("SELECT count(*) FROM readings").fetchone()[0], 1)
            finally:
                other.close()
        client = Client([frame(), frame()], verify_committed)
        await self.receive(client)
        self.assertEqual(client.writes, [frame()[4:12]] * 2)
        self.assertEqual(len(self.records), 1)

    async def test_storage_failure_never_acknowledges(self):
        client = Client([frame()])
        with patch.object(self.journal, "store", side_effect=OSError("disk full")):
            with self.assertRaises(ReceiptError):
                await self.receive(client)
        self.assertEqual(client.writes, [])
        self.assertEqual(self.records, [])

    async def test_actual_database_write_failure_never_acknowledges(self):
        self.journal.db.execute("PRAGMA query_only=ON")
        client = Client([frame()])
        with self.assertRaises(ReceiptError):
            await self.receive(client)
        self.assertEqual(client.writes, [])
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM readings").fetchone()[0], 0)

    async def test_lost_ack_is_retried_without_duplicate_output(self):
        def disconnect(_):
            raise ConnectionError("ACK lost or ATT response lost")
        with self.assertRaises(ConnectionError):
            await self.receive(Client([frame()], disconnect))
        self.assertEqual(len(self.records), 1)
        client = Client([frame(), frame(sequence=43)])
        await self.receive(client)
        self.assertEqual(client.writes, [frame()[4:12], frame(sequence=43)[4:12]])
        self.assertEqual(len(self.records), 2)

    async def test_invalid_frame_and_identity_collision_never_acknowledge(self):
        self.journal.store("A", frame(), -60)
        for data in (b"garbage", frame(humidity=100)):
            client = Client([data])
            with self.assertRaises(ReceiptError):
                await self.receive(client)
            self.assertEqual(client.writes, [])

    async def test_cancellation_after_commit_keeps_reading_for_reack(self):
        def cancel(_):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.receive(Client([frame()], cancel))
        client = Client([frame()])
        await self.receive(client)
        self.assertEqual(client.writes, [frame()[4:12]])
        self.assertEqual(len(self.records), 1)

    async def test_count_stop_does_not_read_or_ack_next_sample(self):
        client = Client([frame(), frame(sequence=43)])
        await drain(client, self.journal, "A", -60, self.records.append, lambda: len(self.records) >= 1)
        self.assertEqual(client.writes, [frame()[4:12]])
        self.assertEqual(len(self.records), 1)

    async def test_listener_filters_coalesces_callbacks_and_prints_after_disconnect(self):
        events = []
        clients = []
        device = SimpleNamespace(address="A")
        advertisement = SimpleNamespace(service_uuids=[SERVICE_UUID], rssi=-60)

        class FakeClient(Client):
            def __init__(self, found, services, timeout, disconnected_callback):
                assert found is device and services == [SERVICE_UUID] and timeout == CONNECT_TIMEOUT
                super().__init__([frame()])
                self.on_disconnect = disconnected_callback
                clients.append(self)

            async def connect(self):
                events.append("connect")

            async def disconnect(self):
                events.append("disconnect")
                self.is_connected = False
                self.on_disconnect(self)

        class FakeScanner:
            def __init__(self, detection_callback, service_uuids):
                assert service_uuids == [SERVICE_UUID]
                self.callback = detection_callback

            async def __aenter__(self):
                self.callback(SimpleNamespace(address="B"), advertisement)
                self.callback(device, SimpleNamespace(service_uuids=[], rssi=-60))
                self.callback(device, advertisement)
                self.callback(device, advertisement)
                return self

            async def __aexit__(self, *args):
                pass

        args = argparse.Namespace(database=self.path, device="a", count=1, json=True, timeout=1, verbose=False)

        def printed(value, **kwargs):
            if kwargs.get("file") is not sys.stderr:
                events.append("output")

        with patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakClient=FakeClient, BleakScanner=FakeScanner)}):
            with patch("builtins.print", side_effect=printed), patch("tools.ble_receive.sys.platform", "test"):
                await listen(args)
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].writes, [frame()[4:12]])
        self.assertEqual(events, ["connect", "disconnect", "output"])


class StreamClient(Client):
    def __init__(self, packets, write=None):
        super().__init__(packets, write)
        self.services = SimpleNamespace(get_characteristic=lambda uuid: object() if uuid == STREAM_UUID else None)
        self.callback = None
        self.started = asyncio.Event()
        self.reads = []

    async def start_notify(self, uuid, callback):
        assert uuid == STREAM_UUID
        self.callback = callback
        self.send_next()  # Simulate notification before subscription returns.
        self.started.set()

    def send_next(self):
        payload = next(self.packets, None)
        if payload is not None:
            self.callback(STREAM_UUID, payload)

    async def write_gatt_char(self, uuid, payload, response):
        await super().write_gatt_char(uuid, payload, response)
        self.send_next()  # Next head can arrive before the write response.

    async def read_gatt_char(self, uuid):
        self.reads.append(uuid)
        assert uuid == LINK_UUID
        return bytes.fromhex("240013005802")  # 45 ms, latency 19, 6 s.


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "readings.sqlite3"
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        self.records = []
        self.disconnected = asyncio.Event()

    async def receive(self, client, count=1, verbose=False):
        await stream(client, self.journal, "A", lambda: None, self.records.append,
                     lambda: len(self.records) >= count, self.disconnected, verbose)

    async def test_subscribe_race_backlog_duplicates_and_commit_before_ack(self):
        def committed(ack):
            reader = Journal(self.path)
            try:
                self.assertTrue(reader.db.execute("SELECT payload FROM readings WHERE sequence=?",
                                                  (int.from_bytes(ack[4:], "little"),)).fetchone())
            finally:
                reader.close()
        client = StreamClient([frame(), frame(), frame(sequence=43)], committed)
        await self.receive(client, count=2)
        self.assertEqual(client.writes, [frame()[4:12], frame()[4:12], frame(sequence=43)[4:12]])
        self.assertEqual(len(self.records), 2)
        self.assertTrue(all(r["rssi"] is None for r in self.records))
        self.assertEqual(client.reads, [])

    async def test_lost_ack_reply_reconnect_deduplicates(self):
        def lost(_):
            raise ConnectionError("lost reply")
        with self.assertRaises(ConnectionError):
            await self.receive(StreamClient([frame()], lost))
        client = StreamClient([frame(), frame(sequence=43)])
        await self.receive(client, count=2)
        self.assertEqual(client.writes, [frame()[4:12], frame(sequence=43)[4:12]])
        self.assertEqual(len(self.records), 2)

    async def test_bad_data_and_storage_failure_never_ack(self):
        for payload in (b"bad", bytes(FRAME.size)):
            client = StreamClient([payload])
            with self.assertRaises(ReceiptError):
                await self.receive(client)
            self.assertEqual(client.writes, [])
        client = StreamClient([frame()])
        with patch.object(self.journal, "store", side_effect=OSError("disk full")):
            with self.assertRaises(ReceiptError):
                await self.receive(client)
        self.assertEqual(client.writes, [])

    async def test_idle_outlives_session_limit_without_polling_and_disconnect_wakes_it(self):
        client = StreamClient([frame(sleep=60)])
        with patch("tools.ble_receive.SESSION_TIMEOUT", 0.02):
            task = asyncio.create_task(self.receive(client, count=2))
            try:
                await client.started.wait()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertEqual(len(self.records), 1)
                self.assertEqual(client.reads, [])
                self.disconnected.set()
                await asyncio.wait_for(task, 0.2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_while_idle_is_prompt_and_leaves_no_wait_tasks(self):
        client = StreamClient([])
        task = asyncio.create_task(self.receive(client))
        await client.started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.writes, [])
        self.assertEqual([t for t in asyncio.all_tasks() if t is not asyncio.current_task()], [])

    async def test_missing_initial_notification_and_stalled_ack_time_out(self):
        with patch("tools.ble_receive.SESSION_TIMEOUT", 0.01):
            with self.assertRaises(TimeoutError):
                await self.receive(StreamClient([]))
            client = StreamClient([frame()])
            client.write_gatt_char = AsyncMock(side_effect=lambda *a, **k: None)
            async def stalled(*args, **kwargs):
                await asyncio.Event().wait()
            client.write_gatt_char.side_effect = stalled
            with self.assertRaises(TimeoutError):
                await self.receive(client)
        self.assertEqual(len(self.records), 1)  # Commit survives a cancelled ACK.

    async def test_overflow_is_bounded_and_fails_without_ack(self):
        client = StreamClient([frame()])
        async def flooded(uuid, callback):
            callback(uuid, frame())
            callback(uuid, frame(sequence=43))
        client.start_notify = flooded
        with self.assertRaisesRegex(ReceiptError, "overflow"):
            await self.receive(client)
        self.assertEqual(client.writes, [])

    async def test_verbose_reports_actual_link_parameters_after_ack(self):
        client = StreamClient([frame(), frame(sequence=43)])
        with patch("builtins.print") as printed:
            await self.receive(client, count=2, verbose=True)
        self.assertEqual(client.reads, [LINK_UUID])
        self.assertIn("interval 45 ms, peripheral latency 19, supervision 6000 ms", printed.call_args.args[0])


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "readings.sqlite3"
        self.device = SimpleNamespace(address="A")
        self.advertisement = SimpleNamespace(service_uuids=[SERVICE_UUID], rssi=-60)
        self.clients = []
        self.outputs = []
        self.callback = None

    async def run_listener(self, configure, count=2, timeout=1, device_filter="a"):
        test = self

        class FakeClient(Client):
            def __init__(self, device, services, timeout, disconnected_callback):
                assert services == [SERVICE_UUID] and timeout == CONNECT_TIMEOUT
                super().__init__([frame(sequence=len(test.clients))])
                self.device = device
                self.on_disconnect = disconnected_callback
                self.is_connected = False
                self.connect = AsyncMock(side_effect=self.connected)
                self.disconnect = AsyncMock(side_effect=self.disconnected)
                test.clients.append(self)
                configure(self, len(test.clients))

            async def connected(self):
                self.is_connected = True

            async def disconnected(self):
                self.is_connected = False
                self.on_disconnect(self)

        class FakeScanner:
            def __init__(self, detection_callback, service_uuids):
                assert service_uuids == [SERVICE_UUID]
                test.callback = detection_callback

            async def __aenter__(self):
                test.callback(test.device, test.advertisement)
                return self

            async def __aexit__(self, *args):
                # All client cleanup must finish while the scanner still exists.
                assert all(c.disconnect.await_count == 1 for c in test.clients)

        args = argparse.Namespace(database=self.path, device=device_filter, count=count, json=True,
                                  timeout=timeout, verbose=False)

        def printed(value, **kwargs):
            if kwargs.get("file") is not sys.stderr:
                self.outputs.append(value)

        with patch.dict(sys.modules, {"bleak": SimpleNamespace(BleakClient=FakeClient, BleakScanner=FakeScanner)}):
            with patch("builtins.print", side_effect=printed), patch("tools.ble_receive.sys.platform", "test"):
                await listen(args)

    async def test_queues_next_connection_without_another_advertisement(self):
        queued, wake = asyncio.Event(), asyncio.Event()

        def configure(client, number):
            if number == 2:
                async def connect_when_board_wakes():
                    queued.set()
                    await wake.wait()
                    client.is_connected = True
                client.connect.side_effect = connect_when_board_wakes

        task = asyncio.create_task(self.run_listener(configure))
        try:
            await asyncio.wait_for(queued.wait(), 1)
            self.assertEqual(len(self.clients), 2)
            self.assertIs(self.clients[1].device, self.device)
            self.assertEqual(self.clients[1].writes, [])
            wake.set()
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.outputs), 2)
        self.assertEqual([c.writes for c in self.clients], [[frame(sequence=n)[4:12]] for n in (0, 1)])

    async def test_persistent_autodetection_keeps_one_connection_across_idle_and_prints_live(self):
        sent = asyncio.Event()
        background = []

        def configure(client, number):
            self.assertEqual(number, 1)
            client.services = SimpleNamespace(get_characteristic=lambda uuid: object() if uuid == STREAM_UUID else None)
            async def subscribe(uuid, callback):
                self.assertEqual(uuid, STREAM_UUID)
                callback(uuid, frame(sequence=0, sleep=1))
                async def later():
                    await asyncio.sleep(0.05)  # Beyond the patched legacy session cap.
                    self.assertEqual(len(self.outputs), 1)
                    client.disconnect.assert_not_awaited()
                    callback(uuid, frame(sequence=1, sleep=1))
                    sent.set()
                background.append(asyncio.create_task(later()))
            client.start_notify = subscribe
            client.read_gatt_char = AsyncMock(side_effect=AssertionError("Streaming must not poll"))

        try:
            with patch("tools.ble_receive.SESSION_TIMEOUT", 0.02):
                await self.run_listener(configure)
            self.assertTrue(sent.is_set())
            self.assertEqual(len(self.clients), 1)
            self.assertEqual(len(self.clients[0].writes), 2)
            self.assertEqual(len(self.outputs), 2)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background)

    async def test_late_old_disconnect_callback_does_not_end_new_persistent_session(self):
        def configure(client, number):
            self.assertLessEqual(number, 2)
            client.services = SimpleNamespace(get_characteristic=lambda uuid: object() if uuid == STREAM_UUID else None)
            async def subscribe(uuid, callback):
                if number == 2:
                    self.clients[0].on_disconnect(self.clients[0])
                callback(uuid, frame(sequence=number))
            client.start_notify = subscribe
            if number == 1:
                def disconnect_after_ack(_):
                    client.is_connected = False
                    client.on_disconnect(client)
                client.write = disconnect_after_ack
        await self.run_listener(configure)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len(self.outputs), 2)

    async def test_fresh_advertisement_updates_cache_without_competing_connection(self):
        refreshed = SimpleNamespace(address="a")

        def configure(client, number):
            if number == 1:
                async def refresh_while_connecting():
                    self.callback(refreshed, self.advertisement)
                    self.callback(refreshed, self.advertisement)
                    client.is_connected = True
                client.connect.side_effect = refresh_while_connecting

        await self.run_listener(configure)
        self.assertEqual(len(self.clients), 2)
        self.assertIs(self.clients[1].device, refreshed)

    async def test_transport_errors_back_off_then_recover_without_rediscovery(self):
        delays = []
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            delays.append(delay)
            await real_sleep(0)

        def configure(client, number):
            if number <= 6:
                # Includes OSError-derived failures; they are not disk failures.
                client.connect.side_effect = ConnectionError("adapter unavailable")

        with patch("tools.ble_receive.asyncio.sleep", side_effect=fast_sleep):
            await self.run_listener(configure, count=1)
        self.assertEqual(delays, [0.25, 0.5, 1, 2, 4, 4])
        self.assertEqual(len(self.clients), 7)
        self.assertEqual(len(self.outputs), 1)

    async def test_timeout_cancels_pending_connection_and_cleans_up(self):
        cancelled = asyncio.Event()

        def configure(client, number):
            async def unavailable():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            client.connect.side_effect = unavailable

        with self.assertRaisesRegex(RuntimeError, "Listening timed out"):
            await self.run_listener(configure, timeout=0.02)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.outputs, [])

    async def test_timeout_rejects_late_discovery_during_cleanup(self):
        def configure(client, number):
            async def unavailable():
                await asyncio.Event().wait()
            async def late_discovery():
                self.callback(SimpleNamespace(address="B"), self.advertisement)
            client.connect.side_effect = unavailable
            client.disconnect.side_effect = late_discovery

        with self.assertRaisesRegex(RuntimeError, "Listening timed out"):
            await self.run_listener(configure, timeout=0.02, device_filter=None)
        self.assertEqual(len(self.clients), 1)

    async def test_shutdown_waits_for_in_progress_disconnect_and_prints_receipt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        def configure(client, number):
            async def delayed_disconnect():
                entered.set()
                await release.wait()
                client.is_connected = False
            client.disconnect.side_effect = delayed_disconnect

        task = asyncio.create_task(self.run_listener(configure, count=1))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            release.set()
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.outputs), 1)
        self.assertEqual(len(self.clients), 1)

    async def test_receipt_failure_stops_without_ack_or_reconnect(self):
        with patch.object(Journal, "store", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ReceiptError, "disk full"):
                await self.run_listener(lambda *_: None)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].writes, [])

    async def test_cleanup_failure_stops_instead_of_overlapping_connections(self):
        def configure(client, number):
            client.disconnect.side_effect = RuntimeError("OS never disconnected")
        with self.assertRaisesRegex(RuntimeError, "Bluetooth cleanup failed"):
            await self.run_listener(configure)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.outputs), 1)

    async def test_macos_cancels_native_pending_request(self):
        for state in (1, 3):
            peripheral = SimpleNamespace(state=lambda: state)
            manager = SimpleNamespace(disconnect=AsyncMock())
            client = SimpleNamespace(disconnect=AsyncMock())
            device = SimpleNamespace(details=(peripheral, manager))
            with patch("tools.ble_receive.sys.platform", "darwin"):
                await disconnect(client, device)
            manager.disconnect.assert_awaited_once_with(peripheral)
            client.disconnect.assert_not_awaited()

    def test_cached_rssi_expires(self):
        peer = Peer(self.device, -60, 10)
        self.assertEqual(peer.recent_rssi(11), -60)
        self.assertIsNone(peer.recent_rssi(15))


if __name__ == "__main__":
    unittest.main()
