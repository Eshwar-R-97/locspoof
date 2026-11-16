import asyncio
import json
import random

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from locpicker import geo
from locpicker.backends import FakeBackend
from locpicker.config import Config, parse_args, parse_duration
from locpicker.engine import Engine
from locpicker.server import create_app
from locpicker.spots import SpotStore

HOME = (41.8781, -87.6298)


async def _client(tmp_path, token=None, host="127.0.0.1"):
    cfg = Config(data_dir=tmp_path, token=token, host=host, port=8765)
    fb = FakeBackend()
    eng = Engine(fb, tick=0.05, rng=random.Random(1))
    spots = SpotStore(tmp_path / "spots.json")
    app = create_app(eng, spots, cfg)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, eng, fb


@pytest.fixture
async def ctx(tmp_path):
    client, eng, fb = await _client(tmp_path)
    yield client, eng, fb
    await client.close()


H = {"Host": "127.0.0.1:8765"}


def hdr(**extra):
    return {**H, **extra}


async def wait_for(pred, timeout=2.0):
    t = 0.0
    while not pred():
        await asyncio.sleep(0.02)
        t += 0.02
        if t > timeout:
            raise AssertionError("timed out")


async def test_index_served(ctx):
    client, *_ = ctx
    r = await client.get("/", headers=H)
    assert r.status == 200
    assert "<html" in (await r.text()).lower()


async def test_status_and_teleport_reach_device(ctx):
    client, eng, fb = ctx
    r = await client.get("/api/status", headers=H)
    assert r.status == 200
    r = await client.post("/api/teleport", json={"lat": HOME[0], "lon": HOME[1]}, headers=H)
    assert r.status == 200
    body = await r.json()
    assert body["mode"] == "static" and body["position"] == list(HOME)
    await wait_for(lambda: HOME in fb.sets)


@pytest.mark.parametrize("body", [
    {"lat": 91, "lon": 0}, {"lat": 0, "lon": -181}, {"lat": "1", "lon": 2}, {"lon": 2}, {"lat": 1, "lon": 2, "wander": "y"},
])
async def test_teleport_validation(ctx, body):
    client, eng, fb = ctx
    r = await client.post("/api/teleport", json=body, headers=H)
    assert r.status == 400
    j = await r.json()
    assert j["ok"] is False and j["error"] and j["message"]
    await asyncio.sleep(0.1)
    assert fb.sets == []


async def test_bad_json(ctx):
    client, *_ = ctx
    r = await client.post("/api/teleport", data="{nope", headers=hdr(**{"Content-Type": "application/json"}))
    assert r.status == 400 and (await r.json())["error"] == "bad_json"


async def test_unknown_api_404_json(ctx):
    client, *_ = ctx
    r = await client.get("/api/nope", headers=H)
    assert r.status == 404 and (await r.json())["ok"] is False


async def test_travel_flow(ctx):
    client, eng, fb = ctx
    await client.post("/api/teleport", json={"lat": HOME[0], "lon": HOME[1]}, headers=H)
    path = [list(HOME), list(geo.destination(HOME, 90, 500))]
    r = await client.post("/api/travel", json={"path": path, "speed_mps": 10, "profile": "drive",
                                                "wander_on_arrival": True}, headers=H)
    assert r.status == 200
    snap = await r.json()
    assert snap["mode"] == "travel" and snap["travel"]["total_m"] == pytest.approx(500, abs=1)
    r = await client.get("/api/route", headers=H)
    assert len((await r.json())["path"]) >= 2
    r = await client.post("/api/travel/pause", headers=H)
    assert (await r.json())["mode"] == "paused"
    r = await client.post("/api/travel/pause", headers=H)
    assert r.status == 409
    r = await client.post("/api/travel/resume", headers=H)
    assert (await r.json())["mode"] == "travel"
    r = await client.post("/api/stop", headers=H)
    assert (await r.json())["mode"] == "static"


@pytest.mark.parametrize("body", [
    {"path": [[0, 0]], "speed_mps": 5},
    {"path": [[0, 0], [0, 1]], "speed_mps": 0},
    {"path": [[0, 0], [0, 1]], "speed_mps": 61},
    {"path": [[0, 0], [100, 1]], "speed_mps": 5},
    {"path": [[0, 0], [0, 1]], "speed_mps": 5, "profile": "rocket"},
    {"path": [[0, 179.9], [0, -179.9]], "speed_mps": 5},
    {"path": "nope", "speed_mps": 5},
])
async def test_travel_validation(ctx, body):
    client, *_ = ctx
    r = await client.post("/api/travel", json=body, headers=H)
    assert r.status == 400


async def test_travel_too_many_points(ctx):
    client, *_ = ctx
    path = [[0, i * 1e-5] for i in range(20_001)]
    r = await client.post("/api/travel", json={"path": path, "speed_mps": 5}, headers=H)
    assert r.status == 400


async def test_wander_and_clear(ctx):
    client, eng, fb = ctx
    r = await client.post("/api/wander", json={"enabled": True}, headers=H)
    assert r.status == 400  # no position yet
    await client.post("/api/teleport", json={"lat": HOME[0], "lon": HOME[1]}, headers=H)
    r = await client.post("/api/wander", json={"enabled": True, "radius_m": 1}, headers=H)
    assert r.status == 400
    r = await client.post("/api/wander", json={"enabled": True, "preset": "indoors"}, headers=H)
    j = await r.json()
    assert j["mode"] == "wander" and j["wander"]["radius_m"] == 10
    r = await client.post("/api/clear", headers=H)
    j = await r.json()
    assert j["mode"] == "idle" and j["position"] is None
    assert any(c[0] == "clear" for c in fb.calls)


async def test_sse_stream_sends_updates(ctx):
    client, eng, fb = ctx
    r = await client.get("/api/events", headers=H)
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    seen = []

    async def read():
        async for line in r.content:
            line = line.decode().strip()
            if line.startswith("data: "):
                seen.append(json.loads(line[6:]))
                if any(s["mode"] == "static" for s in seen):
                    return

    await client.post("/api/teleport", json={"lat": HOME[0], "lon": HOME[1]}, headers=H)
    await asyncio.wait_for(read(), 3)
    assert len(seen) >= 1
    r.close()


async def test_spots_crud(ctx):
    client, *_ = ctx
    r = await client.post("/api/spots", json={"name": "Home", "lat": 41.87, "lon": -87.62, "emoji": "🏠"}, headers=H)
    assert r.status == 201
    home = await r.json()
    r = await client.post("/api/spots", json={"name": "HOME", "lat": 1, "lon": 1}, headers=H)
    assert r.status == 409
    r = await client.post("/api/spots", json={"name": "Work", "lat": 41.88, "lon": -87.63}, headers=H)
    work = await r.json()
    r = await client.patch(f"/api/spots/{home['id']}", json={"name": "House"}, headers=H)
    assert (await r.json())["name"] == "House"
    r = await client.post("/api/spots/reorder", json={"ids": [work["id"], home["id"]]}, headers=H)
    assert [s["name"] for s in (await r.json())["spots"]] == ["Work", "House"]
    r = await client.get("/api/spots/export", headers=H)
    exported = await r.json()
    assert len(exported["spots"]) == 2
    r = await client.delete(f"/api/spots/{work['id']}", headers=H)
    assert (await r.json())["ok"]
    r = await client.delete(f"/api/spots/{work['id']}", headers=H)
    assert r.status == 404
    # import as JSON body
    r = await client.post("/api/spots/import", json=exported, headers=H)
    j = await r.json()
    assert j["added"] == 1 and j["skipped"] == 1
    # import as file upload, replace mode
    fd = FormData()
    fd.add_field("file", json.dumps({"spots": [{"name": "Gym", "lat": 1, "lon": 2}]}).encode(),
                 filename="s.json", content_type="application/json")
    r = await client.post("/api/spots/import?mode=replace", data=fd, headers=H)
    j = await r.json()
    assert [s["name"] for s in j["spots"]] == ["Gym"]
    r = await client.get("/api/spots", headers=H)
    assert len((await r.json())["spots"]) == 1


# ------------------------------------------------------------------ security


async def test_cross_origin_post_rejected(ctx):
    client, eng, fb = ctx
    r = await client.post("/api/teleport", json={"lat": 1, "lon": 2},
                          headers=hdr(Origin="https://evil.example"))
    assert r.status == 403
    r = await client.post("/api/teleport", json={"lat": 1, "lon": 2}, headers=hdr(Origin="null"))
    assert r.status == 403
    r = await client.post("/api/teleport", json={"lat": 1, "lon": 2}, headers=hdr(**{"Sec-Fetch-Site": "cross-site"}))
    assert r.status == 403
    r = await client.post("/api/teleport", json={"lat": 1, "lon": 2}, headers=hdr(Origin="http://127.0.0.1:8765"))
    assert r.status == 200


async def test_dns_rebinding_host_rejected(ctx):
    client, *_ = ctx
    r = await client.get("/api/status", headers={"Host": "attacker.example:8765"})
    assert r.status == 403
    r = await client.get("/api/status", headers={"Host": "localhost:8765"})
    assert r.status == 200


async def test_token_required(tmp_path):
    client, eng, fb = await _client(tmp_path, token="s3cret-token", host="0.0.0.0")
    try:
        r = await client.get("/api/status")
        assert r.status == 401
        r = await client.get("/api/status", headers={"Authorization": "Bearer wrong-token"})
        assert r.status == 401
        r = await client.get("/api/status", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status == 200
        r = await client.get("/api/events?token=s3cret-token")
        assert r.status == 200
        r.close()
        r = await client.post("/api/teleport?token=s3cret-token", json={"lat": 1, "lon": 2})
        assert r.status == 401  # query token only for SSE/download
        r = await client.get("/")
        assert r.status == 200  # page itself carries no data
    finally:
        await client.close()


async def test_session_persisted_and_offered(tmp_path):
    client, eng, fb = await _client(tmp_path)
    await client.post("/api/teleport", json={"lat": HOME[0], "lon": HOME[1]}, headers=H)
    await client.close()  # cleanup saves state, then clears the phone
    assert (tmp_path / "state.json").exists()
    assert any(c[0] == "clear" for c in fb.calls)
    client2, eng2, fb2 = await _client(tmp_path)
    try:
        r = await client2.get("/api/status", headers=H)
        j = await r.json()
        assert j["resume_available"] and j["mode"] == "idle"  # never moves the phone by itself
        r = await client2.post("/api/session/resume", headers=H)
        j = await r.json()
        assert j["mode"] == "static" and j["position"] == list(HOME)
    finally:
        await client2.close()


# ------------------------------------------------------------------ config


def test_parse_duration():
    assert parse_duration("4h") == 14400
    assert parse_duration("1h30m") == 5400
    assert parse_duration("90s") == 90
    assert parse_duration("45") == 45
    for bad in ("", "abc", "4x", "0"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_cli_requires_token_for_lan():
    with pytest.raises(SystemExit):
        parse_args(["--host", "0.0.0.0"])
    cfg = parse_args(["--host", "0.0.0.0", "--token", "abcdefgh", "--auto-reset-after", "4h", "--dry-run"])
    assert cfg.token == "abcdefgh" and cfg.auto_reset_after == 14400 and cfg.dry_run
