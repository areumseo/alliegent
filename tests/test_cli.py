from __future__ import annotations

from alliegent.cli import CHECKS, JOB_CHANNEL, JOBS


def test_every_job_has_a_channel_route():
    """A job with no route would raise KeyError at --send time, which is
    exactly when someone is trying to verify their setup."""
    assert set(JOBS) == set(JOB_CHANNEL)


def test_a_check_is_not_a_job():
    """Checks report on the setup rather than producing a message, so they
    have nothing to send — and must not be held to the route invariant."""
    assert not set(CHECKS) & set(JOBS)


def test_routes_match_the_scheduled_jobs():
    assert JOB_CHANNEL["stale"] == "projects"
    assert JOB_CHANNEL["review"] == "review"
    assert JOB_CHANNEL["brief"] == "agenda"
