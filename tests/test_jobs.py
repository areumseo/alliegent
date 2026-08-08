from __future__ import annotations

from datetime import date

from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config
from alliegent.jobs import Jobs
from alliegent.scheduler import build_scheduler

from .conftest import FakeNotionClient, make_page, make_project

DS = "ds_agenda-db"
PROJ_DS = "ds_proj-db"


def build(agenda_pages=None, project_pages=None):
    sent: list[str] = []

    async def notify(message: str) -> None:
        sent.append(message)

    client = FakeNotionClient({DS: agenda_pages or [], PROJ_DS: project_pages or []})
    config = Config()
    jobs = Jobs(
        AgendaService(client, config, "agenda-db"),
        ProjectService(client, config, "proj-db"),
        config,
        notify,
    )
    return jobs, sent, client


async def test_daily_brief_is_sent_even_on_an_empty_day():
    """A morning brief with nothing in it is still useful — unlike the evening
    nag, it should always arrive."""
    jobs, sent, _ = build()
    await jobs.run_daily_brief()
    assert len(sent) == 1


async def test_incomplete_alert_sends_nothing_when_all_clear():
    jobs, sent, _ = build()
    await jobs.run_incomplete_alert()
    assert sent == []


async def test_scaffold_preview_does_not_write_to_notion():
    jobs, _, client = build()
    message = await jobs.week_scaffold(commit=False)
    assert client.created == []
    assert message is not None


async def test_scaffold_commit_writes_rows():
    jobs, _, client = build()
    await jobs.week_scaffold(commit=True)
    assert len(client.created) == Config().agenda.scaffold_days


async def test_weekly_review_covers_the_trailing_seven_days():
    jobs, sent, _ = build([make_page("p1", "한 일", day="2026-08-05", status="Done")])
    await jobs.run_weekly_review()
    assert "한 일" in sent[0]


async def test_stale_project_job_reports_neglected_projects():
    jobs, sent, _ = build(
        agenda_pages=[], project_pages=[make_project("b", "방치된 프로젝트")]
    )
    await jobs.run_stale_projects()
    assert sent and "방치된 프로젝트" in sent[0]


def test_scheduler_registers_every_job():
    jobs, _, _ = build()
    config = Config()
    scheduler = build_scheduler(jobs, config)
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {
        "daily_brief",
        "incomplete_alert",
        "week_scaffold",
        "stale_projects",
        "weekly_review",
    }


def test_scheduler_uses_configured_timezone():
    jobs, _, _ = build()
    config = Config()
    scheduler = build_scheduler(jobs, config)
    assert str(scheduler.timezone) == "Asia/Seoul"


def test_jobs_today_respects_configured_timezone():
    jobs, _, _ = build()
    assert isinstance(jobs.today(), date)
