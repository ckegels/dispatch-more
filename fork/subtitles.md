# Subtitles: what channels carry, captions made from the sound, and translation

Designed 2026-10-01, against release v242. **Step 1 (§2 and the tab's list, §3.1) is built in
v243; step 2 (teletext) in arrTV arr.72; the first half of step 3 -- the caption worker, what it
finds out and measures, the proposal and the installer (§5b.1-§5b.3, §5b.6) -- in v244;
captions while a TV watches, delivered to arrTV and shown in step with the picture (3b) -- in
v247 and arrTV arr.75.** Translation is built in v248 (§9 step 4); the per-channel setting and the finer
points of keeping up are not built yet. This is the hand-over for
the work: what the user asked for, how it fits Stream Check, the Channel Manager and arrTV,
the models, and what is still to decide (§8). Read `fork/HANDOVER.md` (§2, §3, §5.7, §5.11)
and `fork/arrTV-integration.md` alongside it.

---

## 1. What the user wants

- **Stream Check finds out which streams carry subtitles**, the way it now records resolution,
  codec, frame rate and audio (v240). No extra connection: it is part of the read a check
  already does.
- **A new Channel Manager tab, Subtitles**, like the others: every channel in a list, what it
  carries, and every setting for it on the row.
- **Captions made from the sound** for channels that carry none, with **many models to choose
  from** and switch between, on whatever hardware the server has: a GPU where there is one
  (the user's: an RTX card), the CPU where there is not, a cloud service as a last resort.
- **Translation**: someone watching in arrTV picks the language they want to read.
- Languages the user watches: **English, German, Italian, French, Dutch**. Everything below
  covers all five.

As always: **off means stock.** Every part has a switch, off by default; with it off nothing is
probed beyond what v240 does, nothing runs, nothing is sent, and the tab says what it would do.

---

## 2. Stream Check: which subtitles a stream carries

`_ffprobe` (`apps/channels/stream_check.py`) asks ffprobe for `codec_type,codec_name,...` and
keeps the first video and the first audio stream. Subtitle tracks are already in that answer;
they are thrown away. Asking for three things more costs nothing:

- `stream_tags=language` and `stream_disposition=hearing_impaired` on every stream,
- ~~`closed_captions` on the video stream~~ -- **measured useless** (2026-10-01, NBC 56, 1.5 MB
  read): ffprobe left it empty while 107 frames carried CEA-608/708 data. Captions are found
  instead by a byte search for their ATSC A/53 marker, `GA94` + user data type `03` (cc_data):
  the same 107 hits, for no decoding at all; two hits at least (`CC_MARKER`, `CC_LEAST`).

Kept in `Stream.stream_stats` beside v240's fields, so Dispatcharr's Stats page and the new tab
read it from one place:

```json
"subtitles": [
  {"kind": "teletext", "lang": "deu", "hearing_impaired": false},
  {"kind": "dvb",      "lang": "eng", "hearing_impaired": true},
  {"kind": "cc",       "lang": ""}
],
"audio_languages": ["deu", "eng"],
"subtitles_checked_at": "2026-10-01T20:15:00Z"
```

`kind`: `teletext` (`dvb_teletext`), `dvb` (`dvb_subtitle`, pictures), `cc` (closed captions in
the video), `text` (`webvtt`/`mov_text`, HLS sources). `[]` means "looked and found none", which
is different from the key missing ("never looked").

**What ffprobe cannot say: whether a teletext track has a subtitle page.** It lists the track
and its language. Which pages it holds (150, 777, 888…) is in the teletext data itself. Two
ways to know, both later and optional:
- the PMT's teletext descriptor gives a type per page (type 2 / 5 = subtitles); ffmpeg's mpegts
  demuxer reads it but does not show it, so it would be a small parse of our own over the bytes
  read;
- ffmpeg with libzvbi (`-txt_page subtitle`) decodes the pages. Whether the server's ffmpeg has
  libzvbi is to be checked; Dispatcharr's own does not need it.
Until then the tab says "Teletext (deu) -- subtitle pages not known", which is honest.

The quick check reads `READ_BYTES` (1 MB). That holds the PMT (where every track is listed) many
times over; `closed_captions` needs a decoded picture, which ffprobe does on 1 MB already when
it finds the resolution. So subtitles come with every check, quick or full.

**Rules kept:** nothing about subtitles ever counts against a stream (a stream without subtitles
plays fine); no connection is opened for it; the threads still do not touch the database (the
result rides back in `outcome` like v240's `details`); `save_stream_info` stays the one writer.
Switch: `save_stream_info` already exists and is on; subtitles ride along with it.

---

## 3. The Subtitles tab (Channel Manager)

Eighth tab, after Show Groups: `subtitles.py` + `subtitles_views.py` + `SubtitlesTable.jsx`,
the same layout as the others. Show Groups' copies are left out (`copy_group_ids()`, handover
§7b).

### 3.1 The list

One row per channel, grouped like the Lineup:

| Column | What it shows |
|---|---|
| Channel | name, number, group |
| Spoken | the channel's language: the audio track's tag if it has one, else the country in its name or group (`┃DE┃`), else its guide's language; editable (a German channel with an English tag is common) |
| Broadcast subtitles | badges from the best stream that has them: **Teletext deu**, **DVB eng (HI)**, **CC**; "none found" or "not checked yet"; streams differ, so a row opens to show each stream's |
| Captions | the channel's setting (§3.2) |
| Model | the model it uses, or "default" |
| Translate | allowed or not |
| Last made | when captions were last made for it, and how long it took per minute of sound |

Filters: has broadcast subtitles / has none / not checked; spoken language; group; captions on.
Bulk: set a whole group (or the filtered rows) at once. Nothing is written until Apply, as on the
other tabs.

### 3.2 A channel's caption setting

- **Off**: nothing for this channel.
- **Broadcast only** (default once the feature is on): what the channel carries, nothing made.
- **Make them when the channel has none**: broadcast subtitles when a stream has them, captions
  made from the sound when it does not (or when the stream playing has none).
- **Always make them**: for channels whose broadcast subtitles are poor or late.

### 3.3 Settings card (top of the tab)

- **Make captions with:** Off / this server's processor (CPU) / a caption worker (URL, §5) /
  a cloud service (key).
- **Models:** the list of §6 with a switch each. A switched-on model can be picked as the
  default or for a channel; the worker says which it has downloaded and offers the download.
- **Translate with:** Off / the worker's model / Ollama (URL; its installed models are listed
  from `/api/tags`, so every model the user pulls appears here by itself) / DeepL / Google /
  LibreTranslate.
- **Languages TVs may ask for:** the five by default (en, de, it, fr, nl).
- **At most N channels at once** (default 2 on a GPU, 1 on a CPU): the cap that keeps the
  server and the worker from being swamped.
- What is running now: which channels have captions being made, for whom, at what speed.

---

## 4. How captions are made and delivered

### 4.1 Only while somebody wants them

Captions are made **only for a channel that is being watched by a TV that has asked for them**
(§7), and stop 30 s after the last such TV leaves. A channel nobody watches costs nothing.

### 4.2 The sound comes from the buffer, not the provider

The server already holds every channel being watched in its Redis buffer (that is how a second
viewer joins without a second provider connection). The captioner reads **the same buffer, as
one more client** -- never a new provider connection. This keeps handover rule 6: it cannot cost
anyone their stream, and it uses no connection slot.

### 4.3 Timing

A speech model does not listen continuously. The sound is cut where nobody speaks (Silero VAD),
each piece of speech goes to the model, and the text comes back with times **on the stream's own
clock (PTS)**. arrTV shows each line when its picture plays, wherever the TV is: live, a few
seconds behind, or paused in Live Rewind. Captions are 2-4 s behind live; arrTV covers that by
playing that much behind live while captions are on (the Live Rewind buffer can already).

### 4.4 To arrTV

- `GET /api/core/app-captions/?channel=<uuid>&lang=<xx>&after=<pts>` returns the lines since
  `after` (and is how the TV says it wants them);
- or the socket arrTV already has open: `{"type": "captions", "channel", "lang", "lines": [...]}`
  (lighter; the GET stays for players without the socket).
- Broadcast subtitles need none of this: they are in the stream, arrTV decodes them itself (§7).

### 4.5 Kept or not

Lines are kept in Redis for the depth of a Live Rewind buffer (default 30 min) so a TV that
rewinds still has them. Recordings: captions could be made afterwards, at leisure, and stored
beside the file as WebVTT -- later, not in the first build.

---

## 5. Where the models run: the caption worker

Models do not run inside Dispatcharr's web or Celery workers (v241 moved the Lineup's language
model into a process of its own for the same reason: memory, and a crash taking the server
down). They run in **a caption worker**: one small service with one HTTP interface, run either

- **on the Dispatcharr server** ("this server's processor"): the same worker started locally,
  CPU models only, or
- **on a machine with a GPU** (a Docker container with the NVIDIA runtime, or a Python venv),
  given to Dispatcharr as a URL.

Dispatcharr sends the worker the sound (16 kHz mono PCM pieces, after VAD) and gets text back;
the worker never talks to a provider and needs no access to the database. One interface means
the cloud services sit behind the same calls, and a model can be added without touching
Dispatcharr.

The worker reports what it has: device (GPU name, VRAM free), models downloaded, speed per model
measured on a test clip. The tab shows it.

**Not everybody has a GPU**, and that is the reason for the three-way choice: a server with a
GPU somewhere makes the best captions; a server with only a CPU still gets usable ones from the
small models; a weak one can use a cloud key or stay on broadcast subtitles only.

---

## 5b. It fits each server, not one (the user, 2026-10-01)

"This has to work on more than just my build: make it dynamic, depending on the person's build
and needs." Nothing below may assume the user's GPU. The rule: **the server finds out what it
has, measures what it can do, proposes a setup that fits, and lets the owner change it.**
Broadcast subtitles (§2, teletext in arrTV) need none of this and work everywhere.

### 5b.1 What the worker finds out (at start, and on "Look again")

- **GPU:** NVIDIA through CUDA (name, memory total and free, driver/CUDA version); Intel and AMD
  graphics are listed but used only by runtimes that support them (whisper.cpp with Vulkan or
  OpenVINO), never assumed.
- **CPU:** cores, AVX2/AVX-512 (CTranslate2 and whisper.cpp are several times faster with
  them), architecture (x86-64 or ARM: a Raspberry Pi gets Vosk or nothing).
- **Memory and disk:** RAM free, and room for models (a model is 40 MB to 3 GB).
- **What is already there:** models downloaded, an Ollama on this machine or a URL (its
  installed models from `/api/tags`), service keys for cloud speech or translation (Settings ->
  Service keys gains Deepgram / OpenAI / DeepL / Google).
- **Other workers:** any number of caption workers by URL (a GPU PC beside a small server); each
  reports the above for itself.

### 5b.2 What it measures

Every model that fits is **timed on a 30-second test clip** (shipped with the worker, speech with
some music under it): its real-time factor (seconds of work per second of sound) and its memory.
From that, per model, **how many channels it can caption at once** = what keeps up with live
speech with a margin (`floor(0.6 / RTF)`), limited by memory. Measured, not guessed from the card's
name: two "8 GB" cards or two "8-core" CPUs can differ twofold. Measured again when the hardware
or the models change, or on request.

### 5b.3 What it proposes (the tab says it in words; the owner can change all of it)

| What the server has | Speech to text | Translation | At once (typical) |
|---|---|---|---|
| NVIDIA, 10 GB or more | Whisper large-v3-turbo | Ollama 8B if Ollama is there, else Opus-MT | 3-6 |
| NVIDIA, 4-8 GB | Whisper turbo int8, or medium | Opus-MT (Ollama 3-4B if wanted) | 2-3 |
| NVIDIA, under 4 GB | Whisper small | Opus-MT on the CPU | 1-2 |
| CPU, 8+ cores with AVX2 | Whisper small int8 | Opus-MT | 1-2 |
| CPU, 4 cores | Whisper base int8 | Opus-MT | 1 |
| Weak or ARM CPU | Vosk (one model per language) | none, or a DeepL key | 1 |
| A cloud key only | the cloud service | DeepL / Google | as the plan allows |
| None of these | -- broadcast subtitles only -- | | |

The numbers in the last column are what the measurement replaces; the table only says what is
offered first. Parakeet v3 (NVIDIA, NeMo) is offered where it is installed, for many channels.

### 5b.4 Fitted to what the person needs

- **The languages they watch** (picked on the tab; their channels' spoken languages are
  suggested): Vosk downloads only those, Opus-MT only the pairs between those and the languages
  their TVs ask for, Whisper needs nothing extra.
- **How many channels at once**, up to what was measured.
- **Quality or more channels**: one choice that moves to a bigger or smaller model.
- **Per channel** (§3.2): which get captions at all, so the budget goes where it is wanted.

### 5b.5 When it cannot keep up

The worker reports how far behind live each caption job is. Past 6 s: new channels get the next
smaller model; past 15 s, or when every slot is in use, a TV asking for captions is told "captions
are busy" (shown in arrTV) rather than getting them a minute late. A worker that stops answering
is noticed within seconds and the next one (or broadcast subtitles only) takes over. Nothing of
this ever touches the stream itself.

### 5b.6 Installed only by those who want it

The caption worker is **not** part of a normal Dispatch More install (it is hundreds of MB of
Python packages plus models). The tab offers it when captions are switched on:
- **Linux/LXC:** a button installs it in a virtualenv of its own beside Dispatcharr (CUDA
  packages only when an NVIDIA GPU was found) and runs it as a service; models are downloaded
  when picked, with their size shown first.
- **Docker:** an optional second container (`dispatch-more-captions`, with the NVIDIA runtime when
  there is a GPU), its address given on the tab.
- **Anywhere else:** the same container or venv on another machine, by URL.
Uninstalling Dispatch More removes the worker it installed and its models.

## 6. The models

All of these cover English, German, Italian, French and Dutch. Model versions move fast
(NVIDIA's especially): check the current release of each before building.

### 6.1 Speech to text

| Model | Runtime | Hardware | Notes |
|---|---|---|---|
| Whisper large-v3-turbo | faster-whisper | GPU, ~3 GB | Default on a GPU. Detects the language itself. Not trained to translate. |
| Whisper large-v3 | faster-whisper | GPU, ~5 GB | Slower; translates any language into **English** in the same pass. |
| Whisper medium | faster-whisper | GPU or strong CPU | Middle ground. |
| Whisper small | faster-whisper int8 / whisper.cpp | CPU, 4-8 cores | Default on a CPU: about one channel in real time. Weaker on dialect and noise. |
| Whisper base / tiny | whisper.cpp | weak CPU | Last resort; mistakes are frequent. |
| Parakeet TDT 0.6B v3 | NVIDIA NeMo | GPU | 25 European languages, much faster than Whisper: the pick for many channels at once. |
| Canary 1B | NVIDIA NeMo | GPU | Transcribes and translates to/from English in one model. |
| Vosk small (per language) | Vosk | any CPU, even a Pi | One model per language (the channel's "Spoken" decides). Lowest quality. |
| Deepgram / Google / OpenAI | cloud | none | Paid per minute of sound, and sends what is watched to a third party. |

### 6.2 Translation

| Model | Runtime | Hardware | Notes |
|---|---|---|---|
| Whisper large-v3 (translate) | faster-whisper | GPU | Into English only, no second model. |
| Ollama: Qwen / Llama 3.x 8B, Gemma, Mistral NeMo 12B | Ollama | GPU, 5-8 GB | Best: any pair directly, understands context. Mistral is strong in FR/IT/DE. Any model pulled into Ollama is offered. |
| Opus-MT (Helsinki-NLP) | CTranslate2 | CPU | One small model per pair; through English where a pair is missing. Default on a CPU. |
| NLLB-200 600M / 1.3B | CTranslate2 | GPU or strong CPU | Good, but a **non-commercial licence**: fine for the user's own server, not for shipping as a Dispatch More default. |
| Argos / LibreTranslate | own server | CPU | Offline, simple, a step below Opus-MT. |
| DeepL / Google | cloud | none | Top quality, cheap for subtitle text, paid and online. |

On a 10-12 GB card, Whisper turbo (~3 GB) and an 8B model at 4 bits (~5 GB) fit together; a
12B leaves little room.

---

## 7. Translation, and arrTV

### 7.1 Who chooses the language

**Each TV.** arrTV gets Settings → Player → **Subtitles**:
- **Show subtitles:** Off / When the channel has them / Always (makes captions where allowed).
- **Language:** the TV's own language by default, or any of the languages the server offers.
- **When the programme is in another language:** Original / Translated / Both (two lines).

The TV sends its choice with the request (§4.4: `lang=`), so two TVs on one login can read two
languages. The player's options row gets a **Subtitles** shortcut to switch on the spot.

### 7.2 What is translated

One translation per (channel, language), **shared** by every TV reading it: three TVs watching
ORF 1 in Dutch cost one translation. The text translated is, in order:
1. broadcast subtitles that are text (teletext, CC) -- the server decodes them for this
   (ffmpeg's teletext decoder needs libzvbi, §2);
2. otherwise the captions made from the sound.
DVB subtitles are pictures: translating them would need OCR first. Not in the first build; such
a channel gets captions from its sound instead when translation is asked for.

A programme already in the chosen language is not translated: its own subtitles (or captions)
are shown.

### 7.3 What arrTV must build

- **A teletext subtitle decoder.** Media3 has none (it plays DVB subtitles and CEA-608/708
  already). This alone gives subtitles on most German, Austrian, Dutch, Belgian and Italian
  channels, with no server work and no delay. It is worth doing first, whatever else is decided.
- The Subtitles settings above, and showing server captions as a text track timed on PTS.
- Asking for captions (`app-captions`, or the socket message) only while they are on.

---

## 8. Still to decide

1. ~~**Where the GPU is.**~~ **Decided 2026-10-01: the Proxmox host; the Dispatcharr LXC already
   has access to it.** So the caption worker can run beside Dispatcharr in the same LXC.
   Previously: The user mentioned an RTX 3060; the PC this was designed on has an
   RTX 3080 (10 GB). The worker must run on a machine that is on whenever someone watches: the
   Proxmox host (GPU passed through to an LXC or a VM) is ideal, a desktop that sleeps is not.
2. ~~**Teletext first in arrTV**~~ -- done (arr.72).
3. **First server step:** §2 + the tab's list (§3.1), with nothing made yet -- it shows what the
   channels carry, which decides how much of §4-§6 is needed at all.

## 9. Order of work

1. ~~Stream Check records subtitles (§2); the tab lists them (§3.1).~~ **Done in v243**:
   `stream_check.subtitle_details` (in every check's ffprobe, plus the CC marker search),
   `apps/channels/subtitles.py` + `subtitles_views.py` (`GET/PUT /api/channels/subtitles/`),
   `SubtitlesTable.jsx` (the eighth tab). The spoken language is the audio track's tag, else
   the country in the name or group (`COUNTRY_LANGUAGE`; Belgium is guessed Dutch), else not
   known, and can be set per channel (CoreSettings `subtitles`.spoken). Checked on real samples:
   NPO 1 = teletext dut + sound dut, NBC 56 = CC. The caption columns of §3.1 (setting, model,
   translate, last made) come with step 3. Filled as Stream Check runs: until a stream has been
   checked again it says "not checked yet".
2. ~~arrTV: teletext subtitles (§7.3).~~ **Built in arrTV arr.72** (branch `feature/teletext`, see
   arrTV's `.personal/CHANGES.md` §3k): each subtitle page the PMT names is a text track; checked
   against libzvbi on a minute of NPO 1. Teletext needs nothing from the server.
3. The caption worker with faster-whisper (§5, §6.1) **starting with what it finds out and
   measures (§5b.1-§5b.3)**, delivery to arrTV (§4), the tab's settings (§3.2-§3.3, §5b.4),
   arrTV's Subtitles settings (§7.1), keeping up (§5b.5), and its installer (§5b.6).
   - **3a, done in v244: the worker, measuring, the proposal, the installer.**
     - `apps/channels/captions/worker.py`: standalone (stdlib HTTP, no Django, no database),
       run by its own Python with faster-whisper. `GET /status` (NVIDIA cards via `nvidia-smi`,
       CPU threads/AVX2/arch from `/proc/cpuinfo`, memory, disk, which models are downloaded,
       whether CTranslate2 sees CUDA); `POST /look`, `/download?model=`, `/benchmark?model=`
       (background; progress in `/status`), `/transcribe?model=&offset=&language=` (16 kHz mono
       PCM or WAV -> segments with times plus the offset; VAD on). Optional `X-Worker-Token`.
       CUDA float16 on a card, int8 on the CPU; a card whose CUDA libraries fail falls back to
       the CPU (`cuda_failed` in `/status`). NVIDIA's pip libraries (cuBLAS, cuDNN 9) are put
       on `LD_LIBRARY_PATH` by the worker itself (it starts itself again once), so neither the
       installer nor Docker has to.
     - Measuring: the faster-whisper project's public-domain JFK clip, three times (33 s),
       after a warm-up; `channels = min(floor(0.6 / RTF), memory free / model memory)`.
       Checked on an RTX 3080: tiny 0.034 RTF = 17 channels; Dutch NPO 1 sound recognised as
       `nl` (0.97).
     - `apps/channels/captions/manager.py`: CoreSettings `captions` (`enabled` false,
       `worker_url` "" = 127.0.0.1:9725, or `dispatch-more-captions:9725` in Docker, `token`,
       `model`, `channels_at_once` 2, `quality` balanced/best/channels, `languages`,
       `ollama_url`). `guess()` is the §5b.3 table; `propose()` replaces it with measurements
       (largest measured model that keeps up with the channels asked for, +1 for
       "balanced"; the one with most channels for "channels"; says when none keeps up, and
       which bigger model the hardware suggests measuring next). Before the worker runs, the
       machine is looked at from Dispatcharr with the worker's own functions.
       `GET/PUT /api/channels/captions/`, `POST /api/channels/captions/action/`
       (`install`/`remove` for the root watcher; `look`/`download`/`benchmark` for the worker).
     - Installer `fork/patcher/captions.sh` (systemd only): `install.sh` sets up
       `dispatch-more-captions-request.path` on `$STATE/requests/captions`; the tab's button
       leaves `{"action": "install"|"remove"}` there; the root service makes
       `/opt/dispatch-more-captions` (uv, else `python3 -m venv`), installs faster-whisper
       (+ `nvidia-cublas-cu12`, `nvidia-cudnn-cu12==9.*` only when `nvidia-smi` sees a card),
       writes `dispatch-more-captions.service` (127.0.0.1:9725, as Dispatcharr's user, models in
       `${DISPATCHARR_MODELS_DIR:-/data/models}/captions`), and reports progress in
       `$STATE/captions-status.json` (log `captions-install.log`). Remove takes the service, the
       venv and the models; uninstalling Dispatch More takes the watcher too
       (`captions.sh remove ... all`). Docker: the tab shows a compose service (python:3.12-slim
       that pip-installs faster-whisper and fetches this release's `worker.py` from the fork's
       tag; a GPU variant with the NVIDIA device reservation). DeepL joined Service keys.
     - Tab: the "Captions from the sound" card above the list (closed until opened; nothing is
       asked of the worker before that).
   - **3b, done in v247 (server) and arrTV arr.75: captions while a TV watches.**
     - Worker jobs (`worker.Job`, one per channel, key = channel UUID): a reader decodes the
       channel's sound with PyAV (no ffmpeg) from this server's own proxy,
       `http://127.0.0.1:9191/proxy/ts/stream/<uuid>` (setting `stream_base`), as one more
       client of the channel the TV already plays -- no provider connection of its own -- with
       User-Agent `DispatchMore-Captions/1`; `Chunker` cuts 16 kHz pieces at a pause (2.5-7 s);
       a transcriber turns them into lines stamped with the stream's PTS (seconds, the frame's
       own time); at most 3 pieces wait (the oldest dropped: live first). The language is the
       one set by hand on the tab, else found by the model and fixed once two pieces agree at
       0.7, looked at again every 5 minutes. A job stops when nobody asked for 10 s, or on
       `POST /jobs/stop`. `POST /jobs/poll {key, url, model, language, since}` starts or keeps
       it and answers the lines since `since`; `/status` lists the jobs.
     - Dispatch More (`captions/live.py`): `GET /api/channels/captions/live/<uuid>/?since=`
       (any signed-in user) answers "off" (setting `live` off, or no worker), "not playing"
       (the proxy is not playing it: a job only ever reads alongside), "busy" (as many jobs as
       `channels_at_once`), or the worker's answer; the model is the setting or the proposal
       (asked once a minute). `DELETE` stops the job.
     - Force Close (`probation.stop_skipped_channels`) passes the caption client by
       (`captions.is_caption_client`): it would otherwise be "someone else watching" and the
       channel a viewer left would stay open, holding the provider's connection.
     - arrTV: "Generated captions (from the sound)" in Subtitles; polls once a second; shows a
       line while the frame on screen has its stream time (the TS extractor's
       `TimestampAdjuster`); `DELETE` on leaving. See arrTV's `.personal/CHANGES.md` §3n.
     - Checked live: CBC Vancouver through the user's proxy, model tiny on a CPU here, English
       found by itself, about half a second behind live.
     - Still to do: the per-channel setting (§3.2), smaller-model fallback when behind
       (§5b.5), translation (step 4), playing a few seconds further behind live when captions
       are on, more engines (whisper.cpp, Vosk, Parakeet, cloud).
4. **Translation (§6.2, §7.2) -- built in v248** (what was built is at the end of this item). The user: "subtitles work
   but there is no translation yet". The plan, fitted to what 3b built:
   - **What a TV asks for.** arrTV gets Settings -> Player -> **Caption language** (Original /
     the TV's own language / a list), and the Subtitles menu shows "Generated captions" and, when
     the programme's language differs, "Generated captions (translated to Dutch)". The poll gains
     `&lang=nl` (`GET /api/channels/captions/live/<uuid>/?since=&lang=`); without it nothing
     changes.
   - **One translation per (channel, language), shared** (§7.2): Dispatch More keeps, per job, a
     list of translated cues alongside the originals -- same `seq`, same stream times, the text
     translated -- so every TV asking for Dutch on that channel reads the same lines.
   - **Where it runs.** A translator per server, chosen like the speech model (§5b.3) and shown on
     the captions card: **DeepL** when its key is set (Service keys, v244); else **Ollama** when the
     card found one (an 8B model, prompt "translate these subtitle lines from {src} to {dst}, keep
     one line per line"); else **Opus-MT** in the caption worker (CTranslate2 + SentencePiece,
     one ~300 MB model per pair, through English when a pair is missing, downloaded on first use
     with its size shown first); else none ("translation not available on this server").
   - **In the worker** (it already has CTranslate2): `POST /translate {lines, source, target,
     engine}` for Opus-MT (and Ollama/DeepL called from there too, so one place batches); the job
     translates each finished piece's lines as they come (whole sentences: a line ending
     mid-sentence waits for the next piece, at most 2 s), adding about 0.2-1 s.
   - **Source language**: the job's (set by hand, or found by the model, 3b). Same as the asked
     language: no translation, the originals are shown.
   - **Keeping up**: a translation that falls behind the picture is shown late rather than not at
     all, and the card shows its delay; arrTV's "play further behind live while captions are on"
     (§5b.5, not built) helps both.
   - **Switch**: the captions card's "Translate captions" (on when a translator exists); arrTV's
     setting Original keeps today's behaviour.
   - **Tests**: the per-language cue list (seq/stream times kept, sentences joined), engine
     choice, the `lang` parameter, Opus-MT on a Dutch sample (worker, skipped without the model).
   - **Built (v248)**: `apps/channels/captions/translate.py` -- `translated(answer, channel, lang)`
     on the poll's answer: per (channel, language) a Redis hash `captions:tr:<uuid>:<lang>`
     (seq -> text, 10 min), one TV translating a piece at a time (`:lock`), lines out in order
     (an untranslated line holds back the later ones until the next poll), each cue keeping its
     `original`; `translation: {to, from, state, engine}` with states `translated`, `same
     language`, `waiting for the language`, `not available`, `failed` (the original lines then).
     `engine()`: the card's `translator` ("" automatic, deepl, ollama, opus-mt, off) -- DeepL
     (`api-free` for `:fx` keys), Ollama `/api/generate` asked for a JSON list one line per line
     (`ollama_model`, or its first), the worker's `POST /translate` (Opus-MT: the pair's original
     Marian weights, linked from its Hugging Face card, converted by CTranslate2's
     `OpusMTConverter` to int8 -- nl-en is 82 MB and translates two lines in 0.04 s on a CPU here;
     through English when there is no direct pair, nl-de checked; `sentencepiece` added to the
     worker's install). v249: the captions card lists the models even before the worker runs, and keeps looking for 30 s after Install / Remove (captions.sh says "installing" before it drops the request; the card had stopped at the old "removed"). Not built: joining a sentence cut over two pieces, multi-target Opus-MT
     models. Tests: `apps/channels/tests/test_caption_translation.py`.
5. More models (NeMo, Vosk, cloud), captions for recordings (§4.5), teletext pages (§2).

Tests, as always: the full backend suite (baseline `FAILED (errors=26)`, all `/data`) on a test
database of our own (`POSTGRES_DB=dispatcharr_claude`), vitest green, and the patcher release.
