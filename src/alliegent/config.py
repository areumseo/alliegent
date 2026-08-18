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

    @field_validator(
        "discord_guild_id",
        "discord_channel_id",
        "discord_agenda_channel_id",
        "discord_projects_channel_id",
        "discord_review_channel_id",
        "discord_news_channel_id",
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
    # Number property that fixes the within-a-day order. Without it the rows
    # come back in whatever order Notion returns, which /reorder cannot change.
    order: str = "Order"
    project: str = ""


class ProjectProps(BaseModel):
    title: str = "Name"
    status: str = "Status"
    next_action: str = ""
    last_activity: str = ""
    related_schedule: str = ""

class Schedule(BaseModel):
    daily_brief: str = "08:00"
    ai_news: str = "09:00"
    # A list: one nudge in the afternoon while the day can still be changed,
    # one at night to close it out. A single string is still accepted, so an
    # older alliegent.toml keeps working.
    incomplete_alert: list[str] = Field(default_factory=lambda: ["15:00", "21:00"])
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


class NewsConfig(BaseModel):
    count: int = 10


class Config(BaseModel):
    timezone: str = "Asia/Seoul"
    schedule: Schedule = Field(default_factory=Schedule)
    agenda: AgendaConfig = Field(default_factory=AgendaConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)

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


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()
