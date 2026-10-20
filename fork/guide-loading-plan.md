# Plan: a calm cold start (guide loading, memory, start-up check) -- 2026-10-20

Status (2026-10-20 evening): server S1+S2 published (v272), epgdata streamed (v273, the root
cause of the 8 GB freeze). arrTV on `feature/upstream-picks`, not pushed or released:
`3db81656` enrich writes only changed rows, `f380951f` streamed JSON decode, `71528a90` start-up
check + guide links + quiet background job, `116532be` one rebuild at a time (A3 "no duplicates"),
`a5396191` GuideWorkGate (A3 pacing: tune + heap), `cdc680b8` first view now-1 h..+2 h. Not yet:
A3 on-screen/group-first beyond the first view and grid slices (measure first: after the pieces
above a group switch already shows now..+2 h at once), S3, S4. Next: measure on the Google TV with
v273 on the server.

## 1. What was measured (Google TV / Chromecast HD, 1.4 GB, real ArrTV arr.115, your account)

Cold start straight into TF1 HD, recorded with a sampler on the TV (scripts in the session
scratchpad: `coldstart.sh`, `sampler.sh`, `analyze.py`; to be moved into `.personal/perf/`).

| | run 1 (3 min) | run 2 (5 min) |
|---|---|---|
| Java heap peak (limit 256 MB, largeHeap) | 255 MB | 252 MB |
| Blocking GCs (all threads stopped) | 181 | 91 |
| Frame gaps >= 0.5 s / total judder | many, up to 2.4 s | 15 / 19 s |
| Audio ran dry | yes | 20x |
| False "Reconnecting" (13.7 s buffered!) | 1 | 1 |
| Out of memory | epgdata decode failed | -- |
| Guide fully painted from cache | 102 s | 64 s |
| Fresh guide downloaded | 132 s | 239 s |
| Steady state after the storm | -- | heap ~100 MB, PSS ~194 MB |

What causes it, all at the same time in the first 2 minutes:
1. **`/api/epg/epgdata/`: every EPG data record of the whole server** (43+ sources) is decoded
   into a `JsonElement` tree (`DispatcharrClient.fetchListOrResults`) to keep 3 small fields.
   The heap jumped ~50 -> 236 MB the moment its response arrived (test app, 17:43:26). In run 1
   it ran out of memory; the fallback then re-keyed the guide ("similarity 0.78", 259 channels
   "changed") and caused a full rebuild on top.
2. **The background refresh job runs at every launch**: an interrupted periodic run (app killed
   or force-stopped -- normal on a 1.4 GB box) counts as due, so `PlaylistRefreshWorker` refetches
   groups + epgdata + channels right inside the cold start, next to the app's own start. epgdata
   was requested twice within 1 s.
3. The server's 24 h guide is downloaded twice (launch EPG load + DispatcharrLive "guide window
   (new channels)", 41k programmes each), each as one response decoded in one go.
4. Two full guide rebuilds at once ("cache" and "channels-update", 56k rows each, 24-28 s).
5. Category enrichment keeps the whole downloaded list (40-50k programmes) alive while it runs.
6. Every cache read and rebuild is 10-50x slower than normal because of 1-5 (group first:
   2.9k rows in 21-24 s).

## 2. Goal and targets

A cold start paints from the cache, makes **one** small request to learn whether anything
changed, and downloads only that, in small pieces, never two things at once, never while a
channel is starting, and slower while video plays.

Targets on the Google TV with the real account (same script, same channel):
- nothing changed on the server: 1 check request, 0 downloads;
- Java heap peak < 128 MB; 0 blocking GCs while video plays;
- 0 frame gaps >= 0.5 s, 0 false "Reconnecting";
- the guide on screen (what you look at) from the cache in < 5 s.

## 3. Dispatch More (server) -- new version

**S1. `GET /api/core/app-sync/`** (any logged-in user, cheap, cached a few seconds per login):
```json
{"lineup": "<hash>", "guide_links": "<hash>",
 "guides": {"stamp": "<hash>", "sources": [{"id": 3, "refreshed": "2026-10-20T15:02:11Z", "programmes": 41200}]},
 "window": {"from": "...", "to": "..."}}
```
- `lineup`: hash of the channels this login sees (uuid, number, name, group, logo, hidden,
  profile membership) -- computed with `values_list`, not `updated_at` (bulk `update()` does
  not touch `auto_now`).
- `guide_links`: hash of channel -> guide (epg_data_id, its tvg_id, source).
- `guides`: per source the last successful refresh and programme count; `stamp` over them.
- `capabilities` says `"sync_url": "/api/core/app-sync/"` so the app knows it exists.

**S2. `GET /api/core/app-guide-links/`**: only this login's channels -> `{uuid, epg_data_id,
tvg_id, source_id, source_type}` (~1.5k rows). Replaces downloading all of `/api/epg/epgdata/`.

**S3. Grid for some channels**: `/api/epg/grid/` already takes `start`/`end` (absolute window)
and `channel_profile_id`; add `channels=<uuid,uuid,...>` so the app can fetch what is on screen
first. Responses are already streamed by the server.

**S4. Socket**: after a guide source refresh finishes, send `channels_changed` with
`"source": "guide refresh"` (or a new `guides_refreshed`) so an open app updates without polling.

Stock Dispatcharr / older Dispatch More: no `sync_url` -> the app keeps time-based freshness
(channels 24 h, guide 30 min) but goes through the same queue and fixes below.

## 4. arrTV (app)

**A1. Start-up check instead of downloads** (every install, also existing ones):
- Launch paints channels and guide from the cache; no channel, epgdata or guide download.
- After the first picture and the guide are up: one `app-sync` call; compare with the stamps
  saved at the last successful sync (per playlist).
  - `lineup` / `guide_links` same -> nothing; different -> fetch the lineup + S2, apply as a diff
    (the existing `forLineup` patch path), no full rebuild.
  - `guides.stamp` same and the cache covers now-1 h .. now+window -> nothing; different -> queue
    an update for the changed sources / missing hours only.
- No stamps saved yet (first run after the update): one check, then a normal update through the queue.

**A2. Background refresh job** (option stays, meaning changes): it only checks (A1) on a timer
while the app is in the background or idle; never at launch, never while video plays.
Existing installs: on upgrade the periodic work is re-enqueued with an initial delay of the full
interval (so the overdue run does not fire at the next launch), and the worker itself refuses
to run within the first minutes after a launch or while playback is active.

**A3. One guide loader queue** (one job at a time):
1. on screen: channels in view (+1 screen above/below), now-1 h .. now+2 h;
2. all channels, now-1 h .. now+2 h;
3. widen in small steps (+2 h forward, -1 h back) up to the window;
4. history (catch-up) last; category tints only for what is on screen.
- Group switch or scroll: the current small step finishes, the new view goes first, then the
  rest resumes; groups passed through quickly are skipped.
- Pacing: pause from a channel press until the first frame + a few seconds; small steps with
  pauses while video plays; stop when memory is tight (onTrimMemory, low available memory,
  heap > ~60 % of max) and continue later.
- No duplicates: only one rebuild at a time, a newer request replaces a queued one; the live
  "guide window" check uses the same queue and stamps instead of a second full download.

**A4. Memory fixes** (independent of the server):
- Decode JSON straight into typed objects from the stream (no `JsonElement` trees, no whole body
  string): epgdata (until S2), channels, groups, grid.
- Grid in time slices (`start`/`end`), each written to the cache and released before the next.
- Enrichment does not keep the downloaded list; it works from the cache for what is on screen.
- Later, if needed: descriptions not held in the in-memory guide (read for the focused programme).
- A coarse memory line in release builds (heap used/max once a minute) for reports from users' boxes.

## 5. Order

1. Server S1 + S2 (+ tests) -> Dispatch More vNNN (published on the user's go).
2. App A4 (typed decode), A2 (no job at launch), A1 (start-up check) -> the glitch fix. Measure.
3. App A3 (loader queue, on-screen first, pacing) + grid slices + S3. Measure.
4. S4, enrichment, optional description change. Measure.

Each step: the cold-start script on the Google TV, before/after, with the numbers in §1.

## 6. Tests per step

Scenarios: nothing changed; a channel added on the server; a guide source refreshed; first
install (empty cache); stock Dispatcharr (no `sync_url`); group switch during loading; app
killed mid-load and started again; playback started during loading.

Representative numbers need your account's data in the test app (the test login only has
~26k rows and never reaches the peak): either the test app signed in to your account, or test
builds installed over the real ArrTV (risk: a newer database can't be read by arr.115 again).
