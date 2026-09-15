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
    assert "TOTAL" in text and "LIQUID" in text and "LOCKED" in text


def test_a_change_is_reported_against_the_previous_week():
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000})
    now = A.Snapshot("p1", MONDAY, {"Savings": 1200})
    text = A.snapshot_message(now, before)
    assert "+200" in text and "+20.0" in text


def test_an_unchanged_bucket_reads_as_zero_not_as_a_rise():
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000})
    now = A.Snapshot("p1", MONDAY, {"Savings": 1000})
    row = next(
        line for line in A.snapshot_message(now, before).splitlines()
        if line.startswith("Savings")
    )
    assert row.split()[-2:] == ["0", "0.0"]


def _table(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index("```") + 1
    return lines[start : lines.index("```", start)]


def test_every_table_line_is_the_same_width():
    """Alignment is the whole feature; one ragged row and it is gone."""
    before = A.Snapshot("p0", date(2026, 9, 7), {n: 99_999_999 for n in A.BUCKETS})
    now = A.Snapshot("p1", MONDAY, {n: 1 for n in A.BUCKETS})
    assert len({len(line) for line in _table(A.snapshot_message(now, before))}) == 1


def test_the_table_is_ascii_so_no_glyph_renders_double_width():
    """Arrows, box-drawing rules and ₩ are ambiguous-width and render two cells
    wide in East Asian fonts — which is what a Korean-locale client uses."""
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000, "ESPP": 50})
    now = A.Snapshot("p1", MONDAY, {"Savings": 900, "ESPP": 60})
    assert all(line.isascii() for line in _table(A.snapshot_message(now, before)))


def test_the_table_fits_a_phone_at_the_scale_it_is_used():
    """A phone wraps a code block at about 40 columns, and a wrapped table is
    worse than no table. Widths are measured, so this holds for totals in the
    hundreds of millions and weekly moves in the tens of millions."""
    before = A.Snapshot("p0", date(2026, 9, 7), {n: 60_000_000 for n in A.BUCKETS})
    now = A.Snapshot("p1", MONDAY, {n: 55_000_000 for n in A.BUCKETS})
    assert max(len(line) for line in _table(A.snapshot_message(now, before))) <= 40


def test_the_estimate_is_marked_wherever_it_is_counted():
    """ESPP is a tentative figure; a report that presents it like a bank
    balance invites reading the total as settled."""
    snap = A.Snapshot("p1", MONDAY, {"ESPP": 4500})
    text = A.snapshot_message(snap, None)
    assert "ESPP*" in text and "estimate" in text


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
    assert "Change" not in A.trend_message(one)


def test_the_trend_reports_the_move_across_the_window():
    history = [
        A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000}),
        A.Snapshot("p1", MONDAY, {"Savings": 1500}),
    ]
    text = A.trend_message(history)
    assert "+500" in text
    assert all(line.isascii() for line in _table(text))
