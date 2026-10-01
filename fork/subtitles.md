# Subtitles: what channels carry, captions made from the sound, and translation

Designed 2026-10-01, against release v242. **Step 1 (§2 and the tab's list, §3.1) is built in
v243**; the rest is not. This is the hand-over for
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

1. **Where the GPU is.** The user mentioned an RTX 3060; the PC this was designed on has an
   RTX 3080 (10 GB). The worker must run on a machine that is on whenever someone watches: the
   Proxmox host (GPU passed through to an LXC or a VM) is ideal, a desktop that sleeps is not.
2. **Teletext first in arrTV** (§7.3) before any of the server work? Recommended.
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
2. arrTV: teletext subtitles (§7.3).
3. The caption worker with faster-whisper (§5, §6.1), delivery to arrTV (§4), the tab's settings
   (§3.2-§3.3), arrTV's Subtitles settings (§7.1).
4. Translation (§6.2, §7.2).
5. More models (NeMo, Vosk, cloud), captions for recordings (§4.5), teletext pages (§2).

Tests, as always: the full backend suite (baseline `FAILED (errors=26)`, all `/data`) on a test
database of our own (`POSTGRES_DB=dispatcharr_claude`), vitest green, and the patcher release.
