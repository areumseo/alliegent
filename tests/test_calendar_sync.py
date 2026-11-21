"""Copying calendar events into the agenda.

The parts that matter: an event is copied once, a moved event moves its row
instead of leaving a second one, and nothing is trashed on the strength of a
read that might have been wrong.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

from alliegent.agenda import AgendaItem
from alliegent.calendar_sync import CalendarSync, Link, LinkStore
from alliegent.config import Config
from alliegent.integrations.calendar import Occurrence, _occurrences_from
from alliegent.integrations.notion import NotionError
from alliegent.jobs import Jobs

SEOUL = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 11, 21)


def ics(*events: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n"
        + "".join(events)
        + "END:VCALENDAR\n"
    ).encode()


def vevent(uid: str, start: str, summary: str, extra: str = "") -> str:
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nDTSTART;TZID=Asia/Seoul:{start}\n"
        f"{extra}SUMMARY:{summary}\nEND:VEVENT\n"
    )


# -- reading occurrences -----------------------------------------------------


def test_a_one_off_event_keeps_its_key_when_moved():
    before = _occurrences_from(
        ics(vevent("abc", "20261123T150000", "Dentist")), TODAY, date(2026, 12, 5), SEOUL
    )
    after = _occurrences_from(
        ics(vevent("abc", "20261124T100000", "Dentist")), TODAY, date(2026, 12, 5), SEOUL
    )
    assert before[0].key == after[0].key == "abc"
    assert (after[0].day, after[0].at) == (date(2026, 11, 24), time(10, 0))


def test_each_occurrence_of_a_series_has_its_own_key():
    weekly = vevent("dance", "20261103T191000", "Dance class", "RRULE:FREQ=WEEKLY;BYDAY=TU,TH\n")
    found = _occurrences_from(ics(weekly), TODAY, date(2026, 11, 30), SEOUL)
    assert [o.day for o in found] == [date(2026, 11, 24), date(2026, 11, 26)]
    assert len({o.key for o in found}) == 2
    assert all(o.at == time(19, 10) for o in found)


def test_a_moved_occurrence_keeps_the_key_of_its_slot():
    weekly = vevent("dance", "20261103T191000", "Dance class", "RRULE:FREQ=WEEKLY;BYDAY=TU\n")
    plain = _occurrences_from(ics(weekly), TODAY, date(2026, 11, 30), SEOUL)
    moved = (
        "BEGIN:VEVENT\nUID:dance\nRECURRENCE-ID;TZID=Asia/Seoul:20261124T191000\n"
        "DTSTART;TZID=Asia/Seoul:20261125T200000\nSUMMARY:Dance class\nEND:VEVENT\n"
    )
    shifted = _occurrences_from(ics(weekly, moved), TODAY, date(2026, 11, 30), SEOUL)
    assert [o.key for o in shifted] == [o.key for o in plain]
    assert (shifted[0].day, shifted[0].at) == (date(2026, 11, 25), time(20, 0))


def test_events_the_bot_wrote_are_not_copied_back():
    own = vevent("123@alliegent", "20261123T150000", "From /add")
    assert _occurrences_from(ics(own), TODAY, date(2026, 12, 5), SEOUL) == []


def test_cancelled_events_are_skipped():
    gone = vevent("x", "20261123T150000", "Off", "STATUS:CANCELLED\n")
    assert _occurrences_from(ics(gone), TODAY, date(2026, 12, 5), SEOUL) == []


def test_an_all_day_event_has_no_time():
    allday = "BEGIN:VEVENT\nUID:h\nDTSTART;VALUE=DATE:20261125\nSUMMARY:Trip\nEND:VEVENT\n"
    [occ] = _occurrences_from(ics(allday), TODAY, date(2026, 12, 5), SEOUL)
    assert (occ.day, occ.at) == (date(2026, 11, 25), None)


def test_days_outside_the_window_are_left_out():
    far = vevent("far", "20261225T100000", "Later")
    assert _occurrences_from(ics(far), TODAY, date(2026, 12, 5), SEOUL) == []


# -- syncing -----------------------------------------------------------------


class FakeAgenda:
    def __init__(self, rows: list[AgendaItem] | None = None) -> None:
        self.rows = {r.id: r for r in rows or []}
        self.trashed: list[str] = []
        self.refuse: set[str] = set()
        self._n = 0

    async def items_between(self, start, end):
        return [r for r in self.rows.values() if r.day and start <= r.day <= end]

    async def add_item(self, title, day, *, at=None, infer_category=False, **_):
        self._n += 1
        item = AgendaItem(f"p{self._n}", title, day, None, False, "", at=at)
        self.rows[item.id] = item
        return item

    def _check(self, page_id):
        if page_id in self.refuse:
            raise NotionError(400, "validation_error", "Can't edit block that is archived")

    async def set_time(self, page_id, day, at):
        self._check(page_id)
        self.rows[page_id] = replace(self.rows[page_id], day=day, at=at)

    async def rename(self, page_id, title):
        self._check(page_id)
        self.rows[page_id] = replace(self.rows[page_id], title=title)

    async def trash(self, page_id):
        self.trashed.append(page_id)
        self.rows.pop(page_id, None)


def occ(key, summary, day, at=None):
    return Occurrence(key, summary, day, at, "Personal")


class Calendar:
    def __init__(self, *events: Occurrence) -> None:
        self.events = list(events)
        self.complete = True

    async def __call__(self, first, last):
        return [e for e in self.events if first <= e.day <= last], self.complete


@pytest.fixture
def store(tmp_path):
    return LinkStore(tmp_path / "sync.db")


def make(agenda, store, calendar, **kw):
    return CalendarSync(agenda, store, calendar, **kw)


async def test_a_new_event_becomes_a_row_once(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23), time(15)))
    sync = make(agenda, store, cal)
    first = await sync.run(TODAY)
    second = await sync.run(TODAY)
    assert first.created == ["Dentist"]
    assert not second.changed
    [row] = agenda.rows.values()
    assert (row.title, row.day, row.at) == ("Dentist", date(2026, 11, 23), time(15))


async def test_a_row_already_typed_by_hand_is_linked_not_duplicated(store):
    typed = AgendaItem("mine", "Cafe shift 11AM", date(2026, 11, 22), None, False, "")
    agenda = FakeAgenda([typed])
    cal = Calendar(occ("s", "Cafe shift", date(2026, 11, 22), time(11)))
    result = await make(agenda, store, cal).run(TODAY)
    assert result.linked == ["Cafe shift"] and not result.created
    assert list(agenda.rows) == ["mine"]
    assert store.all()["s"].page_id == "mine"


async def test_a_moved_event_moves_its_row(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23), time(15)))
    sync = make(agenda, store, cal)
    await sync.run(TODAY)
    cal.events = [occ("a", "Dentist", date(2026, 11, 24), time(10))]
    result = await sync.run(TODAY)
    assert result.moved == ["Dentist"]
    [row] = agenda.rows.values()
    assert (row.day, row.at) == (date(2026, 11, 24), time(10))


async def test_a_renamed_event_renames_its_row_but_notion_edits_otherwise_stay(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23), time(15)))
    sync = make(agenda, store, cal)
    await sync.run(TODAY)
    await agenda.rename("p1", "Dentist (bring card)")  # typed in Notion
    await sync.run(TODAY)
    assert agenda.rows["p1"].title == "Dentist (bring card)"
    cal.events = [occ("a", "Dentist checkup", date(2026, 11, 23), time(15))]
    await sync.run(TODAY)
    assert agenda.rows["p1"].title == "Dentist checkup"


async def test_a_deleted_event_trashes_its_row(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23), time(15)))
    sync = make(agenda, store, cal)
    await sync.run(TODAY)
    cal.events = []
    result = await sync.run(TODAY)
    assert result.removed == ["Dentist"] and agenda.trashed == ["p1"]
    assert store.all() == {}


async def test_many_removals_at_once_are_held_back(store):
    agenda = FakeAgenda()
    cal = Calendar(*(occ(str(i), f"E{i}", date(2026, 11, 23)) for i in range(4)))
    sync = make(agenda, store, cal, max_removals=3)
    await sync.run(TODAY)
    cal.events = []
    result = await sync.run(TODAY)
    assert len(result.held) == 4 and agenda.trashed == []
    assert len(store.all()) == 4


async def test_an_incomplete_read_removes_nothing(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23)))
    sync = make(agenda, store, cal)
    await sync.run(TODAY)
    cal.events, cal.complete = [], False
    result = await sync.run(TODAY)
    assert result.held == ["Dentist"] and agenda.trashed == []


async def test_a_row_deleted_in_notion_is_not_brought_back(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23), time(15)))
    sync = make(agenda, store, cal)
    await sync.run(TODAY)
    agenda.refuse.add("p1")
    cal.events = [occ("a", "Dentist", date(2026, 11, 24), time(15))]
    await sync.run(TODAY)
    assert store.all()["a"].detached
    await sync.run(TODAY)
    assert len(agenda.rows) == 1  # nothing re-created


async def test_a_preview_writes_nothing(store):
    agenda = FakeAgenda()
    cal = Calendar(occ("a", "Dentist", date(2026, 11, 23)))
    result = await make(agenda, store, cal).run(TODAY, commit=False)
    assert result.created == ["Dentist"]
    assert agenda.rows == {} and store.all() == {}


async def test_past_links_are_forgotten_and_never_trashed(store):
    store.put(Link("old", "p0", date(2026, 11, 20), None, "Yesterday"))
    agenda = FakeAgenda()
    result = await make(agenda, store, Calendar()).run(TODAY)
    assert agenda.trashed == [] and not result.removed
    assert store.all() == {}


# -- the scheduled job -------------------------------------------------------


async def test_a_lasting_failure_is_reported_once_and_its_recovery_once(store):
    sent: list[str] = []

    async def notify(message, kind):
        sent.append(message)

    class Broken(Exception):
        pass

    cal = Calendar()

    async def flaky(first, last):
        if cal.complete is None:
            raise Broken("No calendar named 'Work'")
        return [], True

    jobs = Jobs(
        None, None, Config(), notify, clock=lambda: TODAY,
        calendar_sync=CalendarSync(FakeAgenda(), store, flaky),
    )
    cal.complete = None
    await jobs.run_calendar_sync()
    await jobs.run_calendar_sync()
    assert len(sent) == 1 and "Work" in sent[0]
    cal.complete = True
    await jobs.run_calendar_sync()
    await jobs.run_calendar_sync()
    assert len(sent) == 2 and "working again" in sent[1]


def test_the_job_is_scheduled_only_when_configured(store):
    from alliegent.scheduler import build_scheduler

    async def notify(message, kind):
        pass

    off = build_scheduler(Jobs(None, None, Config(), notify), Config())
    assert off.get_job("calendar_sync") is None
    on = build_scheduler(
        Jobs(None, None, Config(), notify,
             calendar_sync=CalendarSync(FakeAgenda(), store, Calendar())),
        Config(),
    )
    assert on.get_job("calendar_sync") is not None
