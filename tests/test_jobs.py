from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from alliegent import reports
from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config
from alliegent.integrations import news_feeds
from alliegent.jobs import Jobs
from alliegent.scheduler import build_scheduler

from .conftest import FakeNotionClient, make_page, make_project

DS = "ds_agenda-db"
PROJ_DS = "ds_proj-db"


# Sunday. Pinned so the tests don't drift with the real calendar.
TODAY = date(2026, 8, 9)


def build(
    agenda_pages=None,
    project_pages=None,
    today=TODAY,
    link_projects=True,
    anthropic_api_key="",
):
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
        anthropic_api_key=anthropic_api_key,
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
        [make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True)]
    )
    message = await jobs.week_scaffold(commit=False)
    assert client.created == []
    assert message is not None and "Dance class 7:10PM" in message


async def test_scaffold_commit_writes_rows():
    jobs, _, client = build(
        [make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True)]
    )
    await jobs.week_scaffold(commit=True)
    assert len(client.created) == 1


def _entry(title: str = "A thing shipped", url: str = "https://example.com/a"):
    return news_feeds.Entry(
        title=title,
        url=url,
        source="example.com",
        published=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
        summary="Details.",
    )


def _stub_feeds(monkeypatch, entries=None):
    """Keep the news tests off the network -- feeds are covered separately."""

    async def entries_for(day, tz, **kwargs):
        return entries if entries is not None else [_entry()]

    monkeypatch.setattr(news_feeds, "entries_for", entries_for)


async def test_ai_news_stays_silent_without_an_api_key():
    """No key means the feature is off, not that the channel gets an error."""
    jobs, sent, _ = build()
    await jobs.run_ai_news()
    assert sent == []


async def test_ai_news_stays_silent_when_the_digest_fails(monkeypatch):
    """A missing morning digest is a non-event; a stack trace in Discord isn't."""
    from alliegent.integrations import claude

    async def boom(*args, **kwargs):
        raise claude.NewsUnavailable("rate limited")

    monkeypatch.setattr(claude, "write_digest", boom)
    _stub_feeds(monkeypatch)
    jobs, sent, _ = build(anthropic_api_key="sk-test")
    await jobs.run_ai_news()
    assert sent == []


async def test_ai_news_posts_to_the_news_channel(monkeypatch):
    from alliegent.integrations import claude

    async def digest(api_key, day, entries, count=5):
        return "**1. Something happened**\nhttps://example.com\nEN: x\nKO: x"

    monkeypatch.setattr(claude, "write_digest", digest)
    _stub_feeds(monkeypatch)
    jobs, sent, _ = build(anthropic_api_key="sk-test")
    await jobs.run_ai_news()
    message, kind = sent[0]
    assert kind == "news"
    assert "Something happened" in message
    assert "AI News" in message


async def test_ai_news_is_dated_the_day_it_covers(monkeypatch):
    """The digest reports yesterday, so the header must say yesterday -- a
    09:00 digest headed with today would read as stale news, not fresh."""
    from alliegent.integrations import claude

    async def digest(api_key, day, entries, count=5):
        return f"**1. On {day.isoformat()}**\nhttps://example.com\nEN: x\nKO: x"

    monkeypatch.setattr(claude, "write_digest", digest)
    _stub_feeds(monkeypatch)
    jobs, sent, _ = build(anthropic_api_key="sk-test")
    await jobs.run_ai_news()
    yesterday = jobs.today() - timedelta(days=1)
    assert yesterday.isoformat() in sent[0][0]
    assert reports.fmt_date(yesterday) in sent[0][0]


async def test_ai_news_asks_the_feeds_for_yesterday(monkeypatch):
    from alliegent.integrations import claude

    asked = {}

    async def entries_for(day, tz, **kwargs):
        asked["day"] = day
        return [_entry()]

    monkeypatch.setattr(news_feeds, "entries_for", entries_for)

    async def digest(api_key, day, entries, count=5):
        return "body"

    monkeypatch.setattr(claude, "write_digest", digest)
    jobs, _, _ = build(anthropic_api_key="sk-test")
    await jobs.run_ai_news()
    assert asked["day"] == jobs.today() - timedelta(days=1)


async def test_ai_news_passes_the_configured_count(monkeypatch):
    from alliegent.integrations import claude

    seen = {}

    async def digest(api_key, day, entries, count=5):
        seen["count"] = count
        return "body"

    monkeypatch.setattr(claude, "write_digest", digest)
    _stub_feeds(monkeypatch)
    jobs, _, _ = build(anthropic_api_key="sk-test")
    jobs.config.news.count = 5
    await jobs.run_ai_news()
    assert seen["count"] == 5


async def test_weekly_planning_sends_even_when_next_week_is_empty():
    """An empty week is exactly when the planning nudge earns its place."""
    jobs, sent, _ = build()
    await jobs.run_weekly_planning()
    assert len(sent) == 1
    assert "Nothing scheduled for next week yet." in sent[0][0]


async def test_weekly_planning_reports_what_is_already_scheduled():
    jobs, sent, _ = build(
        [make_page("p1", "Dance class 7:10PM", day="2026-08-11", status="Not started")]
    )
    await jobs.run_weekly_planning()
    message = sent[0][0]
    assert "1 item(s) scheduled" in message
    assert "Mon 8/10" in message  # 8/11 has an item, so 8/10 is listed as empty


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
        agenda_pages=[make_page("t1", "Dance class 7:10PM", day="2026-08-03", recurring=True)],
        project_pages=[make_project("b", "방치됨")],
    )
    await jobs.run_daily_brief()
    await jobs.run_week_scaffold()
    await jobs.run_stale_projects()
    await jobs.run_weekly_review()
    assert [kind for _, kind in sent] == ["agenda", "agenda", "projects", "review"]


def test_scheduler_registers_the_enabled_jobs():
    """week_scaffold ships disabled — nothing in the agenda repeats weekly."""
    config = Config()
    jobs, _, _ = build()
    scheduler = build_scheduler(jobs, config)
    ids = {job.id for job in scheduler.get_jobs()}
    # Derived from the configured times rather than spelled out: the reminder
    # hours are a preference that changes, and a test that has to be edited
    # alongside them tests the edit, not the scheduling.
    assert ids == {
        "daily_brief",
        "ai_news",
        "karrot_report",
        "karrot_candidates",
        "asset_prompt",
        "weekly_planning",
        "stale_projects",
        "weekly_review",
    } | {f"incomplete_alert@{when}" for when in config.schedule.incomplete_alert}


def test_a_reminder_at_half_past_is_scheduled_on_the_minute():
    """19:30 has to mean 19:30, not 19:00 — the minute is parsed, not dropped."""
    config = Config()
    config.schedule.incomplete_alert = ["19:30"]
    jobs, _, _ = build()
    scheduler = build_scheduler(jobs, config)
    job = next(j for j in scheduler.get_jobs() if "incomplete_alert" in j.id)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "19"
    assert fields["minute"] == "30"


def test_an_empty_time_disables_a_job():
    jobs, _, _ = build()
    config = Config()
    config.schedule.daily_brief = ""
    ids = {job.id for job in build_scheduler(jobs, config).get_jobs()}
    assert "daily_brief" not in ids


def test_each_reminder_time_gets_its_own_job():
    """Two runs of the same alert would collide on a single id."""
    jobs, _, _ = build()
    config = Config()
    config.schedule.incomplete_alert = ["09:00", "15:00", "21:00"]
    ids = {job.id for job in build_scheduler(jobs, config).get_jobs()}
    assert {"incomplete_alert@09:00", "incomplete_alert@15:00",
            "incomplete_alert@21:00"} <= ids


def test_a_single_reminder_time_still_works():
    """An older alliegent.toml has a bare string here."""
    from alliegent.config import Schedule

    assert Schedule(incomplete_alert="21:00").incomplete_alert == ["21:00"]
    assert Schedule(incomplete_alert="15:00, 21:00").incomplete_alert == [
        "15:00",
        "21:00",
    ]


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


# -- a calendar that cannot be read ---------------------------------------


async def test_a_broken_calendar_is_reported_in_the_brief():
    """It failed every morning for a week and the only sign was a block that
    wasn't there. An empty calendar and an unreachable one must not look the
    same."""
    from caldav.lib.error import AuthorizationError

    async def source(day, tz):
        raise AuthorizationError(url="https://caldav.icloud.com", reason="Unauthorized")

    jobs, _, _ = build()
    jobs.calendar_source = source
    text = await jobs.build_daily_brief()
    assert "Calendar unavailable" in text
    assert "app password" in text


async def test_a_transient_calendar_failure_says_so_differently():
    """A network blip needs no action; a rejected password needs a new one."""

    async def source(day, tz):
        raise TimeoutError("no answer")

    jobs, _, _ = build()
    jobs.calendar_source = source
    text = await jobs.build_daily_brief()
    assert "Calendar unavailable" in text
    assert "app password" not in text


async def test_a_calendar_failure_still_leaves_a_usable_brief():
    """The agenda is the point of the brief; the calendar is an addition."""

    async def source(day, tz):
        raise TimeoutError("no answer")

    jobs, _, _ = build()
    jobs.calendar_source = source
    text = await jobs.build_daily_brief()
    assert "Daily brief" in text


async def test_no_calendar_configured_is_not_a_failure():
    """Most setups have no calendar at all, and that is not news."""
    jobs, _, _ = build()
    jobs.calendar_source = None
    assert "Calendar unavailable" not in await jobs.build_daily_brief()


# -- failures that used to be silent ---------------------------------------


async def test_a_failing_job_says_so_in_its_channel():
    """Every Notion job failed for two days after a duplicate .env key, and
    the only trace was a stack trace on the server. A bot built to stay quiet
    when there is nothing to say cannot also be quiet when it is broken."""
    from alliegent.scheduler import _announcing

    jobs, sent, _ = build()

    async def boom():
        raise RuntimeError("Notion said no")

    await _announcing(jobs, "daily_brief", boom)()
    message, kind = sent[0]
    assert "daily_brief" in message and "RuntimeError" in message
    assert kind == "agenda"


async def test_a_failure_is_reported_where_the_message_belonged():
    """Not all in one channel: the Karrot report failing is Karrot news."""
    from alliegent.scheduler import _announcing

    async def boom():
        raise RuntimeError("nope")

    jobs, sent, _ = build()
    await _announcing(jobs, "karrot_report", boom)()
    assert sent[0][1] == "karrot"


async def test_a_reminder_time_is_stripped_from_the_channel_lookup():
    """Job ids carry their time (incomplete_alert@14:00); the routing table
    does not."""
    from alliegent.scheduler import _announcing

    async def boom():
        raise RuntimeError("nope")

    jobs, sent, _ = build()
    await _announcing(jobs, "incomplete_alert@14:00", boom)()
    assert sent[0][1] == "agenda"


async def test_a_working_job_announces_nothing_extra():
    from alliegent.scheduler import _announcing

    jobs, sent, _ = build()
    await _announcing(jobs, "daily_brief", jobs.run_daily_brief)()
    assert all("failed" not in message for message, _ in sent)


def test_duplicate_env_keys_are_detected(tmp_path):
    """The failure mode itself: .env keeps the last value, so a second copy of
    a key silently replaces a working one."""
    from alliegent.config import duplicate_env_keys

    env = tmp_path / ".env"
    env.write_text(
        "NOTION_TOKEN=first\n# a comment\nOTHER=1\nNOTION_TOKEN=second\n"
    )
    assert duplicate_env_keys(env) == {"NOTION_TOKEN": 2}


def test_a_clean_env_reports_nothing(tmp_path):
    from alliegent.config import duplicate_env_keys

    env = tmp_path / ".env"
    env.write_text("NOTION_TOKEN=only\nOTHER=1\n")
    assert duplicate_env_keys(env) == {}
