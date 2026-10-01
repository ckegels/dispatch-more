"""Captions made from the sound (fork/subtitles.md step 3): the worker and what Dispatch More asks it."""

# What the caption worker calls itself when it reads a channel through the proxy
# (worker.CAPTIONS_USER_AGENT). Not a viewer: Force Close and the viewer checks pass it by.
CAPTIONS_USER_AGENT = "DispatchMore-Captions/"


def is_caption_client(user_agent):
    return str(user_agent or "").startswith(CAPTIONS_USER_AGENT)
