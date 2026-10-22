"""The one place "today" comes from: the UTC date.

GitHub's ``pushed_at`` is UTC, so comparing it with the local date would make the newest
repositories look future-dated on evenings when UTC is already the next day.
"""
from datetime import datetime, timezone


def utc_today():
    """Today's date in UTC, whatever the local timezone."""
    return datetime.now(timezone.utc).date()
