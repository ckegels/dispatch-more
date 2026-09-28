"""
A guide's past, kept for a few days after the provider's file has let go of it.

Every refresh of a guide deletes all of its programmes and puts in what the source's file
holds now (apps/epg/tasks.py: one guide at a time, or a whole source at once). Most files
start at today, so whatever was on yesterday is gone the moment the file is read again, and
a player scrolling back through the guide finds nothing -- not even this morning. Dispatcharr
can already send the past (a user's "EPG previous days", or ?prev_days= on the guide's
address); it just never has any to send.

With "keep_past_days" set (the arrTV settings, 0 by default, which is stock), a refresh
leaves the finished programmes of that many days in place, and deletes of them only what
the new data covers: where the file does carry the past, the file wins. Nothing older than
the days kept survives the next refresh, so the kept past never grows beyond them.

Nothing here decides what is sent. A player asks for the past as it always could, and gets
none from a guide nobody refreshed while it was on the air (that is, before this was set).
"""

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

# At most a week: the programmes of every guide in use are kept, and a week is already more
# than any guide shows
MOST_DAYS = 7


def keep_days():
    """How many days of finished programmes a refresh keeps, 0 for none (stock)."""
    try:
        from apps.proxy.live_proxy.app_devices import load_settings

        return max(0, min(MOST_DAYS, int(load_settings().get("keep_past_days") or 0)))
    except Exception as e:
        logger.debug(f"Guide past: could not read the setting: {e}")
        return 0


def _past_window(days):
    from django.utils import timezone

    now = timezone.now()
    return now - timedelta(days=days), now


def delete_replaced(programmes, firsts=None):
    """
    Delete the programmes a refresh replaces, and return how many. `programmes` is the
    queryset stock deletes outright; `firsts` is {epg id: the start of the new data's
    first programme} for the guides the new data has anything for.

    Off (0 days) it is exactly stock's delete.
    """
    days = keep_days()
    if not days:
        return programmes.delete()[0]
    since, now = _past_window(days)
    # Everything but what finished in the days kept goes, as it always did
    deleted = programmes.exclude(end_time__gt=since, end_time__lte=now).delete()[0]
    # Of the past kept, what the new data covers goes too: where they overlap, the new wins
    for epg_id, first in (firsts or {}).items():
        if first is not None:
            deleted += programmes.filter(epg_id=epg_id, end_time__gt=first).delete()[0]
    return deleted


def firsts_of(programmes_to_create):
    """{epg id: earliest start} of ProgramData objects about to be created."""
    firsts = {}
    for programme in programmes_to_create or ():
        epg_id = programme.epg_id
        start = programme.start_time
        if start is not None and (epg_id not in firsts or start < firsts[epg_id]):
            firsts[epg_id] = start
    return firsts


def delete_replaced_by_staged(epg_ids, staging_table):
    """
    delete_replaced for a whole source whose new programmes wait in the staging table
    (stock's PostgreSQL path): the earliest new start per guide is read from there, and the
    overlap goes in one statement rather than one per guide.
    """
    from django.db import connection

    from apps.epg.models import ProgramData

    programmes = ProgramData.objects.filter(epg_id__in=epg_ids)
    days = keep_days()
    if not days:
        return programmes.delete()[0]
    since, now = _past_window(days)
    deleted = programmes.exclude(end_time__gt=since, end_time__lte=now).delete()[0]
    table = ProgramData._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            DELETE FROM {table} p
            USING (
                SELECT epg_id, MIN(start_time) AS first FROM {staging_table} GROUP BY epg_id
            ) f
            WHERE p.epg_id = f.epg_id AND p.epg_id = ANY(%s) AND p.end_time > f.first
            """,
            [list(epg_ids)],
        )
        deleted += cursor.rowcount
    return deleted
