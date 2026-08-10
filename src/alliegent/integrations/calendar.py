"""Read-only view of the calendar, over CalDAV or plain ICS feeds.

CalDAV is the preferred source. An ICS subscription link is unauthenticated:
anyone holding it can read that calendar, and an iCloud one can only be
revoked by unpublishing and republishing. CalDAV authenticates with an
app-specific password instead — nothing is published, and the password can be
revoked on its own from the Apple ID account page.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 20.0

# What the jobs are handed: "give me one day's events", with the source of
# those events already decided.
CalendarSource = Callable[["date", "ZoneInfo"], Awaitable[list["Event"]]]


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


def _dedupe(events: list[Event]) -> list[Event]:
    """Two calendars carrying the same event would otherwise print twice."""
    unique = {(e.summary, e.start, e.all_day): e for e in events}
    return sorted(unique.values(), key=Event.sort_key)


async def events_from_feeds(urls: list[str], day: date, tz: ZoneInfo) -> list[Event]:
    """Every event on one day, across all ICS feeds."""
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
    return _dedupe(events)


CALDAV_URL = "https://caldav.icloud.com"


def _display_name(calendar) -> str:
    try:
        return str(calendar.get_display_name() or "")
    except Exception:
        return ""


def _event_calendars(client) -> list:
    """Calendars that hold events.

    An iCloud account also exposes reminder lists, which accept VTODO only —
    searching or writing events against those is meaningless.
    """
    keep = []
    for calendar in client.principal().calendars():
        try:
            supported = calendar.get_supported_components()
        except Exception:
            supported = ["VEVENT"]  # assume usable rather than skip silently
        if "VEVENT" in supported:
            keep.append(calendar)
    return keep


class CalendarWriteError(RuntimeError):
    """The event could not be created; the caller should say so plainly."""


def _caldav_create(
    url: str,
    username: str,
    password: str,
    calendar_name: str,
    summary: str,
    start: datetime,
    end: datetime,
) -> str:
    import caldav
    from icalendar import Calendar as VCalendar
    from icalendar import Event as VEvent

    with caldav.DAVClient(url=url, username=username, password=password) as client:
        calendars = _event_calendars(client)
        target = next(
            (c for c in calendars if _display_name(c) == calendar_name), None
        )
        if target is None:
            available = ", ".join(sorted(_display_name(c) for c in calendars))
            raise CalendarWriteError(
                f"No calendar named {calendar_name!r}. Available: {available}"
            )

        vcal = VCalendar()
        vcal.add("prodid", "-//alliegent//EN")
        vcal.add("version", "2.0")
        event = VEvent()
        event.add("summary", summary)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", datetime.now(start.tzinfo))
        event.add("uid", f"{uuid4()}@alliegent")
        vcal.add_component(event)

        target.save_event(vcal.to_ical().decode())
    return calendar_name


async def create_event(
    secrets,
    summary: str,
    start: datetime,
    end: datetime,
    url: str = CALDAV_URL,
) -> str:
    """Create one event. Returns the calendar it landed in."""
    if not (secrets.icloud_username and secrets.icloud_app_password):
        raise CalendarWriteError("Calendar writing needs ICLOUD_USERNAME and ICLOUD_APP_PASSWORD.")
    if not secrets.icloud_write_calendar:
        raise CalendarWriteError(
            "No calendar is set to write to. Set ICLOUD_WRITE_CALENDAR to the "
            "name of the calendar new events should go in."
        )
    return await asyncio.to_thread(
        _caldav_create,
        url,
        secrets.icloud_username,
        secrets.icloud_app_password,
        secrets.icloud_write_calendar,
        summary,
        start,
        end,
    )


def make_source(secrets) -> CalendarSource | None:
    """Pick the configured calendar source, CalDAV first.

    Returns None when neither is configured, so the caller can skip the
    calendar entirely rather than calling something that always returns [].
    """
    if secrets.icloud_username and secrets.icloud_app_password:
        names = [n.strip() for n in secrets.icloud_calendars.split(",") if n.strip()]

        async def caldav_source(day: date, tz: ZoneInfo) -> list[Event]:
            return await events_from_caldav(
                secrets.icloud_username, secrets.icloud_app_password, day, tz, names
            )

        return caldav_source

    urls = parse_urls(secrets.calendar_ics_urls)
    if urls:
        log.warning(
            "Using ICS feeds: these links are unauthenticated. CalDAV "
            "(ICLOUD_USERNAME + ICLOUD_APP_PASSWORD) publishes nothing."
        )

        async def ics_source(day: date, tz: ZoneInfo) -> list[Event]:
            return await events_from_feeds(urls, day, tz)

        return ics_source

    return None


def _caldav_fetch(
    url: str, username: str, password: str, day: date, tz: ZoneInfo, names: list[str]
) -> list[Event]:
    """Blocking CalDAV read. Runs in a thread; the library is synchronous."""
    import caldav

    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)

    events: list[Event] = []
    with caldav.DAVClient(url=url, username=username, password=password) as client:
        for calendar in _event_calendars(client):
            if names and _display_name(calendar) not in names:
                continue
            try:
                found = calendar.search(start=start, end=end, event=True)
            except Exception:
                log.exception("CalDAV search failed for calendar %r", calendar.name)
                continue
            for obj in found:
                # Expand each object with the same code the ICS path uses, so
                # recurrence, EXDATE and timezone handling stay identical
                # rather than depending on what the server chose to expand.
                try:
                    events.extend(_events_from(obj.data.encode(), day, tz))
                except Exception:
                    log.exception("could not parse a CalDAV event")
    return events


async def events_from_caldav(
    username: str,
    password: str,
    day: date,
    tz: ZoneInfo,
    names: list[str] | None = None,
    url: str = CALDAV_URL,
) -> list[Event]:
    if not (username and password):
        return []
    events = await asyncio.to_thread(
        _caldav_fetch, url, username, password, day, tz, names or []
    )
    return _dedupe(events)
