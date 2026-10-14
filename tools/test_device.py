import tempfile
from pathlib import Path
import unittest

import device


class DeviceTests(unittest.TestCase):
    def test_bootloader_discovery_handles_spaces_and_ignores_other_boards(self):
        with tempfile.TemporaryDirectory() as root:
            for name, board in (("XIAO SENSE", "Seeed_XIAO_nRF52840_Sense"), ("OTHER", "OtherBoard")):
                volume = Path(root) / name
                volume.mkdir()
                (volume / "INFO_UF2.TXT").write_text(f"UF2 Bootloader 0.6.2\nBoard-ID: {board}\n")
            self.assertEqual(device.bootloader_volumes(Path(root)), [str(Path(root) / "XIAO SENSE")])

    def test_usb_identity_inherited_through_interfaces(self):
        registry = {"IORegistryEntryChildren": [
            {"idVendor": 0xC0DE, "idProduct": 0xCAFE, "IORegistryEntryChildren": [
                {"IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodem1101"}]},
            ]},
            {"idVendor": 0x2886, "idProduct": 0x0045, "IORegistryEntryChildren": [
                {"IOCalloutDevice": "/dev/cu.usbmodemBOOT"},
            ]},
            {"IOCalloutDevice": "/dev/cu.Bluetooth-Incoming-Port"},
        ]}
        self.assertEqual(device.ports_from_registry(registry), ["/dev/cu.usbmodem1101"])
        self.assertEqual(device.ports_from_registry([registry]), ["/dev/cu.usbmodem1101"])

    def test_ambiguous_devices_require_selection(self):
        with self.assertRaisesRegex(ValueError, "--volume"):
            device.choose(["/Volumes/A", "/Volumes/B"], "", "bootloader")
        self.assertEqual(device.choose(["/Volumes/A", "/Volumes/B"], "/Volumes/B", "bootloader"), "/Volumes/B")
        self.assertIsNone(device.choose(["/Volumes/A"], "/Volumes/B", "bootloader"))
        self.assertIsNone(device.choose([], "", "bootloader"))


if __name__ == "__main__":
    unittest.main()
