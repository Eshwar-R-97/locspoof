import math
import random

import pytest

from locpicker import geo
from locpicker.backends import FakeBackend
from locpicker.engine import Engine, EngineError, WANDER_PRESETS

HOME = (41.8781, -87.6298)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(seed=1, **kw):
    clock = Clock()
    fb = FakeBackend(clock=clock)
    eng = Engine(fb, clock=clock, rng=random.Random(seed), **kw)
    return eng, fb, clock


async def run(eng, clock, ticks, dt=1.0):
    for _ in range(ticks):
        clock.advance(dt)
        await eng.step()


def straight(start, meters, bearing=90.0):
    return [start, geo.destination(start, bearing, meters)]


# ---------------------------------------------------------------- static + keepalive + reconnect


async def test_first_step_connects_and_teleport_sends():
    eng, fb, clock = make()
    await eng.step()
    assert fb.connected and eng.state.connection == "connected"
    await eng.teleport(*HOME)
    await eng.step()
    assert fb.sets == [HOME]
    assert eng.state.mode == "static"


async def test_keepalive_resends_every_10s_in_static():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()
    await run(eng, clock, 30)  # 30 s
    assert len(fb.sets) == 1 + 3
    assert all(p == HOME for p in fb.sets)


async def test_set_failure_reconnects_and_resends_last_position():
    eng, fb, clock = make()
    fb.fail_set_calls = {3}
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()  # set #1
    await run(eng, clock, 10)  # keepalive: set #2
    await run(eng, clock, 10)  # keepalive: set #3 fails
    assert eng.state.connection == "reconnecting"
    assert not fb.connected
    connects_before = fb.connect_count
    await run(eng, clock, 1)  # 1 s backoff elapsed → reconnect → immediate re-send
    assert fb.connect_count == connects_before + 1
    assert eng.state.connection == "connected"
    assert fb.calls[-1][0] == "set" and fb.calls[-1][2] == HOME
    assert fb.set_count == 4


async def test_connect_backoff_grows_to_15s():
    eng, fb, clock = make()
    fb.no_device = True
    attempts = []
    for _ in range(80):
        before = fb.connect_count
        await eng.step()
        if fb.connect_count > before:
            attempts.append(clock.t)
        clock.advance(1)
    gaps = [round(b - a) for a, b in zip(attempts, attempts[1:])]
    assert gaps[:5] == [1, 2, 4, 8, 15]
    assert all(g == 15 for g in gaps[4:])
    assert eng.state.connection == "no_device"


async def test_teleport_while_disconnected_is_sent_on_connect():
    eng, fb, clock = make()
    fb.fail_connects = 1
    await eng.step()
    await eng.teleport(*HOME)
    assert fb.sets == []
    await run(eng, clock, 1)
    assert fb.sets == [HOME]


async def test_invalid_coordinates_never_reach_backend():
    eng, fb, clock = make()
    await eng.step()
    for lat, lon in [(91, 0), (0, 181), (math.nan, 0), ("1", 2), (True, 0)]:
        with pytest.raises(EngineError):
            await eng.teleport(lat, lon)
    await eng.step()
    assert fb.sets == []


async def test_clear_restores_and_goes_idle():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()
    snap = await eng.clear()
    assert snap["mode"] == "idle" and snap["position"] is None
    assert fb.calls[-1][0] == "clear"
    await run(eng, clock, 30)
    assert fb.calls[-1][0] == "clear"  # nothing sent after clearing


async def test_clear_failure_reports_error():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    fb.fail_clear = True
    with pytest.raises(EngineError) as ei:
        await eng.clear()
    assert ei.value.status == 502


async def test_auto_reset_after():
    eng, fb, clock = make(auto_reset_after=60)
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()
    await run(eng, clock, 59)
    assert eng.state.mode == "static"
    await run(eng, clock, 2)
    assert eng.state.mode == "idle"
    assert ("clear" in [c[0] for c in fb.calls])


async def test_shutdown_clears():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()
    err = await eng.shutdown()
    assert err is None
    assert [c[0] for c in fb.calls][-2:] == ["clear", "close"]


# ---------------------------------------------------------------- travel


async def test_travel_1000m_at_10mps_takes_about_100_ticks_and_ends_exactly():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.step()
    path = straight(HOME, 1000)
    await eng.start_travel(path, 10, "custom", realistic=False)
    ticks = 0
    while eng.state.mode == "travel" and ticks < 200:
        clock.advance(1)
        await eng.step()
        ticks += 1
    assert 99 <= ticks <= 101
    assert eng.state.mode == "static"
    assert eng.state.position == path[-1]
    assert fb.sets[-1] == path[-1]


async def test_travel_progress_and_eta():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, "custom", realistic=False)
    await run(eng, clock, 25)
    t = eng.snapshot()["travel"]
    assert t["progress"] == pytest.approx(0.25, abs=0.01)
    assert t["remaining_m"] == pytest.approx(750, abs=5)
    assert t["eta_s"] == pytest.approx(75, abs=1)


async def test_travel_uses_monotonic_deltas_when_loop_lags():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, "custom", realistic=False)
    await run(eng, clock, 10, dt=2.5)
    assert eng.state.traveled_m == pytest.approx(250, abs=0.1)


async def test_travel_prepends_current_position():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    far = geo.destination(HOME, 0, 500)
    await eng.start_travel(straight(far, 300), 5, "walk", realistic=False)
    assert eng.state.path[0] == HOME
    assert eng.state.total_m == pytest.approx(geo.haversine(HOME, far) + 300, rel=0.01)


async def test_travel_pause_resume():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, "custom", realistic=False)
    await run(eng, clock, 10)
    await eng.pause()
    frozen = eng.state.traveled_m
    pos = eng.state.position
    n = len(fb.sets)
    await run(eng, clock, 25)
    assert eng.state.traveled_m == frozen and eng.state.position == pos
    assert len(fb.sets) - n == 3  # one re-assert on pause, then keepalive every 10 s
    await eng.resume()
    await run(eng, clock, 10)
    assert eng.state.traveled_m == pytest.approx(frozen + 100, abs=1)


async def test_travel_arrive_then_wander():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    path = straight(HOME, 50)
    await eng.start_travel(path, 10, "custom", wander_on_arrival=True, realistic=False)
    await run(eng, clock, 6)
    assert eng.state.mode == "wander"
    assert eng.state.wander_center == path[-1]


async def test_travel_too_fast_or_bad_path_rejected():
    eng, fb, clock = make()
    await eng.step()
    with pytest.raises(EngineError):
        await eng.start_travel(straight(HOME, 10), 1001)
    with pytest.raises(EngineError):
        await eng.start_travel([HOME], 5)
    with pytest.raises(EngineError):
        await eng.start_travel([(0, 179.9), (0, -179.9)], 5)
    with pytest.raises(EngineError):
        await eng.start_travel([HOME, (95, 0)], 5)


async def test_travel_without_prior_position_starts_at_path_start():
    eng, fb, clock = make()
    await eng.step()
    path = straight(HOME, 100)
    await eng.start_travel(path, 5, "walk", realistic=False)
    await eng.step()
    assert fb.sets[0] == path[0]


async def test_teleport_during_travel_goes_static():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, realistic=False)
    await run(eng, clock, 5)
    other = geo.destination(HOME, 180, 2000)
    await eng.teleport(*other)
    assert eng.state.mode == "static" and eng.state.path is None


async def test_stop_freezes():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, realistic=False)
    await run(eng, clock, 5)
    pos = eng.state.position
    await eng.stop()
    await run(eng, clock, 20)
    assert eng.state.mode == "static" and eng.state.position == pos


async def test_disconnect_mid_travel_freezes_progress_then_resumes():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 1000), 10, realistic=False)
    await run(eng, clock, 10)
    fb.fail_set_calls = {fb.set_count + 1}
    fb.fail_connects = 2
    await run(eng, clock, 1)
    at_fail = eng.state.traveled_m
    await run(eng, clock, 5)  # still down
    assert eng.state.traveled_m == at_fail
    await run(eng, clock, 10)
    assert eng.state.connection == "connected"
    assert eng.state.traveled_m > at_fail


# ---------------------------------------------------------------- realism


async def test_realistic_drive_varies_speed_but_finishes():
    eng, fb, clock = make(seed=7)
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 3000), 11, "drive", realistic=True)
    speeds, stops, ticks = [], 0, 0
    while eng.state.mode == "travel" and ticks < 2000:
        clock.advance(1)
        await eng.step()
        ticks += 1
        speeds.append(eng.state.current_speed)
        if eng.state.stop_reason == "traffic":
            stops += 1
    assert eng.state.mode == "static"
    assert eng.state.position == eng.state.target
    moving = [s for s in speeds[:200] if s > 5]
    assert max(moving) <= 11 * 1.1 + 1e-9
    assert len(set(round(s, 3) for s in moving)) > 10  # not constant
    assert stops >= 10  # at least one 10–40 s traffic stop in 3 km
    # deceleration in last 150 m
    assert speeds[-2] < 11 * 0.8


async def test_speed_drift_is_smooth():
    eng, fb, clock = make(seed=3)
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 5000), 5, "bike", realistic=True)
    speeds = []
    for _ in range(200):
        clock.advance(1)
        await eng.step()
        speeds.append(eng.state.current_speed)
    jumps = [abs(b - a) for a, b in zip(speeds, speeds[1:])]
    assert max(jumps) < 0.5  # drift, not per-tick noise
    assert all(4.5 - 1e-9 <= s <= 5.5 + 1e-9 for s in speeds)


async def test_walk_has_occasional_pauses():
    eng, fb, clock = make(seed=11)
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 5000), 1.4, "walk", realistic=True)
    paused = 0
    for _ in range(2000):
        clock.advance(1)
        await eng.step()
        if eng.state.stop_reason == "pause":
            paused += 1
        else:
            assert 1.2 - 1e-9 <= eng.state.current_speed <= 1.6 + 1e-9
    assert paused > 0


# ---------------------------------------------------------------- wander


@pytest.mark.parametrize("preset", list(WANDER_PRESETS))
async def test_wander_stays_in_radius_and_under_speed(preset):
    eng, fb, clock = make(seed=42)
    await eng.step()
    await eng.teleport(*HOME)
    await eng.set_wander(True, preset=preset)
    p = WANDER_PRESETS[preset]
    r = eng.state.wander_radius_m
    assert r == p.radius_m
    max_d, total = 0.0, 0.0
    prev = eng.state.position
    for _ in range(10_000):
        clock.advance(1)
        await eng.step()
        pos = eng.state.position
        max_d = max(max_d, geo.haversine(HOME, pos))
        total += geo.haversine(prev, pos)
        prev = pos
    assert max_d <= r * 1.1
    assert total / 10_000 <= p.max_speed + 1e-6
    assert total > 0


async def test_wander_toggle_and_teleport_with_wander():
    eng, fb, clock = make()
    await eng.step()
    with pytest.raises(EngineError):
        await eng.set_wander(True)
    snap = await eng.teleport(*HOME, wander=True)
    assert snap["mode"] == "wander" and snap["wander"]["center"] == list(HOME)
    await eng.set_wander(True, radius_m=100)
    assert eng.state.wander_radius_m == 100
    await eng.set_wander(False)
    assert eng.state.mode == "static"
    with pytest.raises(EngineError):
        await eng.set_wander(True, radius_m=1)
    with pytest.raises(EngineError):
        await eng.set_wander(True, preset="nope")


async def test_wander_during_travel_means_on_arrival():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    await eng.start_travel(straight(HOME, 30), 10, realistic=False)
    await eng.set_wander(True, preset="drift")
    assert eng.state.mode == "travel" and eng.state.wander_on_arrival
    await run(eng, clock, 5)
    assert eng.state.mode == "wander" and eng.state.wander_preset == "drift"


async def test_pause_requires_motion():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    with pytest.raises(EngineError):
        await eng.pause()
    with pytest.raises(EngineError):
        await eng.resume()


# ---------------------------------------------------------------- session persistence


async def test_session_export_and_resume_travel():
    eng, fb, clock = make()
    await eng.step()
    await eng.teleport(*HOME)
    path = straight(HOME, 1000)
    await eng.start_travel(path, 10, "custom", realistic=False)
    await run(eng, clock, 30)
    sess = eng.export_session()
    assert sess["mode"] == "travel"

    eng2, fb2, clock2 = make()
    eng2.previous_session = sess
    assert eng2.snapshot()["resume_available"]
    snap = await eng2.resume_session()
    assert snap["mode"] == "travel"
    assert snap["travel"]["total_m"] == pytest.approx(700, abs=2)
    assert geo.haversine(tuple(snap["position"]), geo.destination(HOME, 90, 300)) < 2
    assert not eng2.snapshot()["resume_available"]


async def test_snapshot_subscribers_get_updates():
    eng, fb, clock = make()
    q = eng.subscribe()
    await eng.step()
    snap = q.get_nowait()
    assert snap["connection"] == "connected"
    eng.unsubscribe(q)


async def test_fly_profile_ramps_up_and_down_and_arrives():
    eng, fb, clock = make()
    await eng.step()
    far = geo.destination(HOME, 45, 200_000)  # 200 km hop
    await eng.start_travel([HOME, far], 250, "fly", realistic=True)
    assert eng.state.path is not None and len(eng.state.path) <= 202  # 1 km great-circle legs
    speeds = []
    for _ in range(2000):
        clock.advance(1.0)
        await eng.step()
        speeds.append(eng.snapshot()["travel"]["current_speed"] if eng.state.mode == "travel" else None)
        if eng.state.mode != "travel":
            break
    assert eng.state.mode != "travel"  # arrived
    moving = [v for v in speeds if v is not None]
    assert moving[0] < 50  # take-off roll, not instant cruise
    assert max(moving) == pytest.approx(250, rel=0.05)
    assert moving[-1] < 100  # landing
    assert geo.haversine(eng.state.position, far) < 50


async def test_fly_at_1000_mps_unrealistic_is_constant():
    eng, fb, clock = make()
    await eng.step()
    await eng.start_travel([HOME, geo.destination(HOME, 90, 50_000)], 1000, "fly", realistic=False)
    clock.advance(1.0)
    await eng.step()
    assert eng.snapshot()["travel"]["current_speed"] == 1000
