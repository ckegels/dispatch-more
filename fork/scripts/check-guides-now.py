# Why the Guides page says "Nothing on it now": for every EPG source, what its guides hold
# and when. Read-only: it asks the database and changes nothing.
#
#   bash /root/dispatcharr-shell.sh shell < /root/check-guides-now.py
#
# Answers, per source, in the order they go wrong:
#   1. How many of its guides are on a channel, and how many hold any programmes at all.
#      Dispatcharr only keeps programmes for guides a channel uses; every refresh of the
#      source deletes the rest -- including any read from the Guides page.
#   2. Of the guides on a channel: how many hold programmes, how many have one on air
#      right now, and the earliest and latest programme they hold. All in the past means
#      the file is stale; all in the future, or on-air counts of zero with programmes
#      either side of now, means the times were read in the wrong timezone.
#   3. When the source last refreshed, and what it said.
from datetime import timedelta

from django.db.models import Count, Max, Min
from django.utils import timezone

from apps.channels.models import Channel
from apps.epg.models import EPGData, EPGSource, ProgramData

now = timezone.now()
print(f"Now (server, UTC): {now:%Y-%m-%d %H:%M}")
used = set(Channel.objects.exclude(epg_data__isnull=True).values_list("epg_data_id", flat=True))
for source in EPGSource.objects.order_by("-priority", "name"):
    guides = EPGData.objects.filter(epg_source=source)
    ids = list(guides.values_list("id", flat=True))
    on_channels = [i for i in ids if i in used]
    holding = set(
        ProgramData.objects.filter(epg_id__in=ids).values_list("epg_id", flat=True).distinct()
    )
    print()
    print("=" * 70)
    print(f"{source.name}  (type {source.source_type}, active {source.is_active}, priority {source.priority})")
    refreshed = f"{source.updated_at:%Y-%m-%d %H:%M}" if source.updated_at else "never"
    print(f"  last refreshed: {refreshed}  status: {source.status}  {source.last_message or ''}"[:200])
    print(f"  guides: {len(ids)}  on a channel: {len(on_channels)}  holding programmes: {len(holding)}")
    if not on_channels:
        continue
    theirs = ProgramData.objects.filter(epg_id__in=on_channels)
    span = theirs.aggregate(first=Min("start_time"), last=Max("end_time"))
    airing = set(
        theirs.filter(start_time__lte=now, end_time__gt=now).values_list("epg_id", flat=True).distinct()
    )
    soon = set(
        theirs.filter(start_time__lte=now + timedelta(hours=12), end_time__gt=now)
        .values_list("epg_id", flat=True).distinct()
    )
    held = len(holding & set(on_channels))
    print(f"  of those on a channel: {held} hold programmes, {len(airing)} have one on air now, {len(soon)} have one in the next 12 h")
    if span["first"]:
        print(f"  programmes run from {span['first']:%Y-%m-%d %H:%M} to {span['last']:%Y-%m-%d %H:%M} (UTC)")
        if span["last"] < now:
            print("  >> every programme is in the past: the guide file is stale, or has not refreshed")
        elif span["first"] > now:
            print("  >> every programme is in the future: the times were probably read in the wrong timezone")
    # Three guides on a channel that hold programmes and have nothing on air, with the
    # programmes nearest to now either side -- which shows an offset at a glance
    for epg_id in [i for i in on_channels if i in holding and i not in airing][:3]:
        guide = EPGData.objects.get(id=epg_id)
        before = theirs.filter(epg_id=epg_id, end_time__lte=now).order_by("-end_time").first()
        after = theirs.filter(epg_id=epg_id, start_time__gt=now).order_by("start_time").first()
        print(f"   - {guide.name} ({guide.tvg_id}): nothing on air;"
              f" last ended {before.end_time:%m-%d %H:%M} ({before.title[:30]})" if before else f"   - {guide.name}: nothing before now;",
              f" next starts {after.start_time:%m-%d %H:%M} ({after.title[:30]})" if after else " nothing after now")
