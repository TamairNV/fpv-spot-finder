import os
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["SPOTTER_DATA"] = tempfile.mkdtemp()
os.environ["MAIL_LOG_ONLY"] = "1"
os.environ["SPOTTER_ADMIN_EMAILS"] = "boss@example.com"

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


def test_rerank_needs_a_prepared_region_and_ratings():
    k = c.post("/api/profile").json()["key"]
    assert c.post("/api/rerank", headers=auth(k), json={"region": "nowhere"}).status_code == 409
    st = c.get("/api/rerank?region=nowhere", headers=auth(k)).json()
    assert st == {"ready": False, "job": None, "copy": None}


def test_models_on_request_need_the_region_rasters():
    assert c.get("/api/models/newcastle/freestyle5/1-x").status_code == 400
    assert c.get("/api/models/newcastle/nope/1-2").status_code in (400, 404)
    # the test data folder has no rasters for the region
    assert c.get("/api/models/newcastle/freestyle5/1-2").status_code == 404
    assert c.get("/api/models/newcastle/1-2.png").status_code == 404
    assert c.get("/api/models/newcastle/..%2F1-2.png").status_code in (400, 404)


def last_link(kind):
    import time as _t
    from spotter_api import mail
    for _ in range(50):
        if mail.sent:
            break
        _t.sleep(0.02)
    body = mail.sent.pop().get_content()
    return body.split(f"#{kind}=")[1].split()[0]


def signup(name, email, pw="a-long-pass-99", key=None):
    from spotter_api import main
    main._signups.clear()
    main._mails.clear()
    return c.post("/api/account/signup", headers=auth(key) if key else {}, json={"username": name, "email": email, "password": pw})


def test_accounts_need_a_confirmed_email_and_keep_the_device_profile():
    from spotter_api import main
    dev = c.post("/api/profile").json()["key"]
    c.post("/api/sync", headers=auth(dev), json={"region": "newcastle", "changes": [
        {"kind": "saved", "k": "freestyle5/3-4", "value": {"title": "S", "ll": [-1.6, 54.9]}, "t": 10}]})
    r = signup("Pilot_1", "Pilot@Example.com", key=dev)
    assert r.status_code == 200 and r.json() == {"key": dev, "check_email": True}
    token = last_link("verify")
    assert signup("pilot_1", "other@example.com").status_code == 409  # usernames are public, so this can say
    # an email that's taken answers the same as a new one, and its owner gets told
    assert signup("someone", "pilot@example.com").json()["check_email"] is True
    assert "already has yours" in __import__("spotter_api").mail.sent.pop().get_content()
    assert signup("x", "a@b.co").status_code == 400
    assert signup("shorty", "s@example.com", pw="short").status_code == 400
    assert signup("guessy", "g@example.com", pw="guessy-1234567").status_code == 400  # has the username in it
    assert signup("bad", "not-an-email").status_code == 400
    # unconfirmed: can't sign in
    main._logins.clear()
    assert c.post("/api/account/login", json={"username": "pilot_1", "password": "a-long-pass-99"}).status_code == 403
    old, token = token, last_link("verify")  # it sent a new link, and the old one stops working
    assert c.post("/api/account/verify", json={"token": old}).status_code == 404
    v = c.post("/api/account/verify", json={"token": token}).json()
    assert v["username"] == "Pilot_1"
    assert c.post("/api/account/verify", json={"token": token}).status_code == 404  # once only
    assert c.post("/api/account/login", json={"username": "pilot_1", "password": "nope"}).status_code == 401
    other = c.post("/api/account/login", json={"username": "PILOT@example.com", "password": "a-long-pass-99"}).json()
    assert other["username"] == "Pilot_1" and other["key"] != dev
    items = c.post("/api/sync", headers=auth(other["key"]), json={"region": "newcastle"}).json()["items"]
    assert [i["k"] for i in items] == ["freestyle5/3-4"]
    assert c.get("/api/me", headers=auth(other["key"])).json()["username"] == "Pilot_1"
    # a new password signs every other device out
    assert c.post("/api/account/password", headers=auth(dev), json={"old": "bad", "new": "newpass-12345"}).status_code == 403
    assert c.post("/api/account/password", headers=auth(dev), json={"old": "a-long-pass-99", "new": "newpass-12345"}).status_code == 200
    assert c.get("/api/me", headers=auth(other["key"])).status_code == 401
    assert c.post("/api/account/logout", headers=auth(dev)).status_code == 200
    assert c.get("/api/me", headers=auth(dev)).status_code == 401
    # forgot password: same answer for unknown emails, a one-use link for real ones that signs everything out
    main._mails.clear()
    assert c.post("/api/account/forgot", json={"email": "nobody@example.com"}).json() == {"ok": True}
    assert c.post("/api/account/forgot", json={"email": "pilot@example.com"}).json() == {"ok": True}
    tok = last_link("reset")
    main._logins.clear()
    keep = c.post("/api/account/login", json={"username": "pilot_1", "password": "newpass-12345"}).json()["key"]
    assert c.post("/api/account/reset", json={"token": tok, "password": "aaaaaaaaaaaa"}).status_code == 400
    r = c.post("/api/account/reset", json={"token": tok, "password": "fresh-pass-777"}).json()
    assert r["username"] == "Pilot_1"
    assert c.get("/api/me", headers=auth(keep)).status_code == 401
    assert c.post("/api/account/reset", json={"token": tok, "password": "fresh-pass-777"}).status_code == 404
    # delete
    assert c.post("/api/account/delete", headers=auth(r["key"]), json={"password": "wrong"}).status_code == 403
    assert c.post("/api/account/delete", headers=auth(r["key"]), json={"password": "fresh-pass-777"}).status_code == 200
    assert c.post("/api/account/login", json={"username": "pilot_1", "password": "fresh-pass-777"}).status_code == 401


def test_logins_are_rate_limited():
    from spotter_api import main
    main._logins.clear()
    codes = [c.post("/api/account/login", json={"username": "victim", "password": f"x{i}"}).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    main._logins.clear()


def test_reviews_are_public_only_from_confirmed_accounts():
    anon = c.post("/api/profile").json()["key"]
    unconfirmed = signup("lurker", "lurk@example.com").json()["key"]
    last_link("verify")
    named = signup("reviewer", "rev@example.com").json()["key"]
    c.post("/api/account/verify", json={"token": last_link("verify")})
    for key, stars in ((anon, 1), (unconfirmed, 1), (named, 4)):
        c.post("/api/sync", headers=auth(key), json={"region": "newcastle", "changes": [
            {"kind": "review", "k": "cine85/7-8", "value": {"stars": stars, "text": "nice gaps"}, "t": 5},
            {"kind": "note", "k": "7-8", "value": {"text": "private"}, "t": 5}]})
    r = c.get("/api/reviews?region=newcastle&drone=cine85&spot=7-8").json()
    assert r["n"] == 1 and r["avg"] == 4 and r["reviews"][0]["user"] == "reviewer" and r["reviews"][0]["text"] == "nice gaps"
    assert c.get("/api/reviews/summary?region=newcastle&drone=cine85").json() == {"7-8": [4.0, 1]}
    assert c.get("/api/reviews/summary?region=newcastle&drone=cine%25").status_code == 400
    # only admins can hide one
    body = {"region": "newcastle", "drone": "cine85", "spot": "7-8", "user": "reviewer"}
    assert c.post("/api/reviews/hide", headers=auth(named), json=body).status_code == 403


def test_stats_page_is_only_for_admin_emails():
    from spotter_api import main
    user = signup("plainuser", "plain@example.com").json()["key"]
    c.post("/api/account/verify", json={"token": last_link("verify")})
    boss = signup("boss", "Boss@Example.com").json()["key"]
    c.post("/api/account/verify", json={"token": last_link("verify")})
    main._logins.clear()
    c.post("/api/account/login", json={"username": "boss", "password": "wrong"})
    assert c.get("/api/stats/data").status_code == 404
    assert c.get("/api/stats/data", headers=auth(user)).status_code == 404
    assert c.get("/api/me", headers=auth(boss)).json()["admin"] is True
    d = c.get("/api/stats/data?days=7", headers=auth(boss)).json()
    assert len(d["days"]) == 7 and d["days"][-1]["requests"] > 0
    assert any(e["kind"] == "login_fail" and e["ip"] for e in d["events"])
    assert {g["label"] for g in d["gauges"]} >= {"Accounts", "Star reviews"}
    assert "127.0.0.1" not in str(d) or all(e["ip"] in (None, "testclient", "?") or e["ip"].endswith("/24") for e in d["events"])
    assert c.get("/api/stats/dashboard.js").status_code == 200
