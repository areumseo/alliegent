"""Rearranging a day.

Notion has no API-visible row order, so this rests on an Order number
property; every listing sorts by it, which is what keeps the numbers people
read in one message and type into another pointing at the same rows.
"""

from __future__ import annotations

from datetime import date

import pytest

from alliegent.agenda import AgendaService
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
DAY = date(2026, 8, 18)


def service(pages=None, with_order=True):
    client = FakeNotionClient({DS: pages or []})
    config = Config()
    if not with_order:
        config.agenda.props.order = ""
    return client, AgendaService(client, config, "agenda-db")


def day(*titles, orders=None):
    orders = orders or [None] * len(titles)
    return [
        make_page(f"p{i}", t, day=DAY.isoformat(), order=o)
        for i, (t, o) in enumerate(zip(titles, orders, strict=True), start=1)
    ]


async def test_a_full_reorder_renumbers_every_row():
    client, svc = service(day("a", "b", "c", orders=[1, 2, 3]))
    arranged = await svc.reorder(DAY, [3, 1, 2])
    assert [i.title for i in arranged] == ["c", "a", "b"]
    assert [(pid, props["Order"]["number"]) for pid, props in client.updated] == [
        ("p3", 1),
        ("p1", 2),
        ("p2", 3),
    ]


async def test_a_partial_sequence_moves_those_to_the_front():
    """/reorder 3 means "this one first", not "retype the whole day"."""
    _, svc = service(day("a", "b", "c", "d", orders=[1, 2, 3, 4]))
    arranged = await svc.reorder(DAY, [3])
    assert [i.title for i in arranged] == ["c", "a", "b", "d"]


async def test_the_untouched_rows_keep_their_relative_order():
    _, svc = service(day("a", "b", "c", "d", orders=[1, 2, 3, 4]))
    arranged = await svc.reorder(DAY, [4, 2])
    assert [i.title for i in arranged] == ["d", "b", "a", "c"]


async def test_rows_already_in_place_are_not_rewritten():
    """Every row gets a contiguous number, but only the ones that actually
    change are written — a reorder shouldn't touch the whole day."""
    client, svc = service(day("a", "b", "c", orders=[1, 2, 3]))
    await svc.reorder(DAY, [1])
    assert client.updated == []


async def test_unordered_rows_get_numbered_on_first_reorder():
    client, svc = service(day("a", "b", "c"))
    await svc.reorder(DAY, [2])
    assert [props["Order"]["number"] for _, props in client.updated] == [1, 2, 3]


async def test_listings_come_back_in_the_stored_order():
    _, svc = service(day("a", "b", "c", orders=[3, 1, 2]))
    assert [i.title for i in await svc.items_on(DAY)] == ["b", "c", "a"]


async def test_unordered_rows_sort_after_ordered_ones():
    """New items have no Order, and belong at the end of the day rather than
    jumping ahead of rows that were placed deliberately."""
    pages = day("placed", "new", orders=[1, None])
    _, svc = service(pages)
    assert [i.title for i in await svc.items_on(DAY)] == ["placed", "new"]


async def test_reorder_without_the_property_says_what_to_do():
    _, svc = service(day("a", "b"), with_order=False)
    with pytest.raises(RuntimeError, match="Order property"):
        await svc.reorder(DAY, [2, 1])
