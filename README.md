# XIAO nRF52840 + AHT10

A low-power temperature and humidity sensor built with **bare-metal Rust**. The XIAO reads an AHT10, sends readings over Bluetooth Low Energy (BLE), and sleeps between work. A Python receiver on your Mac saves each reading to SQLite before acknowledging it to the board.

Choose between short BLE connections and a persistent connection. Both use the same sensor, receiver, and acknowledged delivery protocol.

## Quick start

### 1. Wire the sensor

You need a Seeed Studio XIAO nRF52840, an AHT10 breakout, a data-capable USB-C cable, and a Mac running macOS 12.3 or newer.

Disconnect power before wiring. Use **3.3 V** for the sensor and its I²C pull-ups; most AHT10 breakouts include pull-ups.

| AHT10 | XIAO nRF52840 |
| --- | --- |
| VCC | 3V3 |
| GND | GND |
| SDA | D4 / P0.04 |
| SCL | D5 / P0.05 |

### 2. Install tools on macOS

Install Rust through rustup, Python 3.10+, Git, and the Xcode Command Line Tools (`xcode-select --install`). The project pins Rust 1.95.0 and its ARM target in [rust-toolchain.toml](rust-toolchain.toml). See the [tool installation guide](docs/usb-serial.md#install-tools-on-macos) for commands.

BLE builds also need libclang to generate controller bindings. If it cannot be found, set `LIBCLANG_PATH` to your installed LLVM library directory.

From the project directory, install the receiver dependencies once:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
```

### 3. Flash and receive

Connect USB-C and run:

```sh
./flash.sh
```

**Double-press RESET when prompted.** The helper verifies the board's bootloader layout, checks and builds the firmware, copies it as UF2, and starts the receiver. Allow Bluetooth access for your terminal, and keep the Mac awake while collecting readings.

By default, the board samples, tries delivery for **up to 1.5 seconds**, then sleeps for **10 seconds**. Delivery finishes early when all queued readings are acknowledged. Readings are saved in `artifacts/readings.sqlite3`.

BLE firmware does not expose a USB serial port. Double-press RESET again to enter the bootloader for future flashing. The installed MBR, S140, and UF2 bootloader are preserved.

## Choose a BLE mode

| | Disconnected (default) | Persistent (`--persistent`) |
| --- | --- | --- |
| Connection | Connect for delivery, then disconnect | Keep a healthy connection open |
| Sampling | Sleep after sensing and delivery | Independent start-to-start sampling period |
| Delivery | Mac reads each queued sample | Board notifies when a sample is ready |
| Between readings | BLE controller disabled | CPU sleeps between scheduled BLE events |
| Energy tradeoff | No idle connection traffic | Avoid repeated advertising, connection setup, and service discovery |

```sh
# Default mode with five minutes of sleep after each cycle.
./flash.sh --sleep-seconds 300

# Retain the connection and sample every ten seconds.
./flash.sh --persistent --sleep-seconds 10
```

Persistent mode reduces setup work for frequent readings. Disconnected mode may use less energy for sparse readings. **Neither mode has a measured battery-life guarantee**: compare average current at the same actual sampling cadence on your hardware. In default mode, ten seconds of sleep means readings are more than ten seconds apart.

The receiver detects the mode automatically. Restart it after switching firmware modes.

## Why bare metal?

This device has a focused job: measure, deliver, and sleep. Bare metal gives the firmware direct control over when hardware runs, how much memory it uses, and how long it spends attempting work. That makes it a good fit for a small battery-powered sensor.

Here, **bare metal means `no_std` Rust running without an OS, RTOS, or heap allocator**. Embassy provides asynchronous tasks and interrupt-driven timers, so sensor waits and BLE work can overlap while the CPU sleeps whenever no task is ready. Nordic's SoftDevice Controller (SDC) and Multiprotocol Service Layer (MPSL) handle radio timing; the bootloader-installed S140 remains disabled.

This approach lets us:

- **Control hardware lifetime.** Enable I²C only for transfers, shut down the BLE controller between disconnected attempts, and turn off unused board components.
- **Make memory use predictable.** Tasks and buffers are statically allocated, and the reading queue has a fixed capacity. There is no heap allocation or fragmentation to manage.
- **Bound unsuccessful work.** Sensor deadlines, delivery deadlines, and retry backoff prevent a missing sensor or receiver from causing endless retries within an attempt.
- **Tailor concurrency to the device.** Persistent sampling continues independently of delivery, without an RTOS thread and separate stack for each activity.

The tradeoff is more responsibility in the firmware: peripheral shutdown, interrupt ownership, cancellation, bus recovery, and bootloader layout must be handled correctly. Bare metal gives us control over those choices; low power still depends on the implementation and physical measurements.

## Optimizations already in the firmware

These optimizations apply to the BLE builds; USB diagnostics keep USB active.

| Optimization | What it does and why it helps |
| --- | --- |
| Sleep during waits | Embassy uses interrupts and RTC timers instead of busy-waiting between readings or throughout sensor conversion. System ON sleep retains RAM and allows timed wakeups. |
| Short peripheral lifetimes | The I²C controller (TWIM) is disabled between transfers, including the sensor's 80 ms conversion wait. The AHT10 stays powered in standby and retains calibration during normal operation. |
| Less board overhead | RGB LEDs and the battery divider are disabled, unused Sense IMU/microphone power is cut, fitted SPI flash enters deep power-down, and the DC/DC regulator is enabled. |
| Integer sensor conversion | BLE readings use integer arithmetic in hundredths of a degree and percent humidity, avoiding floating-point conversion work. |
| Compact BLE traffic | A reading is 20 bytes and its acknowledgment is 8 bytes. Both fit the default ATT MTU of 23, avoiding an MTU exchange; the reading also avoids the boundary that can trigger an extra GATT read. |
| Small, fixed resources | A peripheral-only controller, one connection, small packet buffers, and a 64-reading RAM queue keep resource use bounded without writing samples to device flash. |
| Bounded delivery and backoff | Attempts stop at their deadlines. Attempts without an acknowledgment back off to every 2, 4, then 8 sampling cycles; sampling continues. A successful acknowledgment resets backoff. |
| Reuse healthy connections | Persistent mode sends one notification at a time and waits for its acknowledgment. It avoids per-reading discovery, characteristic reads, empty-queue polling, and application keepalives. |
| Fewer idle connection events | Persistent mode requests parameters that allow the board to skip idle BLE events. The Mac chooses the actual parameters. |

The receiver also remembers discovered boards during its process lifetime and queues reconnection while a disconnected board sleeps. This avoids waiting for another discovery callback. It uses recent advertisements for signal strength instead of requesting an extra radio transaction; `unknown` RSSI is normal when no fresh advertisement is available.

## How delivery works

Each sample carries a boot ID generated once per boot and an incrementing sequence number. Delivery follows this order:

1. The board queues the reading, or a sensor error, in RAM.
2. The Mac receives it and commits it to SQLite.
3. The Mac sends an acknowledgment (ACK) containing the exact boot ID and sequence.
4. The board removes that reading from its queue.

Lost ACKs cause retries. The receiver deduplicates previously saved readings, including after restarting with the same database, and acknowledges them again. The SQLite journal is authoritative: a crash after commit can leave a saved reading that never reached the console.

**Delivery limits:**

- The board holds **64 readings in RAM**. Pending readings are lost on reset or power loss. Overflow drops the oldest reading; during a persistent connection, it preserves the in-flight head and drops the next-oldest instead.
- At a ten-second persistent sampling period, the queue holds about **640 seconds of history**. An unlimited outage cannot be recovered. At maximum backoff, a returning receiver may wait roughly eight sampling cycles for another attempt.
- Record `time` is the **Mac's receipt time**, not the original sample time. Sequence gaps indicate missing sample attempts, not necessarily radio loss.
- BLE is **unencrypted and unpaired**, with unauthenticated ACKs. Use it in a trusted local environment and run one receiver per board.
- Firmware and receiver must use the current **AHT2** protocol together; the old AHT1 broadcast protocol is incompatible.

## Settings and useful commands

Settings are compiled into the firmware. **Changing them requires reflashing.**

| Option | Default | Range | Meaning |
| --- | --- | --- | --- |
| `--sleep-seconds` | 10 s | 1–604800 | Sleep after a default-mode cycle; start-to-start sampling period in persistent mode |
| `--delivery-window-ms` | 1500 ms | 100–10000 | Entire default-mode delivery attempt; advertising and connection establishment only in persistent mode |
| `--ack-timeout-ms` | 5000 ms | 1000–60000 | Persistent-mode deadline for progress while readings are pending |

The persistent ACK deadline includes initial service discovery and subscription when data is waiting. Only a valid ACK for the offered head advances it; an empty queue has no ACK deadline. Expiry allows up to 100 ms for disconnect before disabling the controller. In default mode, teardown stays inside the original delivery window. These are software budgets, not measured worst-case hardware timings.

```sh
# Give initial discovery more time.
./flash.sh --delivery-window-ms 3000
./flash.sh --persistent --delivery-window-ms 3000 --ack-timeout-ms 8000

# Flash without starting the receiver.
./flash.sh --no-monitor

# Compile and inspect without a board.
./flash.sh --build-only
./flash.sh --persistent --build-only

# Run the receiver independently.
.venv/bin/python tools/ble_receive.py --json
.venv/bin/python tools/ble_receive.py --count 3 --timeout 120
.venv/bin/python tools/ble_receive.py --device YOUR-MAC-DEVICE-UUID
.venv/bin/python tools/ble_receive.py --database ~/aht10-readings.sqlite3

# Inspect saved readings.
sqlite3 artifacts/readings.sqlite3 'SELECT record FROM readings ORDER BY rowid;'
```

`--count` counts newly saved readings. macOS identifies boards with a device UUID rather than a radio MAC address. Keep the database and its WAL file together, and do not delete them during collection. The receiver uses SQLite WAL, FULL synchronization, and macOS fullfsync.

Use `--volume PATH` to select among attached bootloader volumes, or `BLE_PYTHON` to select another receiver Python environment. Direct builds accept `SLEEP_SECONDS=300 DELIVERY_WINDOW_MS=1500 cargo build --locked --release`; add `--features ble-persistent` and optionally `ACK_TIMEOUT_MS=5000` for persistent mode. Offline builds use the existing `memory.x` and do not verify another board's layout.

## Troubleshooting and power checks

| Symptom | What to check |
| --- | --- |
| No readings | Allow terminal Bluetooth access, keep the Mac awake, and run the receiver with `--verbose`. Discovery may need a longer delivery window; persistent discovery may also need a longer ACK timeout. |
| No USB serial port | Expected for BLE firmware. Double-press RESET to flash again, or use USB diagnostics below. |
| Long delay after an outage | Retry backoff can reach eight sampling cycles. Pending readings drain once reception resumes. |
| Persistent mode reconnects unexpectedly | `--verbose` shows connection numbers and session ages. A retained connection keeps its number while age increases. Age is receiver session time, not CPU awake time. |
| Persistent firmware detected as read/ACK mode | Restart the receiver and check for a stale OS GATT service cache. |
| Flash helper exits with code 75 | USB disconnected near the end of the UF2 copy; the result is unverified. Check readings from the intended board before retrying. A completed copy alone does not prove operation. |
| Sensor errors | Check 3.3 V power, D4/D5 wiring, and pull-ups. Errors are reported explicitly instead of reusing stale measurements. |

For current measurements, disconnect USB/debug hardware and leave receiver `--verbose` off: it adds diagnostic reads. Compare both modes at the same actual sampling cadence, including normal operation, receiver outages, and stalled ACKs. Sensor breakouts, pull-ups, and board regulators also affect consumption.

## USB diagnostics

```sh
./flash.sh --usb-serial
```

This separate build samples once per second and prints over USB. It cannot be combined with `--persistent` and is intended for diagnostics, not BLE power measurements. See the [USB and bootloader reference](docs/usb-serial.md) for setup and historical USB hardware results.

## Developer reference

<details>
<summary>Timing and connection details</summary>

- BLE sampling has a 600 ms deadline, followed by bounded bus recovery on error. Normal conversion waits 80 ms; the one-time sensor power-up wait is 40 ms.
- Persistent sampling continues during delivery and outages, without bursts of catch-up samples after delays. Reconnection waits for future sample completions, so samples produced during a connection cannot cause immediate retry bursts.
- After five seconds connected, persistent firmware makes one L2CAP request for a 30–45 ms interval, peripheral latency 19, and a six-second supervision timeout. This permits skipping up to 19 idle events if accepted; it does not guarantee they will be skipped. The receiver's `--verbose` output reports actual parameters.
- Keep TrouBLE's `connection-params-update` feature disabled with the pinned peripheral-only SDC: the L2CAP path avoids its unavailable HCI `LeConnUpdate` implementation.
- Embassy uses RTC1 and the 32.768 kHz crystal. MPSL owns radio scheduling, RTC0, TIMER0, and its clock requests. System ON retains the queue and supports RTC wakeups; System OFF cannot wake from the internal RTC.
- Firmware and receiver enforce separate timeouts. Receiver connection attempts wait up to 60 seconds, plus five seconds for service discovery/timeout cleanup. Persistent sessions have no overall lifetime cap, but notification and ACK waits are bounded. Ctrl-C, `--count`, and `--timeout` cancel pending connections; unconfirmed teardown stops the receiver rather than allowing overlapping attempts.

Implementation: [BLE setup and sampling](src/ble_mode.rs), [disconnected delivery](src/ble_link.rs), [persistent delivery](src/ble_persistent.rs), [queue and retry policy](src/delivery.rs), and [I²C lifetime and recovery](src/bus.rs).

</details>

<details>
<summary>GATT and wire format</summary>

Service UUID: `a8170001-9c67-4f88-9b44-7d52e2b16a10`. Advertising includes the service UUID without a local name or manufacturer payload. Each characteristic below uses the same UUID suffix.

| UUID prefix | Purpose |
| --- | --- |
| `a8170002` | Read oldest pending 20-byte sample; all zeros means empty |
| `a8170003` | Write with response: 8-byte little-endian boot ID + sequence ACK |
| `a8170004` | Persistent only: notify the same sample frame |
| `a8170005` | Persistent only: read actual link parameters as little-endian `<HHH>`: interval in 1.25 ms units, peripheral latency, timeout in 10 ms units |

The sample format is Python `struct.Struct("<2sBBIIhHI")`, with little-endian fields:

| Field | Bytes | Meaning |
| --- | --- | --- |
| Magic | 2 | ASCII `AH` |
| Version | 1 | `2` |
| Status | 1 | 0 valid, 1 I²C, 2 busy, 3 uncalibrated, 4 sample deadline |
| Boot ID | 4 | Hardware random value generated each boot |
| Sequence | 4 | Incremented each sample attempt; wraps at 2³² |
| Temperature | 2 | Signed hundredths of °C |
| Humidity | 2 | Unsigned hundredths of % RH |
| Sleep seconds | 4 | Delay after the cycle; sampling period in persistent mode |

Error frames zero measurements; the receiver saves null values and a named error. ACKs must match the current head after it has been read or notified in that connection. The receiver rejects replays with the same identity but different bytes. See [packet encoding](src/packet.rs) and the [receiver](tools/ble_receive.py).

</details>

<details>
<summary>Build and test commands</summary>

```sh
./flash.sh --build-only
./flash.sh --persistent --build-only
./flash.sh --usb-serial --build-only
python3 -m unittest discover -s tests -v
rustc --edition=2024 --test tests/protocol.rs -o target/protocol-tests
./target/protocol-tests
rustc --edition=2024 --test src/reading.rs -o target/reading-tests
./target/reading-tests
```

Each build helper runs formatting, Cargo checks, a release build, and ELF layout inspection. Host tests cover encoding, conversion, ACK validation, overflow, backoff, durable receipt, replay handling, receiver cancellation, and persistent delivery regressions.

**BLE timing, current consumption, controller restart, and Mac interoperability need physical testing.** Verify receipt and database contents, outage/backlog recovery, lost and invalid ACKs, sensor failure, and overflow. For persistent mode, also verify multiple readings on one connection, sampling periods above 12 seconds, and watchdog expiry when a subscribed receiver stops acknowledging.

</details>
