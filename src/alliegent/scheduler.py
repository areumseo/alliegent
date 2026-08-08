"""Cron scheduling for the jobs, sharing the bot's asyncio loop."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .jobs import Jobs

log = logging.getLogger(__name__)


def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def build_scheduler(jobs: Jobs, config: Config) -> AsyncIOScheduler:
    sched = config.schedule
    scheduler = AsyncIOScheduler(timezone=config.tz)

    def add(name: str, func, *, time: str, day_of_week: str | None = None) -> None:
        hour, minute = _hhmm(time)
        trigger = CronTrigger(
            hour=hour, minute=minute, day_of_week=day_of_week, timezone=config.tz
        )
        scheduler.add_job(
            func,
            trigger,
            id=name,
            name=name,
            # A missed run (deploy, restart, host sleep) should still fire if
            # we come back within the hour, but never pile up duplicates.
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        log.info("scheduled %s at %s%s", name, time, f" ({day_of_week})" if day_of_week else "")

    add("daily_brief", jobs.run_daily_brief, time=sched.daily_brief)
    add("incomplete_alert", jobs.run_incomplete_alert, time=sched.incomplete_alert)
    add(
        "week_scaffold",
        jobs.run_week_scaffold,
        time=sched.week_scaffold_time,
        day_of_week=sched.week_scaffold_weekday,
    )
    add(
        "stale_projects",
        jobs.run_stale_projects,
        time=sched.stale_project_time,
        day_of_week=sched.stale_project_weekday,
    )
    add(
        "weekly_review",
        jobs.run_weekly_review,
        time=sched.weekly_review_time,
        day_of_week=sched.weekly_review_weekday,
    )
    return scheduler
