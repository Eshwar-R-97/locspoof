import json
import os

import pytest

from locpicker import spots as spots_mod
from locpicker.spots import SpotError, SpotStore


@pytest.fixture
def store(tmp_path):
    return SpotStore(tmp_path / "spots.json")


async def test_create_list_and_persist(store, tmp_path):
    a = await store.create("Home", 41.87, -87.62, "🏠")
    b = await store.create("Work", 41.88, -87.63)
    assert [s["name"] for s in store.list()] == ["Home", "Work"]
    data = json.loads((tmp_path / "spots.json").read_text())
    assert data["version"] == 1 and len(data["spots"]) == 2
    again = SpotStore(tmp_path / "spots.json")
    assert [s["id"] for s in again.list()] == [a["id"], b["id"]]
    assert again.list()[0]["emoji"] == "🏠"


async def test_duplicate_names_rejected_case_insensitive(store):
    await store.create("Home", 1, 2)
    with pytest.raises(SpotError) as e:
        await store.create("  home ", 3, 4)
    assert e.value.status == 409


@pytest.mark.parametrize("name", ["", " ", "x" * 41, 5, None])
async def test_bad_names(store, name):
    with pytest.raises(SpotError):
        await store.create(name, 1, 2)


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, -181), (float("nan"), 0), ("1", 2)])
async def test_bad_coords(store, lat, lon):
    with pytest.raises(SpotError):
        await store.create("X", lat, lon)


async def test_rename_keeps_id_and_move(store):
    a = await store.create("Home", 1, 2)
    await store.create("Work", 3, 4)
    r = await store.update(a["id"], {"name": "House", "lat": 10, "lon": 20})
    assert r["id"] == a["id"] and r["name"] == "House" and r["lat"] == 10
    with pytest.raises(SpotError):
        await store.update(a["id"], {"name": "WORK"})
    # renaming to own name in different case is allowed
    await store.update(a["id"], {"name": "HOUSE"})
    with pytest.raises(SpotError):
        await store.update("nope", {"name": "x"})
    with pytest.raises(SpotError):
        await store.update(a["id"], {"color": "red"})


async def test_delete_and_reorder(store):
    a = await store.create("A", 1, 1)
    b = await store.create("B", 2, 2)
    c = await store.create("C", 3, 3)
    await store.reorder([c["id"], a["id"], b["id"]])
    assert [s["name"] for s in store.list()] == ["C", "A", "B"]
    assert [s["order"] for s in store.list()] == [0, 1, 2]
    await store.update(b["id"], {"order": 0})
    assert [s["name"] for s in store.list()] == ["B", "C", "A"]
    await store.delete(c["id"])
    assert [s["name"] for s in store.list()] == ["B", "A"]
    with pytest.raises(SpotError):
        await store.reorder([a["id"]])


async def test_crash_mid_write_leaves_old_file(store, tmp_path, monkeypatch):
    await store.create("Home", 1, 2)
    before = (tmp_path / "spots.json").read_text()

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(spots_mod.os, "replace", boom)
    with pytest.raises(OSError):
        await store.create("Work", 3, 4)
    assert (tmp_path / "spots.json").read_text() == before
    assert not (tmp_path / "spots.json.tmp").exists()
    assert [s["name"] for s in store.list()] == ["Home"]  # in-memory rolled back too


async def test_corrupt_file_is_backed_up(tmp_path):
    p = tmp_path / "spots.json"
    p.write_text("{ not json")
    s = SpotStore(p)
    assert s.list() == []
    assert s.warning and "backed up" in s.warning
    backups = [f for f in os.listdir(tmp_path) if f.startswith("spots.json.bak-")]
    assert len(backups) == 1
    assert (tmp_path / backups[0]).read_text() == "{ not json"
    await s.create("Fresh", 0, 0)
    assert SpotStore(p).list()[0]["name"] == "Fresh"


async def test_export_import_merge_and_replace(store):
    await store.create("Home", 1, 2)
    exp = store.export()
    other = SpotStore(store.path.with_name("other.json"))
    await other.create("Gym", 5, 5)
    res = await other.import_(exp)
    assert res == {"added": 1, "skipped": 0, "total": 2}
    res = await other.import_(exp)
    assert res["skipped"] == 1
    res = await other.import_({"spots": [{"name": "Only", "lat": 0, "lon": 0}]}, replace=True)
    assert [s["name"] for s in other.list()] == ["Only"]
    with pytest.raises(SpotError):
        await other.import_({"spots": [{"name": "Bad", "lat": 100, "lon": 0}]})
    with pytest.raises(SpotError):
        await other.import_("nope")
