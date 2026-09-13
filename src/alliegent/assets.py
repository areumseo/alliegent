"""Weekly asset snapshots: what was written down, and what changed since.

Balances are not fetched. Korean banks have no personal API worth building
on, and scraping survives neither the certificates nor the one-time codes.
So a person types the numbers once a week and the machine does the part a
person will not: compare, weigh, and show the trend.

**Locked money is in the total but shown apart from it.** A single "net
worth" figure reads as money you could spend, and a deposit or a pension is
not. Keeping the two numbers side by side is half of what this module is for.

The amounts live in Notion, never in this repository -- every example and
fixture here is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

# Liquid: could be cash within days.
LIQUID = ("Savings", "Stock/Funds", "RSU", "ESPP")
# Locked: assets, but not spendable now. Still counted in the total.
LOCKED = ("Pension", "Deposit", "Mom")
BUCKETS = LIQUID + LOCKED

# An estimate. Counted, but marked as one wherever it is shown.
TENTATIVE = ("ESPP",)

LABELS = {
    "Savings": "Savings",
    "Stock/Funds": "Stock/Funds",
    "RSU": "RSU",
    "ESPP": "ESPP",
    "Pension": "Pension",
    "Deposit": "Deposit",
    "Mom": "Mom",
}


@dataclass(frozen=True)
class Snapshot:
    id: str
    day: date | None
    amounts: dict[str, int]
    note: str = ""

    def bucket(self, names: tuple[str, ...]) -> int:
        return sum(self.amounts.get(name, 0) for name in names)

    @property
    def liquid(self) -> int:
        return self.bucket(LIQUID)

    @property
    def locked(self) -> int:
        return self.bucket(LOCKED)

    @property
    def total(self) -> int:
        return self.bucket(BUCKETS)


class AssetService:
    def __init__(self, client: NotionClient, config: Config, db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    def _to_snapshot(self, page: dict) -> Snapshot:
        return Snapshot(
            id=page["id"],
            day=n.read_date(page, "Date"),
            amounts={
                name: int(n.read_number(page, name) or 0) for name in BUCKETS
            },
            note=n.read_text(page, "Note"),
        )

    async def history(self) -> list[Snapshot]:
        """Oldest first. Undated rows are dropped: nothing can be compared to them."""
        ds = await self.data_source_id()
        rows = [self._to_snapshot(page) async for page in self._client.query(ds)]
        return sorted((r for r in rows if r.day), key=lambda r: r.day or date.min)

    async def latest(self) -> Snapshot | None:
        rows = await self.history()
        return rows[-1] if rows else None

    async def record(self, day: date, amounts: dict[str, int]) -> Snapshot:
        """Write one week's snapshot, replacing any row already on that date.

        Replacing rather than appending, because correcting a figure an hour
        later is normal -- and a second row for the same week turns next
        week's comparison into a change of zero.
        """
        props: dict = {
            "Name": n.title(day.isoformat()),
            "Date": n.date_prop(day),
        }
        for name in BUCKETS:
            if name in amounts:
                props[name] = n.number(int(amounts[name]))

        existing = next((r for r in await self.history() if r.day == day), None)
        if existing:
            await self._client.update_page(existing.id, props)
            merged = {**existing.amounts, **amounts}
            return Snapshot(existing.id, day, merged, existing.note)
        page = await self._client.create_page(await self.data_source_id(), props)
        return self._to_snapshot(page)

    def monday_of(self, today: date) -> date:
        return today - timedelta(days=today.weekday())


# -- messages ----------------------------------------------------------------


def _won(amount: int) -> str:
    return f"₩{amount:,}"


def _delta(now: int, before: int | None) -> str:
    if before is None:
        return ""
    diff = now - before
    if diff == 0:
        return "  (no change)"
    arrow = "▲" if diff > 0 else "▼"
    share = f" {abs(diff) / before * 100:.1f}%" if before else ""
    return f"  ({arrow} {_won(abs(diff))}{share})"


def snapshot_message(
    current: Snapshot, previous: Snapshot | None, *, title: str = "Assets"
) -> str:
    """Total, liquid and locked together: any one of them alone misleads."""
    when = current.day.isoformat() if current.day else ""

    def headline(label: str, now: int, before: int | None) -> str:
        return f"{label:<8} {_won(now):>14}{_delta(now, before)}"

    out = [
        f"💰 **{title} — {when}**",
        "```",
        headline("Total", current.total, previous.total if previous else None),
        headline("Liquid", current.liquid, previous.liquid if previous else None),
        headline("Locked", current.locked, previous.locked if previous else None),
        "```",
    ]

    lines = []
    for name in BUCKETS:
        amount = current.amounts.get(name, 0)
        if not amount:
            continue
        before = previous.amounts.get(name) if previous else None
        mark = " *" if name in TENTATIVE else ""
        lines.append(f"• {LABELS[name]}{mark} {_won(amount)}{_delta(amount, before)}")
    if lines:
        out += ["**By bucket**", *lines]
    if any(current.amounts.get(name) for name in TENTATIVE):
        out.append("_* estimate_")
    if previous and previous.day:
        out.append(f"_previous: {previous.day.isoformat()}_")
    return "\n".join(out)


def prompt_message(previous: Snapshot | None, today: date) -> str:
    """Monday's reminder, carrying last week's figures.

    Editing last week's numbers is faster than filling a blank form, and a
    bucket you forgot is visible as one that did not change.
    """
    out = [f"💰 **This week's balances — {today.isoformat()}**", ""]
    if previous is None:
        out.append("No records yet. Add a row to the Assets database in Notion.")
        return "\n".join(out)

    out.append(f"Last recorded ({previous.day.isoformat() if previous.day else '?'})")
    out.append("```")
    for name in BUCKETS:
        out.append(f"{LABELS[name]:<12} {_won(previous.amounts.get(name, 0)):>14}")
    out.append(f"{'Total':<12} {_won(previous.total):>14}")
    out.append("```")
    out.append("_Add a row in Notion; next week this will show what changed._")
    return "\n".join(out)


def trend_message(history: list[Snapshot], weeks: int = 8) -> str:
    """The last few weeks. One snapshot is a point; the trend is the message."""
    if not history:
        return "No records yet."
    recent = history[-weeks:]
    out = [f"💰 **Trend — last {len(recent)}**", "```"]
    for row in recent:
        when = row.day.isoformat() if row.day else "?"
        out.append(
            f"{when}  total {_won(row.total):>14}  liquid {_won(row.liquid):>14}"
        )
    out.append("```")
    if len(recent) >= 2:
        diff = recent[-1].total - recent[0].total
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")
        out.append(f"Over the period  {arrow} {_won(abs(diff))}")
    return "\n".join(out)
