"""Pins the scheduler's fire-timing logic (killboard GDD §8.3).

Pure-function tests for :func:`killboard.schedule.should_fire` — no sqlite, no
Discord. The load-bearing case is the DST guard: a daily period that spans a
spring-forward day is only 23h in UTC, and naive +24h fire-due math would land
the due instant on the *next* period's start and silently skip that day's post.
"""

from __future__ import annotations

from datetime import UTC, datetime

from killboard.schedule import should_fire


def test_daily_waits_until_the_due_hour_then_fires() -> None:
    """Under UTC, a daily schedule at hour_utc=4 is not due before 04:00Z and is
    due after — with no prior run this period."""
    before = datetime(2026, 5, 10, 3, 0, tzinfo=UTC)
    after = datetime(2026, 5, 10, 5, 0, tzinfo=UTC)
    assert should_fire("daily", 4, None, before, "UTC") is False
    assert should_fire("daily", 4, None, after, "UTC") is True


def test_daily_does_not_refire_within_the_same_period() -> None:
    """Once stamped within the current period, the schedule is idempotent — a
    restart or a later tick the same day does not double-post."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    # Ran earlier today (after the period start) → must not fire again.
    assert should_fire("daily", 4, "2026-05-10T04:05:00Z", now, "UTC") is False
    # Last run was yesterday (before this period's start) → fires.
    assert should_fire("daily", 4, "2026-05-09T04:05:00Z", now, "UTC") is True


def test_daily_not_skipped_on_dst_spring_forward() -> None:
    """The regression: on a US spring-forward day (2026-03-08) the local day is
    only 23h in UTC — [Mar 8 05:00Z, Mar 9 04:00Z). With naive +24h fire-due math
    hour_utc=4 lands exactly on the next period's start, so the day's post would
    be skipped entirely. The DST guard fires it at the period start instead."""
    tz = "America/New_York"
    # A moment inside the shortened period, past its start.
    now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert should_fire("daily", 4, None, now, tz) is True
    # And it still doesn't double-fire once stamped this period.
    assert should_fire("daily", 4, "2026-03-08T05:30:00Z", now, tz) is False


def test_unknown_kind_never_fires() -> None:
    """A bad kind is tolerated as never-fire, not an exception."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    assert should_fire("hourly", 4, None, now, "UTC") is False
