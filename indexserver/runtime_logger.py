"""Runtime logging setup for the daemon process.

Configures a file logger and optionally mirrors logs to an attached console.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT_LOGGER = logging.getLogger("tscodesearch")
_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def configure(log_path: Path, level: int = logging.INFO) -> None:
    """Enable runtime logs to *log_path* and optional attached console."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _ROOT_LOGGER.setLevel(level)
    _ROOT_LOGGER.propagate = False

    if not any(
        isinstance(h, logging.FileHandler)
        and Path(getattr(h, "baseFilename", "")) == log_path
        for h in _ROOT_LOGGER.handlers
    ):
        fh = logging.FileHandler(log_path, mode="a", encoding="ascii", errors="replace")
        fh.setLevel(level)
        fh.setFormatter(_FORMATTER)
        _ROOT_LOGGER.addHandler(fh)

    stdout = sys.__stdout__
    has_console = stdout is not None and getattr(stdout, "isatty", lambda: False)()
    if has_console and not any(
        isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is stdout
        for h in _ROOT_LOGGER.handlers
    ):
        sh = logging.StreamHandler(stdout)
        sh.setLevel(level)
        sh.setFormatter(_FORMATTER)
        _ROOT_LOGGER.addHandler(sh)

    logging.getLogger("tscodesearch.daemon").info(
        "runtime log configured -> %s (console=%s)",
        log_path,
        has_console,
    )
