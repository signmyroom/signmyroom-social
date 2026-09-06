# SignMyRoom social publisher

Publishes queued posts to Instagram (signmyroom_com) and the SignMyRoom.com
Facebook Page via the Meta Graph API, on a schedule, from GitHub Actions.

## Repo secrets to set
Settings -> Secrets and variables -> Actions -> New repository secret

- `META_TOKEN`  - System User access token (never expires)
- `IG_USER_ID`  - Instagram Business account id
- `FB_PAGE_ID`  - 113314831371174

`RAW_BASE` is derived automatically from the repo, so media must live in a
PUBLIC repo for Meta's servers to fetch it.

## Adding a day
Create `queue/YYYY-MM-DD/`, drop the graphics in, and add `schedule.json`:

```json
[
  {
    "id": "2026-09-07-0800",
    "when": "2026-09-07T08:00:00",
    "type": "image",
    "media": "p_0800.jpg",
    "platforms": ["facebook", "instagram"],
    "caption_fb": "...",
    "caption_ig": "..."
  }
]
```

- `when` is America/New_York, no timezone suffix.
- `type` is `image` or `reel`.
- `id` must be unique forever - it is the duplicate guard.

## How duplicates are prevented
`state/published.json` records every id the moment it is claimed, *before* any
network call, and is committed back after each run. The workflow uses a
`concurrency` group so two runs can never publish at once. If a publish crashes
half way, the id is already burned and no later run will retry it - a missed
post is recoverable, a triple post is not.

## Timing
GitHub cron fires every 15 minutes and can run 5-20 minutes late under load.
Slots are "publish at or after", not exact. Do not queue anything that has to
land on an exact minute.
