"""The optional Priority column: read, written, offered, shown and carried."""

from __future__ import annotations

from datetime import date, time

import pytest

from alliegent import reports
from alliegent.agenda import AgendaItem, AgendaService
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
TODAY = date(2026, 8, 8)


def service(pages, **kwargs):
    client = FakeNotionClient({DS: pages}, **kwargs)
    return client, AgendaService(client, Config(), "agenda-db")


def prioritised_client(options):
    return FakeNotionClient(
        {DS: []},
        schema={
            "Name": {"type": "title"},
            "Date": {"type": "date"},
            "Status": {"type": "status"},
            "Priority": {
                "type": "select",
                "select": {"options": [{"name": name} for name in options]},
            },
        },
    )


# -- the agenda ------------------------------------------------------------


async def test_an_item_reads_its_priority():
    _, svc = service([make_page("p1", "Report", day="2026-08-08", priority="High")])
    assert (await svc.items_on(TODAY))[0].priority == "High"


async def test_an_item_without_one_reads_as_none():
    _, svc = service([make_page("p1", "Report", day="2026-08-08")])
    assert (await svc.items_on(TODAY))[0].priority is None


async def test_priority_never_reorders_the_day():
    """The numbers /done and /delete take are positions in the day; ranking an
    item must not move it and send a typed number to a different row."""
    _, svc = service(
        [
            make_page("p1", "Low first", day="2026-08-08", priority="Low"),
            make_page("p2", "High second", day="2026-08-08", priority="High"),
        ]
    )
    assert [i.title for i in await svc.items_on(TODAY)] == ["Low first", "High second"]


async def test_add_writes_the_priority_only_when_given():
    client, svc = service([])
    await svc.add_item("Report", TODAY, priority="High")
    await svc.add_item("Read", TODAY)
    assert client.created[0][1]["Priority"] == {"select": {"name": "High"}}
    assert "Priority" not in client.created[1][1]


async def test_set_priority_sets_and_clears():
    client, svc = service([])
    await svc.set_priority("p1", "Medium")
    await svc.set_priority("p1", None)
    assert client.updated == [
        ("p1", {"Priority": {"select": {"name": "Medium"}}}),
        ("p1", {"Priority": {"select": None}}),
    ]


async def test_no_priority_property_turns_it_off():
    config = Config()
    config.agenda.props.priority = ""
    client = prioritised_client(["High"])
    svc = AgendaService(client, config, "agenda-db")
    assert await svc.priority_names() == []
    with pytest.raises(ValueError):
        await svc.set_priority("p1", "High")


async def test_scaffolded_rows_keep_last_weeks_priority():
    client, svc = service(
        [make_page("t1", "Dance class", day="2026-08-03", recurring=True, priority="High")]
    )
    planned = await svc.scaffold_week(date(2026, 8, 10))
    assert planned == [("Dance class", date(2026, 8, 10), None)]
    assert client.created[0][1]["Priority"] == {"select": {"name": "High"}}


# -- choosing one ----------------------------------------------------------


async def test_the_offered_priorities_come_from_notion():
    svc = AgendaService(prioritised_client(["High", "Medium", "Low"]), Config(), "agenda-db")
    assert await svc.priority_names() == ["High", "Medium", "Low"]


async def test_a_typed_priority_need_not_match_case():
    from alliegent.integrations.discord_bot import resolve_priority

    class Bot:
        agenda = AgendaService(prioritised_client(["High"]), Config(), "agenda-db")

    assert await resolve_priority(Bot(), "high") == "High"


async def test_an_unknown_priority_is_refused_rather_than_created():
    from alliegent.integrations.discord_bot import resolve_priority

    class Bot:
        agenda = AgendaService(prioritised_client(["High"]), Config(), "agenda-db")

    with pytest.raises(ValueError, match="Hgih"):
        await resolve_priority(Bot(), "Hgih")


# -- the tables ------------------------------------------------------------


def task(title, *, priority=None, at=None, done=False):
    return AgendaItem(
        id=title, title=title, day=TODAY, status=None, done=done, url="",
        at=at, priority=priority,
    )


def table(text: str) -> list[str]:
    return text.split("```")[1].strip().splitlines()


def test_a_day_with_priorities_shows_the_column():
    text = reports.today_list(
        TODAY, [task("Report", priority="High", at=time(9, 0)), task("Read")]
    )
    header, _, first, second = table(text)
    assert header.split() == ["#", "Time", "P", "Task", "Category"]
    assert first.split() == ["1", "09:00", "H", "Report"]
    assert second.split() == ["2", "-", "Read"]


def test_a_day_without_any_leaves_the_column_out():
    """Most days may carry none, and an empty column still costs width."""
    header = table(reports.today_list(TODAY, [task("Read")]))[0]
    assert "P" not in header.split()


def test_a_short_priority_name_is_shown_whole():
    text = reports.today_list(TODAY, [task("Report", priority="P1")])
    assert "P1" in table(text)[2].split()


def test_the_done_table_keeps_the_priority():
    lines = reports.done_table([task("Report", priority="Low", done=True)])
    assert lines[1].split() == ["#", "Time", "P", "Task", "Category"]
    assert lines[3].split() == ["1", "-", "L", "Report"]


def test_the_weekly_review_shows_priorities():
    text = reports.weekly_review(TODAY, TODAY, [task("Report", priority="Medium")])
    assert "P" in table(text)[0].split()
    assert "M" in table(text)[2].split()
