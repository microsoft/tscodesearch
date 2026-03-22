"""
Typesense server utilities for WSL.

The Typesense binary lives at ~/.local/typesense/typesense-server.
This script runs natively in WSL.

Usage:
    python start_server.py --stop    -- stop the server
    python start_server.py --install -- download and install the binary
    python start_server.py --log     -- print the info log
    python start_server.py --errlog  -- print the error log
"""

import os
import sys
import time
import signal
import subprocess
import argparse
import urllib.request
from pathlib import Path

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

from indexserver.config import TYPESENSE_VERSION

_HOME    = Path.home()

# Support Docker: TYPESENSE_DATA env var overrides default location
_RUN_DIR = Path(os.environ.get("TYPESENSE_DATA", _HOME / ".local" / "typesense"))
_RUN_DIR.mkdir(parents=True, exist_ok=True)

PID_FILE       = _RUN_DIR / "typesense.pid"
LOG_PATH       = str(_RUN_DIR / "typesense.log")
ERROR_LOG_PATH = str(_RUN_DIR / "typesense-error.log")

# Docker pre-installs binary at TYPESENSE_DIR; otherwise use ~/.local/typesense
_TYPESENSE_DIR = os.environ.get("TYPESENSE_DIR", "")
_DOCKER_BIN = f"{_TYPESENSE_DIR}/typesense-server" if _TYPESENSE_DIR else ""
BIN_PATH = str(_RUN_DIR / "typesense-server")


# ── Core operations ────────────────────────────────────────────────────────────

def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    pid = PID_FILE.read_text().strip()
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)  # signal 0 = existence check
        return True
    except (OSError, ProcessLookupError):
        return False


def stop():
    if not PID_FILE.exists():
        subprocess.run(["pkill", "-f", "typesense-server"], capture_output=True)
        print("Sent kill signal (no PID file found).")
        return
    pid_str = PID_FILE.read_text().strip()
    pid = int(pid_str) if pid_str.isdigit() else None
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait up to 10s for graceful shutdown before escalating to SIGKILL
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)  # existence check
                    time.sleep(0.2)
                except (OSError, ProcessLookupError):
                    break  # process gone
            else:
                # Still alive after 10s — force kill
                try:
                    print(f"  Typesense (pid={pid}) did not stop in 10s — sending SIGKILL")
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
                except (OSError, ProcessLookupError):
                    pass
        except (OSError, ProcessLookupError):
            pass
    PID_FILE.unlink(missing_ok=True)
    print(f"Typesense (pid={pid_str}) stopped.")


def install_binary() -> None:
    """Download and install the Typesense binary to BIN_PATH if not already present."""
    if _DOCKER_BIN and os.path.isfile(_DOCKER_BIN):
        print(f"Typesense binary provided by Docker at {_DOCKER_BIN} — skipping download.")
        return

    if os.path.isfile(BIN_PATH) and os.access(BIN_PATH, os.X_OK):
        print(f"Typesense binary already installed at {BIN_PATH}.")
        return

    import tarfile
    import io

    tar_url = (
        f"https://dl.typesense.org/releases/{TYPESENSE_VERSION}/"
        f"typesense-server-{TYPESENSE_VERSION}-linux-amd64.tar.gz"
    )
    print(f"Downloading Typesense v{TYPESENSE_VERSION} from dl.typesense.org ...")
    _RUN_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with urllib.request.urlopen(tar_url, timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        print(f"ERROR: Failed to download Typesense binary: {e}")
        print(f"       URL: {tar_url}")
        sys.exit(1)

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            member = next(
                (m for m in tf.getmembers() if m.name.endswith("typesense-server")),
                None,
            )
            if member is None:
                print("ERROR: typesense-server not found in downloaded archive.")
                sys.exit(1)
            member.name = "typesense-server"  # flatten to bare filename
            tf.extract(member, path=str(_RUN_DIR))
    except Exception as e:
        print(f"ERROR: Failed to extract Typesense binary: {e}")
        sys.exit(1)

    os.chmod(BIN_PATH, 0o755)
    print(f"Typesense v{TYPESENSE_VERSION} installed at {BIN_PATH}.")


def show_log():
    subprocess.run(["cat", LOG_PATH])


def show_error_log():
    subprocess.run(["cat", ERROR_LOG_PATH])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stop",    action="store_true", help="Stop the server")
    ap.add_argument("--install", action="store_true", help="Download and install the Typesense binary")
    ap.add_argument("--log",     action="store_true", help="Print the info log")
    ap.add_argument("--errlog",  action="store_true", help="Print the error log")
    args = ap.parse_args()
    if args.log:
        show_log()
    elif args.errlog:
        show_error_log()
    elif args.stop:
        stop()
    elif args.install:
        install_binary()
    else:
        ap.print_help()
