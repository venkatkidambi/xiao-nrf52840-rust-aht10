"""Synthetic fixtures test flash safety; these are NOT hardware observations."""
import contextlib
import io
from pathlib import Path
import struct
import tempfile
import unittest

import firmware as fw


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.volume = Path(self.temp.name)

    def board(self, version=7003000, start=0x27000, exported_end=0xED000):
        info = "UF2 Bootloader 0.9.2\r\nModel: Seeed XIAO nRF52840\r\nBoard-ID: nRF52840-SeeedXiao-v1\r\n"
        (self.volume / "INFO_UF2.TXT").write_text(info)
        flash = bytearray(b"\xff" * (exported_end - 0x1000))
        flash[0x2000] = 0x30
        struct.pack_into("<II", flash, 0x2004, 0x51B1E5DB, start)
        struct.pack_into("<II", flash, 0x2010, 140, version)
        (self.volume / "CURRENT.UF2").write_bytes(fw.encode_uf2(flash, 0x1000))

    def elf(self, start=0x27000, data_addr=0x27100, sp=fw.RAM_END):
        data = bytearray(512)
        data[:7] = b"\x7fELF\x01\x01\x01"
        struct.pack_into("<HHIIIIIHHHHHH", data, 16, 2, 40, 1, start + 9, 52, 0, 0x5000400, 52, 32, 2, 40, 0, 0)
        struct.pack_into("<8I", data, 52, 1, 256, start, start, 32, 32, 5, 4)
        struct.pack_into("<8I", data, 84, 1, 288, fw.RAM_START, data_addr, 4, 4, 6, 4)
        struct.pack_into("<II", data, 256, sp, start + 9)
        path = self.volume / "test.elf"
        path.write_bytes(data)
        return path

    def inspect(self, path, start=0x27000, end=fw.SAFE_END):
        with contextlib.redirect_stdout(io.StringIO()):
            return fw.inspect_elf(path, start, end)

    def test_softdevice_7_layout(self):
        self.board()
        layout, _, _ = fw.installed_layout(self.volume)
        self.assertEqual(layout["flash_start"], 0x27000)
        self.assertEqual(layout["flash_end"], 0xED000)

    def test_softdevice_6_uses_different_offset(self):
        self.board(version=6001001, start=0x26000)
        self.assertEqual(fw.installed_layout(self.volume)[0]["flash_start"], 0x26000)

    def test_smaller_export_is_respected(self):
        self.board(exported_end=0xEC000)
        self.assertEqual(fw.installed_layout(self.volume)[0]["flash_end"], 0xEC000)

    def test_legacy_seeed_sense_identity_and_export(self):
        # Match the observed 2021 bootloader's identity, family, and bounds.
        # Flash payload remains synthetic; no hardware is needed for this test.
        self.board(exported_end=0xEA000)
        info = self.volume / "INFO_UF2.TXT"
        info.write_text(
            "UF2 Bootloader 0.6.2-12-g459adc9-dirty\r\n"
            "Model: Seeed XIAO nRF52840\r\n"
            "Board-ID: Seeed_XIAO_nRF52840_Sense\r\n"
            "Date: Nov 30 2021\r\nSoftDevice: S140 7.3.0\r\n"
        )
        current = self.volume / "CURRENT.UF2"
        data = bytearray(current.read_bytes())
        for offset in range(0, len(data), 512):
            struct.pack_into("<I", data, offset + 28, 0x28860045)
        current.write_bytes(data)
        layout, _, _ = fw.installed_layout(self.volume)
        self.assertEqual(layout["board_id"], "Seeed_XIAO_nRF52840_Sense")
        self.assertEqual(layout["softdevice"], "S140 7.3.0")
        self.assertEqual(layout["flash_start"], 0x27000)
        self.assertEqual(layout["flash_end"], 0xEA000)

    def test_wrong_sd_offset_rejected(self):
        self.board(start=0x26000)
        with self.assertRaisesRegex(ValueError, "size disagrees"):
            fw.installed_layout(self.volume)

    def test_unknown_board_rejected(self):
        self.board()
        info = self.volume / "INFO_UF2.TXT"
        info.write_text(info.read_text().replace("SeeedXiao-v1", "OtherBoard-v1"))
        with self.assertRaisesRegex(ValueError, "Board-ID"):
            fw.installed_layout(self.volume)

    def test_info_mismatch_rejected(self):
        self.board()
        with (self.volume / "INFO_UF2.TXT").open("a") as file:
            file.write("SoftDevice: S140 6.1.1\n")
        with self.assertRaisesRegex(ValueError, "disagree"):
            fw.installed_layout(self.volume)

    def test_missing_sd_rejected(self):
        self.board()
        path = self.volume / "CURRENT.UF2"
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, (0x2000 // 256) * 512 + 32 + 4, 0)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "magic absent"):
            fw.installed_layout(self.volume)

    def test_uf2_roundtrip_and_padding(self):
        binary = bytes(range(256)) + b"abc"
        uf2 = fw.encode_uf2(binary, 0x27000)
        blocks = fw.decode_uf2(uf2)
        self.assertEqual(blocks[0x27000], binary[:256])
        self.assertEqual(blocks[0x27100], b"abc" + b"\xff" * 253)
        self.assertEqual(struct.unpack_from("<I", uf2, 28)[0], 0xADA52840)

    def test_truncation_duplicates_and_bad_family_rejected(self):
        valid = fw.encode_uf2(bytes(512), 0x27000)
        corrupt = bytearray(valid)
        struct.pack_into("<I", corrupt, 28, 123)
        for data in (valid[:-1], valid[:512] * 2, corrupt):
            with self.subTest(data_length=len(data)), self.assertRaises(ValueError):
                fw.decode_uf2(data)

    def test_data_load_address_and_gap_preserved(self):
        image = self.inspect(self.elf())
        self.assertEqual(len(image), 260)
        self.assertEqual(image[32:256], b"\xff" * 224)
        self.assertEqual(image[256:], bytes(4))

    def test_rejects_softdevice_and_bootloader_loads(self):
        for addr in (0, 0x1000, 0x26000, 0xF4000, 0x10001014):
            with self.subTest(addr=addr), self.assertRaises(ValueError):
                self.inspect(self.elf(data_addr=addr))

    def test_stale_elf_offset_rejected(self):
        with self.assertRaises(ValueError):
            self.inspect(self.elf(start=0x26000))

    def test_bad_stack_rejected(self):
        with self.assertRaisesRegex(ValueError, "stack"):
            self.inspect(self.elf(sp=0x20000000))

    def test_rejects_padding_outside_bound(self):
        with self.assertRaisesRegex(ValueError, "padding"):
            self.inspect(self.elf(), end=0x27104)


if __name__ == "__main__":
    unittest.main()
