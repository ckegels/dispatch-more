# Picture check: is this stream really that channel? (design, not built)

Status: **proposed, waiting for the user's decision** (2026-09-28). Connections are not a
concern: the user is fine with it running at night.

## Why

The Lineup decides which provider stream is which channel from names, tvg-ids, call signs
and iptv-org's channel database. There is no public list linking one reseller's streams to
another's (TiviBridge's "┃BE┃ VRT 1 FHD" to Digitalizard's "BE| VRT 1 HD"): resellers
publish nothing shared, and playlist tools (IPTVEditor, m3u-editor) match each provider to
a guide, not to each other. What is on screen is the only proof that two streams are one
channel, and it is also what tells apart what names get wrong: +1 channels, East and West
feeds, regional variants.

It builds on the remembered streams (`apps/channels/pairings.py`, v218): a confirmed pair
is written down there with `how: "picture"` and is from then on put back after renames.

## How it works

1. **What is checked.** Pairs the Lineup is unsure of (low score, or suggested by tvg-id or
   loose name only), new suggestions, and channels that already carry streams from several
   providers (to catch a wrong stream already on a channel).
2. **A short sample of both, at the same time.** Each stream is opened for 20-30 s. ffmpeg
   (already on the box for Stream Check) writes, per stream:
   - one frame a second, scaled to 32x32 grey: a perceptual hash (dHash) per second;
   - the loudness of the sound, 10 values a second.
   Nothing is kept as video.
3. **Providers are not in step.** One is often 5-30 s behind another, so the two samples
   are slid along each other (offsets up to the sample length) and the best-aligned offset
   is taken, as a person finds the same scene in two recordings.
4. **Verdict.**
   - **Same:** frame hashes close (Hamming distance under a threshold on most aligned
     seconds) *and* the loudness curves correlate at the same offset.
   - **Different:** neither lines up. +1 and East/West feeds are hours apart, so they come
     out different, which is right.
   - **Can't tell:** a still picture (black, a test card, "channel unavailable"), or
     silence. A picture that does not move is never evidence of anything. Retried later.
   Resolution, bitrate, a provider's logo in a corner and HD vs FHD do not change the hashes
   enough to matter.
5. **What is done with it.**
   - Same: the pair is remembered (`pairings`, `how: "picture"`) and the Lineup shows it
     as confirmed.
   - Different: the Lineup marks the row "picture differs" and does not suggest it again;
     a stream already on a channel that differs from the channel's other streams is flagged
     in Channel health.
   - Can't tell: tried again another night.

## Cost and scheduling

One connection per provider per pair for ~30 s. It runs through Stream Check's scheduler,
which already knows each provider's connection limits, always yields to a viewer, and has
a night window. Roughly 100+ pairs an hour; a whole lineup in a night or two, then only
new or changed pairs. A switch of its own, off by default.

## Open questions

- The thresholds (hash distance, correlation) need measuring on the user's real streams:
  take a handful of known-same and known-different pairs first.
- Channels showing the same programme at the same time (a simulcast, or a network's
  regional feeds between their local slots) will look the same. The loudness of the ads is
  what usually separates regional feeds; a pair judged the same while the names disagree on
  a region should be "can't tell", not "same".
