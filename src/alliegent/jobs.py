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
from .config import Config

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
    ) -> None:
        self.agenda = agenda
        self.projects = projects
        self.config = config
        self.notify = notify
        # Injectable so tests can pin a date instead of drifting with the
        # calendar; production leaves it as the configured timezone's today.
        self._clock = clock

    def today(self) -> date:
        if self._clock is not None:
            return self._clock()
        return datetime.now(self.config.tz).date()

    # -- message builders --------------------------------------------------

    async def build_daily_brief(self) -> str:
        today = self.today()
        todays = await self.agenda.items_on(today)
        overdue = await self.agenda.overdue(today)
        active = await self.projects.active() if self.projects else []
        return reports.daily_brief(today, todays, overdue, active)

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

    async def run_daily_brief(self) -> None:
        await self._send(await self.build_daily_brief(), "agenda")

    async def run_incomplete_alert(self) -> None:
        await self._send(await self.build_incomplete_alert(), "agenda")

    async def run_stale_projects(self) -> None:
        await self._send(await self.build_stale_projects(), "projects")

    async def run_weekly_review(self) -> None:
        await self._send(await self.build_weekly_review(), "review")

    async def run_week_scaffold(self) -> None:
        await self._send(await self.week_scaffold(commit=True), "agenda")
