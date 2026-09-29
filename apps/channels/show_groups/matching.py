# What kind of show a programme is, decided layer by layer. Taken from the Show Groups plugin
# (github.com/ckegels/show-groups, HANDOVER.md §4 has the measurements behind each layer). There
# is no Django in here on purpose: the rules are tested without a server, and the plan and the
# live groups decide in exactly the same way because they call the same function.
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta

# Words guides put in <category> that say how a programme is broadcast, not what it is about
STRUCTURAL = {"series", "episode", "special", "show", "programme", "program"}

# Layer names, as the report prints them
PIN = "pin"
GUIDE = "guide category"
OTHER_GUIDE = "same title in your guides"
TITLE_WORDS = "title words"
NOTHING = "nothing knew it"


def fold(text):
    """Lower case without accents, so "Küche" and "kuche" are the same word."""
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def plain(title):
    """The title as a dictionary key. The episode name after " - " or ": " and anything in
    brackets are cut, so every episode of a show lands on the same key. The surveys used exactly
    this rule, so their numbers hold for the plugin."""
    text = re.split(r" - |: ", fold(title))[0]
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def show_name(title):
    """The title as the viewer knows the show, without the episode name: the part plain() keys
    on, in the guide's own spelling."""
    return re.split(r" - |: ", str(title or "").strip())[0].strip() or str(title or "")


def _flat(text):
    """Folded, with everything but letters and digits made a space. Unlike plain() nothing is
    cut, because a category such as "Cooking - How-to" has no episode name to drop."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", fold(text)).split())


def word_list(text):
    """A setting typed as a list (commas or new lines) as folded words. Titles and phrases go
    through plain() so "Hell's Kitchen" matches the key "hell s kitchen"."""
    return [plain(part) for part in re.split(r"[,\n]", str(text or "")) if plain(part)]


def title_list(text):
    """Pins are whole titles, one per line; commas are allowed inside a title."""
    return {plain(line) for line in str(text or "").splitlines() if plain(line)}


def real_categories(categories):
    """A programme's categories without the structural ones, in the guide's own spelling."""
    if isinstance(categories, str):
        categories = [categories]
    found = []
    for category in categories or ():
        name = str(category).strip()
        if name and fold(name) not in STRUCTURAL and name not in found:
            found.append(name)
    return found


@dataclass
class Group:
    """The owner's definition of one show group."""

    name: str
    category_words: list
    title_words: list = field(default_factory=list)
    title_exclusions: list = field(default_factory=list)
    disqualifiers: list = field(default_factory=list)
    use_title_words: bool = False
    use_disqualifiers: bool = False
    always: set = field(default_factory=set)
    never: set = field(default_factory=set)


def group_from_theme(theme):
    """One show group as the Show Groups tab defines it (themes.py)."""
    return Group(
        name=str(theme.get("name") or "").strip() or "Show group",
        category_words=word_list(theme.get("category_words")),
        title_words=word_list(theme.get("title_words")),
        title_exclusions=word_list(theme.get("title_exclusions")),
        disqualifiers=word_list(theme.get("disqualifiers")),
        use_title_words=bool(theme.get("use_title_words")),
        use_disqualifiers=bool(theme.get("use_disqualifiers")),
        always=title_list(theme.get("always")),
        never=title_list(theme.get("never")),
    )


@dataclass
class Verdict:
    taken: bool
    layer: str
    reason: str
    # Found only by a word in the title: never as sure as a category, and the owner sees it
    uncertain: bool = False
    # A category from the disqualifying set, even while that lever is off, so the preview can
    # show what turning it on would change before anyone turns it on
    disqualifier: str = ""
    # What the title-words layer would have said while it is switched off
    title_word: str = ""


def _hit(words, text):
    """The first word that occurs in the text. Substrings, not whole words, because Dutch and
    German put the telling word at the end of a compound: Buiten*keuken*, Küchen*schlacht*."""
    for word in words:
        if word and word in text:
            return word
    return ""


def _answer(group, layer, categories):
    folded = [_flat(c) for c in categories]
    matched = next((c for c, f in zip(categories, folded) if _hit(group.category_words, f)), "")
    shown = ", ".join(categories)
    if not matched:
        return Verdict(False, layer, f"not {group.name.lower()}: {shown}")
    spoiler = next((c for c, f in zip(categories, folded) if _hit(group.disqualifiers, f)), "")
    if spoiler and group.use_disqualifiers:
        return Verdict(False, layer, f"{shown}, but {spoiler} disqualifies it", disqualifier=spoiler)
    return Verdict(True, layer, shown, disqualifier=spoiler)


def judge(group, title, own_categories=(), from_guides=None, from_online=None):
    """Whether a programme belongs in the group, and which layer said so.

    The first layer that knows anything about the programme answers, including when it says
    "not this kind of show": a guide that files a programme under Drama is trusted over a
    word in its title. Pins are the exception and always win, both ways (handover §3.5).

    from_guides: plain title -> categories seen on that title anywhere in the owner's guides.
    from_online: plain title -> {"source": "tvmaze"|"wikidata", "genres": [...]}.
    """
    key = plain(title)
    if key in group.never:
        return Verdict(False, PIN, "pinned: never")
    if key in group.always:
        return Verdict(True, PIN, "pinned: always")

    own = real_categories(own_categories)
    if own:
        return _answer(group, GUIDE, own)
    elsewhere = real_categories((from_guides or {}).get(key))
    if elsewhere:
        return _answer(group, OTHER_GUIDE, elsewhere)
    online = (from_online or {}).get(key) or {}
    if online.get("genres"):
        return _answer(group, online.get("source") or "online", real_categories(online["genres"]))

    word = _hit(group.title_words, key)
    if word and not _hit(group.title_exclusions, key):
        if group.use_title_words:
            return Verdict(True, TITLE_WORDS, f'"{word}" in the title', uncertain=True, title_word=word)
        return Verdict(False, TITLE_WORDS, f'"{word}" in the title, but title words are off',
                       uncertain=True, title_word=word)
    return Verdict(False, NOTHING, "no category, no online answer")


@dataclass
class Stay:
    """One stretch a channel spends in the group: it joins, holds one or more shows, leaves."""

    joins: object
    ended: object  # when its last show ends
    leaves: object  # ended plus the linger
    airings: list


def stays(airings, join_ahead, leave_after, linger=0):
    """When a channel is in the group, from its qualifying airings as (start, end, payload).

    It joins join_ahead minutes before a show starts. When the show ends it stays for the next
    one if that starts within leave_after minutes. When nothing follows, it still stays linger
    minutes more before it leaves, so a guide running late or a show coming back soon does not
    make the channel come and go all the time."""
    ahead = timedelta(minutes=join_ahead)
    after = timedelta(minutes=leave_after)
    extra = timedelta(minutes=linger)
    found = []
    for start, end, payload in sorted(airings, key=lambda a: (a[0], a[1])):
        last = found[-1] if found else None
        if last and (start - last.ended <= after or start - ahead <= last.leaves):
            last.ended = max(last.ended, end)
            last.leaves = last.ended + extra
            last.airings.append((start, end, payload))
        else:
            found.append(Stay(start - ahead, end, end + extra, [(start, end, payload)]))
    return found
