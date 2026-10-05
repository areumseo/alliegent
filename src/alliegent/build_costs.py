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
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
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
    # Averaged from Build Spend by the service: the last three complete months.
    # Never typed; Usage (mo), when filled, wins over it.
    usage_avg: Decimal | None = None
    # The month it began being paid. Empty counts it from January of the year
    # being totalled, which is right for a tool that predates it.
    since: date | None = None

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
        """A typical month's usage: what was typed, else the average of the
        recorded months, else -- for a tool with no plan -- its Cost, which is
        what Cost meant there before usage had a column of its own."""
        if not self.metered:
            return None
        if self.usage is not None:
            return self.usage
        if self.usage_avg is not None:
            return self.usage_avg
        return None if self.has_plan else self.cost

    @property
    def usage_computed(self) -> bool:
        """Whether the usage figure shown is an average, not something typed."""
        return self.metered and self.usage is None and self.usage_avg is not None

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
        """What the row says, in the tool's own currency: the plan fee with a
        typical month's usage after a plus -- or the usage alone for a tool with
        no plan. A tilde marks a computed figure, so an average is not read as
        something typed."""
        mark = "~" if self.usage_computed else ""
        guess = self.usage_monthly()
        if not self.has_plan:
            return f"{mark}{_amount(guess)}" if guess is not None else "?"
        text = _amount(self.cost) if self.cost is not None else "?"
        if self.metered:
            text += f"+{mark}{_amount(guess)}" if guess is not None else "+?"
        return text


@dataclass(frozen=True)
class Api:
    """The live usage figure, in dollars."""

    this: Decimal
    last: Decimal | None


def average_usage(
    rows: list[tuple[date, str, Decimal]], asof: date
) -> dict[str, Decimal]:
    """A typical month's usage per tool id from recorded charges, for the month
    beginning `asof`; see ToolService.usage_averages for the rule."""
    first_seen: dict[str, date] = {}
    by_month: dict[tuple[str, date], Decimal] = {}
    for month, tool_id, amount in rows:
        first_seen[tool_id] = min(first_seen.get(tool_id, month), month)
        key = (tool_id, month)
        by_month[key] = by_month.get(key, Decimal(0)) + amount
    window = [_months_before(asof, k) for k in (3, 2, 1)]
    out: dict[str, Decimal] = {}
    for tool_id, started in first_seen.items():
        counted = [m for m in window if m >= started]
        if counted:
            total = sum((by_month.get((tool_id, m), Decimal(0)) for m in counted), Decimal(0))
            out[tool_id] = total / len(counted)
    return out


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
            since=n.read_date(page, "Since"),
        )

    async def spend_rows(self) -> list[tuple[date, str, Decimal]]:
        """Every recorded charge as (month, tool id, amount), in the tool's own
        currency. A month is titled YYYY-MM, as the plan rows are; a title that
        is not a month is skipped rather than guessed, and so is a row with no
        amount. Several rows for a tool in a month are all returned: a second
        invoice is a second row, not a correction. Nothing without a database."""
        if not self._spend_db_id:
            return []
        if self._spend_ds is None:
            self._spend_ds = await self._client.resolve_data_source(self._spend_db_id)
        rows: list[tuple[date, str, Decimal]] = []
        async for page in self._client.query(self._spend_ds):
            month = _month(n.read_title(page, "Month"))
            amount = n.read_number(page, "Amount")
            if month is None or amount is None:
                continue
            for tool_id in n.read_relation_ids(page, "Tool"):
                rows.append((month, tool_id, Decimal(str(amount))))
        return rows

    async def usage_averages(self, asof: date) -> dict[str, Decimal]:
        """A typical month's usage per tool, from what was recorded: the three
        complete months before the month beginning `asof`, summed and divided
        by the months counted.

        A month in that span with no row counts as nothing -- but only from the
        tool's first record on, so a tool recorded for two months is averaged
        over two, not dragged down by a third that predates it. Months are
        calendar months rather than "the last three that have a row": a lumpy
        tool (a prepaid credit bought in May) would otherwise carry that May
        into every estimate until three newer rows pushed it out.
        """
        if not self._spend_db_id:
            return {}
        return average_usage(await self.spend_rows(), asof)

    async def find(self, name: str) -> Tool:
        """A tool billed by usage, by name: exact in any case, else a unique
        part of one. Refused when it is not billed by usage, because a charge
        recorded against a plan-only tool would be read by nothing."""
        needle = name.strip().casefold()
        tools = await self.tools()
        hits = [t for t in tools if t.name.casefold() == needle] or [
            t for t in tools if needle and needle in t.name.casefold()
        ]
        if not hits:
            raise ValueError(f"No tool called {name!r}.")
        if len(hits) > 1:
            raise ValueError(
                f"{name!r} could be " + ", ".join(t.name for t in hits) + ". Be more specific."
            )
        if not hits[0].metered:
            raise ValueError(
                f"{hits[0].name} is not billed by usage. Add Usage to its Billing first."
            )
        return hits[0]

    async def record_spend(
        self, tool: Tool, month: date, amount: Decimal, note: str = ""
    ) -> Decimal:
        """Write one charge to Build Spend and return the tool's total for that
        month with it in. A second charge in a month is another row, not a
        replacement: it is a second invoice."""
        if not self._spend_db_id:
            raise ValueError("NOTION_BUILD_SPEND_DB_ID is not set.")
        before = sum(
            (a for m, tool_id, a in await self.spend_rows() if m == month and tool_id == tool.id),
            Decimal(0),
        )
        if self._spend_ds is None:
            self._spend_ds = await self._client.resolve_data_source(self._spend_db_id)
        props: dict = {
            "Month": n.title(month.strftime("%Y-%m")),
            "Tool": n.relation([tool.id]),
            "Amount": n.number(float(amount)),
        }
        if note:
            props["Note"] = n.rich_text(note)
        await self._client.create_page(self._spend_ds, props)
        return before + amount

    async def tools_for(
        self,
        asofs: list[date],
        spend: list[tuple[date, str, Decimal]] | None = None,
    ) -> list[list[Tool]]:
        """The tools once for several months: each list has usage averaged from
        Build Spend as it stood for that month. The tools and the charges are
        read a single time, which is what a report that looks at two months
        would otherwise do twice -- and every read is a trip to Notion."""
        ds = await self.data_source_id()
        rows = [self._to_tool(page) async for page in self._client.query(ds)]
        if spend is None:
            spend = await self.spend_rows()
        out = []
        for asof in asofs:
            averages = average_usage(spend, asof) if self._spend_db_id else {}
            tools = [
                replace(t, usage_avg=averages.get(t.id)) if t.id in averages else t
                for t in rows
            ]
            out.append(sorted((t for t in tools if t.name), key=lambda t: (not t.counts, t.name)))
        return out

    async def tools(self, asof: date | None = None) -> list[Tool]:
        """The tools, with usage averaged from Build Spend as it stood for the
        month beginning `asof` (left out: no averages)."""
        if asof is None:
            ds = await self.data_source_id()
            rows = [self._to_tool(page) async for page in self._client.query(ds)]
            return sorted((t for t in rows if t.name), key=lambda t: (not t.counts, t.name))
        return (await self.tools_for([asof]))[0]


# -- layout ------------------------------------------------------------------


def _month(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip()[:7], "%Y-%m").date()
    except ValueError:
        return None


def parse_month(text: str) -> date:
    """A month argument as the first of it; ValueError with the format if not."""
    month = _month(text) if re.fullmatch(r"\s*\d{4}-\d{2}\s*", text) else None
    if month is None:
        raise ValueError("Give the month as YYYY-MM, e.g. 2026-09.")
    return month


def _months_before(first: date, count: int) -> date:
    year, month = divmod(first.year * 12 + first.month - 1 - count, 12)
    return date(year, month + 1, 1)


def _amount(value: Decimal) -> str:
    return f"{value:,.0f}" if value == value.to_integral() else f"{value:,.2f}"


def _won(tool: Tool, rates: dict[str, float]) -> int | None:
    monthly = tool.monthly()
    if monthly is None or tool.currency not in rates:
        return None
    return round(float(monthly) * rates[tool.currency])


SHARED = "Shared"


def split_by_project(
    items: list[tuple[Tool, int]], names: dict[str, str]
) -> list[tuple[str, int]]:
    """Won per project, largest first, Shared last.

    A tool for several projects is split equally between them -- the one rule
    that needs no weights to keep up to date, and that adds back to the total
    exactly: the odd won goes to the first projects rather than being rounded
    away. A tool with no project is Shared, not spread across all of them,
    because who it is for is the thing the column records.
    """
    totals: dict[str, int] = {}
    for t, won in items:
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


def by_project(
    tools: list[Tool], rates: dict[str, float], names: dict[str, str]
) -> list[tuple[str, int]]:
    """Fixed won a month per project, for the tools as they stand."""
    items = [(t, won) for t in tools if t.counts and (won := _won(t, rates)) is not None]
    return split_by_project(items, names)


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
    foreign = {t.currency for t in tools if t.counts and t.monthly() is not None} - {HOME}
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
    names: dict[str, str] | None = None,
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

    usd = rates.get("USD")

    def dollars(won: int) -> str:
        """An approximate dollar figure beside a won one; nothing without a rate."""
        return f" (≈ ${won / usd:,.2f})" if usd else ""

    if api is not None and usd:
        parts = [f"${api.this:,.2f} this month so far (₩{round(float(api.this) * usd):,})"]
        if api.last is not None:
            parts.append(f"${api.last:,.2f} last month (₩{round(float(api.last) * usd):,})")
        lines.append("Anthropic API: " + " · ".join(parts))
        if split:
            lines.append("_The API is not split by project._")
        if api.last is not None and counted:
            with_api = total + round(float(api.last) * usd)
            lines.append(
                "Fixed plus the last full month of API: "
                f"₩{with_api:,}{dollars(with_api)} a month"
            )
    if counted:
        lines.append(f"Fixed over a year: ₩{total * 12:,}")
        if usd:
            lines.append(
                f"In dollars: ≈ ${total / usd:,.2f} a month · ≈ ${total * 12 / usd:,.2f} a year"
            )
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
        t.name
        for t in tools
        if t.counts and t.metered and t.has_plan and t.usage_monthly() is None
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


async def _api(admin_key: str, today: date) -> tuple[Api | None, str | None]:
    """The live figure -- this month so far against the last full one -- or why
    there is none. Never raises: a cost report that is down should cost the
    message its API line, not the whole message."""
    if not admin_key:
        return None, None
    first, _, prev = month_bounds(today)
    try:
        usage = AnthropicUsage(admin_key)
        this = await usage.cost(first, today + timedelta(days=1))
        last = await usage.cost(prev, first)
    except UsageError as exc:
        log.warning("Anthropic usage: %s", exc)
        return None, str(exc)
    return Api(this, last), None


async def _rates(tools: list[Tool]) -> tuple[dict | None, date | None, str | None]:
    # Every currency something is priced in -- by any figure, not only Cost: a
    # tool billed by usage alone has no Cost and an average instead -- and
    # always dollars, which the totals are also shown in.
    wanted = {t.currency for t in tools if t.counts and t.monthly() is not None}
    wanted.add("USD")
    try:
        rates, day = await Rates().to_won(wanted)
    except FxError as exc:
        log.warning("exchange rates: %s", exc)
        return None, None, str(exc)
    return rates, day, None


async def tools_table(
    service: ToolService,
    admin_key: str,
    today: date,
    *,
    names: dict[str, str] | None = None,
) -> str:
    """The tools as they stand, a month each in won -- what /build tools shows."""
    tools = await service.tools(asof=month_bounds(today)[0])
    api, api_problem = await _api(admin_key, today)
    rates, day, rate_problem = await _rates(tools)
    return costs_message(
        tools, rates, day, today, api=api, api_problem=api_problem,
        rate_problem=rate_problem, names=names, title="Build tools",
    )


async def weekly_line(service: ToolService, admin_key: str, today: date) -> str | None:
    """The line under the weekly summary; None, with a log line, on any failure
    so the summary still goes out."""
    try:
        tools = await service.tools(asof=month_bounds(today)[0])
        api, _ = await _api(admin_key, today)
        rates, _, _ = await _rates(tools)
        return cost_line(tools, rates, today, api=api)
    except Exception:
        log.exception("could not add costs to the weekly summary")
        return None
