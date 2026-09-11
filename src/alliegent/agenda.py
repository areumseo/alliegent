"""Domain operations over the Weekly Agenda and Projects databases.

Everything Notion-shaped stays in `integrations.notion`; this module speaks in
agenda items and projects so the jobs and Discord commands stay readable.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)


def normalise_title(title: str) -> str:
    """Reduce a title to what identifies the activity, dropping the specifics.

    Titles carry their time: "Cafe shift 6PM", "Cafe shift 11AM", "Dance class 7:10PM".
    Matching on the raw string would treat every one of those as a new kind of
    task, so the clock time and any bare numbers come off before comparing.
    """
    text = re.sub(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", " ", title, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    text = re.sub(r"[^\w가-힣]+", " ", text)
    return " ".join(text.split()).casefold()


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
    at: time | None = None
    created: datetime | None = None
    started: bool = False

    def sort_key(self) -> tuple:
        """Where this item sits in its day.

        The clock time decides, because that is the order the day actually
        happens in, and it is the same value whether the item was added from
        Notion or from Discord. Items with no time follow the timed ones in
        the order they were added -- `created` never changes, so adding a task
        cannot renumber the ones already there.

        Notion cannot do this sort itself: its date sort compares full
        timestamps, so a row with a time and a row without are never tied and
        no second sort key is ever consulted.
        """
        return (
            self.day or date.max,
            self.at is None,
            self.at or time.min,
            self.created or datetime.min,
        )


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
        self._closed: set[str] | None = None
        self._started: set[str] | None = None

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
            self._closed = n.closed_statuses(
                definition, self._cfg.agenda.status_values["done"]
            )
            # The In progress group, read the same way. Work already started is
            # worth telling apart from work not begun: they need different
            # things from the person reading the list.
            self._started = n.group_statuses(
                definition, self._cfg.agenda.status_values["doing"]
            ) - self._closed
        return self._status_type

    async def closed(self) -> set[str]:
        """Statuses that mean the item is finished with, Canceled included."""
        if self._closed is None:
            await self.status_type()
        return self._closed or {self._cfg.agenda.status_values["done"]}

    async def status_names(self) -> list[str]:
        """Every status the database offers, in the order Notion lists them."""
        schema = await self._client.get_schema(await self.data_source_id())
        definition = schema.get(self.props.status) or {}
        if definition.get("type") != "status":
            return sorted(self._cfg.agenda.status_values.values())
        return [
            option["name"]
            for option in (definition.get("status") or {}).get("options", [])
            if option.get("name")
        ]

    async def started(self) -> set[str]:
        """Statuses that mean the item is underway."""
        if self._started is None:
            await self.status_type()
        return self._started or {self._cfg.agenda.status_values["doing"]}

    # -- reads -------------------------------------------------------------

    def _sorts(self) -> list[dict]:
        """Ask Notion for date order; the exact order is decided here.

        Every listing sorts through `AgendaItem.sort_key`, because the numbers
        read off one message and typed into another only line up if every
        message orders the day identically -- and Notion's own sort cannot
        express "time, then insertion order".
        """
        return [{"property": self.props.date, "direction": "ascending"}]

    def _to_item(self, page: dict) -> AgendaItem:
        p = self.props
        return AgendaItem(
            id=page["id"],
            title=n.read_title(page, p.title) or "(untitled)",
            day=n.read_date(page, p.date),
            status=n.read_status(page, p.status),
            done=n.is_done(
                page, p.status, self._cfg.agenda.status_values["done"], self._closed
            ),
            url=n.page_url(page),
            project_ids=tuple(n.read_relation_ids(page, p.project)) if p.project else (),
            recurring=n.read_checkbox(page, p.recurring) if p.recurring else False,
            category=n.read_select(page, p.category) if p.category else None,
            at=n.read_time(page, p.date),
            created=n.read_created(page),
            started=bool(
                self._started
                and (n.read_status(page, p.status) or "") in self._started
            ),
        )

    async def items_between(self, start: date, end: date) -> list[AgendaItem]:
        """All agenda items dated in [start, end], inclusive, by local date.

        The query asks for a day either side of the range and the range is
        applied here, because Notion compares date filters as timestamps in
        UTC. An item at 06:30 on the 24th is 21:30 UTC on the 23rd, so a
        filter for the 23rd returns it -- and a 06:00 item on the 23rd, being
        the 22nd in UTC, is missed entirely. Both go unnoticed until items
        start carrying times, which is exactly what orders a day now.
        """
        ds = await self.data_source_id()
        # Resolves the Complete group before any page is mapped; without it the
        # first read of a session would count Cancelled items as outstanding.
        await self.status_type()
        query_filter = {
            "and": [
                {
                    "property": self.props.date,
                    "date": {"on_or_after": (start - timedelta(days=1)).isoformat()},
                },
                {
                    "property": self.props.date,
                    "date": {"on_or_before": (end + timedelta(days=1)).isoformat()},
                },
            ]
        }
        items = [self._to_item(page) async for page in self._client.query(
            ds, filter=query_filter, sorts=self._sorts()
        )]
        inside = [i for i in items if i.day is not None and start <= i.day <= end]
        return sorted(inside, key=AgendaItem.sort_key)

    async def items_on(self, day: date) -> list[AgendaItem]:
        return await self.items_between(day, day)

    async def overdue(self, today: date) -> list[AgendaItem]:
        """Unfinished items dated before today — the ones that quietly pile up."""
        ds = await self.data_source_id()
        await self.status_type()
        query_filter = {
            "property": self.props.date,
            # One day wide, then narrowed below: see items_between for why a
            # timestamp filter cannot be trusted to mean a local date.
            "date": {"before": (today + timedelta(days=1)).isoformat()},
        }
        items = [self._to_item(page) async for page in self._client.query(
            ds, filter=query_filter, sorts=self._sorts()
        )]
        past = (i for i in items if not i.done and i.day is not None and i.day < today)
        return sorted(past, key=AgendaItem.sort_key)

    # -- writes ------------------------------------------------------------

    async def guess_category(self, title: str, today: date) -> str | None:
        """Infer a category from how the same activity was filed before.

        Deliberately history-based rather than a model call: the categories
        that matter are this person's own, and "Cafe shift" meaning Work is a fact
        about their past entries, not something to reason about. It also costs
        one query instead of an API round trip on every add.

        Returns None when nothing matches — better an empty Category than a
        confidently wrong one, which is invisible until it skews a filter.
        """
        if not self.props.category:
            return None

        key = normalise_title(title)
        if not key:
            return None

        lookback = self._cfg.agenda.category_lookback_days
        past = await self.items_between(today - timedelta(days=lookback), today)
        seen = [
            i.category
            for i in past
            if i.category and normalise_title(i.title) == key
        ]
        if not seen:
            return None
        # Most common wins, and ties go to whichever appeared most recently,
        # since Counter keeps insertion order and the query is date-ascending.
        return Counter(reversed(seen)).most_common(1)[0][0]

    async def add_item(
        self,
        title: str,
        day: date,
        *,
        at: time | None = None,
        project_id: str | None = None,
        recurring: bool = False,
        category: str | None = None,
        infer_category: bool = False,
    ) -> AgendaItem:
        p = self.props
        if category is None and infer_category:
            category = await self.guess_category(title, day)
        properties = {
            p.title: n.title(title),
            p.date: n.date_prop(day, at=at, tz=self._cfg.tz),
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

    async def set_time(self, page_id: str, day: date, at: time | None) -> None:
        """Move an item to a clock time, which is what moves it in the day.

        `at=None` clears the time, sending the item to the end of the day
        rather than to midnight.
        """
        await self._client.update_page(
            page_id, {self.props.date: n.date_prop(day, at=at, tz=self._cfg.tz)}
        )

    async def trash(self, page_id: str) -> None:
        """Move an item to Notion's trash — recoverable, not a hard delete."""
        await self._client.trash_page(page_id)

    async def reschedule(self, page_id: str, day: date, at: time | None = None) -> None:
        """Move an item to another day, keeping its time of day."""
        await self._client.update_page(
            page_id, {self.props.date: n.date_prop(day, at=at, tz=self._cfg.tz)}
        )

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
