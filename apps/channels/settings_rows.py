"""
Changing one of the fork's CoreSettings rows without two writers losing each other.

Everything this fork remembers lives in a JSON row of CoreSettings: what is ignored, what
is settled, what a run suggested, what each guide was read as. Nearly all of it is changed
by reading the row, altering a key of it, and writing the whole thing back -- and two of
those at once means the second one writes what it read before the first one happened, so
the first change is gone without a word.

It is not a rare pairing either: the Guides run writes its suggestions from a Celery
worker for as long as it takes to go through a lineup, and the page it is filling is where
somebody sits clicking "not that one" while it does. Same row, same second.

So the read and the write happen together, inside a transaction, with the row locked for
the length of it -- which is what Stream Check's own `_change_key` has always done, and
what nothing else did.
"""

import logging

logger = logging.getLogger(__name__)


def change_row(key, name, change, default=None):
    """
    Read a CoreSettings row, change it, and write it back, holding it throughout.

    `change` is given the row's value (a dict, or whatever `default` is) to alter in
    place, and whatever it returns is returned from here. The row is made if it is not
    there yet, so a first change is like any other.
    """
    from django.db import transaction

    from core.models import CoreSettings

    empty = {} if default is None else default
    with transaction.atomic():
        row, _ = CoreSettings.objects.select_for_update().get_or_create(
            key=key, defaults={"name": name, "value": empty}
        )
        value = row.value
        if not isinstance(value, type(empty)):
            value = empty
        answer = change(value)
        row.value = value
        row.save(update_fields=["value"])
    return answer
