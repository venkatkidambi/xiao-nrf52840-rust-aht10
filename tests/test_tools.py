"""Host-tool regressions. All flash writes are mocked; no hardware is changed."""
import argparse
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools import firmware as fw


class ToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def board(self, version=7003000, start=0x27000, board=fw.BOARD_IDS[-1]):
        (self.root / "INFO_UF2.TXT").write_text(f"UF2 Bootloader 0.6.2\r\nBoard-ID: {board}\r\n")
        flash = bytearray(b"\xff" * (0xEA000 - 0x1000))
        flash[0x2000] = 0x30
        struct.pack_into("<II", flash, 0x2004, 0x51B1E5DB, start)
        struct.pack_into("<II", flash, 0x2010, 140, version)
        (self.root / "CURRENT.UF2").write_bytes(fw.encode_uf2(flash, 0x1000))

    def test_installed_layout_and_identity(self):
        for version, start in ((7003000, 0x27000), (7002000, 0x27000), (6001001, 0x26000)):
            for board in fw.BOARD_IDS:
                with self.subTest(version=version, board=board):
                    self.board(version, start, board)
                    layout, _, _ = fw.installed_layout(self.root)
                    self.assertEqual((layout["flash_start"], layout["flash_end"]), (start, 0xEA000))
        for params in ({"board": "OtherBoard"}, {"start": 0x26000}, {"version": 1}):
            self.board(**params)
            with self.subTest(params=params), self.assertRaises(ValueError):
                fw.installed_layout(self.root)

    def test_metadata_mismatch_and_missing_softdevice(self):
        self.board()
        with (self.root / "INFO_UF2.TXT").open("a") as file:
            file.write("SoftDevice: S140 6.1.1\n")
        with self.assertRaisesRegex(ValueError, "disagree"):
            fw.installed_layout(self.root)
        self.board()
        path = self.root / "CURRENT.UF2"
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, 0x2000 // 256 * 512 + 36, 0)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "magic absent"):
            fw.installed_layout(self.root)

    def test_uf2_format_padding_and_corruption(self):
        binary = bytes(range(256)) + b"abc"
        data = fw.encode_uf2(binary, 0x27000)
        self.assertEqual(b"".join(fw.decode_uf2(data).values()), binary.ljust(512, b"\xff"))
        for family in (fw.FAMILY, 0x28860044, 0x28860045):
            changed = bytearray(data)
            for offset in range(0, len(changed), 512):
                struct.pack_into("<I", changed, offset + 28, family)
            self.assertEqual(len(fw.decode_uf2(changed)), 2)
        corruptions = [data[:-1], data[:512] * 2]
        for offset in (0, 8, 16, 24, 28, 508):
            changed = bytearray(data)
            struct.pack_into("<I", changed, offset, 123)
            corruptions.append(changed)
        for changed in corruptions:
            with self.subTest(header=changed[:32]), self.assertRaises(ValueError):
                fw.decode_uf2(changed)

    def elf(self, start=0x27000, data_addr=0x27100, sp=fw.RAM_END):
        data = bytearray(512)
        data[:7] = b"\x7fELF\x01\x01\x01"
        struct.pack_into("<HHIIIIIHHHHHH", data, 16, 2, 40, 1, start + 9, 52, 0, 0x5000400, 52, 32, 2, 40, 0, 0)
        struct.pack_into("<8I", data, 52, 1, 256, start, start, 32, 32, 5, 4)
        struct.pack_into("<8I", data, 84, 1, 288, fw.RAM_START, data_addr, 4, 4, 6, 4)
        struct.pack_into("<II", data, 256, sp, start + 9)
        path = self.root / "test.elf"
        path.write_bytes(data)
        return path

    def test_elf_addresses_and_protected_regions(self):
        with contextlib.redirect_stdout(io.StringIO()):
            image = fw.inspect_elf(self.elf(), 0x27000, fw.SAFE_END)
            self.assertEqual(image[32:], b"\xff" * 224 + bytes(4))
            cases = [{"data_addr": a} for a in (0, 0x1000, 0x26000, 0xF4000, 0x10001014)]
            cases += [{"start": 0x26000}, {"sp": 0x20000000}]
            for params in cases:
                with self.subTest(params=params), self.assertRaises(ValueError):
                    fw.inspect_elf(self.elf(**params), 0x27000, fw.SAFE_END)
            with self.assertRaisesRegex(ValueError, "padding"):
                fw.inspect_elf(self.elf(), 0x27000, 0x27104)

    def test_copy_progress_and_error_classification(self):
        error = OSError(errno.EIO, "disconnected")
        cases = [([128, 384, 512, 512], None, None), ([0], None, ValueError),
                 ([512, error], None, OSError), ([512, 512, error], None, fw.Uf2Disconnect),
                 ([512] * 3, error, fw.Uf2Disconnect),
                 ([512] * 3, OSError(errno.ENOSPC, "full"), OSError)]
        for writes, sync_error, expected in cases:
            stream = MagicMock()
            stream.__enter__.return_value = stream
            stream.write.side_effect = writes
            with self.subTest(writes=writes), patch.object(Path, "open", return_value=stream), \
                    patch.object(fw.os, "fsync", side_effect=sync_error):
                with self.assertRaises(expected) if expected else contextlib.nullcontext():
                    fw.copy_uf2(self.root / "test.uf2", bytes(1536))
        with patch.object(Path, "open", side_effect=error), self.assertRaises(OSError):
            fw.copy_uf2(self.root / "test.uf2", bytes(512))

    def test_discovery_and_ambiguous_devices(self):
        self.board()
        self.assertIn(str(self.root), fw.bootloader_volumes(self.root.parent))
        usb = {"idVendor": 0xC0DE, "idProduct": 0xCAFE, "IORegistryEntryChildren": [
            {"IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodemTEST"}]}]}
        self.assertEqual(fw.ports_from_registry(usb), ["/dev/cu.usbmodemTEST"])
        usb["idVendor"] = 0x2886
        self.assertEqual(fw.ports_from_registry([usb]), [])
        args = argparse.Namespace(command="wait-volume", volume=None, port="", exclude="")
        with patch.object(fw, "bootloader_volumes", return_value=["/Volumes/A", "/Volumes/B"]):
            with self.assertRaisesRegex(ValueError, "Multiple devices"):
                fw.wait_device(args)

    def test_shell_stops_on_failure_and_checks_restart(self):
        # Substitute only external tools; execute the real shell from another cwd.
        stub = f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["TEST_LOG"], "a") as f: f.write(json.dumps([name, *args]) + "\\n")
if name == "uname": print("Darwin")
elif name == "cargo" and args[0] == "build": sys.exit(int(os.environ["BUILD_STATUS"]))
elif name == "python3":
    if args[1] == "wait-volume": print("/Volumes/XIAO SENSE")
    elif args[1] == "flash": sys.exit(int(os.environ["FLASH_STATUS"]))
    elif args[1] == "wait-port":
        print("/dev/cu.usbmodemTEST")
        sys.exit(int(os.environ["PORT_STATUS"]))
'''
        for tool in ("python3", "cargo", "uname"):
            path = self.root / tool
            path.write_text(stub)
            path.chmod(0o755)
        log = self.root / "calls.jsonl"
        cases = [(0, 0, 0, True), (42, 0, 0, False), (0, 1, 0, False),
                 (0, 75, 0, True), (0, 75, 1, False)]
        for build, flash, port, success in cases:
            log.write_text("")
            env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}", TEST_LOG=str(log),
                       BUILD_STATUS=str(build), FLASH_STATUS=str(flash), PORT_STATUS=str(port))
            result = subprocess.run(["sh", str(fw.ROOT / "flash.sh"), "--usb-serial", "--no-monitor"],
                                    cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            flashed = ["python3", "tools/firmware.py", "flash", "--volume", "/Volumes/XIAO SENSE"] in calls
            with self.subTest(build=build, flash=flash, port=port):
                self.assertEqual(result.returncode == 0, success, result.stderr)
                self.assertEqual(flashed, build == 0)
                if success:
                    self.assertEqual(calls[-1][2], "wait-port")
                if flash == 75 and success:
                    self.assertIn("have not been read back", result.stdout)

        # BLE must never wait for a USB port, and cannot certify an interrupted
        # copy using an unrelated nearby beacon.
        for status in (0, 75):
            log.write_text("")
            env.update(BUILD_STATUS="0", FLASH_STATUS=str(status))
            result = subprocess.run(["sh", str(fw.ROOT / "flash.sh"), "--no-monitor",
                                     "--sleep-seconds", "10", "--adv-events", "1"],
                                    cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertFalse(any("wait-port" in call or "ports" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
