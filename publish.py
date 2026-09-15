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
import urllib.error
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

class GraphError(RuntimeError):
    """A Graph failure with Meta's own explanation attached.

    urllib's default message is just "HTTP Error 403: Forbidden", which says
    nothing about WHY. Meta puts the real reason in the response body, so read
    it before it is thrown away -- a 403 that reads "(#200) requires
    publish_video" is a five-minute fix, and a bare 403 is a day of guessing.
    """


def _raise(where, exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = "<unreadable>"
        try:
            err = json.loads(body).get("error", {})
            detail = (
                f"{err.get('type')} code={err.get('code')} "
                f"subcode={err.get('error_subcode')} "
                f"msg={err.get('message')!r} "
                f"user_msg={err.get('error_user_msg')!r}"
            )
        except Exception:
            detail = body[:600]
        raise GraphError(f"{where}: HTTP {exc.code} -- {detail}") from None
    raise GraphError(f"{where}: {exc}") from None


def _post(path, params, token=None):
    params = {**params, "access_token": token or TOKEN}
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except Exception as exc:
        _raise(f"POST /{path}", exc)


def _get(path, params, token=None):
    params = {**params, "access_token": token or TOKEN}
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.load(r)
    except Exception as exc:
        _raise(f"GET /{path}", exc)


def _wait_for_container(creation_id, timeout=600):
    """Video containers process asynchronously. Poll until FINISHED."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _get(creation_id, {"fields": "status_code,status"})
        code = status.get("status_code")
        if code in ("FINISHED", "PUBLISHED", None):
            return
        if code == "ERROR":
            raise RuntimeError(f"container {creation_id} failed: {status.get('status')}")
        time.sleep(10)
    raise TimeoutError(f"container {creation_id} not ready after {timeout}s")


# ------------------------------------------------------------------ publishing

_PAGE_TOKEN = None


def page_token():
    """Publishing AS the Page needs a Page access token.

    The system-user token is a user token; Instagram's endpoints accept it but
    /{page-id}/photos answers 403 Forbidden. Exchange it once per run.
    """
    global _PAGE_TOKEN
    if _PAGE_TOKEN is None:
        _PAGE_TOKEN = _get(FB_PAGE_ID, {"fields": "access_token"})["access_token"]
    return _PAGE_TOKEN


def _media_publish(creation_id, attempts=6, delay=10):
    """Publish a container, tolerating the "not ready yet" race.

    media_publish answers 400 code=9007 subcode=2207027 "Media ID is not
    available / The media is not ready for publishing" when the container is
    still processing. That is a TIMING problem, not a permanent one -- it cost
    the 2026-09-14 noon post and probably the three unexplained 400s on 9/08
    and 9/09. Wait and try again rather than burning the slot.
    """
    last = None
    for attempt in range(attempts):
        try:
            return _post(f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
        except GraphError as exc:
            if "2207027" not in str(exc) and "code=9007" not in str(exc):
                raise
            last = exc
            print(f"    instagram: container not ready (try {attempt + 1}/{attempts}), waiting {delay}s")
            time.sleep(delay)
    raise last


def _create_container(params, attempts=4, delay=15):
    """Create the IG container, tolerating Meta's transient fetch failures.

    /media answers 400 code=9004 subcode=2207052 "The media could not be
    fetched from this URI" when Meta's fetcher has a bad moment against
    raw.githubusercontent.com. The URL is fine -- 2026-09-15 noon failed this
    way while the 8:00 slot on the same host succeeded. Retry before burning
    the slot; any other Graph error still raises immediately.
    """
    last = None
    for attempt in range(attempts):
        try:
            return _post(f"{IG_USER_ID}/media", params)
        except GraphError as exc:
            if "2207052" not in str(exc):
                raise
            last = exc
            print(f"    instagram: media fetch failed (try {attempt + 1}/{attempts}), waiting {delay}s")
            time.sleep(delay)
    raise last


def publish_instagram(item, media_url):
    params = {"caption": item["caption_ig"]}
    if item["type"] == "reel":
        params.update({"media_type": "REELS", "video_url": media_url})
    else:
        params["image_url"] = media_url

    container = _create_container(params)
    creation_id = container["id"]

    # Wait for EVERY type, not just reels. An image container is usually
    # FINISHED immediately, but "usually" is what lost the noon slot.
    _wait_for_container(creation_id)

    return _media_publish(creation_id)["id"]


def publish_facebook(item, media_url):
    tok = page_token()
    if item["type"] == "reel":
        # Reels use the dedicated video_reels upload flow; hosted-URL publish.
        start = _post(f"{FB_PAGE_ID}/video_reels", {"upload_phase": "start"}, token=tok)
        video_id = start["video_id"]
        req = urllib.request.Request(
            f"https://rupload.facebook.com/video-upload/v21.0/{video_id}",
            method="POST",
            headers={"Authorization": f"OAuth {tok}", "file_url": media_url},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                json.load(r)
        except Exception as exc:
            _raise("rupload video-upload", exc)
        _post(
            f"{FB_PAGE_ID}/video_reels",
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": item["caption_fb"],
            },
            token=tok,
        )
        return video_id

    result = _post(
        f"{FB_PAGE_ID}/photos",
        {"url": media_url, "caption": item["caption_fb"]},
        token=tok,
    )
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
