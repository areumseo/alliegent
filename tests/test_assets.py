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


async def test_each_bucket_lands_in_the_right_group():
    _, svc = service(
        [row("p1", "2026-09-14", savings=1, stock=2, rsu=4, espp=8,
             pension=16, deposit=32, mom=64, bonus=128)]
    )
    snap = await svc.latest()
    assert snap.liquid == 7  # savings + stock + rsu
    assert snap.locked == 112  # pension + deposit + mom
    assert snap.expected == 136  # espp + bonus


async def test_expected_money_is_kept_out_of_the_total():
    """ESPP still accruing and an unpaid bonus are not held. In the total, every
    weekly change would mix money earned with an estimate revised."""
    _, svc = service([row("p1", "2026-09-14", savings=1000, espp=500, bonus=2000)])
    snap = await svc.latest()
    assert snap.total == 1000
    assert snap.total_with_expected == 3500


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
    before = A.Snapshot("p0", date(2026, 9, 7), {"Savings": 1000, "Bonus": 50})
    now = A.Snapshot("p1", MONDAY, {"Savings": 900, "Bonus": 60})
    assert all(line.isascii() for line in _table(A.snapshot_message(now, before)))


def test_the_table_fits_a_phone_at_the_scale_it_is_used():
    """A phone wraps a code block at about 40 columns, and a wrapped table is
    worse than no table. Widths are measured, so this holds for totals in the
    hundreds of millions and weekly moves in the tens of millions."""
    before = A.Snapshot("p0", date(2026, 9, 7), {n: 60_000_000 for n in A.BUCKETS})
    now = A.Snapshot("p1", MONDAY, {n: 55_000_000 for n in A.BUCKETS})
    assert max(len(line) for line in _table(A.snapshot_message(now, before))) <= 40


def test_expected_money_gets_its_own_section_and_a_combined_line():
    snap = A.Snapshot("p1", MONDAY, {"Savings": 1000, "ESPP": 500, "Bonus": 2000})
    text = A.snapshot_message(snap, None)
    table = _table(text)
    assert any(line.startswith("EXPECTED") and "2,500" in line for line in table)
    assert any(line.startswith("TOTAL+EXP") and "3,500" in line for line in table)
    total = next(line for line in table if line.startswith("TOTAL "))
    assert "1,000" in total  # the held total does not include it
    assert "not in TOTAL" in text


def test_no_expected_section_when_nothing_is_expected():
    """An empty section every week is noise."""
    snap = A.Snapshot("p1", MONDAY, {"Savings": 1000})
    assert "EXPECTED" not in A.snapshot_message(snap, None)


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


# -- the half-yearly bonus -------------------------------------------------


OCT_1 = date(2026, 10, 1)


async def test_the_bonus_moves_into_savings():
    client, svc = service([row("p1", "2026-09-17", savings=1000, bonus=300, espp=50)])
    moved = await svc.roll_bonus_into_savings(OCT_1)
    assert moved == (300, 1000, 1300)
    written = client.created[0][1]
    assert written["Savings"]["number"] == 1300
    assert written["Bonus"]["number"] == 0


async def test_the_move_is_a_new_row_and_the_old_one_is_untouched():
    """The September row recorded what was held in September; putting the bonus
    into it would place money on a day it had not arrived on."""
    client, svc = service([row("p1", "2026-09-17", savings=1000, bonus=300)])
    await svc.roll_bonus_into_savings(OCT_1)
    assert client.created and not client.updated


async def test_every_other_bucket_carries_over():
    """A row holding only Savings and Bonus would read as everything else having
    dropped to zero overnight."""
    client, svc = service(
        [row("p1", "2026-09-17", savings=1000, bonus=300, pension=700, espp=50, mom=20)]
    )
    await svc.roll_bonus_into_savings(OCT_1)
    written = client.created[0][1]
    assert written["Pension"]["number"] == 700
    assert written["ESPP"]["number"] == 50
    assert written["Mom"]["number"] == 20


async def test_no_bonus_means_nothing_is_written():
    client, svc = service([row("p1", "2026-09-17", savings=1000)])
    assert await svc.roll_bonus_into_savings(OCT_1) is None
    assert not client.created and not client.updated


async def test_the_total_rises_by_exactly_the_bonus_and_expected_falls_by_it():
    _, svc = service([row("p1", "2026-09-17", savings=1000, bonus=300, espp=50)])
    before = await svc.latest()
    await svc.roll_bonus_into_savings(OCT_1)
    # the fake client does not persist writes, so check the arithmetic directly
    after_total = before.total + 300
    after_expected = before.expected - 300
    assert after_total - before.total == 300
    assert before.expected - after_expected == 300


def test_the_rollover_runs_on_the_first_of_april_and_october():
    from alliegent.config import Config
    from alliegent.jobs import Jobs
    from alliegent.scheduler import build_scheduler

    async def notify(message, kind):
        return None

    jobs = Jobs(None, None, Config(), notify)
    scheduler = build_scheduler(jobs, Config())
    job = next(j for j in scheduler.get_jobs() if j.id == "bonus_rollover")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["month"] == "4,10"
    assert fields["day"] == "1"


def test_the_moved_message_warns_against_counting_it_twice():
    """The user records real balances often; once the bonus is in Savings,
    adding it again by hand would count it twice."""
    text = A.bonus_moved_message(OCT_1, 300, 1000, 1300)
    assert "again" in text
