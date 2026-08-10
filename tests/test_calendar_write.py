"""Creating calendar events from chat.

Writing to a real calendar is the one place the bot touches something it
cannot undo, so the guards around *which* calendar and *whether* to write at
all matter more than the happy path.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from alliegent.agenda import AgendaService
from alliegent.chat import TOOLS, ChatAgent
from alliegent.config import Config, Secrets

from .conftest import FakeNotionClient

SEOUL = ZoneInfo("Asia/Seoul")
TODAY = date(2026, 8, 10)


def agent(secrets: Secrets | None = None) -> ChatAgent:
    client = FakeNotionClient()
    config = Config()
    a = ChatAgent("sk-test", AgendaService(client, config, "db"), config, secrets)
    a._today = lambda: TODAY
    return a


# -- configuration guards --------------------------------------------------


async def test_no_credentials_means_no_writing():
    out = await agent(Secrets())._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "15:00"},
    )
    assert "ICLOUD_USERNAME" in out


async def test_credentials_without_a_target_calendar_refuse_to_guess():
    """Nine calendars exist; writing into whichever came back first is not a
    guess worth making on someone's real calendar."""
    out = await agent(
        Secrets(icloud_username="me@example.com", icloud_app_password="abcd")
    )._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "15:00"},
    )
    assert "ICLOUD_WRITE_CALENDAR" in out


async def test_chat_without_secrets_says_so_rather_than_crashing():
    out = await agent(None)._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "15:00"},
    )
    assert "isn't configured" in out


# -- time handling ---------------------------------------------------------


def configured() -> Secrets:
    return Secrets(
        icloud_username="me@example.com",
        icloud_app_password="abcd",
        icloud_write_calendar="Calendar",
    )


@pytest.fixture
def captured(monkeypatch):
    """Intercept the write so the times can be asserted without a network."""
    from alliegent.integrations import calendar as cal

    seen: dict = {}

    async def fake_create(secrets, summary, start, end, url=cal.CALDAV_URL):
        seen.update(summary=summary, start=start, end=end)
        return secrets.icloud_write_calendar

    monkeypatch.setattr(cal, "create_event", fake_create)
    return seen


async def test_an_event_defaults_to_one_hour(captured):
    await agent(configured())._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "15:00"},
    )
    assert captured["start"] == datetime(2026, 8, 11, 15, 0, tzinfo=SEOUL)
    assert captured["end"] == datetime(2026, 8, 11, 16, 0, tzinfo=SEOUL)


async def test_an_explicit_end_time_is_used(captured):
    await agent(configured())._run_tool(
        "add_calendar_event",
        {
            "summary": "Workshop",
            "day": "2026-08-11",
            "start_time": "09:00",
            "end_time": "17:30",
        },
    )
    assert captured["end"] == datetime(2026, 8, 11, 17, 30, tzinfo=SEOUL)


async def test_an_end_before_the_start_crosses_midnight(captured):
    """22:00–01:00 is the only sane reading; a negative-length event is not."""
    await agent(configured())._run_tool(
        "add_calendar_event",
        {
            "summary": "Flight",
            "day": "2026-08-11",
            "start_time": "22:00",
            "end_time": "01:00",
        },
    )
    assert captured["end"] == datetime(2026, 8, 12, 1, 0, tzinfo=SEOUL)


async def test_events_are_created_in_the_local_timezone(captured):
    await agent(configured())._run_tool(
        "add_calendar_event",
        {"summary": "Call", "day": "2026-08-11", "start_time": "09:30"},
    )
    assert captured["start"].tzinfo is SEOUL


async def test_the_reply_names_the_calendar_it_landed_in(captured):
    out = await agent(configured())._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "15:00"},
    )
    assert "Calendar" in out and "Dentist" in out


async def test_a_bad_time_comes_back_as_text(captured):
    out = await agent(configured())._run_tool(
        "add_calendar_event",
        {"summary": "Dentist", "day": "2026-08-11", "start_time": "3pm"},
    )
    assert "failed" in out.lower()


# -- tool surface ----------------------------------------------------------


def test_the_calendar_can_be_written_but_not_edited():
    """Creating is recoverable by deleting in the Calendar app; editing or
    removing someone's existing event on a misread request is not."""
    names = {t["name"] for t in TOOLS}
    assert "add_calendar_event" in names
    assert not any("calendar" in n and n != "add_calendar_event" for n in names)
