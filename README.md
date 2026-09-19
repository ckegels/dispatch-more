# Dispatch More
<p align="center">
<img width="200" height="200" alt="DispatchMore" src="https://github.com/user-attachments/assets/8c3a0b64-f10b-4463-8d91-e1beccfc5738" />
</p>



**An unofficial, modified build of [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).**
It is not made, reviewed or supported by the Dispatcharr developers.

> **Before reporting any problem:** uninstall Dispatch More (Settings → System → Modified
> build → Uninstall) and try the same thing on stock Dispatcharr. **Do not report problems
> with this build on the official Dispatcharr GitHub or Discord** — only problems that also
> happen on stock Dispatcharr belong there. Problems with Dispatch More go to
> [this repository's issues](../../issues).

> This is what i Wish Dispatcharr could be, however due to the big amount of changes and the heavy usage of llms i decided to not try and add it to the official repo.
> if people could test this and tell me their findings and maybe some of these features could be added to the official repo. 

Dispatch More installs over an existing Dispatcharr, on Linux, in an LXC or in Docker, and
takes itself off again with one button. It changes no database tables, and with its features
switched off Dispatcharr behaves as stock.
## What issues it fixes

- **Media servers** — When using a media server like plex or jellyfin they would leave open streams too long, with the channel overlap on this is solved. It also adds one place where all your media server are managed, so you can add and delete tuners.
- **channel loading times** — When only one provider is available streams take very long to load because it has to close the old stream and reopen the new one, this recognizes streams by login and allows streams to be closed immediately and allows channels to overlap if allowed by the provider.
- **Diagnostics and Logs** — Dispatcharr doesn't have an easy way to see at a glance what is going wrong and why things take long, the diagnostics page aims to resolve that.
- **Find Logo** — Finding and adding logos to channels is not easy, the new Find logos tab inside the logo Manager uses collections and epg sources to make this easier.
- **Channel Manager** — There is already plugins that help with merging and creating channels however these have no easy way of seeing exactly what is happening, this should give you all the tools you need.
  
## What it adds

- **Channel Switch Overlap** — switching channels on an account that allows one stream no
  longer waits for the old stream to let go (per M3U account, off by default).
- **Media Servers** — Plex and Jellyfin: who is watching what, stop a session, and manage
  Dispatcharr's HDHomeRun tuners and guides on the server.
- **Stream Recovery** and **Channel health** — a provider rotating a working connection is not
  a failure; what every running channel is doing, and how stopped ones ended.
- **Diagnostics** — where the time goes when a channel starts.
- **Find Logos** — logos from public collections, your playlists and your guides, side by side.
- **Channel Manager** — merge the same channel from every provider and quality into one
  (matching the way DispatcharrUtils does by default), and **Stream Check**: finds the streams
  on your channels that no longer play — dead, refused, black, frozen, or showing the
  provider's "no stream" picture — without ever touching a provider someone is watching.

## Disadvantages
- **Registered Devices** — For this to work each device has to be unique, so each device needs its own login when external, and on local network it will use the ip adress to recognize the device. (you have to add the local lan in the m3u settings)



## Install

Each release is built for one Dispatcharr version, and its installer refuses any other: on a
Dispatcharr it was not built for, nothing is changed. Download the release for **your**
Dispatcharr version from [Releases](../../releases).

### One command

On **Linux or an LXC**:

```bash
curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | sudo bash
```

In **Docker** (use your container's name instead of `dispatcharr`):

```bash
docker exec dispatcharr bash -c "curl -fsSL https://github.com/ckegels/dispatch-more/releases/latest/download/quick-install.sh | bash" && docker restart dispatcharr
```

It finds which Dispatcharr you have, downloads the Dispatch More release made for it, checks that
every file it replaces is the stock one (and stops if not), keeps the originals, and installs --
backend and an already-built frontend, so nothing is built on your server.

In Docker, a restart keeps it, but **recreating** the container (a new image, or a changed
compose file) starts stock Dispatcharr again: run the command again then. Or have it put back
at every start, by adding this to the Dispatcharr container in `docker-compose.yml`:

```yaml
    entrypoint: ["/bin/bash", "/data/dispatch-more/docker-entrypoint.sh"]
```

With separate Celery containers, give them the same `entrypoint` and
`environment: DISPATCHARR_ENTRYPOINT=/app/docker/entrypoint.celery.sh`.

### By hand

Download `dispatch-more-<release>-dispatcharr-<version>.tar.gz` from [Releases](../../releases)
and run `sudo bash dispatch-more/install.sh` (add `--app /path/to/dispatcharr` if it is not in
`/opt/dispatcharr`).

## Uninstall

- **From the page:** Settings → System → Modified build → *Uninstall and go back to stock
  Dispatcharr*. On Linux it happens right away; in Docker on the next restart of the container.
- **By hand:** `sudo bash /var/lib/dispatch-more/uninstall.sh` (Linux). In Docker, recreate the
  container (`docker compose up -d --force-recreate`), without the `entrypoint` line if you added
  it: a new container is stock Dispatcharr.

Every file is put back as it was. Your channels, streams and settings stay: stock Dispatcharr
simply ignores the settings only Dispatch More uses.

## New Dispatcharr versions

A release only fits the Dispatcharr version it was built for. When Dispatcharr releases a new
version, this repository's automation tries the changes against it and reports whether they
still apply; a new release follows. Until then, keep the Dispatcharr version you have, or
uninstall first and update to stock.

## License and source

Dispatcharr is licensed under the [GNU AGPL v3](LICENSE), and so is Dispatch More. This
repository is the complete source: the `feature/probation-slots` branch is Dispatcharr's code
with Dispatch More's changes, every one explained in its commit message. The installed build
names this repository under Settings → System → Modified build.

For people working on it: [`fork/HANDOVER.md`](fork/HANDOVER.md) and [`CLAUDE.md`](CLAUDE.md).
