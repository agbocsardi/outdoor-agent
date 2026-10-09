from datetime import UTC, datetime

import pytest

from outdoor_agent.models.time import overlaps, parse_event_time, parse_window


def test_date_only_dst_day_uses_local_midnights() -> None:
    window = parse_window("2026-03-29")
    assert window.precision == "date"
    assert (window.end - window.start).total_seconds() == 23 * 60 * 60


def test_nonexistent_and_ambiguous_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="ambiguous or nonexistent"):
        parse_window("2026-03-29T02:30:00")
    with pytest.raises(ValueError, match="ambiguous or nonexistent"):
        parse_window("2026-10-25T02:30:00")


def test_explicit_offset_datetime_and_overnight_event_overlap() -> None:
    instant = parse_window("2026-10-25T02:30:00+02:00")
    assert instant.start == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    starts = datetime(2026, 10, 24, 21, tzinfo=UTC)
    ends = datetime(2026, 10, 25, 1, tzinfo=UTC)
    assert overlaps(instant, starts, ends) is True


def test_expired_closure_does_not_overlap_requested_day() -> None:
    day = parse_window("2026-10-11")
    assert (
        overlaps(day, datetime(2026, 10, 9, tzinfo=UTC), datetime(2026, 10, 10, tzinfo=UTC))
        is False
    )


def test_ambiguous_feed_timestamp_stays_unknown() -> None:
    assert parse_event_time("2026-10-25T02:30:00") is None
