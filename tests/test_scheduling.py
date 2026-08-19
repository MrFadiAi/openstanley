"""Scheduling v0.3 — media upload, image drafts, quote posts, scheduled replies."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["XOPENSTANLEY_NO_SCHEDULER"] = "1"  # before importing the server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient   # noqa: E402  (needs httpx)

from openstanley.core import db               # noqa: E402
db.init_db()

import openstanley.server.__main__ as server  # noqa: E402

client = TestClient(server.app)

TWEET_URL = "https://x.com/someuser/status/1790123456789012345"


def _cleanup(prefix: str) -> None:
    for f in (server.MEDIA_DIR).glob(prefix + "*"):
        f.unlink(missing_ok=True)


def test_media_upload_and_serve():
    _cleanup("media_")
    # 1x1 transparent PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
    r = client.post("/api/media", files={"file": ("t.png", png, "image/png")})
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert name.startswith("media_") and name.endswith(".png")
    # serve it back
    r2 = client.get(f"/api/media/{name}")
    assert r2.status_code == 200 and r2.content == png
    # path traversal blocked
    assert client.get("/api/media/..%2F..%2Fconfig.toml").status_code in (400, 404)
    # wrong type rejected
    r3 = client.post("/api/media", files={"file": ("t.txt", b"hi", "text/plain")})
    assert r3.status_code == 400
    _cleanup("media_")
    return name


def test_tweet_preview_dryrun():
    r = client.get("/api/tweet", params={"url": TWEET_URL})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["x_id"] == "1790123456789012345"
    assert data["text"]  # dry-run returns simulated preview text
    assert client.get("/api/tweet", params={"url": "https://example.com"}).status_code == 400


def test_draft_with_image_full_flow():
    _cleanup("media_")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
    up = client.post("/api/media", files={"file": ("i.png", png, "image/png")}).json()
    name = up["name"]

    # manual draft carrying the image
    r = client.post("/api/drafts", json={
        "text": "the ugly version teaches you things slides never will. what did you ship ugly?",
        "image": name})
    assert r.status_code == 200, r.text
    did = r.json()["draft_id"]
    d = db.get_draft(did)
    assert d["image"] == name
    assert d["meta"]["alg"]["score"] > 0

    # approve + publish (dry-run): image rides along
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    assert client.post(f"/api/drafts/{did}/approve", json={"scheduled_at": past}).status_code == 200
    res = client.post("/api/loops/publish").json()
    assert res["ok"], res
    pub = [p for p in res["result"]["published"] if p["draft_id"] == did]
    assert pub, res
    d = db.get_draft(did)
    assert d["status"] == "published" and d["x_id"]

    # media attach/clear on a fresh draft
    did2 = db.add_draft(text="second draft for attach test")
    assert client.post(f"/api/drafts/{did2}/attach", json={"image": name}).status_code == 200
    assert db.get_draft(did2)["image"] == name
    assert client.post(f"/api/drafts/{did2}/attach", json={"image": None}).status_code == 200
    assert db.get_draft(did2)["image"] is None
    db.update_draft(did2, status="rejected")
    _cleanup("media_")


def test_quote_post_flow():
    # create quote draft via endpoint
    r = client.post("/api/drafts", json={
        "text": "this is the angle the announcement lacked: shipping beats announcing.",
        "quote_of": {"url": TWEET_URL}})
    assert r.status_code == 200, r.text
    did = r.json()["draft_id"]
    d = db.get_draft(did)
    assert d["kind"] == "quote" and d["quote_of"] == "1790123456789012345"

    # set-quote on an existing draft also works
    did2 = db.add_draft(text="plain draft to convert")
    r2 = client.post(f"/api/drafts/{did2}/quote",
                     json={"url": TWEET_URL, "text": "original text", "author": "someuser"})
    assert r2.status_code == 200 and r2.json()["quote_of"]["x_id"] == "1790123456789012345"
    assert db.get_draft(did2)["kind"] == "quote"

    # publish both (dry-run) — quote_of must be passed through
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    for i in (did, did2):
        client.post(f"/api/drafts/{i}/approve", json={"scheduled_at": past})
    res = client.post("/api/loops/publish").json()
    published_ids = [p["draft_id"] for p in res["result"]["published"]]
    assert did in published_ids and did2 in published_ids


def test_scheduled_reply_flow():
    """Engage-style scheduled reply: kind='reply', scheduled_at, publish sends it as a reply."""
    target_x_id = "niche-target-987"
    when = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    did = db.add_draft(
        text="solid question — the stack is boring on purpose. what would you have picked?",
        kind="reply", scheduled_at=when,
        meta={"reply_to_x_id": target_x_id, "target_author": "niche_user_9",
              "source": "engage-niche", "alg": {"score": 70, "grade": "good",
                                                "factors": []}})
    # approval gate: not approved → publish must NOT send it
    res0 = client.post("/api/loops/publish").json()
    assert did not in [p["draft_id"] for p in res0["result"]["published"]], \
        "unapproved reply must not publish"

    # approve (keeps the proposed slot) → publish
    r = client.post(f"/api/drafts/{did}/approve", json={})
    assert r.status_code == 200
    assert r.json()["scheduled_at"] == when, "approve must keep proposed slot"
    res = client.post("/api/loops/publish").json()
    assert did in [p["draft_id"] for p in res["result"]["published"]], res


def test_reschedule_and_calendar():
    did = db.add_draft(text="calendar drag test post")
    day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT15:30:00")
    r = client.post(f"/api/drafts/{did}/reschedule", json={"scheduled_at": day})
    assert r.status_code == 200
    assert db.get_draft(did)["scheduled_at"] == day
    assert client.post(f"/api/drafts/{did}/reschedule",
                       json={"scheduled_at": "not-a-date"}).status_code == 400

    cal = client.get("/api/calendar").json()
    assert "days" in cal and "empty_slots" in cal
    key = day[:10]
    assert any(i["id"] == did for i in cal["days"].get(key, []))
    item = next(i for i in cal["days"][key] if i["id"] == did)
    assert item["time"] == "15:30" and item["kind"] == "post"
    db.update_draft(did, status="rejected")


def test_insights_endpoint():
    r = client.get("/api/insights")
    assert r.status_code == 200
    data = r.json()
    for key in ("engagement_over_time", "best_hours", "hours_heatmap",
                "format_performance", "language_mix", "summary"):
        assert key in data


def test_style_profile_endpoint():
    from openstanley.gen import style_scan
    db.set_setting("style_profile", {"stats": {"posts_scanned": 5},
                                     "human_summary": "s", "updated_at": "now"})
    r = client.get("/api/style-profile").json()
    assert r["exists"] and r["stats"]["posts_scanned"] == 5
    db.set_setting("style_profile", None)  # clear for other tests? keep simple
    db.set_setting("style_profile", {"stats": {"posts_scanned": 5},
                                     "human_summary": "s", "updated_at": "now"})


def test_settings_language():
    r = client.post("/api/settings", json={"language": "ar"})
    assert r.status_code == 200 and r.json()["language"] == "ar"
    assert client.get("/api/settings").json()["language"] == "ar"
    client.post("/api/settings", json={"language": "en"})
