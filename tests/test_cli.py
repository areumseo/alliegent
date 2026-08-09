from __future__ import annotations

from alliegent.cli import JOB_CHANNEL, JOBS


def test_every_job_has_a_channel_route():
    """A job with no route would raise KeyError at --send time, which is
    exactly when someone is trying to verify their setup."""
    assert set(JOBS) == set(JOB_CHANNEL)


def test_routes_match_the_scheduled_jobs():
    assert JOB_CHANNEL["stale"] == "projects"
    assert JOB_CHANNEL["review"] == "review"
    assert JOB_CHANNEL["brief"] == "agenda"
