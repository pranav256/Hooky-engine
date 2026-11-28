"""Hooky Main All-in-One Launcher: Auto-setup, Web Studio & CLI runner.

Usage:
    python main.py              -> Launch Web Studio (http://127.0.0.1:8000) & open browser
    python main.py make         -> Run autonomous batch pipeline
    python main.py doctor       -> Run system health checks
    python main.py <command>    -> Pass any command to Hooky CLI
"""
from __future__ import annotations

import sys
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ensure_env():
    env_file = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env_file.exists() and example.exists():
        shutil.copyfile(example, env_file)
        print("[hooky] Created .env configuration from template.")


def main() -> int:
    _ensure_env()
    args = sys.argv[1:]

    # Try importing hooky directly (if running inside .venv or environment)
    try:
        from hooky.cli import main as hooky_cli
    except ImportError:
        # Not inside active virtualenv; delegate to project .venv python
        venv_python = ROOT / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")
        if venv_python.exists():
            cmd = [str(venv_python), "-m", "hooky"] + (args if args else ["web"])
            return subprocess.call(cmd)

        # Virtualenv does not exist yet; trigger setup via main launcher
        print("[hooky] Virtual environment not detected. Running automated setup...")
        if sys.platform == "win32":
            script = ROOT / "main.ps1"
            return subprocess.call(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script), *args])
        else:
            script = ROOT / "main.sh"
            return subprocess.call(["bash", str(script), *args])

    return hooky_cli(args or ["web"])


if __name__ == "__main__":
    sys.exit(main())
