"""Karrot listings: what sold, for how much, and what is still waiting.

Only the item names are Korean, because they are data -- copied from a Korean
marketplace, and a listing translated into "Clothing" is one the reader cannot
search for. Everything the bot writes around them is English, like the rest of
the bot: one language for the interface, whichever language the data came in.

The logic is ported from a standalone script rather than written fresh, and
its judgement calls are kept -- they are the useful part:

- `Sold At` is stamped only on the first move into Sold, so changing the
  status twice does not push the sale date to today.
- Unpaid sales count as revenue and are flagged rather than deducted: the item
  is gone and the price is settled.
- Two statuses are off the market -- a candidate not yet listed, and an item
  already posted -- and neither is chased for sitting unsold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

# Not listed is a real state, not a missing one: the item is here, decided on,
# and not yet put up for sale. It cannot go stale -- nothing is waiting on a
# buyer -- but it is still work, so it keeps a number.
NOT_LISTED = "Not listed"
LISTED = "Listed"
RESERVED = "Reserved"
SENT = "Sent"
SOLD = "Sold"
# The order a sale actually moves through. Not listed sits before it: those are
# candidates, things being considered rather than offered.
STATUSES = (NOT_LISTED, LISTED, RESERVED, SENT, SOLD)

# Listed At and Bumped were dropped from the database on 2026-09-13: this is a
# record of what sold for how much, not of how long a listing sat. Staleness
# went with them -- it was measured from those dates and nothing else could
# stand in for them.

# Renamed from Korean on 2026-09-11, to match Status, which was always
# English. The split is by kind, not language: fixed choices are interface,
# item names are data and stay in the language they were listed in.
CATEGORIES = (
    "Games",
    "Clothing",
    "Electronics",
    "Ballet",
    "Beauty",
    "Stationery",
    "Accessories",
    "Other",
)


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    price: int | None
    status: str | None
    paid: bool
    sold_at: date | None
    category: str | None
    note: str = ""

    @property
    def needs_attention(self) -> bool:
        """Whether this item gets a number: not sold yet, or sold and unpaid.

        One number space, because these are exactly the items with something
        left to do. A sold and settled row is worth reading and never worth
        acting on, so numbering it would only compete with these.
        """
        return self.status != SOLD or not self.paid

    @property
    def won(self) -> str:
        return f"₩{self.price:,}" if self.price is not None else "no price"


class KarrotService:
    def __init__(self, client: NotionClient, config: Config, db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    def _to_item(self, page: dict) -> Item:
        return Item(
            id=page["id"],
            name=n.read_title(page, "Name") or "(untitled)",
            price=n.read_number(page, "Price"),
            status=n.read_select(page, "Status"),
            paid=n.read_checkbox(page, "Paid"),
            sold_at=n.read_date(page, "Sold At"),
            category=n.read_select(page, "Category"),
            note=n.read_text(page, "Note"),
        )

    # -- reads --------------------------------------------------------------

    async def all_items(self) -> list[Item]:
        ds = await self.data_source_id()
        return [self._to_item(page) async for page in self._client.query(ds)]

    async def open_items(self, today: date) -> list[Item]:
        """The numbered list.

        The order has to be deterministic for the same reason it does in the
        agenda: a number read in one message is typed into another, and that
        command recomputes the list rather than remembering it.
        """
        items = [i for i in await self.all_items() if i.needs_attention]
        return sorted(
            items,
            key=lambda i: (
                i.status == SOLD,  # unpaid sales last: the item is already gone
                i.status == NOT_LISTED,  # candidates after things actually on sale
                i.name,
            ),
        )

    async def by_status(self, status: str) -> list[Item]:
        if status not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        items = [i for i in await self.all_items() if i.status == status]
        return sorted(items, key=lambda i: i.name)

    # -- writes --------------------------------------------------------------

    async def add(
        self, name: str, price: int, today: date, category: str | None = None
    ) -> Item:
        """A new listing: Status becomes Listed."""
        if category and category not in CATEGORIES:
            raise ValueError(f"Category must be one of: {', '.join(CATEGORIES)}")
        props = {
            "Name": n.title(name),
            "Price": n.number(price),
            "Status": n.select(LISTED),
            "Paid": n.checkbox(False),
        }
        if category:
            props["Category"] = n.select(category)
        page = await self._client.create_page(await self.data_source_id(), props)
        return self._to_item(page)

    async def mark_sold(self, item: Item, today: date, paid: bool = False) -> None:
        props = {"Status": n.select(SOLD), "Paid": n.checkbox(paid)}
        # Stamped only on the first move into Sold, so changing the status
        # twice does not push the sale date to today.
        if item.sold_at is None:
            props["Sold At"] = n.date_prop(today)
        await self._client.update_page(item.id, props)

    async def mark_sent(self, item: Item) -> None:
        """Posted to the buyer -- not finished, waiting on delivery."""
        await self._client.update_page(item.id, {"Status": n.select(SENT)})

    async def mark_paid(self, item: Item, paid: bool = True) -> None:
        await self._client.update_page(item.id, {"Paid": n.checkbox(paid)})

    async def set_status(self, item: Item, status: str, today: date) -> None:
        if status not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        if status == SOLD:
            await self.mark_sold(item, today)
            return
        await self._client.update_page(item.id, {"Status": n.select(status)})

    # -- totals --------------------------------------------------------------

    async def unpaid(self) -> list[Item]:
        """Sold, but the money has not arrived."""
        items = await self.all_items()
        return sorted(
            (i for i in items if i.status == SOLD and not i.paid), key=lambda i: i.name
        )

    async def sales(self, today: date) -> dict:
        """Revenue by period, counting undated sales apart.

        The point is not to hide money that fell out of the periods. A sale
        with no Sold At belongs to no week and no month, and with 109 of them
        "this month ₩0" would read as a month with no sales rather than a
        month with no dates.
        """
        items = await self.all_items()
        sold = [i for i in items if i.status == SOLD]
        dated = [i for i in sold if i.sold_at]
        undated = [i for i in sold if not i.sold_at]

        week_start = today - timedelta(days=today.weekday())
        last_week = week_start - timedelta(days=7)
        month_start = today.replace(day=1)
        last_month_end = month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        def between(start: date, end: date) -> list[Item]:
            return [i for i in dated if i.sold_at and start <= i.sold_at <= end]

        def money(rows: list[Item]) -> int:
            return sum(i.price or 0 for i in rows)

        periods = {
            "this_week": between(week_start, today),
            "last_week": between(last_week, week_start - timedelta(days=1)),
            "this_month": between(month_start, today),
            "last_month": between(last_month_start, last_month_end),
            "this_year": between(today.replace(month=1, day=1), today),
        }
        # Unpaid sales are already in `sold`. Flagged, not deducted: the item
        # is gone and the price is agreed, so the sale happened.
        unpaid = [i for i in sold if not i.paid]
        return {
            "week_start": week_start,
            "periods": {name: (len(rows), money(rows)) for name, rows in periods.items()},
            "total": (len(sold), money(sold)),
            "undated": (len(undated), money(undated)),
            "unpaid": (len(unpaid), money(unpaid)),
        }

    async def summary(self, today: date) -> dict:
        items = await self.all_items()
        sold = [i for i in items if i.status == SOLD]
        listed = [i for i in items if i.status == LISTED]
        reserved = [i for i in items if i.status == RESERVED]
        sent = [i for i in items if i.status == SENT]
        waiting = [i for i in items if i.status == NOT_LISTED]
        unpaid = [i for i in sold if not i.paid]
        month_start = today.replace(day=1)
        this_month = [i for i in sold if i.sold_at and i.sold_at >= month_start]

        def total(rows: list[Item]) -> int:
            return sum(i.price or 0 for i in rows)

        return {
            "total": len(items),
            "sold": len(sold),
            "sold_amount": total(sold),
            "listed": len(listed),
            "listed_amount": total(listed),
            "reserved": len(reserved),
            "sent": len(sent),
            "sent_amount": total(sent),
            "not_listed": len(waiting),
            "not_listed_amount": total(waiting),
            "unpaid": len(unpaid),
            "unpaid_amount": total(unpaid),
            "month": len(this_month),
            "month_amount": total(this_month),
        }


# -- messages --------------------------------------------------------------
#
# English, like everything else the bot writes. The item names inside these
# messages are Korean because they are data, quoted from the marketplace.


def _line(index: int, item: Item) -> str:
    bits = [item.won]
    if item.status == NOT_LISTED:
        bits.append("candidate")
    if item.status == RESERVED:
        bits.append("reserved")
    if item.status == SENT:
        bits.append("sent")
    if item.status == SOLD and not item.paid:
        bits.append("⚠️ unpaid")
    if item.note:
        # The note is context the user wrote for themselves; hiding it sends
        # them back to Notion to find out why an item is flagged.
        note = item.note if len(item.note) <= 40 else item.note[:39] + "…"
        bits.append(note)
    return f"`{index}.` {item.name} — {' · '.join(bits)}"


def item_list(items: list[Item], today: date, *, title: str = "Open") -> str:
    if not items:
        return "Nothing to handle."
    lines = [f"🥕 **{title} ({len(items)})**"]
    lines += [_line(i, item) for i, item in enumerate(items, start=1)]
    lines.append("_`/karrot sold <n>` · `/karrot sent <n>` · `/karrot paid <n>`_")
    return "\n".join(lines)


def read_only_list(items: list[Item], today: date, *, title: str) -> str:
    """번호 없는 목록. 손댈 수 없는 것에 번호를 붙이면 다른 목록의 번호와
    헷갈리기만 한다."""
    if not items:
        return f"{title}: 해당하는 매물이 없습니다."
    lines = [f"🥕 **{title} ({len(items)})**"]
    for item in items:
        lines.append(f"• {item.name} — {item.won}")
    return "\n".join(lines)


def weekly_message(data: dict, unpaid: list[Item], today: date) -> str | None:
    """Saturday's week. None when nothing sold and nothing is owed.

    A weekly report that reads 0 every week is one you stop opening. Money
    owed still speaks up, because nothing sold is not nothing to do.
    """
    count, amount = data["periods"]["this_week"]
    if not count and not unpaid:
        return None

    start = data["week_start"]
    end = start + timedelta(days=6)
    out = [
        f"🥕 **This week — {start.month}/{start.day}–{end.month}/{end.day}**",
        "",
        f"This week   {count} sold · ₩{amount:,}",
    ]
    last_count, last_amount = data["periods"]["last_week"]
    if last_count or last_amount:
        diff = amount - last_amount
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")
        out.append(
            f"Last week   {last_count} sold · ₩{last_amount:,}"
            f"  ({arrow} ₩{abs(diff):,})"
        )
    month_count, month_amount = data["periods"]["this_month"]
    out.append(f"This month  {month_count} sold · ₩{month_amount:,}")
    year_count, year_amount = data["periods"]["this_year"]
    out.append(f"This year   {year_count} sold · ₩{year_amount:,}")
    total_count, total_amount = data["total"]
    out.append(f"All time    {total_count} sold · ₩{total_amount:,}")

    undated_count, undated_amount = data["undated"]
    if undated_count:
        out.append(
            f"_{undated_count} sale(s) · ₩{undated_amount:,} have no date and fall "
            "into no period_"
        )
    if unpaid:
        owed = sum(i.price or 0 for i in unpaid)
        out += ["", f"**Unpaid {len(unpaid)} · ₩{owed:,}**"]
        out += [f"• {i.name} — {i.won}" for i in unpaid]
    return "\n".join(out)


def sales_message(data: dict, today: date) -> str:
    """Revenue by period, naming the money that fits in none of them."""

    def line(label: str, pair: tuple[int, int]) -> str:
        count, amount = pair
        return f"{label:<11} {count:>3} · ₩{amount:,}"

    week_end = data["week_start"] + timedelta(days=6)
    p = data["periods"]
    out = [
        f"🥕 **Sales — {today.isoformat()}**",
        "```",
        line("This week", p["this_week"]),
        line("Last week", p["last_week"]),
        line("This month", p["this_month"]),
        line("Last month", p["last_month"]),
        line("This year", p["this_year"]),
        line("All time", data["total"]),
        "```",
        f"_This week: {data['week_start'].month}/{data['week_start'].day}"
        f"–{week_end.month}/{week_end.day}_",
    ]
    undated_count, undated_amount = data["undated"]
    if undated_count:
        out.append(
            f"⚠️ {undated_count} sale(s) · ₩{undated_amount:,} have no date, so they "
            "are in the total but in no period"
        )
    unpaid_count, unpaid_amount = data["unpaid"]
    if unpaid_count:
        out.append(f"⚠️ Of these, {unpaid_count} unpaid · ₩{unpaid_amount:,}")
    return "\n".join(out)


def candidates_message(items: list[Item]) -> str | None:
    """Monday's candidates. None when there are none.

    An item nobody has put up cannot sell, and the start of a week is when
    that is worth knowing.
    """
    waiting = [i for i in items if i.status == NOT_LISTED]
    if not waiting:
        return None
    total = sum(i.price or 0 for i in waiting)
    out = [f"🥕 **To list this week — {len(waiting)} · ₩{total:,}**", ""]
    out += [f"• {i.name} — {i.won}" for i in waiting]
    out += ["", "_Set them to Listed in Notion once they are up._"]
    return "\n".join(out)


def summary_message(data: dict, today: date) -> str:
    out = [
        f"🥕 **Summary — {today.isoformat()}**",
        "",
        f"{data['total']} items",
        f"• Sold {data['sold']} · ₩{data['sold_amount']:,}",
        f"• Listed {data['listed']} · ₩{data['listed_amount']:,}"
        + (f" (reserved {data['reserved']})" if data["reserved"] else ""),
    ]
    if data["sent"]:
        out.append(f"• Sent {data['sent']} · ₩{data['sent_amount']:,}")
    if data["not_listed"]:
        out.append(f"• Candidates {data['not_listed']} · ₩{data['not_listed_amount']:,}")
    out.append(f"• This month {data['month']} · ₩{data['month_amount']:,}")
    out.append(f"• Unpaid {data['unpaid']} · ₩{data['unpaid_amount']:,}")
    return "\n".join(out)
