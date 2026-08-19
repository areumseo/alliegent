"""Scheduled jobs.

Each job *computes* its message and returns it (or None when there is nothing
worth saying). Sending is a separate step, which keeps the jobs testable and
makes `--dry-run` honest: it runs the same code path, minus the send.

The one job that writes to Notion (week_scaffold) takes an explicit `commit`
flag so a dry run can report what it would create without creating it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from . import reports
from .agenda import AgendaService, ProjectService
from .config import Config, Secrets

log = logging.getLogger(__name__)

# (message, channel_kind) -> None. The kind is routed to a channel by the
# notifier, so jobs stay unaware of Discord IDs.
Notifier = Callable[[str, str], Awaitable[None]]


class Jobs:
    def __init__(
        self,
        agenda: AgendaService,
        projects: ProjectService | None,
        config: Config,
        notify: Notifier,
        clock: Callable[[], date] | None = None,
        anthropic_api_key: str = "",
        calendar_source: Callable | None = None,
        secrets: Secrets | None = None,
    ) -> None:
        self.calendar_source = calendar_source
        self.agenda = agenda
        self.projects = projects
        self.config = config
        self.notify = notify
        self._anthropic_api_key = anthropic_api_key
        self._secrets = secrets
        # Injectable so tests can pin a date instead of drifting with the
        # calendar; production leaves it as the configured timezone's today.
        self._clock = clock

    def today(self) -> date:
        if self._clock is not None:
            return self._clock()
        return datetime.now(self.config.tz).date()

    # -- message builders --------------------------------------------------

    async def calendar_on(self, day: date) -> list:
        """Today's calendar events, or nothing if the feeds can't be read.

        A calendar outage should cost the brief its calendar block, not the
        whole brief.
        """
        if self.calendar_source is None:
            return []
        try:
            return await self.calendar_source(day, self.config.tz)
        except Exception:
            log.exception("calendar lookup failed")
            return []

    async def build_daily_brief(self) -> str:
        today = self.today()
        todays = await self.agenda.items_on(today)
        overdue = await self.agenda.overdue(today)
        active = await self.projects.active() if self.projects else []
        events = await self.calendar_on(today)
        return reports.daily_brief(today, todays, overdue, active, events)

    async def build_incomplete_alert(self) -> str | None:
        today = self.today()
        todays = await self.agenda.items_on(today)
        overdue = await self.agenda.overdue(today)
        return reports.incomplete_alert(today, todays, overdue)

    async def build_stale_projects(self) -> str | None:
        if not self.projects:
            return None
        stale = await self.projects.stale(self.today(), self.agenda)
        return reports.stale_projects(stale)

    async def build_ai_news(self) -> str | None:
        """Fetch and format today's AI news digest.

        Returns None when the digest can't be produced. A missing morning
        digest is a non-event; a stack trace in the news channel is not.
        """
        if not self._anthropic_api_key:
            log.warning("ANTHROPIC_API_KEY not set — AI news is disabled")
            return None

        from .integrations.claude import NewsUnavailable, fetch_ai_news

        today = self.today()
        # The one job that costs money per run, so the call is on the record.
        log.info("requesting the AI news digest")
        try:
            body = await fetch_ai_news(
                self._anthropic_api_key, today, count=self.config.news.count
            )
        except NewsUnavailable as exc:
            log.error("AI news unavailable: %s", exc)
            return None
        return reports.ai_news(today, body)

    async def build_weekly_planning(self) -> str:
        """Nudge to plan the coming week, with what is already in it.

        Always sends, even when the week is empty — an empty week is exactly
        when the reminder is worth having.
        """
        today = self.today()
        week_start = self.coming_monday(today)
        items = await self.agenda.items_between(week_start, week_start + timedelta(days=6))
        overdue = await self.agenda.overdue(today)
        return reports.weekly_planning(week_start, items, overdue)

    async def build_weekly_review(self) -> str:
        today = self.today()
        start = today - timedelta(days=6)
        items = await self.agenda.items_between(start, today)
        return reports.weekly_review(start, today, items)

    def coming_monday(self, today: date) -> date:
        """The Monday of the week to scaffold — today if it already is Monday."""
        return today + timedelta(days=(7 - today.weekday()) % 7)

    async def week_scaffold(self, *, commit: bool = True) -> str | None:
        """Copy last week's recurring items onto the coming week.

        Unlike the other jobs this one writes to Notion, so `commit=False`
        reports what *would* be created without touching the database.
        """
        week_start = self.coming_monday(self.today())
        if commit:
            created = await self.agenda.scaffold_week(week_start)
        else:
            created = await self.agenda.plan_week(week_start)
        return reports.week_scaffold(week_start, created)

    # -- run + send --------------------------------------------------------

    async def _send(self, message: str | None, kind: str) -> None:
        if message is None:
            log.info("nothing to report for %s; staying quiet", kind)
            return
        await self.notify(message, kind)
        # Logged on every send so "did this go out once or twice?" is a
        # question the log can answer. A second instance is invisible from
        # inside either one -- each looks perfectly healthy -- and for the
        # news digest a duplicate is a duplicate model call, so the count
        # is worth being able to check.
        log.info("sent %s to the %s channel", kind, kind)

    async def run_daily_brief(self) -> None:
        await self._send(await self.build_daily_brief(), "agenda")

    async def run_incomplete_alert(self) -> None:
        await self._send(await self.build_incomplete_alert(), "agenda")

    async def run_stale_projects(self) -> None:
        await self._send(await self.build_stale_projects(), "projects")

    async def run_ai_news(self) -> None:
        message = await self.build_ai_news()
        await self._send(message, "news")

        if (
            message is None
            or self._secrets is None
            or not self._secrets.ai_news_email_to
        ):
            return

        from .integrations.email import send_ai_news_email

        subject = f"Alliegent AI News — {self.today().isoformat()}"

        try:
            await send_ai_news_email(self._secrets, subject, message)
            log.info(
                "AI news email sent to %s",
                self._secrets.ai_news_email_to,
            )
        except Exception:
            # Email failure must not prevent or undo Discord delivery.
            log.exception("AI news email delivery failed")

    async def run_weekly_planning(self) -> None:
        await self._send(await self.build_weekly_planning(), "agenda")

    async def run_weekly_review(self) -> None:
        await self._send(await self.build_weekly_review(), "review")

    async def run_week_scaffold(self) -> None:
        await self._send(await self.week_scaffold(commit=True), "agenda")
