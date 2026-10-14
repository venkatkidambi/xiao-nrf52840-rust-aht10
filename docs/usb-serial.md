> This document describes the optional `usb-serial` diagnostic build and historical USB verification. The default application now uses BLE; start with [the main README](../README.md). Hardware results below apply to the earlier USB firmware, not the new BLE implementation.

# USB diagnostic firmware and bootloader reference

Native USB CDC-ACM firmware for the **Seeed Studio XIAO nRF52840**. It reads an AHT10 on D4/D5 once per second and sends UTF-8 text to a Mac:

```text
AHT10 initialized
Temperature: 23.42 °C | Humidity: 48.71 %
```

The readings above illustrate the format; they are not captured hardware results. On 2026-10-14, the user reported that flashing and running the firmware worked. The application USB identity and serial-port discovery have also been observed on this Mac. Sensor accuracy, timing, and electrical fault recovery have not been independently tested; see the verification section.

`memory.x` initially contained a **compile-only S140 7.x example**; `configure` replaces it with the connected board's layout. The supplied UF2/flash commands require the connected board's `INFO_UF2.TXT` and `CURRENT.UF2`, derive its application base, and reject mismatches. Always run the build helper with your current board's volume. Do not copy an image built for an unverified offset.

## Build, flash, and watch with one command

After the one-time tool installation below, run from this project directory:

```sh
./flash.sh --usb-serial
```

Connect the XIAO with USB-C. The script waits up to two minutes for its bootloader drive; **double-press RESET when prompted**. Close any previous serial terminal before starting. The script detects the volume, verifies its layout, runs formatting/check/release-build, generates UF2, copies it to the board, waits for the application's USB CDC device, and opens its serial output. A failed check/build stops the script before flashing.

The serial terminal is `picocom` when installed, otherwise macOS `screen`. Exit picocom with Ctrl-A then Ctrl-X; exit screen with Ctrl-A then `\` and confirm. The script prints the applicable shortcut. Device discovery uses the firmware's USB VID/PID, excludes other application ports already present before flashing, and rejects ambiguous matches.

To build and flash without opening a terminal:

```sh
./flash.sh --usb-serial --no-monitor
```

Both modes wait for the bootloader volume to disappear and a new application USB port to appear. On a disconnect during the final UF2 block, flush, or close, the script checks for that restart before continuing. It explicitly reports that the exact image contents have not been read back. Earlier write errors, permission errors, and an application that never reappears remain failures. This handles a board rebooting before macOS finishes the copy without treating every I/O error as success.

With multiple boards, select the intended volume (and optionally its application port):

```sh
./flash.sh --usb-serial --volume /Volumes/XIAO-SENSE
./flash.sh --usb-serial --volume /Volumes/XIAO-SENSE --port /dev/cu.usbmodem1101
```

Use `./flash.sh --usb-serial --build-only` for an offline compile/check/ELF inspection, or `./flash.sh --usb-serial --help` for options. The script also works when launched by its absolute path from another directory. It requires the tools below to be installed; Rust's pinned toolchain/dependencies may download on first use. RESET remains a physical step because this firmware does not implement a software bootloader-entry command.

Host tooling consists of **`flash.sh` and `tools/firmware.py`**. The shell owns the build/flash/monitor sequence; the standard-library Python utility handles USB metadata, flash-layout validation, and the ELF/UF2 binary formats. Focused regression tests live separately in `tests/test_tools.py`.

## Wiring

Disconnect power while wiring. Use the board's **3V3**, not its 5V pin.

| AHT10 | XIAO nRF52840 |
| --- | --- |
| VCC/VDD | 3V3 |
| GND | GND |
| SDA | D4 / P0.04 |
| SCL | D5 / P0.05 |

Use short wires and a data-capable USB-C cable. Most sensor breakouts include pull-ups; SDA and SCL must be pulled up to **3.3 V**. The firmware enables the MCU's internal pull-ups as well. A bare sensor needs suitable decoupling and bus pull-ups; a ready-made AHT10 module is simplest. There is no USB-to-UART adapter or debugger involved. The board pin mapping is confirmed by [Seeed's variant definition](https://github.com/Seeed-Studio/Adafruit_nRF52_Arduino/blob/master/variants/Seeed_XIAO_nRF52840/variant.cpp).

## Install tools on macOS

Install Apple's command-line tools if needed:

```sh
xcode-select --install
```

Install Rust using rustup if it is not already available:

```sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
. "$HOME/.cargo/env"
```

From this project directory:

```sh
rustup toolchain install 1.95.0 --profile minimal
rustup target add --toolchain 1.95.0 thumbv7em-none-eabihf
rustup component add --toolchain 1.95.0 rustfmt llvm-tools
python3 --version
```

The Python utility uses only the standard library. If Python or a serial terminal is missing, with Homebrew installed:

```sh
brew install python picocom
```

`rust-toolchain.toml` selects Rust 1.95.0. Direct dependency versions are exact; `Cargo.lock` locks transitive dependencies. Use `--locked` for repeatable builds. No Arduino, C/C++ firmware, OS, Nordic SDK installation, `nrfutil`, or SWD tooling is required.

## Identify the existing UF2 bootloader

Connect USB-C and **press RESET twice quickly**, within about half a second. A UF2 mass-storage volume should mount. See [Seeed's bootloader instructions](https://wiki.seeedstudio.com/XIAO_BLE/). The actual volume name can vary; do not assume it is always `XIAO-SENSE`.

```sh
ls /Volumes
find /Volumes -maxdepth 2 -name INFO_UF2.TXT -print 2>/dev/null
```

Set this to the actual volume found above:

```sh
UF2_VOLUME='/Volumes/XIAO-SENSE'
cat "$UF2_VOLUME/INFO_UF2.TXT"
ls -l "$UF2_VOLUME/CURRENT.UF2"
python3 tools/firmware.py probe --volume "$UF2_VOLUME"
```

`probe` only reads the board. It displays the bootloader information, detected application range, and a hash of the installed SoftDevice. Some non-Sense boards ship with a Sense bootloader. The tool accepts the modern `nRF52840-SeeedXiao-v1` and `nRF52840-SeeedXiaoSense-v1` IDs, plus the legacy `Seeed_XIAO_nRF52840_Sense` ID defined in the [legacy Seeed bootloader source](https://github.com/0hotpotman0/Adafruit_nRF52_Bootloader/blob/master/src/boards/Seeed_XIAO_nRF52840_Sense/board.h). All use these same D4/D5 connections. Other IDs remain rejected.

## Verify the layout, build, and generate UF2

`./flash.sh --usb-serial` performs the complete build and flash sequence. To prepare and inspect a UF2 **without copying it to the board**, keep the board in bootloader mode and run these individual commands:

```sh
python3 tools/firmware.py configure --volume "$UF2_VOLUME"
cargo fmt --check
cargo check --no-default-features --features usb-serial --locked --target thumbv7em-none-eabihf
cargo build --no-default-features --features usb-serial --locked --release --target thumbv7em-none-eabihf
python3 tools/firmware.py inspect
python3 tools/firmware.py uf2 --volume "$UF2_VOLUME"
```

Outputs:

| File | Purpose |
| --- | --- |
| `target/thumbv7em-none-eabihf/release/xiao-aht10` | Linked ARM ELF with debug information |
| `artifacts/xiao-aht10.uf2` | Checked application-only UF2 |
| `artifacts/layout.json` | Observed board/SoftDevice metadata |
| `artifacts/backup-*/INFO_UF2.TXT`, `CURRENT.UF2` | First readback saved for that SoftDevice hash |

The backup covers the exported region, **not the MBR, bootloader, or UICR**, and is not a complete recovery image. Do not flash it as part of these instructions. `configure` changes local `memory.x`; it never writes the board. Rebuild after every layout change. UF2 generation reads the connected board again rather than trusting `layout.json`.

The converter reads ELF `PT_LOAD` **physical/load addresses**, including the flash initializer for `.data`. It checks the initial SP, reset vector, hard-float ARM ABI, all flash/RAM segments, overlaps, and final padding. It emits 512-byte UF2 blocks with 256-byte payloads, family-present flag `0x2000`, and the nRF52840 **application family ID `0xADA52840`**. It never emits a bootloader-family image or UICR data. This follows the [UF2 format](https://github.com/microsoft/uf2/blob/master/README.md) and [Adafruit's nRF52 UF2 configuration](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/src/usb/uf2/uf2cfg.h).

For an offline compile-only check, run `./flash.sh --usb-serial --build-only`. That does **not** generate a UF2 or establish compatibility with an installed board.

## Flash using USB-C

This replaces the current application. Leave the existing MBR, SoftDevice, and bootloader installed.

The checked copy command re-reads the board and validates the ELF immediately before generating and copying UF2:

```sh
python3 tools/firmware.py flash --volume "$UF2_VOLUME"
```

Alternatively, immediately after successful UF2 generation for this same board:

```sh
cp artifacts/xiao-aht10.uf2 "$UF2_VOLUME/AHT10.UF2"
```

Use one of these methods, not both. The bootloader should finish programming and reboot automatically. Its volume then disappears; macOS may report that the disk was not ejected properly. Wait several seconds. A copy error alone does not prove success: check whether the application enumerates. If it stays in bootloader mode, press RESET once after the transfer has finished. To retry an application image, double-press RESET again. These commands perform no chip erase, bootloader update, or SoftDevice update; the bootloader may update its own application-validity settings as part of normal UF2 handling.

Prefer `./flash.sh --usb-serial` for automatic handling of disconnects at the end of copying. When called directly, `tools/firmware.py flash` returns status **75** for a possible final-block/flush disconnect; this means the result needs checking, not that the flash is confirmed. The wrapper checks for the returning application before opening the monitor. It never automatically retries a copy onto a volume that has disappeared.

## Open the application's serial port

The bootloader and application are **different USB devices**, even though they use the same USB-C connector:

| Mode | USB interfaces | What to use it for |
| --- | --- | --- |
| UF2 bootloader | Removable UF2 volume, often also a DFU CDC port | Copy firmware; no AHT10 readings |
| This application | CDC-ACM, product `XIAO AHT10 Serial`; no disk | Temperature/humidity output |

After flashing, discover the port again; its name can change:

```sh
find /dev -maxdepth 1 -name 'cu.usbmodem*' -print
system_profiler SPUSBDataType
```

In `system_profiler`, look for `XIAO AHT10 Serial` (development VID/PID `c0de:cafe`). The USB serial number comes from the MCU's device ID. Open the specific port shown on your Mac:

```sh
screen /dev/cu.usbmodemYOUR_PORT 115200
```

Exit `screen` with **Ctrl-A**, then **\\**, then confirm. Or:

```sh
picocom --baud 115200 --flow n /dev/cu.usbmodemYOUR_PORT
```

Exit `picocom` with **Ctrl-A**, then **Ctrl-X**. 115200 is a conventional CDC line setting; there is no UART baud-rate clock in this data path. The terminal must assert DTR, as these programs normally do. The initialization banner appears when a terminal is open and a first valid sample arrives, including after reconnecting. UTF-8 renders `°` correctly.

## Why the flash offset is verified instead of assumed

For the supported Adafruit/Seeed bootloader, the application start is `SD_SIZE_GET(MBR_SIZE)` when a SoftDevice is installed. It is not necessarily zero or `0x27000`. The [bootloader launch code](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/lib/sdk11/components/libraries/bootloader_dfu/bootloader.c) obtains this value from flash. Nordic's [S140 information-structure definitions](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/lib/softdevice/s140_nrf52_7.3.0/s140_nrf52_7.3.0_API/include/nrf_sdm.h) specify these locations:

| Absolute address | Meaning |
| --- | --- |
| `0x3000` | SoftDevice information structure size byte |
| `0x3004` | SoftDevice magic `0x51B1E5DB` |
| `0x3008` | Application base, including MBR reservation |
| `0x3010` | SoftDevice ID, expected S140 |
| `0x3014` | Encoded SoftDevice version |

The tool reads those bytes from the mounted `CURRENT.UF2`. It supports S140 **6.1.1** (`0x26000`), **7.2.0**, and **7.3.0** (`0x27000`), requiring the metadata's size to agree with the release. An optional `SoftDevice:` line in `INFO_UF2.TXT` must also agree. Older bootloaders may omit that line.

Do not use the first address of `CURRENT.UF2` as the application base. [GhostFAT's readback implementation](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/src/usb/uf2/ghostfat.c) exports a region beginning at `0x1000`, including the SoftDevice, and ending at its configured `USER_FLASH_END`. The converter requires a complete contiguous export and sets the application ceiling to the smaller of that end and `0xED000`, rounded down to a flash page. This also leaves the conventional high application-data area alone. A typical bootloader begins at `0xF4000`, with parameter/settings pages above it, as shown in the [bootloader linker script](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/linker/nrf52840.ld); the tool does not infer your board's actual bootloader start from that example.

The first 32 KiB of RAM are reserved, including the MBR forwarding word and the usual double-reset mailbox at `0x20007F7C`. Application RAM is `0x20008000..0x20040000`. The reset handler explicitly relocates VTOR to this application's linked vector table using `cortex-m-rt`'s `set-vtor` feature.

The [Adafruit bootloader startup path](https://github.com/adafruit/Adafruit_nRF52_Bootloader/blob/master/src/main.c) disables the SoftDevice before launching the application. This firmware never enables it. Thus Embassy owns interrupts, timers, and USB POWER events and uses `HardwareVbusDetect`; it does not need the BLE stack running. Preserving SoftDevice bytes and running SoftDevice are different requirements. The firmware contains no application flash-writing logic and does not change bootloader address settings. Embassy retains RESET-pin functionality; no reset-pin-as-GPIO or NFC-pin-as-GPIO feature is enabled, and debug protection/regulator-voltage changes are not requested.

### If discovery refuses your board

An absent `CURRENT.UF2`, unknown Board-ID/SoftDevice, nonstandard export, or mismatched metadata is a real compatibility limitation. Do not guess an offset, substitute a fabricated readback, erase anything, or install another bootloader to make this example fit.

Using only USB, first inspect the installed `INFO_UF2.TXT`, then locate the **matching bootloader release/source and board configuration**. Check `CODE_REGION_1_START`/`SD_SIZE_GET`, `DFU_APP_DATA_RESERVED`, the bootloader linker origin, `USER_FLASH_END`, and whether the handoff disables SoftDevice. A known-good application ELF/linker configuration for that exact release provides another useful cross-check. The bootloader version string alone does not establish the SoftDevice size. If the bootloader does not expose sufficient metadata, the automatic tool cannot verify the board, and this project deliberately provides only an offline build until its layout and handoff are established. No extra hardware is required for the supported factory UF2 path; no verified flashing claim is made for an unknown bootloader.

## Firmware behavior

The implementation uses Embassy nRF 0.11.0, Embassy USB 0.6.0, and Embassy executor 0.10.0 on `thumbv7em-none-eabihf`. It is `#![no_std]` / `#![no_main]`, with no allocator. HF and LF clocks use the XIAO's crystals. USB control handling, sensor work, CDC transmission, and CDC reception are cooperatively polled; all sensor delays yield.

The AHT10 driver uses address `0x38`, waits after power-up, sends soft reset `BA`, checks calibration, and initializes with `E1 08 00` if needed. It triggers `AC 33 00`, waits 80 ms, then polls bit 7 with a deadline. Bit 3 must indicate calibration. Six-byte frames hold 20-bit humidity and temperature values, converted with `RH = raw × 100 / 2²⁰` and `T = raw × 200 / 2²⁰ − 50`. Output rounds to two decimals. See the [Aosong AHT10 technical manual](https://arduino.ua/files/AOSONG_AHT10_EN.PDF), sections 5–6. Sampling is one second as requested; the manual suggests two seconds to reduce self-heating.

Each I²C transfer has a 25 ms deadline. Busy polling has a 250 ms deadline (plus a bounded final transfer). On an error, the firmware reports it, tries up to nine open-drain bus-clear clocks and a STOP, then retries initialization on the next cycle. A shorted line cannot be fixed by software, but it does not cause an unbounded sensor wait. A normal sample cycle is one second; prolonged errors can stretch that interval.

The pinned HAL's async TWIM future alone does not abort DMA on cancellation. `bus.rs` owns a short-lived TWIM instance for each transfer and drops it before buffers are released; the HAL disables the peripheral, followed by explicit barriers. Each subsequent transfer gets a fresh configuration. USB remains independently polled during timeouts/recovery. Hardware testing is still needed to confirm electrical recovery behavior.

A single-slot latest-result signal prevents USB backpressure from stopping sampling. Disconnected/slow terminals can miss readings rather than accumulating an unbounded backlog. Writes have deadlines and respect DTR. Sensor failures are ordinary results, not panics. Unexpected software panics reset the MCU. The example USB VID/PID is for local development; a distributed product needs appropriately assigned identifiers.

## Verification and reproduction

On 2026-10-14, the connected board's bootloader files were read successfully: bootloader `0.6.2-12-g459adc9-dirty`, Board-ID `Seeed_XIAO_nRF52840_Sense`, S140 7.3.0, application base `0x27000`, exported end `0xEA000`. The build helper now configures this board's application region as `0x27000..0xEA000`. A regression test covers this legacy identity and its smaller export range. The user subsequently reported success after flashing; the application's `c0de:cafe` USB device was observed at `/dev/cu.usbmodem1101`. The new one-command wrapper is checked with device-discovery tests and stubbed build/flash sequencing tests, not an additional physical flash cycle.

Performed on macOS with Rust 1.95.0 on 2026-10-13:

- `cargo fmt --check`: passed.
- `cargo check --no-default-features --features usb-serial --locked --target thumbv7em-none-eabihf`: passed.
- `cargo build --no-default-features --features usb-serial --locked --release --target thumbv7em-none-eabihf`: passed.
- ELF inspected with the included parser, `llvm-readobj`, `llvm-size`, and reset-handler disassembly.
- Four host tests of measurement decoding/formatting passed.
- Host-tool regressions passed using synthetic fixtures and mocked flash writes. The consolidated suite has seven table-driven tests covering board identities/layouts, protected flash addresses, malformed UF2, copy errors, device selection, and build/flash/restart sequencing.

The compile-only example ELF uses these addresses (ends exclusive):

| Item | Address / size |
| --- | --- |
| Vector table | `0x00027000`, 256 bytes |
| Reset vector | `0x00027101` (Thumb) |
| Flash load span | `0x00027000..0x0002F850`, 34,896 bytes including alignment |
| `.data` | 24 bytes at `0x20008000`, initializer in application flash |
| `.bss` | 2,304 bytes |
| Static RAM total | 2,328 bytes; excludes runtime stack |
| Initial stack pointer | `0x20040000` |

The reset disassembly confirms VTOR relocation before Rust initialization. The ELF's debug sections are not flashed. These addresses describe the checked-in example layout only; regenerate for your board.

Reproduce the host tests without trying to run an embedded binary on macOS:

```sh
python3 -m unittest discover -s tests -v
mkdir -p target
rustc --edition=2024 --test src/reading.rs -o target/reading-tests
./target/reading-tests
```

For independent ELF inspection with the Rust-provided LLVM tools:

```sh
RUST_HOST=$(rustc -vV | sed -n 's/^host: //p')
LLVM_BIN="$(rustc --print sysroot)/lib/rustlib/$RUST_HOST/bin"
ELF=target/thumbv7em-none-eabihf/release/xiao-aht10
"$LLVM_BIN/llvm-readobj" --file-headers --program-headers "$ELF"
"$LLVM_BIN/llvm-size" -A "$ELF"
"$LLVM_BIN/llvm-objdump" -d --disassemble-symbols=Reset "$ELF"
```

For further hardware validation, check valid readings, disconnect/reopen the terminal, and test an unplugged sensor. Sensor errors should remain visible and readings should resume after reconnecting correctly. These detailed timing and recovery checks have not been performed here.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No UF2 volume after double reset | Data-capable cable, USB port, reset timing; identify the existing bootloader before changing anything |
| A serial port exists but there are no readings | Ensure the UF2 disk has disappeared and `XIAO AHT10 Serial` is present; open the application's new port with DTR asserted |
| `I2C AddressNack` | Sensor power/ground, SDA/SCL not swapped, address `0x38`, actual AHT10 part |
| `I2C Timeout` | Stuck lines, shorts, pull-ups, long wires; USB should continue while retries occur |
| Calibration/busy errors | Verify the sensor is AHT10, not an AHT20 requiring a different initialization command |
| App never enumerates after a completed copy | Re-enter UF2, repeat layout discovery and build; check the bootloader handoff against its exact source |
| Garbled degree symbol | Use a UTF-8 terminal |

Project entry points are `src/main.rs`, `src/aht10.rs`, `src/bus.rs`, `src/reading.rs`, and `src/serial.rs`. Host tooling is `flash.sh` plus `tools/firmware.py`; host-tool regression tests are in `tests/test_tools.py`.
