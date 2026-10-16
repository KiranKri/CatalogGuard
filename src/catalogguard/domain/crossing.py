"""Downward-crossing and cooldown rules.

A low-stock event fires only when quantity crosses from at-or-above the
threshold to strictly below it. A first insert has no previous quantity, so
it is not a crossing even if the new quantity is already low. Moving from
4 to 2 while the threshold is 5 is not a new crossing.

Cooldown is a second gate. After an event fires for a (SKU, store), another
crossing inside the cooldown window is dropped. It is not queued for later.
If stock stays below the threshold, nothing else fires until the quantity
goes back to the threshold or above and crosses down again after the window.

Trade-off: a real restock-and-sellout inside the window is silent, and a
crossing that happens during the window is gone even if the item stays low.
The manual rewrite endpoint is the operator path. The cooldown stops a
flapping count (5 → 4 → 6 → 4) from rewriting the listing and queueing mail
on every wobble. Default window is 24 hours.
"""

from datetime import datetime, timedelta


def is_downward_crossing(*, previous: int | None, new: int, threshold: int) -> bool:
    if previous is None:
        return False
    return previous >= threshold and new < threshold


def is_cooldown_active(
    *,
    last_triggered_at: datetime | None,
    now: datetime,
    cooldown: timedelta,
) -> bool:
    if last_triggered_at is None:
        return False
    return (now - last_triggered_at) < cooldown
