"""What counts as finished.

An item can be closed without being "Done" -- cancelling one is a decision,
not an omission -- and Notion says so in the schema: its status options are
grouped, and the Complete group holds every status that means finished. Read
that, and a status added or renamed in Notion needs no change here.
"""

from __future__ import annotations

from datetime import date

import pytest

from alliegent.agenda import AgendaService
from alliegent.config import Config
from alliegent.integrations.notion import closed_statuses

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
DAY = date(2026, 8, 18)


def status_schema(*, done_name: str = "Done", closed_name: str = "Cancelled") -> dict:
    """A Notion status property, shaped the way the API returns one."""
    return {
        "Name": {"type": "title"},
        "Date": {"type": "date"},
        "Status": {
            "type": "status",
            "status": {
                "options": [
                    {"id": "o1", "name": "Not started"},
                    {"id": "o2", "name": "In progress"},
                    {"id": "o5", "name": "On hold"},
                    {"id": "o6", "name": "Ready"},
                    {"id": "o3", "name": closed_name},
                    {"id": "o4", "name": done_name},
                ],
                "groups": [
                    {"id": "g1", "name": "To-do", "option_ids": ["o1", "o6"]},
                    {"id": "g2", "name": "In progress", "option_ids": ["o2", "o5"]},
                    {"id": "g3", "name": "Complete", "option_ids": ["o3", "o4"]},
                ],
            },
        },
    }


def service(pages=None, schema=None):
    client = FakeNotionClient({DS: pages or []}, schema=schema or status_schema())
    return client, AgendaService(client, Config(), "agenda-db")


def test_the_complete_group_is_read_from_the_schema():
    assert closed_statuses(status_schema()["Status"], "Done") == {"Done", "Cancelled"}


def test_a_renamed_status_needs_no_code_change():
    """The user is renaming Cancelled to Canceled. Nothing here spells it."""
    schema = status_schema(closed_name="Canceled")
    assert closed_statuses(schema["Status"], "Done") == {"Done", "Canceled"}


def test_the_group_is_found_by_its_contents_not_its_name():
    """Notion lets the groups be renamed, so the Complete group is identified
    as the one holding the configured done value."""
    schema = status_schema()
    schema["Status"]["status"]["groups"][2]["name"] = "Finished"
    assert closed_statuses(schema["Status"], "Done") == {"Done", "Cancelled"}


def test_a_select_property_falls_back_to_the_configured_value():
    """A plain select has no groups to read, so only the done value closes it."""
    assert closed_statuses({"type": "select"}, "Done") == {"Done"}


def test_a_status_outside_the_complete_group_stays_open():
    schema = status_schema()
    assert "In progress" not in closed_statuses(schema["Status"], "Done")


async def test_a_cancelled_item_is_not_pending():
    """The reported bug: an item cancelled in Notion kept appearing in the
    brief and in /status as outstanding work."""
    _, svc = service([make_page("p1", "called off", day=DAY.isoformat(), status="Cancelled")])
    assert (await svc.items_on(DAY))[0].done is True


async def test_a_cancelled_item_is_not_overdue():
    _, svc = service(
        [make_page("p1", "called off", day="2026-08-01", status="Cancelled")]
    )
    assert await svc.overdue(DAY) == []


async def test_an_unfinished_item_is_still_overdue():
    _, svc = service(
        [make_page("p1", "still open", day="2026-08-01", status="Not started")]
    )
    assert len(await svc.overdue(DAY)) == 1


@pytest.mark.parametrize("status", ["Done", "Cancelled"])
async def test_every_complete_status_closes_an_item(status):
    _, svc = service([make_page("p1", "x", day=DAY.isoformat(), status=status)])
    assert (await svc.items_on(DAY))[0].done is True


async def test_a_checkbox_database_is_unaffected():
    """Not every agenda models status as a status property."""
    schema = {"Status": {"type": "checkbox"}}
    _, svc = service(
        [make_page("p1", "x", day=DAY.isoformat(), checkbox=True)], schema=schema
    )
    assert (await svc.items_on(DAY))[0].done is True


# -- work already underway -------------------------------------------------


async def test_an_in_progress_item_is_marked_as_started():
    """Started work needs finishing, not beginning. Reading a list where it
    looks identical to untouched work is what prompted this."""
    _, svc = service(
        [make_page("p1", "half done", day=DAY.isoformat(), status="In progress")]
    )
    item = (await svc.items_on(DAY))[0]
    assert item.started is True
    assert item.done is False


async def test_the_whole_in_progress_group_counts_as_started():
    """On hold sits in that group in Notion, so it is underway, not untouched."""
    _, svc = service([make_page("p1", "paused", day=DAY.isoformat(), status="On hold")])
    assert (await svc.items_on(DAY))[0].started is True


async def test_a_to_do_status_is_not_started():
    for status in ("Not started", "Ready"):
        _, svc = service([make_page("p1", "x", day=DAY.isoformat(), status=status)])
        assert (await svc.items_on(DAY))[0].started is False, status


async def test_a_finished_item_is_not_also_started():
    """Complete wins: an item cannot want both finishing and beginning."""
    _, svc = service([make_page("p1", "x", day=DAY.isoformat(), status="Done")])
    item = (await svc.items_on(DAY))[0]
    assert item.done is True and item.started is False


def test_the_in_progress_group_is_read_from_the_schema():
    from alliegent.integrations.notion import group_statuses

    assert group_statuses(status_schema()["Status"], "In progress") == {
        "In progress",
        "On hold",
    }
