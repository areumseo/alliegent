"""Configuration: secrets from the environment, everything else from alliegent.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
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

    discord_bot_token: str = ""
    discord_channel_id: int = 0
    discord_guild_id: int = 0

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
    project: str = "Project"


class ProjectProps(BaseModel):
    title: str = "Name"
    status: str = "Status"
    next_action: str = "Next action"
    last_activity: str = ""


class Schedule(BaseModel):
    daily_brief: str = "08:00"
    incomplete_alert: str = "21:00"
    week_scaffold_weekday: str = "mon"
    week_scaffold_time: str = "06:00"
    stale_project_weekday: str = "wed"
    stale_project_time: str = "10:00"
    weekly_review_weekday: str = "sun"
    weekly_review_time: str = "21:00"


class AgendaConfig(BaseModel):
    scaffold_days: int = 7
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


class Config(BaseModel):
    timezone: str = "Asia/Seoul"
    schedule: Schedule = Field(default_factory=Schedule)
    agenda: AgendaConfig = Field(default_factory=AgendaConfig)
    projects: ProjectsConfig = Field(default_factory=ProjectsConfig)

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
