"""Guide Layout: what order your channels come in, and on which numbers.

A lineup is not a list of channels, it is an arrangement of them: this group here, that
one after it, this channel between those two. Dispatcharr has the numbers but nowhere to
arrange them, so doing it by hand means editing one channel at a time and working out the
knock-on numbers yourself.

Two rules, and the first is the important one:

- **Channels keep the numbers they have.** Dragging one between two others does not
  renumber a group; it gives the channel the place it was dropped in and pushes the
  others along **only as far as it has to**, stopping the moment there is room. A lineup
  people have built over months does not get rearranged because one channel moved.
- **Renumbering a group outright is asked for separately**, with a number to start at and
  a step to go up by, because that is a different intention and a much bigger change.

Nothing is written until it is applied. What is shown before that is worked out here too,
so there is one set of rules rather than one in Python and another in the page.

Channels are read and written as the channel list shows them: an auto-created channel's name,
number and group are its provider's, and what the user chose instead is its override, which
wins (Dispatcharr's effective values, used for sorting and numbering everywhere since 0.32).
"""

import logging
import re

logger = logging.getLogger(__name__)


def numbers_for(order, existing, moved=None):
    """
    The number each channel would have, arranged in this order.

    `order` is the channel ids as they are to come out; `existing` is the number each one
    has now; `moved` is the one just dragged, which takes the number of the place it was
    dropped in rather than keeping its own -- everything else keeps what it has unless the
    order would be wrong, and then it moves by as little as possible.
    """
    # What the first number of this group is, worked out without the channel being moved:
    # one coming from another group brings its old number with it, and a channel numbered
    # 5 dropped at the top of a group that starts at 100 must take 100, not 5.
    base = min(
        (n for one, n in existing.items() if n is not None and one != moved), default=None
    )
    if base is None:
        base = existing.get(moved) or 1
    numbers = {}
    last = None
    for channel_id in order:
        mine = existing.get(channel_id)
        takes_its_place = channel_id == moved or mine is None
        if last is None:
            wanted = base if takes_its_place else mine
        elif takes_its_place or mine <= last:
            wanted = last + 1
        else:
            wanted = mine
        numbers[channel_id] = wanted
        last = wanted
    return numbers


def renumbered(order, start, step=1):
    """Every channel in this order given a number of its own, from `start`, every `step`."""
    step = step if step else 1
    return {channel_id: start + at * step for at, channel_id in enumerate(order)}


def effective(queryset):
    """
    These channels with what the channel list shows for them (effective_name,
    effective_channel_number, effective_channel_group_id and the effective_*_obj properties).
    Reading the channel's own columns showed an auto-created channel where its provider put
    it rather than where the user did, and the two lists disagreed.
    """
    from .managers import with_effective_values

    return with_effective_values(queryset, select_related_fks=True)


# The fields given by id whose model field is the relation (tvg_id is a plain column)
_FOREIGN_KEYS = {
    "channel_group_id": "channel_group", "epg_data_id": "epg_data", "logo_id": "logo",
    "stream_profile_id": "stream_profile",
}


def write_effective(channel, values):
    """
    {field: value} written where the channel list reads it. A channel made by hand is
    written itself. An auto-created channel's fields are its provider's and a refresh puts
    them back, so the value goes on its override -- as Dispatcharr's own renumbering does
    (apply_effective_channel_numbers) -- and an override that comes to equal the provider's
    value is cleared rather than kept. `channel` comes with its override (effective()).
    Returns whether anything changed.
    """
    from .models import ChannelOverride

    if not channel.auto_created:
        fields = [f for f, v in values.items() if getattr(channel, f) != v]
        for field in fields:
            setattr(channel, field, values[field])
        if fields:
            # Saved one at a time with update_fields: a channel moving group or number is
            # something other parts of Dispatcharr watch for (signals)
            channel.save(update_fields=[_FOREIGN_KEYS.get(f, f) for f in fields])
        return bool(fields)

    try:
        override = channel.override
    except ChannelOverride.DoesNotExist:
        override = None
    changed = False
    for field, value in values.items():
        wanted = None if value == getattr(channel, field) else value
        if (getattr(override, field) if override else None) == wanted:
            continue
        if override is None:
            override = ChannelOverride(channel=channel)
        setattr(override, field, wanted)
        changed = True
    if changed:
        if override.has_any_override():
            override.save()
        elif override.pk:
            override.delete()
    return changed


def _channel_row(channel, clashing):
    group = channel.effective_channel_group_obj
    logo = channel.effective_logo_obj
    epg = channel.effective_epg_data_obj
    return {
        "id": channel.id,
        "name": channel.effective_name,
        "number": channel.effective_channel_number,
        "logo_url": logo.url if logo else "",
        "group": group.name if group else "",
        "group_id": channel.effective_channel_group_id,
        "epg": epg.name if epg else "",
        # Two channels on one number: a media server sees one flat lineup, so it is a
        # problem across all the groups and not only inside one. Nothing else says so.
        "clashes": channel.effective_channel_number in clashing,
    }


def layout(group_ids=()):
    """Every group with its channels in number order, and where the numbers clash."""
    from collections import Counter

    from .models import Channel, ChannelGroup
    from .show_groups.live import copy_group_ids

    channels = (
        effective(Channel.objects.select_related("channel_group", "logo", "epg_data"))
        # Show Groups' copies keep the numbers it gave them (see show_groups.live.copy_group_ids)
        .exclude(effective_channel_group_id__in=copy_group_ids())
        .order_by("effective_channel_number", "id")
    )
    if group_ids:
        channels = channels.filter(effective_channel_group_id__in=[int(g) for g in group_ids])
    channels = list(channels)

    # Clashes are counted over every channel there is, whatever is being looked at
    used = Counter(
        n for n in effective(Channel.objects.all()).values_list("effective_channel_number", flat=True)
        if n is not None
    )
    clashing = {number for number, how_many in used.items() if how_many > 1}

    names = dict(ChannelGroup.objects.values_list("id", "name"))
    groups = {}
    for channel in channels:
        group_id = channel.effective_channel_group_id
        group = groups.setdefault(group_id, {
            "id": group_id,
            "name": names.get(group_id, "No group"),
            "channels": [],
        })
        group["channels"].append(_channel_row(channel, clashing))

    out = []
    for group in groups.values():
        numbers = [c["number"] for c in group["channels"] if c["number"] is not None]
        group["first"] = min(numbers) if numbers else None
        group["last"] = max(numbers) if numbers else None
        out.append(group)
    out.sort(key=lambda g: (g["first"] is None, g["first"] or 0, g["name"]))

    # Where one group ends and the next begins: room to grow, or none
    for earlier, later in zip(out, out[1:]):
        if earlier["last"] is not None and later["first"] is not None:
            earlier["room_after"] = max(0, int(later["first"] - earlier["last"]) - 1)
    return {"groups": out, "clashes": sorted(clashing)}


def renamed(names, take_off="", replace_with=""):
    """
    What these channels would be called with `take_off` taken out of their names.

    `take_off` is plain text, not a pattern: people type "┃DE┃" or "VIP" and mean those
    characters, and a name full of box-drawing and brackets is exactly the sort of thing
    that turns into a pattern nobody meant. What is left is tidied of the double spaces
    that taking something out of the middle leaves behind.

    Returns {channel id: new name} for the ones that would actually change.
    """
    wanted = str(take_off or "")
    if not wanted:
        return {}
    changed = {}
    for channel_id, name in (names or {}).items():
        now = re.sub(r"\s+", " ", str(name or "").replace(wanted, replace_with or "")).strip()
        if now and now != name:
            changed[channel_id] = now
    return changed


def rename_group(group_id, name):
    """A group renamed. Its channels are untouched: their names are their own."""
    from django.db import IntegrityError

    from .models import ChannelGroup

    # ChannelGroup.name is a TextField: there is no length to cut it to, and cutting it
    # to 255 silently shortened a name somebody meant
    name = str(name or "").strip()
    if not name:
        raise ValueError("A group needs a name")
    group = ChannelGroup.objects.filter(id=group_id).first()
    if not group:
        raise ValueError("That group is gone")
    if ChannelGroup.objects.filter(name__iexact=name).exclude(id=group.id).exists():
        raise ValueError(f"There is already a group called {name}")
    group.name = name
    try:
        group.save(update_fields=["name"])
    except IntegrityError:
        # The name was taken between the asking and the saving: the same answer, not a 500
        raise ValueError(f"There is already a group called {name}")
    logger.info(f"Guide Layout: group {group_id} renamed to {name}")
    return {"name": name}


def rename_channels(names):
    """
    Channels renamed: {channel id: name}. Saved one at a time with update_fields, as
    everything that changes a channel here is.
    """
    from .models import Channel

    from .models import Channel as _Channel

    # As long as the field really is, not a number picked out of the air: names were being
    # cut at 255 where the column holds 512
    longest = _Channel._meta.get_field("name").max_length
    wanted = {}
    for channel_id, name in (names or {}).items():
        name = str(name or "").strip()[:longest]
        if name:
            try:
                wanted[int(channel_id)] = name
            except (TypeError, ValueError):
                continue
    if not wanted:
        return {"renamed": 0}
    renamed_count = 0
    for channel in effective(Channel.objects.filter(id__in=wanted)):
        if channel.effective_name != wanted[channel.id] and write_effective(channel, {"name": wanted[channel.id]}):
            renamed_count += 1
    logger.info(f"Guide Layout: {renamed_count} channel(s) renamed")
    return {"renamed": renamed_count}


def apply(numbers, groups=None):
    """
    Put these numbers on these channels: {channel id: number}, and with `groups`
    ({channel id: group id}) move them as well.

    Saved one at a time with update_fields, because a channel moving group or number is
    something other parts of Dispatcharr watch for. Worked out against what is there now:
    a channel gone since the page was looked at is left alone rather than being an error.
    """
    from .models import Channel

    wanted = {}
    for channel_id, number in (numbers or {}).items():
        try:
            wanted[int(channel_id)] = float(number)
        except (TypeError, ValueError):
            continue
    moving = {}
    for channel_id, group_id in (groups or {}).items():
        try:
            moving[int(channel_id)] = int(group_id) if group_id else None
        except (TypeError, ValueError):
            continue
    if not wanted and not moving:
        return {"changed": 0}

    from django.db import transaction

    changed = 0
    # All of it or none of it. Renumbering a group one channel at a time and stopping half
    # way leaves an arrangement nobody asked for and no way to tell which half is which.
    with transaction.atomic():
        changed = _write(wanted, moving)
    logger.info(f"Guide Layout: {changed} channel(s) renumbered or moved")
    return {"changed": changed}


def _write(wanted, moving):
    from .models import Channel

    changed = 0
    for channel in effective(Channel.objects.filter(id__in=set(wanted) | set(moving))):
        values = {}
        number = wanted.get(channel.id)
        if number is not None and channel.effective_channel_number != number:
            values["channel_number"] = number
        if channel.id in moving and channel.effective_channel_group_id != moving[channel.id]:
            values["channel_group_id"] = moving[channel.id]
        if values and write_effective(channel, values):
            changed += 1
    return changed
