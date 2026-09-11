"""Karrot listings: what gets a number, and what counts as waiting.

The numbers printed by /karrot list are typed into /karrot sold and friends,
which recompute the list — so the ordering has to be deterministic and the
membership rule has to be one anyone can predict.
"""

from __future__ import annotations

from datetime import date

from alliegent import karrot as K
from alliegent.config import Config

from .conftest import FakeNotionClient

DS = "ds_karrot-db"
TODAY = date(2026, 9, 11)


def page(pid, name, *, status="Listed", paid=False, listed=None, bumped=None, price=1000):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": name, "type": "text"}]},
        "Price": {"type": "number", "number": price},
        "Status": {"type": "select", "select": {"name": status}},
        "Paid": {"type": "checkbox", "checkbox": paid},
    }
    if listed:
        props["Listed At"] = {"type": "date", "date": {"start": listed, "end": None}}
    if bumped:
        props["Bumped"] = {"type": "date", "date": {"start": bumped, "end": None}}
    return {
        "object": "page",
        "id": pid,
        "url": f"https://notion.so/{pid}",
        "in_trash": False,
        "created_time": "2026-08-01T00:00:00.000Z",
        "properties": props,
    }


def service(pages=None):
    client = FakeNotionClient({DS: pages or []})
    return client, K.KarrotService(client, Config(), "karrot-db")


async def test_a_not_listed_item_still_gets_a_number():
    """It is not on sale, but putting it up is the work — dropping it from the
    numbered list would hide the one thing left to do with it."""
    _, svc = service([page("p1", "아직 안 올린 것", status="Not listed")])
    assert [i.name for i in await svc.open_items(TODAY)] == ["아직 안 올린 것"]


async def test_a_not_listed_item_never_goes_stale():
    """Staleness means "nobody is buying it", and nothing can buy what was
    never put up. Counting it would fill the morning report with items the
    report cannot help with."""
    _, svc = service(
        [page("p1", "안 올림", status="Not listed", listed="2026-01-01")]
    )
    report = await svc.stale_report(TODAY, days=30)
    assert report["stale"] == []
    assert report["undated"] == []


async def test_a_listed_item_older_than_the_window_is_stale():
    _, svc = service([page("p1", "오래된 것", listed="2026-01-01")])
    report = await svc.stale_report(TODAY, days=30)
    assert [i.name for i in report["stale"]] == ["오래된 것"]


async def test_bumping_restarts_the_clock():
    """The whole point of bumping: the listing was shown again today."""
    _, svc = service(
        [page("p1", "끌올함", listed="2026-01-01", bumped=TODAY.isoformat())]
    )
    assert (await svc.stale_report(TODAY, days=30))["stale"] == []


async def test_selling_stamps_the_date_once():
    client, svc = service([page("p1", "팔린 것")])
    item = (await svc.open_items(TODAY))[0]
    await svc.mark_sold(item, TODAY)
    assert client.updated[0][1]["Sold At"]["date"]["start"] == TODAY.isoformat()


async def test_selling_again_leaves_the_original_date():
    """Changing the status twice must not move the sale to today."""
    sold = page("p1", "이미 팔린 것", status="Sold")
    sold["properties"]["Sold At"] = {"type": "date", "date": {"start": "2026-08-01"}}
    client, svc = service([sold])
    item = K.Item(
        id="p1", name="x", price=1, status="Sold", paid=False,
        listed_at=None, sold_at=date(2026, 8, 1), bumped=None, category=None,
    )
    await svc.mark_sold(item, TODAY)
    assert "Sold At" not in client.updated[0][1]


async def test_sold_and_paid_items_leave_the_numbered_list():
    """Read them with /karrot list Sold; there is nothing left to do to them."""
    _, svc = service([page("p1", "끝난 것", status="Sold", paid=True)])
    assert await svc.open_items(TODAY) == []


async def test_an_unpaid_sale_stays_in_the_list_but_sorts_last():
    _, svc = service(
        [
            page("p1", "안 팔린 것", listed="2026-09-01"),
            page("p2", "미입금", status="Sold", paid=False),
        ]
    )
    assert [i.name for i in await svc.open_items(TODAY)] == ["안 팔린 것", "미입금"]


async def test_an_unknown_category_is_refused_with_the_list():
    _, svc = service()
    try:
        await svc.add("x", 1000, TODAY, category="의류")
    except ValueError as exc:
        assert "Clothing" in str(exc)
    else:
        raise AssertionError("a category that is not in the database must be refused")
