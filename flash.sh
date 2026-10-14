#!/bin/sh
# macOS: verify layout, compile, flash through UF2, and receive BLE (or USB).
set -eu
cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
Usage: ./flash.sh [--no-monitor | --build-only] [--sleep-seconds N] [--delivery-window-ms N]
                  [--persistent] [--ack-timeout-ms N] [--usb-serial] [--volume PATH] [--port PATH]

Connect the XIAO over USB-C. Double-press RESET when prompted.
The script finds the bootloader, builds, flashes, then saves and acknowledges BLE readings.

  --sleep-seconds N  Sleep between attempts, or persistent sampling period (1..604800; default 10).
  --delivery-window-ms N  BLE deadline; persistent: advertising only (100..10000 ms; default 1500).
  --persistent   Keep BLE connected; notify readings and sleep between events.
  --ack-timeout-ms N  Persistent pending-data deadline (1000..60000 ms; default 5000).
  --usb-serial   Build the original USB diagnostic application instead of BLE.
  --no-monitor   Build and flash without starting a receiver/terminal.
  --build-only   Compile and inspect using memory.x; no board needed or written.
  --volume PATH  Select a bootloader volume when multiple boards are attached.
  --port PATH    Select an application serial port when needed.
  -h, --help     Show this help.

Requires Rust/rustup and Python 3. BLE monitoring uses .venv/bin/python + Bleak.
USB monitoring uses picocom or screen. See README for receiver setup.
EOF
}

volume=''
port=''
monitor=1
build_only=0
usb_serial=0
persistent=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-monitor) monitor=0; shift ;;
        --build-only) build_only=1; monitor=0; shift ;;
        --usb-serial) usb_serial=1; shift ;;
        --persistent) persistent=1; shift ;;
        --sleep-seconds|--delivery-window-ms|--ack-timeout-ms)
            if [ "$#" -lt 2 ]; then echo "ERROR: $1 needs an integer." >&2; exit 2; fi
            case "$2" in ''|*[!0-9]*) echo "ERROR: $1 needs a positive integer." >&2; exit 2 ;; esac
            case "$1" in
                --sleep-seconds) export SLEEP_SECONDS="$2" ;;
                --delivery-window-ms) export DELIVERY_WINDOW_MS="$2" ;;
                --ack-timeout-ms) export ACK_TIMEOUT_MS="$2" ;;
            esac
            shift 2 ;;
        --volume|--port)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo "ERROR: $1 needs a path." >&2
                exit 2
            fi
            case "$1" in
                --volume) volume=$2 ;;
                --port) port=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$usb_serial" -eq 1 ] && [ "$persistent" -eq 1 ]; then
    echo "ERROR: --persistent and --usb-serial are separate firmware modes." >&2
    exit 2
fi

build() {
    cargo fmt --check
    if [ "$usb_serial" -eq 1 ]; then
        cargo check --locked --target thumbv7em-none-eabihf --no-default-features --features usb-serial
        cargo build --locked --release --target thumbv7em-none-eabihf --no-default-features --features usb-serial
    elif [ "$persistent" -eq 1 ]; then
        cargo check --locked --target thumbv7em-none-eabihf --features ble-persistent
        cargo build --locked --release --target thumbv7em-none-eabihf --features ble-persistent
    else
        cargo check --locked --target thumbv7em-none-eabihf
        cargo build --locked --release --target thumbv7em-none-eabihf
    fi
}

if [ "$build_only" -eq 1 ]; then
    build
    python3 tools/firmware.py inspect
    exit 0
fi

if [ "$(uname -s)" != Darwin ]; then
    echo "ERROR: This script is for macOS." >&2
    exit 1
fi
for dependency in python3 cargo; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        echo "ERROR: Install $dependency first; see README.md, Install tools on macOS." >&2
        exit 1
    fi
done
terminal=''
ble_python=${BLE_PYTHON:-.venv/bin/python}
if [ "$monitor" -eq 1 ] && [ "$usb_serial" -eq 0 ]; then
    if ! "$ble_python" -c 'import bleak' >/dev/null 2>&1; then
        echo "ERROR: Set up .venv with tools/requirements.txt, or use --no-monitor. See README." >&2
        exit 1
    fi
fi
if [ "$monitor" -eq 1 ] && [ "$usb_serial" -eq 1 ]; then
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        echo "ERROR: Run in an interactive Terminal, or use --no-monitor." >&2
        exit 1
    fi
    if command -v picocom >/dev/null 2>&1; then
        terminal=picocom
    elif command -v screen >/dev/null 2>&1; then
        terminal=screen
    else
        echo "ERROR: Install picocom (brew install picocom), or use --no-monitor." >&2
        exit 1
    fi
fi

echo "Connect the XIAO. If its UF2 drive isn't mounted, double-press RESET now."
echo "Waiting up to 120 seconds for the bootloader (Ctrl-C cancels)..."
if [ -n "$volume" ]; then
    volume=$(python3 tools/firmware.py wait-volume --volume "$volume")
else
    volume=$(python3 tools/firmware.py wait-volume)
fi
echo "Bootloader: $volume"

# The target is now in bootloader mode. Record other running applications so
# their already-present ports cannot be mistaken for this board after reset.
previous_ports=''
if [ "$usb_serial" -eq 1 ]; then previous_ports=$(python3 tools/firmware.py ports); fi

echo "Verifying board layout and building firmware..."
python3 tools/firmware.py configure --volume "$volume"
build
echo "Flashing the application..."
copy_status=0
python3 tools/firmware.py flash --volume "$volume" || copy_status=$?
case "$copy_status" in
    0) ;;
    75)
        if [ "$usb_serial" -eq 0 ]; then
            echo "BLE image copy was interrupted near completion; flash outcome is unverified." >&2
            echo "Run .venv/bin/python tools/ble_receive.py and check readings from the intended board." >&2
            exit 75
        fi
        echo "USB disconnected near the end of the copy. Checking for application restart..." ;;
    *) exit "$copy_status" ;;
esac

if [ "$usb_serial" -eq 0 ]; then
    echo "UF2 copy completed. BLE firmware has no application USB port. Hardware operation is not yet verified."
    if [ "$monitor" -eq 0 ]; then exit 0; fi
    exec "$ble_python" tools/ble_receive.py
fi

echo "Waiting up to 60 seconds for XIAO AHT10 Serial..."
port=$(python3 tools/firmware.py wait-port --port "$port" --exclude "$previous_ports" --volume "$volume")
if [ "$copy_status" -eq 75 ]; then
    echo "Application is running at $port. Copy completion was interrupted; exact image contents have not been read back."
fi

if [ "$monitor" -eq 0 ]; then
    echo "Done: application USB detected at $port."
    exit 0
fi

echo "Opening $port at 115200. Close any other terminal using this port."
if [ "$terminal" = picocom ]; then
    echo "Exit with Ctrl-A, then Ctrl-X."
    exec picocom --baud 115200 --flow n "$port"
else
    echo 'Exit with Ctrl-A, then \, then confirm.'
    exec screen "$port" 115200
fi
