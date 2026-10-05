"""Scheduled jobs.

Each job *computes* its message and returns it (or None when there is nothing
worth saying). Sending is a separate step, which keeps the jobs testable and
makes `--dry-run` honest: it runs the same code path, minus the send.

The one job that writes to Notion (week_scaffold) takes an explicit `commit`
flag so a dry run can report what it would create without creating it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

import httpx

from . import reports
from .agenda import AgendaService, Project, ProjectService
from .config import Config, Secrets

log = logging.getLogger(__name__)

# (message, channel_kind) -> None. The kind is routed to a channel by the
# notifier, so jobs stay unaware of Discord IDs.
Notifier = Callable[[str, str], Awaitable[None]]


class Jobs:
    def __init__(
        self,
        agenda: AgendaService,
        projects: ProjectService | None,
        config: Config,
        notify: Notifier,
        clock: Callable[[], date] | None = None,
        karrot=None,
        expenses=None,
        assets=None,
        plans=None,
        costs=None,
        english=None,
        anthropic_api_key: str = "",
        calendar_source: Callable | None = None,
        secrets: Secrets | None = None,
        post_project: Callable[[int | None, str, str], Awaitable[int | None]] | None = None,
    ) -> None:
        self.calendar_source = calendar_source
        self.agenda = agenda
        self.projects = projects
        self.karrot = karrot
        self.expenses = expenses
        self.assets = assets
        self.plans = plans
        self.costs = costs
        self.english = english
        self.config = config
        self.notify = notify
        self._anthropic_api_key = anthropic_api_key
        self._secrets = secrets
        # Posts to a project's forum thread, making it if need be, and returns
        # its id. Injected, like notify, so the job runs without Discord.
        self._post_project = post_project
        # Injectable so tests can pin a date instead of drifting with the
        # calendar; production leaves it as the configured timezone's today.
        self._clock = clock

    def today(self) -> date:
        if self._clock is not None:
            return self._clock()
        return datetime.now(self.config.tz).date()

    # -- message builders --------------------------------------------------

    async def calendar_on(self, day: date) -> tuple[list, str | None]:
        """Today's events, plus why they are missing when they are.

        A calendar outage should cost the brief its calendar block, not the
        whole brief -- but it should not cost it silently either. This failed
        every morning for a week with an expired password and the only sign
        was the absence of a block nobody was looking for.
        """
        if self.calendar_source is None:
            return [], None
        try:
            return await self.calendar_source(day, self.config.tz), None
        except Exception as exc:
            log.exception("calendar lookup failed")
            # An authorisation failure will not clear up on its own: the app
            # password has been revoked or has expired, and only the person
            # holding the Apple ID can issue another.
            if "authorization" in type(exc).__name__.casefold():
                return [], "auth"
            return [], "error"

    async def build_daily_brief(self) -> str:
        today = self.today()
        todays = await self.agenda.items_on(today)
        overdue = await self.agenda.overdue(today)
        events, calendar_problem = await self.calendar_on(today)
        return reports.daily_brief(
            today, todays, overdue, events, calendar_problem=calendar_problem
        )

    async def build_incomplete_alert(self) -> str | None:
        today = self.today()
        todays = await self.agenda.items_on(today)
        overdue = await self.agenda.overdue(today)
        return reports.incomplete_alert(today, todays, overdue)

    async def build_project_week(self) -> str | None:
        """Every open project's last seven days, for the Overview post."""
        if not self.projects:
            return None
        from . import project_log
        from .integrations.github import GitHub, GitHubError

        today = self.today()
        projects = await self.projects.active(today, self.agenda)
        code_done: dict[str, int] = {}
        coded = [p for p in projects if p.repos]
        if coded:
            github = GitHub(self._secrets.github_token if self._secrets else "")
            first = today - timedelta(days=6)
            try:
                for project in coded:
                    for repo in project.repos:
                        try:
                            span = await github.span(
                                repo, first, today + timedelta(days=1), self.config.tz,
                                open_items=False,
                            )
                        except (GitHubError, httpx.HTTPError) as exc:
                            log.warning("GitHub: %s", exc)
                            continue
                        code_done[project.id] = code_done.get(project.id, 0) + len(span.done)
            finally:
                await github.aclose()
        message = project_log.week_message(
            projects,
            code_done,
            today,
            stale_after_days=self.config.projects.stale_after_days,
        )
        # What the tools cost, as one line under the week: the status and the
        # spend are read together, which is why #build holds both.
        line = None
        if self.costs is not None:
            from . import build_costs

            line = await build_costs.weekly_line(self.costs, self._admin_key(), today)
        text = "\n".join(part for part in (message, line) if part)
        return text or None

    def _admin_key(self) -> str:
        return self._secrets.anthropic_admin_key if self._secrets else ""

    async def build_costs(self, *, settle: bool = False) -> str | None:
        """The costs table; the month just ended when `settle`."""
        if self.costs is None:
            return None
        from . import build_costs

        return await build_costs.build_costs(
            self.costs, self._admin_key(), self.today(), settle=settle
        )

    async def run_build_costs(self) -> None:
        await self._send(await self.build_costs(settle=True), "build")

    async def build_ai_news(self) -> str | None:
        """Collect yesterday's AI articles and write the digest.

        Returns None when the digest can't be produced. A missing morning
        digest is a non-event; a stack trace in the news channel is not.
        """
        if not self._anthropic_api_key:
            log.warning("ANTHROPIC_API_KEY not set — AI news is disabled")
            return None

        from .integrations.claude import NewsUnavailable, write_digest
        from .integrations.news_feeds import entries_for, recent_window, stale_feeds

        # Yesterday, not today: at nine in the morning the day's own stories
        # have barely been filed, and the reader wants the day they just had.
        day = recent_window(self.today())
        entries = await entries_for(day, self.config.tz)

        quiet = stale_feeds(entries)
        if quiet:
            # A feed that moves or dies goes silent rather than failing, and
            # the digest stays plausible while quietly losing a publication.
            log.warning("no articles from: %s", ", ".join(quiet))

        # The one job that costs money per run, so the call is on the record.
        log.info("writing the AI news digest from %d articles", len(entries))
        try:
            body = await write_digest(
                self._anthropic_api_key, day, entries, count=self.config.news.count
            )
        except NewsUnavailable as exc:
            log.error("AI news unavailable: %s", exc)
            return None
        return reports.ai_news(day, body)

    async def build_karrot_report(self) -> str | None:
        """Sunday's sales week, in Korean.

        None when nothing sold and nothing is owed: a weekly report that says
        "0건" every week is one you stop opening.
        """
        if self.karrot is None:
            return None
        from . import karrot as karrot_module

        today = self.today()
        data = await self.karrot.sales(today)
        unpaid = await self.karrot.unpaid()
        spending = await self.expenses.spending(today) if self.expenses else None
        return karrot_module.weekly_message(data, unpaid, today, spending)

    async def build_karrot_candidates(self) -> str | None:
        """Monday's list of what is decided on but not yet listed."""
        if self.karrot is None:
            return None
        from . import karrot as karrot_module

        return karrot_module.candidates_message(await self.karrot.all_items())

    async def build_asset_prompt(self) -> str | None:
        """Monday's reminder to record the week's balances."""
        if self.assets is None:
            return None
        from . import assets as assets_module

        today = self.today()
        return assets_module.prompt_message(
            await self.assets.latest(),
            today,
            pending=assets_module.moves_before(today, self.config.schedule),
        )

    async def build_plan_check(self, *, commit: bool = True) -> str | None:
        """The month just finished against its plan, once its snapshot exists."""
        if self.plans is None or self.assets is None:
            return None
        from .plans import monthly_check

        return await monthly_check(
            self.plans, self.assets, self.today(), self.config, commit=commit
        )

    async def run_plan_check(self) -> None:
        await self._send(await self.build_plan_check(), "assets")

    async def build_project_logs(
        self, *, only: str | None = None
    ) -> tuple[list[tuple[Project, str]], dict[str, date]]:
        """Each project with a repository and its day, when it had one -- and,
        by project id, today for each whose code moved, for the sync.

        `only` narrows it to one project id, for /projects log.
        """
        if self.projects is None:
            return [], {}
        from . import project_log
        from .integrations.github import GitHub, GitHubError
        from .reports import finished

        today = self.today()
        projects = [
            p
            for p in await self.projects.open_projects()
            if p.repos and (only is None or p.id == only)
        ]
        if not projects:
            return [], {}
        github = GitHub(self._secrets.github_token if self._secrets else "")
        out: list[tuple[Project, str]] = []
        code_days: dict[str, date] = {}
        try:
            for project in projects:
                activities, errors = [], []
                for repo in project.repos:
                    try:
                        activities.append(await github.day(repo, today, self.config.tz))
                    except (GitHubError, httpx.HTTPError) as exc:
                        # One repository down should not cost the project its day.
                        log.warning("GitHub: %s", exc)
                        errors.append(str(exc))
                if any(a.done for a in activities):
                    code_days[project.id] = today
                linked = await self.agenda.items_for_project(project.id)
                message = project_log.log_message(
                    project,
                    today,
                    activities,
                    errors,
                    done_items=[i for i in linked if i.day == today and finished(i)],
                    todo_items=[i for i in linked if not i.done],
                )
                if message:
                    out.append((project, message))
        finally:
            await github.aclose()
        return out, code_days

    async def build_project_open(self) -> str | None:
        """What is open in every project -- in review and to-do -- for the
        Overview post each morning."""
        if self.projects is None:
            return None
        from . import project_log
        from .integrations.github import GitHub, GitHubError

        projects = await self.projects.open_projects()
        if not projects:
            return None
        github = GitHub(self._secrets.github_token if self._secrets else "")
        entries = []
        try:
            for project in projects:
                activities, errors = [], []
                for repo in project.repos:
                    try:
                        activities.append(await github.open_items(repo))
                    except (GitHubError, httpx.HTTPError) as exc:
                        log.warning("GitHub: %s", exc)
                        errors.append(str(exc))
                linked = await self.agenda.items_for_project(project.id)
                entries.append((project, activities, [i for i in linked if not i.done], errors))
        finally:
            await github.aclose()
        return project_log.open_message(self.today(), entries)

    async def run_project_open(self) -> None:
        await self._send(await self.build_project_open(), "build")

    async def run_project_log(self) -> None:
        logs, code_days = await self.build_project_logs()
        for project, message in logs:
            if self._post_project is None:
                await self._send(f"**{project.title}**\n{message}", "build")
                continue
            thread = await self._post_project(project.thread_id, project.title, message)
            # A new post -- the first, or one made because the old was deleted
            # -- is remembered, so tomorrow's day lands under today's.
            if thread and thread != project.thread_id and self.projects:
                await self.projects.set_thread(project.id, thread)
        # Every open project, not only those that posted: the seven-day count
        # and the next item move on quiet days too.
        if self.projects is not None:
            await self.projects.sync(self.today(), self.agenda, code_days)

    async def run_english_quiz(self) -> None:
        """Quiz on today's lessons, if there were any not quizzed yet.

        Sent directly rather than through _send, because the quiz needs the id
        of the message it was posted as: answers are replies to it, and that
        is how a reply is told apart from new lesson material.
        """
        if self.english is None:
            return
        from . import english as eng

        today = self.today()
        lessons = [lesson for lesson in await self.english.lessons_on(today) if not lesson.quizzed]
        if not lessons:
            log.info("no new English lesson today; no quiz")
            return
        expressions = await self.english.expressions_for({lesson.id for lesson in lessons})
        questions = [e for e in expressions if e.context][: eng.QUIZ_SIZE]
        if not questions:
            await self.english.mark_quizzed([lesson.id for lesson in lessons])
            log.info("today's lessons had nothing to quiz on")
            return

        sent = await self.notify(
            eng.quiz_message(questions, [lesson.topic for lesson in lessons]), "english"
        )
        message_id = str(sent[0].id) if sent else ""
        await self.english.record_questions(today, questions, message_id)
        await self.english.mark_quizzed([lesson.id for lesson in lessons])
        log.info("sent english quiz (%d questions)", len(questions))

    async def run_bonus_rollover(self) -> None:
        if self.assets is None:
            return
        from . import assets as assets_module

        today = self.today()
        moved = await self.assets.roll_bonus_into_savings(today)
        if moved is None:
            log.info("no bonus recorded; nothing to move into Savings")
            return
        await self._send(assets_module.bonus_moved_message(today, *moved), "assets")

    async def run_espp_rollover(self) -> None:
        if self.assets is None:
            return
        from . import assets as assets_module

        today = self.today()
        moved = await self.assets.roll_espp_into_vested(today)
        if moved is None:
            log.info("no ESPP recorded; nothing to move into Vested")
            return
        await self._send(assets_module.espp_moved_message(today, *moved), "assets")

    async def build_weekly_planning(self) -> str:
        """Nudge to plan the coming week, with what is already in it.

        Always sends, even when the week is empty — an empty week is exactly
        when the reminder is worth having.
        """
        today = self.today()
        week_start = self.coming_monday(today)
        items = await self.agenda.items_between(week_start, week_start + timedelta(days=6))
        overdue = await self.agenda.overdue(today)
        return reports.weekly_planning(week_start, items, overdue)

    async def build_weekly_review(self) -> str:
        today = self.today()
        start = today - timedelta(days=6)
        items = await self.agenda.items_between(start, today)
        return reports.weekly_review(start, today, items)

    def coming_monday(self, today: date) -> date:
        """The Monday of the week to scaffold — today if it already is Monday."""
        return today + timedelta(days=(7 - today.weekday()) % 7)

    async def week_scaffold(self, *, commit: bool = True) -> str | None:
        """Copy last week's recurring items onto the coming week.

        Unlike the other jobs this one writes to Notion, so `commit=False`
        reports what *would* be created without touching the database.
        """
        week_start = self.coming_monday(self.today())
        if commit:
            created = await self.agenda.scaffold_week(week_start)
        else:
            created = await self.agenda.plan_week(week_start)
        return reports.week_scaffold(week_start, created)

    # -- run + send --------------------------------------------------------

    async def _send(self, message: str | None, kind: str) -> None:
        if message is None:
            log.info("nothing to report for %s; staying quiet", kind)
            return
        await self.notify(message, kind)
        # Logged on every send so "did this go out once or twice?" is a
        # question the log can answer. A second instance is invisible from
        # inside either one -- each looks perfectly healthy -- and for the
        # news digest a duplicate is a duplicate model call, so the count
        # is worth being able to check.
        log.info("sent %s to the %s channel", kind, kind)

    async def run_daily_brief(self) -> None:
        await self._send(await self.build_daily_brief(), "agenda")

    async def run_incomplete_alert(self) -> None:
        await self._send(await self.build_incomplete_alert(), "agenda")

    async def run_project_week(self) -> None:
        await self._send(await self.build_project_week(), "build")

    async def run_ai_news(self) -> None:
        message = await self.build_ai_news()
        await self._send(message, "news")

        if (
            message is None
            or self._secrets is None
            or not self._secrets.ai_news_email_to
        ):
            return

        from .integrations.email import send_ai_news_email

        subject = f"Alliegent AI News — {self.today().isoformat()}"

        try:
            await send_ai_news_email(self._secrets, subject, message)
            log.info(
                "AI news email sent to %s",
                self._secrets.ai_news_email_to,
            )
        except Exception:
            # Email failure must not prevent or undo Discord delivery.
            log.exception("AI news email delivery failed")

    async def run_karrot_report(self) -> None:
        await self._send(await self.build_karrot_report(), "karrot")

    async def run_karrot_candidates(self) -> None:
        await self._send(await self.build_karrot_candidates(), "karrot")

    async def run_asset_prompt(self) -> None:
        await self._send(await self.build_asset_prompt(), "assets")

    async def run_weekly_planning(self) -> None:
        await self._send(await self.build_weekly_planning(), "agenda")

    async def run_weekly_review(self) -> None:
        await self._send(await self.build_weekly_review(), "review")

    async def run_week_scaffold(self) -> None:
        await self._send(await self.week_scaffold(commit=True), "agenda")
