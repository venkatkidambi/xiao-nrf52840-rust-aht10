# XIAO nRF52840 + AHT10: low-power acknowledged BLE

Two bare-metal BLE modes are available: **disconnected** (default) and **persistent** (`--persistent`). Both use `no_std` Rust, statically allocated Embassy futures, hardware interrupts and RTC timers, with no OS, RTOS, allocator, or busy-waiting between readings. Nordic SDC/MPSL schedules the radio; the bootloader-installed S140 stays disabled.

The default firmware measures temperature/humidity, queues the reading, gives the Mac a **1.5-second maximum BLE delivery window**, and sleeps for **10 seconds**. It finishes early when the queue is acknowledged. The Mac commits each reading to SQLite **before** acknowledging its exact boot ID and sequence. Lost acknowledgments are retried and deduplicated, including after a receiver restart.

Persistent mode keeps a healthy connection open, samples on an independent schedule, and sends notifications only when readings are available. It avoids per-reading advertising, reconnection, service discovery, and GATT reads while retaining the same durable application ACK. See [persistent mode](#persistent-mode) for tuning and limits.

Delivery has explicit limits: the board retains **64 readings in RAM**, drops the oldest on overflow (persistent connections retain the in-flight head and drop the next-oldest), and loses pending readings on reset/power loss. It cannot guarantee every sample through an unlimited outage. This is an unencrypted, unpaired protocol for a trusted local environment; acknowledgments are not authenticated. Run one receiver per board. Both firmware and receiver must be updated together; the old `AHT1` broadcast protocol is replaced by `AHT2` GATT delivery.

## Build, flash, receive

The project pins Rust 1.95.0 and the ARM target in `rust-toolchain.toml`. Install Rust/rustup and Python 3.10+; see the [toolchain and bootloader guide](docs/usb-serial.md). BLE builds also require Git and libclang for the controller's generated bindings; on macOS, install the Xcode Command Line Tools (`xcode-select --install`). If bindgen cannot locate libclang, set `LIBCLANG_PATH` to your installed LLVM library directory. Compatible TrouBLE and Nordic controller revisions are pinned in Cargo.toml/Cargo.lock.

On macOS 12.3 or newer, install the receiver once:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
```

Connect USB-C, then run:

```sh
./flash.sh --sleep-seconds 10
```

Double-press RESET when prompted. The helper verifies the connected bootloader's flash layout, checks and builds firmware, copies UF2, and starts the receiver. Allow Bluetooth access for your terminal. Keep the Mac awake and the receiver running. BLE operation does not create an application USB port; double-press RESET to return to the bootloader for later flashing.

```sh
# Retain the BLE connection and sample every 10 seconds.
./flash.sh --persistent --sleep-seconds 10

# Compile/inspect persistent mode without flashing.
./flash.sh --persistent --build-only

# Longer sleep reduces sensing and delivery frequency.
./flash.sh --sleep-seconds 300

# Shorten the maximum window after measuring reliable Mac discovery timing.
./flash.sh --sleep-seconds 10 --delivery-window-ms 500

# Allow more time for initial CoreBluetooth service discovery if needed.
./flash.sh --delivery-window-ms 3000

# Flash without monitoring, or compile/inspect without a board.
./flash.sh --no-monitor
./flash.sh --build-only

# Receive independently; --count counts newly saved readings.
.venv/bin/python tools/ble_receive.py --json
.venv/bin/python tools/ble_receive.py --count 3 --timeout 120
.venv/bin/python tools/ble_receive.py --device YOUR-MAC-DEVICE-UUID
.venv/bin/python tools/ble_receive.py --database ~/aht10-readings.sqlite3
```

`--sleep-seconds` accepts 1..604800; `--delivery-window-ms` accepts 100..10000; persistent `--ack-timeout-ms` accepts 1000..60000. Defaults are 10, 1500, and 5000 respectively. Settings are compiled into the image; changing them requires reflashing. Direct builds accept `SLEEP_SECONDS=300 DELIVERY_WINDOW_MS=1500 cargo build --locked --release`. Add `--features ble-persistent` for persistent mode and optionally set `ACK_TIMEOUT_MS=5000`. `--persistent` and `--usb-serial` are mutually exclusive. `--adv-events` is removed. In disconnected mode the delivery deadline controls advertising and connection time together; in persistent mode it bounds advertising/connection establishment only. Short windows may repeatedly expire before macOS finishes discovery; the minimum accepted value is not a reliability recommendation.

The receiver's default journal is `artifacts/readings.sqlite3`, using SQLite WAL, FULL synchronization, and macOS fullfsync. Keep the database and its WAL together; do not delete them while collecting data. Console output is secondary: a process crash after commit can leave a saved row that was never printed. Duplicate rows are acknowledged again without reprinting. `record` contains the JSON record and `payload` the original frame:

```sh
sqlite3 artifacts/readings.sqlite3 'SELECT record FROM readings ORDER BY rowid;'
```

The receiver discovers the persistent notification characteristic automatically; no receiver mode flag is needed. After the first discovery, the receiver remembers the board's `BLEDevice` object for the lifetime of the process. After a saved reading and disconnect, it immediately queues the next connection while the board sleeps; another advertisement callback is not required. Restarting the receiver performs initial discovery again. The service-filtered scanner remains available to discover other boards and refresh remembered objects, with one connection worker per board.

Each Mac-side connection attempt waits up to 60 seconds for the board, with another 5 seconds bounding service discovery/timeout cleanup. Disconnected-mode sessions are capped at 12 seconds after connection, and disconnect cleanup at 3 seconds. Persistent sessions have no lifetime cap; notification subscription and each ACK are bounded to 12 seconds. The first notification has a 12-second wait; subsequent notifications have a wait of the reported sampling period plus 12 seconds. Disconnect callbacks wake idle waits immediately. Failed or empty sessions retry after 0.25, 0.5, 1, 2, then at most 4 seconds; a newly saved reading resets this delay and queues the next connection immediately. These are receiver limits; the firmware independently enforces its mode's deadlines. `--count`, `--timeout`, and Ctrl-C cancel pending requests, including the native CoreBluetooth request while a board is asleep. If teardown cannot be confirmed, the receiver stops rather than starting an overlapping connection.

RSSI is taken from advertisements received within the last two seconds. A queued connection can succeed without a fresh advertisement callback; in that case RSSI is `unknown` in text and `null` in JSON/SQLite. Persistent reception reevaluates advertisement age for every reading, so RSSI normally becomes unknown after two seconds. The receiver avoids an extra radio transaction just to refresh signal strength. Remembered-device reconnect removes the wait for an application-level discovery callback; macOS connection scheduling and GATT discovery still affect latency. Measure before/after receipt intervals with the same board settings; no specific speedup is guaranteed.

Record `time` is the Mac's receipt time, not the original sample time; queued readings can arrive later. A new or empty journal cannot reconstruct receipts saved in a different journal. Use `--verbose` to diagnose failed connections or sessions that hit the board's deadline. macOS exposes a device UUID rather than a radio MAC address; see [Bleak's macOS backend](https://bleak.readthedocs.io/en/latest/backends/macos.html).

The helper supports `--volume PATH` for multiple attached boards and `BLE_PYTHON` for another Python environment. `--port` applies only to USB diagnostics. A completed UF2 copy is not proof of firmware operation. A final-block/flush disconnect exits **75**, reporting an unverified outcome; check the intended board before retrying. Offline builds use the existing `memory.x` and do not establish compatibility with another board.

## Persistent mode

To verify a retained connection, restart the receiver with `--verbose`. It logs the detected receiver mode, a connection number, and the session age for each new receipt. Successive readings on one connection keep the same number with increasing age; an increment means a new connection. Session-end logs include ordinary disconnects and an empty read queue, not just exceptions. Age starts after connection/service discovery completes and measures the receiver session, **not MCU uptime or CPU/radio awake time**. RSSI by itself cannot establish whether the connection was retained. If persistent firmware is detected as `read/ACK (notification characteristic absent)`, check that this receiver was restarted and investigate the discovered GATT table/cache before changing power settings.

```sh
./flash.sh --persistent --sleep-seconds 10
# An initial connection needing more discovery/receipt time:
./flash.sh --persistent --delivery-window-ms 3000 --ack-timeout-ms 8000
# Actual negotiated connection parameters on stderr (adds a diagnostic read per sample):
.venv/bin/python tools/ble_receive.py --verbose
```

| Behavior | Disconnected (default) | Persistent |
| --- | --- | --- |
| Sampling | Sleep after sensing and delivery | Start-to-start period, independent of delivery |
| Healthy connection | Disconnect after queue drains | Retain connection while idle |
| Delivery | Read head, commit, ACK | Notify head, commit, ACK |
| `--delivery-window-ms` | Entire advertising + connected attempt | Advertising + connection establishment |
| `--ack-timeout-ms` | Not used | Maximum time without head ACK while data is pending |
| Idle BLE activity | Controller disabled | Scheduled connection events; sleep between events |
| Unavailable receiver | Bounded attempts, 2/4/8-cycle backoff | Same backoff; sampling continues independently |

Persistent sampling starts after the one-time 40 ms sensor power-up wait. A 10-second setting targets ten seconds between conversion starts, including during backlog delivery or receiver outages. Conversion and recovery remain bounded as below; delayed scheduling does not generate a series of catch-up samples. The AHT10 stays powered in standby and TWIM is disabled between transfers, including during conversion waits. The CPU uses System ON sleep whenever the asynchronous work has nothing to do; a retained connection does not keep the radio continuously active.

A subscription immediately delivers the oldest pending 20-byte frame. There is one outstanding notification, followed by an 8-byte application ACK written with response after SQLite commit. The next head is sent immediately after that ACK; no characteristic read, empty-queue polling, keepalive application message, MTU exchange, or rediscovery is needed. Both frames fit the default ATT MTU of 23. The notification itself is not proof of durable receipt. The receiver uses a single bounded notification queue and serializes commit/ACK, deduplicating across reconnects. It prints new persistent receipts after ACK submission without waiting for a disconnect or accumulating session-long output buffers.

After five seconds connected, firmware makes one **L2CAP connection parameter update request**: 30–45 ms interval, peripheral latency 19, and 6-second supervision timeout. This permits skipping up to 19 idle events (up to 900 ms between attended events at 45 ms), while pending peripheral data can be sent sooner. It balances short active exchanges and idle wakeups; latency is not a guarantee that every idle event will be skipped. The request follows the conservative limits in [Apple QA1931](https://developer.apple.com/library/archive/qa/qa1931/index.html). The central chooses the actual parameters and may reject the request. `--verbose` reads the actual interval, latency and timeout after each receipt, logging changes; leave it off for power profiling. A larger peripheral latency also delays central-to-peripheral ACKs. See [Nordic's connection power guidance](https://academy.nordicsemi.com/courses/designing-low-power-bluetooth-le-products/lessons/lesson-4-bluetooth-le-power-optimization/topic/bluetooth-le-connection-parameters-and-power-consumption/).

The pinned peripheral-only SDC library lacks the HCI `LeConnUpdate` implementation. Keep TrouBLE's `connection-params-update` feature disabled: its peripheral API then sends the L2CAP request instead, preserving the smaller peripheral-only controller. The normal default build remains disconnected; `ble-persistent` compiles in the persistent implementation.

The independent **pending-data watchdog** starts at connection acceptance with an existing backlog, or when a new sample becomes pending on an idle connection. Its default is 5 seconds. Only a valid ACK for an offered current head advances the deadline. Reads, subscriptions, invalid ACKs, queue overflow, or additional sensor readings cannot extend it. An empty queue has no ACK deadline, even with a sampling period of several minutes. The watchdog covers stalled notification/ATT transmission as well as a silent peer. Expiry requests disconnect, allows up to 100 ms for teardown, and drops/disables SDC; a blocked GATT operation cannot prevent this watchdog from running. Initial service discovery and subscription must finish within this pending-data budget, so use a larger `--ack-timeout-ms` if the Mac needs it.

After any ended attempt, firmware waits for future sample completions before another attempt. Failed attempts back off over 2, 4, then 8 future sample completions; an attempt with an ACK resets backoff. Signals from samples produced during an active attempt are coalesced, and do not trigger immediate retry bursts afterward. The RAM outbox remains capped at 64 readings. While connected, overflow retains the head the Mac may be committing and drops the next-oldest pending reading, using one 20-byte copy. This prevents a simultaneous sensor conversion from invalidating an in-flight ACK. While disconnected, overflow drops the oldest as before. Boot/sequence identities, reset losses, and commit-before-ACK rules are unchanged. With 10-second persistent sampling, the outbox holds about 640 seconds of history. Receiver sleep/disconnect causes reconnect attempts; there is no claim of continuous reception while the Mac sleeps.

This mode is tuned to reduce setup traffic, radio work per reading, and idle CPU work. It is a candidate for frequent readings, especially with a nearby awake Mac. It does not establish maximum battery life or a specific throughput: measure energy per sample and long-run average current against disconnected mode at the same **actual sampling cadence**, with USB/debug disconnected. Use the normal receiver without `--verbose`, and include initial connection, a steady session, receiver outages, and stalled ACKs. For sparse readings, disconnected operation may consume less energy.

## Disconnected delivery and energy budget

Each cycle samples once and enqueues the result, including sensor errors. A delivery attempt advertises at a 20 ms interval plus the controller's BLE advertising delay, accepts at most one connection, and serves the oldest pending reading first. The Mac reads, commits, and writes the 8-byte acknowledgment. Only an exact acknowledgment of the head reading removes it. More queued readings can drain through the same connection, without extending the original deadline.

After the last acknowledgment, firmware requests disconnect and allows at most 100 ms for graceful termination **within** the remaining delivery window. At completion or deadline, it drops the controller. The pinned controller's destructor calls Nordic's synchronous `sdc_disable`, stopping BLE procedures even if asynchronous cancellation/disconnect cannot complete. Discovery, partial delivery, invalid ACKs, and a silent connected Mac cannot restart the timer. See the [controller implementation](https://github.com/alexmoon/nrf-sdc/blob/a22ca716c807496f98ae8d95cba5622b9fdd8a00/nrf-sdc/src/sdc.rs) and [Nordic API contract](https://github.com/alexmoon/nrf-sdc/blob/a22ca716c807496f98ae8d95cba5622b9fdd8a00/nrf-mpsl-sys/third_party/nordic/nrfxlib/softdevice_controller/include/sdc.h).

Attempts with no acknowledgment back off to every **2, 4, then 8 sampling cycles**, capped at 8. Sampling continues during skipped radio cycles. Any acknowledged reading restores an attempt every cycle. This bounds energy during an outage while preserving recent readings without device flash writes. With the default sleep, a returning Mac may wait roughly 80 seconds for the next attempt; 64 slots retain roughly eleven minutes of samples. Longer sleeps proportionally increase both times. Overflow appears as sequence gaps when there is an earlier receipt in the same boot.

The device uses a peripheral-only controller, one connection, small packet buffers, and no application notifications, subscriptions, pairing, or MTU exchange. A 20-byte reading stays below the 22-byte ATT read limit at the default MTU of 23, avoiding the boundary that can trigger an extra read, and the ACK fits one write request. The Mac passes the already discovered device to Bleak and filters discovery to the service UUID. The controller handles link-layer retransmissions within the application deadline. Initial service discovery and Mac scheduling still determine actual successful-session latency; **1.5 seconds is a cap, not a target**.

Sleep starts **after** the delivery attempt (or a skipped attempt), so sampling is not exactly start-to-start periodic. A normal sensor conversion takes 80 ms. Sensor sampling is capped at 600 ms, with bounded bus recovery afterward. The default scheduled sensor-plus-delivery waits are therefore at most about **2.1 seconds**, plus initialization, recovery, and controller setup/teardown CPU time. These are software budgets, not measured worst-case hardware timings or fault-proof real-time guarantees.

## Wiring and sleep

| AHT10 | XIAO nRF52840 |
| --- | --- |
| VCC | 3V3 |
| GND | GND |
| SDA | D4 / P0.04 |
| SCL | D5 / P0.05 |

The sensor stays powered and returns to standby. Use 3.3 V external I²C pull-ups, normally fitted to AHT10 breakouts. Internal pull-ups are enabled only during transfers. The existing 100 kHz bus rate is preserved for wiring compatibility. Disconnect power when changing wiring.

The nRF52840's System OFF cannot wake on an internal RTC, so this uses low-power **System ON with RAM retained**, Embassy's sleeping executor, and RTC1 with the 32.768 kHz crystal. MPSL owns radio scheduling, RTC0, TIMER0, and clock requests; firmware does not force-stop its clocks. In disconnected mode and persistent reconnect backoff, controller disable means there are no BLE connections or advertisements during sleep. A healthy persistent connection instead wakes for scheduled connection events. RTC/MPSL bookkeeping can briefly wake the CPU. USB and TWIM are inactive. See [Nordic's power specification](https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/power.html).

Sensor conversion uses integer arithmetic, avoiding floating-point work and the [FPU sleep erratum](https://docs.nordicsemi.com/r/bundle/errata_nrf52840_rev3/page/err/nrf52840/rev3/latest/anomaly_840_87.html). Calibration is retained during normal operation. The driver sleeps for the [specified 80 ms conversion wait](https://arduino.ua/files/AOSONG_AHT10_EN.PDF) and reads status plus measurement together. Errors are queued as status codes, never stale measurements.

Board setup disables RGB LEDs and the battery divider, cuts unused Sense IMU/microphone power, sends `B9` deep-power-down to the SPI NOR when fitted, and enables DC/DC. Pins follow [Seeed's board definition](https://github.com/Seeed-Studio/Adafruit_nRF52_Arduino/blob/master/variants/Seeed_XIAO_nRF52840/variant.cpp). Charging-current selection remains unchanged. Measure battery current with USB/debug hardware disconnected; regulators, sensor boards, LEDs, and pull-ups contribute to consumption. Software cannot establish actual sleep current or battery lifetime.

## GATT protocol

Service UUID: `a8170001-9c67-4f88-9b44-7d52e2b16a10`. Advertising contains flags and this complete UUID, without a local name or manufacturer payload. The factory-derived random static address remains stable across boots. The implementation uses [TrouBLE](https://github.com/embassy-rs/trouble/tree/fecc67076fc1eda955ac12cbb5473803576dda5e) and Nordic's application-linked SoftDevice Controller/MPSL; the bootloader-installed S140 remains untouched and disabled.

| Characteristic | UUID prefix (same remaining UUID) | Operation |
| --- | --- | --- |
| Oldest pending sample | `a8170002` | Read 20 bytes; all zeros means empty |
| Acknowledgment | `a8170003` | Write with response: little-endian boot ID + sequence, 8 bytes |
| Pending notification (persistent only) | `a8170004` | Notify the same 20-byte frame; subscribe once per connection |
| Actual link parameters (persistent only) | `a8170005` | Read 6 bytes: little-endian `<HHH>` interval in 1.25 ms units, peripheral latency, supervision timeout in 10 ms units |

The sample is Python `struct.Struct("<2sBBIIhHI")`, with little-endian fields:

| Field | Bytes | Meaning |
| --- | --- | --- |
| Magic | 2 | ASCII `AH` (service UUID plus version identify AHT2) |
| Version | 1 | `2` |
| Status | 1 | 0 valid, 1 I²C, 2 busy, 3 uncalibrated, 4 sample deadline |
| Boot ID | 4 | Hardware random value generated each boot |
| Sequence | 4 | Incremented each sample attempt; wraps at 2³² |
| Temperature | 2 | Signed hundredths of °C |
| Humidity | 2 | Unsigned hundredths of % RH |
| Sleep seconds | 4 | Delay after the cycle; persistent mode: sampling period |

Error frames zero measurements; the receiver records null values and a named error. An ACK requires a prior sample read or notification in that connection and must match the current head. Partial, stale, or wrong-boot ACKs are rejected. A lost final ATT response may look like a disconnect on the Mac even though the board accepted the ACK; the already committed journal is authoritative. Replays with the same identity and different bytes are rejected by the receiver. Sequence gaps count missing sample attempts, not verified RF losses; first receipts/new boots have no computable gap.

## USB diagnostics and verification

```sh
./flash.sh --usb-serial
./flash.sh --usb-serial --build-only
```

USB diagnostics sample once per second and keep USB active. Direct Cargo commands require `--no-default-features --features usb-serial`; BLE and USB use different interrupt/critical-section implementations. All modes preserve the installed MBR, S140, and UF2 bootloader. Layout is still derived from the attached board's `INFO_UF2.TXT` and `CURRENT.UF2`; no chip erase or guessed flash offset is introduced. The [USB reference](docs/usb-serial.md) retains historical USB hardware results.

```sh
cargo fmt --check
cargo check --locked
./flash.sh --build-only
./flash.sh --persistent --build-only
./flash.sh --usb-serial --build-only
python3 -m unittest discover -s tests -v
rustc --edition=2024 --test tests/protocol.rs -o target/protocol-tests
./target/protocol-tests
rustc --edition=2024 --test src/reading.rs -o target/reading-tests
./target/reading-tests
```

Host tests cover wire bytes, ACK identity validation, overflow, retry pacing, durable commit before ACK, restarts, duplicates, gaps, sequence wrap, storage failure, cancellation, and lost ACKs. Receiver tests also cover reconnecting without new advertisements, refreshing remembered devices, transport-error backoff, native pending-request cancellation, and shutdown during teardown. All three firmware modes (disconnected BLE, persistent BLE, USB) must compile/link and pass ELF layout inspection. Persistent regressions cover subscription races, one-connection idle reception, bounded notification buffering, disconnect/cancellation, missing notifications, stalled ACKs, live output, negotiated-parameter decoding, and a watchdog that only advances on a valid offered-head ACK. **BLE timing, current consumption, controller restart across cycles, and Mac interoperability still require physical testing.**

For hardware acceptance, receive three new readings from the intended board and inspect the database. Stop/sleep the Mac, verify bounded attempts followed by 2/4/8-cycle backoff, then resume reception and verify backlog drainage. Interrupt the Mac after a database commit to check replay deduplication. Exercise a connected peer that never ACKs and one that writes incorrect ACKs; both must hit the original deadline. Disconnect the sensor to verify error frames. Profile battery current across successful, unavailable, and stalled-peer cases before shortening the window. Reset and queue overflow intentionally lose pending data as described above.

For persistent hardware acceptance, run the receiver with `--count 10` and verify a single connection spans multiple sampling periods. Inspect `--verbose` once to confirm the central's accepted parameters after the five-second request, then disable it for current measurement. Check a sampling period above 12 seconds to confirm the idle connection survives. Stop/sleep the Mac and verify continued sampling, controller shutdown between reconnect attempts, backlog drainage, and deduplication on return. With a test client, subscribe but withhold ACKs (and try wrong ACKs/repeated reads); verify disconnect within the configured pending-data deadline plus the 100 ms grace. Repeat after a long idle period to check the first new sample starts the watchdog. Exercise sensor failure while connected, queue overflow during an outage, and `--count`/`--timeout`/Ctrl-C during notification and ACK handling. When switching firmware modes, restart the receiver; if the OS presents a stale GATT service table, refresh the Bluetooth connection/cache before evaluating autodetection.
