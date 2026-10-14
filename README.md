# XIAO nRF52840 + AHT10: BLE, then sleep

The default firmware wakes, measures temperature/humidity, broadcasts the reading over BLE, and sleeps for **10 seconds**. Set the sleep interval when building or flashing. The Mac receiver listens without pairing, prints readings, deduplicates repeated advertisements, and reports sequence gaps.

The implementation is a small **transmit-only BLE advertiser**, using the nRF52840 radio directly. It has no connection, acknowledgment, GATT server, or running SoftDevice. Broadcasts are unencrypted and can be missed. Keep the Mac awake and the receiver running. Repeated events improve reception but do not guarantee delivery.

## Build, flash, receive

The project pins Rust 1.95.0 and the ARM target in `rust-toolchain.toml`. Install Rust/rustup and Python 3.10+ if needed; see the [toolchain and bootloader guide](docs/usb-serial.md). On macOS 12.3 or newer, install the receiver once:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
```

Connect USB-C, then run:

```sh
./flash.sh --sleep-seconds 10
```

Double-press RESET when prompted. The helper verifies the connected bootloader's flash layout, checks and builds firmware, copies UF2, and starts the BLE receiver. Allow Bluetooth access for your terminal when macOS requests it. BLE operation does not create an application USB port; double-press RESET to return to the UF2 bootloader for future flashing.

Useful commands:

```sh
# Change sleep interval to five minutes.
./flash.sh --sleep-seconds 300

# Minimum radio burst: one event, covering all three advertising channels.
./flash.sh --sleep-seconds 10 --adv-events 1

# More opportunities for a busy Mac to receive each reading.
./flash.sh --sleep-seconds 10 --adv-events 10

# Flash only, or compile/inspect without a board.
./flash.sh --no-monitor --sleep-seconds 10
./flash.sh --build-only

# Receive independently; optional machine-readable JSON lines.
.venv/bin/python tools/ble_receive.py
.venv/bin/python tools/ble_receive.py --json
.venv/bin/python tools/ble_receive.py --count 3 --timeout 45

# With multiple boards, use the Mac device UUID printed by the receiver.
.venv/bin/python tools/ble_receive.py --device YOUR-MAC-DEVICE-UUID
```

`--sleep-seconds` accepts 1..604800 seconds; `--adv-events` accepts 1..100 events. Defaults are 10 seconds and 3 events. These settings are compiled into the image, so changing them requires a reflash. Direct Cargo builds also accept `SLEEP_SECONDS=300 ADV_EVENTS=3 cargo build --locked --release`; `build.rs` validates them and tracks environment changes. The interval is **sleep after the final transmission**, so sample start times are slightly more than the chosen interval apart. Reset restarts sampling immediately after sensor initialization.

The helper supports `--volume PATH` for multiple attached boards. `BLE_PYTHON` can select another Python environment. `--port` applies only to USB diagnostics.

A completed UF2 copy is not proof of correct firmware operation. If USB disconnects during the final block/flush, the BLE helper exits **75**, explicitly reporting an unverified flash outcome. Start the receiver and check the intended board before retrying. Unlike the USB build, it cannot identify that board by a returning USB port; a nearby AHT10 beacon is not sufficient evidence. An offline build uses the existing `memory.x` and does not establish compatibility with another board.

## Wiring

| AHT10 | XIAO nRF52840 |
| --- | --- |
| VCC | 3V3 |
| GND | GND |
| SDA | D4 / P0.04 |
| SCL | D5 / P0.05 |

The sensor stays powered and returns to its own standby between measurements. Use 3.3 V I²C pull-ups, normally fitted to an AHT10 breakout. The MCU's internal pull-ups are enabled only during transfers; use external pull-ups to keep the sleeping bus defined. The firmware keeps the existing 100 kHz bus rate for wiring compatibility. Disconnect power when changing wiring.

## Sleep and timing

The nRF52840's **System OFF cannot wake on an internal RTC**. This firmware therefore uses low-power **System ON with RAM retained**, Embassy's sleeping executor, and RTC1 driven by the 32.768 kHz crystal. True System OFF with timed wake would require an external RTC/timer and a wake pin. See [Nordic's power specification](https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/power.html).

During the long sleep, radio, high-frequency crystal, USB, RNG, and TWIM are inactive. RTC driver bookkeeping may briefly wake the CPU during long intervals. There is no active polling during the sleep interval or sensor conversion. Each radio event uses interrupts and hardware READY→START / END→DISABLE shortcuts; dropping a cancelled event resets radio DMA before releasing its buffer. Radio and HFXO are switched off even between repeated events.

The BLE path converts raw sensor values to hundredths using integer arithmetic, avoiding floating-point work and the nRF52840 [FPU sleep erratum](https://docs.nordicsemi.com/r/bundle/errata_nrf52840_rev3/page/err/nrf52840/rev3/latest/anomaly_840_87.html). Normal cycles retain sensor calibration. The driver triggers conversion, sleeps for 80 ms, and reads status plus the measurement in one frame. Initialization and power-up delays occur only at boot or after errors. The [AHT10 manual](https://arduino.ua/files/AOSONG_AHT10_EN.PDF) specifies the conversion wait; reducing it blindly would trade correctness for nominal speed. Sensor transactions have deadlines, the complete sample attempt is capped at 600 ms, and errors are broadcast as error codes instead of stale readings. Bus recovery runs once on failure, then the board sleeps normally.

Each advertising event transmits on channels 37, 38, and 39 at BLE 1 Mbit/s, 0 dBm. Event starts are separated by 20 ms plus a pseudorandom 0..10 ms delay seeded by hardware RNG. This follows the current [Bluetooth advertising interval rules](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Core-61/out/en/low-energy-controller/link-layer-specification.html). There is no receive window or scan response. The default three events suggest approximately **125–150 ms elapsed per normal measurement/burst**, most of which is CPU sleep. One event approaches the 80 ms sensor conversion floor. These are design estimates, not measured hardware timings.

Board-level power setup turns off the RGB LEDs, disables the battery divider, cuts power to the unused Sense IMU and microphone, sends `B9` deep-power-down to the onboard SPI NOR when fitted, and enables the MCU's DC/DC regulator. Pins follow [Seeed's board definition](https://github.com/Seeed-Studio/Adafruit_nRF52_Arduino/blob/master/variants/Seeed_XIAO_nRF52840/variant.cpp) and [schematic](https://files.seeedstudio.com/wiki/XIAO-BLE/Seeed_Studio_XIAO_nRF52840_PDF.pdf). The charging-current selection is left alone. Full RAM retention is kept to preserve application state safely.

Measure consumption on battery power with USB/debug hardware disconnected. The regulator, charger, sensor breakout LEDs, external pull-ups, and any other connected hardware contribute to total current. Software alone cannot establish a board-level sleep current or battery lifetime.

## Packet format

The legacy `ADV_NONCONN_IND` PDU uses a factory-derived random static address, standard BLE advertising access address, hardware CRC, and channel-specific whitening. Advertising data contains flags and manufacturer data; it deliberately omits a name to minimize payload and avoid scan-response dependencies.

Manufacturer ID `0xffff` is a **local development/test identifier**, not an assigned company identity for a distributed product. The 22-byte manufacturer payload uses little endian fields, matching Python `struct.Struct("<4sBBIIhHI")`:

| Field | Bytes | Meaning |
| --- | --- | --- |
| Magic | 4 | ASCII `AHT1` |
| Version | 1 | `1` |
| Status | 1 | 0 valid, 1 I²C, 2 busy, 3 uncalibrated, 4 sample deadline |
| Boot ID | 4 | Hardware random value generated each boot |
| Sequence | 4 | Incremented each sample attempt; wraps at 2³² |
| Temperature | 2 | Signed hundredths of °C |
| Humidity | 2 | Unsigned hundredths of % RH |
| Sleep seconds | 4 | Configured delay after burst |

Error frames zero the measurement fields; the receiver outputs null measurements and a named error. Boot ID plus sequence suppress repeated events without confusing a reboot with a duplicate. Sequence gaps count missed sample attempts within an observed boot, not verified RF losses. A first reading or new boot has no computable gap. macOS exposes a device UUID rather than the radio MAC address; see [Bleak's macOS backend](https://bleak.readthedocs.io/en/latest/backends/macos.html).

## USB diagnostics and bootloader preservation

The previous continuous USB serial application remains available:

```sh
./flash.sh --usb-serial
./flash.sh --usb-serial --build-only
```

This diagnostic mode samples once per second and keeps USB active. It does not provide BLE or the duty-cycled power behavior. The [USB reference](docs/usb-serial.md) preserves detailed bootloader identification, verified memory-layout rules, wiring references, and historical hardware results.

Both modes preserve the installed MBR, SoftDevice, and UF2 bootloader. The installed SoftDevice is not enabled. Application interrupts use this image's relocated vector table. Flash layout is still derived from the attached board's `INFO_UF2.TXT` and `CURRENT.UF2`; no chip erase or guessed flash offset is introduced.

## Verification

The BLE work has passed compilation for both modes, release builds, ELF layout inspection, host tests of sensor conversion and BLE packet bytes, and Python regressions for receiver parsing, duplicate/gap/reboot handling, and flash sequencing. Historical USB success does **not** validate the new radio implementation. BLE reception, wake timing, NOR deep-power-down, and actual sleep current still require physical board testing.

Reproduce software checks:

```sh
cargo fmt --check
cargo check --locked
cargo check --locked --features usb-serial
cargo build --locked --release --features usb-serial
./flash.sh --build-only
python3 -m unittest discover -s tests -v
rustc --edition=2024 --test src/reading.rs -o target/reading-tests
./target/reading-tests
rustc --edition=2024 --test src/packet.rs -o target/packet-tests
./target/packet-tests
```

For hardware acceptance, first flash with a 10-second interval and receive at least three samples from the intended board. Confirm stable temperature/humidity, sequence increments, configured interval, and error frames with the sensor disconnected. Then compare one-event and three-event reception while profiling battery current: verify the approximately 80 ms conversion wait, bounded RF burst, and low-current interval after the burst. Repeat without a receiver present to confirm sleep does not depend on the Mac.
