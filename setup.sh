#!/usr/bin/env bash
# Setup for the brain-mapper demo on macOS and Linux.
#
# Usage: ./setup.sh [--yes]
#
# Each step checks before it acts, so running the script again changes
# nothing that is already in place. It prints one PASS or FAIL line per step
# and, at the end, the command that starts the demo.
#
# The script does not use sudo, does not edit shell profiles, does not open a
# serial port and writes only inside this folder and uv's own folders. It
# installs uv (after asking; --yes answers yes) and nothing else. For anything
# else it prints the command.
#
# The Linux serial-port group check is written from the documentation of the
# usual device groups. It has not been run on Linux.

set -u

assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --yes | -y) assume_yes=1 ;;
        -h | --help)
            echo "usage: ./setup.sh [--yes]"
            exit 0
            ;;
        *)
            echo "setup.sh: unknown option: $arg" >&2
            echo "usage: ./setup.sh [--yes]" >&2
            exit 2
            ;;
    esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
cd "$here" || exit 1

UV_INSTALL='curl -LsSf https://astral.sh/uv/install.sh | sh'
RUST_INSTALL="curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
START='uv run brain-mapper'

pass() { echo "PASS $1: $2"; }
fail() { echo "FAIL $1: $2"; }

# --- Step 1: uv -------------------------------------------------------------
if command -v uv > /dev/null 2>&1; then
    pass uv "$(uv --version)"
else
    answer=n
    if [ "$assume_yes" -eq 1 ]; then
        answer=y
    else
        printf 'uv is not installed. Install it with: %s\nInstall it now? [y/N] ' "$UV_INSTALL"
        read -r answer || answer=n
    fi
    case "$answer" in
        y | Y | yes | YES | Yes)
            if ! command -v curl > /dev/null 2>&1; then
                fail uv "curl is not installed, so the installer cannot be fetched. Install uv with: $UV_INSTALL"
                exit 1
            fi
            if ! sh -c "$UV_INSTALL"; then
                fail uv "the installer did not finish. Command: $UV_INSTALL"
                exit 1
            fi
            # The installer puts uv in one of these folders; it does not change
            # the shell profile of this script's caller beyond what it does itself.
            PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
            export PATH
            if ! command -v uv > /dev/null 2>&1; then
                fail uv "uv was installed but is not on PATH. Open a new terminal and run ./setup.sh again"
                exit 1
            fi
            pass uv "installed $(uv --version)"
            ;;
        *)
            fail uv "not installed, nothing was installed. Install it with: $UV_INSTALL"
            exit 1
            ;;
    esac
fi

# --- Step 2: Rust toolchain and project environment ---------------------------
if cargo_version="$(cargo --version 2> /dev/null)"; then
    pass rust "$cargo_version"
else
    fail rust "cargo was not found, nothing was installed. Install the Rust toolchain with: $RUST_INSTALL (then open a new terminal and run ./setup.sh again)"
    exit 1
fi

# The default groups of the project (the same set `uv run` keeps in place, so
# that `uv run brain-mapper` afterwards changes nothing). The documentation
# group is not installed. --locked leaves uv.lock as it is.
if CARGO_BUILD_JOBS=4 uv sync --locked; then
    pass environment "uv sync --locked"
else
    fail environment "uv sync did not finish; the messages above say why"
    exit 1
fi

# --- Step 3: checks of the demo itself ----------------------------------------
# `brain-mapper --check` prints one line per check, each starting with PASS or
# FAIL, and exits 1 if one failed. It installs nothing and opens no serial port.
if CARGO_BUILD_JOBS=4 uv run --locked brain-mapper --check; then
    pass check "brain-mapper --check"
else
    fail check "brain-mapper --check reported a problem; the lines above say which"
    check_failed=1
fi

# On Linux, the serial device of the dongle belongs to a group (often dialout
# or uucp); a user who is not in it cannot open the port.
if [ "$(uname -s)" = Linux ]; then
    dev=""
    for candidate in /dev/ttyACM* /dev/ttyUSB*; do
        if [ -e "$candidate" ]; then
            dev="$candidate"
            break
        fi
    done
    if [ -n "$dev" ]; then
        group="$(stat -c %G "$dev" 2> /dev/null || true)"
        if [ -n "$group" ] && ! id -nG | tr ' ' '\n' | grep -qx "$group"; then
            fail serial-group "$dev belongs to the group $group and this user is not in it. Run: sudo usermod -aG $group \"\$USER\" (then log out and in again)"
            check_failed=1
        else
            pass serial-group "$dev is readable by this user's groups"
        fi
    fi
fi

if [ "${check_failed:-0}" -ne 0 ]; then
    exit 1
fi

# --- Step 4: how to start -----------------------------------------------------
echo "To start the demo, run in this folder: $START"
exit 0
