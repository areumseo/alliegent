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
from datetime import date, timedelta

from . import reports
from .agenda import AgendaService, ProjectService
from .config import Config

log = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]


class Jobs:
    def __init__(
        self,
        agenda: AgendaService,
        projects: ProjectService | None,
        config: Config,
        notify: Notifier,
    ) -> None:
        self.agenda = agenda
        self.projects = projects
        self.config = config
        self.notify = notify

    def today(self) -> date:
        from datetime import datetime

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

    async def week_scaffold(self, *, commit: bool = True) -> str | None:
        """Create placeholder rows for upcoming dates that have none.

        Unlike the other jobs this one writes to Notion, so `commit=False`
        reports what *would* be created without touching the database.
        """
        today = self.today()
        days = self.config.agenda.scaffold_days
        if not commit:
            end = today + timedelta(days=days - 1)
            existing = {i.day for i in await self.agenda.items_between(today, end)}
            missing = [
                today + timedelta(days=offset)
                for offset in range(days)
                if today + timedelta(days=offset) not in existing
            ]
            return reports.week_scaffold(missing)

        created = await self.agenda.ensure_days(today, days, "{date}")
        return reports.week_scaffold(created)

    # -- run + send --------------------------------------------------------

    async def _send(self, message: str | None) -> None:
        if message is None:
            log.info("nothing to report; staying quiet")
            return
        await self.notify(message)

    async def run_daily_brief(self) -> None:
        await self._send(await self.build_daily_brief())

    async def run_incomplete_alert(self) -> None:
        await self._send(await self.build_incomplete_alert())

    async def run_stale_projects(self) -> None:
        await self._send(await self.build_stale_projects())

    async def run_weekly_review(self) -> None:
        await self._send(await self.build_weekly_review())

    async def run_week_scaffold(self) -> None:
        await self._send(await self.week_scaffold(commit=True))
