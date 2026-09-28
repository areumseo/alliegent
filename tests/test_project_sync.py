"""Keeping the Projects database current without anyone keeping it so.

Every night the bot writes each open project's Last Active, Next Action,
seven-day count, open count and progress, from the agenda rows linked to it
and from GitHub. Titles are invented.
"""

from __future__ import annotations

from datetime import date

from alliegent import project_log
from alliegent.agenda import AgendaService, Project, ProjectService
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
PROJ_DS = "ds_proj-db"
TODAY = date(2026, 9, 28)

# Canceled sits in the Complete group beside Done, as it does in the real
# agenda -- which is the case the canceled rules exist for.
AGENDA_SCHEMA = {
    "Name": {"type": "title"},
    "Date": {"type": "date"},
    "Project": {"type": "relation"},
    "Status": {
        "type": "status",
        "status": {
            "options": [
                {"id": "o1", "name": "Not started"},
                {"id": "o2", "name": "In progress"},
                {"id": "o3", "name": "Canceled"},
                {"id": "o4", "name": "Done"},
            ],
            "groups": [
                {"id": "g1", "name": "To-do", "option_ids": ["o1"]},
                {"id": "g2", "name": "In progress", "option_ids": ["o2"]},
                {"id": "g3", "name": "Complete", "option_ids": ["o3", "o4"]},
            ],
        },
    },
}


def project_page(pid, title, *, last=None, next_ids=(), done_week=None, open_=None,
                 progress=None):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": title, "type": "text"}]},
        "Status": {"type": "status", "status": {"name": "In progress"}},
        "Last Active": {"type": "date", "date": {"start": last} if last else None},
        "Next Action": {"type": "relation", "relation": [{"id": i} for i in next_ids]},
        "Done (7d)": {"type": "number", "number": done_week},
        "Open": {"type": "number", "number": open_},
        "Progress": {"type": "number", "number": progress},
    }
    return {"object": "page", "id": pid, "url": "", "properties": props}


def services(project_pages, agenda_pages=()):
    config = Config()
    config.agenda.props.project = "Project"
    props = config.projects.props
    props.last_activity = "Last Active"
    props.next_item = "Next Action"
    props.done_week = "Done (7d)"
    props.open_count = "Open"
    props.progress = "Progress"
    client = FakeNotionClient(
        {DS: list(agenda_pages), PROJ_DS: project_pages}, schema=AGENDA_SCHEMA
    )
    return (
        client,
        AgendaService(client, config, "agenda-db"),
        ProjectService(client, config, "proj-db"),
    )


def task(pid, title, day, status="Not started", project="p1"):
    return make_page(pid, title, day=day, status=status, projects=[project])


# -- what is derived --------------------------------------------------------


async def test_the_week_counts_what_was_finished_in_the_last_seven_days():
    _, agenda, projects = services(
        [project_page("p1", "App")],
        [
            task("a1", "this week", "2026-09-25", "Done"),
            task("a2", "today", "2026-09-28", "Done"),
            task("a3", "eight days ago", "2026-09-20", "Done"),
            task("a4", "not done", "2026-09-27"),
        ],
    )
    [project] = await projects.active(TODAY, agenda)
    assert project.done_week == 2


async def test_progress_leaves_canceled_out_of_both_sides():
    """As every other rate the bot shows does."""
    _, agenda, projects = services(
        [project_page("p1", "App")],
        [
            task("a1", "did", "2026-09-25", "Done"),
            task("a2", "called off", "2026-09-26", "Canceled"),
            task("a3", "open", "2026-09-29"),
        ],
    )
    [project] = await projects.active(TODAY, agenda)
    assert project.progress == 0.5
    assert project.open_count == 1


async def test_a_project_with_nothing_linked_has_no_progress():
    _, agenda, projects = services([project_page("p1", "App")])
    [project] = await projects.active(TODAY, agenda)
    assert project.progress is None


async def test_a_later_recorded_last_active_is_kept():
    """The sync writes the days GitHub saw work, which the agenda does not
    know about; reading it back keeps a project busy in code from looking
    stalled."""
    _, agenda, projects = services(
        [project_page("p1", "App", last="2026-09-27")],
        [task("a1", "older", "2026-09-10", "Done")],
    )
    [project] = await projects.active(TODAY, agenda)
    assert project.last_activity == date(2026, 9, 27)


async def test_an_older_recorded_last_active_gives_way():
    """The hand-typed date that sat at 8/13 while the project moved."""
    _, agenda, projects = services(
        [project_page("p1", "App", last="2026-08-13")],
        [task("a1", "recent", "2026-09-24", "Done")],
    )
    [project] = await projects.active(TODAY, agenda)
    assert project.last_activity == date(2026, 9, 24)


# -- what is written --------------------------------------------------------


async def test_the_sync_writes_every_derived_column():
    client, agenda, projects = services(
        [project_page("p1", "App")],
        [
            task("a1", "did", "2026-09-25", "Done"),
            task("a2", "next", "2026-09-29"),
        ],
    )
    assert await projects.sync(TODAY, agenda) == 1
    [(page_id, props)] = client.updated
    assert page_id == "p1"
    assert props["Last Active"]["date"]["start"] == "2026-09-25"
    assert props["Next Action"] == {"relation": [{"id": "a2"}]}
    assert props["Done (7d)"] == {"number": 1}
    assert props["Open"] == {"number": 1}
    assert props["Progress"] == {"number": 0.5}


async def test_a_row_already_current_is_not_written():
    """Only what differs, so a quiet night writes nothing."""
    client, agenda, projects = services(
        [project_page("p1", "App", last="2026-09-25", next_ids=("a2",), done_week=1,
                      open_=1, progress=0.5)],
        [task("a1", "did", "2026-09-25", "Done"), task("a2", "next", "2026-09-29")],
    )
    assert await projects.sync(TODAY, agenda) == 0
    assert client.updated == []


async def test_a_day_of_code_moves_last_active_forward():
    client, agenda, projects = services(
        [project_page("p1", "App")], [task("a1", "did", "2026-09-20", "Done")]
    )
    await projects.sync(TODAY, agenda, {"p1": TODAY})
    assert client.updated[0][1]["Last Active"]["date"]["start"] == "2026-09-28"


async def test_next_action_is_cleared_when_nothing_is_left():
    client, agenda, projects = services(
        [project_page("p1", "App", next_ids=("gone",))],
        [task("a1", "did", "2026-09-25", "Done")],
    )
    await projects.sync(TODAY, agenda)
    assert client.updated[0][1]["Next Action"] == {"relation": []}


async def test_an_unconfigured_column_is_left_alone():
    client, agenda, projects = services(
        [project_page("p1", "App")], [task("a1", "did", "2026-09-25", "Done")]
    )
    projects.props.progress = ""
    await projects.sync(TODAY, agenda)
    assert "Progress" not in client.updated[0][1]


# -- the weekly table --------------------------------------------------------


def project(title, *, last=None, done=0, next_=""):
    return Project(id=title, title=title, status="In progress", next_action=next_,
                   url="", last_activity=last, done_week=done)


def test_the_week_lists_every_project_with_work_from_both_sources():
    text = project_log.week_message(
        [project("App", last=date(2026, 9, 27), done=2, next_="Ship it")],
        {"App": 3},
        TODAY,
        stale_after_days=7,
    )
    assert "Projects — 9/22–9/28" in text
    row = next(line for line in text.splitlines() if line.startswith("App"))
    assert row.split()[:4] == ["App", "5", "9/27", "Ship"]


def test_a_stalled_project_is_flagged_under_the_table():
    text = project_log.week_message(
        [project("Quiet", last=date(2026, 9, 1)), project("Busy", last=TODAY)],
        {},
        TODAY,
        stale_after_days=7,
    )
    assert "⚠️ Quiet: nothing since 9/1" in text
    assert "⚠️ Busy" not in text


def test_a_project_with_nothing_linked_says_so():
    text = project_log.week_message([project("New")], {}, TODAY, stale_after_days=7)
    assert "⚠️ New: nothing linked to it yet" in text


def test_no_open_projects_no_message():
    assert project_log.week_message([], {}, TODAY, stale_after_days=7) is None
