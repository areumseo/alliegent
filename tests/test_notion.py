from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from alliegent.integrations import notion as n
from alliegent.integrations.notion import API_BASE, NotionClient, NotionError

from .conftest import make_page

DB_ID = "abc123"
DS_ID = "ds-999"


@pytest.fixture
async def client():
    async with NotionClient("token", max_retries=3) as c:
        yield c


@respx.mock
async def test_resolve_data_source_maps_database_to_data_source(client):
    route = respx.get(f"{API_BASE}/databases/{DB_ID}").mock(
        return_value=httpx.Response(
            200, json={"object": "database", "data_sources": [{"id": DS_ID, "name": "Main"}]}
        )
    )
    assert await client.resolve_data_source(DB_ID) == DS_ID
    # Cached: a second call must not re-hit the API.
    assert await client.resolve_data_source(DB_ID) == DS_ID
    assert route.call_count == 1


@respx.mock
async def test_resolve_data_source_errors_when_none_present(client):
    respx.get(f"{API_BASE}/databases/{DB_ID}").mock(
        return_value=httpx.Response(200, json={"object": "database", "data_sources": []})
    )
    with pytest.raises(NotionError, match="no_data_source"):
        await client.resolve_data_source(DB_ID)


@respx.mock
async def test_pins_api_version_and_auth_header(client):
    route = respx.get(f"{API_BASE}/databases/{DB_ID}").mock(
        return_value=httpx.Response(200, json={"data_sources": [{"id": DS_ID}]})
    )
    await client.get_database(DB_ID)
    request = route.calls[0].request
    assert request.headers["Notion-Version"] == "2026-03-11"
    assert request.headers["Authorization"] == "Bearer token"


@respx.mock
async def test_query_follows_pagination_and_skips_trashed(client):
    first = {
        "results": [make_page("p1", "one"), make_page("p2", "two", in_trash=True)],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    second = {"results": [make_page("p3", "three")], "has_more": False, "next_cursor": None}
    respx.post(f"{API_BASE}/data_sources/{DS_ID}/query").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )

    titles = [n.read_title(p, "Name") async for p in client.query(DS_ID)]
    assert titles == ["one", "three"]


@respx.mock
async def test_query_sends_cursor_on_second_page(client):
    respx.post(f"{API_BASE}/data_sources/{DS_ID}/query").mock(
        side_effect=[
            httpx.Response(
                200, json={"results": [], "has_more": True, "next_cursor": "cursor-2"}
            ),
            httpx.Response(200, json={"results": [], "has_more": False}),
        ]
    )
    _ = [p async for p in client.query(DS_ID)]
    body = respx.calls[1].request.content.decode()
    assert "cursor-2" in body


@respx.mock
async def test_retries_on_429_then_succeeds(client):
    respx.post(f"{API_BASE}/data_sources/{DS_ID}/query").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "rate_limited"}),
            httpx.Response(200, json={"results": [make_page("p1", "ok")], "has_more": False}),
        ]
    )
    titles = [n.read_title(p, "Name") async for p in client.query(DS_ID)]
    assert titles == ["ok"]


@respx.mock
async def test_raises_on_404_without_retrying(client):
    route = respx.get(f"{API_BASE}/databases/{DB_ID}").mock(
        return_value=httpx.Response(
            404, json={"code": "object_not_found", "message": "Could not find database"}
        )
    )
    with pytest.raises(NotionError, match="object_not_found"):
        await client.get_database(DB_ID)
    assert route.call_count == 1


@respx.mock
async def test_create_page_uses_data_source_parent(client):
    route = respx.post(f"{API_BASE}/pages").mock(
        return_value=httpx.Response(200, json=make_page("new", "task"))
    )
    await client.create_page(DS_ID, {"Name": n.title("task")})
    payload = route.calls[0].request.content.decode()
    assert '"data_source_id"' in payload
    assert '"database_id"' not in payload


# -- property helpers ------------------------------------------------------


def test_read_date_tolerates_full_timestamps():
    page = make_page("p", "t", day="2026-08-08T09:00:00.000+09:00")
    assert n.read_date(page, "Date") == date(2026, 8, 8)


def test_read_date_returns_none_when_unset():
    assert n.read_date(make_page("p", "t"), "Date") is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"checkbox": True}, True),
        ({"checkbox": False}, False),
        ({"status": "Done"}, True),
        ({"status": "done"}, True),  # case-insensitive
        ({"status": "In progress"}, False),
        ({"status": "Done", "status_type": "select"}, True),
    ],
)
def test_is_done_across_property_types(kwargs, expected):
    page = make_page("p", "t", **kwargs)
    assert n.is_done(page, "Status", "Done") is expected


def test_status_value_builds_per_type():
    assert n.status_value("checkbox", "Done", done=True) == {"checkbox": True}
    assert n.status_value("select", "Done", done=True) == {"select": {"name": "Done"}}
    assert n.status_value("status", "Done", done=True) == {"status": {"name": "Done"}}
    with pytest.raises(ValueError):
        n.status_value("rich_text", "Done", done=True)


def test_read_title_handles_missing_property():
    assert n.read_title({"properties": {}}, "Name") == ""
