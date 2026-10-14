"""Simulate file-write failures without writing to hardware."""
import errno
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import firmware as fw


class CopyTests(unittest.TestCase):
    def transfer(self, writes=None, sync_error=None, close_error=None):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.write.side_effect = writes or (lambda data: len(data))
        stream.__exit__.side_effect = close_error
        with patch.object(Path, "open", return_value=stream), patch.object(fw.os, "fsync", side_effect=sync_error):
            fw.copy_uf2(Path("/unused/test.uf2"), bytes(1536))
        return stream

    def test_short_writes_are_completed(self):
        stream = self.transfer(writes=[128, 384, 512, 512])
        self.assertEqual(stream.write.call_count, 4)

    def test_final_block_disconnect_is_distinct(self):
        with self.assertRaises(fw.Uf2Disconnect):
            self.transfer(writes=[512, 512, OSError(errno.EIO, "disconnected")])

    def test_flush_or_close_disconnect_is_distinct(self):
        for stage in ("sync_error", "close_error"):
            with self.subTest(stage=stage), self.assertRaises(fw.Uf2Disconnect):
                self.transfer(**{stage: OSError(errno.ENXIO, "gone")})

    def test_early_io_failure_is_not_treated_as_reboot(self):
        with self.assertRaises(OSError):
            self.transfer(writes=[512, OSError(errno.EIO, "failed")])

    def test_disk_full_is_not_treated_as_reboot(self):
        with self.assertRaises(OSError):
            self.transfer(sync_error=OSError(errno.ENOSPC, "full"))

    def test_zero_write_is_failure(self):
        with self.assertRaisesRegex(ValueError, "no progress"):
            self.transfer(writes=[0])

    def test_open_failure_is_not_treated_as_reboot(self):
        with patch.object(Path, "open", side_effect=OSError(errno.EIO, "failed")):
            with self.assertRaises(OSError):
                fw.copy_uf2(Path("/unused/test.uf2"), bytes(512))


if __name__ == "__main__":
    unittest.main()
