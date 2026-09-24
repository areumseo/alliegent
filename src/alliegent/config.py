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
    # English lesson review: four linked databases. Lessons alone switches the
    # feature on; the other three are required with it.
    notion_english_lessons_db_id: str = ""
    notion_english_expressions_db_id: str = ""
    notion_english_mistakes_db_id: str = ""
    notion_english_reviews_db_id: str = ""

    anthropic_api_key: str = ""

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
            "projects": self.discord_projects_channel_id or self.discord_channel_id,
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
    stale_project_weekday: str = "wed"
    stale_project_time: str = "10:00"
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
