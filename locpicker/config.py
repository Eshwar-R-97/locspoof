"""Settings dataclass + CLI parsing."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    token: Optional[str] = None
    udid: Optional[str] = None
    legacy: bool = False
    dry_run: bool = False
    tick: float = 1.0
    auto_reset_after: Optional[float] = None  # seconds
    data_dir: Path = Path("./data")
    verbose: bool = False
    tunneld_host: str = "127.0.0.1"
    tunneld_port: int = 49151
    no_mount: bool = False

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms]?)", re.I)


def parse_duration(text: str) -> float:
    """Parse ``4h``, ``30m``, ``90s``, ``1h30m`` or a bare number of seconds."""
    s = text.strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    pos, total = 0, 0.0
    for m in _DUR_RE.finditer(s):
        if m.start() != pos:
            raise ValueError(f"invalid duration: {text!r}")
        pos = m.end()
        n = float(m.group(1))
        total += n * {"h": 3600, "m": 60, "s": 1, "": 1}[m.group(2)]
    if pos != len(s):
        raise ValueError(f"invalid duration: {text!r}")
    if total <= 0:
        raise ValueError("duration must be positive")
    return total


def parse_args(argv: Optional[Sequence[str]] = None) -> Config:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Location Picker — set your own iPhone's simulated GPS location from a local web UI.",
    )
    p.add_argument("--legacy", action="store_true", help="force the iOS ≤16 backend (usbmux/lockdown)")
    p.add_argument("--udid", help="device UDID (required when several devices are connected)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="bind address (non-loopback requires --token)")
    p.add_argument("--token", help="bearer token required on every /api request")
    p.add_argument("--tick", type=float, default=1.0, help="seconds between location updates (default 1.0)")
    p.add_argument("--dry-run", action="store_true", help="no phone: use a fake backend")
    p.add_argument("--auto-reset-after", metavar="DURATION", help="restore real GPS after e.g. 4h, 30m")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--tunneld", default="127.0.0.1:49151", help="tunneld address host:port")
    p.add_argument("--no-mount", action="store_true", help="skip developer disk image auto-mount")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    if not (0.1 <= a.tick <= 10):
        p.error("--tick must be between 0.1 and 10 seconds")
    if not (1 <= a.port <= 65535):
        p.error("--port out of range")
    auto = None
    if a.auto_reset_after:
        try:
            auto = parse_duration(a.auto_reset_after)
        except ValueError as e:
            p.error(str(e))
    th, _, tp = a.tunneld.rpartition(":")
    try:
        tport = int(tp)
    except ValueError:
        p.error("--tunneld must be host:port")
    cfg = Config(
        host=a.host,
        port=a.port,
        token=a.token,
        udid=a.udid,
        legacy=a.legacy,
        dry_run=a.dry_run,
        tick=a.tick,
        auto_reset_after=auto,
        data_dir=Path(a.data_dir),
        verbose=a.verbose,
        tunneld_host=th or "127.0.0.1",
        tunneld_port=tport,
        no_mount=a.no_mount,
    )
    if not cfg.is_loopback and not cfg.token:
        p.error("--host other than 127.0.0.1 requires --token (otherwise anyone on your network could move your location)")
    if cfg.token is not None and len(cfg.token) < 8:
        p.error("--token must be at least 8 characters")
    return cfg
