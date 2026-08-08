"""Minimal async Notion client pinned to API version 2026-03-11.

Two things about this version are easy to get wrong:

1. Databases no longer hold a schema. A database contains one or more
   *data sources*, and queries/schema/page-creation all target a
   ``data_source_id`` — not the database ID from the URL. See
   ``resolve_data_source``.
2. The trash flag is ``in_trash``; ``archived`` was renamed in 2026-03-11.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"


class NotionError(RuntimeError):
    """A non-retryable error returned by the Notion API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Notion API {status} [{code}]: {message}")
        self.status = status
        self.code = code


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 4,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self._ds_cache: dict[str, str] = {}

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        url = f"{API_BASE}{path}"

        for attempt in range(self._max_retries):
            resp = await self._client.request(method, url, headers=headers, **kwargs)

            # 429 carries Retry-After; 5xx is worth a back-off too.
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries - 1:
                    break
                delay = float(resp.headers.get("Retry-After", 2**attempt))
                log.warning(
                    "Notion %s %s -> %s, retrying in %.1fs",
                    method,
                    path,
                    resp.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.is_success:
                return resp.json()

            body = _safe_json(resp)
            raise NotionError(
                resp.status_code,
                body.get("code", "unknown"),
                body.get("message", resp.text[:300]),
            )

        body = _safe_json(resp)
        raise NotionError(
            resp.status_code,
            body.get("code", "rate_limited"),
            body.get("message", "exhausted retries"),
        )

    # -- databases / data sources -----------------------------------------

    async def get_database(self, database_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/databases/{database_id}")

    async def resolve_data_source(self, database_id: str) -> str:
        """Map a database ID (what you copy from the Notion URL) to a data source ID.

        Cached per client: the mapping only changes if the user restructures
        the database, which does not happen mid-process.
        """
        if database_id in self._ds_cache:
            return self._ds_cache[database_id]

        db = await self.get_database(database_id)
        sources = db.get("data_sources") or []
        if not sources:
            raise NotionError(
                200,
                "no_data_source",
                f"Database {database_id} exposes no data sources. Confirm the ID is a "
                "database (not a page) and that it is shared with the integration.",
            )
        if len(sources) > 1:
            log.warning(
                "Database %s has %d data sources; using the first (%s).",
                database_id,
                len(sources),
                sources[0].get("name"),
            )
        ds_id = sources[0]["id"]
        self._ds_cache[database_id] = ds_id
        return ds_id

    async def get_schema(self, data_source_id: str) -> dict[str, Any]:
        """Return the data source's ``properties`` map (name -> definition)."""
        ds = await self._request("GET", f"/data_sources/{data_source_id}")
        return ds.get("properties", {})

    # -- pages -------------------------------------------------------------

    async def query(
        self,
        data_source_id: str,
        *,
        filter: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every page in a data source, following pagination."""
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": page_size}
            if filter:
                payload["filter"] = filter
            if sorts:
                payload["sorts"] = sorts
            if cursor:
                payload["start_cursor"] = cursor

            data = await self._request(
                "POST", f"/data_sources/{data_source_id}/query", json=payload
            )
            for row in data.get("results", []):
                # Trashed rows still come back on some filters; never act on them.
                if not row.get("in_trash", False):
                    yield row

            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    async def create_page(
        self,
        data_source_id: str,
        properties: dict[str, Any],
        *,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return await self._request("POST", "/pages", json=payload)

    async def update_page(
        self, page_id: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/pages/{page_id}", json={"properties": properties}
        )


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        parsed = resp.json()
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


# -- property builders -----------------------------------------------------
# Notion wants a differently-shaped payload per property type. These keep that
# shape knowledge in one place.


def title(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def rich_text(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def date_prop(value: date, end: date | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"start": value.isoformat()}
    if end:
        payload["end"] = end.isoformat()
    return {"date": payload}


def relation(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": pid} for pid in page_ids]}


def status_value(prop_type: str, value: str, *, done: bool) -> dict[str, Any]:
    """Build a status payload for whichever property type the user actually has.

    Weekly agendas in the wild use a checkbox, a select, or a status property
    interchangeably, so the caller passes the detected type from the schema.
    """
    if prop_type == "checkbox":
        return {"checkbox": done}
    if prop_type == "select":
        return {"select": {"name": value}}
    if prop_type == "status":
        return {"status": {"name": value}}
    raise ValueError(f"Unsupported status property type: {prop_type!r}")


# -- property readers ------------------------------------------------------


def read_title(page: dict[str, Any], prop: str) -> str:
    parts = (page.get("properties", {}).get(prop) or {}).get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def read_text(page: dict[str, Any], prop: str) -> str:
    value = page.get("properties", {}).get(prop) or {}
    parts = value.get("rich_text") or value.get("title") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def read_date(page: dict[str, Any], prop: str) -> date | None:
    value = (page.get("properties", {}).get(prop) or {}).get("date")
    if not value or not value.get("start"):
        return None
    # start may be a date or a full timestamp; the date half is all we need.
    return date.fromisoformat(value["start"][:10])


def read_status(page: dict[str, Any], prop: str) -> str | None:
    """Return a human-readable status regardless of the underlying type."""
    value = page.get("properties", {}).get(prop) or {}
    kind = value.get("type")
    if kind == "checkbox":
        return "Done" if value.get("checkbox") else "Not started"
    if kind in ("select", "status"):
        inner = value.get(kind)
        return inner.get("name") if inner else None
    return None


def is_done(page: dict[str, Any], prop: str, done_value: str) -> bool:
    value = page.get("properties", {}).get(prop) or {}
    if value.get("type") == "checkbox":
        return bool(value.get("checkbox"))
    name = read_status(page, prop)
    return name is not None and name.casefold() == done_value.casefold()


def read_relation_ids(page: dict[str, Any], prop: str) -> list[str]:
    value = (page.get("properties", {}).get(prop) or {}).get("relation") or []
    return [item["id"] for item in value if "id" in item]


def page_url(page: dict[str, Any]) -> str:
    return page.get("url", "")
