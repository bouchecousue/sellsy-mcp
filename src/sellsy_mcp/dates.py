from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def boundary(value: str, *, end: bool = False) -> datetime:
    if len(value) == 10:
        day = date.fromisoformat(value)
        result = datetime.combine(day, time.min, ZoneInfo("Europe/Paris"))
        if end:
            result += timedelta(days=1, microseconds=-1)
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Les horodatages doivent inclure un fuseau horaire.")
    return result.astimezone(UTC)


def in_period(value: str | None, start: str | None, end: str | None) -> bool:
    if not start and not end:
        return True
    if not value:
        return False
    instant = boundary(value)
    return (not start or instant >= boundary(start)) and (not end or instant <= boundary(end, end=True))
