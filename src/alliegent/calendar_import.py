"""Copy calendar events into the agenda, so the day lives in one place.

Something put in the calendar -- on the phone, by the person -- should show up
in the Weekly Agenda without being typed twice. Every few minutes the next two
weeks of the calendar are read and each occurrence not already in the agenda
becomes a row: the event's title, its day, its start time when it has one, and
a category taken from the calendar it is in.

**The category comes from the calendar.** The calendar's own groupings (Work,
Personal, a class calendar) are not the agenda's, so a mapping says which
calendar files under which category (`CALENDAR_CATEGORIES`). A calendar left
out of it gets the category inferred from how the same title was filed before,
as /add does; and a mapping that names a category the agenda does not have is
ignored rather than written, since writing it would quietly add a new option.

**An occurrence is added once.** Its key -- the event's UID and the day -- is
written on the row, and also kept in a local file of every key ever imported,
so a row deleted in Notion because it was not wanted does not come back on the
next run. A row that is already there under the same title on the same day is
taken to be the event too: that is how an item added with `/add … cal: true`,
which went into the agenda first, is not imported back as a second copy.

**It adds; it does not follow.** A moved event appears on its new day as a new
row and the old row stays; a cancelled one stays in the agenda. Following edits
and deletions would mean deleting rows on the calendar's say-so, and a misread
deletion costs more than a stale row someone can tick off.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path

from . import reports
from .agenda import AgendaService
from .integrations.calendar import Event

log = logging.getLogger(__name__)

# Keys are forgotten once their day is this far behind, so the file does not
# grow forever. Long enough that nothing in the import window is ever forgotten.
FORGET_AFTER_DAYS = 60


@dataclass(frozen=True)
class Planned:
    """One row the import adds (or, in a preview, would add)."""

    title: str
    day: date
    at: time | None
    category: str | None
    key: str


def parse_categories(raw: str) -> dict[str, str]:
    """`Calendar=Category` pairs, separated by ";" or new lines. Names are
    matched without regard to case; blank or malformed entries are skipped."""
    out: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, _, category = chunk.partition("=")
        if name.strip() and category.strip():
            out[name.strip().casefold()] = category.strip()
    return out


def event_key(event: Event) -> str:
    """The event's UID and day. A recurring event shares one UID across its
    occurrences, so the day is what tells them apart."""
    ident = event.uid or event.summary
    return f"{ident}/{event.day.isoformat() if event.day else ''}"


def _norm(title: str) -> str:
    return " ".join(title.split()).casefold()


# -- what has been imported -------------------------------------------------------


def load_imported(path: Path) -> dict[str, str]:
    """Key -> day (ISO) of every occurrence imported. Empty when there is no
    file yet, or it cannot be read -- the Notion rows still stop duplicates."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("calendar import: could not read %s; starting afresh", path)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_imported(path: Path, keys: dict[str, str], today: date) -> None:
    """Write the keys, dropping those long past, atomically."""
    cutoff = (today - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    kept = {k: d for k, d in keys.items() if d >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False, sort_keys=True))
    os.replace(tmp, path)


# -- the import ----------------------------------------------------------------------


async def plan_import(
    agenda: AgendaService,
    events: list[Event],
    categories: dict[str, str],
    imported: dict[str, str],
    first: date,
    days: int,
) -> list[Planned]:
    """The occurrences not yet in the agenda, with the category each would get.
    Reads the agenda; writes nothing."""
    last = first + timedelta(days=days - 1)
    existing = await agenda.items_between(first, last)
    keys_in_notion = {i.calendar_id for i in existing if i.calendar_id}
    titles_by_day: dict[date, set[str]] = {}
    for item in existing:
        if item.day:
            titles_by_day.setdefault(item.day, set()).add(_norm(item.title))

    offered = set(await agenda.category_names()) if agenda.props.category else set()
    planned: list[Planned] = []
    seen: set[str] = set()
    ordered = sorted(
        (e for e in events if e.day and first <= e.day <= last),
        key=lambda e: (e.day, e.sort_key()),
    )
    for event in ordered:
        key = event_key(event)
        if key in seen or key in imported or key in keys_in_notion:
            continue
        seen.add(key)
        if _norm(event.summary) in titles_by_day.get(event.day, set()):
            continue
        category = categories.get(event.calendar.casefold())
        if category and category not in offered:
            log.warning(
                "calendar import: %r maps to %r, which the agenda has no such "
                "category for; inferring instead", event.calendar, category,
            )
            category = None
        if category is None:
            category = await agenda.guess_category(event.summary, event.day)
        at = None if event.all_day or event.start is None else event.start.time()
        planned.append(Planned(event.summary, event.day, at, category, key))
    return planned


async def run_import(
    agenda: AgendaService,
    events: list[Event],
    categories: dict[str, str],
    imported_path: Path,
    first: date,
    days: int,
    *,
    commit: bool = True,
) -> list[Planned]:
    """Add the new occurrences to the agenda and remember their keys.

    `commit=False` reports what would be added without writing anything, to
    Notion or to the file.
    """
    imported = load_imported(imported_path)
    planned = await plan_import(agenda, events, categories, imported, first, days)
    if not commit:
        return planned
    added: list[Planned] = []
    for row in planned:
        try:
            await agenda.add_item(
                row.title, row.day, at=row.at, category=row.category, calendar_id=row.key
            )
        except Exception:
            # One row Notion will not take should not cost the others; it is
            # not remembered, so the next run tries it again.
            log.exception("calendar import: could not add %r", row.title)
            continue
        imported[row.key] = row.day.isoformat()
        added.append(row)
    # Saved when anything changed, and on a quiet run too, which is when the
    # old keys are pruned.
    if added or not planned:
        save_imported(imported_path, imported, first)
    return added


def import_message(rows: list[Planned], *, preview: bool = False) -> str | None:
    """What was added, for the agenda channel. Nothing when nothing was."""
    if not rows:
        return None
    head = "📅 **From the calendar** — " + (
        "would add" if preview else f"added {len(rows)} to the agenda"
    )
    lines = [head]
    for row in rows:
        when = reports.fmt_date(row.day) + (f" {reports.fmt_time(row.at)}" if row.at else "")
        tag = f" · {row.category}" if row.category else ""
        lines.append(f"• {row.title} ({when}){tag}")
    return "\n".join(lines)
