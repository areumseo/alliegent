from __future__ import annotations

from typing import Any

import pytest

from alliegent.config import Config


def make_page(
    page_id: str,
    title: str,
    *,
    day: str | None = None,
    status: str | None = "Not started",
    status_type: str = "status",
    checkbox: bool | None = None,
    projects: list[str] | None = None,
    recurring: bool | None = None,
    category: str | None = None,
    in_trash: bool = False,
) -> dict[str, Any]:
    """Build a Notion page object shaped the way the API returns it."""
    props: dict[str, Any] = {
        "Name": {
            "type": "title",
            "title": [{"plain_text": title, "type": "text"}],
        }
    }
    if day is not None:
        props["Date"] = {"type": "date", "date": {"start": day, "end": None}}
    if checkbox is not None:
        props["Status"] = {"type": "checkbox", "checkbox": checkbox}
    elif status is not None:
        props["Status"] = {"type": status_type, status_type: {"name": status}}
    if projects is not None:
        props["Project"] = {
            "type": "relation",
            "relation": [{"id": pid} for pid in projects],
        }
    if recurring is not None:
        props["Recurring"] = {"type": "checkbox", "checkbox": recurring}
    if category is not None:
        props["Category"] = {"type": "select", "select": {"name": category}}
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "in_trash": in_trash,
        "properties": props,
    }


def make_project(
    page_id: str,
    title: str,
    *,
    status: str = "In progress",
    next_action: str = "",
    last_activity: str | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {
        "Name": {"type": "title", "title": [{"plain_text": title, "type": "text"}]},
        "Status": {"type": "status", "status": {"name": status}},
    }
    if next_action:
        props["Next action"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": next_action, "type": "text"}],
        }
    if last_activity:
        props["Last activity"] = {
            "type": "date",
            "date": {"start": last_activity, "end": None},
        }
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "in_trash": False,
        "properties": props,
    }


class FakeNotionClient:
    """Stands in for NotionClient in service-level tests.

    Records writes so tests can assert on what would hit the real API.
    """

    def __init__(
        self,
        pages: dict[str, list[dict[str, Any]]] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        # keyed by data source id
        self.pages = pages or {}
        self.schema = schema or {
            "Name": {"type": "title"},
            "Date": {"type": "date"},
            "Status": {"type": "status"},
            "Project": {"type": "relation"},
        }
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.trashed: list[str] = []

    async def resolve_data_source(self, database_id: str) -> str:
        return f"ds_{database_id}"

    async def get_schema(self, data_source_id: str) -> dict[str, Any]:
        return self.schema

    async def query(self, data_source_id, *, filter=None, sorts=None, page_size=100):
        # Apply date filters for real. The week-scaffolding logic queries two
        # different date ranges and compares them, so a fake that ignored
        # filters would make those tests meaningless.
        bounds = _date_bounds(filter)
        for page in self.pages.get(data_source_id, []):
            if page.get("in_trash"):
                continue
            if bounds and not _within(page, bounds):
                continue
            yield page

    async def create_page(self, data_source_id, properties, *, children=None):
        self.created.append((data_source_id, properties))
        title = properties["Name"]["title"][0]["text"]["content"]
        day = properties.get("Date", {}).get("date", {}).get("start")
        return make_page(f"new_{len(self.created)}", title, day=day)

    async def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id}

    async def trash_page(self, page_id):
        self.trashed.append(page_id)
        return {"id": page_id, "in_trash": True}


def _date_bounds(filter: dict[str, Any] | None) -> dict[str, str] | None:
    """Flatten the date conditions out of a Notion filter into {op: value}."""
    if not filter:
        return None
    clauses = filter.get("and") or [filter]
    bounds: dict[str, str] = {}
    for clause in clauses:
        for op, value in (clause.get("date") or {}).items():
            bounds[op] = value
    return bounds or None


def _within(page: dict[str, Any], bounds: dict[str, str]) -> bool:
    value = (page.get("properties", {}).get("Date") or {}).get("date")
    if not value or not value.get("start"):
        return False
    day = value["start"][:10]
    if "on_or_after" in bounds and day < bounds["on_or_after"]:
        return False
    if "on_or_before" in bounds and day > bounds["on_or_before"]:
        return False
    if "before" in bounds and day >= bounds["before"]:
        return False
    return True


@pytest.fixture
def config() -> Config:
    return Config()


SECRET_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ICLOUD_USERNAME",
    "ICLOUD_APP_PASSWORD",
    "ICLOUD_CALENDARS",
    "CALENDAR_ICS_URLS",
    "DISCORD_NEWS_CHANNEL_ID",
    "NOTION_TOKEN",
    "NOTION_AGENDA_DB_ID",
    "NOTION_PROJECTS_DB_ID",
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_CHANNEL_ID",
    "DISCORD_AGENDA_CHANNEL_ID",
    "DISCORD_PROJECTS_CHANNEL_ID",
    "DISCORD_REVIEW_CHANNEL_ID",
)


@pytest.fixture(autouse=True)
def isolate_secrets(monkeypatch, tmp_path):
    """Keep Secrets() from picking up the developer's real .env or shell vars.

    Without this, a filled-in local .env makes tests pass here and fail in CI
    (or the reverse), which is the worst kind of flake.
    """
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # `env_file=".env"` resolves against the working directory.
    monkeypatch.chdir(tmp_path)
