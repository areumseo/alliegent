"""Weekly asset snapshots: what was written down, and what changed since.

Balances are not fetched. Korean banks have no personal API worth building
on, and scraping survives neither the certificates nor the one-time codes.
So a person types the numbers once a week and the machine does the part a
person will not: compare, weigh, and show the trend.

**Locked money is in the total but shown apart from it.** A single "net
worth" figure reads as money you could spend, and a deposit or a pension is
not. Keeping the two numbers side by side is half of what this module is for.

**Expected money is not in the total at all.** ESPP still accruing and a bonus
not yet paid are real, but not held; folded into the total, every weekly
change would mix money earned with an estimate revised. They are summed on
their own line, with a total that includes them shown beneath.

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
# Vested is company stock actually owned: vested RSUs and ESPP shares once
# bought. They sit in the same brokerage account as the same shares, so one
# column, not two.
LIQUID = ("Savings", "Stock/Funds", "Vested")
# Locked: assets, but not spendable now. Still counted in the total.
LOCKED = ("Pension", "Deposit", "Mom")
# Expected: on its way, not held. Kept out of the total. Two columns rather than
# one, because they move differently -- ESPP builds up each payday, a bonus is a
# single estimate -- and a single column would hide which one changed.
EXPECTED = ("ESPP", "Bonus")

HELD = LIQUID + LOCKED
BUCKETS = HELD + EXPECTED

LABELS = {
    "Savings": "Savings",
    "Stock/Funds": "Stock/Funds",
    "Vested": "Vested",
    "Pension": "Pension",
    "Deposit": "Deposit",
    "Mom": "Mom",
    "ESPP": "ESPP (6mo)",
    "Bonus": "Bonus",
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
        """What is held now. Expected money is deliberately not in here."""
        return self.bucket(HELD)

    @property
    def expected(self) -> int:
        return self.bucket(EXPECTED)

    @property
    def total_with_expected(self) -> int:
        return self.total + self.expected


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

    async def move(
        self, today: date, source: str, target: str
    ) -> tuple[int, int, int] | None:
        """Move one bucket's whole amount into another, as of today.

        Returns (amount moved, target before, target after), or None when the
        source was empty.

        Written as today's row rather than by editing the latest one: that row
        records what was held on its own date, and rewriting it would put money
        into a day it had not arrived on and bend the trend. Every other bucket
        is carried over, because a row holding only two figures would read as
        everything else having gone to zero.
        """
        history = await self.history()
        if not history:
            return None
        base = next((row for row in history if row.day == today), history[-1])
        amount = base.amounts.get(source, 0)
        if not amount:
            return None
        before = base.amounts.get(target, 0)
        amounts = {name: base.amounts.get(name, 0) for name in BUCKETS}
        amounts[target] = before + amount
        amounts[source] = 0
        await self.record(today, amounts)
        return amount, before, before + amount

    async def roll_bonus_into_savings(self, today: date) -> tuple[int, int, int] | None:
        return await self.move(today, "Bonus", "Savings")

    async def roll_espp_into_vested(self, today: date) -> tuple[int, int, int] | None:
        return await self.move(today, "ESPP", "Vested")

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


# Fixed-width layout for Discord, which has no tables: a code block with every
# cell padded. Three constraints shape it:
#
# - ASCII only inside the block. Arrows (▲▼) and box-drawing lines (─) are
#   "ambiguous width" and render two cells wide in East Asian fonts, which is
#   exactly what a Korean-locale Discord client uses -- one arrow and every
#   column after it shifts. Signs are + and -, rules are plain hyphens.
# - No ₩ in the cells, for the same reason; the header says KRW once.
# - Column widths come from the values, not constants. Fixed widths fit the
#   figures they were tuned on and break silently on the first larger one;
#   measured widths keep every line the same length at any magnitude. At this
#   table's real scale that is under 40 columns, which a phone shows unwrapped.


def _signed(diff: int | None) -> str:
    if diff is None:
        return ""
    if diff == 0:
        return "0"
    return f"{diff:+,}"


def _pct(now: int, before: int | None) -> str:
    if before is None:
        return ""
    if not before:
        return "-"
    if now == before:
        return "0.0"
    return f"{(now - before) / before * 100:+.1f}"


def _cells(label: str, now: int, before: int | None) -> tuple[str, str, str, str]:
    diff = None if before is None else now - before
    return label, f"{now:,}", _signed(diff), _pct(now, before)


def _render(rows: list[tuple[str, str, str, str] | None]) -> list[str]:
    """Pad every column to its widest cell. None is a rule line."""
    header = ("KRW", "Amount", "Change", "%")
    cells = [header, *(r for r in rows if r is not None)]
    widths = [max(len(row[col]) for row in cells) for col in range(4)]
    # One space between columns; the name column is left-aligned, numbers right.
    total = widths[0] + sum(w + 1 for w in widths[1:])

    def line(row: tuple[str, str, str, str]) -> str:
        name, *numbers = row
        return f"{name:<{widths[0]}}" + "".join(
            f" {value:>{width}}" for value, width in zip(numbers, widths[1:], strict=True)
        )

    out = [line(header), "-" * total]
    out += ["-" * total if row is None else line(row) for row in rows]
    return out


def snapshot_message(
    current: Snapshot, previous: Snapshot | None, *, title: str = "Assets"
) -> str:
    """One aligned table: liquid buckets, their subtotal, locked buckets,
    theirs, then the total.

    The grouping is the point of the layout, not decoration -- it puts the
    money you could spend and the money you could not on either side of a
    line, which a flat list of seven buckets never showed.
    """
    when = current.day.isoformat() if current.day else ""

    def before(name: str) -> int | None:
        return previous.amounts.get(name) if previous else None

    def buckets(names: tuple[str, ...]) -> list[tuple[str, str, str, str]]:
        return [
            _cells(LABELS[name], current.amounts.get(name, 0), before(name))
            for name in names
        ]

    # Subtotals in capitals, so a sum reads as one without costing a line.
    rows: list[tuple[str, str, str, str] | None] = [
        *buckets(LIQUID),
        _cells("LIQUID", current.liquid, previous.liquid if previous else None),
        None,
        *buckets(LOCKED),
        _cells("LOCKED", current.locked, previous.locked if previous else None),
        None,
        _cells("TOTAL", current.total, previous.total if previous else None),
    ]
    if current.expected or (previous and previous.expected):
        rows += [
            None,
            *buckets(EXPECTED),
            _cells("EXPECTED", current.expected, previous.expected if previous else None),
            _cells(
                "TOTAL+EXP",
                current.total_with_expected,
                previous.total_with_expected if previous else None,
            ),
        ]

    out = [f"💰 **{title} — {when}**", "```", *_render(rows), "```"]
    notes = []
    if current.expected:
        notes.append("EXPECTED is not in TOTAL")
    if previous and previous.day:
        notes.append(f"vs {previous.day.isoformat()}")
    if notes:
        out.append("_" + " · ".join(notes) + "_")
    return "\n".join(out)


@dataclass(frozen=True)
class PendingMove:
    """A rollover that will happen before the next weekly prompt."""

    source: str
    target: str
    day: date


def moves_before(today: date, schedule, *, days: int = 7) -> list[PendingMove]:
    """Rollovers falling in the next `days`, soonest first.

    This week's figures are what such a rollover will move: `move` takes the
    row dated that day if one exists and otherwise the latest, and between two
    Mondays the latest is this Monday's. So an estimate typed in today is the
    number that lands in Savings on the first — which is exactly the moment to
    say so, rather than in a message nobody is reading on the day.

    Today itself is left out: a rollover running this morning announces
    itself, and warning about a move that has already happened is worse than
    saying nothing.
    """
    window = [today + timedelta(days=n) for n in range(1, days + 1)]
    pending: list[PendingMove] = []
    if schedule.bonus_rollover_time.strip():
        pending += [
            PendingMove("Bonus", "Savings", day)
            for day in window
            if day.month in schedule.bonus_rollover_months
            and day.day == schedule.bonus_rollover_day
        ]
    if schedule.espp_rollover_time.strip():
        for end in schedule.espp_purchase_dates:
            # The day after the period closes, as the scheduler has it.
            day = date.fromisoformat(end) + timedelta(days=1)
            if day in window:
                pending.append(PendingMove("ESPP", "Vested", day))
    return sorted(pending, key=lambda move: move.day)


def _pending_line(move: PendingMove) -> str:
    return (
        f"⚠️ **{move.source} moves into {move.target} on {move.day.isoformat()}.** "
        f"Whatever {move.source} holds then is what moves, so put the real amount "
        f"in it this week — and leave {move.target} as your bank shows it: adding "
        "it there yourself as well would count it twice."
    )


def prompt_message(
    previous: Snapshot | None,
    today: date,
    *,
    pending: list[PendingMove] | tuple[()] = (),
) -> str:
    """Monday's reminder, carrying last week's figures.

    Editing last week's numbers is faster than filling a blank form, and a
    bucket you forgot is visible as one that did not change.
    """
    out = [f"💰 **This week's balances — {today.isoformat()}**", ""]
    if previous is None:
        out.append("No records yet. Add a row to the Assets database in Notion.")
        out += [line for move in pending for line in ("", _pending_line(move))]
        return "\n".join(out)

    out.append(f"Last recorded ({previous.day.isoformat() if previous.day else '?'})")
    out.append("```")
    for name in HELD:
        out.append(f"{LABELS[name]:<12} {_won(previous.amounts.get(name, 0)):>14}")
    out.append(f"{'Total':<12} {_won(previous.total):>14}")
    for name in EXPECTED:
        out.append(f"{LABELS[name]:<12} {_won(previous.amounts.get(name, 0)):>14}")
    out.append("```")
    # Above the sign-off rather than below it: the sign-off is the line you
    # stop reading at, and this one has to be read before the numbers go in.
    if pending:
        out += ["", *(_pending_line(move) for move in pending), ""]
    out.append("_Add a row in Notion; next week this will show what changed._")
    return "\n".join(out)


def bonus_moved_message(today: date, bonus: int, before: int, after: int) -> str:
    return "\n".join(
        [
            f"💰 **Bonus moved to Savings — {today.isoformat()}**",
            "```",
            f"Bonus     {bonus:>14,}",
            f"Savings   {before:>14,}  ->  {after:,}",
            "```",
            "_Recorded as today's row. If the amount paid was different, "
            "correct Savings in Notion — and don't add the bonus to Savings "
            "again when you next enter your balances._",
        ]
    )


def espp_moved_message(today: date, amount: int, before: int, after: int) -> str:
    return "\n".join(
        [
            f"💰 **ESPP bought — moved to Vested — {today.isoformat()}**",
            "```",
            f"ESPP      {amount:>14,}",
            f"Vested    {before:>14,}  ->  {after:,}",
            "```",
            "_This moves what was paid in. The shares are usually worth more "
            "than that — the plan discount and the price lookback — so update "
            "Vested to their market value once they are in your account._",
        ]
    )


def trend_message(history: list[Snapshot], weeks: int = 8) -> str:
    """The last few weeks. One snapshot is a point; the trend is the message.

    Same layout rules as the snapshot table: ASCII, measured widths.
    """
    if not history:
        return "No records yet."
    recent = history[-weeks:]
    header = ("Week", "Total", "Liquid")
    rows = [
        (row.day.strftime("%m-%d") if row.day else "?", f"{row.total:,}", f"{row.liquid:,}")
        for row in recent
    ]
    cells = [header, *rows]
    widths = [max(len(r[c]) for r in cells) for c in range(3)]

    def line(r: tuple[str, str, str]) -> str:
        return f"{r[0]:<{widths[0]}} {r[1]:>{widths[1]}} {r[2]:>{widths[2]}}"

    out = [f"💰 **Trend — last {len(recent)}**", "```", line(header)]
    out.append("-" * (sum(widths) + 2))
    out += [line(r) for r in rows]
    if len(recent) >= 2:
        diff = recent[-1].total - recent[0].total
        out.append("-" * (sum(widths) + 2))
        out.append(f"{'Change':<{widths[0]}} {_signed(diff):>{widths[1]}}")
    out.append("```")
    return "\n".join(out)
