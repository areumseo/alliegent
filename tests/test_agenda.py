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


def service(pages, **kwargs):
    client = FakeNotionClient({DS: pages}, **kwargs)
    from alliegent.config import Config

    return client, AgendaService(client, Config(), DB)


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
    # The fake client ignores filters, so both rows come back and the
    # done-filtering in `overdue` is what's under test here.
    _, svc = service(
        [
            make_page("p1", "밀린 것", day="2026-08-01", status="Not started"),
            make_page("p2", "끝낸 것", day="2026-08-02", status="Done"),
        ]
    )
    items = await svc.overdue(TODAY)
    assert [i.title for i in items] == ["밀린 것"]


async def test_ensure_days_only_creates_missing_dates():
    client, svc = service([make_page("p1", "이미 있음", day="2026-08-09")])
    created = await svc.ensure_days(TODAY, 3, "{date}")
    assert created == [date(2026, 8, 8), date(2026, 8, 10)]
    assert len(client.created) == 2


async def test_ensure_days_creates_nothing_when_week_is_full():
    days = [make_page(f"p{i}", "x", day=f"2026-08-0{8 + i}") for i in range(2)]
    client, svc = service(days)
    assert await svc.ensure_days(TODAY, 2, "{date}") == []
    assert client.created == []


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
    client, svc = service([])
    await svc.add_item("할 일", TODAY, project_id="proj-1")
    _, props = client.created[0]
    assert props["Project"] == {"relation": [{"id": "proj-1"}]}


# -- projects --------------------------------------------------------------


def project_services(agenda_pages, project_pages):
    from alliegent.config import Config

    client = FakeNotionClient({DS: agenda_pages, PROJ_DS: project_pages})
    config = Config()
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
    job must not nag about every project."""
    from alliegent.config import Config

    config = Config()
    config.agenda.props.project = ""
    client = FakeNotionClient({DS: [], PROJ_DS: [make_project("b", "프로젝트")]})
    agenda = AgendaService(client, config, DB)
    projects = ProjectService(client, config, PROJ_DB)
    assert await projects.stale(TODAY, agenda, days=7) == []
