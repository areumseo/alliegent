"""Where money will come from after work stops: pensions, public and company,
and what the investments will be drawn down as.

Kept apart from the weekly snapshots and the three-year plan on purpose.
Those measure what is held now; this is income that starts decades away, and
folded into either it would read as money on hand. One row per source in
Notion, each in the currency it will be paid in. Won figures are converted at
the day's rate when asked for, never stored: a rate written down today would
be the wrong one every day after.

The amounts live in Notion, never in this repository -- every example and
fixture here is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import reports
from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

HOME = "KRW"
# The order rows are read in: what is settled first, what is still a question
# last, where the list of what to look up next starts.
STATUS_ORDER = ("Confirmed", "Estimate", "Unknown")


@dataclass(frozen=True)
class Source:
    name: str
    currency: str
    monthly: int | None
    start_age: int | None = None
    status: str = "Unknown"
    note: str = ""

    @property
    def known(self) -> bool:
        return self.monthly is not None


class RetirementService:
    def __init__(self, client: NotionClient, config: Config, db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    @staticmethod
    def _to_source(page: dict) -> Source:
        monthly = n.read_number(page, "Monthly")
        age = n.read_number(page, "Start Age")
        return Source(
            name=n.read_title(page, "Name"),
            currency=(n.read_select(page, "Currency") or HOME).upper(),
            monthly=None if monthly is None else int(monthly),
            start_age=None if age is None else int(age),
            status=n.read_select(page, "Status") or "Unknown",
            note=n.read_text(page, "Note"),
        )

    async def sources(self) -> list[Source]:
        ds = await self.data_source_id()
        rows = [self._to_source(page) async for page in self._client.query(ds)]

        def rank(source: Source) -> tuple[int, str]:
            order = (
                STATUS_ORDER.index(source.status)
                if source.status in STATUS_ORDER
                else len(STATUS_ORDER)
            )
            return order, source.name

        return sorted((r for r in rows if r.name), key=rank)


def _rate_line(currency: str, rate: float) -> str:
    """Per 100 for a currency worth a few won, as it is quoted in Korea."""
    per = 100 if rate < 100 else 1
    return f"{currency} {per:,} = {HOME} {rate * per:,.1f}"


def retirement_message(
    sources: list[Source],
    rates: dict[str, float] | None,
    rate_day: date | None = None,
    *,
    rate_problem: str | None = None,
) -> str:
    """One table, each source in its own currency and in won a month.

    Unknown amounts stay in the table as "?" rather than dropping out: a
    total over the sources found so far must not look like the whole of it.
    """
    if not sources:
        return "🌅 **Retirement income**\nNothing recorded yet."
    rates = rates or {}
    rows: list[tuple[str, ...] | None] = []
    total = 0
    counted = 0
    for s in sources:
        won = (
            round(s.monthly * rates[s.currency])
            if s.known and s.currency in rates
            else None
        )
        if won is not None:
            total += won
            counted += 1
        rows.append((
            s.name,
            s.currency,
            f"{s.monthly:,}" if s.known else "?",
            f"{won:,}" if won is not None else "?",
            str(s.start_age) if s.start_age else "-",
        ))
    if counted:
        rows += [None, ("TOTAL", "", "", f"{total:,}", "")]

    lines = ["🌅 **Retirement income** (monthly)"]
    lines += reports._table(
        ("Source", "Cur", "Monthly", "KRW", "Age"), rows, flex=0, right=(2, 3, 4)
    )
    foreign = sorted({s.currency for s in sources if s.known} - {HOME})
    quoted = [_rate_line(c, rates[c]) for c in foreign if c in rates]
    if quoted:
        when = f" (ECB, {rate_day.isoformat()})" if rate_day else ""
        lines.append(" · ".join(quoted) + when)
    if rate_problem:
        lines.append(f"⚠️ No exchange rate, so no won figure: {rate_problem}")
    unknown = [s.name for s in sources if not s.known]
    if unknown:
        lines.append("Still to look up: " + ", ".join(unknown))
    return "\n".join(lines)
