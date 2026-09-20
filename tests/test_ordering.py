"""What order a day comes back in.

This is load-bearing: the numbers printed in one message are typed into
another (`/done 3`, `/change 2 at:14:00`), so if adding a task can renumber the
ones already listed, the wrong task gets ticked off. That is exactly what
happened while ordering was left to Notion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from alliegent.agenda import AgendaService
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
DAY = date(2026, 8, 18)


def service(pages=None):
    client = FakeNotionClient({DS: pages or []})
    return client, AgendaService(client, Config(), "agenda-db")


def row(page_id, title, *, at=None, created=1):
    """A page as Notion returns it: `at` folded into the date, plus a
    created_time, which is what breaks ties between untimed items."""
    start = DAY.isoformat() if at is None else f"{DAY.isoformat()}T{at}+09:00"
    page = make_page(page_id, title, day=start)
    page["created_time"] = datetime(2026, 8, 1, created, tzinfo=UTC).isoformat()
    return page


async def titles(pages):
    _, svc = service(pages)
    return [item.title for item in await svc.items_on(DAY)]


async def test_a_day_runs_in_clock_order():
    assert await titles(
        [
            row("p1", "evening", at="19:00"),
            row("p2", "morning", at="09:00"),
            row("p3", "noon", at="12:00"),
        ]
    ) == ["morning", "noon", "evening"]


async def test_untimed_items_come_after_timed_ones():
    """Chosen deliberately: an item with no time is not an item at midnight,
    and a day should open with what is actually pinned to an hour."""
    assert await titles(
        [row("p1", "whenever"), row("p2", "at nine", at="09:00")]
    ) == ["at nine", "whenever"]


async def test_untimed_items_keep_the_order_they_were_added_in():
    assert await titles(
        [
            row("p3", "third", created=3),
            row("p1", "first", created=1),
            row("p2", "second", created=2),
        ]
    ) == ["first", "second", "third"]


async def test_adding_a_task_does_not_renumber_the_others():
    """The bug this whole design exists to prevent: a new task must land at the
    end of the untimed items, not somewhere alphabetical in the middle."""
    existing = [row("p1", "Zebra task", created=1), row("p2", "Apple task", created=2)]
    before = await titles(existing)
    after = await titles([*existing, row("p3", "Banana task", created=9)])
    assert before == ["Zebra task", "Apple task"]
    assert after == ["Zebra task", "Apple task", "Banana task"]


async def test_a_time_moves_an_item_up_the_day():
    early = [row("p1", "later", created=1), row("p2", "moved", at="07:00", created=2)]
    assert await titles(early) == ["moved", "later"]


async def test_days_stay_in_date_order_across_a_range():
    _, svc = service(
        [
            make_page("p1", "tuesday", day="2026-08-18"),
            make_page("p2", "monday", day="2026-08-17T23:00+09:00"),
        ]
    )
    items = await svc.items_between(date(2026, 8, 17), date(2026, 8, 18))
    assert [i.title for i in items] == ["monday", "tuesday"]


async def test_a_time_is_read_back_off_the_page():
    _, svc = service([row("p1", "class", at="11:30")])
    assert (await svc.items_on(DAY))[0].at == time(11, 30)


async def test_a_plain_date_reads_as_no_time():
    """Notion stores "no time" as a bare date, which must not read as 00:00 --
    that would sort the item to the front of the day."""
    _, svc = service([row("p1", "someday")])
    assert (await svc.items_on(DAY))[0].at is None


async def test_adding_with_a_time_writes_a_timestamp():
    client, svc = service()
    await svc.add_item("Cafe shift", DAY, at=time(11, 0))
    _, props = client.created[0]
    assert props["Date"]["date"]["start"].startswith("2026-08-18T11:00")


async def test_adding_without_a_time_writes_a_plain_date():
    client, svc = service()
    await svc.add_item("Diary", DAY)
    _, props = client.created[0]
    assert props["Date"]["date"]["start"] == "2026-08-18"


async def test_setting_a_time_keeps_the_day():
    client, svc = service([row("p1", "task")])
    await svc.set_time("p1", DAY, time(14, 30))
    assert client.updated[0][1]["Date"]["date"]["start"].startswith("2026-08-18T14:30")


async def test_clearing_a_time_leaves_a_plain_date():
    """`/change 1 at:none` sends an item to the end of the day, not to midnight."""
    client, svc = service([row("p1", "task", at="09:00")])
    await svc.set_time("p1", DAY, None)
    assert client.updated[0][1]["Date"]["date"]["start"] == "2026-08-18"


async def test_moving_a_day_carries_the_time_along():
    client, svc = service([row("p1", "class", at="11:00")])
    target = date(2026, 8, 20)
    await svc.set_time("p1", target, time(11, 0))
    assert client.updated[0][1]["Date"]["date"]["start"].startswith("2026-08-20T11:00")


async def test_an_early_morning_item_belongs_to_its_own_day():
    """Notion compares date filters as UTC timestamps, so 06:30 on the 24th
    (21:30 UTC on the 23rd) comes back from a query for the 23rd. It is the
    24th's item, and listing it under the 23rd would number it there too."""
    pages = [
        row("p1", "today's task"),
        make_page("p2", "tomorrow at dawn", day="2026-08-19T06:30:00+09:00"),
    ]
    assert await titles(pages) == ["today's task"]


async def test_an_early_morning_item_is_not_lost_from_its_own_day():
    """The other half: 06:00 on the 18th is the 17th in UTC, so a naive filter
    for the 18th misses it -- the item simply vanishes from its day."""
    _, svc = service([make_page("p1", "dawn start", day="2026-08-18T06:00:00+09:00")])
    assert [i.title for i in await svc.items_on(DAY)] == ["dawn start"]


async def test_overdue_counts_days_not_timestamps():
    _, svc = service(
        [
            make_page("p1", "late last night", day="2026-08-17T23:30:00+09:00"),
            make_page("p2", "early today", day="2026-08-18T06:00:00+09:00"),
        ]
    )
    assert [i.title for i in await svc.overdue(DAY)] == ["late last night"]
