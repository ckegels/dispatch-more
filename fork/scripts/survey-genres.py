# What the guides say a programme is: the raw material for groups of channels by kind of show
# ("everything with a cooking programme on now"). Read-only; changes nothing.
#
#   bash dispatcharr-shell.sh shell < survey-genres.py
#   WORD=kook,cook,bak,chef bash dispatcharr-shell.sh shell < survey-genres.py
#
# Prints: how many programmes carry a category at all, per guide source; the categories
# themselves, most used first; and, for each word given, what titles would be caught.
import os
from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from apps.epg.models import EPGSource, ProgramData

WORDS = [w.strip().lower() for w in os.environ.get("WORD", "kook,cook,bak,chef,recept,küche,keuken").split(",") if w.strip()]
AHEAD = int(os.environ.get("AHEAD", "48"))  # hours of guide to look at

now = timezone.now()
window = ProgramData.objects.filter(start_time__gte=now - timedelta(hours=6), start_time__lte=now + timedelta(hours=AHEAD))
total = window.count()
print(f"{total:,} programmes between 6 h ago and {AHEAD} h ahead\n")
if not total:
    raise SystemExit("No guide data in that window: refresh a guide first.")

# Per source: how many of its programmes say what kind of programme they are
print("Per guide source, programmes with a category:")
for source in EPGSource.objects.filter(is_active=True).order_by("name"):
    of_source = window.filter(epg__epg_source=source)
    count = of_source.count()
    if not count:
        continue
    with_category = sum(1 for p in of_source.only("custom_properties").iterator() if (p.custom_properties or {}).get("categories"))
    print(f"  {source.name[:40]:40} {with_category:>7,} of {count:>7,}  ({100 * with_category / count:.0f} %)")

categories = Counter()
titles_by_category = {}
caught = {word: Counter() for word in WORDS}
for program in window.only("title", "description", "custom_properties").iterator():
    for category in (program.custom_properties or {}).get("categories") or ():
        name = str(category).strip()
        categories[name] += 1
        titles_by_category.setdefault(name.lower(), set()).add(program.title)
    text = f"{program.title} {program.description or ''}".lower()
    for word in WORDS:
        if word in text:
            caught[word][program.title] += 1

print(f"\n{len(categories)} different categories; the 40 most used:")
for name, count in categories.most_common(40):
    print(f"  {count:>7,}  {name}")

print("\nWhat a word in the title or description would catch:")
for word in WORDS:
    hits = caught[word]
    print(f"  '{word}': {sum(hits.values()):,} programmes, {len(hits)} different titles")
    for title, count in hits.most_common(5):
        print(f"      {count:>4}x  {title[:70]}")
