"""Copy calendar events into the agenda database, and keep them following.

An appointment typed into the calendar used to need typing again as an
agenda row. This job reads the calendars named by ICLOUD_SYNC_CALENDARS every
few minutes and keeps one agenda row per event occurrence:

- a new event becomes a row (title, day, and time; all-day events have none);
- a moved or renamed event moves or renames its row;
- a deleted event sends its row to Notion's trash, where it can be restored.

One way only. The calendar is where the event is typed, so it decides the
day and time. What is typed only in Notion -- status, category, project,
priority -- is never touched, and a title edited in Notion stays unless the
event's own title changes later.

Which row came from which event is kept in a small local SQLite file rather
than in a Notion column, so nothing in the database's schema has to change.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Protocol

from .agenda import AgendaItem, normalise_title
from .integrations.calendar import Occurrence
from .integrations.notion import NotionError

log = logging.getLogger(__name__)

# (first day, last day) -> (occurrences, whether every event could be read)
Fetch = Callable[[date, date], Awaitable[tuple[list[Occurrence], bool]]]


class Agenda(Protocol):
    async def items_between(self, start: date, end: date) -> list[AgendaItem]: ...
    async def add_item(self, title: str, day: date, **kwargs) -> AgendaItem: ...
    async def set_time(self, page_id: str, day: date, at: time | None) -> None: ...
    async def rename(self, page_id: str, title: str) -> None: ...
    async def trash(self, page_id: str) -> None: ...


@dataclass(frozen=True)
class Link:
    key: str
    page_id: str
    day: date
    at: time | None
    summary: str
    # The row was deleted in Notion while the event stayed. Removing it there
    # is an answer -- "not on my list" -- so the event is not copied again.
    detached: bool = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    key TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    day TEXT NOT NULL,
    at TEXT,
    summary TEXT NOT NULL,
    detached INTEGER NOT NULL DEFAULT 0
);
"""


class LinkStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
        self._memory = sqlite3.connect(":memory:") if str(self.path) == ":memory:" else None
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            with self._memory:
                yield self._memory
            return
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self) -> dict[str, Link]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT key, page_id, day, at, summary, detached FROM links"
            ).fetchall()
        return {
            key: Link(
                key,
                page_id,
                date.fromisoformat(day),
                time.fromisoformat(at) if at else None,
                summary,
                bool(detached),
            )
            for key, page_id, day, at, summary, detached in rows
        }

    def put(self, link: Link) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?, ?)",
                (
                    link.key,
                    link.page_id,
                    link.day.isoformat(),
                    link.at.isoformat() if link.at else None,
                    link.summary,
                    int(link.detached),
                ),
            )

    def delete(self, key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM links WHERE key = ?", (key,))

    def prune(self, before: date) -> None:
        """Forget events that have passed; they are no longer followed."""
        with self._connect() as db:
            db.execute("DELETE FROM links WHERE day < ?", (before.isoformat(),))


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Removals not carried out: too many at once, or the read was incomplete.
    held: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.linked or self.moved or self.renamed or self.removed)

    def summary(self) -> str:
        parts = []
        for label, titles in (
            ("added", self.created),
            ("linked to an existing row", self.linked),
            ("moved", self.moved),
            ("renamed", self.renamed),
            ("trashed", self.removed),
            ("held back from trashing", self.held),
        ):
            if titles:
                parts.append(f"{label}: {', '.join(titles)}")
        return "; ".join(parts) or "nothing to change"


def _gone_in_notion(exc: NotionError) -> bool:
    # Archived/trashed pages refuse edits with a 400; a deleted one is a 404.
    return exc.status in (400, 404)


class CalendarSync:
    def __init__(
        self,
        agenda: Agenda,
        store: LinkStore,
        fetch: Fetch,
        *,
        lookahead_days: int = 14,
        max_removals: int = 10,
    ) -> None:
        self.agenda = agenda
        self.store = store
        self.fetch = fetch
        self.lookahead_days = lookahead_days
        self.max_removals = max_removals

    async def run(self, today: date, *, commit: bool = True) -> SyncResult:
        last = today + timedelta(days=self.lookahead_days)
        occurrences, complete = await self.fetch(today, last)
        # The same event in two synced calendars (a shared one, say) is one row.
        current = {o.key: o for o in occurrences}
        links = self.store.all()
        result = SyncResult()

        existing: list[AgendaItem] | None = None
        taken = {link.page_id for link in links.values()}

        for occ in sorted(current.values(), key=lambda o: (o.day, o.at or time.max)):
            link = links.get(occ.key)
            if link is not None:
                if link.detached:
                    continue
                updated = await self._follow(link, occ, result, commit)
                if commit and updated != link:
                    self.store.put(updated)
                continue

            if existing is None:
                existing = await self.agenda.items_between(today, last)
            match = _matching_row(occ, existing, taken)
            if match is not None:
                page_id = match.id
                result.linked.append(occ.summary)
            else:
                result.created.append(occ.summary)
                if not commit:
                    continue
                item = await self.agenda.add_item(
                    occ.summary, occ.day, at=occ.at, infer_category=True
                )
                page_id = item.id
            taken.add(page_id)
            if commit:
                self.store.put(Link(occ.key, page_id, occ.day, occ.at, occ.summary))

        gone = [
            link
            for key, link in links.items()
            if key not in current and not link.detached and today <= link.day <= last
        ]
        if gone and (not complete or len(gone) > self.max_removals):
            result.held = [link.summary for link in gone]
            log.warning(
                "calendar sync: %d removals held back (%s)",
                len(gone),
                "incomplete read" if not complete else "too many at once",
            )
        else:
            for link in gone:
                result.removed.append(link.summary)
                if not commit:
                    continue
                try:
                    await self.agenda.trash(link.page_id)
                except NotionError as exc:
                    if not _gone_in_notion(exc):
                        raise
                self.store.delete(link.key)

        if commit:
            self.store.prune(today)
        return result

    async def _follow(
        self, link: Link, occ: Occurrence, result: SyncResult, commit: bool
    ) -> Link:
        moved = (occ.day, occ.at) != (link.day, link.at)
        renamed = occ.summary != link.summary
        if not (moved or renamed):
            return link
        try:
            if moved:
                result.moved.append(occ.summary)
                if commit:
                    await self.agenda.set_time(link.page_id, occ.day, occ.at)
            if renamed:
                result.renamed.append(occ.summary)
                if commit:
                    await self.agenda.rename(link.page_id, occ.summary)
        except NotionError as exc:
            if not _gone_in_notion(exc):
                raise
            log.info("calendar sync: %r was removed in Notion; no longer followed", occ.summary)
            return Link(link.key, link.page_id, occ.day, occ.at, occ.summary, detached=True)
        return Link(link.key, link.page_id, occ.day, occ.at, occ.summary)


def _matching_row(
    occ: Occurrence, rows: list[AgendaItem], taken: set[str]
) -> AgendaItem | None:
    """A row already typed by hand for this event, if there is one.

    Events were typed twice until now, so the first run meets rows that
    already stand for the events it reads. Same day and the same activity
    once clock times are stripped -- "Cafe shift 11AM" is the calendar's
    "Cafe shift" -- and a row is claimed by one event at most.
    """
    key = normalise_title(occ.summary)
    if not key:
        return None
    for row in rows:
        if row.id in taken or row.day != occ.day:
            continue
        if normalise_title(row.title) == key:
            return row
    return None
