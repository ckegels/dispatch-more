# Look back gets a provider: moving another viewer to a stream of their own

Planned 2026-10-02, against release v247 and arrTV arr.87. Nothing here is built yet.

## 1. What happened, and what the user wants

On 2026-10-02 a look back on ┃UK┃ FOOD NETWORK failed with "Catch-up Unavailable (503)". The
programme's archive exists only on Digitalizard ("UK| FOOD NETWORK HD", catch-up 2 days -- both
TiviBridge streams of the channel have none), Digitalizard allows one connection, and a Sony
BRAVIA running arrTV (user 4) had just been moved onto Digitalizard by Channel Switch Overlap's
"Use another account" for a live channel. The server's log: `Timeshift: profile 15 profile_full
on account 15` for 13 s, then arrTV gave up.

The user (2026-10-02): when someone wants a look back on a provider another viewer is using,

1. see whether that viewer's channel has **another stream** they could watch instead;
2. if so, **move them** to it, and **check the move worked**;
3. if it did not, **move them back**, and tell the look-back viewer it is **unavailable due to
   current viewing priorities**;
4. give that answer a **cooldown**, so the look-back viewer cannot hammer the other viewer with
   moves -- the cooldown ends early the moment the provider is free anyway;
5. while this happens, the look-back viewer's loading screen **says each step**: "Another viewer
   is watching on this provider", "Moving them to another stream", "Stream moved", "Stream
   verified", "Starting look back", or the refusal.

Nobody loses their picture to someone else's look back: the live viewer is only ever moved to a
stream that plays, and stays where they were otherwise.

## 2. Where it fits

- Look back is served by `apps/timeshift` (stock, `views.catchup_proxy`). For each catch-up
  stream of the channel, `_prepare_catchup_stream_attempt(..., reserve=True)` reserves a slot on
  the stream's account profile; a full profile is `blocked` (`capacity_blocked`), and with none
  free the request ends in 503 "Stream slot busy".
- arrTV mints a native session first (`POST /api/catchup/sessions/`), then plays
  `GET /proxy/catchup/<uuid>?session_id=`. The mint is where to make room: before the player
  asks, with an answer arrTV can show (a video GET cannot carry progress).
- A live channel's stream can be changed in place, the viewers keeping their connection: the live
  proxy's stream switch (what arrTV's "Switch stream" uses, `change_stream`), the same one
  Stream Check and failover rely on. Alternatives with a free slot: `url_utils.get_alternate_streams`.
- Which live channel holds a profile: Redis `stream_profile:<stream_id>` / `channel_stream:<id>`
  and the channel's metadata (`live_proxy/redis_keys.py`); which viewers: the channel's clients
  (`probation._channel_clients`).

## 3. Design

### 3.1 The session mint makes room (server)

`POST /api/catchup/sessions/` gains an outcome when no catch-up stream of the channel has a free
slot (today it always answers 201 and the GET fails later):

1. **Catch-up streams of the channel** whose account can serve the programme (catch-up days
   cover the start). None at all: 404 "no archive" as today.
2. **A free slot on any of them**: 201 as today.
3. **All full** -- for each full profile, the **live channels holding it** (recordings are never
   touched: `_is_being_recorded`; neither are other look-back sessions or VOD).
4. For each holding channel, in order of fewest viewers: an **alternative stream** of that channel
   on another account with a free slot (`get_alternate_streams`, excluding the account the look
   back needs and custom/fallback streams), preferring the same quality or better.
5. **None anywhere**: the refusal (§3.4).
6. Otherwise answer **202 Accepted** with the session and `state: "making_room"`, and start the
   move in the background (a Celery task on the default queue, or a thread in the web worker that
   minted it -- a few seconds of work, it must not hold the request).

### 3.2 The move, step by step

Each step is written to the session's status (Redis, `timeshift:api-room:<session>`, the session's
TTL), which arrTV polls (§3.3):

| Step | `step` | Shown |
|---|---|---|
| Found the other viewer | `found` | "Another viewer is watching on {provider}" |
| Switching their channel | `moving` | "Moving them to another stream ({stream})" |
| The switch was made | `moved` | "Stream moved" |
| Their picture plays again | `verified` | "Stream verified" |
| The slot is free | `ready` | "Starting look back" |
| Could not | `refused` | "Unavailable due to current viewing priorities" |

- **moving**: the live proxy's stream switch on the holding channel to the chosen alternative,
  marked as made by look back (logged, and shown in Stats as "moved for a look back").
- **moved -> verified**: within 10 s, the channel's metadata names the new stream, its state is
  `active`, and its buffer index advances (bytes from the new provider) -- the same test Stream
  Check's failover verification uses. The old profile's connection count drops.
- **Failed** (no bytes, an error, the slot not freed): **switch back** to the original stream,
  verify that too (if even that fails, the channel's own failover takes over -- the viewer was
  watching a working stream a moment ago, so it is the same as any provider hiccup), then
  `refused`.
- **ready**: the look-back slot is reserved for this session (`reserve=True` on the freed profile,
  held for 20 s for the player's GET), so another viewer cannot take it in between.
- A viewer is moved at most **once per 10 minutes** for look back (Redis per channel), so two
  look-back viewers cannot bounce one live viewer between providers.

### 3.3 arrTV shows the steps

- The mint answering 202: arrTV polls `GET /api/catchup/sessions/<id>/room/` every 0.5 s and
  shows the step lines on the catch-up loading screen, one under the other (ticked as they pass).
- `ready`: plays the session as today. `refused`: the catch-up error card with the server's text
  and, when given, "Try again in N min".
- A server without it answers 201 as today: nothing changes.
- The 503 retry from arr.76 stays for the other cases (a slot freed a moment late).

### 3.4 The refusal and its cooldown

- 409 (or the room status `refused`) with `reason: "viewing_priorities"`, the text, and
  `retry_after` seconds.
- **Cooldown**: Redis `timeshift:priority-cooldown:<user>:<channel>`, 5 minutes. While it runs, a
  new mint for that channel by that user is refused at once **unless** a catch-up profile has a
  free slot by then (the other viewer stopped) -- then it goes ahead and the cooldown is deleted.
- Logged and listed in Diagnostics (who wanted what, who was watching, why it was refused).

### 3.5 Switch

`look_back_priority` in the arrTV settings page (Dispatch More -> arrTV), on by default as the user
asked; off = stock (the mint never moves anyone). A second switch, `look_back_priority_notify`:
the moved viewer's arrTV shows a small "Moved to {stream} for another viewer" toast (a socket
message, as guide changes already use) -- on.

## 4. Tests to write

- Mint with a free slot: 201 unchanged. All full, no alternative: refused, cooldown set. All full,
  an alternative: 202, the switch called with the right stream, steps in order; verification
  failing switches back and refuses.
- Recordings, other look backs and custom streams are never moved; one viewer is not moved twice
  in 10 minutes.
- Cooldown: refused at once while it runs; ignored and cleared when a slot frees.
- arrTV: the poll loop shows each step and ends on ready / refused; a 201 server unchanged.

## 5. Files

Server: `apps/timeshift/api_views.py` (mint), a new `apps/timeshift/priority.py` (the room
making), `apps/proxy/live_proxy/...` (the switch and its verification, reused), settings in
`app_devices.DEFAULTS`, the arrTV settings page. arrTV: `CatchupPlaybackResolver` (202 and the
room poll), the catch-up loading card (`PlayerScreen` / `CatchupUnavailableCard`), the toast.
