#!/usr/bin/env python3
"""Read-only preflight for the SignMyRoom publisher.

Proves the token can do every READ that publishing depends on, and never
calls a publishing endpoint. Safe to run at any time; posts nothing.

Exits non-zero if anything that would break a real run is broken.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

GRAPH = "https://graph.facebook.com/v21.0"
TZ = ZoneInfo("America/New_York")

TOKEN = os.environ["META_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
RAW_BASE = os.environ["RAW_BASE"].rstrip("/")

problems = []


def ok(msg):
    print(f"  PASS  {msg}")


def bad(msg):
    print(f"  FAIL  {msg}")
    problems.append(msg)


def info(msg):
    """Worth seeing, but not a reason to fail the run."""
    print(f"  INFO  {msg}")


def get(path, params, token=None):
    params = {**params, "access_token": token or TOKEN}
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def explain(exc):
    """Graph errors carry the useful part in the body, not the status line."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.load(exc)["error"]
            return f"HTTP {exc.code}: {body.get('message')} (code {body.get('code')})"
        except Exception:
            return f"HTTP {exc.code}"
    return str(exc)


print("\n=== 1. Is the token alive, and what is it? ===")
try:
    me = get("me", {"fields": "id,name"})
    ok(f"token authenticates as {me.get('name')} ({me.get('id')})")
except Exception as exc:
    bad(f"token is dead or invalid -- {explain(exc)}")
    print("\nNothing else can pass until the token works. Stopping.\n")
    sys.exit(1)

print("\n=== 2. Can it see the Facebook Page? ===")
try:
    page = get(FB_PAGE_ID, {"fields": "id,name"})
    ok(f"page {page.get('name')} ({page.get('id')}) is visible")
except Exception as exc:
    bad(f"cannot read the Page -- {explain(exc)}")

print("\n=== 3. THE ONE THAT FAILED BEFORE: exchange for a Page token ===")
page_token = None
try:
    page_token = get(FB_PAGE_ID, {"fields": "access_token"})["access_token"]
    ok(f"got a Page access token ({len(page_token)} chars, not shown)")
except Exception as exc:
    bad(f"Page token exchange failed -- {explain(exc)}")
    bad("this is exactly what produced the 403 on /photos; Facebook posts will fail")

print("\n=== 4. Does that Page token actually carry publish rights? ===")
if page_token:
    # "tasks" is NOT a field on the Page node -- asking for it there returns
    # "(#100) Tried accessing nonexisting field". It is only returned on the
    # /me/accounts EDGE. And a system-user token may not list the Page there
    # at all, so a blank result is informational, never a failure. The real
    # predictor of a publish 403 is the /photos read below.
    try:
        accts = get("me/accounts", {"fields": "id,name,tasks", "limit": "100"})
        entry = next((d for d in accts.get("data", []) if str(d.get("id")) == str(FB_PAGE_ID)), None)
        if entry is None:
            info("Page not listed under /me/accounts (normal for a system user)")
        else:
            tasks = entry.get("tasks") or []
            print(f"        tasks granted: {', '.join(tasks) if tasks else '(none reported)'}")
            if "CREATE_CONTENT" in tasks:
                ok("CREATE_CONTENT present -- the Page can be posted to")
            else:
                bad("CREATE_CONTENT missing -- publishing to the Page will be refused")
    except Exception as exc:
        info(f"could not enumerate Page tasks ({explain(exc)}) -- not fatal")

    # Read the photos edge. A GET here needs the same token the POST uses,
    # so a 403 on this line predicts a 403 on a real post.
    try:
        get(f"{FB_PAGE_ID}/photos", {"limit": "1"}, token=page_token)
        ok("/photos edge is readable with the Page token (no 403)")
    except Exception as exc:
        bad(f"/photos edge refused the Page token -- {explain(exc)}")
else:
    bad("skipped -- no Page token to test")

print("\n=== 5. Can it see the Instagram account? ===")
try:
    ig = get(IG_USER_ID, {"fields": "id,username,media_count"})
    ok(f"@{ig.get('username')} ({ig.get('id')}), {ig.get('media_count')} posts")
except Exception as exc:
    bad(f"cannot read the Instagram account -- {explain(exc)}")

print("\n=== 6. Is tomorrow's media actually fetchable at its public URL? ===")
day = None
queue = os.path.join(os.path.dirname(__file__), "..", "..", "queue")
queue = os.path.abspath(queue)
days = sorted(d for d in os.listdir(queue)) if os.path.isdir(queue) else []
today = datetime.now(TZ).strftime("%Y-%m-%d")
future = [d for d in days if d >= today]
day = future[0] if future else (days[-1] if days else None)

if not day:
    bad("no queue/<date>/ folders at all")
else:
    print(f"        checking queue/{day}/")
    sched_path = os.path.join(queue, day, "schedule.json")
    try:
        items = json.load(open(sched_path))
    except Exception as exc:
        items = []
        bad(f"schedule.json unreadable -- {exc}")

    for item in items:
        url = f"{RAW_BASE}/queue/{day}/{item['media']}"
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                size = r.headers.get("Content-Length", "?")
                ok(f"{item['id']}  {item['media']}  ({size} bytes)")
        except Exception as exc:
            bad(f"{item['id']}  {item['media']}  NOT FETCHABLE -- {explain(exc)}")
            bad("  this slot would 404, mark itself published, and burn")

print("\n=== 7. THE REELS PATH (the 9:00 AM slot failed here on 2026-09-14) ===")
if page_token:
    # Reels do NOT go through /photos. They use /{page-id}/video_reels, which
    # asks for its own permission surface on top of pages_manage_posts. Photos
    # succeeding tells you nothing about whether reels will.
    try:
        reels = get(f"{FB_PAGE_ID}/video_reels", {"limit": "1"}, token=page_token)
        ok(f"/video_reels edge readable with the Page token ({len(reels.get('data', []))} recent)")
    except Exception as exc:
        bad(f"/video_reels refused the Page token -- {explain(exc)}")
        bad("  this is the 2026-09-14 09:00 reel failure; photo posts are unaffected")

    try:
        perms = get("me/permissions", {})
        granted = sorted(d["permission"] for d in perms.get("data", []) if d.get("status") == "granted")
        print(f"        granted: {', '.join(granted) if granted else '(none listed)'}")
        # pages_show_list is NOT optional for reels: Meta answers the
        # /video_reels POST with "(#200) Subject does not have permission to
        # post videos on this target, due to lack of pages_show_list
        # permission." Photos never need it, so it is easy to miss.
        for need in ("pages_manage_posts", "pages_read_engagement",
                     "publish_video", "pages_show_list"):
            if need in granted:
                ok(f"{need} granted")
            else:
                info(f"{need} not listed (a system-user token may not enumerate these)")
    except Exception as exc:
        info(f"could not enumerate token permissions ({explain(exc)}) -- not fatal")
else:
    bad("skipped -- no Page token")

print("\n" + "=" * 60)
if problems:
    print(f"{len(problems)} PROBLEM(S) FOUND:\n")
    for p in problems:
        print(f"  - {p}")
    print()
    sys.exit(1)
print("ALL CHECKS PASSED -- nothing was published.")
print("=" * 60 + "\n")
