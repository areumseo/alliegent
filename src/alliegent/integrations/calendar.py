"""Read-only view of one or more iCalendar (ICS) feeds.

Calendar events stay in the calendar; the agenda database stays for tasks.
Nothing is written to Notion, so there is no sync problem to solve — a moved
or cancelled event simply reads differently tomorrow.

Recurrence is the part that has to be right: an ICS stores "every Tue and Thu
at 19:10" as one VEVENT with an RRULE, plus separate exception entries for the
weeks that were moved or cancelled. Expanding that correctly is what
`recurring_ical_events` is for; doing it by hand gets the common case right
and the exceptions wrong.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 20.0


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime | None  # None for all-day events
    all_day: bool

    def sort_key(self) -> tuple[int, time]:
        # All-day events belong at the top of the day, ahead of timed ones.
        return (0, time.min) if self.all_day or self.start is None else (1, self.start.timetz())


def parse_urls(raw: str) -> list[str]:
    """Split a configured list of feeds, and normalise webcal:// to https://.

    webcal is the scheme calendar apps hand you when you click "share"; it is
    ordinary HTTPS underneath, and httpx does not know it.
    """
    urls = []
    for chunk in raw.replace(",", " ").split():
        url = chunk.strip()
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://") :]
        if url:
            urls.append(url)
    return urls


async def _fetch(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        response = await client.get(url, follow_redirects=True, timeout=FETCH_TIMEOUT)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        # One unreachable feed shouldn't cost the reader the others.
        log.error("calendar feed failed (%s): %s", url.split("?")[0], exc)
        return None


def _events_from(ics: bytes, day: date, tz: ZoneInfo) -> list[Event]:
    calendar = icalendar.Calendar.from_ical(ics)
    events: list[Event] = []
    # `.at(day)` covers the whole day; `.between(day, day)` is a zero-length
    # span and quietly returns nothing.
    for component in recurring_ical_events.of(calendar).at(day):
        start = component.get("DTSTART")
        summary = str(component.get("SUMMARY", "")).strip() or "(untitled)"
        value = start.dt if start is not None else None

        if isinstance(value, datetime):
            # Feeds mix UTC, floating, and zoned times; normalise so the
            # ordering and the printed clock time are both in local terms.
            local = value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
            if local.date() != day:
                # A recurrence can land just outside the day once converted.
                continue
            events.append(Event(summary, local, all_day=False))
        else:
            events.append(Event(summary, None, all_day=True))
    return events


async def events_on(urls: list[str], day: date, tz: ZoneInfo) -> list[Event]:
    """Every event on one day, across all feeds, ordered for reading."""
    if not urls:
        return []

    async with httpx.AsyncClient() as client:
        payloads = await asyncio.gather(*(_fetch(client, url) for url in urls))

    events: list[Event] = []
    for payload in payloads:
        if payload is None:
            continue
        try:
            events.extend(_events_from(payload, day, tz))
        except Exception:
            log.exception("could not parse a calendar feed")

    # Two calendars subscribed to the same event would otherwise print twice.
    unique = {(e.summary, e.start, e.all_day): e for e in events}
    return sorted(unique.values(), key=Event.sort_key)
