#!/usr/bin/env python3
"""Location Picker — entrypoint.

    python3 run.py [--legacy] [--udid UDID] [--port 8765] [--host 127.0.0.1]
                   [--token TOKEN] [--tick 1.0] [--dry-run]
                   [--auto-reset-after DURATION] [--data-dir ./data] [-v]
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from aiohttp import web

from locpicker import __version__
from locpicker.backends import BackendError, make_backend
from locpicker.config import Config, parse_args
from locpicker.engine import Engine
from locpicker.server import create_app
from locpicker.spots import SpotStore


def setup_logging(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if cfg.verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    fileh = RotatingFileHandler(cfg.data_dir / "locpicker.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)
    root.addHandler(fileh)
    # pymobiledevice3 is chatty at INFO
    for noisy in ("pymobiledevice3", "asyncio", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if cfg.verbose else logging.WARNING)


def _fmt_dur(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return " ".join(p for p in (f"{h}h" if h else "", f"{m}m" if m else "", f"{sec}s" if sec else "") if p)


async def main(cfg: Config) -> int:
    log = logging.getLogger("locpicker")
    backend = make_backend(cfg)
    engine = Engine(backend, tick=cfg.tick, auto_reset_after=cfg.auto_reset_after)

    # One connection attempt up front so the banner can show the device; the engine keeps retrying.
    device_line = "not connected yet (will keep retrying)"
    try:
        await asyncio.wait_for(backend.connect(), timeout=45)
        info = backend.info
        device_line = f"{info.get('name')} · iOS {info.get('ios')} · {info.get('udid')}"
        engine.state.device = dict(info)
        engine.state.connection = "connected"
    except BackendError as e:
        log.warning("%s", e)
    except asyncio.TimeoutError:
        log.warning("Timed out connecting to the device; will keep retrying.")
    except Exception as e:
        log.warning("Device connection failed: %s: %s", type(e).__name__, e)

    spots = SpotStore(cfg.data_dir / "spots.json")
    app = create_app(engine, spots, cfg)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    try:
        await site.start()
    except OSError as e:
        print(f"Couldn't listen on {cfg.host}:{cfg.port}: {e}", file=sys.stderr)
        await runner.cleanup()
        return 1

    shown_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::") else cfg.host
    url = f"http://{shown_host}:{cfg.port}/"
    if cfg.token:
        url += f"?token={cfg.token}"
    kind = {"fake": "dry-run (fake device)", "auto": "auto (tunneld → legacy)", "legacy": "legacy (iOS ≤16)",
            "dvt": "DVT (iOS 17+)"}.get(backend.name, backend.name)
    if backend.connected and getattr(backend, "info", {}).get("backend"):
        kind += f" → {backend.info['backend']}"
    print()
    print(f"  Location Picker {__version__}")
    print(f"  Backend : {kind}")
    print(f"  Device  : {device_line}")
    print(f"  Open    : {url}")
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  LAN     : listening on {cfg.host}:{cfg.port} (token required)")
    if cfg.auto_reset_after:
        print(f"  Auto-reset after {_fmt_dur(cfg.auto_reset_after)} of simulated location")
    print("  Ctrl+C restores your real location and exits.")
    print()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    await stop.wait()
    print("\nShutting down…")
    await runner.cleanup()  # cancels the tick loop, clears the location (5 s timeout), closes the connection
    return 0


if __name__ == "__main__":
    cfg = parse_args()
    setup_logging(cfg)
    try:
        raise SystemExit(asyncio.run(main(cfg)))
    except KeyboardInterrupt:
        raise SystemExit(130)
