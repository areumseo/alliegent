from __future__ import annotations

from datetime import date

import pytest

from alliegent.agenda import AgendaService, ProjectService

from .conftest import FakeNotionClient, make_page, make_project

DB = "agenda-db"
DS = "ds_agenda-db"
PROJ_DB = "proj-db"
PROJ_DS = "ds_proj-db"
TODAY = date(2026, 8, 8)


def make_config(link_projects: bool = False):
    """Config() has no project relation by default — the real workspace has no
    projects database yet. Tests that exercise linking opt in explicitly."""
    from alliegent.config import Config

    config = Config()
    config.agenda.props.project = "Project" if link_projects else ""
    return config


def service(pages, link_projects=False, **kwargs):
    client = FakeNotionClient({DS: pages}, **kwargs)
    return client, AgendaService(client, make_config(link_projects), DB)


async def test_items_on_reads_titles_and_status():
    _, svc = service(
        [
            make_page("p1", "보고서 초안", day="2026-08-08", status="Done"),
            make_page("p2", "회의 준비", day="2026-08-08", status="Not started"),
        ]
    )
    items = await svc.items_on(TODAY)
    assert [i.title for i in items] == ["보고서 초안", "회의 준비"]
    assert [i.done for i in items] == [True, False]


async def test_untitled_rows_get_a_placeholder_not_an_empty_string():
    _, svc = service([make_page("p1", "", day="2026-08-08")])
    items = await svc.items_on(TODAY)
    assert items[0].title == "(제목 없음)"


async def test_overdue_excludes_completed_items():
    _, svc = service(
        [
            make_page("p1", "밀린 것", day="2026-08-01", status="Not started"),
            make_page("p2", "끝낸 것", day="2026-08-02", status="Done"),
        ]
    )
    items = await svc.overdue(TODAY)
    assert [i.title for i in items] == ["밀린 것"]


# -- week scaffolding ------------------------------------------------------
# Monday 2026-08-10 is the week being filled; 2026-08-03 is the template week.

WEEK_START = date(2026, 8, 10)


async def test_scaffold_copies_recurring_items_onto_the_same_weekday():
    client, svc = service(
        [
            # Template week: Monday and Wednesday.
            make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True,
                      category="Exercise"),
            make_page("t2", "BC English 9:30PM", day="2026-08-05", recurring=True),
        ]
    )
    planned = await svc.scaffold_week(WEEK_START)
    assert planned == [
        ("Ballet 7:10PM", date(2026, 8, 10), "Exercise"),
        ("BC English 9:30PM", date(2026, 8, 12), None),
    ]
    assert len(client.created) == 2


async def test_scaffold_ignores_one_off_items():
    """Only rows flagged Recurring are templates; a one-off stays in its week."""
    client, svc = service(
        [make_page("t1", "가짜연구소 지원", day="2026-08-03", recurring=False)]
    )
    assert await svc.scaffold_week(WEEK_START) == []
    assert client.created == []


async def test_scaffold_is_idempotent():
    """Re-running the job must not duplicate rows already in the target week."""
    client, svc = service(
        [
            make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True),
            make_page("e1", "Ballet 7:10PM", day="2026-08-10", recurring=True),
        ]
    )
    assert await svc.scaffold_week(WEEK_START) == []
    assert client.created == []


async def test_plan_week_previews_without_writing():
    client, svc = service(
        [make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True)]
    )
    planned = await svc.plan_week(WEEK_START)
    assert [t for t, _, _ in planned] == ["Ballet 7:10PM"]
    assert client.created == []


async def test_scaffolded_rows_stay_recurring_so_the_next_week_works():
    client, svc = service(
        [make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True)]
    )
    await svc.scaffold_week(WEEK_START)
    _, props = client.created[0]
    assert props["Recurring"] == {"checkbox": True}


async def test_set_done_uses_the_detected_property_type():
    client, svc = service([], schema={"Status": {"type": "checkbox"}})
    await svc.set_done("p1")
    page_id, props = client.updated[0]
    assert page_id == "p1"
    assert props["Status"] == {"checkbox": True}


async def test_set_done_uses_status_name_for_status_property():
    client, svc = service([], schema={"Status": {"type": "status"}})
    await svc.set_done("p1")
    assert client.updated[0][1]["Status"] == {"status": {"name": "Done"}}


async def test_missing_status_property_gives_an_actionable_error():
    _, svc = service([], schema={"Name": {"type": "title"}})
    with pytest.raises(RuntimeError, match="inspect_notion"):
        await svc.status_type()


async def test_add_item_links_project_when_relation_configured():
    client, svc = service([], link_projects=True)
    await svc.add_item("할 일", TODAY, project_id="proj-1")
    _, props = client.created[0]
    assert props["Project"] == {"relation": [{"id": "proj-1"}]}


# -- projects --------------------------------------------------------------


def project_services(agenda_pages, project_pages, link_projects=True):
    client = FakeNotionClient({DS: agenda_pages, PROJ_DS: project_pages})
    config = make_config(link_projects)
    return (
        AgendaService(client, config, DB),
        ProjectService(client, config, PROJ_DB),
    )


async def test_active_excludes_done_projects():
    _, projects = project_services(
        [],
        [
            make_project("a", "진행 중", status="In progress"),
            make_project("b", "끝남", status="Done"),
        ],
    )
    assert [p.title for p in await projects.active()] == ["진행 중"]


async def test_stale_flags_projects_without_recent_agenda_activity():
    agenda, projects = project_services(
        [make_page("p1", "작업", day="2026-08-07", projects=["a"])],
        [make_project("a", "활발한 것"), make_project("b", "방치된 것")],
    )
    stale = await projects.stale(TODAY, agenda, days=7)
    assert [p.title for p, _ in stale] == ["방치된 것"]


async def test_stale_reports_no_activity_as_none_date():
    agenda, projects = project_services([], [make_project("b", "방치된 것")])
    stale = await projects.stale(TODAY, agenda, days=7)
    assert stale[0][1] is None


async def test_stale_stays_silent_when_relation_is_not_configured():
    """Without a project relation there is no evidence of inactivity, so the
    job must not nag about every project. This is the shipped default today,
    since the workspace has no projects database yet."""
    agenda, projects = project_services(
        [], [make_project("b", "프로젝트")], link_projects=False
    )
    assert await projects.stale(TODAY, agenda, days=7) == []
