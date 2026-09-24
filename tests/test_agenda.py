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
    """Config() has no project relation by default, so an agenda without a
    projects database keeps working. Tests that exercise linking opt in."""
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
    assert items[0].title == "(untitled)"


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
            make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True,
                      category="Exercise"),
            make_page("t2", "Spanish class 9:30PM", day="2026-08-05", recurring=True),
        ]
    )
    planned = await svc.scaffold_week(WEEK_START)
    assert planned == [
        ("Dance class 7:10PM", date(2026, 8, 10), "Exercise"),
        ("Spanish class 9:30PM", date(2026, 8, 12), None),
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
            make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True),
            make_page("e1", "Dance class 7:10PM", day="2026-08-10", recurring=True),
        ]
    )
    assert await svc.scaffold_week(WEEK_START) == []
    assert client.created == []


async def test_plan_week_previews_without_writing():
    client, svc = service(
        [make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True)]
    )
    planned = await svc.plan_week(WEEK_START)
    assert [t for t, _, _ in planned] == ["Dance class 7:10PM"]
    assert client.created == []


async def test_scaffolded_rows_stay_recurring_so_the_next_week_works():
    client, svc = service(
        [make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True)]
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
    agenda, projects = project_services(
        [],
        [
            make_project("a", "진행 중", status="In progress"),
            make_project("b", "끝남", status="Done"),
        ],
    )
    assert [p.title for p in await projects.active(TODAY, agenda)] == ["진행 중"]


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


async def test_stale_reports_the_real_date_however_far_back_it_is():
    """The window decides what counts as stale, not how far back the lookup
    can see. Scanning a `days`-wide window made every long-neglected project
    report "never", which is the one case where the date is worth reading."""
    agenda, projects = project_services(
        [make_page("p1", "작업", day="2026-05-02", projects=["a"])],
        [make_project("a", "오래된 것")],
    )
    stale = await projects.stale(TODAY, agenda, days=7)
    assert stale[0][1] == date(2026, 5, 2)


async def test_stale_stays_silent_when_relation_is_not_configured():
    """Without a project relation there is no evidence of inactivity, so the
    job must not nag about every project — which is what an agenda with no
    projects database at all would otherwise get every Wednesday."""
    agenda, projects = project_services(
        [], [make_project("b", "프로젝트")], link_projects=False
    )
    assert await projects.stale(TODAY, agenda, days=7) == []


# -- derived activity ------------------------------------------------------
# What a project is waiting on and when it last moved are read off the linked
# agenda rows rather than out of columns someone has to keep current.


async def test_next_action_is_the_soonest_unfinished_linked_item():
    agenda, projects = project_services(
        [
            make_page("p1", "끝난 일", day="2026-08-06", status="Done", projects=["a"]),
            make_page("p2", "다음 일", day="2026-08-09", projects=["a"]),
            make_page("p3", "그 다음", day="2026-08-12", projects=["a"]),
        ],
        [make_project("a", "프로젝트")],
    )
    active = await projects.active(TODAY, agenda)
    assert active[0].next_action == "다음 일"


async def test_next_action_prefers_the_overdue_item():
    """Already late is the thing to do next, not the thing to skip past."""
    agenda, projects = project_services(
        [
            make_page("p1", "밀린 일", day="2026-08-01", projects=["a"]),
            make_page("p2", "내일 일", day="2026-08-09", projects=["a"]),
        ],
        [make_project("a", "프로젝트")],
    )
    active = await projects.active(TODAY, agenda)
    assert active[0].next_action == "밀린 일"


async def test_last_activity_ignores_work_scheduled_ahead():
    """A row dated next week is a plan, not evidence the project moved."""
    agenda, projects = project_services(
        [
            make_page("p1", "지난 일", day="2026-08-05", projects=["a"]),
            make_page("p2", "다음 주 일", day="2026-08-15", projects=["a"]),
        ],
        [make_project("a", "프로젝트")],
    )
    active = await projects.active(TODAY, agenda)
    assert active[0].last_activity == date(2026, 8, 5)


async def test_a_maintained_column_survives_when_nothing_is_linked():
    """Deriving replaces the columns; it does not delete someone's answer for
    a project the agenda has never heard of."""
    client = FakeNotionClient(
        {DS: [], PROJ_DS: [make_project("a", "프로젝트", next_action="직접 적은 것")]}
    )
    config = make_config(link_projects=True)
    config.projects.props.next_action = "Next action"
    agenda = AgendaService(client, config, DB)
    projects = ProjectService(client, config, PROJ_DB)
    active = await projects.active(TODAY, agenda)
    assert active[0].next_action == "직접 적은 것"


async def test_activity_ignores_another_projects_rows():
    agenda, projects = project_services(
        [make_page("p1", "남의 일", day="2026-08-07", projects=["b"])],
        [make_project("a", "내 프로젝트")],
    )
    active = await projects.active(TODAY, agenda)
    assert active[0].last_activity is None and active[0].next_action == ""


# -- naming a project from Discord -----------------------------------------


async def test_find_matches_a_name_exactly_and_by_substring():
    _, projects = project_services(
        [], [make_project("a", "사이드 프로젝트"), make_project("b", "블로그")]
    )
    assert (await projects.find("블로그")).id == "b"
    assert (await projects.find("사이드")).id == "a"


async def test_find_refuses_an_ambiguous_name():
    """Linking to the wrong project leaves no trace in the confirmation, so a
    guess here would be wrong quietly."""
    _, projects = project_services(
        [], [make_project("a", "블로그 글"), make_project("b", "블로그 개편")]
    )
    with pytest.raises(ValueError, match="more than one"):
        await projects.find("블로그")


async def test_find_refuses_an_unknown_name_and_names_the_options():
    _, projects = project_services([], [make_project("a", "블로그")])
    with pytest.raises(ValueError, match="블로그"):
        await projects.find("없는 것")


async def test_set_project_links_and_clears():
    client, svc = service([], link_projects=True)
    await svc.set_project("p1", "proj-1")
    await svc.set_project("p1", None)
    assert client.updated == [
        ("p1", {"Project": {"relation": [{"id": "proj-1"}]}}),
        ("p1", {"Project": {"relation": []}}),
    ]


async def test_set_project_refuses_when_no_relation_is_configured():
    _, svc = service([], link_projects=False)
    with pytest.raises(ValueError, match="relation"):
        await svc.set_project("p1", "proj-1")


async def test_items_for_project_is_empty_without_a_relation():
    _, svc = service(
        [make_page("p1", "작업", day="2026-08-07", projects=["a"])], link_projects=False
    )
    assert await svc.items_for_project("a") == []
