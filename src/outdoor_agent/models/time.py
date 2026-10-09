"""Date and time parsing that preserves the precision supplied by the user."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    precision: Literal["date", "instant"]
    timezone: str


def _valid_local(value: datetime, zone: ZoneInfo) -> bool:
    """Round trip local times to reject DST gaps and ambiguous wall times."""
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return False
    return first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value


def parse_window(value: str, default_timezone: str = "Europe/Amsterdam") -> TimeWindow:
    """Parse an ISO date as its local calendar day, or a datetime as an instant.

    An ISO date has day precision and expands to the next local midnight, including
    23/25-hour DST days. A datetime without an offset is interpreted in the configured
    zone only when that wall time is unambiguous and exists.
    """
    zone = ZoneInfo(default_timezone)
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        parsed_date = None
    if parsed_date is not None and "T" not in value and " " not in value:
        start_local = datetime.combine(parsed_date, time.min, zone)
        next_day = parsed_date + timedelta(days=1)
        end_local = datetime.combine(next_day, time.min, zone)
        return TimeWindow(
            start_local.astimezone(UTC), end_local.astimezone(UTC), "date", default_timezone
        )

    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Date must be an ISO-8601 date or datetime.") from exc
    if moment.tzinfo is None:
        if not _valid_local(moment, zone):
            raise ValueError(
                "Naive datetime is ambiguous or nonexistent in the configured timezone; supply an offset."
            )
        moment = moment.replace(tzinfo=zone)
    instant = moment.astimezone(UTC)
    return TimeWindow(instant, instant, "instant", str(moment.tzinfo))


def parse_event_time(value: str | None, timezone: str = "Europe/Amsterdam") -> datetime | None:
    """Parse a feed timestamp; unknown/ambiguous local timestamps stay unknown."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    zone = ZoneInfo(timezone)
    if not _valid_local(parsed, zone):
        return None
    return parsed.replace(tzinfo=zone).astimezone(UTC)


def overlaps(
    window: TimeWindow, starts_at: datetime | None, ends_at: datetime | None
) -> bool | None:
    """Check half-open interval overlap; for an instant, test the event interval."""
    if starts_at is None and ends_at is None:
        return None
    if window.precision == "instant":
        instant = window.start
        return (starts_at is None or starts_at <= instant) and (
            ends_at is None or instant < ends_at
        )
    event_start = starts_at or datetime.min.replace(tzinfo=UTC)
    event_end = ends_at or datetime.max.replace(tzinfo=UTC)
    return event_start < window.end and window.start < event_end
