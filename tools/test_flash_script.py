"""Exercise shell sequencing with stubbed tools; no physical board is written."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FlashScriptTests(unittest.TestCase):
    def run_script(self, fail="", monitor=False, flash_status=0, port_status=0):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            log = tmp / "calls.jsonl"
            stub = f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["SCRIPT_TEST_LOG"], "a") as file:
    file.write(json.dumps([name, *args]) + "\\n")
if name == "uname":
    print("Darwin")
elif name == "cargo" and args[0] == os.environ.get("SCRIPT_TEST_FAIL"):
    sys.exit(42)
elif name == "python3" and args[:2] == ["tools/device.py", "wait-volume"]:
    print(args[args.index("--volume") + 1] or "/Volumes/XIAO SENSE")
elif name == "python3" and args[:2] == ["tools/firmware.py", "flash"]:
    sys.exit(int(os.environ["SCRIPT_TEST_FLASH_STATUS"]))
elif name == "python3" and args[:2] == ["tools/device.py", "wait-port"]:
    print("/dev/cu.usbmodemTEST")
    sys.exit(int(os.environ["SCRIPT_TEST_PORT_STATUS"]))
'''
            for tool in ("python3", "cargo", "uname"):
                path = tmp / tool
                path.write_text(stub)
                path.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}", SCRIPT_TEST_LOG=str(log), SCRIPT_TEST_FAIL=fail)
            env.update(SCRIPT_TEST_FLASH_STATUS=str(flash_status), SCRIPT_TEST_PORT_STATUS=str(port_status))
            args = ["sh", str(ROOT / "flash.sh"), "--volume", "/Volumes/XIAO SENSE"]
            if not monitor:
                args.append("--no-monitor")
            result = subprocess.run(args, cwd=tmp, env=env, capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            return result, calls

    def test_success_flashes_only_after_release_build(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        release = calls.index(["cargo", "build", "--locked", "--release", "--target", "thumbv7em-none-eabihf"])
        flash = calls.index(["python3", "tools/firmware.py", "flash", "--volume", "/Volumes/XIAO SENSE"])
        self.assertLess(release, flash)
        self.assertEqual(calls[-1][:3], ["python3", "tools/device.py", "wait-port"])

    def test_final_disconnect_waits_for_application(self):
        result, calls = self.run_script(flash_status=75)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("have not been read back", result.stdout)
        self.assertEqual(calls[-1][:3], ["python3", "tools/device.py", "wait-port"])

    def test_disconnect_without_application_remains_failure(self):
        result, _ = self.run_script(flash_status=75, port_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Done:", result.stdout)

    def test_ordinary_copy_failure_is_not_ignored(self):
        result, calls = self.run_script(flash_status=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:3] == ["python3", "tools/device.py", "wait-port"] for call in calls))

    def test_failed_build_never_flashes(self):
        result, calls = self.run_script(fail="build")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[:3] == ["python3", "tools/firmware.py", "flash"] for call in calls))

    def test_noninteractive_monitor_fails_before_flash(self):
        result, calls = self.run_script(monitor=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--no-monitor", result.stderr)
        self.assertFalse(any(call[0] == "python3" for call in calls))


if __name__ == "__main__":
    unittest.main()
