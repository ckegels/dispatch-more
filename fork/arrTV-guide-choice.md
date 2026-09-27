# arrTV: "Wrong guide? Choose another"

Written 2026-09-27, against release v215. **The server part is built (v216):**
`apps/proxy/live_proxy/app_guides.py`, the endpoints in `core/api_views.py`, the preload task
`apps.channels.tasks.preload_guide_choices`, the settings on Settings → arrTV, tests in
`apps/proxy/live_proxy/tests/test_app_guides.py`; the app's side is `fork/arrTV-integration.md`
§7a. **arrTV's part is not built yet.** Where the build differs from this design it is said
below. This document is the whole hand-over: what the user asked for, how it fits into
what is already there, the server work, the app work and the tests. Read `fork/HANDOVER.md`
(§3, §5.6b, §5.11) and `fork/arrTV-integration.md` alongside it.

---

## 1. What the user wants

Someone watching a channel in arrTV sees that the guide is wrong: the picture shows one
programme and the guide says another. They hold OK, and in the player's options, directly
**under "Send a report to the server"**, there is **"Wrong guide? Choose another"**. It opens
a list of other guides that could be this channel, taken from the other EPG sources, each
showing **what is on it right now**, the way the Guides tab of the Channel Manager does. They
compare it with the picture, pick the one that matches, and the channel is on that guide from
then on, for everybody.

The user's decisions, as given:

1. **Everyone may change the guide.** Any login arrTV uses, not only admins.
2. **A toggle** in Dispatch More's arrTV settings (Settings → Streaming → arrTV) turns the
   whole feature off. Off by default, like every other arrTV switch; off means the endpoints
   refuse and the app does not show the entry.
3. **It does not replace the report.** "Send a report to the server" stays exactly as it is,
   including its "Wrong or missing guide" choice. This is a separate entry, placed under it.
4. **The change is marked as chosen by a user**, with who did it: the user, the device, and
   the IP address it came from.
5. **Nothing without information is shown.** A guide that has no programme to show right now
   is never on the list. If that means loading programmes for guides nobody uses, they are
   loaded ahead of time (the user offered dummy channels for this; §4 explains why that is not
   needed and does the same thing without them).

---

## 2. What already exists and is reused

The Guides tab already does almost all of the work. The new part is mainly a way for arrTV
to reach it, and a way to make sure the guides on offer have programmes loaded.

| Needed | Already there | Where |
|---|---|---|
| Guides that could be this channel, best first | `guide_candidates(name, tvg_id, …, current, source)`: Dispatcharr's own fuzzy matcher plus the fork's country scoring, with the channel's current guide kept first | `apps/channels/channel_manager.py:2394` |
| What is on each one now | `on_now(epg_ids)` and `_what_they_carry(entries)`: the programme on now, when it changes, how many it holds, whether a channel uses it | `channel_manager.py:1093`, `:1830` |
| Loading programmes for a guide no channel uses | `load_programmes(epg_ids)`: one pass through each source's XMLTV file for every entry wanted from it | `channel_manager.py:2062` |
| Keeping those programmes past the next refresh | `kept_after_reading()`, which stock's orphan clean-up already asks (the fork's hook in `apps/epg/tasks.py`, around line 2085) | `channel_manager.py:1989` |
| Putting a guide on a channel properly | `guide_manager.apply({channel id: epg id})`: saves with `update_fields` so Dispatcharr's own signal drops the guide cache and reads the new guide's programmes | `apps/channels/guide_manager.py:955` |
| "Chosen already" (the Guides tab will not suggest another guide for it later) | `CHOSEN_KEY` (`guide-manager-chosen`), `choose()`, `settled()` | `guide_manager.py:48`, `:226`, `:268` |
| The arrTV switches, settings page and capabilities | `app_devices.DEFAULTS`, `load_settings`, `capabilities()`; `ArrTvSettings.jsx` | `apps/proxy/live_proxy/app_devices.py` |
| The device and its name | `declared_device(request)`, `X-Dispatch-Device` / `-Name` | `app_devices.py:153` |
| The client's IP address | `dispatcharr.utils.get_client_ip(request)`, the same one the proxy uses | `dispatcharr/utils.py:117` |

The existing Channel Manager endpoints (`channel_manager_guides` and the Guides tab's apply)
are **admin-only** (`IsAdmin`) and stay that way. The arrTV endpoints are new and separate,
guarded by the new switch, the way `app-reports` and `app-stall` are.

---

## 3. The server

### 3.1 The switch

`app_devices.DEFAULTS` gains `"guide_choice": False`. On `ArrTvSettings.jsx` it is one more
switch, with the other arrTV switches:

> **Let arrTV change a channel's guide.** Whoever is watching can pick another guide for the
> channel from the player. The change is for every viewer and for Plex and Jellyfin, and is
> recorded with the user, device and address that made it.

`capabilities()` gains:

```json
"guide_choice": true,
"guide_choice_url": "/api/core/app-guide/"
```

It does **not** depend on `devices`. A device that does not identify itself can still use
it; the record then has the user and IP address, and the device is written as unknown.

**Off means stock.** With the switch off, both endpoints answer 403, no preloading runs,
nothing is kept past the clean-up that stock would not keep, and nothing else in Dispatcharr
changes.

### 3.2 `GET /api/core/app-guide/?channel=<uuid or id>`

Any logged-in user, the same as `app-reports`. The channel must be one the login's own
playlist gives it (the same channel access the stream endpoints check); otherwise it gets a
404, not a list.

The answer lists the guides that could be this channel **and hold a programme right now**,
best match first, one list across all sources (as the Guides tab ranks them), each with the
source it comes from:

```json
{
  "channel": {"uuid": "0f1e…", "id": 1234, "name": "┃AT┃ ORF 1", "number": 101},
  "current": {
    "epg_id": 5501, "name": "ORF1.at", "source": "EPGShare AT",
    "now":  {"title": "Zeit im Bild", "start": "2026-09-27T17:30:00Z", "end": "2026-09-27T17:50:00Z"},
    "next": {"title": "Wetter", "start": "2026-09-27T17:50:00Z"}
  },
  "guides": [
    {
      "epg_id": 88213, "name": "ORF 1 HD", "tvg_id": "ORF1HD.de", "score": 91,
      "source": {"id": 3, "name": "EPGShare DE"},
      "now":  {"title": "Zeit im Bild", "start": "…", "end": "…"},
      "next": {"title": "Wetter", "start": "…"}
    }
  ],
  "reading": false
}
```

The rules:

- **Only guides with a programme on now.** A guide with nothing on now, whether it was never
  read or read and found empty, is left out. This is the user's rule 5, and it is also what
  makes a choice safe: whatever is picked has a title on screen at once.
- **`current`** is the guide the channel is on, with its programme now, so the list can show
  "now on this channel's guide" next to the others. It is `null` for a channel on no guide.
  It is shown even when it holds nothing, because it is not a choice; it is what is being
  replaced. The app says "no information" for it in that case.
- **Which sources.** Every active EPG source, including the one the current guide comes from:
  the wrong entry on the right source is a common case. **Dummy sources are left out**, since
  they make programmes up from the channel's name and say nothing about which channel it is.
  An admin can limit the sources in the settings (§3.5).
- **How many.** The **20 best in total**, across all sources, by the same score
  `guide_candidates` gives (the Guides tab's ranking, only longer), current guide excluded.
  Candidates are taken in score order and those with nothing on now are skipped until 20
  with information are found, looking at the best **50** candidates of each step below.
  The user wants a long list to scroll through and find the right one, and almost never
  an empty one.
- **Widening (v217).** A list of maybes beats none, so until it holds 20 the list widens a
  step at a time (`WIDEN` in `app_guides.py`): (1) the Guides tab's matcher and matching
  settings **at any confidence** (the Guides tab itself stops at `MIN_GUIDE_SCORE`, 55);
  (2) **wide**: the same without the matching settings' limits (sources, tvg-id pattern,
  country must agree); the TV's own sources setting (§3.5) still holds; (3) a **plain
  search** on the words of the channel's name, then on its longest word alone. Each step
  adds below the ones before it, so the list stays best first. The app keeps asking every
  5 s while `reading` is true (at most a minute), adding rows as they are read.
- **Order.** By score, best first, whatever source each comes from.
- **`reading`.** If some of this channel's candidates have not had their programmes loaded
  yet (§4: a new channel, or the preload not finished), the server starts loading them in the
  background and says `"reading": true`. The list still holds only guides with information.
  The app may ask once more after about 10 seconds to see if more have arrived (§5.3).
- Time is UTC, ISO 8601, as everywhere else in the contract.

### 3.3 `POST /api/core/app-guide/`

```
POST /api/core/app-guide/
Authorization: Bearer <access token>
X-Dispatch-Device: <device id>
X-Dispatch-Device-Name: Living room SHIELD
Content-Type: application/json

{"channel": "0f1e…", "epg_id": 88213}
```

What the server does, in this order:

1. Refuse (403) when the switch is off. 404 when the login cannot see the channel, or when
   the guide no longer exists.
2. **Check the choice is one it would have offered:** the guide belongs to an active,
   non-dummy source and **holds a programme now**. If not, 409 with a message ("That guide has
   nothing on now; choose another"). The app must never be able to put a channel on an empty
   guide.
3. Put it on with `guide_manager.apply({channel.id: epg_id})`, which saves with
   `update_fields` (Dispatcharr's own signal then drops the cache and reads the programmes),
   honours the Guides tab's "copy tvg-id" setting, marks the channel as chosen, and takes it
   off the Guides tab's suggestions.
4. **Write who chose it** into that channel's `CHOSEN_KEY` entry (§3.4).
5. Log one line (`Guide: ┃AT┃ ORF 1 → ORF 1 HD (EPGShare DE), by alice on Living room SHIELD
   from 192.168.2.40`) so it shows on Diagnostics → Logs.
6. Answer with the channel's new guide and its programme now and next, in the same shape as
   `current` above, so the app can show the new title at once without waiting for its own EPG
   to reload:

```json
{"ok": true, "channel": {…}, "guide": {"epg_id": 88213, "name": "ORF 1 HD", "source": "EPGShare DE", "now": {…}, "next": {…}}}
```

Choosing the guide the channel is already on is not an error: it is answered 200 and
recorded the same way. It means "this one is right", and the Guides tab should stop suggesting
another one for that channel.

### 3.4 "Chosen by a user"

The Guides tab's `CHOSEN_KEY` entry today is `{"name", "epg", "at"}`. A choice from arrTV
adds who made it, and the guide it replaced:

```json
"1234": {
  "name": "┃AT┃ ORF 1",
  "epg": 88213,
  "at": "2026-09-27T17:41:02+00:00",
  "was": 5501,
  "by": {
    "via": "arrTV",
    "user_id": 7,
    "username": "alice",
    "device": "3f2a9c1e-0b7d-4e21-9a55-0f1c2d3e4f50",
    "device_name": "Living room SHIELD",
    "ip": "192.168.2.40"
  }
}
```

- `device` and `device_name` come from `X-Dispatch-Device` / `-Name`, checked the way
  `declared_device` checks them; missing or invalid means `null` and "unknown device".
- `ip` is `get_client_ip(request)`, the address as the server sees it (through the VPN it is
  the VPN's address, `192.168.65.x`; see HANDOVER §1).
- `was` is the guide it had before, or `null`. It makes an undo possible.
- Entries written by the Guides tab keep their current shape. Code reading `CHOSEN_KEY`
  (`settled`, the "Chosen already" view) must treat `by` and `was` as optional.

`guide_manager.apply()` writes the chosen entries itself (`settle`). Give it an optional
`by` / `was` to add, rather than writing the row a second time afterwards, so there is one
write and no moment where the entry lacks its author.

**Where an admin sees it:**

- **Channel Manager → Guides, "Chosen already"**: an arrTV choice says "chosen in arrTV by
  alice on Living room SHIELD (192.168.2.40), 27 Sep 17:41", beside what it replaced.
- **Settings → arrTV, "Guide changes"**: the arrTV choices, newest first, with the same
  information and **Put back**, which puts `was` back on the channel (through
  `guide_manager.apply`, so it is saved the proper way) and removes the entry. Everyone may
  change a guide, so an admin needs to be able to see and undo what was changed.

### 3.5 Settings on the arrTV page

Besides the switch, two optional settings, both saved in the same `app-integration` row:

- **Sources to offer** (`guide_choice_sources`, a list of EPG source ids; empty means every
  active non-dummy source). The same idea as the Guides tab's matching chips.
  *As built:* kept as comma-separated text in the `app-integration` row, like the other
  arrTV settings. The candidates also follow the Guides tab's own matching settings (sources
  matched against, tvg-id filter), since they come from its matcher: a source left out
  there is left out here too.
- **Keep programmes for the guides on offer** (§4). It is part of the feature, not a
  separate switch: with the feature on it runs, with it off it does not. There is nothing to
  set here except that it exists, so the page shows only its state ("Programmes loaded for
  4,812 guides, last 27 Sep 06:10") and a **Load now** button.

---

## 4. Every guide on offer has its information: preloading, not dummy channels

### 4.1 Why candidates are empty today

Dispatcharr reads a guide's programmes **only once a channel uses it**. On every refresh its
clean-up (`apps/epg/tasks.py`, around line 2080) deletes programmes of every guide entry no
channel uses. So the alternatives for a channel, which by definition no channel uses, hold
nothing. This is why the Guides tab says "not read yet" for most of what it suggests
(HANDOVER §5.6b).

### 4.2 Why not dummy channels

The user offered to add dummy channels to all the guides so their programmes would be loaded.
It would work, because a guide on any channel is read and kept. But it is not needed, and it
would cost a lot:

- There are about **36,000 guide entries** (HANDOVER §5.6). Each would need its own channel.
- Dummy channels are channels. They appear in the M3U and XMLTV output, in the HDHomeRun
  lineup Plex and Jellyfin read, in arrTV's channel list, in the Guide Layout and the
  Channel Manager, and in every count and check over channels. Hiding them everywhere means
  touching stock code in many places, which breaks "off means stock".
- Uninstalling the fork would leave them behind in the database.

**The fork already has what dummy channels would give: a way to keep programmes for guides
no channel uses.** The Guides tab reads a guide with `load_programmes` (one pass per source
file) and stock's clean-up is told to leave it alone through `kept_after_reading()`. This
feature uses the same two pieces, only ahead of time and for a chosen set of guides, not all
36,000.

### 4.3 What is preloaded

**For every channel, its best candidates**: the top **10** by score that `guide_candidates`
would offer, across all sources (the first part of the up to 40 §3.2 looks at, before the
"holds something now" filter). These are loaded ahead of time so the best matches are on the
list the moment it opens. Ranks 11 to 40 are loaded when the list is opened for that channel
(§4.5) and kept from then on: preloading 40 for every channel would approach all 36,000
entries. On
the user's 1,360 channels this is a few thousand distinct guides, many shared between
channels, instead of 36,000. Only these ever appear in the picker, so only these need
programmes.

*As built:* batches of 25 channels (`PRELOAD_BATCH_CHANNELS`), not `BATCH_CHANNELS`: each
channel is matched on its own through the picker's matcher, which scans the guide table per
channel. The guides already on a channel are not read (Dispatcharr reads those itself). The
record is `{"ids": {epg id: {"found", "at", "how"}}, "preloaded_at", "preloaded"}`; a full
preload drops the entries it no longer wants except those a viewer's list asked for (`how:
"asked"`).

A new Celery task, `tasks.preload_guide_choices`, **batched like `suggest_guides`**
(`BATCH_CHANNELS` channels per batch, each batch queues the next), because there is one
Celery worker and M3U and EPG refreshes must not wait behind it:

1. Batches of channels: work out each channel's candidates and collect their ids. Scoring
   reads the catalogue once per batch, as the Guides tab does.
2. When the channels are done: `load_programmes(ids)`, which reads each source's file once
   for every id wanted from it.
3. Record the ids read and found under a new `CoreSettings` key, `app-guide-kept`
   (`{epg id: {"found": true, "at": …}}`), and make `kept_after_reading()` return the union
   of the Guides tab's reads and these. The clean-up then leaves them alone.

**When it runs:**

- when the switch is turned on,
- after an EPG source finishes refreshing, for that source's guides (the refresh has just
  replaced the file, and the kept programmes are only as fresh as the last read), and
- from **Load now** on the settings page.

**When it stops:** the switch turned off clears `app-guide-kept`. The next refresh's
clean-up then removes those programmes as stock would, and the database is back to what stock
keeps.

### 4.4 What to measure before switching it on for good

Keeping programmes for a few thousand more guides adds rows to `ProgramData`. How many
depends on how many days the sources carry. Count `ProgramData` rows before and after the
first full preload on 192.168.2.142, and how long the preload and the next EPG refresh take,
and write the numbers into HANDOVER §6. If it is too much, lower the preloaded count per
channel (10 → 5) before anything else; the list itself stays at 20.

### 4.5 Candidates not loaded yet

Ranks 11 to 40 (not preloaded, §4.3), a channel added since the last preload,
or a preload still running: the GET starts
`load_programmes` for just this channel's candidates (in the background; a read goes through
the whole source file and can take several seconds) and answers at once with what already
has information, with `"reading": true`. Those ids go into `app-guide-kept` like the rest.

---

## 5. arrTV

### 5.1 Where

In the player's options (hold OK while a channel plays):

```
Send a report to the server
Wrong guide? Choose another          <- new, only when capabilities say "guide_choice": true
…
```

The report entry and its "What went wrong?" list are unchanged. Choosing "Wrong or missing
guide" there still only sends a report. The two are independent.

### 5.2 The screen

One screen, laid out for a remote:

- At the top, **what the channel's guide says now**: "Guide now: ORF1.at — Zeit im Bild
  (17:30–17:50)", or "Guide now: no information".
- Then the list, best match first (up to 20, one list across all sources). One row per
  guide: **the programme on now in large type** (that is what gets compared with the
  picture), and under it the guide's name, its source and the time it ends. The next
  programme may be shown smaller.
- The video keeps playing behind or beside the list, so the picture and the titles can be
  compared without leaving the channel.
- **Back** closes it without changing anything. There is no confirmation step: one press on a
  row chooses it (as the report does, because every extra step costs a remote press).
- An empty list (nothing with information): "No other guide has anything on for this channel
  right now." and Back.
- While `reading` is true, a small line at the bottom: "Looking for more guides…". Ask once
  more after 10 seconds and add what came. Do not ask repeatedly.

### 5.3 The calls

- Opening the screen: `GET /api/core/app-guide/?channel=<uuid>`, with the usual
  `Authorization` and `X-Dispatch-Device` / `-Name` headers.
- Choosing: `POST /api/core/app-guide/` with `{"channel": <uuid>, "epg_id": <id>}`.
  - **200**: close the screen, show "Guide changed to ORF 1 HD" for a few seconds, and put
    the `guide` from the answer into the player's now/next display at once.
  - **409**: show the message from the answer and ask for the list again (the guide ran out
    of programmes since the list was made).
  - **403**: the feature was switched off meanwhile; show "Changing the guide is switched off
    on the server" and remove the entry until capabilities are read again.
  - **404**: "That channel or guide no longer exists".
- **Reload the guide data for that channel.** The app's own EPG (from XMLTV or Xtream) still
  has the old guide's programmes. After a 200, reload that channel's programmes: Xtream
  `get_short_epg` / `get_simple_data_table` for the channel, or the XMLTV again if that is how
  the app loads it. Wait about 5 seconds before doing it, because Dispatcharr is loading the
  new guide's programmes in the background after the save. Until then the `guide` from the
  POST answer is what is shown.

### 5.4 For `fork/arrTV-integration.md`

Once built, this becomes a section there (after §7, reports) and a row in "Where arrTV
stands": server ready, app not built.

---

## 6. Tests

Server (`apps/channels/tests/` or `apps/proxy/live_proxy/tests/`, next to the other arrTV
tests), with a "Could Not Dispatch" fallback stream on every test channel as always:

- Switch off: GET and POST answer 403, `capabilities()` says `false`, no preload is queued,
  and `kept_after_reading()` is exactly what it was before (stock).
- GET lists only guides with a programme now; a candidate with none is left out; a dummy
  source is left out; at most 20 in total, best score first across sources, candidates with
  nothing on skipped (looking at most at 40); `current` present and shown even
  when it holds nothing.
- A login that cannot see the channel gets 404 for both.
- POST with a guide that holds nothing now → 409 and the channel is unchanged.
- POST puts the guide on through `guide_manager.apply` (the save uses `update_fields`, so the
  signal fires), leaves the channel's streams and their order untouched (fallback last), and
  writes `by` (user id, username, device, device name, IP) and `was` into `CHOSEN_KEY`.
- No device header: recorded with `device: null`, still works.
- The Guides tab does not suggest a guide for a channel chosen from arrTV, and its "Chosen
  already" view reads an entry with and without `by`.
- Put back restores `was` and removes the entry.
- Preload: batches queue the next; stops between batches when asked; reads each source once;
  records to `app-guide-kept`; the clean-up keeps those programmes; switching off clears the
  key.

Frontend (`npx vitest run`): the switch and its text on the arrTV page; the "Guide changes"
list and Put back; the "Chosen already" wording for an arrTV choice.

As always: the full backend suite (baseline `FAILED (errors=26)`, all `/data`
PermissionErrors), vitest green, commit, `fork/patcher/release.sh vNN`.

---

## 7. Decided, and still open

Decided by the user (§1): everyone may change a guide; a switch turns it off; it is a separate
entry under the report; choices are recorded with user, device and IP; nothing without
information is shown.

Chosen in this design, easy to change:

- 20 in total, best score first across all sources, like the Guides tab but longer (the
  user's choice); looking at up to 40 candidates to find 20 with something on; the top 10
  per channel preloaded, ranks 11 to 40 read when the list is opened.
- The current guide's own source is included.
- Dummy EPG sources are left out.
- Choosing the guide already on the channel counts as "this one is right".
- Settings → arrTV gets "Guide changes" with Put back.

Not in this design, possible later:

- Searching by typing. The list is only what the matcher finds, because typing with a remote
  is slow. If the right guide is not on the list, the report is still there.
- "No guide" as a choice (taking a wrong guide off without putting another on).
- A limit on how often one user or device may change guides.
