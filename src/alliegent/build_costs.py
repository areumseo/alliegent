"""What it costs to build and run the services: one row per tool.

Two sources, because the bills behave differently. A subscription is a fixed
figure that changes a few times a year, so it is typed into the Build Tools
database once and the bot does the arithmetic. Usage billed by the token moves
every day, so where the provider has a report API the bot reads it live
(`integrations/anthropic_usage.py`) rather than trust a number typed last week.

Everything is shown per month in won. A yearly plan is a twelfth of its price,
and each foreign currency is converted at the day's rate, never stored -- a
stored rate is wrong the day after.

**The live figure is kept out of the fixed total.** A month still running has
only part of its bill, and folded into a monthly total it would make the total
climb all month and then reset. It is shown beneath, with last month's full
figure, and the total "with the API" uses the last month that is complete.

The amounts live in Notion, never in this repository -- every example and
fixture here is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from . import reports
from .config import Config
from .integrations import notion as n
from .integrations.anthropic_usage import AnthropicUsage, UsageError, month_bounds
from .integrations.fx import FxError, Rates
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

HOME = "KRW"
# How far ahead a renewal is worth a mention: long enough to cancel in time.
RENEWAL_DAYS = 14
# A tool can be billed more than one way: a plan fee (Monthly or Yearly) with
# usage on top. Cost is always the plan fee; the usage on top is a typical
# month's, typed in Usage (mo). A tool billed by Usage alone has no plan, and
# its Cost is read as that monthly estimate, as it was before the two parted.
PLANS = ("Monthly", "Yearly")


@dataclass(frozen=True)
class Tool:
    name: str
    currency: str
    cost: Decimal | None
    billing: tuple[str, ...] = ("Monthly",)
    renews: date | None = None
    status: str = "Active"
    note: str = ""
    # Projects it is for. Empty means shared: the Claude plan or the editor
    # serve everything, and pinning them to one project would be wrong.
    project_ids: tuple[str, ...] = ()
    usage: Decimal | None = None
    id: str = ""

    @property
    def metered(self) -> bool:
        return "Usage" in self.billing

    @property
    def has_plan(self) -> bool:
        return any(b in PLANS for b in self.billing)

    @property
    def counts(self) -> bool:
        """Cancelled stays in the database as history and leaves the sums; a
        trial costs nothing until it converts."""
        return self.status == "Active"

    def plan_monthly(self) -> Decimal | None:
        """The plan fee a month: a yearly one is a twelfth."""
        if not self.has_plan or self.cost is None:
            return None
        return self.cost / (12 if "Yearly" in self.billing else 1)

    def usage_monthly(self) -> Decimal | None:
        if not self.metered:
            return None
        if self.usage is not None:
            return self.usage
        return None if self.has_plan else self.cost

    def monthly(self) -> Decimal | None:
        """Plan plus usage; None only when neither has a figure."""
        parts = [p for p in (self.plan_monthly(), self.usage_monthly()) if p is not None]
        return sum(parts, Decimal(0)) if parts else None

    @property
    def per(self) -> str:
        plan = "yr" if "Yearly" in self.billing else "mo"
        if not self.has_plan:
            return "use"
        return f"{plan}+use" if self.metered else plan

    @property
    def shown_cost(self) -> str:
        """What was typed, in the tool's own currency: the plan fee, with the
        usual usage after a plus."""
        fee = self.cost if self.has_plan else self.usage_monthly()
        text = _amount(fee) if fee is not None else "?"
        if self.has_plan and self.metered:
            text += f"+{_amount(self.usage)}" if self.usage is not None else "+?"
        return text


@dataclass(frozen=True)
class Actual:
    """A finished month in won: what was really billed where it was recorded,
    the estimate where it was not, and who is missing a record."""

    total: int
    fixed: int  # plan fees, which are known without a record
    recorded: int
    estimated: int
    missing: tuple[str, ...]


@dataclass(frozen=True)
class Api:
    """The live usage figure, in dollars."""

    this: Decimal
    last: Decimal | None
    closed: bool = False  # `this` is a finished month rather than one in progress


class ToolService:
    def __init__(
        self, client: NotionClient, config: Config, db_id: str, spend_db_id: str = ""
    ) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None
        # Optional: with no Build Spend database a month is only ever estimated.
        self._spend_db_id = spend_db_id
        self._spend_ds: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    @staticmethod
    def _to_tool(page: dict) -> Tool:
        cost = n.read_number(page, "Cost")
        usage = n.read_number(page, "Usage (mo)")
        return Tool(
            name=n.read_title(page, "Name"),
            currency=(n.read_select(page, "Currency") or HOME).upper(),
            cost=None if cost is None else Decimal(str(cost)),
            billing=tuple(n.read_multi_select(page, "Billing")) or ("Monthly",),
            usage=None if usage is None else Decimal(str(usage)),
            renews=n.read_date(page, "Renews"),
            status=n.read_select(page, "Status") or "Active",
            note=n.read_text(page, "Note"),
            project_ids=tuple(n.read_relation_ids(page, "Projects")),
            id=page.get("id", ""),
        )

    async def spend_in(self, first: date) -> dict[str, Decimal] | None:
        """Usage charges actually recorded for the month beginning `first`, by
        tool id and in the tool's own currency. None when there is no Build
        Spend database, which is not the same as a month with nothing in it.

        A month is titled YYYY-MM, as the plan rows are. Several rows for one
        tool in a month add up, since a second invoice is a second row, not a
        correction; a title that is not a month is skipped rather than guessed.
        """
        if not self._spend_db_id:
            return None
        if self._spend_ds is None:
            self._spend_ds = await self._client.resolve_data_source(self._spend_db_id)
        wanted = first.strftime("%Y-%m")
        out: dict[str, Decimal] = {}
        async for page in self._client.query(self._spend_ds):
            if n.read_title(page, "Month").strip()[:7] != wanted:
                continue
            amount = n.read_number(page, "Amount")
            if amount is None:
                continue
            for tool_id in n.read_relation_ids(page, "Tool"):
                out[tool_id] = out.get(tool_id, Decimal(0)) + Decimal(str(amount))
        return out

    async def tools(self) -> list[Tool]:
        ds = await self.data_source_id()
        rows = [self._to_tool(page) async for page in self._client.query(ds)]
        return sorted((r for r in rows if r.name), key=lambda t: (not t.counts, t.name))


# -- layout ------------------------------------------------------------------


def _amount(value: Decimal) -> str:
    return f"{value:,.0f}" if value == value.to_integral() else f"{value:,.2f}"


def _won(tool: Tool, rates: dict[str, float]) -> int | None:
    monthly = tool.monthly()
    if monthly is None or tool.currency not in rates:
        return None
    return round(float(monthly) * rates[tool.currency])


def actual_for(
    tools: list[Tool], spend: dict[str, Decimal], rates: dict[str, float]
) -> Actual:
    """The finished month: plan fees as known, each usage tool at what was
    recorded or, lacking a record, at its estimate -- and named as missing, so
    an estimate cannot pass for an invoice."""
    fixed = recorded = estimated = 0
    missing: list[str] = []
    for t in tools:
        if not t.counts or t.currency not in rates:
            continue
        rate = rates[t.currency]
        plan = t.plan_monthly()
        if plan is not None:
            fixed += round(float(plan) * rate)
        if not t.metered:
            continue
        if t.id in spend:
            recorded += round(float(spend[t.id]) * rate)
            continue
        missing.append(t.name)
        guess = t.usage_monthly()
        if guess is not None:
            estimated += round(float(guess) * rate)
    return Actual(fixed + recorded + estimated, fixed, recorded, estimated, tuple(missing))


SHARED = "Shared"


def by_project(
    tools: list[Tool], rates: dict[str, float], names: dict[str, str]
) -> list[tuple[str, int]]:
    """Fixed won a month per project, largest first, Shared last.

    A tool for several projects is split equally between them -- the one rule
    that needs no weights to keep up to date, and that adds back to the fixed
    total exactly: the odd won goes to the first projects rather than being
    rounded away. A tool with no project is Shared, not spread across all of
    them, because who it is for is the thing the column records.
    """
    totals: dict[str, int] = {}
    for t in tools:
        won = _won(t, rates) if t.counts else None
        if won is None:
            continue
        if not t.project_ids:
            totals[SHARED] = totals.get(SHARED, 0) + won
            continue
        ids = sorted(t.project_ids, key=lambda i: names.get(i, i))
        share, extra = divmod(won, len(ids))
        for index, pid in enumerate(ids):
            label = names.get(pid, "(removed project)")
            totals[label] = totals.get(label, 0) + share + (1 if index < extra else 0)
    shared = totals.pop(SHARED, None)
    out = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    if shared is not None:
        out.append((SHARED, shared))
    return out


def _soon(tools: list[Tool], today: date) -> list[str]:
    horizon = today + timedelta(days=RENEWAL_DAYS)
    due = sorted(
        (t for t in tools if t.status != "Cancelled" and t.renews and today <= t.renews <= horizon),
        key=lambda t: t.renews or today,
    )
    return [
        f"{t.name} {'ends' if t.status == 'Trial' else 'renews'} {reports.fmt_date(t.renews)}"
        for t in due
        if t.renews
    ]


def _rate_lines(
    tools: list[Tool], rates: dict[str, float], rate_day: date | None, api: Api | None
) -> str | None:
    foreign = {t.currency for t in tools if t.counts and t.cost is not None} - {HOME}
    if api is not None:
        foreign.add("USD")
    quoted = []
    for currency in sorted(foreign):
        if currency in rates:
            per = 100 if rates[currency] < 100 else 1
            quoted.append(f"{currency} {per:,} = {HOME} {rates[currency] * per:,.1f}")
    if not quoted:
        return None
    when = f" (ECB, {rate_day.isoformat()})" if rate_day else ""
    return " · ".join(quoted) + when


def costs_message(
    tools: list[Tool],
    rates: dict[str, float] | None,
    rate_day: date | None,
    today: date,
    *,
    api: Api | None = None,
    api_problem: str | None = None,
    rate_problem: str | None = None,
    title: str = "Build costs",
    api_labels: tuple[str, str] = ("this month so far", "last month"),
    names: dict[str, str] | None = None,
    actual: Actual | None = None,
    actual_label: str = "",
) -> str:
    rates = rates or {}
    if not tools and api is None:
        return f"🛠 **{title}**\nNo tools recorded yet."
    rows: list[tuple[str, ...] | None] = []
    total = 0
    counted = False
    for t in tools:
        won = _won(t, rates) if t.counts else None
        if won is not None:
            total += won
            counted = True
        if t.status == "Cancelled":
            continue
        if not t.counts:  # a trial: named, costing nothing yet
            rows.append((t.name, t.currency, "trial", "-", "-"))
            continue
        rows.append((
            t.name,
            t.currency,
            t.shown_cost,
            t.per,
            f"{won:,}" if won is not None else "?",
        ))
    if counted:
        rows += [None, ("FIXED / month", "", "", "", f"{total:,}")]

    lines = [f"🛠 **{title}** (monthly)"]
    lines += reports._table(
        ("Tool", "Cur", "Cost", "Per", "KRW/mo"), rows, flex=0, right=(2, 4)
    )

    # Only with titles to show: without a projects database the split would be
    # a table of "(removed project)".
    split = by_project(tools, rates, names) if names and any(t.project_ids for t in tools) else []
    if split:
        lines.append("**By project** (fixed, KRW/mo)")
        lines += reports._table(
            ("Project", "KRW/mo"),
            [*(((name, f"{won:,}")) for name, won in split), None,
             ("TOTAL", f"{sum(won for _, won in split):,}")],
            flex=0, right=(1,),
        )

    if actual is not None and counted:
        detail = f"plan ₩{actual.fixed:,}, usage ₩{actual.recorded:,} recorded"
        if actual.estimated:
            detail += f" + ₩{actual.estimated:,} estimated"
        lines.append(f"Actual {actual_label}: ₩{actual.total:,} ({detail})")
        diff = actual.total - total
        lines.append(
            f"The estimate was ₩{total:,}: "
            + ("on it" if diff == 0 else f"{'+' if diff > 0 else '-'}₩{abs(diff):,}")
        )
        if actual.missing:
            lines.append(
                f"No record for {actual_label}: " + ", ".join(actual.missing)
            )

    usd = rates.get("USD")
    if api is not None and usd:
        this_label, last_label = api_labels
        parts = [f"${api.this:,.2f} {this_label} (₩{round(float(api.this) * usd):,})"]
        if api.last is not None:
            parts.append(f"${api.last:,.2f} {last_label} (₩{round(float(api.last) * usd):,})")
        lines.append("Anthropic API: " + " · ".join(parts))
        if split:
            lines.append("_The API is not split by project._")
        full = api.this if api.closed else api.last
        if full is not None and counted:
            with_api = total + round(float(full) * usd)
            lines.append(f"Fixed plus the last full month of API: ₩{with_api:,} a month")
    if counted:
        lines.append(f"Fixed over a year: ₩{total * 12:,}")
    shown = _rate_lines(tools, rates, rate_day, api)
    if shown:
        lines.append(shown)
    if rate_problem:
        lines.append(f"⚠️ No exchange rate, so no won figures: {rate_problem}")
    if api_problem:
        lines.append(f"⚠️ Anthropic usage unavailable: {api_problem}")
    unpriced = [t.name for t in tools if t.counts and t.monthly() is None]
    if unpriced:
        lines.append("Still to price: " + ", ".join(unpriced))
    # A plan with usage on top and no estimate of the usage: counted at the plan
    # fee alone, which understates it, so it is said.
    unestimated = [
        t.name for t in tools if t.counts and t.metered and t.has_plan and t.usage is None
    ]
    if unestimated:
        lines.append("Usage not estimated, counted at the plan fee: " + ", ".join(unestimated))
    soon = _soon(tools, today)
    if soon:
        lines.append("⏰ " + " · ".join(soon))
    return "\n".join(lines)


def cost_line(
    tools: list[Tool],
    rates: dict[str, float] | None,
    today: date,
    *,
    api: Api | None = None,
) -> str | None:
    """One line for the weekly summary: the fixed total, the live figure, and
    anything about to renew."""
    rates = rates or {}
    won = [w for t in tools if t.counts and (w := _won(t, rates)) is not None]
    parts = []
    if won:
        parts.append(f"₩{sum(won):,}/mo fixed")
    if api is not None and rates.get("USD"):
        parts.append(f"API ₩{round(float(api.this) * rates['USD']):,} this month so far")
    soon = _soon(tools, today)
    if not parts and not soon:
        return None
    return "🛠 Build costs: " + " · ".join([*parts, *(f"⏰ {s}" for s in soon)])


# -- gathering ---------------------------------------------------------------


async def _api(admin_key: str, today: date, *, closed: bool) -> tuple[Api | None, str | None]:
    """The live figure, or why there is none. Never raises: a cost report that
    is down should cost the message its API line, not the whole message."""
    if not admin_key:
        return None, None
    first, nxt, prev = month_bounds(today)
    # Closed: the month that just ended against the one before it. Open: this
    # month so far against the last full one.
    spans = (
        ((prev, first), (month_bounds(prev)[2], prev))
        if closed
        else ((first, today + timedelta(days=1)), (prev, first))
    )
    try:
        usage = AnthropicUsage(admin_key)
        this = await usage.cost(*spans[0])
        last = await usage.cost(*spans[1])
    except UsageError as exc:
        log.warning("Anthropic usage: %s", exc)
        return None, str(exc)
    return Api(this, last, closed=closed), None


async def _rates(tools: list[Tool], with_usd: bool) -> tuple[dict | None, date | None, str | None]:
    wanted = {t.currency for t in tools if t.counts and t.cost is not None}
    if with_usd:
        wanted.add("USD")
    try:
        rates, day = await Rates().to_won(wanted)
    except FxError as exc:
        log.warning("exchange rates: %s", exc)
        return None, None, str(exc)
    return rates, day, None


async def build_costs(
    service: ToolService,
    admin_key: str,
    today: date,
    *,
    settle: bool = False,
    names: dict[str, str] | None = None,
) -> str:
    """The costs table: now for /build costs, or the month just ended for the
    settlement on the 1st."""
    tools = await service.tools()
    api, api_problem = await _api(admin_key, today, closed=settle)
    rates, day, rate_problem = await _rates(tools, with_usd=bool(admin_key))
    if settle:
        ended = month_bounds(today)[2]
        spend = await service.spend_in(ended)
        actual = (
            actual_for(tools, spend, rates) if spend is not None and rates else None
        )
        return costs_message(
            tools, rates, day, today, api=api, api_problem=api_problem,
            rate_problem=rate_problem,
            title=f"Build costs — {ended.strftime('%B %Y')}",
            api_labels=(ended.strftime("%B"), month_bounds(ended)[2].strftime("%B")),
            names=names,
            actual=actual,
            actual_label=ended.strftime("%B"),
        )
    return costs_message(
        tools, rates, day, today, api=api, api_problem=api_problem,
        rate_problem=rate_problem, names=names,
    )


async def weekly_line(service: ToolService, admin_key: str, today: date) -> str | None:
    """The line under the weekly summary; None, with a log line, on any failure
    so the summary still goes out."""
    try:
        tools = await service.tools()
        api, _ = await _api(admin_key, today, closed=False)
        rates, _, _ = await _rates(tools, with_usd=api is not None)
        return cost_line(tools, rates, today, api=api)
    except Exception:
        log.exception("could not add costs to the weekly summary")
        return None
