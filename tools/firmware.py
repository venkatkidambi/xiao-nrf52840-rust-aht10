#!/usr/bin/env python3
"""Read-only board discovery, checked ELF32 -> UF2, and explicit USB flashing.

Python 3 standard library only. No objcopy, nrfutil, probe, or mass erase.
See README for the supported Adafruit/Seeed bootloader assumptions.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ELF = ROOT / "target/thumbv7em-none-eabihf/release/xiao-aht10"
FAMILY = 0xADA52840
MAGIC = (0x0A324655, 0x9E5D5157, 0x0AB16F30)
RAM_START, RAM_END = 0x20008000, 0x20040000
SAFE_END = 0xED000
# Legacy Seeed spelling is defined in:
# 0hotpotman0/Adafruit_nRF52_Bootloader/src/boards/Seeed_XIAO_nRF52840_Sense/board.h
BOARD_IDS = (
    "nRF52840-SeeedXiao-v1",
    "nRF52840-SeeedXiaoSense-v1",
    "Seeed_XIAO_nRF52840_Sense",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


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
    ports = set()

    def walk(node, vendor=None, product=None):
        vendor, product = node.get("idVendor", vendor), node.get("idProduct", product)
        port = node.get("IOCalloutDevice", "")
        if (vendor, product) == (0xC0DE, 0xCAFE) and port.startswith("/dev/cu.usbmodem"):
            ports.add(port)
        for child in node.get("IORegistryEntryChildren", []):
            walk(child, vendor, product)

    for root in [registry] if isinstance(registry, dict) else registry:
        walk(root)
    return sorted(ports)


def application_ports():
    data = subprocess.check_output(["ioreg", "-a", "-l", "-p", "IOService"], timeout=10)
    return ports_from_registry(plistlib.loads(data))


def wait_device(args):
    is_volume = args.command == "wait-volume"
    requested = str(args.volume.resolve()) if is_volume and args.volume else args.port
    deadline = time.monotonic() + (120 if is_volume else 60)
    while time.monotonic() < deadline:
        if is_volume:
            candidates = bootloader_volumes()
        else:
            candidates = [p for p in application_ports() if p not in args.exclude.splitlines()]
            if args.volume and args.volume.exists():
                candidates = []  # Wait for the old bootloader volume to disappear.
        if requested:
            candidates = [p for p in candidates if p == requested]
        require(len(candidates) <= 1,
                f"Multiple devices: {', '.join(candidates)}. Select one with --{'volume' if is_volume else 'port'} PATH.")
        if candidates:
            print(candidates[0])
            return
        time.sleep(1)
    hint = "Double-press RESET and check the cable." if is_volume else "Application restart was not confirmed; check the board before retrying."
    raise ValueError(f"Timed out waiting for {'bootloader' if is_volume else 'application'}. {hint}")


def decode_uf2(data):
    require(data and len(data) % 512 == 0, "UF2 must contain complete 512-byte blocks")
    blocks, numbers = {}, set()
    count = len(data) // 512
    families = set()
    for offset in range(0, len(data), 512):
        a, b, flags, addr, size, number, total, family = struct.unpack_from("<8I", data, offset)
        end_magic = struct.unpack_from("<I", data, offset + 508)[0]
        require((a, b, end_magic) == MAGIC, "Invalid UF2 magic")
        require(flags == 0x2000, "Expected ordinary family-ID flash blocks")
        require(size == 256 and addr % 256 == 0, "Expected aligned 256-byte payloads")
        require(total == count and number < count, "Incomplete UF2 block set")
        require(number not in numbers and addr not in blocks, "Duplicate UF2 block/address")
        require(0x1000 <= addr and addr + size <= 0x100000, "UF2 outside readable user flash")
        numbers.add(number)
        families.add(family)
        blocks[addr] = data[offset + 32:offset + 32 + size]
    require(len(families) == 1, "Mixed UF2 family IDs")
    require(next(iter(families)) in (FAMILY, 0x28860044, 0x28860045), "Not a XIAO/nRF52840 UF2")
    return blocks


def installed_layout(volume):
    info_data = (volume / "INFO_UF2.TXT").read_bytes()
    info = info_data.decode("utf-8")
    require(info.startswith("UF2 Bootloader "), "Not an Adafruit-style UF2 bootloader")
    match = re.search(r"^Board-ID:\s*(\S+)", info, re.M)
    board_id = match[1] if match else "<missing>"
    require(board_id in BOARD_IDS,
            f"Unrecognized XIAO Board-ID {board_id!r}; inspect bootloader source before proceeding")
    current = (volume / "CURRENT.UF2").read_bytes()
    blocks = decode_uf2(current)
    addresses = sorted(blocks)
    require(addresses == list(range(0x1000, addresses[-1] + 256, 256)),
            "CURRENT.UF2 must export contiguous flash from 0x1000 including the SoftDevice")
    image = b"".join(blocks[a] for a in addresses)

    def word(address):
        return struct.unpack_from("<I", image, address - 0x1000)[0]

    require(len(image) >= 0x2018 and image[0x2000] >= 0x18,
            "Missing SoftDevice information structure")
    require(word(0x3004) == 0x51B1E5DB, "SoftDevice magic absent; refusing to guess the layout")
    start, sd_id, version = word(0x3008), word(0x3010), word(0x3014)
    # SD_SIZE_GET(MBR_SIZE) is already the absolute application base. Do not
    # add 0x1000 again (SD_FLASH_SIZE is a different, compile-time constant).
    known = {6001001: 0x26000, 7002000: 0x27000, 7003000: 0x27000}
    require(sd_id == 140 and version in known, "Unsupported SoftDevice; verify its headers/source")
    require(start == known[version], "SoftDevice size disagrees with the supported release")
    version_text = f"{version // 1000000}.{version // 1000 % 1000}.{version % 1000}"
    reported = re.search(r"^SoftDevice:\s*(.*?)\s*$", info, re.M)
    if reported:
        require(reported[1] == f"S140 {version_text}", "INFO_UF2 and flash metadata disagree")
    # GhostFAT exports only [USER_FLASH_START, USER_FLASH_END), below its
    # bootloader/app-data reservation. Cap further at the conservative ED000.
    exported_end = addresses[-1] + 256
    end = min(exported_end, SAFE_END) & ~0xFFF
    require(start < end <= SAFE_END, "Invalid application bounds")
    require(start - 0x1000 <= len(image), "SoftDevice extends beyond exported flash")
    layout = {
        "board_id": match[1], "bootloader": info.splitlines()[0],
        "softdevice": f"S140 {version_text}", "flash_start": start, "flash_end": end,
        "exported_end": exported_end,
        "softdevice_sha256": hashlib.sha256(image[:start - 0x1000]).hexdigest(),
    }
    return layout, info_data, current


def memory_bounds():
    text = (ROOT / "memory.x").read_text()
    match = re.search(r"FLASH\s*:\s*ORIGIN\s*=\s*(0x[0-9a-fA-F]+),\s*LENGTH\s*=\s*(0x[0-9a-fA-F]+)", text)
    require(match, "Cannot parse memory.x FLASH bounds")
    start, length = (int(v, 16) for v in match.groups())
    return start, start + length


def inspect_elf(path, start, end):
    data = path.read_bytes()
    require(len(data) >= 52 and data[:7] == b"\x7fELF\x01\x01\x01", "Expected little-endian ELF32")
    header = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    typ, machine, _, entry, phoff, _, flags, _, phsize, phnum, *_ = header
    require(typ == 2 and machine == 40 and flags & 0x400, "Expected ARM executable with hard-float ABI")
    require(phsize == 32 and phoff + phsize * phnum <= len(data), "Invalid ELF program headers")
    segments, ram_bytes = [], 0
    for i in range(phnum):
        kind, offset, vaddr, paddr, filesz, memsz, perms, _ = struct.unpack_from("<8I", data, phoff + i * phsize)
        if kind != 1:
            continue
        require(filesz <= memsz and offset + filesz <= len(data), "Invalid LOAD segment")
        if memsz:
            in_ram = RAM_START <= vaddr and vaddr + memsz <= RAM_END
            in_flash = start <= vaddr and vaddr + memsz <= end
            require(in_ram or in_flash, f"LOAD runtime address outside application memory: {vaddr:#x}")
            if in_ram:
                ram_bytes += memsz
        if filesz:
            require(start <= paddr and paddr + filesz <= end, f"LOAD would overwrite reserved flash: {paddr:#x}")
            segments.append((paddr, data[offset:offset + filesz], vaddr, perms))
    segments.sort()
    require(segments and segments[0][0] == start, "ELF does not start at installed application base; rebuild")
    last = start
    for addr, payload, _, _ in segments:
        require(addr >= last, "Overlapping ELF LOAD segments")
        last = addr + len(payload)
    binary = bytearray(b"\xff" * (last - start))
    for addr, payload, _, _ in segments:
        binary[addr - start:addr - start + len(payload)] = payload
    require(len(binary) >= 8, "Missing vector table")
    sp, reset = struct.unpack_from("<II", binary)
    require(RAM_START < sp <= RAM_END and sp % 8 == 0, "Invalid initial stack pointer")
    require(reset & 1 and reset == entry, "Reset vector/ELF entry mismatch or missing Thumb bit")
    require(any(perms & 1 and vaddr <= (reset & ~1) < vaddr + len(payload)
                for _, payload, vaddr, perms in segments), "Reset handler outside executable segment")
    padded_end = start + (len(binary) + 255) // 256 * 256
    require(padded_end <= end, "UF2 padding would extend beyond application flash")
    for addr, payload, vaddr, _ in segments:
        print(f"LOAD flash {addr:#010x}..{addr + len(payload):#010x}, runtime {vaddr:#010x}")
    print(f"Vector table {start:#010x}; reset {reset:#010x}; initial SP {sp:#010x}")
    print(f"Flash span {len(binary)} / {end - start} bytes; RAM LOAD allocation {ram_bytes} / {RAM_END - RAM_START} bytes")
    print("RAM figure excludes runtime stack; low 32 KiB reserved. End addresses are exclusive.")
    return bytes(binary)


def encode_uf2(binary, start):
    require(binary and start % 4096 == 0, "Empty image or unaligned application base")
    count = (len(binary) + 255) // 256
    result = bytearray()
    for number in range(count):
        payload = binary[number * 256:(number + 1) * 256].ljust(256, b"\xff")
        result += struct.pack("<8I", MAGIC[0], MAGIC[1], 0x2000, start + number * 256, 256, number, count, FAMILY)
        result += payload + bytes(220) + struct.pack("<I", MAGIC[2])
    return bytes(result)


class Uf2Disconnect(Exception):
    """A final-block/flush disconnect: outcome requires application detection."""


def copy_uf2(destination, uf2):
    require(uf2 and len(uf2) % 512 == 0, "Expected complete UF2 blocks")
    submitted = 0
    # Open failures (permissions, absent volume) are never reboot candidates.
    stream = destination.open("wb", buffering=0)
    try:
        with stream:
            while submitted < len(uf2):
                # Track progress at UF2-block granularity, including short writes.
                end = min((submitted // 512 + 1) * 512, len(uf2))
                written = stream.write(uf2[submitted:end])
                require(written is not None and 0 < written <= end - submitted,
                        "UF2 write made no progress")
                submitted += written
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if (error.errno in (errno.EIO, errno.ENODEV, errno.ENXIO)
                and submitted >= len(uf2) - 512):
            raise Uf2Disconnect(
                f"USB copy disconnected at the final block or flush ({submitted}/{len(uf2)} bytes submitted). "
                "The board may have rebooted; application reappearance must be checked."
            ) from error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("wait-volume", "ports", "wait-port", "probe", "configure", "inspect", "uf2", "flash"))
    parser.add_argument("--volume", type=Path, help="Mounted XIAO bootloader volume")
    parser.add_argument("--port", default="")
    parser.add_argument("--exclude", default="", help="Existing application ports, separated by newlines")
    parser.add_argument("--elf", type=Path, default=ELF)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/xiao-aht10.uf2")
    args = parser.parse_args()
    if args.command == "ports":
        print("\n".join(application_ports()))
        return
    if args.command.startswith("wait-"):
        wait_device(args)
        return
    if args.command == "inspect":
        inspect_elf(args.elf, *memory_bounds())
        print("Offline inspection only: memory.x is not evidence of an installed board layout.")
        return
    require(args.volume is not None, "--volume is required; flashing offsets are never guessed")
    layout, info, current = installed_layout(args.volume)
    print(f"{layout['board_id']}: {layout['softdevice']}, application {layout['flash_start']:#x}..{layout['flash_end']:#x}")
    if args.command == "probe":
        print(info.decode().strip())
        return
    start, end = layout["flash_start"], layout["flash_end"]
    if args.command == "configure":
        (ROOT / "memory.x").write_text(
            "/* Generated from installed CURRENT.UF2 metadata by tools/firmware.py.\n"
            " * Re-run configure when changing boards/bootloaders; rebuild afterward. */\n"
            "MEMORY\n{\n"
            f"  FLASH : ORIGIN = 0x{start:08X}, LENGTH = 0x{end - start:08X}\n"
            f"  RAM   : ORIGIN = 0x{RAM_START:08X}, LENGTH = 0x{RAM_END - RAM_START:08X}\n}}\n"
        )
        # Keep the first backup for this board/SoftDevice instead of replacing
        # it on every build. CURRENT.UF2 does not include the bootloader/MBR.
        backup = ROOT / "artifacts" / ("backup-" + layout["softdevice_sha256"][:16])
        backup.mkdir(parents=True, exist_ok=True)
        for name, contents in (("INFO_UF2.TXT", info), ("CURRENT.UF2", current)):
            with_path = backup / name
            if not with_path.exists():
                with_path.write_bytes(contents)
        (ROOT / "artifacts/layout.json").write_text(json.dumps(layout, indent=2) + "\n")
        print("Configured memory.x. Now rebuild before generating UF2.")
        return
    require(memory_bounds() == (start, end), "memory.x disagrees with board; run configure and rebuild")
    binary = inspect_elf(args.elf, start, end)
    uf2 = encode_uf2(binary, start)
    blocks = decode_uf2(uf2)
    require(min(blocks) == start and max(blocks) + 256 <= end, "UF2 range check failed")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(uf2)
    print(f"Wrote {args.output}: {len(blocks)} blocks, family {FAMILY:#010x}")
    if args.command == "flash":
        # This is the only operation in this tool that writes to the board.
        # The bootloader may disconnect immediately after the final block.
        destination = args.volume / "AHT10.UF2"
        copy_uf2(destination, uf2)
        print("UF2 copy completed. Verify readings over BLE (or USB in the diagnostic build); copy success is not a hardware test.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Uf2Disconnect as error:
        print(f"NOTICE: {error}", file=sys.stderr)
        sys.exit(75)  # flash.sh verifies device reappearance; not unconditional success.
    except (ValueError, OSError, struct.error, UnicodeError, subprocess.SubprocessError, plistlib.InvalidFileException) as error:
        sys.exit(f"ERROR: {error}")
