"""Monthly targets, held against what the asset snapshots actually say.

The Plans database is one row per month with a target for the month's end:
Total, and its Liquid and Locked halves, inside a Low-to-High range. It also
carries what the plan expects to arrive from the employer that month -- RSU
after tax, ESPP purchases, DC contributions -- and, when shares are sold, the
gain realised. The Assets database is the weekly record of what was held.

**The month's end is read on the 1st.** Its snapshot is the first one dated on
or after the first of the following month: a snapshot on 1 November is
October's close. When the 1st has none -- it usually falls on a day nobody
records anything -- the next one after it stands in, which is why the check
runs daily and reports once, on the day that snapshot exists. Linking the row
to its snapshot is what marks it done; running again finds it linked and says
nothing.

**A month's change is split by where the money came from, and the parts add
up.** Savings is the change in Savings. Auto invest is what the plan says goes
out of pay into IRP and funds by standing order -- money saved, but landing in
Pension and Stock/Funds, where it would otherwise pass for returns. Company
inflows are the plan's RSU, ESPP and DC for the month. Market is whatever else
moved in the valued holdings -- Stock/Funds, Vested, Pension -- after those.
Deposit and Mom are none of these, and get a line of their own when they move,
so the parts always sum to the change in Total. Auto invest and the inflows are
the *planned* figures: when what arrived differs, the difference lands in
Market, which the report says.

Every amount lives in Notion. Nothing here holds one, and the tests invent
theirs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from .assets import SNOW, Snapshot
from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

REVISED = "Revised"

_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True)
class Plan:
    id: str
    month: str  # YYYY-MM, the row's title
    target_total: int
    target_liquid: int
    target_locked: int
    low: int
    high: int
    rsu_net: int
    espp_buy: int
    dc: int
    auto_invest: int
    realized_gain: int
    status: str | None
    snapshot_ids: tuple[str, ...]
    # What the month's pay should leave in Savings. Read against the change in
    # Savings, it is the measure of spending that nobody has to keep a list
    # for: a dear month shows as a short one.
    cash_target: int = 0
    bonus: int = 0

    @property
    def first_day(self) -> date:
        year, month = self.month.split("-")
        return date(int(year), int(month), 1)

    @property
    def inflows(self) -> int:
        return self.rsu_net + self.espp_buy + self.dc


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def next_month(first: date) -> date:
    return date(first.year + first.month // 12, first.month % 12 + 1, 1)


def previous_month(first: date) -> date:
    return date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)


def month_end(history: list[Snapshot], first_of_next: date) -> Snapshot | None:
    """The snapshot that closes the month before `first_of_next`.

    The first one dated on or after that 1st. `history` is oldest first, so
    the first match is the nearest -- the rule the plan was written against.
    """
    return next((s for s in history if s.day and s.day >= first_of_next), None)


class PlanService:
    def __init__(self, client: NotionClient, config: Config, db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    def _to_plan(self, page: dict) -> Plan | None:
        month = n.read_title(page, "Month")
        if not _MONTH.match(month):
            # A row whose title is not a month cannot be placed, and guessing
            # which month a note belongs to is worse than leaving it out.
            log.warning("plan row %s has no YYYY-MM title; skipped", page.get("id"))
            return None

        def number(name: str) -> int:
            return int(n.read_number(page, name) or 0)

        return Plan(
            id=page["id"],
            month=month,
            target_total=number("Target Total"),
            target_liquid=number("Target Liquid"),
            target_locked=number("Target Locked"),
            low=number("Low"),
            high=number("High"),
            rsu_net=number("RSU Vest (net)"),
            espp_buy=number("ESPP Buy"),
            dc=number("DC"),
            auto_invest=number("Auto Invest"),
            realized_gain=number("Realized Gain"),
            status=n.read_select(page, "Status"),
            snapshot_ids=tuple(n.read_relation_ids(page, "Actual Snapshot")),
            cash_target=number("Cash Save Target"),
            bonus=number("Bonus"),
        )

    async def all_plans(self) -> list[Plan]:
        """Every month, earliest first."""
        ds = await self.data_source_id()
        plans = [self._to_plan(page) async for page in self._client.query(ds)]
        return sorted((p for p in plans if p is not None), key=lambda p: p.month)

    async def link(self, plan_id: str, snapshot_id: str) -> None:
        await self._client.update_page(
            plan_id, {"Actual Snapshot": n.relation([snapshot_id])}
        )

    async def mark_revised(self, plan_id: str) -> None:
        await self._client.update_page(plan_id, {"Status": n.select(REVISED)})


# -- arithmetic ------------------------------------------------------------


@dataclass(frozen=True)
class Breakdown:
    since: date
    savings: int  # cash: the change in Savings
    invested: int  # saved by standing order into IRP and funds
    inflows: int  # from the employer: RSU, ESPP, DC
    market: int
    other: int  # Deposit and Mom: neither saving nor market

    @property
    def total(self) -> int:
        return self.savings + self.invested + self.inflows + self.market + self.other


def breakdown(snapshot: Snapshot, previous: Snapshot, plan: Plan) -> Breakdown:
    def change(name: str) -> int:
        return snapshot.amounts.get(name, 0) - previous.amounts.get(name, 0)

    valued = change("Stock/Funds") + change("Vested") + change("Pension")
    assert previous.day is not None
    return Breakdown(
        since=previous.day,
        savings=change("Savings"),
        invested=plan.auto_invest,
        inflows=plan.inflows,
        market=valued - plan.inflows - plan.auto_invest,
        other=change("Deposit") + change("Mom"),
    )


def snow_share(snapshot: Snapshot) -> float | None:
    liquid = snapshot.liquid
    if liquid <= 0:
        return None
    return (snapshot.amounts.get("Vested", 0) + snapshot.amounts.get(SNOW, 0)) / liquid


def low_streak(
    plan: Plan, plans: list[Plan], history: list[Snapshot], total: int
) -> int:
    """Months in a row, ending with `plan`, that closed under their Low.

    Earlier months are read through the snapshot each is linked to, so a month
    nobody linked breaks the run rather than counting either way: no evidence
    is not evidence of being under.
    """
    by_id = {s.id: s for s in history}
    count = 0
    for row in sorted((p for p in plans if p.month <= plan.month), key=lambda p: p.month,
                      reverse=True):
        if row.month == plan.month:
            closed = total
        else:
            linked = next((by_id[i] for i in row.snapshot_ids if i in by_id), None)
            if linked is None:
                break
            closed = linked.total
        if row.low and closed < row.low:
            count += 1
        else:
            break
    return count


def gains_this_year(plan: Plan, plans: list[Plan]) -> int:
    year = plan.month[:4]
    return sum(p.realized_gain for p in plans if p.month[:4] == year and p.month <= plan.month)


# -- the message -----------------------------------------------------------


def _pct(actual: int, target: int) -> str:
    return f"{actual / target:.0%}" if target else "-"


def _range_line(total: int, low: int, high: int) -> str | None:
    if not low or high <= low:
        return None
    if total < low:
        return f"Range: under Low by ₩{low - total:,}"
    if total > high:
        return f"Range: over High by ₩{total - high:,}"
    return f"Range: {(total - low) / (high - low):.0%} of the way from Low to High"


def _cash_line(saved: int, plan: Plan) -> str | None:
    """Savings against what the month's pay should have left in it.

    Spending that changes month to month is hard to average and easy to
    guess wrong; what it leaves behind is measured already. A short month
    is flagged only when nothing else moved Savings: in a vesting month the
    tax on the RSU comes out of it, and around a bonus the rollover puts
    money in, so there the number is shown and not judged.
    """
    if not plan.cash_target:
        return None
    share = f"{saved / plan.cash_target:.0%}"
    line = f"Cash saved ₩{saved:,} of the ₩{plan.cash_target:,} target ({share})"
    distorted = plan.rsu_net or plan.bonus
    if saved < plan.cash_target and not distorted:
        return f"⚠️ {line} — spending ran ahead of plan this month."
    if distorted:
        return f"{line} · moved by the {'RSU tax' if plan.rsu_net else 'bonus'} too"
    return line


def report_message(
    plan: Plan,
    snapshot: Snapshot,
    previous: Snapshot | None,
    *,
    streak: int,
    year_gains: int,
    config: Config,
    revised: bool,
) -> str:
    from . import reports

    cfg = config.plans
    taken = snapshot.day.isoformat() if snapshot.day else "?"
    out = [
        f"🧭 **Plan check — {plan.month}** · snapshot {taken}",
        *reports._table(
            ("KRW", "Actual", "Target", "%"),
            [
                ("Liquid", f"{snapshot.liquid:,}", f"{plan.target_liquid:,}",
                 _pct(snapshot.liquid, plan.target_liquid)),
                ("Locked", f"{snapshot.locked:,}", f"{plan.target_locked:,}",
                 _pct(snapshot.locked, plan.target_locked)),
                None,
                ("Total", f"{snapshot.total:,}", f"{plan.target_total:,}",
                 _pct(snapshot.total, plan.target_total)),
            ],
            flex=0,
            right=(1, 2, 3),
        ),
    ]
    position = _range_line(snapshot.total, plan.low, plan.high)
    if position:
        out.append(position)

    out.append("")
    if previous is None:
        out.append("_No earlier month-end snapshot to compare with yet._")
    else:
        parts = breakdown(snapshot, previous, plan)
        rows: list[tuple[str, ...] | None] = [
            ("Savings", f"{parts.savings:+,}"),
            ("Auto invest", f"{parts.invested:+,}"),
            ("Company inflows", f"{parts.inflows:+,}"),
            ("Market", f"{parts.market:+,}"),
        ]
        if parts.other:
            rows.append(("Deposit/Mom", f"{parts.other:+,}"))
        rows += [None, ("Total", f"{parts.total:+,}")]
        out.append(f"**Since {parts.since.isoformat()}**")
        out += reports._table(("KRW", "Change"), rows, flex=0, right=(1,))
        cash = _cash_line(parts.savings, plan)
        if cash:
            out.append(cash)
        out.append(
            "_Auto invest and company inflows are the plan's figures for the "
            "month; whatever actually arrived differently shows up in Market._"
        )

    out.append("")
    share = snow_share(snapshot)
    if share is not None:
        over = share > cfg.snow_cap
        out.append(
            f"{'⚠️ ' if over else ''}SNOW is {share:.0%} of Liquid · cap {cfg.snow_cap:.0%}"
            + (" — sell down at the next vest." if over else "")
        )

    if streak >= cfg.low_streak:
        out.append(
            f"⚠️ Under Low {streak} months in a row. "
            + (f"{plan.month} is marked {REVISED} — " if revised else "")
            + "time to recompute the remaining targets from where things actually are."
        )
    elif streak:
        out.append(f"Under Low this month ({streak} of {cfg.low_streak} before revising).")

    if plan.rsu_net:
        out.append("📌 RSU vested this month — check that 35–40% was sold to cover the tax.")
    if plan.realized_gain:
        left = cfg.gain_allowance - year_gains
        year = plan.month[:4]
        if left >= 0:
            out.append(
                f"📌 Foreign-stock gains realised in {year}: ₩{year_gains:,} "
                f"of ₩{cfg.gain_allowance:,} · ₩{left:,} left."
            )
        else:
            out.append(
                f"⚠️ Foreign-stock gains realised in {year}: ₩{year_gains:,}, "
                f"₩{-left:,} over the ₩{cfg.gain_allowance:,} allowance — 22% applies to that."
            )
    return "\n".join(line for line in out).rstrip()


def nudge_message(month: str, today: date) -> str:
    return (
        f"🧭 **{month} is over** — record a snapshot in Assets. The plan check "
        f"goes out once there is one dated {today.isoformat()} or later."
    )


# -- the job ---------------------------------------------------------------


async def monthly_check(
    plans: PlanService,
    assets,
    today: date,
    config: Config,
    *,
    commit: bool = True,
) -> str | None:
    """The report for the month just finished, or None when there is nothing
    to say yet.

    With `commit`, links the row to its snapshot -- which is what stops the
    next day's run from reporting it again -- and marks it Revised when it has
    closed under Low for too long. Without, it only renders, and reports even a
    month already linked, which is what a preview wants.
    """
    first = today.replace(day=1)
    month = month_of(previous_month(first))
    all_plans = await plans.all_plans()
    plan = next((p for p in all_plans if p.month == month), None)
    if plan is None:
        return None
    if plan.snapshot_ids and commit:
        return None

    history = await assets.history()
    snapshot = month_end(history, first)
    if snapshot is None:
        # The 1st is when the month closes; after that, a reminder a day
        # would be nagging. The report follows on its own once one exists.
        return nudge_message(month, today) if today == first and commit else None

    previous = month_end(history, plan.first_day)
    if previous is not None and previous.day and snapshot.day and previous.day >= snapshot.day:
        previous = None

    streak = low_streak(plan, all_plans, history, snapshot.total)
    revise = streak >= config.plans.low_streak and plan.status != REVISED
    if commit:
        await plans.link(plan.id, snapshot.id)
        if revise:
            await plans.mark_revised(plan.id)

    return report_message(
        plan,
        snapshot,
        previous,
        streak=streak,
        year_gains=gains_this_year(plan, all_plans),
        config=config,
        revised=revise or plan.status == REVISED,
    )
