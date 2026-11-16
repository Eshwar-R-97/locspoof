"""Saved-spot storage: ``data/spots.json`` with atomic writes and corrupt-file recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("locpicker.spots")

NAME_MAX = 40
EMOJI_MAX = 8
MAX_SPOTS = 1000


class SpotError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def atomic_write_json(path: Path, data: Any) -> None:
    """Write to ``<name>.tmp``, fsync, then ``os.replace`` — a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    try:  # make the rename durable
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise SpotError("bad_name", "Name must be text.")
    name = " ".join(name.split())
    if not 1 <= len(name) <= NAME_MAX:
        raise SpotError("bad_name", f"Name must be 1–{NAME_MAX} characters.")
    return name


def _clean_coord(v: Any, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise SpotError(f"bad_{what}", f"{what} must be a number between {lo:g} and {hi:g}.")
    return float(v)


def _clean_emoji(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v) > EMOJI_MAX:
        raise SpotError("bad_emoji", f"Emoji must be at most {EMOJI_MAX} characters.")
    return v.strip() or None


class SpotStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.warning: Optional[str] = None
        self._spots: list[dict] = self._load()

    # ---------------------------------------------------------------- load / save

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("spots"), list):
                raise ValueError("missing 'spots' list")
            spots = [self._validate_loaded(s) for s in raw["spots"]]
            self._check_unique(spots)
            spots.sort(key=lambda s: s["order"])
            return spots
        except (ValueError, SpotError, TypeError, KeyError, UnicodeDecodeError) as e:
            backup = self.path.with_name(f"{self.path.name}.bak-{int(time.time())}")
            os.replace(self.path, backup)
            self.warning = f"spots.json was unreadable ({e}); backed up to {backup.name} and started fresh."
            log.warning(self.warning)
            return []

    @staticmethod
    def _validate_loaded(s: Any) -> dict:
        if not isinstance(s, dict):
            raise ValueError("spot entry is not an object")
        return {
            "id": str(s["id"]),
            "name": _clean_name(s["name"]),
            "lat": _clean_coord(s["lat"], -90, 90, "lat"),
            "lon": _clean_coord(s["lon"], -180, 180, "lon"),
            "emoji": _clean_emoji(s.get("emoji")),
            "created": str(s.get("created") or _now_iso()),
            "order": int(s.get("order", 0)),
        }

    @staticmethod
    def _check_unique(spots: list[dict]) -> None:
        seen: set[str] = set()
        for s in spots:
            k = s["name"].casefold()
            if k in seen:
                raise SpotError("duplicate_name", f"Duplicate spot name {s['name']!r}.", 409)
            seen.add(k)

    def _renumber(self) -> None:
        for i, s in enumerate(self._spots):
            s["order"] = i

    async def _save(self) -> None:
        self._renumber()
        data = {"version": 1, "spots": self._spots}
        await asyncio.to_thread(atomic_write_json, self.path, data)

    # ---------------------------------------------------------------- queries

    def list(self) -> list[dict]:
        return [dict(s) for s in self._spots]

    def _find(self, spot_id: str) -> dict:
        for s in self._spots:
            if s["id"] == spot_id:
                return s
        raise SpotError("not_found", "No such spot.", 404)

    def _name_taken(self, name: str, except_id: Optional[str] = None) -> bool:
        k = name.casefold()
        return any(s["name"].casefold() == k and s["id"] != except_id for s in self._spots)

    # ---------------------------------------------------------------- mutations

    async def create(self, name: Any, lat: Any, lon: Any, emoji: Any = None) -> dict:
        spot = {
            "id": uuid.uuid4().hex,
            "name": _clean_name(name),
            "lat": _clean_coord(lat, -90, 90, "lat"),
            "lon": _clean_coord(lon, -180, 180, "lon"),
            "emoji": _clean_emoji(emoji),
            "created": _now_iso(),
            "order": len(self._spots),
        }
        async with self._lock:
            if len(self._spots) >= MAX_SPOTS:
                raise SpotError("too_many", f"At most {MAX_SPOTS} spots.")
            if self._name_taken(spot["name"]):
                raise SpotError("duplicate_name", f"A spot named {spot['name']!r} already exists.", 409)
            self._spots.append(spot)
            try:
                await self._save()
            except BaseException:
                self._spots.remove(spot)
                raise
            return dict(spot)

    async def update(self, spot_id: str, fields: dict) -> dict:
        allowed = {"name", "lat", "lon", "emoji", "order"}
        unknown = set(fields) - allowed
        if unknown:
            raise SpotError("bad_field", f"Unknown field(s): {', '.join(sorted(unknown))}")
        async with self._lock:
            s = self._find(spot_id)
            new = dict(s)
            if "name" in fields:
                new["name"] = _clean_name(fields["name"])
                if self._name_taken(new["name"], spot_id):
                    raise SpotError("duplicate_name", f"A spot named {new['name']!r} already exists.", 409)
            if "lat" in fields:
                new["lat"] = _clean_coord(fields["lat"], -90, 90, "lat")
            if "lon" in fields:
                new["lon"] = _clean_coord(fields["lon"], -180, 180, "lon")
            if "emoji" in fields:
                new["emoji"] = _clean_emoji(fields["emoji"])
            old_list = [dict(x) for x in self._spots]
            idx = self._spots.index(s)
            self._spots[idx] = new
            if "order" in fields:
                o = fields["order"]
                if isinstance(o, bool) or not isinstance(o, int):
                    raise SpotError("bad_order", "order must be an integer.")
                self._spots.pop(idx)
                self._spots.insert(max(0, min(o, len(self._spots))), new)
            try:
                await self._save()
            except BaseException:
                self._spots = old_list
                raise
            return dict(new)

    async def delete(self, spot_id: str) -> None:
        async with self._lock:
            s = self._find(spot_id)
            old = list(self._spots)
            self._spots.remove(s)
            try:
                await self._save()
            except BaseException:
                self._spots = old
                raise

    async def reorder(self, ids: list) -> list[dict]:
        async with self._lock:
            if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(s["id"] for s in self._spots):
                raise SpotError("bad_order", "ids must list every spot id exactly once.")
            by_id = {s["id"]: s for s in self._spots}
            old = list(self._spots)
            self._spots = [by_id[str(i)] for i in ids]
            try:
                await self._save()
            except BaseException:
                self._spots = old
                raise
            return self.list()

    def export(self) -> dict:
        return {"version": 1, "exported": _now_iso(), "spots": self.list()}

    async def import_(self, data: Any, replace: bool = False) -> dict:
        """Merge (default) or replace. Merge skips names that already exist."""
        if isinstance(data, dict):
            items = data.get("spots")
        else:
            items = data
        if not isinstance(items, list):
            raise SpotError("bad_import", "Expected {\"spots\": [...]} or a list of spots.")
        cleaned = []
        for i, s in enumerate(items):
            if not isinstance(s, dict):
                raise SpotError("bad_import", f"spots[{i}] is not an object.")
            try:
                cleaned.append({
                    "name": _clean_name(s.get("name")),
                    "lat": _clean_coord(s.get("lat"), -90, 90, "lat"),
                    "lon": _clean_coord(s.get("lon"), -180, 180, "lon"),
                    "emoji": _clean_emoji(s.get("emoji")),
                    "created": str(s.get("created") or _now_iso()),
                })
            except SpotError as e:
                raise SpotError("bad_import", f"spots[{i}]: {e.message}")
        async with self._lock:
            old = list(self._spots)
            base = [] if replace else list(self._spots)
            names = {s["name"].casefold() for s in base}
            added = skipped = 0
            for c in cleaned:
                if c["name"].casefold() in names:
                    skipped += 1
                    continue
                names.add(c["name"].casefold())
                base.append({"id": uuid.uuid4().hex, **c, "order": len(base)})
                added += 1
            if len(base) > MAX_SPOTS:
                raise SpotError("too_many", f"At most {MAX_SPOTS} spots.")
            self._spots = base
            try:
                await self._save()
            except BaseException:
                self._spots = old
                raise
            return {"added": added, "skipped": skipped, "total": len(base)}
