from __future__ import annotations

from datetime import date

from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config
from alliegent.jobs import Jobs
from alliegent.scheduler import build_scheduler

from .conftest import FakeNotionClient, make_page, make_project

DS = "ds_agenda-db"
PROJ_DS = "ds_proj-db"


# Sunday. Pinned so the tests don't drift with the real calendar.
TODAY = date(2026, 8, 9)


def build(agenda_pages=None, project_pages=None, today=TODAY, link_projects=True):
    sent: list[tuple[str, str]] = []

    async def notify(message: str, kind: str) -> None:
        sent.append((message, kind))

    client = FakeNotionClient({DS: agenda_pages or [], PROJ_DS: project_pages or []})
    config = Config()
    # Staleness needs the agenda->project relation as evidence; the shipped
    # default has none, so tests that exercise it opt in.
    config.agenda.props.project = "Project" if link_projects else ""
    jobs = Jobs(
        AgendaService(client, config, "agenda-db"),
        ProjectService(client, config, "proj-db"),
        config,
        notify,
        clock=lambda: today,
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
    jobs, _, client = build(
        [make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True)]
    )
    message = await jobs.week_scaffold(commit=False)
    assert client.created == []
    assert message is not None and "Ballet 7:10PM" in message


async def test_scaffold_commit_writes_rows():
    jobs, _, client = build(
        [make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True)]
    )
    await jobs.week_scaffold(commit=True)
    assert len(client.created) == 1


async def test_weekly_planning_sends_even_when_next_week_is_empty():
    """An empty week is exactly when the planning nudge earns its place."""
    jobs, sent, _ = build()
    await jobs.run_weekly_planning()
    assert len(sent) == 1
    assert "아직 없습니다" in sent[0][0]


async def test_weekly_planning_reports_what_is_already_scheduled():
    jobs, sent, _ = build(
        [make_page("p1", "Ballet 7:10PM", day="2026-08-11", status="Not started")]
    )
    await jobs.run_weekly_planning()
    message = sent[0][0]
    assert "등록된 항목 1건" in message
    assert "8/10(월)" in message  # 8/11 has an item, so 8/10 is listed as empty


async def test_scaffold_says_nothing_when_the_week_is_already_set_up():
    jobs, sent, _ = build()
    await jobs.run_week_scaffold()
    assert sent == []


def test_coming_monday_is_today_when_today_is_monday():
    jobs, _, _ = build()
    assert jobs.coming_monday(date(2026, 8, 10)) == date(2026, 8, 10)


def test_coming_monday_skips_ahead_from_any_other_day():
    jobs, _, _ = build()
    assert jobs.coming_monday(date(2026, 8, 9)) == date(2026, 8, 10)
    assert jobs.coming_monday(date(2026, 8, 11)) == date(2026, 8, 17)


async def test_weekly_review_covers_the_trailing_seven_days():
    jobs, sent, _ = build([make_page("p1", "한 일", day="2026-08-05", status="Done")])
    await jobs.run_weekly_review()
    assert "한 일" in sent[0][0]


async def test_stale_project_job_reports_neglected_projects():
    jobs, sent, _ = build(
        agenda_pages=[], project_pages=[make_project("b", "방치된 프로젝트")]
    )
    await jobs.run_stale_projects()
    assert sent and "방치된 프로젝트" in sent[0][0]


async def test_jobs_route_to_their_own_channel_kinds():
    """Agenda chatter and project nudges go to different channels, so the
    kind each job emits is part of its contract."""
    jobs, sent, _ = build(
        # A recurring template item, so the scaffolding job has something to
        # say rather than staying silent.
        agenda_pages=[make_page("t1", "Ballet 7:10PM", day="2026-08-03", recurring=True)],
        project_pages=[make_project("b", "방치됨")],
    )
    await jobs.run_daily_brief()
    await jobs.run_week_scaffold()
    await jobs.run_stale_projects()
    await jobs.run_weekly_review()
    assert [kind for _, kind in sent] == ["agenda", "agenda", "projects", "review"]


def test_scheduler_registers_the_enabled_jobs():
    """week_scaffold ships disabled — nothing in the agenda repeats weekly."""
    jobs, _, _ = build()
    scheduler = build_scheduler(jobs, Config())
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {
        "daily_brief",
        "incomplete_alert",
        "weekly_planning",
        "stale_projects",
        "weekly_review",
    }


def test_an_empty_time_disables_a_job():
    jobs, _, _ = build()
    config = Config()
    config.schedule.daily_brief = ""
    ids = {job.id for job in build_scheduler(jobs, config).get_jobs()}
    assert "daily_brief" not in ids


def test_setting_a_time_enables_week_scaffolding_again():
    jobs, _, _ = build()
    config = Config()
    config.schedule.week_scaffold_time = "06:00"
    ids = {job.id for job in build_scheduler(jobs, config).get_jobs()}
    assert "week_scaffold" in ids


def test_scheduler_uses_configured_timezone():
    jobs, _, _ = build()
    config = Config()
    scheduler = build_scheduler(jobs, config)
    assert str(scheduler.timezone) == "Asia/Seoul"


def test_jobs_today_uses_the_configured_timezone_by_default():
    """No clock injected — falls back to the real Asia/Seoul date."""
    jobs, _, _ = build()
    jobs._clock = None
    assert isinstance(jobs.today(), date)
