# Can the guides that say what a programme is teach the guides that do not? Half the programmes
# here carry no category at all (TiviBridge, epg.pw, open-epg), so this builds a dictionary of
# title -> categories from the guides that do carry them, and measures how much of the rest it
# could name: by the same title, by a near-enough title, or not at all. Read-only.
#
#   bash dispatcharr-shell.sh shell < survey-titles.py
#   CATEGORY=Cooking AHEAD=48 bash dispatcharr-shell.sh shell < survey-titles.py
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from django.utils import timezone

from apps.epg.models import ProgramData

AHEAD = int(os.environ.get("AHEAD", "48"))
CATEGORY = os.environ.get("CATEGORY", "Cooking").lower()
NEAR = float(os.environ.get("NEAR", "0.9"))
# Categories that say how a programme is made, not what it is about
STRUCTURAL = {"series", "episode", "special", "show", "programme", "program"}

def plain(title):
    text = unicodedata.normalize("NFKD", (title or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Off with the episode's own name, the year, the repeat marks
    text = re.split(r" - |: ", text)[0]
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())

now = timezone.now()
window = ProgramData.objects.filter(
    start_time__gte=now - timedelta(hours=6), start_time__lte=now + timedelta(hours=AHEAD)
).only("title", "custom_properties")

known = defaultdict(set)     # plain title -> categories, from the guides that give them
without = Counter()          # plain title -> airings, from the guides that do not
titles = {}                  # plain title -> how it is written
for program in window.iterator():
    key = plain(program.title)
    if not key:
        continue
    titles.setdefault(key, program.title)
    categories = [str(c).strip() for c in (program.custom_properties or {}).get("categories") or () if str(c).strip()]
    real = [c for c in categories if c.lower() not in STRUCTURAL]
    if real:
        known[key].update(real)
    elif not categories:
        without[key] += 1

buckets = defaultdict(list)
for key in known:
    buckets[key.split(" ")[0][:4]].append(key)

same = near = unknown = 0
near_examples, unknown_top = [], Counter()
for key, airings in without.items():
    if key in known:
        same += airings
        continue
    best, score = None, 0.0
    for candidate in buckets.get(key.split(" ")[0][:4], ()):
        ratio = SequenceMatcher(None, key, candidate).ratio()
        if ratio > score:
            best, score = candidate, ratio
    if best and score >= NEAR:
        near += airings
        if len(near_examples) < 8:
            near_examples.append((titles[key], titles[best], score, sorted(known[best])[:3]))
    else:
        unknown += airings
        unknown_top[key] += airings

total = same + near + unknown
print(f"{len(known):,} titles come with a category; {len(without):,} titles do not "
      f"({total:,} airings in the next {AHEAD} h)\n")
print("Could the ones without be named from the ones with?")
for label, count in (("the very same title", same), (f"a near-enough title (>= {NEAR})", near), ("no match at all", unknown)):
    print(f"  {label:32} {count:>7,}  ({100 * count / total:.0f} %)" if total else "")
print("\nNear matches it made:")
for was, like, score, categories in near_examples:
    print(f"  {was[:34]:34} ~ {like[:34]:34} {score:.2f}  {', '.join(categories)}")
print("\nMost aired titles nothing could name (what an online lookup would be for):")
for key, airings in unknown_top.most_common(15):
    print(f"  {airings:>4}x  {titles[key][:70]}")

wanted = {key for key, categories in known.items() if any(CATEGORY in c.lower() for c in categories)}
print(f"\n'{CATEGORY}': {len(wanted)} titles carry it. In the guides without categories, that "
      f"would catch:")
caught = [(titles[key], without[key]) for key in without if key in wanted]
for title, airings in sorted(caught, key=lambda c: -c[1])[:15]:
    print(f"  {airings:>4}x  {title[:70]}")
print(f"  {sum(a for _t, a in caught)} airings on {len(caught)} titles")
