"""Copying calendar events into the agenda. Calendar names, titles and
categories are invented."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alliegent import calendar_import as ci
from alliegent.agenda import AgendaItem
from alliegent.integrations.calendar import Event, _events_from

TZ = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 11, 28)
OFFERED = ["Work", "Exercise", "Personal", "Study"]


def ev(summary, day, *, at=None, cal="Work", uid=None):
    start = datetime.combine(day, at, tzinfo=TZ) if at else None
    return Event(summary, start, at is None, uid or f"{summary}-uid", cal, day)


def item(title, day, *, key=""):
    return AgendaItem(
        id=title, title=title, day=day, status="Not started", done=False, url="",
        calendar_id=key,
    )


class Agenda:
    """The parts of AgendaService the import uses."""

    def __init__(self, existing=(), guesses=None, fail=()):
        self.existing = list(existing)
        self.guesses = guesses or {}
        self.fail = set(fail)
        self.added = []
        self.props = SimpleNamespace(category="Category", calendar_id="Calendar ID")

    async def items_between(self, first, last):
        return [i for i in self.existing if i.day and first <= i.day <= last]

    async def category_names(self):
        return OFFERED

    async def guess_category(self, title, day):
        return self.guesses.get(title)

    async def add_item(self, title, day, *, at=None, category=None, calendar_id=None):
        if title in self.fail:
            raise RuntimeError("Notion said no")
        self.added.append((title, day, at, category, calendar_id))


def plan(agenda, events, mapping="Work=Work; Gym=Exercise", imported=None, days=14):
    return ci.plan_import(
        agenda, events, ci.parse_categories(mapping), imported or {}, TODAY, days
    )


# -- the mapping -----------------------------------------------------------------


def test_the_mapping_reads_pairs_separated_by_semicolons_or_lines():
    got = ci.parse_categories("Work=Work; Gym = Exercise\nFamily=Personal;;broken")
    assert got == {"work": "Work", "gym": "Exercise", "family": "Personal"}


def test_a_key_is_the_uid_and_the_day():
    assert ci.event_key(ev("Class", TODAY, uid="abc")) == "abc/2026-11-28"


# -- what is planned -----------------------------------------------------------------


async def test_a_new_event_becomes_a_row_with_its_calendars_category_and_time():
    [row] = await plan(Agenda(), [ev("Shift", TODAY, at=time(9, 30), cal="work")])
    assert (row.title, row.day, row.at, row.category) == ("Shift", TODAY, time(9, 30), "Work")
    assert row.key == "Shift-uid/2026-11-28"


async def test_an_all_day_event_has_no_time():
    [row] = await plan(Agenda(), [ev("Trip", TODAY, cal="Gym")])
    assert row.at is None and row.category == "Exercise"


async def test_an_unmapped_calendar_takes_the_category_from_past_titles():
    agenda = Agenda(guesses={"Piano": "Study"})
    [row] = await plan(agenda, [ev("Piano", TODAY, at=time(19), cal="Lessons")])
    assert row.category == "Study"


async def test_a_mapping_to_a_category_the_agenda_lacks_is_not_written():
    """Writing it would quietly add a new option to the column."""
    agenda = Agenda(guesses={"Run": None})
    [row] = await plan(agenda, [ev("Run", TODAY, cal="Gym")], mapping="Gym=Fitness")
    assert row.category is None


async def test_an_occurrence_already_imported_is_not_planned_again():
    event = ev("Shift", TODAY, at=time(9))
    assert await plan(Agenda(), [event], imported={ci.event_key(event): "2026-11-28"}) == []


async def test_a_row_carrying_the_key_in_notion_stops_it_too():
    event = ev("Shift", TODAY, at=time(9))
    agenda = Agenda([item("Renamed shift", TODAY, key=ci.event_key(event))])
    assert await plan(agenda, [event]) == []


async def test_the_same_title_on_the_same_day_is_taken_to_be_the_event():
    """How an item added with /add … cal: true is not imported back."""
    agenda = Agenda([item("dentist  appointment", TODAY)])
    assert await plan(agenda, [ev("Dentist Appointment", TODAY, at=time(15))]) == []


async def test_the_same_title_on_another_day_is_still_added():
    agenda = Agenda([item("Shift", TODAY)])
    [row] = await plan(agenda, [ev("Shift", TODAY + timedelta(days=1), at=time(9))])
    assert row.day == TODAY + timedelta(days=1)


async def test_each_occurrence_of_a_recurring_event_is_its_own_row():
    rows = await plan(Agenda(), [
        ev("Gym", TODAY, at=time(7), uid="r1"),
        ev("Gym", TODAY + timedelta(days=2), at=time(7), uid="r1"),
    ])
    assert [r.key for r in rows] == ["r1/2026-11-28", "r1/2026-11-30"]


async def test_events_outside_the_window_are_left_alone():
    events = [ev("Past", TODAY - timedelta(days=1)), ev("Far", TODAY + timedelta(days=14))]
    assert await plan(Agenda(), events) == []


async def test_rows_come_in_the_order_of_the_day():
    rows = await plan(Agenda(), [
        ev("Late", TODAY, at=time(18)), ev("All day", TODAY), ev("Early", TODAY, at=time(8)),
    ])
    assert [r.title for r in rows] == ["All day", "Early", "Late"]


# -- writing ------------------------------------------------------------------------------


async def test_a_run_adds_the_rows_and_remembers_their_keys(tmp_path):
    path = tmp_path / "imported.json"
    agenda = Agenda()
    added = await ci.run_import(
        agenda, [ev("Shift", TODAY, at=time(9))], ci.parse_categories("Work=Work"),
        path, TODAY, 14,
    )
    assert [r.title for r in added] == ["Shift"]
    assert agenda.added == [("Shift", TODAY, time(9), "Work", "Shift-uid/2026-11-28")]
    assert json.loads(path.read_text()) == {"Shift-uid/2026-11-28": "2026-11-28"}


async def test_a_row_deleted_in_notion_is_not_brought_back(tmp_path):
    path = tmp_path / "imported.json"
    event = ev("Shift", TODAY, at=time(9))
    await ci.run_import(Agenda(), [event], {}, path, TODAY, 14)
    again = Agenda()   # the row is gone from Notion
    assert await ci.run_import(again, [event], {}, path, TODAY, 14) == []
    assert again.added == []


async def test_a_preview_writes_nothing_anywhere(tmp_path):
    path = tmp_path / "imported.json"
    agenda = Agenda()
    rows = await ci.run_import(
        agenda, [ev("Shift", TODAY)], {}, path, TODAY, 14, commit=False
    )
    assert [r.title for r in rows] == ["Shift"]
    assert agenda.added == [] and not path.exists()


async def test_a_row_notion_refuses_is_not_remembered_so_it_is_tried_again(tmp_path):
    path = tmp_path / "imported.json"
    agenda = Agenda(fail={"Broken"})
    added = await ci.run_import(
        agenda, [ev("Broken", TODAY), ev("Fine", TODAY)], {}, path, TODAY, 14
    )
    assert [r.title for r in added] == ["Fine"]
    assert list(json.loads(path.read_text())) == ["Fine-uid/2026-11-28"]


def test_old_keys_are_forgotten_and_recent_ones_kept(tmp_path):
    path = tmp_path / "imported.json"
    old = (TODAY - timedelta(days=ci.FORGET_AFTER_DAYS + 1)).isoformat()
    ci.save_imported(path, {"old/x": old, "new/y": TODAY.isoformat()}, TODAY)
    assert ci.load_imported(path) == {"new/y": TODAY.isoformat()}


def test_a_missing_or_unreadable_file_reads_as_nothing_imported(tmp_path):
    assert ci.load_imported(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ci.load_imported(bad) == {}


# -- the message ----------------------------------------------------------------------------


def test_the_message_lists_what_was_added():
    text = ci.import_message([
        ci.Planned("Shift", TODAY, time(9, 30), "Work", "k1"),
        ci.Planned("Trip", TODAY, None, None, "k2"),
    ])
    assert text.splitlines() == [
        "📅 **From the calendar** — added 2 to the agenda",
        "• Shift (Sat 11/28 09:30) · Work",
        "• Trip (Sat 11/28)",
    ]


def test_nothing_added_is_no_message():
    assert ci.import_message([]) is None


# -- what the calendar gives ------------------------------------------------------------------


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:gym-1
SUMMARY:Gym
DTSTART;TZID=Asia/Seoul:20261128T070000
DTEND;TZID=Asia/Seoul:20261128T080000
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:trip-1
SUMMARY:Trip
DTSTART;VALUE=DATE:20261129
DTEND;VALUE=DATE:20261130
END:VEVENT
END:VCALENDAR
"""


def test_an_event_carries_its_uid_its_calendar_and_its_day():
    events = _events_from(ICS, date(2026, 11, 29), TZ, "Fitness")
    gym = next(e for e in events if e.summary == "Gym")
    assert (gym.uid, gym.calendar, gym.day) == ("gym-1", "Fitness", date(2026, 11, 29))
    assert gym.start.time() == time(7)


def test_an_all_day_event_carries_its_day_too():
    events = _events_from(ICS, date(2026, 11, 29), TZ, "Fitness")
    trip = next(e for e in events if e.summary == "Trip")
    assert trip.all_day and trip.day == date(2026, 11, 29) and trip.uid == "trip-1"


# -- the job ---------------------------------------------------------------------------------


def jobs(range_source, *, calendar_id="Calendar ID", notify=None, tmp_path=None):
    from alliegent.config import Config, Secrets
    from alliegent.jobs import Jobs

    config = Config()
    if tmp_path is not None:
        config.calendar_import.imported_path = str(tmp_path / "imported.json")
    agenda = Agenda()
    agenda.props.calendar_id = calendar_id

    async def sent(message, kind):
        (notify if notify is not None else []).append((message, kind))

    return Jobs(
        agenda, None, config, sent, clock=lambda: TODAY,
        calendar_range=range_source, secrets=Secrets(calendar_categories="Work=Work"),
    ), agenda


async def test_without_a_calendar_or_the_column_the_job_does_nothing():
    job, _ = jobs(None)
    assert await job.calendar_import() is None

    async def source(first, days, tz):
        raise AssertionError("read the calendar")

    job, _ = jobs(source, calendar_id="")
    assert await job.calendar_import() is None


async def test_the_job_reads_the_window_and_reports_what_it_added(tmp_path):
    seen = {}

    async def source(first, days, tz):
        seen["span"] = (first, days)
        return [ev("Shift", TODAY, at=time(9))]

    sent = []
    job, agenda = jobs(source, notify=sent, tmp_path=tmp_path)
    await job.run_calendar_import()
    assert seen["span"] == (TODAY, 14)
    assert agenda.added[0][3] == "Work"
    [(message, kind)] = sent
    assert kind == "agenda" and "Shift" in message


async def test_an_unreachable_calendar_says_so_once_a_day_not_every_run(tmp_path):
    async def source(first, days, tz):
        raise ConnectionError("down")

    sent = []
    job, _ = jobs(source, notify=sent, tmp_path=tmp_path)
    await job.run_calendar_import()
    await job.run_calendar_import()
    assert len(sent) == 1 and "failed" in sent[0][0]


@pytest.mark.parametrize("raw", ["", "nothing here"])
def test_no_mapping_is_an_empty_mapping(raw):
    assert ci.parse_categories(raw) == {}


async def test_the_brief_does_not_print_an_event_twice_once_it_is_in_the_agenda():
    from alliegent.config import Config
    from alliegent.jobs import Jobs

    class DayAgenda(Agenda):
        async def items_on(self, day):
            return [item("Shift", day)]

        async def overdue(self, day):
            return []

    async def calendar(day, tz):
        return [ev("shift", day, at=time(9)), ev("Dinner", day, at=time(19))]

    async def sent(message, kind):
        pass

    job = Jobs(DayAgenda(), None, Config(), sent, clock=lambda: TODAY,
               calendar_source=calendar)
    text = await job.build_daily_brief()
    assert "Dinner" in text
    assert text.count("Shift") + text.count("shift") == 1
