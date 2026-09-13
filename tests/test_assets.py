"""Weekly asset snapshots.

Every figure here is invented. The real ones live in Notion and must never
reach this repository, which is public.
"""

from __future__ import annotations

from datetime import date

from alliegent import assets as A
from alliegent.config import Config

from .conftest import FakeNotionClient

DS = "ds_assets-db"
MONDAY = date(2026, 9, 14)


def row(pid, day, **amounts):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": day, "type": "text"}]},
        "Date": {"type": "date", "date": {"start": day, "end": None}},
    }
    for name, value in amounts.items():
        key = {
            "stock": "Stock/Funds",
            "rsu": "RSU",
            "espp": "ESPP",
        }.get(name, name.title())
        props[key] = {"type": "number", "number": value}
    return {
        "object": "page",
        "id": pid,
        "url": f"https://notion.so/{pid}",
        "in_trash": False,
        "created_time": "2026-09-01T00:00:00.000Z",
        "properties": props,
    }


def service(pages=None):
    client = FakeNotionClient({DS: pages or []})
    return client, A.AssetService(client, Config(), "assets-db")


async def test_locked_money_is_in_the_total_but_counted_apart():
    """A single net-worth figure reads as money you could spend; a deposit is
    not. Both numbers are always shown together for that reason."""
    _, svc = service([row("p1", "2026-09-14", savings=1000, deposit=5000)])
    snap = await svc.latest()
    assert snap.liquid == 1000
    assert snap.locked == 5000
    assert snap.total == 6000


async def test_each_bucket_lands_in_the_right_half():
    _, svc = service(
        [row("p1", "2026-09-14", savings=1, stock=2, rsu=4, espp=8,
             pension=16, deposit=32, mom=64)]
    )
    snap = await svc.latest()
    assert snap.liquid == 15  # savings + stock + rsu + espp
    assert snap.locked == 112  # pension + deposit + mom


async def test_history_runs_oldest_first():
    _, svc = service(
        [row("p2", "2026-09-14", savings=2), row("p1", "2026-09-07", savings=1)]
    )
    assert [s.day for s in await svc.history()] == [date(2026, 9, 7), MONDAY]


async def test_an_undated_row_cannot_anchor_a_comparison():
    """Nothing can be measured against a snapshot with no week."""
    undated = row("p1", "2026-09-14", savings=1)
    del undated["properties"]["Date"]
    _, svc = service([undated])
    assert await svc.history() == []


async def test_recording_twice_in_a_week_corrects_rather_than_appends():
    """Fixing a figure an hour later is normal; a second row for the same week
    would turn next week's comparison into a change of zero."""
    client, svc = service([row("p1", "2026-09-14", savings=1000)])
    await svc.record(MONDAY, {"Savings": 1500})
    assert client.updated and not client.created
    assert client.updated[0][1]["Savings"]["number"] == 1500


async def test_a_new_week_is_a_new_row():
    client, svc = service([row("p1", "2026-09-07", savings=1000)])
    await svc.record(MONDAY, {"Savings": 1500})
    assert client.created and not client.updated


def test_the_snapshot_message_shows_all_three_headline_numbers():
    snap = A.Snapshot("p1", MONDAY, {"Savings": 1000, "Deposit": 5000})
    text = A.snapshot_message(snap, None)
    assert "Total" in text and "Liquid" in text and "Locked" in text


def test_a_change_is_reported_against_the_previous_week():
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000})
    now = A.Snapshot("p1", MONDAY, {"Savings": 1200})
    text = A.snapshot_message(now, before)
    assert "▲" in text and "₩200" in text


def test_an_unchanged_bucket_says_so_rather_than_showing_an_arrow():
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000})
    now = A.Snapshot("p1", MONDAY, {"Savings": 1000})
    assert "no change" in A.snapshot_message(now, before)


def test_the_estimate_is_marked_wherever_it_is_counted():
    """ESPP is a tentative figure; a report that presents it like a bank
    balance invites reading the total as settled."""
    snap = A.Snapshot("p1", MONDAY, {"ESPP": 4500})
    assert "estimate" in A.snapshot_message(snap, None)


def test_the_monday_prompt_carries_last_weeks_figures():
    """Editing last week is faster than filling a blank form, and a bucket you
    forgot shows up as one that did not change."""
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000, "Pension": 2000})
    text = A.prompt_message(before, MONDAY)
    assert "₩1,000" in text and "₩2,000" in text and "₩3,000" in text


def test_the_first_prompt_explains_where_to_write():
    text = A.prompt_message(None, MONDAY)
    assert "Notion" in text


def test_the_trend_needs_two_points_before_it_reports_a_change():
    one = [A.Snapshot("p1", MONDAY, {"Savings": 1000})]
    assert "Over the period" not in A.trend_message(one)


def test_the_trend_reports_the_move_across_the_window():
    history = [
        A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000}),
        A.Snapshot("p1", MONDAY, {"Savings": 1500}),
    ]
    text = A.trend_message(history)
    assert "▲" in text and "₩500" in text
