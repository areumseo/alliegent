"""The report on what the tools cost: a month, the year so far, the year ahead.

A table of what each tool costs a month says what the running rate is, not what
was spent. This reports what was: the month just ended tool by tool against the
month before, the year so far, and what the year is expected to come to --
spent so far, plus the months left at the current rate.

**What counts for a month.** A plan fee is its monthly figure, and a yearly plan
is a twelfth of its price in every month, so months compare. Usage is what
Build Spend recorded for that month. A month with no row counts as nothing --
except the month being reported, which takes the tool's estimate and is named,
so an estimate is never mistaken for an invoice. A tool counts from its `Since`
month, or from January when that is empty. Cancelled tools and trials are not
counted, in past months either: there is no end date to say when a cancelled
tool stopped.

**Amounts are converted at today's rate**, for every month. A month's own rate
would need a rate history this bot does not keep, and the figures are for
planning, not for the books.

The amounts live in Notion, never in this repository -- every example and
fixture here is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import reports
from .build_costs import (
    HOME,
    Tool,
    ToolService,
    _months_before,
    _rates,
    split_by_project,
)
from .integrations.anthropic_usage import AnthropicUsage, UsageError, month_bounds

log = logging.getLogger(__name__)

API_NAME = "Anthropic API"


@dataclass(frozen=True)
class Month:
    first: date
    by_tool: dict[str, int]  # tool id -> won, plan and usage together
    api: int  # won, from the cost report; 0 when there is none
    estimated: tuple[str, ...]  # usage tools taken at the estimate, for want of a row

    @property
    def total(self) -> int:
        return sum(self.by_tool.values()) + self.api


def _same_or_after(month: date, start: date) -> bool:
    return (month.year, month.month) >= (start.year, start.month)


def active(tool: Tool, month: date) -> bool:
    """Whether a tool is being paid for in `month`."""
    if not tool.counts:
        return False
    start = tool.since or date(month.year, 1, 1)
    return _same_or_after(month, start)


def month_figures(
    tools: list[Tool],
    spend: list[tuple[date, str, Decimal]],
    rates: dict[str, float],
    month: date,
    *,
    api_won: int = 0,
    reported: bool = False,
) -> Month:
    """One month in won. `reported` is the month the report is about: a usage
    tool with no row there is taken at its estimate and named."""
    recorded: dict[str, Decimal] = {}
    for when, tool_id, amount in spend:
        if when == month:
            recorded[tool_id] = recorded.get(tool_id, Decimal(0)) + amount
    by_tool: dict[str, int] = {}
    estimated: list[str] = []
    for t in tools:
        if not active(t, month) or t.currency not in rates:
            continue
        rate = rates[t.currency]
        plan = t.plan_monthly() or Decimal(0)
        usage = Decimal(0)
        if t.metered:
            if t.id in recorded:
                usage = recorded[t.id]
            elif reported:
                estimated.append(t.name)
                usage = t.usage_monthly() or Decimal(0)
        by_tool[t.id or t.name] = round(float(plan + usage) * rate)
    return Month(month, by_tool, api_won, tuple(estimated))


def expected_month(tools: list[Tool], rates: dict[str, float], month: date) -> int:
    """What `month` should cost at the current rate: plan fees and the usage
    estimate, for every tool in force then."""
    total = 0.0
    for t in tools:
        if active(t, month) and t.currency in rates and (monthly := t.monthly()) is not None:
            total += float(monthly) * rates[t.currency]
    return round(total)


async def _api_months(
    admin_key: str, months: list[date], usd: float | None
) -> tuple[dict[date, int], str | None]:
    """Won per month from the cost report, or why there is none. Never raises."""
    if not admin_key or not usd:
        return {}, None
    out: dict[date, int] = {}
    usage = AnthropicUsage(admin_key)
    try:
        for first in months:
            out[first] = round(float(await usage.cost(first, month_bounds(first)[1])) * usd)
    except UsageError as exc:
        log.warning("Anthropic usage: %s", exc)
        return {}, str(exc)
    return out, None


def _signed(value: int) -> str:
    return "0" if value == 0 else f"{value:+,}"


def report_message(
    tools: list[Tool],
    rates: dict[str, float] | None,
    rate_day: date | None,
    month: date,
    figures: dict[date, Month],
    *,
    expected_rest: int,  # won over the months left, tools and their usage estimates
    expected_api: int,  # won a month, from the cost report
    names: dict[str, str],
    today: date,
    api_problem: str | None = None,
    rate_problem: str | None = None,
) -> str:
    rates = rates or {}
    usd = rates.get("USD")
    this = figures[month]
    before = figures[_months_before(month, 1)]
    year = [figures[m] for m in sorted(figures) if m.year == month.year and m <= month]
    spent = sum(m.total for m in year)
    left = 12 - month.month
    ahead = expected_rest + left * expected_api
    expected = spent + ahead

    def dollars(won: int) -> str:
        return f" (≈ ${won / usd:,.2f})" if usd else ""

    first_month = date(month.year, 1, 1).strftime("%b")
    in_progress = (month.year, month.month) == (today.year, today.month)
    label = month.strftime("%B %Y") + (" — so far" if in_progress else "")
    lines = [f"📊 **Build costs — {label}**"]
    diff = this.total - before.total
    lines.append(
        f"{month.strftime('%B')}: ₩{this.total:,}{dollars(this.total)} · "
        f"vs {before.first.strftime('%B')} {'+' if diff >= 0 else '-'}₩{abs(diff):,}"
    )
    lines.append(
        f"Year so far ({first_month}–{month.strftime('%b')}): "
        f"₩{spent:,}{dollars(spent)}"
    )
    tail = (
        f"{left} more month{'s' if left != 1 else ''} at about "
        f"₩{round(ahead / left) if left else 0:,}"
    )
    lines.append(
        f"Expected for {month.year}: ₩{expected:,}{dollars(expected)}"
        + (f" — {tail}" if left else "")
    )

    by_id = {t.id or t.name: t for t in tools}
    rows: list[tuple[str, ...] | None] = []
    ids = sorted(
        (i for i in set(this.by_tool) | set(before.by_tool)
         if this.by_tool.get(i, 0) or before.by_tool.get(i, 0)),
        key=lambda i: (-this.by_tool.get(i, 0), by_id[i].name),
    )
    for i in ids:
        now, prev = this.by_tool.get(i, 0), before.by_tool.get(i, 0)
        rows.append((by_id[i].name, f"{now:,}", f"{prev:,}", _signed(now - prev)))
    if this.api or before.api:
        rows.append((API_NAME, f"{this.api:,}", f"{before.api:,}", _signed(this.api - before.api)))
    if rows:
        rows += [None, ("TOTAL", f"{this.total:,}", f"{before.total:,}", _signed(diff))]
        lines.append("**By tool** (KRW)")
        lines += reports._table(
            ("Tool", month.strftime("%b"), before.first.strftime("%b"), "Chg"),
            rows, flex=0, right=(1, 2, 3),
        )

    if names and any(t.project_ids for t in tools):
        items = [
            (by_id[i], won) for i, won in this.by_tool.items() if won and i in by_id
        ]
        split = split_by_project(items, names)
        if this.api:
            split.append((API_NAME + " (not split)", this.api))
        if split:
            lines.append(f"**By project** (KRW, {month.strftime('%B')})")
            lines += reports._table(
                ("Project", "KRW"),
                [*((n, f"{w:,}") for n, w in split), None, ("TOTAL", f"{this.total:,}")],
                flex=0, right=(1,),
            )

    if this.estimated:
        lines.append(
            f"No record for {month.strftime('%B')}, counted at the estimate: "
            + ", ".join(this.estimated)
        )
    if usd and rate_day:
        lines.append(
            f"USD 1 = {HOME} {usd:,.1f} (ECB, {rate_day.isoformat()}), used for every month"
        )
    lines.append("Plan fees are spread evenly over the year.")
    if any(t.status == "Cancelled" for t in tools):
        lines.append("Cancelled tools are not counted, in past months either.")
    if rate_problem:
        lines.append(f"⚠️ No exchange rate, so no won figures: {rate_problem}")
    if api_problem:
        lines.append(f"⚠️ Anthropic usage unavailable: {api_problem}")
    unpriced = [t.name for t in tools if t.counts and t.monthly() is None]
    if unpriced:
        lines.append("Still to price: " + ", ".join(unpriced))
    return "\n".join(lines)


async def build_report(
    service: ToolService,
    admin_key: str,
    today: date,
    month: date | None = None,
    *,
    names: dict[str, str] | None = None,
) -> str:
    """The report for `month` (default: the month before today's)."""
    month = month or month_bounds(today)[2]
    month = month.replace(day=1)
    prior = _months_before(month, 1)

    # The estimate for the reported month is the average as it stood *before*
    # it, so a month is never held against an average that contains it. The
    # months ahead use the average including it.
    tools = await service.tools(asof=month)
    ahead_tools = await service.tools(asof=_months_before(month, -1))
    rates, day, rate_problem = await _rates(tools)
    spend = await service.spend_rows()

    year = [date(month.year, m, 1) for m in range(1, month.month + 1)]
    wanted = sorted({*year, prior})
    api_won, api_problem = await _api_months(admin_key, wanted, (rates or {}).get("USD"))

    figures = {
        m: month_figures(
            tools, spend, rates or {}, m, api_won=api_won.get(m, 0), reported=(m == month)
        )
        for m in wanted
    }
    # What the API will cost a month: the mean of the last three it was read for.
    recent = [api_won[m] for m in wanted if m <= month and m in api_won][-3:]
    expected_api = round(sum(recent) / len(recent)) if recent else 0
    return report_message(
        tools, rates, day, month, figures,
        expected_rest=sum(
            expected_month(ahead_tools, rates or {}, date(month.year, m, 1))
            for m in range(month.month + 1, 13)
        ),
        expected_api=expected_api,
        names=names or {},
        today=today,
        api_problem=api_problem,
        rate_problem=rate_problem,
    )


__all__ = ["Month", "build_report", "month_figures", "report_message"]
