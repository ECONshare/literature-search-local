#!/bin/bash
# LitReview Local - double-click this file in Finder to start the app.
#
# It finds a Python 3.8 or newer to run launcher.py, which does the rest: it
# sets up the app's own Python 3.10+ environment, starts the app and opens it
# in your browser. If this Mac has no usable Python, it offers to install uv,
# a small tool that fetches Python - and asks first.
#
# If double-clicking says it can't be opened, or nothing happens (a ZIP
# download loses the permission to run it), open Terminal, type  bash  and a
# space, drag this file into the window, and press Return.
#
# This file must keep Unix (LF) line endings; see .gitattributes.

cd "$(dirname "$0")" || { echo "Could not open the folder of $0."; exit 1; }

pause_then_exit() {
    echo
    printf 'Press Return to close this window. '
    read -r _
    exit "${1:-1}"
}

python_help() {
    echo
    echo "To install Python yourself:"
    echo "  1. Go to https://www.python.org/downloads/macos/ and download the"
    echo "     macOS installer for Python 3.12."
    echo "  2. Open it and follow the steps."
    echo "  3. Double-click start-mac.command again."
    echo "  (With Homebrew you can instead run:  brew install python@3.12 )"
}

# Each candidate is run to check its version, never trusted by its name.
PY=""
try_python() {
    if [ -z "$PY" ] && [ -n "$1" ] && \
        "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
    then
        PY="$1"
    fi
}

try_python ".venv/bin/python"                       # made by an earlier start
for minor in 14 13 12 11 10 9 8; do
    try_python "$(command -v "python3.$minor" 2>/dev/null)"
done
try_python "/opt/homebrew/bin/python3"              # Homebrew, Apple silicon
try_python "/usr/local/bin/python3"                 # Homebrew on Intel, python.org
for minor in 14 13 12 11 10 9 8; do
    try_python "/Library/Frameworks/Python.framework/Versions/3.$minor/bin/python3"
done
on_path="$(command -v python3 2>/dev/null)"
if [ "$on_path" != "/usr/bin/python3" ]; then
    try_python "$on_path"
fi
# Apple's /usr/bin/python3 works only once the Command Line Tools are
# installed; without them it pops up an installer and fails. So ask first.
if [ -z "$PY" ] && xcode-select -p >/dev/null 2>&1; then
    try_python "/usr/bin/python3"
fi

if [ -n "$PY" ]; then
    "$PY" launcher.py "$@"
    status=$?
else
    echo
    echo "LitReview needs Python, and none that works was found on this Mac."
    echo
    UV="$(command -v uv 2>/dev/null)"
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -z "$UV" ] && [ -x "$candidate" ]; then UV="$candidate"; fi
    done
    if [ -z "$UV" ]; then
        echo "It can set Python up for you with uv, a small free tool from Astral"
        echo "(https://docs.astral.sh/uv/) that downloads a private copy of Python just"
        echo "for LitReview. uv installs into your home folder and needs no"
        echo "administrator rights."
        echo
        answer=""
        printf 'Install uv now? Type y and press Return (or just Return to cancel): '
        read -r answer
        case "$answer" in
            [yY]|[yY][eE][sS]) ;;
            *)
                echo
                echo "Nothing was installed."
                python_help
                pause_then_exit 1
                ;;
        esac
        echo
        echo "Installing uv..."
        if ! (set -o pipefail; curl -LsSf https://astral.sh/uv/install.sh | sh); then
            echo
            echo "Installing uv did not work. It needs an internet connection."
            python_help
            pause_then_exit 1
        fi
        for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
            if [ -z "$UV" ] && [ -x "$candidate" ]; then UV="$candidate"; fi
        done
        if [ -z "$UV" ]; then
            echo "uv seems to be installed, but it can't be found yet. Close this"
            echo "window and double-click start-mac.command again."
            pause_then_exit 1
        fi
    fi
    echo "Starting LitReview with Python 3.12 from uv. The first time, uv downloads"
    echo "it, which takes a minute."
    echo
    "$UV" run --no-project --python 3.12 python launcher.py "$@"
    status=$?
fi

if [ "$status" -ne 0 ]; then
    echo
    echo "LitReview could not start. The messages above say why and what to do."
    pause_then_exit "$status"
fi
exit 0
