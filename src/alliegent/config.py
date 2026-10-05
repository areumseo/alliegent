"""Configuration: secrets from the environment, everything else from alliegent.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOML = REPO_ROOT / "alliegent.toml"


class Secrets(BaseSettings):
    """Credentials. Never written to disk by this app, never logged."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    notion_token: str = ""
    notion_agenda_db_id: str = ""
    notion_projects_db_id: str = ""
    notion_karrot_db_id: str = ""
    # Optional: selling costs, netted off revenue. Without it revenue is gross.
    notion_karrot_expenses_db_id: str = ""
    notion_assets_db_id: str = ""
    # Monthly targets to hold the asset snapshots against. Needs the assets
    # database too: a plan with nothing to compare it to reports nothing.
    notion_plans_db_id: str = ""
    # 🛠 Build Tools: what each tool used to build and run the services costs.
    # Fixed subscriptions are typed here; usage with an API is read live.
    notion_build_tools_db_id: str = ""
    # 🧾 Build Spend: what usage-billed tools actually charged, one row per tool
    # per month. Optional; without it a month is only ever estimated.
    notion_build_spend_db_id: str = ""
    # 🌅 Retirement Income: pensions and other sources after work stops, each
    # in its own currency. Needs the assets database too.
    notion_retirement_db_id: str = ""
    # English lesson review: four linked databases. Lessons alone switches the
    # feature on; the other three are required with it.
    notion_english_lessons_db_id: str = ""
    notion_english_expressions_db_id: str = ""
    notion_english_mistakes_db_id: str = ""
    notion_english_reviews_db_id: str = ""

    anthropic_api_key: str = ""
    # Organisation admin key (sk-ant-admin...), which is not the key above: it
    # can read the usage cost report and nothing else this bot does. Optional;
    # without it the Anthropic line is whatever is typed into Build Tools.
    anthropic_admin_key: str = ""

    # Optional. Public repositories are read without one; a private one needs
    # a token that can read contents, pull requests and issues.
    github_token: str = ""

    # Preferred calendar source: authenticated, nothing published. The
    # password is an app-specific one from appleid.apple.com, revocable on its
    # own without touching the account password.
    icloud_username: str = ""
    icloud_app_password: str = ""
    # Optional: only read these calendars, by name. Empty means all of them.
    icloud_calendars: str = ""
    # Which calendar new events go into. Deliberately has no default: writing
    # into whichever calendar happened to come back first is not a guess worth
    # making on someone's real calendar.
    icloud_write_calendar: str = ""

    # Fallback source. ICS subscription links are unauthenticated — anyone
    # holding one can read that calendar — so they are secrets despite looking
    # like ordinary URLs, and an iCloud one can only be revoked by
    # unpublishing the calendar. Prefer CalDAV above.
    calendar_ics_urls: str = ""

    # Optional Gmail delivery for the daily AI news digest.
    ai_news_email_to: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    discord_bot_token: str = ""
    discord_guild_id: int = 0

    # Fallback used by any channel left unset below.
    discord_channel_id: int = 0
    discord_agenda_channel_id: int = 0
    # #build: projects' days and weeks, and what the tools cost. The older name
    # still works, so a .env written when it was #projects keeps going.
    discord_build_channel_id: int = 0
    discord_projects_channel_id: int = 0
    discord_review_channel_id: int = 0
    discord_news_channel_id: int = 0
    discord_karrot_channel_id: int = 0
    discord_assets_channel_id: int = 0
    discord_english_channel_id: int = 0

    @field_validator(
        "discord_guild_id",
        "discord_channel_id",
        "discord_agenda_channel_id",
        "discord_build_channel_id",
        "discord_projects_channel_id",
        "discord_review_channel_id",
        "discord_news_channel_id",
        "discord_karrot_channel_id",
        "discord_assets_channel_id",
        "discord_english_channel_id",
        mode="before",
    )
    @classmethod
    def _optional_id(cls, value: object) -> object:
        """Treat a blank ID as 'not configured' rather than a parse error.

        A .env full of `KEY=` placeholders is the normal starting state, and it
        should not crash before the app can explain what is missing. Trailing
        comments are stripped too, since python-dotenv leaves them on the value.
        """
        if isinstance(value, str):
            text = value.split("#", 1)[0].strip()
            return text or 0
        return value

    def channel_for(self, kind: str) -> int:
        """Resolve a job's target channel, falling back to the default.

        Review posts fall back to the agenda channel rather than the generic
        default, since a weekly review belongs with the agenda if it has no
        channel of its own.
        """
        agenda = self.discord_agenda_channel_id or self.discord_channel_id
        routes = {
            "agenda": agenda,
            "build": (
                self.discord_build_channel_id
                or self.discord_projects_channel_id
                or self.discord_channel_id
            ),
            "review": self.discord_review_channel_id or agenda,
            "news": self.discord_news_channel_id or self.discord_channel_id,
            "karrot": self.discord_karrot_channel_id or self.discord_channel_id,
            "assets": self.discord_assets_channel_id or self.discord_channel_id,
            "english": self.discord_english_channel_id or self.discord_channel_id,
        }
        target = routes.get(kind, agenda)
        if not target:
            raise RuntimeError(
                f"No Discord channel configured for {kind!r}. Set "
                f"DISCORD_{kind.upper()}_CHANNEL_ID or DISCORD_CHANNEL_ID."
            )
        return target

    def require(self, *names: str) -> None:
        """Fail loudly and early rather than mid-job with a confusing 401."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                "Missing required settings: "
                + ", ".join(n.upper() for n in missing)
                + ". Set them in .env (local) or `fly secrets set` (production). "
                "See .env.example."
            )


class AgendaProps(BaseModel):
    title: str = "Name"
    date: str = "Date"
    status: str = "Status"
    recurring: str = "Recurring"
    category: str = "Category"
    project: str = ""


class ProjectProps(BaseModel):
    title: str = "Name"
    status: str = "Status"
    # Both are derived from the linked agenda rows when left empty, which is
    # the default and the better answer — see ProjectService._with_activity.
    # Name a column here only if you keep one up to date by hand.
    next_action: str = ""
    last_activity: str = ""
    # Written by the bot each night from the linked agenda rows: a relation to
    # the soonest unfinished item, and how many were finished in seven days.
    # "" leaves the column alone.
    next_item: str = ""
    done_week: str = ""
    # Also written nightly: linked items still open, and the share finished of
    # those not canceled. Calculated here rather than as Notion rollups, which
    # the API creates without their calculation and which would count
    # Canceled as complete.
    open_count: str = ""
    progress: str = ""
    # GitHub repositories, as owner/repo or their URLs, comma-separated. A
    # project with none stays out of the #build forum -- which is how the
    # forum is limited to the projects that have code to report on.
    repos: str = "GitHub"
    # Where the bot records the forum post it made for the project. Written by
    # the bot; kept in Notion so a renamed project keeps its post.
    thread: str = "Discord Thread"

class Schedule(BaseModel):
    daily_brief: str = "08:00"
    ai_news: str = "09:00"
    # A list: one nudge in the afternoon while the day can still be changed,
    # one in the evening to close it out. A single string is still accepted,
    # so an older alliegent.toml keeps working.
    incomplete_alert: list[str] = Field(default_factory=lambda: ["14:00", "19:30"])
    weekly_planning_weekday: str = "sat"
    weekly_planning_time: str = "10:00"
    week_scaffold_weekday: str = "mon"
    # Empty disables the job. Off by default: nothing in the agenda repeats
    # weekly yet, so there is no template to copy from.
    week_scaffold_time: str = ""
    # Every open project's week, in the #build Overview post. It took over
    # from a Wednesday check that named only the stalled ones; the end of the
    # week is when "this week" means a whole one.
    project_week_weekday: str = "sun"
    project_week_time: str = "20:00"
    weekly_review_weekday: str = "sun"
    weekly_review_time: str = "21:00"
    # Two Karrot reports, at the two ends of a week. Monday asks what is
    # waiting to be listed -- the week is the unit you would act in. Saturday
    # counts what the week actually sold. Blank either time to switch it off.
    karrot_candidates_weekday: str = "mon"
    karrot_candidates_time: str = "09:00"
    karrot_report_weekday: str = "sat"
    karrot_report_time: str = "20:00"
    # Monday morning, with last week's figures to edit rather than a blank
    # form to fill. Blank the time to switch it off.
    asset_prompt_weekday: str = "mon"
    asset_prompt_time: str = "09:00"
    # Evenings only matter on lesson days; the job stays silent otherwise.
    english_quiz: str = "20:30"
    # The half-yearly bonus arrives at the end of September and March. On the
    # first of the following month it moves from Expected into Savings. Blank
    # the time to switch it off.
    bonus_rollover_months: list[int] = Field(default_factory=lambda: [4, 10])
    # The last day of each ESPP offering period; contributions become shares
    # the next day. Listed rather than recurring because each cycle's dates are
    # set by the plan and drift. Add the next one when it is announced.
    espp_purchase_dates: list[str] = Field(default_factory=lambda: ["2027-03-11"])
    espp_rollover_time: str = "09:00"
    bonus_rollover_day: int = 1
    bonus_rollover_time: str = "09:00"
    # Checked daily, not only on the 1st: the month-end snapshot is whichever
    # is recorded first on or after the 1st, which is usually a Monday later.
    # The report goes out once, on the day that snapshot exists.
    plan_report_time: str = "09:00"
    # Each project's day in its #build post: what was done, what is in
    # review, what is next. Late enough to catch the evening's work.
    project_log_time: str = "22:00"
    # What is open in every project, in the #build Overview post: the
    # morning half of the log, read when the day is being planned.
    project_open_time: str = "08:00"
    # The month just ended, tool by tool, on the day after the bill. Blank the
    # time to switch it off.
    build_costs_day: int = 1
    build_costs_time: str = "09:30"

    @field_validator("incomplete_alert", mode="before")
    @classmethod
    def _times(cls, value: object) -> object:
        """Accept a bare string as well as a list, so an older config works."""
        if isinstance(value, str):
            return [t.strip() for t in value.split(",") if t.strip()]
        return value


class AgendaConfig(BaseModel):
    scaffold_days: int = 7
    scaffold_from_recurring: bool = True
    # How far back to look when inferring a new item's category from how the
    # same activity was filed before.
    category_lookback_days: int = 120
    props: AgendaProps = Field(default_factory=AgendaProps)
    status_values: dict[str, str] = Field(
        default_factory=lambda: {
            "todo": "Not started",
            "doing": "In progress",
            "done": "Done",
        }
    )


class ProjectsConfig(BaseModel):
    stale_after_days: int = 7
    props: ProjectProps = Field(default_factory=ProjectProps)
    status_values: dict[str, str] = Field(
        default_factory=lambda: {"active": "In progress", "done": "Done"}
    )


class KarrotConfig(BaseModel):
    """Nothing to configure yet; the report is scheduled from [schedule]."""


class PlansConfig(BaseModel):
    # Vested plus SNOW over Liquid, above which the report warns.
    snow_cap: float = 0.30
    # Months in a row under Low before the row is marked Revised.
    low_streak: int = 3
    # The yearly allowance on foreign-stock gains before the 22% applies.
    # Tax law, not a personal figure, which is why it may live here.
    gain_allowance: int = 2_500_000


class NewsConfig(BaseModel):
    # Five items of three sentences in two languages is most of the output
    # tokens this job spends, and the digest is read on a phone.
    count: int = 5


class Config(BaseModel):
    timezone: str = "Asia/Seoul"
    schedule: Schedule = Field(default_factory=Schedule)
    agenda: AgendaConfig = Field(default_factory=AgendaConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    karrot: KarrotConfig = Field(default_factory=KarrotConfig)
    plans: PlansConfig = Field(default_factory=PlansConfig)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_TOML
    if not path.exists():
        return Config()
    with path.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def duplicate_env_keys(path: Path | None = None) -> dict[str, int]:
    """Keys that appear more than once in .env, with how many times.

    The last line wins silently, so a key pasted a second time replaces a
    working value with no error anywhere. A second NOTION_TOKEN did exactly
    that on 2026-09-11 and every Notion job failed for two days while the
    file still visibly contained the right token, further up.
    """
    path = path or Path(".env")
    if not path.exists():
        return {}
    seen: dict[str, int] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        seen[key] = seen.get(key, 0) + 1
    return {key: count for key, count in seen.items() if count > 1}


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
