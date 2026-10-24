import os
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["SPOTTER_DATA"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from spotter_api import forecast  # noqa: E402
from spotter_api.main import app  # noqa: E402

c = TestClient(app)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_sync_last_write_wins_and_link():
    a = c.post("/api/profile").json()["key"]
    r = c.post("/api/sync", headers=auth(a), json={"region": "newcastle", "rev": 0, "changes": [
        {"kind": "rating", "k": "freestyle5/1-2", "value": {"v": 1, "ll": [-1.6, 54.97], "title": "A"}, "t": 100},
        {"kind": "note", "k": "1-2", "value": {"text": "gate code"}, "t": 100},
    ]}).json()
    assert r["rev"] == 2 and len(r["items"]) == 2
    # an older edit loses, a newer one wins
    c.post("/api/sync", headers=auth(a), json={"region": "newcastle", "changes": [
        {"kind": "rating", "k": "freestyle5/1-2", "value": {"v": -1}, "t": 50}]})
    code = c.post("/api/link", headers=auth(a)).json()["code"]
    b = c.post("/api/link/claim", json={"code": code.lower()}).json()["key"]
    assert b != a
    assert c.post("/api/link/claim", json={"code": code}).status_code == 404  # single use
    r = c.post("/api/sync", headers=auth(b), json={"region": "newcastle", "rev": 0, "changes": [
        {"kind": "note", "k": "1-2", "value": None, "t": 200}]}).json()
    assert {i["k"]: i["value"] for i in r["items"]} == {"freestyle5/1-2": {"v": 1, "ll": [-1.6, 54.97], "title": "A"}, "1-2": None}
    # device a only gets what changed since its rev
    r = c.post("/api/sync", headers=auth(a), json={"region": "newcastle", "rev": 2}).json()
    assert [i["k"] for i in r["items"]] == ["1-2"] and r["rev"] == 3
    assert c.get("/api/me", headers=auth(a)).json()["devices"] == 2


def test_bad_key_and_region():
    assert c.post("/api/sync", headers=auth("nope"), json={"region": "x"}).status_code == 401
    k = c.post("/api/profile").json()["key"]
    assert c.post("/api/sync", headers=auth(k), json={"region": "../etc"}).status_code == 400


def test_photo_upload_strips_exif(tmp_path):
    from PIL import Image
    k = c.post("/api/profile").json()["key"]
    p = tmp_path / "x.jpg"
    exif = Image.Exif()
    exif[0x8825] = {2: (54.0, 58.0, 0.0)}  # GPS
    Image.new("RGB", (3000, 2000), "red").save(p, exif=exif)
    r = c.post("/api/photos?region=newcastle&flight=f1", headers=auth(k), files={"file": ("x.jpg", p.read_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    info = r.json()
    assert (info["w"], info["h"]) == (2048, 1365)
    img = c.get(f"/api/photos/{info['id']}.jpg")
    assert img.status_code == 200
    out = Path(os.environ["SPOTTER_DATA"]) / "photos" / f"{info['id']}.jpg"
    assert not Image.open(out).getexif()
    s = c.post("/api/sync", headers=auth(k), json={"region": "newcastle"}).json()
    assert s["photos"]["f1"][0]["id"] == info["id"]
    assert c.delete(f"/api/photos/{info['id']}", headers=auth(k)).status_code == 200
    assert c.get(f"/api/photos/{info['id']}.jpg").status_code == 404


def fake_fc(gusts):
    times = [f"2026-10-2{5 + i // 24}T{i % 24:02d}:00" for i in range(48)]
    return {"hourly": {"time": times, "wind_gusts_10m": gusts, "wind_speed_10m": [g / 2 for g in gusts],
                       "precipitation_probability": [0] * 48, "precipitation": [0] * 48},
            "daily": {"sunrise": ["2026-10-25T07:30", "2026-10-26T07:32"], "sunset": ["2026-10-25T17:00", "2026-10-26T16:58"]}}


def test_forecast_windows(monkeypatch):
    calm_morning = [10] * 12 + [40] * 12 + [40] * 24  # day 1 calm until noon (from the 07:00 hour, sunrise 07:30), day 2 windy
    monkeypatch.setattr(forecast, "fetch", lambda pts: {forecast._key(*pts[0]): fake_fc(calm_morning)})
    wk = forecast.week([{"id": "1-2", "title": "A", "lon": -1.6, "lat": 54.97}], "freestyle5", now=datetime(2026, 10, 25, 6))
    d0 = wk["days"][0]
    assert d0["best"]["start"] == "2026-10-25T07:00" and d0["best"]["end"] == "2026-10-25T12:00"
    assert wk["days"][1]["best"] is None
    assert wk["next"]["spots"][0]["id"] == "1-2"
    # the whoop's limits are much lower: 10 mph gusts are fine, 13 aren't
    assert forecast.week([{"id": "1", "title": "A", "lon": -1.6, "lat": 54.97}], "cine85", now=datetime(2026, 10, 25, 6))["days"][0]["best"]
