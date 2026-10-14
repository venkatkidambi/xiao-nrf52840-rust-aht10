#!/bin/sh
# macOS: verify layout, compile, flash through UF2, and monitor native USB CDC.
set -eu
cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
Usage: ./flash.sh [--no-monitor | --build-only] [--volume PATH] [--port PATH]

Connect the XIAO over USB-C. Double-press RESET when prompted.
The script finds the bootloader, builds, flashes, and opens serial output.

  --no-monitor   Build and flash without opening a serial terminal.
  --build-only   Compile and inspect using memory.x; no board needed or written.
  --volume PATH  Select a bootloader volume when multiple boards are attached.
  --port PATH    Select an application serial port when needed.
  -h, --help     Show this help.

Requires Rust/rustup and Python 3. Serial monitoring uses picocom or screen.
EOF
}

volume=''
port=''
monitor=1
build_only=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-monitor) monitor=0; shift ;;
        --build-only) build_only=1; monitor=0; shift ;;
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

build() {
    cargo fmt --check
    cargo check --locked --target thumbv7em-none-eabihf
    cargo build --locked --release --target thumbv7em-none-eabihf
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
if [ "$monitor" -eq 1 ]; then
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
previous_ports=$(python3 tools/firmware.py ports)

echo "Verifying board layout and building firmware..."
python3 tools/firmware.py configure --volume "$volume"
build
echo "Flashing the application..."
copy_status=0
python3 tools/firmware.py flash --volume "$volume" || copy_status=$?
case "$copy_status" in
    0) ;;
    75) echo "USB disconnected near the end of the copy. Checking for application restart..." ;;
    *) exit "$copy_status" ;;
esac

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
