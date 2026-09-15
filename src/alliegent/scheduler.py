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


# Which channel a failing job should complain in. A job that dies quietly is
# indistinguishable from a job with nothing to say, and this bot is built to
# stay quiet when there is nothing to say -- so the two look identical.
JOB_CHANNELS = {
    "daily_brief": "agenda",
    "incomplete_alert": "agenda",
    "weekly_planning": "agenda",
    "week_scaffold": "agenda",
    "weekly_review": "review",
    "stale_projects": "projects",
    "ai_news": "news",
    "karrot_report": "karrot",
    "karrot_candidates": "karrot",
    "asset_prompt": "assets",
    "english_quiz": "english",
}


def _announcing(jobs: Jobs, name: str, func):
    """Run a job, and say so in its own channel when it fails.

    Once per scheduled run, which is the same cadence as the message that
    failed to arrive -- so a job broken for two days says so twice, rather
    than leaving two days of silence to be noticed by accident.
    """
    kind = JOB_CHANNELS.get(name.split("@")[0], "agenda")

    async def run() -> None:
        try:
            await func()
        except Exception as exc:
            log.exception("%s failed", name)
            try:
                await jobs.notify(
                    f"⚠️ `{name}` failed: {type(exc).__name__}. "
                    "The message it would have sent is missing, not empty.",
                    kind,
                )
            except Exception:
                # Reporting the failure must not become a second failure.
                log.exception("could not report the failure of %s", name)

    return run


def build_scheduler(jobs: Jobs, config: Config) -> AsyncIOScheduler:
    sched = config.schedule
    scheduler = AsyncIOScheduler(timezone=config.tz)

    def add(name: str, func, *, time: str, day_of_week: str | None = None) -> None:
        # An empty time is how a job is switched off in alliegent.toml.
        if not time.strip():
            log.info("skipped %s (no time configured)", name)
            return
        hour, minute = _hhmm(time)
        trigger = CronTrigger(
            hour=hour, minute=minute, day_of_week=day_of_week, timezone=config.tz
        )
        scheduler.add_job(
            _announcing(jobs, name, func),
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
    add("ai_news", jobs.run_ai_news, time=sched.ai_news)
    add(
        "karrot_candidates",
        jobs.run_karrot_candidates,
        time=sched.karrot_candidates_time,
        day_of_week=sched.karrot_candidates_weekday,
    )
    add(
        "karrot_report",
        jobs.run_karrot_report,
        time=sched.karrot_report_time,
        day_of_week=sched.karrot_report_weekday,
    )
    # One job per configured time; the id carries the time so two runs of the
    # same alert don't collide on a single id.
    for when in sched.incomplete_alert:
        add(f"incomplete_alert@{when}", jobs.run_incomplete_alert, time=when)
    add("english_quiz", jobs.run_english_quiz, time=sched.english_quiz)
    add(
        "asset_prompt",
        jobs.run_asset_prompt,
        time=sched.asset_prompt_time,
        day_of_week=sched.asset_prompt_weekday,
    )
    add(
        "weekly_planning",
        jobs.run_weekly_planning,
        time=sched.weekly_planning_time,
        day_of_week=sched.weekly_planning_weekday,
    )
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
