# Dispatch More
<p align="center">
<img width="200" height="200" alt="DispatchMore" src="https://github.com/user-attachments/assets/8c3a0b64-f10b-4463-8d91-e1beccfc5738" />
</p>

**An unofficial, modified build of [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)** that
adds faster channel switching, a Channel Manager for your whole lineup, diagnostics, recordings
that work on every install, and a lot more. It installs over the Dispatcharr you already have
and takes itself off again with one button.

> [!TIP]
> **Use it together with [arrTV](https://github.com/ckegels/AerioTV-Android)**, the Android TV
> app built for Dispatch More. Many features only work, or work much better, when both are used
> together: see [Better together with arrTV](#better-together-with-arrtv).

> [!WARNING]
> Dispatch More is **not made, reviewed or supported by the Dispatcharr developers.**
> Before reporting a problem, uninstall Dispatch More (Settings → System → Modified build →
> Uninstall) and try the same thing on stock Dispatcharr. **Do not report Dispatch More problems
> on the official Dispatcharr GitHub or Discord**; report them in
> [this repository's issues](../../issues).

This is what I wish Dispatcharr could be. Because of the size of the changes, and because a lot
of it was written with the help of AI, I did not try to add it to the official project. If you
test it, please share what you find: maybe some of these features can make it into Dispatcharr
itself one day.

**Contents:** [What it is](#what-it-is) · [Better together with arrTV](#better-together-with-arrtv) ·
[Features](#features) · [Install](#install) · [Install arrTV](#install-arrtv) ·
[Update](#update) · [Uninstall](#uninstall) · [Good to know](#good-to-know) ·
[Screenshots](#screenshots) · [License](#license-and-source)

## What it is

- **A layer over stock Dispatcharr.** It replaces a set of Dispatcharr's files and keeps the
  originals, so going back to stock is one button (or one command).
- **Safe to try.** It adds no database tables. Everything new is **off, or only suggests
  changes, until you switch it on or press Apply**. With its features off, Dispatcharr behaves as
  stock.
- **For Linux, LXC (including the Proxmox script) and Docker.** Each release is built for one
  Dispatcharr version; the installer picks the right one for you.

## Better together with arrTV

[arrTV](https://github.com/ckegels/AerioTV-Android) is an app for Google TV / Android TV (and
phones) that works with any Dispatcharr, but talks to Dispatch More directly. Using both is
**highly recommended**:

| With Dispatch More + arrTV | What you get |
|---|---|
| **Each TV is its own device** | Switching channels on one TV never cuts off another TV on the same provider login, and each TV gets streams it can actually play. |
| **Faster channel switching and failover** | A stream that does not start is replaced by the next one sooner, and a stuttering stream is reported so the server can switch. |
| **Report a problem from the TV** | One press on the remote sends what went wrong with a channel to the server, where you see it under Settings → arrTV. |
| **Wrong guide? Choose another** | From the player, pick the right guide for a channel by comparing what is on each with the picture; it changes for everyone. |
| **Instant updates** | New channels, Show Groups and guide changes reach the TV within seconds, without restarting the app. |
| **Recordings with ad skipping** | Commercial breaks marked on the server are skipped during playback; the recording itself is never cut. |
| **Away from home** | Viewers outside your home network get a quality your connection can carry. |

Every one of these has its own switch in Dispatch More (Settings → Streaming → arrTV) and in
arrTV (Settings → General → arrTV optimizations).

## Features

### Channels start faster

- **Channel Switch Overlap** (per provider account) — on a provider that allows one stream,
  switching channels no longer waits for the old stream to close: the new channel may use one
  extra connection for a few seconds.
- **Force Close on Identified Traffic** (per provider account) — when a player asks for a new
  channel, the one it was watching is closed straight away, so its connection is free at once.
  Only for players Dispatcharr can tell apart (see [Good to know](#good-to-know)).

### Channel Manager

A new page with everything for keeping a big lineup tidy. Nothing changes until you apply it.

- **Lineup** — merges every copy of a channel, from every provider and in every quality, into
  one channel, and suggests new streams as new channels (in the right group, on the next free
  number, with a logo and your fallback stream last). Each row shows what an apply adds.
  Optional: **Remember matched streams** (a stream the provider renames goes back on its
  channel) and **Dispatcharr's language model** (finds matches whose names differ, like
  "DE| DISCOVERY CHANNEL" and "┃DE┃ DISCOVERY").
- **Guides** — finds channels with no guide, an empty guide, or a clearly better guide, and
  shows what is on each guide now so you can pick the right one.
- **Guide Layout** — drag channels into order, group by group; the numbers follow.
- **Logos** — logos from public collections, your playlists and your guides, side by side.
- **Stream Check** — finds streams that no longer play (dead, refused, black, frozen, or the
  provider's "no stream" picture). It never touches a provider someone is watching, and can put
  dead streams aside automatically.
- **Show Groups** — groups by what is on right now: Cooking, Travel, Movies, Documentaries,
  Sport, Kids and more, plus groups you add. A group holds every channel airing that kind of
  show and lets it go when the show ends (never while someone watches). Channels can be kept in
  a group for good. Uses your guides and, optionally, TVmaze, Wikidata, Wikipedia, TMDB,
  TheTVDB, Trakt and OMDb to recognise shows.
- **EPG Grabber** — runs iptv-org's guide grabber on a schedule (when it is installed on the
  server) and only replaces your guide once the new one is complete.

### Recordings

- **Recordings work on Linux installs.** Stock Dispatcharr never starts a recording on a
  Linux/LXC install (only Docker has the worker for it). Dispatch More fixes that and adds a
  worker of its own for recordings, up to 20 at once.
- **Commercial breaks marked, not cut.** With Comskip in *Mark* mode, the breaks are stored with
  the recording so arrTV can skip them; nothing is cut out, so a wrong guess costs nothing.
- **Comskip when the recording asks for it**, even with the server-wide switch off.

### Diagnostics

- **Channel starts and switches** — where the time goes when a channel starts.
- **Channel health** — what each running channel plays, from where, to whom, and how stopped
  ones ended.
- **Logs** — every log Dispatcharr writes, filtered and downloadable.
- **Memory** — how much memory each part of Dispatcharr uses.

### And more

- **Media Servers** — Plex and Jellyfin in one place: who watches what, stop a session, and
  manage Dispatcharr's HDHomeRun tuners and guides on them. Plex and Jellyfin no longer keep
  streams open too long.
- **Service keys** (Settings → System) — keys for TMDB, TheTVDB, Trakt and OMDb in one place,
  with a Test button.
- **Modified build** (Settings → System) — what is installed, and the button back to stock.

## Install

> [!IMPORTANT]
> **Make a backup first:** Dispatcharr → Settings → Backup & Restore.

Dispatch More needs a working Dispatcharr. The installer checks your version and **changes
nothing** if no release was built for it.

### Linux or LXC (Debian install, Proxmox script)

Run on the Dispatcharr machine:

```bash
curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash
```

### Docker

Run on the Docker host (use your container's name instead of `dispatcharr`):

```bash
docker exec dispatcharr bash -c "curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | bash" && docker restart dispatcharr
```

A **restart** keeps Dispatch More, but **recreating** the container (a new image, or a changed
compose file) brings back stock Dispatcharr. To have it put back at every start, add this to
the Dispatcharr service in `docker-compose.yml`:

```yaml
    entrypoint: ["/bin/bash", "/data/dispatch-more/docker-entrypoint.sh"]
```

With separate Celery containers, give them the same `entrypoint` and
`environment: DISPATCHARR_ENTRYPOINT=/app/docker/entrypoint.celery.sh`.

### What the installer does

1. Finds your Dispatcharr and its version, and downloads the Dispatch More release made for it.
2. Checks that every file it replaces is the stock one, and **stops** if one is not.
3. Keeps the originals, installs the new files (the web page comes ready-built, so nothing is
   compiled on your server) and restarts Dispatcharr.
4. On Linux only: adds a worker for recordings (`dispatcharr-celery-dvr`). Leave it out with
   `--no-dvr-worker`.

### By hand

Download `dispatch-more-<release>-dispatcharr-<version>.tar.gz` from [Releases](../../releases),
unpack it and run `sudo bash dispatch-more/install.sh` (add `--app /path/to/dispatcharr` if
Dispatcharr is not in `/opt/dispatcharr`).

### After installing

Open Dispatcharr: **Settings → System → Modified build** says which release is installed. New
pages: **Channel Manager** in the menu, **Diagnostics** and **arrTV** under Settings →
Streaming. Switch on what you want to use; the rest stays off.

## Install arrTV

On a Google TV / Android TV:

1. Install **Downloader** (by AFTVnews) from the Play Store.
2. Enter `https://github.com/ckegels/AerioTV-Android/releases/latest/download/ArrTV.apk` and
   install the app (allow Downloader to install apps when asked).
3. Open arrTV. It finds Dispatcharr on your network by itself; pick it and log in.
4. In Dispatch More, switch on what you want under **Settings → Streaming → arrTV**.

Phones, tablets and other ways to install are in the
[arrTV instructions](https://github.com/ckegels/AerioTV-Android#install).

## Update

Run the same install command again: it replaces the installed release with the newest one.
arrTV updates itself (Settings › App Updates).

**A new Dispatcharr version:** a Dispatch More release only fits the Dispatcharr version it was
built for. When Dispatcharr updates, a new Dispatch More release follows. Until then, keep your
Dispatcharr version, or uninstall Dispatch More first and update to stock.

## Uninstall

- **From the page:** Settings → System → Modified build → *Uninstall and go back to stock
  Dispatcharr*. On Linux it happens right away; in Docker at the next restart of the container.
- **By hand (Linux):** `sudo bash /var/lib/dispatch-more/uninstall.sh`
- **Docker:** recreate the container (`docker compose up -d --force-recreate`), without the
  `entrypoint` line if you added it.

Every file is put back as it was. Your channels, streams and settings stay; stock Dispatcharr
ignores the settings only Dispatch More uses.

## Good to know

- **Telling devices apart.** Force Close and the per-TV features need each device to be
  recognisable. arrTV identifies itself; other players are told apart by their address, so on
  your home network add your local network in the provider account's settings, and away from
  home give each device its own login.
- **One Dispatcharr version per release.** See [Update](#update).
- **AI-assisted.** Much of the code was written with the help of AI and tested on one large
  setup. Report what does not work in [the issues](../../issues).

## Screenshots

<div align="center">
<img width="1588" height="1089" alt="Screenshot_20260919_180716" src="https://github.com/user-attachments/assets/1289637a-7385-4ec4-ae0c-3fc35ef7b1e0" />
<img width="1217" height="709" alt="Screenshot_20260919_180638" src="https://github.com/user-attachments/assets/5a6fe98f-bd69-4c95-86ba-669b483c23e5" />
<img width="1323" height="1351" alt="Screenshot_20260919_180445" src="https://github.com/user-attachments/assets/4d8c196a-221f-4891-b3e7-decd1af62802" />
<img width="1136" height="1339" alt="Screenshot_20260919_180332" src="https://github.com/user-attachments/assets/03b597f7-26fc-4852-9173-ebe21297b1aa" />
<img width="917" height="1011" alt="Screenshot_20260919_180252" src="https://github.com/user-attachments/assets/0c1a4038-f12e-43b4-959c-51eed60ad6ba" />
<img width="2558" height="1347" alt="Screenshot_20260919_180216" src="https://github.com/user-attachments/assets/b5f287db-c63b-4f1a-87f7-5f53490e0fd8" />
</div>

## License and source

Dispatcharr is licensed under the [GNU AGPL v3](LICENSE), and so is Dispatch More. This
repository is the complete source: the `feature/probation-slots` branch is Dispatcharr's code
with Dispatch More's changes, each explained in its commit message. The installed build names
this repository under Settings → System → Modified build.

For people working on it: [`fork/HANDOVER.md`](fork/HANDOVER.md) and [`CLAUDE.md`](CLAUDE.md).
