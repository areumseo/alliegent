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

    async def resolve_data_source(self, database_id: str) -> str:
        return f"ds_{database_id}"

    async def get_schema(self, data_source_id: str) -> dict[str, Any]:
        return self.schema

    async def query(self, data_source_id, *, filter=None, sorts=None, page_size=100):
        for page in self.pages.get(data_source_id, []):
            if not page.get("in_trash"):
                yield page

    async def create_page(self, data_source_id, properties, *, children=None):
        self.created.append((data_source_id, properties))
        title = properties["Name"]["title"][0]["text"]["content"]
        day = properties.get("Date", {}).get("date", {}).get("start")
        return make_page(f"new_{len(self.created)}", title, day=day)

    async def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id}


@pytest.fixture
def config() -> Config:
    return Config()
