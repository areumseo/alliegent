"""Calendar feed parsing.

Recurrence and timezones are where an ICS reader goes wrong quietly: the
common case looks right while moved or cancelled occurrences don't, and a
feed in UTC prints the wrong clock time without failing.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from alliegent.integrations.calendar import Event, _events_from, parse_urls

SEOUL = ZoneInfo("Asia/Seoul")


def ics(*events: str) -> bytes:
    body = "".join(events)
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n" + body + "END:VCALENDAR\n"
    ).encode()


TIMED = (
    "BEGIN:VEVENT\nUID:1\n"
    "DTSTART;TZID=Asia/Seoul:20260810T191000\n"
    "DTEND;TZID=Asia/Seoul:20260810T200000\n"
    "SUMMARY:Ballet\nEND:VEVENT\n"
)


# -- URL handling ----------------------------------------------------------


def test_webcal_is_rewritten_to_https():
    """webcal:// is what a calendar app hands you; httpx doesn't know it."""
    assert parse_urls("webcal://p1.icloud.com/x.ics") == ["https://p1.icloud.com/x.ics"]


def test_several_feeds_can_be_listed():
    assert parse_urls("https://a/x.ics, https://b/y.ics") == [
        "https://a/x.ics",
        "https://b/y.ics",
    ]


def test_whitespace_and_newlines_separate_too():
    assert len(parse_urls(" https://a/x.ics \n https://b/y.ics ")) == 2


def test_no_feeds_configured():
    assert parse_urls("") == []


# -- parsing ---------------------------------------------------------------


def test_a_timed_event_keeps_its_local_clock_time():
    events = _events_from(ics(TIMED), date(2026, 8, 10), SEOUL)
    assert [e.summary for e in events] == ["Ballet"]
    assert events[0].start.strftime("%H:%M") == "19:10"
    assert events[0].all_day is False


def test_a_utc_event_is_converted_to_local_time():
    """A feed in UTC would otherwise print nine hours off, with no error."""
    utc = (
        "BEGIN:VEVENT\nUID:2\nDTSTART:20260810T101000Z\n"
        "DTEND:20260810T110000Z\nSUMMARY:Call\nEND:VEVENT\n"
    )
    events = _events_from(ics(utc), date(2026, 8, 10), SEOUL)
    assert events[0].start.strftime("%H:%M") == "19:10"


def test_an_all_day_event_has_no_time():
    all_day = (
        "BEGIN:VEVENT\nUID:3\nDTSTART;VALUE=DATE:20260810\n"
        "DTEND;VALUE=DATE:20260811\nSUMMARY:Holiday\nEND:VEVENT\n"
    )
    events = _events_from(ics(all_day), date(2026, 8, 10), SEOUL)
    assert events[0].all_day is True
    assert events[0].start is None


def test_an_untitled_event_still_renders():
    bare = "BEGIN:VEVENT\nUID:4\nDTSTART;VALUE=DATE:20260810\nEND:VEVENT\n"
    assert _events_from(ics(bare), date(2026, 8, 10), SEOUL)[0].summary == "(untitled)"


def test_other_days_are_excluded():
    assert _events_from(ics(TIMED), date(2026, 8, 11), SEOUL) == []


# -- recurrence ------------------------------------------------------------

WEEKLY = (
    "BEGIN:VEVENT\nUID:r1\n"
    "DTSTART;TZID=Asia/Seoul:20260804T191000\n"
    "DTEND;TZID=Asia/Seoul:20260804T200000\n"
    "RRULE:FREQ=WEEKLY;BYDAY=TU,TH\n"
    "SUMMARY:Ballet\nEND:VEVENT\n"
)


def test_a_weekly_rule_is_expanded_onto_later_days():
    """The rule is stored once; the occurrence three weeks out has to be
    computed, not looked up."""
    assert _events_from(ics(WEEKLY), date(2026, 8, 25), SEOUL)[0].summary == "Ballet"


def test_a_recurring_event_does_not_appear_on_other_weekdays():
    assert _events_from(ics(WEEKLY), date(2026, 8, 26), SEOUL) == []


def test_a_cancelled_occurrence_disappears():
    """EXDATE removes one date from the series — the case a hand-rolled
    expander gets wrong."""
    cancelled = WEEKLY.replace(
        "SUMMARY:Ballet", "EXDATE;TZID=Asia/Seoul:20260811T191000\nSUMMARY:Ballet"
    )
    assert _events_from(ics(cancelled), date(2026, 8, 11), SEOUL) == []
    assert _events_from(ics(cancelled), date(2026, 8, 13), SEOUL) != []


# -- ordering --------------------------------------------------------------


def test_all_day_events_sort_ahead_of_timed_ones():
    timed = Event("Ballet", datetime(2026, 8, 10, 19, 10, tzinfo=SEOUL), all_day=False)
    morning = Event("Call", datetime(2026, 8, 10, 9, 30, tzinfo=SEOUL), all_day=False)
    whole_day = Event("Holiday", None, all_day=True)
    ordered = sorted([timed, morning, whole_day], key=Event.sort_key)
    assert [e.summary for e in ordered] == ["Holiday", "Call", "Ballet"]
