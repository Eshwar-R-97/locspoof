"""Device layer: one object owns one long-lived connection to the phone.

* ``DvtBackend``    — iOS 17+, via ``remote tunneld`` + DVT LocationSimulation. The simulated
                      location only lasts while this connection stays open (PLAN §3.1).
* ``LegacyBackend`` — iOS ≤16, via usbmux/lockdown ``com.apple.dt.simulatelocation``.
* ``AutoBackend``   — tries tunneld first, falls back to legacy when the device is iOS <17.
* ``FakeBackend``   — for ``--dry-run`` and tests.

API names verified against pymobiledevice3 11.56.2.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger("locpicker.backends")


class BackendError(Exception):
    """Base error for device problems."""


class DeviceNotFound(BackendError):
    """No usable device (tunneld not running, phone not plugged in, locked, …)."""


class MultipleDevices(DeviceNotFound):
    def __init__(self, udids: list[str]):
        self.udids = udids
        super().__init__(
            "Several devices are connected; pick one with --udid. Found: " + ", ".join(udids)
        )


@runtime_checkable
class DeviceBackend(Protocol):
    name: str

    async def connect(self) -> None: ...
    async def set(self, lat: float, lon: float) -> None: ...
    async def clear(self) -> None: ...
    async def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    @property
    def info(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- fake


class FakeBackend:
    """Records every call. Can be told to fail to exercise the reconnect path.

    * ``fail_set_calls`` — 1-based indices of ``set`` calls that raise (and drop the connection).
    * ``fail_connects``  — number of upcoming ``connect`` calls that raise.
    * ``no_device``      — connect raises ``DeviceNotFound`` while True.
    """

    name = "fake"

    def __init__(self, clock=time.time):
        self.clock = clock
        self.calls: list[tuple[str, float, tuple]] = []
        self.set_count = 0
        self.connect_count = 0
        self.fail_set_calls: set[int] = set()
        self.fail_connects = 0
        self.fail_clear = False
        self.no_device = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def info(self) -> dict[str, Any]:
        return {"backend": "fake", "name": "Dry-run iPhone", "ios": "—", "udid": "FAKE-0000"}

    @property
    def sets(self) -> list[tuple[float, float]]:
        return [c[2] for c in self.calls if c[0] == "set"]

    async def connect(self) -> None:
        self.connect_count += 1
        self.calls.append(("connect", self.clock(), ()))
        if self.no_device:
            raise DeviceNotFound("fake: no device")
        if self.fail_connects > 0:
            self.fail_connects -= 1
            raise ConnectionError("fake: connect failed")
        self._connected = True

    async def set(self, lat: float, lon: float) -> None:
        if not self._connected:
            raise ConnectionError("fake: not connected")
        self.set_count += 1
        if self.set_count in self.fail_set_calls:
            self._connected = False
            self.calls.append(("set_failed", self.clock(), (lat, lon)))
            raise ConnectionError(f"fake: set #{self.set_count} failed")
        self.calls.append(("set", self.clock(), (lat, lon)))

    async def clear(self) -> None:
        if not self._connected:
            raise ConnectionError("fake: not connected")
        if self.fail_clear:
            raise ConnectionError("fake: clear failed")
        self.calls.append(("clear", self.clock(), ()))

    async def close(self) -> None:
        if self._connected:
            self.calls.append(("close", self.clock(), ()))
        self._connected = False


# --------------------------------------------------------------------------- DDI


_mounted_once: set[str] = set()


async def ensure_ddi(provider: Any, enabled: bool = True) -> None:
    """Auto-mount the Developer Disk Image once per device. Never raises."""
    if not enabled:
        return
    udid = getattr(provider, "udid", None) or "?"
    if udid in _mounted_once:
        return
    _mounted_once.add(udid)
    try:
        from pymobiledevice3.exceptions import AlreadyMountedError
        from pymobiledevice3.services.mobile_image_mounter import auto_mount
    except Exception as e:  # pragma: no cover
        log.warning("DDI auto-mount unavailable: %s", e)
        return
    try:
        await asyncio.wait_for(auto_mount(provider), timeout=120)
        log.info("Developer disk image mounted.")
    except AlreadyMountedError:
        log.info("Developer disk image already mounted.")
    except Exception as e:
        log.warning("Developer disk image auto-mount failed (%s: %s). If location calls fail, run "
                    "`python3 -m pymobiledevice3 mounter auto-mount --tunnel \"\"` and check Developer Mode.",
                    type(e).__name__, e)


def _device_info(provider: Any, backend: str) -> dict[str, Any]:
    vals = getattr(provider, "all_values", None) or {}
    try:
        ios = provider.product_version
    except Exception:
        ios = vals.get("ProductVersion", "?")
    return {
        "backend": backend,
        "name": vals.get("DeviceName") or getattr(provider, "product_type", None) or "iPhone",
        "ios": ios,
        "udid": getattr(provider, "udid", None) or vals.get("UniqueDeviceID"),
    }


# --------------------------------------------------------------------------- DVT (iOS 17+)


class DvtBackend:
    name = "dvt"

    def __init__(self, udid: Optional[str] = None, tunneld_address: tuple[str, int] = ("127.0.0.1", 49151),
                 mount: bool = True):
        self.udid = udid
        self.tunneld_address = tunneld_address
        self.mount = mount
        self._stack: Optional[AsyncExitStack] = None
        self._sim: Any = None
        self._info: dict[str, Any] = {"backend": "dvt"}

    @property
    def connected(self) -> bool:
        return self._sim is not None

    @property
    def info(self) -> dict[str, Any]:
        return dict(self._info)

    async def _pick_rsd(self):
        from pymobiledevice3.exceptions import TunneldConnectionError
        from pymobiledevice3.tunneld.api import get_tunneld_device_by_udid, get_tunneld_tunnels

        try:
            # get_tunneld_tunnels runs the blocking HTTP call in a thread.
            tunnels = await get_tunneld_tunnels(self.tunneld_address)
        except TunneldConnectionError as e:
            raise DeviceNotFound(
                "Can't reach tunneld at %s:%d. Start it with `sudo python3 -m pymobiledevice3 remote tunneld`."
                % self.tunneld_address
            ) from e
        udids = list(tunnels)
        if self.udid:
            if self.udid not in tunnels:
                raise DeviceNotFound(f"Device {self.udid} is not in tunneld (found: {', '.join(udids) or 'none'}). "
                                     "Is the phone unlocked and connected?")
            udid = self.udid
        else:
            if not udids:
                raise DeviceNotFound("No device in tunneld. Is `remote tunneld` running and the phone unlocked?")
            if len(udids) > 1:
                raise MultipleDevices(udids)
            udid = udids[0]
        # The tunnel list is already known reachable, so this second (blocking) listing is quick.
        rsd = await get_tunneld_device_by_udid(udid, self.tunneld_address)
        if rsd is None:
            raise DeviceNotFound(f"tunneld listed {udid} but connecting to it failed. Unlock the phone and retry.")
        return rsd

    async def connect(self) -> None:
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

        await self.close()
        rsd = await self._pick_rsd()
        stack = AsyncExitStack()
        try:
            stack.push_async_callback(rsd.close)
            await ensure_ddi(rsd, self.mount)
            dvt = await stack.enter_async_context(DvtProvider(rsd))
            self._sim = await stack.enter_async_context(LocationSimulation(dvt))
        except BaseException:
            self._sim = None
            await _safe_aclose(stack)
            raise
        self._stack = stack
        self._info = _device_info(rsd, "dvt")
        log.info("Connected (DVT) to %s, iOS %s, %s", self._info["name"], self._info["ios"], self._info["udid"])

    async def set(self, lat: float, lon: float) -> None:
        if self._sim is None:
            raise ConnectionError("not connected")
        await self._sim.set(lat, lon)

    async def clear(self) -> None:
        if self._sim is None:
            raise ConnectionError("not connected")
        await self._sim.clear()

    async def close(self) -> None:
        stack, self._stack, self._sim = self._stack, None, None
        if stack is not None:
            await _safe_aclose(stack)


async def _safe_aclose(stack: AsyncExitStack) -> None:
    try:
        await asyncio.wait_for(stack.aclose(), timeout=5)
    except Exception as e:
        log.debug("error while closing connection: %s", e)


# --------------------------------------------------------------------------- legacy (iOS ≤16)


class LegacyBackend:
    """iOS ≤16. Each set/clear opens its own short service connection; the location persists
    after it closes, until clear() or a reboot."""

    name = "legacy"

    def __init__(self, udid: Optional[str] = None, mount: bool = True, lockdown: Any = None):
        self.udid = udid
        self.mount = mount
        self._lockdown = lockdown
        self._sim: Any = None
        self._info: dict[str, Any] = {"backend": "legacy"}
        if lockdown is not None:
            self._adopt(lockdown)

    def _adopt(self, lockdown: Any) -> None:
        from pymobiledevice3.services.simulate_location import DtSimulateLocation

        self._lockdown = lockdown
        self._sim = DtSimulateLocation(lockdown)
        self._info = _device_info(lockdown, "legacy")

    @property
    def connected(self) -> bool:
        return self._sim is not None

    @property
    def info(self) -> dict[str, Any]:
        return dict(self._info)

    async def connect(self) -> None:
        if self._lockdown is not None and self._sim is not None:
            return
        await self.close()
        lockdown = await usbmux_lockdown(self.udid)
        try:
            await ensure_ddi(lockdown, self.mount)
            self._adopt(lockdown)
        except BaseException:
            await _close_quietly(lockdown)
            raise
        log.info("Connected (legacy) to %s, iOS %s, %s", self._info["name"], self._info["ios"], self._info["udid"])

    async def set(self, lat: float, lon: float) -> None:
        if self._sim is None:
            raise ConnectionError("not connected")
        await self._sim.set(lat, lon)

    async def clear(self) -> None:
        if self._sim is None:
            raise ConnectionError("not connected")
        await self._sim.clear()

    async def close(self) -> None:
        lockdown, self._lockdown, self._sim = self._lockdown, None, None
        if lockdown is not None:
            await _close_quietly(lockdown)


async def _close_quietly(obj: Any) -> None:
    try:
        r = obj.close()
        if asyncio.iscoroutine(r):
            await asyncio.wait_for(r, timeout=5)
    except Exception as e:
        log.debug("close failed: %s", e)


async def usbmux_lockdown(udid: Optional[str]):
    from pymobiledevice3.exceptions import NoDeviceConnectedError, DeviceNotFoundError
    from pymobiledevice3.lockdown import create_using_usbmux

    try:
        return await create_using_usbmux(serial=udid)
    except (NoDeviceConnectedError, DeviceNotFoundError) as e:
        raise DeviceNotFound("No device over USB/Wi-Fi (usbmux). Plug in and unlock the phone, and trust this Mac.") from e
    except ConnectionRefusedError as e:
        raise DeviceNotFound("usbmuxd is not reachable.") from e


def _major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except ValueError:
        return 0


# --------------------------------------------------------------------------- auto


class AutoBackend:
    """Tunneld/DVT first; if the device is iOS <17 (or tunneld has nothing), use lockdown."""

    name = "auto"

    def __init__(self, udid: Optional[str] = None, tunneld_address=("127.0.0.1", 49151), mount: bool = True):
        self.udid = udid
        self.tunneld_address = tunneld_address
        self.mount = mount
        self._inner: Optional[Any] = None

    @property
    def connected(self) -> bool:
        return self._inner is not None and self._inner.connected

    @property
    def info(self) -> dict[str, Any]:
        return self._inner.info if self._inner else {"backend": "auto"}

    async def connect(self) -> None:
        # Stick with the backend that worked last time.
        if self._inner is not None:
            await self._inner.connect()
            return
        dvt = DvtBackend(self.udid, self.tunneld_address, self.mount)
        try:
            await dvt.connect()
            self._inner = dvt
            return
        except MultipleDevices:
            raise
        except DeviceNotFound as dvt_err:
            try:
                lockdown = await usbmux_lockdown(self.udid)
            except DeviceNotFound:
                raise dvt_err
            if _major(lockdown.product_version) >= 17:
                await _close_quietly(lockdown)
                raise DeviceNotFound(
                    f"iOS {lockdown.product_version} device found over USB, but it isn't in tunneld. "
                    "Run `sudo python3 -m pymobiledevice3 remote tunneld` and keep the phone unlocked."
                ) from dvt_err
            legacy = LegacyBackend(self.udid, self.mount)
            try:
                await ensure_ddi(lockdown, self.mount)
                legacy._adopt(lockdown)
            except BaseException:
                await _close_quietly(lockdown)
                raise
            self._inner = legacy
            log.info("Using legacy backend for iOS %s", lockdown.product_version)

    async def set(self, lat: float, lon: float) -> None:
        if self._inner is None:
            raise ConnectionError("not connected")
        await self._inner.set(lat, lon)

    async def clear(self) -> None:
        if self._inner is None:
            raise ConnectionError("not connected")
        await self._inner.clear()

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


def make_backend(cfg) -> Any:
    if cfg.dry_run:
        return FakeBackend()
    addr = (cfg.tunneld_host, cfg.tunneld_port)
    if cfg.legacy:
        return LegacyBackend(cfg.udid, mount=not cfg.no_mount)
    return AutoBackend(cfg.udid, addr, mount=not cfg.no_mount)


# --------------------------------------------------------------------------- smoke command


async def _smoke(args) -> int:
    from .config import Config

    cfg = Config(udid=args.udid, legacy=args.legacy, dry_run=args.dry_run, no_mount=args.no_mount)
    backend = make_backend(cfg)
    try:
        await backend.connect()
        print("Connected:", backend.info)
        if args.clear:
            await backend.clear()
            print("Cleared — real location restored.")
            return 0
        lat, lon = args.set
        await backend.set(lat, lon)
        print(f"Location set to {lat}, {lon}.")
        if backend.name in ("dvt", "auto") and isinstance(getattr(backend, "_inner", backend), DvtBackend):
            print("Holding the DVT connection open (iOS 17+ needs it). Press Ctrl+C to clear and exit.")
            try:
                while True:
                    await asyncio.sleep(10)
                    await backend.set(lat, lon)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            await backend.clear()
            print("Cleared — real location restored.")
        return 0
    except BackendError as e:
        print("Error:", e)
        return 1
    finally:
        await backend.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m locpicker.backends", description="Backend smoke test")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--set", nargs=2, type=float, metavar=("LAT", "LON"),
                   help="set location (use --set -- LAT LON for negative values)")
    g.add_argument("--clear", action="store_true", help="restore real location")
    p.add_argument("--udid")
    p.add_argument("--legacy", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-mount", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(_smoke(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
