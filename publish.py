#!/usr/bin/env python3
"""
SignMyRoom social publisher.

Reads queue/<date>/schedule.json, publishes any item whose slot time has passed
and that has not already been published, to Instagram and/or the Facebook Page
via the Meta Graph API.

Designed to be run repeatedly (every 15 min from GitHub Actions cron). It is
idempotent: an item is published at most once, ever, because its id is recorded
in state/published.json before the run exits.

Env vars required:
  META_TOKEN      System User (or long-lived Page) access token
  IG_USER_ID      Instagram Business account id
  FB_PAGE_ID      Facebook Page id
  RAW_BASE        Public base URL for media, e.g.
                  https://raw.githubusercontent.com/<user>/<repo>/main
"""

import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

GRAPH = "https://graph.facebook.com/v21.0"
TZ = ZoneInfo("America/New_York")
ROOT = pathlib.Path(__file__).parent
STATE_PATH = ROOT / "state" / "published.json"

TOKEN = os.environ["META_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
RAW_BASE = os.environ["RAW_BASE"].rstrip("/")

DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------- graph calls

def _post(path, params):
    params = {**params, "access_token": TOKEN}
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _get(path, params):
    params = {**params, "access_token": TOKEN}
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def _wait_for_container(creation_id, timeout=600):
    """Video containers process asynchronously. Poll until FINISHED."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _get(creation_id, {"fields": "status_code,status"})
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"container {creation_id} failed: {status.get('status')}")
        time.sleep(10)
    raise TimeoutError(f"container {creation_id} not ready after {timeout}s")


# ------------------------------------------------------------------ publishing

def publish_instagram(item, media_url):
    params = {"caption": item["caption_ig"]}
    if item["type"] == "reel":
        params.update({"media_type": "REELS", "video_url": media_url})
    else:
        params["image_url"] = media_url

    container = _post(f"{IG_USER_ID}/media", params)
    creation_id = container["id"]

    if item["type"] == "reel":
        _wait_for_container(creation_id)

    result = _post(f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
    return result["id"]


def publish_facebook(item, media_url):
    if item["type"] == "reel":
        # Reels use the dedicated video_reels upload flow; hosted-URL publish.
        start = _post(f"{FB_PAGE_ID}/video_reels", {"upload_phase": "start"})
        video_id = start["video_id"]
        req = urllib.request.Request(
            f"https://rupload.facebook.com/video-upload/v21.0/{video_id}",
            method="POST",
            headers={"Authorization": f"OAuth {TOKEN}", "file_url": media_url},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            json.load(r)
        _post(
            f"{FB_PAGE_ID}/video_reels",
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": item["caption_fb"],
            },
        )
        return video_id

    result = _post(f"{FB_PAGE_ID}/photos", {"url": media_url, "caption": item["caption_fb"]})
    return result.get("post_id") or result["id"]


# ----------------------------------------------------------------------- state

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"published": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# ------------------------------------------------------------------------ main

def due_items(now):
    """Every queued item whose slot has arrived, oldest first."""
    items = []
    for schedule_file in sorted((ROOT / "queue").glob("*/schedule.json")):
        day = schedule_file.parent.name
        for item in json.loads(schedule_file.read_text()):
            when = datetime.fromisoformat(item["when"]).replace(tzinfo=TZ)
            if when <= now:
                items.append((when, day, item))
    return [(d, i) for _, d, i in sorted(items, key=lambda t: t[0])]


def main():
    now = datetime.now(TZ)
    state = load_state()
    published = state["published"]

    todo = [(day, item) for day, item in due_items(now) if item["id"] not in published]
    if not todo:
        print(f"{now:%Y-%m-%d %H:%M %Z}: nothing due.")
        return 0

    failures = 0
    for day, item in todo:
        media_url = f"{RAW_BASE}/queue/{day}/{item['media']}"
        record = {"at": datetime.now(timezone.utc).isoformat(), "media_url": media_url}
        print(f"--> {item['id']} ({item['type']}) -> {', '.join(item['platforms'])}")

        if DRY_RUN:
            print(f"    DRY RUN, would publish {media_url}")
            continue

        # Mark it claimed BEFORE the network calls. A crash mid-publish must
        # never let a later run publish the same thing twice -- a duplicate is
        # far worse than a missed post.
        published[item["id"]] = record
        save_state(state)

        for platform in item["platforms"]:
            try:
                if platform == "instagram":
                    record["instagram"] = publish_instagram(item, media_url)
                elif platform == "facebook":
                    record["facebook"] = publish_facebook(item, media_url)
                print(f"    {platform}: ok ({record.get(platform)})")
            except Exception as exc:  # noqa: BLE001 - report and keep going
                failures += 1
                record[f"{platform}_error"] = str(exc)[:500]
                print(f"    {platform}: FAILED {exc}", file=sys.stderr)

        save_state(state)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
