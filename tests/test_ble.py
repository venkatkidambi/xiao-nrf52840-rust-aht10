"""Receiver protocol and delivery tracking; no Bluetooth hardware required."""
import unittest
from tools.ble_receive import FRAME, Receiver, decode


def frame(boot=0x12345678, sequence=42, status=0, temperature=-1234, humidity=5678):
    return FRAME.pack(b"AHT1", 1, status, boot, sequence, temperature, humidity, 60)


class BleTests(unittest.TestCase):
    def test_rust_wire_fixture(self):
        data = bytes.fromhex("414854310100785634122a0000002efb2e163c000000")
        self.assertEqual(frame(), data)
        result = decode(data)
        self.assertEqual(result["temperature_c"], -12.34)
        self.assertEqual(result["humidity_percent"], 56.78)
        self.assertEqual(result["sleep_seconds"], 60)

    def test_invalid_packets_and_error_samples(self):
        for data in (b"", frame()[:-1], frame() + b"x", b"BAD!" + frame()[4:],
                     frame(status=5), frame(humidity=10001), frame(temperature=-5001),
                     frame()[:4] + b"\x02" + frame()[5:]):
            self.assertIsNone(decode(data))
        for status in (1, 2, 3, 4):
            result = decode(frame(status=status))
            self.assertIsNone(result["temperature_c"])
            self.assertIsNone(result["humidity_percent"])
            self.assertIsNotNone(result["error"])

    def test_duplicates_gaps_reboot_wrap_and_devices(self):
        receiver = Receiver()
        self.assertIsNotNone(receiver.accept("A", frame(), -60))
        self.assertIsNone(receiver.accept("A", frame(), -65))
        self.assertIsNotNone(receiver.accept("B", frame(), -65))
        self.assertEqual(receiver.accept("A", frame(sequence=45), -60)["missed_since_previous"], 2)
        self.assertIsNone(receiver.accept("A", frame(sequence=43), -60))
        self.assertIsNone(receiver.accept("A", frame(boot=1, sequence=0), -60)["missed_since_previous"])
        receiver = Receiver("A")
        self.assertIsNone(receiver.accept("B", frame(), -60))
        receiver.accept("A", frame(sequence=0xFFFFFFFF), -60)
        self.assertEqual(receiver.accept("A", frame(sequence=0), -60)["missed_since_previous"], 0)


if __name__ == "__main__":
    unittest.main()
