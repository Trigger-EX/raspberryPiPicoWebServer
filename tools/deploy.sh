#!/bin/sh
# Compile PicoMail to .mpy (saves RAM on the Pico) and copy it to a Pico W
# connected over USB. Requires: pip install mpremote mpy-cross
#
#   tools/deploy.sh            # code only
#   tools/deploy.sh --config   # also upload src/config.py
#   tools/deploy.sh --certs certs/   # also upload cert.der + key.der
set -e
cd "$(dirname "$0")/.."
BUILD=build/picomail
rm -rf build && mkdir -p "$BUILD"

for f in src/picomail/*.py; do
    mpy-cross -march=armv6m -o "$BUILD/$(basename "$f" .py).mpy" "$f"
done

mpremote mkdir :picomail 2>/dev/null || true
for f in "$BUILD"/*.mpy; do
    mpremote cp "$f" ":picomail/$(basename "$f")"
done

while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            [ -f src/config.py ] || { echo "create src/config.py from src/config_example.py first"; exit 1; }
            mpremote cp src/config.py :config.py ;;
        --certs)
            shift
            mpremote mkdir :certs 2>/dev/null || true
            mpremote cp "$1/cert.der" :certs/cert.der
            mpremote cp "$1/key.der" :certs/key.der ;;
    esac
    shift
done

mpremote cp src/main.py :main.py
echo "Deployed. Resetting the Pico..."
mpremote reset
