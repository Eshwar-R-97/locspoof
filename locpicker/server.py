"""aiohttp routes, SSE, static files, auth/CSRF, session persistence."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from aiohttp import web

from .config import Config
from .engine import Engine, EngineError
from .spots import SpotError, SpotStore, atomic_write_json

log = logging.getLogger("locpicker.server")

STATIC_DIR = Path(__file__).parent / "static"
ENGINE_KEY = web.AppKey("engine", Engine)
SPOTS_KEY = web.AppKey("spots", SpotStore)
CONFIG_KEY = web.AppKey("config", Config)
TASKS_KEY = web.AppKey("tasks", list)
SESSION_SAVE_S = 5.0
SSE_HEARTBEAT_S = 15.0


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _err(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": code, "message": message}, status=status)


# ------------------------------------------------------------------------ middlewares


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except (EngineError, SpotError, ApiError) as e:
        return _err(e.status, e.code, e.message)
    except web.HTTPException as e:
        if request.path.startswith("/api/"):
            return _err(e.status, e.reason.lower().replace(" ", "_"), e.text or e.reason)
        raise
    except Exception as e:  # pragma: no cover
        log.exception("unhandled error on %s %s", request.method, request.path)
        return _err(500, "internal", f"Internal error: {type(e).__name__}")


def _allowed_hosts(cfg: Config) -> Optional[set[str]]:
    if not cfg.is_loopback:
        return None  # LAN mode is protected by the token
    p = cfg.port
    return {f"127.0.0.1:{p}", f"localhost:{p}", f"[::1]:{p}"}


def security_middleware(cfg: Config):
    hosts = _allowed_hosts(cfg)

    @web.middleware
    async def mw(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        # DNS-rebinding guard: a hostile page that rebinds its domain to 127.0.0.1 would send its own Host.
        if hosts is not None and request.host not in hosts:
            return _err(403, "bad_host", "Requests must be addressed to localhost.")
        # CSRF guard: any web page can POST to localhost; only our own origin may.
        origin = request.headers.get("Origin")
        if origin is not None:
            o = urlsplit(origin)
            if origin == "null" or o.netloc != request.host:
                return _err(403, "bad_origin", "Cross-origin requests are not allowed.")
        elif request.method not in ("GET", "HEAD", "OPTIONS"):
            # Browsers always send Origin on cross-site POSTs; a fetch from our own page does too.
            # Requests without one must at least not come from another site.
            sfs = request.headers.get("Sec-Fetch-Site")
            if sfs is not None and sfs not in ("same-origin", "none"):
                return _err(403, "bad_origin", "Cross-site requests are not allowed.")
        if cfg.token:
            supplied = None
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:].strip()
            elif request.path == "/api/events" or request.path == "/api/spots/export":
                supplied = request.query.get("token")  # EventSource / download links can't set headers
            if not supplied or not hmac.compare_digest(supplied.encode(), cfg.token.encode()):
                return _err(401, "unauthorized", "Missing or wrong token.")
        return await handler(request)

    return mw


# ------------------------------------------------------------------------ helpers


async def _json(request: web.Request) -> dict:
    if request.content_length == 0 or not request.body_exists:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError("bad_json", "Body must be valid JSON.")
    if not isinstance(data, dict):
        raise ApiError("bad_json", "Body must be a JSON object.")
    return data


def _req(d: dict, key: str) -> Any:
    if key not in d:
        raise ApiError("missing_field", f"Missing field: {key}")
    return d[key]


def _bool(d: dict, key: str, default: bool = False) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ApiError("bad_field", f"{key} must be true or false")
    return v


def _opt_num(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError("bad_field", f"{key} must be a number")
    return float(v)


# ------------------------------------------------------------------------ handlers


async def index(request: web.Request) -> web.StreamResponse:
    resp = web.FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def status(request):
    return web.json_response(request.app[ENGINE_KEY].snapshot())


async def route(request):
    return web.json_response(request.app[ENGINE_KEY].route())


async def teleport(request):
    d = await _json(request)
    snap = await request.app[ENGINE_KEY].teleport(_req(d, "lat"), _req(d, "lon"), _bool(d, "wander"))
    return web.json_response(snap)


async def travel(request):
    d = await _json(request)
    snap = await request.app[ENGINE_KEY].start_travel(
        _req(d, "path"),
        _req(d, "speed_mps"),
        d.get("profile", "custom"),
        _bool(d, "wander_on_arrival"),
        _bool(d, "realistic", True),
    )
    return web.json_response(snap)


async def travel_pause(request):
    return web.json_response(await request.app[ENGINE_KEY].pause())


async def travel_resume(request):
    return web.json_response(await request.app[ENGINE_KEY].resume())


async def wander(request):
    d = await _json(request)
    preset = d.get("preset")
    if preset is not None and not isinstance(preset, str):
        raise ApiError("bad_field", "preset must be a string")
    snap = await request.app[ENGINE_KEY].set_wander(_bool(d, "enabled", True), _opt_num(d, "radius_m"), preset)
    return web.json_response(snap)


async def stop(request):
    return web.json_response(await request.app[ENGINE_KEY].stop())


async def clear(request):
    return web.json_response(await request.app[ENGINE_KEY].clear())


async def session_resume(request):
    return web.json_response(await request.app[ENGINE_KEY].resume_session())


async def session_discard(request):
    eng = request.app[ENGINE_KEY]
    snap = eng.discard_session()
    _save_session(request.app, force=True)
    return web.json_response(snap)


async def events(request: web.Request) -> web.StreamResponse:
    eng = request.app[ENGINE_KEY]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = eng.subscribe()
    try:
        await resp.write(f"retry: 2000\ndata: {json.dumps(eng.snapshot())}\n\n".encode())
        while True:
            try:
                snap = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_S)
                await resp.write(f"data: {json.dumps(snap)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        eng.unsubscribe(q)
    return resp


async def spots_list(request):
    st = request.app[SPOTS_KEY]
    return web.json_response({"spots": st.list(), "warning": st.warning})


async def spots_create(request):
    d = await _json(request)
    spot = await request.app[SPOTS_KEY].create(d.get("name"), d.get("lat"), d.get("lon"), d.get("emoji"))
    return web.json_response(spot, status=201)


async def spots_update(request):
    d = await _json(request)
    return web.json_response(await request.app[SPOTS_KEY].update(request.match_info["id"], d))


async def spots_delete(request):
    await request.app[SPOTS_KEY].delete(request.match_info["id"])
    return web.json_response({"ok": True})


async def spots_reorder(request):
    d = await _json(request)
    return web.json_response({"spots": await request.app[SPOTS_KEY].reorder(_req(d, "ids"))})


async def spots_export(request):
    data = request.app[SPOTS_KEY].export()
    return web.json_response(data, headers={
        "Content-Disposition": 'attachment; filename="spots-export.json"'})


async def spots_import(request: web.Request):
    replace = request.query.get("mode") == "replace"
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        part = await reader.next()
        while part is not None and getattr(part, "name", None) != "file":
            part = await reader.next()
        if part is None:
            raise ApiError("missing_file", "Upload a JSON file in the 'file' field.")
        raw = await part.read(decode=True)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("bad_json", "The uploaded file is not valid JSON.")
    else:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("bad_json", "Body must be valid JSON.")
    res = await request.app[SPOTS_KEY].import_(data, replace=replace)
    return web.json_response({"ok": True, **res, "spots": request.app[SPOTS_KEY].list()})


async def api_not_found(request):
    return _err(404, "not_found", f"No such endpoint: {request.method} {request.path}")


# ------------------------------------------------------------------------ session persistence


def _session_path(app) -> Path:
    return app[CONFIG_KEY].data_dir / "state.json"


def _save_session(app, force: bool = False) -> None:
    eng = app[ENGINE_KEY]
    if not (eng.dirty or force):
        return
    eng.dirty = False
    data = eng.export_session()
    if eng.previous_session is not None and data.get("mode") == "idle":
        return  # don't overwrite the resumable session until the user resumes or discards it
    data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        atomic_write_json(_session_path(app), data)
    except OSError as e:
        log.warning("couldn't save session state: %s", e)


def load_previous_session(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("mode") in (None, "idle") or not data.get("position"):
        return None
    return data


async def _session_saver(app):
    while True:
        await asyncio.sleep(SESSION_SAVE_S)
        _save_session(app)


# ------------------------------------------------------------------------ app


def create_app(engine: Engine, spots: SpotStore, cfg: Config, *, start_engine: bool = True,
               clear_on_shutdown: bool = True) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, security_middleware(cfg)],
        client_max_size=8 * 1024 * 1024,
    )
    app[ENGINE_KEY] = engine
    app[SPOTS_KEY] = spots
    app[CONFIG_KEY] = cfg
    app[TASKS_KEY] = []

    async def revalidate_static(request, response):
        # Make browsers re-check app.js/app.css on every load so updates never mix old and new files.
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(revalidate_static)

    r = app.router
    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_get("/api/route", route)
    r.add_get("/api/events", events)
    r.add_post("/api/teleport", teleport)
    r.add_post("/api/travel", travel)
    r.add_post("/api/travel/pause", travel_pause)
    r.add_post("/api/travel/resume", travel_resume)
    r.add_post("/api/wander", wander)
    r.add_post("/api/stop", stop)
    r.add_post("/api/clear", clear)
    r.add_post("/api/session/resume", session_resume)
    r.add_post("/api/session/discard", session_discard)
    r.add_get("/api/spots", spots_list)
    r.add_post("/api/spots", spots_create)
    r.add_get("/api/spots/export", spots_export)
    r.add_post("/api/spots/import", spots_import)
    r.add_post("/api/spots/reorder", spots_reorder)
    r.add_patch("/api/spots/{id}", spots_update)
    r.add_delete("/api/spots/{id}", spots_delete)
    r.add_route("*", "/api/{tail:.*}", api_not_found)
    r.add_static("/static/", STATIC_DIR, append_version=True)

    async def on_startup(app):
        if engine.previous_session is None:
            engine.previous_session = load_previous_session(_session_path(app))
        if start_engine:
            engine.start()
        app[TASKS_KEY].append(asyncio.create_task(_session_saver(app)))

    async def on_cleanup(app):
        for t in app[TASKS_KEY]:
            t.cancel()
        _save_session(app, force=True)  # before clearing, so "Resume previous session?" works next time
        if clear_on_shutdown:
            err = await engine.shutdown()
            if err:
                print(f"\n⚠️  Could not restore real location: {err}\n   Restarting the phone always restores real GPS.")
            else:
                print("\nRestored real location.")

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
