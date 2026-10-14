#!/bin/sh
# Compile and inspect without touching hardware. Optional arg: UF2 volume.
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -gt 1 ]; then
    echo "Usage: sh tools/build.sh [bootloader-volume]" >&2
    exit 2
fi
if [ "$#" -eq 1 ]; then
    python3 tools/firmware.py configure --volume "$1"
fi
cargo fmt --check
cargo check --locked --target thumbv7em-none-eabihf
cargo build --locked --release --target thumbv7em-none-eabihf
python3 tools/firmware.py inspect
if [ "$#" -eq 1 ]; then
    python3 tools/firmware.py uf2 --volume "$1"
else
    echo "Compile-only build. No UF2 generated: installed board layout has not been read."
fi
