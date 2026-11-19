"""Async state machine + tick loop. No file/network I/O beyond the injected backend.

Tests drive it deterministically by injecting ``clock`` and ``rng`` and calling ``step()``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from . import geo
from .backends import DeviceNotFound
from .geo import LatLon

log = logging.getLogger("locpicker.engine")

Mode = Literal["idle", "static", "travel", "wander", "paused"]
Profile = Literal["walk", "bike", "drive", "fly", "custom"]
PROFILES = ("walk", "bike", "drive", "fly", "custom")
Connection = Literal["connected", "reconnecting", "no_device"]

KEEPALIVE_S = 10.0
BACKOFF = [1, 2, 4, 8, 15]
MAX_DT = 5.0
CONNECT_TIMEOUT = 30.0

# Validation limits (shared with the server).
SPEED_MIN, SPEED_MAX = 0.1, 1000.0
RADIUS_MIN, RADIUS_MAX = 2.0, 500.0
PATH_MAX_POINTS = 20_000
FLY_ACCEL = 1.5  # m/s² take-off/landing ramp for realistic flights
FLY_RAMP_FRAC = 0.15  # each ramp covers at most this share of the route
START_SNAP_M = 200.0


@dataclass(frozen=True)
class WanderParams:
    radius_m: float = 20.0
    k: float = 0.02
    sigma: float = 0.3
    max_speed: float = 0.6


WANDER_PRESETS: dict[str, WanderParams] = {
    "default": WanderParams(),
    "indoors": WanderParams(radius_m=10.0, k=0.03, sigma=0.15, max_speed=0.3),
    "walking": WanderParams(radius_m=40.0, k=0.01, sigma=0.45, max_speed=1.2),
    "drift": WanderParams(radius_m=5.0, k=0.05, sigma=0.08, max_speed=0.2),
}


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class EngineState:
    mode: Mode = "idle"
    position: Optional[LatLon] = None  # current simulated position (last sent, or pending send)
    target: Optional[LatLon] = None
    # travel
    path: Optional[list[LatLon]] = None
    cum: Optional[list[float]] = None
    total_m: float = 0.0
    traveled_m: float = 0.0
    speed_mps: float = 1.4
    current_speed: float = 0.0
    profile: Profile = "walk"
    realistic: bool = True
    wander_on_arrival: bool = False
    route_id: int = 0
    stop_reason: Optional[str] = None  # "traffic" | "pause" while realism holds the car/walker
    # wander
    wander_center: Optional[LatLon] = None
    wander_radius_m: float = 20.0
    wander_preset: str = "default"
    wander_vel: tuple[float, float] = (0.0, 0.0)
    wander_offset: tuple[float, float] = (0.0, 0.0)
    # paused
    paused_from: Optional[Mode] = None
    # meta
    started_at: float = 0.0
    faked_since: Optional[float] = None
    connection: Connection = "reconnecting"
    last_error: Optional[str] = None
    last_sent_at: Optional[float] = None
    device: dict = field(default_factory=dict)


class SpeedModel:
    """Realistic speed: slow drift (±10 %), drive deceleration + traffic stops, walk pauses,
    flight take-off/landing ramps."""

    def __init__(self, base: float, profile: Profile, realistic: bool, rng: random.Random, total_m: float = 0.0):
        self.base, self.profile, self.realistic, self.rng = base, profile, realistic, rng
        # Flights ramp up/down at FLY_ACCEL, steeper on short hops so each ramp stays under FLY_RAMP_FRAC.
        self.fly_accel = max(FLY_ACCEL, base * base / (2 * FLY_RAMP_FRAC * total_m)) if total_m > 0 else FLY_ACCEL
        self.factor = 1.0  # slow multiplicative drift around 1.0
        self.walk_delta = 0.0  # ±0.2 m/s for walking
        self.hold_until: Optional[float] = None
        self.hold_reason: Optional[str] = None
        self.next_stop_at_m = self._next_traffic(0.0)
        self.started_hold_check = False

    def _next_traffic(self, traveled: float) -> float:
        return traveled + self.rng.uniform(600, 1200)

    def speed(self, now: float, dt: float, traveled: float, remaining: float) -> float:
        if not self.realistic:
            self.hold_reason = None
            return self.base
        if self.hold_until is not None:
            if now < self.hold_until:
                return 0.0
            self.hold_until, self.hold_reason = None, None
        if dt > 0:
            # Ornstein–Uhlenbeck drift on the factor: smooth, not per-tick noise.
            self.factor += -0.05 * (self.factor - 1.0) * dt + 0.02 * self.rng.gauss(0, 1) * math.sqrt(dt)
            self.factor = min(1.1, max(0.9, self.factor))
        v = self.base * self.factor
        if self.profile == "walk":
            if dt > 0:
                self.walk_delta += -0.1 * self.walk_delta * dt + 0.05 * self.rng.gauss(0, 1) * math.sqrt(dt)
                self.walk_delta = min(0.2, max(-0.2, self.walk_delta))
            v = self.base + self.walk_delta
            # Occasional 5–15 s pause (≈ once every 4 minutes of walking), not near the end.
            if dt > 0 and remaining > 20 and self.rng.random() < dt / 240.0:
                self.hold_until = now + self.rng.uniform(5, 15)
                self.hold_reason = "pause"
                return 0.0
        elif self.profile == "drive":
            if traveled >= self.next_stop_at_m:
                self.next_stop_at_m = self._next_traffic(traveled)
                if remaining > 200:
                    self.hold_until = now + self.rng.uniform(10, 40)
                    self.hold_reason = "traffic"
                    return 0.0
            if remaining < 150:
                v *= 0.4 + 0.6 * (remaining / 150.0)
        elif self.profile == "fly":
            # Cruise drift is gentler than on the ground (±3 %), plus take-off and landing ramps.
            v = self.base * (1.0 + (self.factor - 1.0) * 0.3)
            a = self.fly_accel
            v = min(v, max(math.sqrt(2 * a * traveled), 10.0), max(math.sqrt(2 * a * remaining), 5.0))
        return max(0.2 if self.profile == "drive" else 0.1, v)


Subscriber = asyncio.Queue


class Engine:
    def __init__(
        self,
        backend,
        *,
        tick: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
        keepalive_s: float = KEEPALIVE_S,
        auto_reset_after: Optional[float] = None,
    ):
        self.backend = backend
        self.tick = tick
        self.clock = clock
        self.rng = rng or random.Random()
        self.keepalive_s = keepalive_s
        self.auto_reset_after = auto_reset_after
        self.state = EngineState(started_at=clock())
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._subs: set[Subscriber] = set()
        self._force_send = False
        self._last_tick: Optional[float] = None
        self._next_retry = 0.0
        self._retry_idx = 0
        self._speed: Optional[SpeedModel] = None
        self._wander_params = WANDER_PRESETS["default"]
        self._wander_after_teleport = False
        self.previous_session: Optional[dict] = None
        self.dirty = False  # session state changed since last persisted

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="locpicker-engine")

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the loop die
                log.exception("engine tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick)
            except asyncio.TimeoutError:
                pass

    async def shutdown(self, clear_timeout: float = 5.0) -> Optional[str]:
        """Stop the loop, restore real GPS, close the connection. Returns an error string or None."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        err = None
        try:
            if not self.backend.connected:
                await asyncio.wait_for(self.backend.connect(), timeout=clear_timeout)
            await asyncio.wait_for(self.backend.clear(), timeout=clear_timeout)
        except Exception as e:
            err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        try:
            await asyncio.wait_for(self.backend.close(), timeout=clear_timeout)
        except Exception:
            pass
        return err

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ pub/sub

    def subscribe(self) -> Subscriber:
        q: Subscriber = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: Subscriber) -> None:
        self._subs.discard(q)

    def publish(self) -> None:
        snap = self.snapshot()
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(snap)

    # ------------------------------------------------------------------ tick

    async def step(self) -> None:
        now = self.clock()
        if not self.backend.connected:
            if now < self._next_retry:
                self.publish()
                return
            if not await self._reconnect(now):
                self.publish()
                return
            self._last_tick = now  # don't jump forward by the outage
        async with self._lock:
            dt = 0.0 if self._last_tick is None else min(MAX_DT, max(0.0, now - self._last_tick))
            self._last_tick = now
            if self._auto_reset_due(now):
                await self._do_clear("auto-reset")
            else:
                moved = self._advance(dt, now)
                await self._maybe_send(now, moved)
        self.publish()

    async def _reconnect(self, now: float) -> bool:
        s = self.state
        try:
            await asyncio.wait_for(self.backend.connect(), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            delay = BACKOFF[min(self._retry_idx, len(BACKOFF) - 1)]
            self._retry_idx += 1
            self._next_retry = now + delay
            s.connection = "no_device" if isinstance(e, DeviceNotFound) else "reconnecting"
            msg = str(e) or type(e).__name__
            if msg != s.last_error:
                log.warning("connect failed: %s (retrying in %ss)", msg, delay)
            s.last_error = msg
            return False
        self._retry_idx = 0
        s.connection = "connected"
        s.last_error = None
        s.device = dict(getattr(self.backend, "info", {}) or {})
        self._force_send = True  # re-send last position immediately
        log.info("device connected")
        return True

    async def _mark_failed(self, e: Exception, now: float) -> None:
        s = self.state
        msg = str(e) or type(e).__name__
        log.warning("device update failed: %s — reconnecting", msg)
        s.last_error = msg
        s.connection = "reconnecting"
        self._force_send = True
        try:
            await asyncio.wait_for(self.backend.close(), timeout=5)
        except Exception:
            pass
        self._next_retry = now + BACKOFF[0]
        self._retry_idx = 1

    async def _maybe_send(self, now: float, moved: bool) -> None:
        s = self.state
        if s.position is None or not self.backend.connected:
            return
        due = s.last_sent_at is None or (now - s.last_sent_at) >= self.keepalive_s
        if not (moved or due or self._force_send):
            return
        try:
            await self.backend.set(s.position[0], s.position[1])
        except Exception as e:
            await self._mark_failed(e, now)
            return
        s.last_sent_at = now
        self._force_send = False
        s.connection = "connected"
        if s.faked_since is None:
            s.faked_since = now

    def _auto_reset_due(self, now: float) -> bool:
        s = self.state
        return (
            self.auto_reset_after is not None
            and s.faked_since is not None
            and s.mode != "idle"
            and now - s.faked_since >= self.auto_reset_after
        )

    # ------------------------------------------------------------------ mode logic

    def _advance(self, dt: float, now: float) -> bool:
        s = self.state
        if s.mode == "travel":
            return self._advance_travel(dt, now)
        if s.mode == "wander":
            return self._advance_wander(dt)
        return False

    def _advance_travel(self, dt: float, now: float) -> bool:
        s = self.state
        assert s.path is not None and s.cum is not None and self._speed is not None
        remaining = s.total_m - s.traveled_m
        v = self._speed.speed(now, dt, s.traveled_m, remaining)
        s.current_speed = v
        s.stop_reason = self._speed.hold_reason
        if dt <= 0:
            return False
        s.traveled_m = min(s.total_m, s.traveled_m + v * dt)
        if s.traveled_m >= s.total_m - 1e-6:
            self._arrive(now)
            return True
        pos, _ = geo.point_at_distance(s.path, s.cum, s.traveled_m)
        s.position = pos
        self.dirty = True
        return v > 0

    def _arrive(self, now: float) -> None:
        s = self.state
        final = s.path[-1]
        s.position = (final[0], final[1])
        s.traveled_m = s.total_m
        s.current_speed = 0.0
        s.stop_reason = None
        log.info("arrived at %.6f, %.6f", *final)
        if s.wander_on_arrival:
            self._enter_wander(final, None, None)
        else:
            s.mode = "static"
        self.dirty = True

    def _advance_wander(self, dt: float) -> bool:
        s = self.state
        if dt <= 0 or s.wander_center is None:
            return False
        p = self._wander_params
        r = s.wander_radius_m
        on, oe = s.wander_offset
        vn, ve = s.wander_vel
        sq = math.sqrt(dt)
        vn += -p.k * on * dt + p.sigma * self.rng.gauss(0, 1) * sq
        ve += -p.k * oe * dt + p.sigma * self.rng.gauss(0, 1) * sq
        sp = math.hypot(vn, ve)
        if sp > p.max_speed:
            vn, ve = vn * p.max_speed / sp, ve * p.max_speed / sp
        on, oe = on + vn * dt, oe + ve * dt
        dist = math.hypot(on, oe)
        if dist > r:
            # Reflect the radial velocity component toward the center, and pull back to the edge.
            un, ue = on / dist, oe / dist
            radial = vn * un + ve * ue
            if radial > 0:
                vn, ve = vn - 2 * radial * un, ve - 2 * radial * ue
            on, oe = un * r, ue * r
        s.wander_vel = (vn, ve)
        s.wander_offset = (on, oe)
        s.current_speed = math.hypot(vn, ve)
        s.position = geo.offset_meters(s.wander_center, on, oe)
        self.dirty = True
        return True

    def _enter_wander(self, center: LatLon, radius: Optional[float], preset: Optional[str]) -> None:
        s = self.state
        preset = preset or s.wander_preset or "default"
        if preset not in WANDER_PRESETS:
            raise EngineError("bad_preset", f"Unknown wander preset {preset!r}. Choose: {', '.join(WANDER_PRESETS)}")
        base = WANDER_PRESETS[preset]
        if radius is None:
            radius = base.radius_m if preset != s.wander_preset or s.mode != "wander" else s.wander_radius_m
        self._wander_params = base
        s.wander_preset = preset
        s.wander_radius_m = float(radius)
        if s.mode != "wander" or s.wander_center is None:
            s.wander_center = (center[0], center[1])
            s.wander_offset = (0.0, 0.0)
            s.wander_vel = (0.0, 0.0)
        s.mode = "wander"
        s.paused_from = None

    def _clear_travel(self) -> None:
        s = self.state
        s.path = s.cum = None
        s.total_m = s.traveled_m = 0.0
        s.current_speed = 0.0
        s.stop_reason = None
        self._speed = None

    # ------------------------------------------------------------------ commands

    def _changed(self) -> dict:
        self._force_send = True
        self.dirty = True
        self.wake()
        return self.snapshot()

    async def teleport(self, lat: float, lon: float, wander: bool = False) -> dict:
        _check_latlon(lat, lon)
        async with self._lock:
            s = self.state
            self._clear_travel()
            s.mode = "static"
            s.paused_from = None
            s.position = (float(lat), float(lon))
            s.target = s.position
            s.wander_center = None
            if wander:
                self._enter_wander(s.position, None, None)
            return self._changed()

    async def start_travel(
        self,
        path: list,
        speed_mps: float,
        profile: Profile = "custom",
        wander_on_arrival: bool = False,
        realistic: bool = True,
        traveled_m: float = 0.0,
    ) -> dict:
        if profile not in PROFILES:
            raise EngineError("bad_profile", "profile must be walk, bike, drive, fly or custom")
        _check_speed(speed_mps)
        pts = validate_path(path)
        async with self._lock:
            s = self.state
            if s.position is not None and geo.haversine(s.position, pts[0]) > 0.5:
                # Always begin where the phone actually is (PLAN §6: prepend current position).
                pts = [s.position] + pts
            pts = geo.dedupe(pts)
            if len(pts) > 5000:
                pts = geo.simplify(pts, 3.0)
            pts = geo.densify(pts, 1000.0 if profile == "fly" else 100.0)  # great-circle legs
            if len(pts) < 2:
                raise EngineError("too_short", "The route is shorter than a few centimeters — already there.")
            cum = geo.cumulative_distances(pts)
            s.path, s.cum, s.total_m = pts, cum, cum[-1]
            s.traveled_m = max(0.0, min(float(traveled_m), s.total_m))
            s.speed_mps = float(speed_mps)
            s.profile = profile
            s.realistic = bool(realistic)
            s.wander_on_arrival = bool(wander_on_arrival)
            s.target = pts[-1]
            s.route_id += 1
            s.mode = "travel"
            s.paused_from = None
            s.wander_center = None
            s.position = geo.point_at_distance(pts, cum, s.traveled_m)[0]
            self._speed = SpeedModel(s.speed_mps, profile, s.realistic, self.rng, s.total_m)
            self._last_tick = self.clock()
            return self._changed()

    async def set_wander(self, enabled: bool, radius_m: Optional[float] = None, preset: Optional[str] = None) -> dict:
        if radius_m is not None:
            _check_radius(radius_m)
        async with self._lock:
            s = self.state
            if preset is not None and preset not in WANDER_PRESETS:
                raise EngineError("bad_preset", f"Unknown wander preset {preset!r}. Choose: {', '.join(WANDER_PRESETS)}")
            if not enabled:
                if s.mode == "wander" or (s.mode == "paused" and s.paused_from == "wander"):
                    s.mode = "static"
                    s.paused_from = None
                    s.wander_center = None
                    s.current_speed = 0.0
                return self._changed()
            if s.position is None:
                raise EngineError("no_position", "Teleport somewhere first, then turn on wander.")
            if s.mode == "travel" or (s.mode == "paused" and s.paused_from == "travel"):
                # During travel, "wander" means wander on arrival.
                s.wander_on_arrival = True
                if preset:
                    s.wander_preset = preset
                    self._wander_params = WANDER_PRESETS[preset]
                if radius_m is not None:
                    s.wander_radius_m = float(radius_m)
                return self._changed()
            if s.mode == "paused":
                s.mode = s.paused_from or "static"
            self._clear_travel()
            self._enter_wander(s.position, radius_m, preset)
            return self._changed()

    async def pause(self) -> dict:
        async with self._lock:
            s = self.state
            if s.mode not in ("travel", "wander"):
                raise EngineError("not_moving", "Nothing to pause — not travelling or wandering.", 409)
            s.paused_from = s.mode
            s.mode = "paused"
            s.current_speed = 0.0
            return self._changed()

    async def resume(self) -> dict:
        async with self._lock:
            s = self.state
            if s.mode != "paused":
                raise EngineError("not_paused", "Not paused.", 409)
            s.mode = s.paused_from or "static"
            s.paused_from = None
            self._last_tick = self.clock()
            return self._changed()

    async def stop(self) -> dict:
        async with self._lock:
            s = self.state
            if s.position is None:
                s.mode = "idle"
            else:
                s.mode = "static"
                s.target = s.position
            s.paused_from = None
            s.wander_center = None
            self._clear_travel()
            return self._changed()

    async def clear(self) -> dict:
        async with self._lock:
            await self._do_clear("user")
            return self.snapshot()

    async def _do_clear(self, why: str) -> None:
        s = self.state
        if not self.backend.connected:
            try:
                await asyncio.wait_for(self.backend.connect(), timeout=CONNECT_TIMEOUT)
                s.connection = "connected"
            except Exception as e:
                s.last_error = str(e) or type(e).__name__
                raise EngineError("device_unreachable",
                                  f"Couldn't reach the phone to reset it: {s.last_error}. "
                                  "Restarting the phone always restores real GPS.", 502)
        try:
            await asyncio.wait_for(self.backend.clear(), timeout=10)
        except Exception as e:
            s.last_error = str(e) or type(e).__name__
            await self._mark_failed(e, self.clock())
            raise EngineError("clear_failed", f"Reset failed: {s.last_error}. Restarting the phone always restores real GPS.", 502)
        log.info("cleared simulated location (%s)", why)
        self._clear_travel()
        s.mode = "idle"
        s.position = s.target = s.wander_center = None
        s.paused_from = None
        s.faked_since = None
        s.last_sent_at = None
        s.last_error = None
        self._force_send = False
        self.dirty = True
        self.wake()

    # ------------------------------------------------------------------ views

    def eta_s(self) -> Optional[float]:
        s = self.state
        if s.path is None:
            return None
        return max(0.0, s.total_m - s.traveled_m) / max(0.1, s.speed_mps)

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        now = self.clock()
        travel = None
        if s.path is not None:
            travel = {
                "route_id": s.route_id,
                "total_m": round(s.total_m, 1),
                "traveled_m": round(s.traveled_m, 1),
                "remaining_m": round(max(0.0, s.total_m - s.traveled_m), 1),
                "progress": (s.traveled_m / s.total_m) if s.total_m > 0 else 1.0,
                "eta_s": round(self.eta_s() or 0.0, 1),
                "speed_mps": s.speed_mps,
                "current_speed": round(s.current_speed, 2),
                "profile": s.profile,
                "realistic": s.realistic,
                "wander_on_arrival": s.wander_on_arrival,
                "stop_reason": s.stop_reason,
                "points": len(s.path),
            }
        wander = None
        if s.wander_center is not None:
            wander = {
                "center": list(s.wander_center),
                "radius_m": s.wander_radius_m,
                "preset": s.wander_preset,
                "speed": round(s.current_speed, 2),
            }
        auto_reset_in = None
        if self.auto_reset_after is not None and s.faked_since is not None and s.mode != "idle":
            auto_reset_in = max(0.0, round(self.auto_reset_after - (now - s.faked_since)))
        return {
            "mode": s.mode,
            "paused_from": s.paused_from,
            "position": list(s.position) if s.position else None,
            "target": list(s.target) if s.target else None,
            "travel": travel,
            "wander": wander,
            "wander_on_arrival": s.wander_on_arrival,
            "connection": s.connection,
            "last_error": s.last_error,
            "device": s.device,
            "auto_reset_in": auto_reset_in,
            "resume_available": self.previous_session is not None,
            "previous_session": _session_summary(self.previous_session),
            "uptime_s": round(now - s.started_at),
        }

    def route(self) -> dict:
        s = self.state
        return {"route_id": s.route_id, "path": [list(p) for p in s.path] if s.path else None}

    # ------------------------------------------------------------------ persistence (data only)

    def export_session(self) -> dict:
        s = self.state
        mode = s.paused_from if s.mode == "paused" else s.mode
        out: dict[str, Any] = {"version": 1, "mode": mode, "position": list(s.position) if s.position else None}
        if mode == "travel" and s.path:
            out["travel"] = {
                "path": [list(p) for p in s.path],
                "traveled_m": s.traveled_m,
                "speed_mps": s.speed_mps,
                "profile": s.profile,
                "realistic": s.realistic,
                "wander_on_arrival": s.wander_on_arrival,
            }
        if mode == "wander" and s.wander_center:
            out["wander"] = {"center": list(s.wander_center), "radius_m": s.wander_radius_m, "preset": s.wander_preset}
        return out

    async def resume_session(self) -> dict:
        sess = self.previous_session
        if not sess:
            raise EngineError("no_session", "No previous session to resume.", 404)
        self.previous_session = None
        mode = sess.get("mode")
        try:
            if mode == "travel" and sess.get("travel"):
                t = sess["travel"]
                full = validate_path(t["path"])
                cum = geo.cumulative_distances(full)
                here, seg = geo.point_at_distance(full, cum, float(t.get("traveled_m", 0.0)))
                rest = [here] + full[seg + 1:]
                await self.teleport(*here)
                if len(geo.dedupe(rest)) < 2:
                    return self.snapshot()
                return await self.start_travel(rest, t["speed_mps"], t.get("profile", "custom"),
                                               t.get("wander_on_arrival", False), t.get("realistic", True))
            if mode == "wander" and sess.get("wander"):
                w = sess["wander"]
                await self.teleport(*sess.get("position") or w["center"])
                async with self._lock:
                    self._enter_wander(tuple(w["center"]), w.get("radius_m"), w.get("preset"))
                    return self._changed()
            if sess.get("position"):
                return await self.teleport(*sess["position"])
        except (KeyError, TypeError, ValueError) as e:
            raise EngineError("bad_session", f"Saved session is unreadable: {e}")
        raise EngineError("no_session", "Saved session had no position.", 404)

    def discard_session(self) -> dict:
        self.previous_session = None
        self.dirty = True
        return self.snapshot()


def _session_summary(sess: Optional[dict]) -> Optional[dict]:
    if not sess:
        return None
    out = {"mode": sess.get("mode"), "position": sess.get("position"), "saved_at": sess.get("saved_at")}
    if sess.get("travel"):
        t = sess["travel"]
        out["remaining_m"] = round(geo.path_length([tuple(p) for p in t["path"]]) - t.get("traveled_m", 0))
    return out


# ---------------------------------------------------------------------- validation helpers


def _num(x: Any, what: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise EngineError("bad_number", f"{what} must be a number")
    return float(x)


def _check_latlon(lat: Any, lon: Any) -> None:
    lat, lon = _num(lat, "lat"), _num(lon, "lon")
    if not -90 <= lat <= 90:
        raise EngineError("bad_lat", "Latitude must be between -90 and 90.")
    if not -180 <= lon <= 180:
        raise EngineError("bad_lon", "Longitude must be between -180 and 180.")


def _check_speed(v: Any) -> None:
    v = _num(v, "speed_mps")
    if not SPEED_MIN <= v <= SPEED_MAX:
        raise EngineError("bad_speed", f"Speed must be {SPEED_MIN}–{SPEED_MAX} m/s.")


def _check_radius(r: Any) -> None:
    r = _num(r, "radius_m")
    if not RADIUS_MIN <= r <= RADIUS_MAX:
        raise EngineError("bad_radius", f"Radius must be {RADIUS_MIN:g}–{RADIUS_MAX:g} m.")


def validate_path(path: Any) -> list[LatLon]:
    if not isinstance(path, list) or not (2 <= len(path) <= PATH_MAX_POINTS):
        raise EngineError("bad_path", f"path must be a list of 2–{PATH_MAX_POINTS} [lat, lon] points.")
    pts: list[LatLon] = []
    for i, p in enumerate(path):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise EngineError("bad_path", f"path[{i}] must be [lat, lon].")
        try:
            _check_latlon(p[0], p[1])
        except EngineError as e:
            raise EngineError("bad_path", f"path[{i}]: {e.message}")
        pts.append((float(p[0]), float(p[1])))
    if geo.crosses_antimeridian(pts):
        raise EngineError("antimeridian", "Routes that cross the ±180° meridian aren't supported.")
    return pts
