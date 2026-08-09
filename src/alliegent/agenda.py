"""Domain operations over the Weekly Agenda and Projects databases.

Everything Notion-shaped stays in `integrations.notion`; this module speaks in
agenda items and projects so the jobs and Discord commands stay readable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgendaItem:
    id: str
    title: str
    day: date | None
    status: str | None
    done: bool
    url: str
    project_ids: tuple[str, ...] = ()
    recurring: bool = False
    category: str | None = None


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    status: str | None
    next_action: str
    url: str
    last_activity: date | None = None


class AgendaService:
    def __init__(self, client: NotionClient, config: Config, agenda_db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = agenda_db_id
        self._ds_id: str | None = None
        self._status_type: str | None = None

    @property
    def props(self):
        return self._cfg.agenda.props

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    async def status_type(self) -> str:
        """Detect whether Status is a checkbox, select, or status property.

        People model 'done' all three ways, so we read it from the schema once
        instead of assuming.
        """
        if self._status_type is None:
            schema = await self._client.get_schema(await self.data_source_id())
            definition = schema.get(self.props.status)
            if definition is None:
                raise RuntimeError(
                    f"Agenda database has no property named {self.props.status!r}. "
                    "Run scripts/inspect_notion.py and fix [agenda.props] in alliegent.toml."
                )
            self._status_type = definition["type"]
        return self._status_type

    # -- reads -------------------------------------------------------------

    def _to_item(self, page: dict) -> AgendaItem:
        p = self.props
        return AgendaItem(
            id=page["id"],
            title=n.read_title(page, p.title) or "(untitled)",
            day=n.read_date(page, p.date),
            status=n.read_status(page, p.status),
            done=n.is_done(page, p.status, self._cfg.agenda.status_values["done"]),
            url=n.page_url(page),
            project_ids=tuple(n.read_relation_ids(page, p.project)) if p.project else (),
            recurring=n.read_checkbox(page, p.recurring) if p.recurring else False,
            category=n.read_select(page, p.category) if p.category else None,
        )

    async def items_between(self, start: date, end: date) -> list[AgendaItem]:
        """All agenda items with a date in [start, end], inclusive."""
        ds = await self.data_source_id()
        query_filter = {
            "and": [
                {"property": self.props.date, "date": {"on_or_after": start.isoformat()}},
                {"property": self.props.date, "date": {"on_or_before": end.isoformat()}},
            ]
        }
        sorts = [{"property": self.props.date, "direction": "ascending"}]
        return [
            self._to_item(page)
            async for page in self._client.query(ds, filter=query_filter, sorts=sorts)
        ]

    async def items_on(self, day: date) -> list[AgendaItem]:
        return await self.items_between(day, day)

    async def overdue(self, today: date) -> list[AgendaItem]:
        """Unfinished items dated before today — the ones that quietly pile up."""
        ds = await self.data_source_id()
        query_filter = {
            "property": self.props.date,
            "date": {"before": today.isoformat()},
        }
        sorts = [{"property": self.props.date, "direction": "ascending"}]
        items = [
            self._to_item(page)
            async for page in self._client.query(ds, filter=query_filter, sorts=sorts)
        ]
        return [i for i in items if not i.done]

    # -- writes ------------------------------------------------------------

    async def add_item(
        self,
        title: str,
        day: date,
        *,
        project_id: str | None = None,
        recurring: bool = False,
        category: str | None = None,
    ) -> AgendaItem:
        p = self.props
        properties = {
            p.title: n.title(title),
            p.date: n.date_prop(day),
        }
        if project_id and p.project:
            properties[p.project] = n.relation([project_id])
        if recurring and p.recurring:
            properties[p.recurring] = n.checkbox(True)
        if category and p.category:
            properties[p.category] = n.select(category)
        page = await self._client.create_page(await self.data_source_id(), properties)
        return self._to_item(page)

    async def set_done(self, page_id: str, done: bool = True) -> None:
        values = self._cfg.agenda.status_values
        payload = n.status_value(
            await self.status_type(),
            values["done"] if done else values["todo"],
            done=done,
        )
        await self._client.update_page(page_id, {self.props.status: payload})

    async def reschedule(self, page_id: str, day: date) -> None:
        await self._client.update_page(page_id, {self.props.date: n.date_prop(day)})

    async def plan_week(self, week_start: date) -> list[tuple[str, date, str | None]]:
        """Work out which recurring items are missing from the given week.

        The template is the previous week's recurring rows, copied onto the same
        weekday. That mirrors how the agenda is actually maintained — the same
        lessons and classes reappear each week — instead of inventing empty
        placeholder rows nobody asked for.

        Returns (title, target_day, category) for each item that would be
        created, so callers can preview without writing.
        """
        days = self._cfg.agenda.scaffold_days
        week_end = week_start + timedelta(days=days - 1)
        prev_start = week_start - timedelta(days=7)

        template = [
            item
            for item in await self.items_between(prev_start, prev_start + timedelta(days=6))
            if item.recurring and item.day is not None
        ]
        # Match on (title, day) so re-running the job never duplicates a row.
        existing = {
            (item.title, item.day) for item in await self.items_between(week_start, week_end)
        }

        planned: list[tuple[str, date, str | None]] = []
        for item in template:
            assert item.day is not None
            target = item.day + timedelta(days=7)
            if not (week_start <= target <= week_end):
                continue
            if (item.title, target) in existing:
                continue
            planned.append((item.title, target, item.category))
        return planned

    async def scaffold_week(self, week_start: date) -> list[tuple[str, date, str | None]]:
        """Create the missing recurring rows returned by `plan_week`."""
        planned = await self.plan_week(week_start)
        for title, day, category in planned:
            await self.add_item(title, day, recurring=True, category=category)
        return planned


class ProjectService:
    def __init__(self, client: NotionClient, config: Config, projects_db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = projects_db_id
        self._ds_id: str | None = None

    @property
    def props(self):
        return self._cfg.projects.props

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    def _to_project(self, page: dict) -> Project:
        p = self.props
        return Project(
            id=page["id"],
            title=n.read_title(page, p.title) or "(untitled)",
            status=n.read_status(page, p.status) if p.status else None,
            next_action=n.read_text(page, p.next_action) if p.next_action else "",
            url=n.page_url(page),
            last_activity=n.read_date(page, p.last_activity) if p.last_activity else None,
        )

    async def active(self) -> list[Project]:
        """Projects not marked done. Filtering happens client-side because the
        status property may be a checkbox, select, or status."""
        ds = await self.data_source_id()
        done_value = self._cfg.projects.status_values["done"].casefold()
        projects = [self._to_project(page) async for page in self._client.query(ds)]
        return [
            p
            for p in projects
            if p.status is None or p.status.casefold() != done_value
        ]

    async def stale(
        self, today: date, agenda: AgendaService, *, days: int | None = None
    ) -> list[tuple[Project, date | None]]:
        """Active projects with no agenda activity in the last `days`.

        Activity means an agenda item linked to the project via the relation
        property. If the agenda has no project relation configured, this falls
        back to the project's own last-activity date, and returns nothing when
        neither is available — rather than nagging about every project.
        """
        window = days if days is not None else self._cfg.projects.stale_after_days
        cutoff = today - timedelta(days=window)
        projects = await self.active()

        last_seen: dict[str, date] = {}
        if agenda.props.project:
            recent = await agenda.items_between(cutoff, today)
            for item in recent:
                if item.day is None:
                    continue
                for pid in item.project_ids:
                    if pid not in last_seen or item.day > last_seen[pid]:
                        last_seen[pid] = item.day

        result: list[tuple[Project, date | None]] = []
        for project in projects:
            latest = last_seen.get(project.id) or project.last_activity
            if latest is None:
                # No signal at all — only report when the relation is wired up,
                # where "no linked item in the window" is genuine evidence.
                if agenda.props.project:
                    result.append((project, None))
                continue
            if latest < cutoff:
                result.append((project, latest))
        return result
