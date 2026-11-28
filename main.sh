#!/usr/bin/env bash
# Hooky Main All-in-One Runner & Setup (Linux & macOS)
# Usage:
#   ./main.sh          -> Auto-setup if needed, launch Web Studio, open browser
#   ./main.sh web      -> Launch Web Studio (http://127.0.0.1:8000)
#   ./main.sh make     -> Run autonomous batch pipeline
#   ./main.sh doctor   -> Run system health checks
#   ./main.sh <cmd>    -> Any Hooky CLI command
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

install_system_tools() {
    case "$(uname -s)" in
        Darwin)
            if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 &&
                command -v curl >/dev/null 2>&1; then
                return
            fi
            command -v brew >/dev/null 2>&1 || {
                printf '%s\n' '[hooky] Install Homebrew first: https://brew.sh' >&2
                exit 1
            }
            brew install ffmpeg
            ;;
        Linux)
            if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1 &&
                command -v curl >/dev/null 2>&1; then
                return
            fi
            if command -v apt-get >/dev/null 2>&1; then
                as_root apt-get update
                as_root apt-get install -y ffmpeg curl
            elif command -v dnf >/dev/null 2>&1; then
                as_root dnf install -y ffmpeg curl
            elif command -v pacman >/dev/null 2>&1; then
                as_root pacman -Sy --needed --noconfirm ffmpeg curl
            else
                printf '%s\n' '[hooky] Install FFmpeg and FFprobe with your package manager, then rerun main.sh.' >&2
                exit 1
            fi
            ;;
        *)
            printf '%s\n' '[hooky] Unsupported OS. Install FFmpeg and FFprobe, then run uv sync.' >&2
            exit 1
            ;;
    esac
}

install_uv() {
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"
        return
    fi

    command -v curl >/dev/null 2>&1 || {
        printf '%s\n' '[hooky] curl is required to install uv.' >&2
        exit 1
    }
    mkdir -p "$ROOT/.tools"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$ROOT/.tools" sh
    UV="$ROOT/.tools/uv"
}

# 1. Install FFmpeg & uv if virtual environment does not exist
if [ ! -f "$ROOT/.venv/bin/python" ]; then
    printf '%s\n' '[hooky] Initializing dependencies & environment...'
    install_system_tools
    install_uv
    "$UV" sync --extra all
fi

# 2. Check .env and prompt for environment configuration choices if interactive
IS_INTERACTIVE=0
if [ -t 0 ]; then
    IS_INTERACTIVE=1
fi

SKIP_SETUP="${SKIP_SETUP:-0}"

if [ "${1:-}" = "--setup" ] || [ "${1:-}" = "setup" ]; then
    shift
    exec "$ROOT/.venv/bin/python" -m hooky setup "$@"
fi

if [ ! -f "$ROOT/.env" ]; then
    if [ -f "$ROOT/.env.example" ]; then
        cp "$ROOT/.env.example" "$ROOT/.env"
        printf '%s\n' '[hooky] Created .env configuration from template.'
    fi
    if [ "$IS_INTERACTIVE" -eq 1 ] && [ "$SKIP_SETUP" -eq 0 ]; then
        printf '\n%s\n' '[hooky] First-run configuration needed.'
        read -r -p "[hooky] Would you like to configure your API keys and environment settings now? (Y/n) " answer
        if [ "$answer" != "n" ] && [ "$answer" != "N" ]; then
            "$ROOT/.venv/bin/python" -m hooky setup
        fi
    fi
elif [ "$IS_INTERACTIVE" -eq 1 ] && [ "$SKIP_SETUP" -eq 0 ]; then
    if [ $# -eq 0 ] || [ "${1:-}" = "web" ] || [ "${1:-}" = "make" ] || [ "${1:-}" = "parts" ] || [ "${1:-}" = "post" ]; then
        read -r -p "[hooky] Would you like to review or update your .env settings before launch? (y/N) " answer
        if [ "$answer" = "y" ] || [ "$answer" = "Y" ] || [ "$answer" = "yes" ]; then
            "$ROOT/.venv/bin/python" -m hooky setup --redo
        fi
    fi
fi

export HOOKY_LAUNCHER_PROMPTED="1"

# 3. Determine command: default to Web Studio
if [ $# -eq 0 ]; then
    printf '\n%s\n\n' '[hooky] Launching Hooky Web Studio at http://127.0.0.1:8000...'
    exec "$ROOT/.venv/bin/python" -m hooky web
else
    exec "$ROOT/.venv/bin/python" -m hooky "$@"
fi
