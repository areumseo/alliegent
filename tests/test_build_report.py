"""The cost report: a month, the year so far, the year expected, and the
commands around it. Tool names and amounts are invented; rates are round so
the arithmetic can be read."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from alliegent import build_report
from alliegent.build_costs import ToolService, parse_month
from alliegent.build_report import month_figures
from alliegent.config import Config

from .conftest import FakeNotionClient
from .test_build_costs import spend_page, tool

TODAY = date(2026, 10, 5)
SEP = date(2026, 9, 1)
RATES = {"KRW": 1.0, "USD": 1000.0}


def tool_page(pid, name, *, currency="KRW", cost=None, billing=("Monthly",), since=None,
              status="Active", projects=()):
    return {
        "object": "page",
        "id": pid,
        "url": "",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name, "type": "text"}]},
            "Currency": {"type": "select", "select": {"name": currency}},
            "Cost": {"type": "number", "number": cost},
            "Billing": {"type": "multi_select", "multi_select": [{"name": b} for b in billing]},
            "Since": {"type": "date", "date": {"start": since} if since else None},
            "Status": {"type": "select", "select": {"name": status}},
            "Projects": {"type": "relation", "relation": [{"id": i} for i in projects]},
            "Note": {"type": "rich_text", "rich_text": []},
        },
    }


def service(tools, spend=()):
    client = FakeNotionClient({"ds_tools-db": list(tools), "ds_spend-db": list(spend)})
    return ToolService(client, Config(), "tools-db", "spend-db")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def rates(tools):
        return RATES, date(2026, 10, 5), None

    monkeypatch.setattr(build_report, "_rates", rates)


def basic():
    """A yearly plan (12,000 won, so 1,000 a month) and a usage tool with three
    months of charges: 3, 6 and 9 dollars for July, August and September."""
    return service(
        [
            tool_page("t1", "Plan", cost=12000, billing=("Yearly",)),
            tool_page("t2", "Host", currency="USD", billing=("Usage",)),
        ],
        [
            spend_page("s1", "2026-07", "t2", 3),
            spend_page("s2", "2026-08", "t2", 6),
            spend_page("s3", "2026-09", "t2", 9),
        ],
    )


async def report(svc, month=None, **kw):
    return await build_report.build_report(svc, "", TODAY, month, **kw)


# -- the three headline figures ----------------------------------------------


async def test_the_month_the_year_so_far_and_the_year_expected():
    text = await report(basic())
    assert "Build costs — September 2026" in text
    # September: plan 1,000 + 9 dollars. August was 1,000 + 6 dollars.
    assert "September: ₩10,000 (≈ $10.00) · vs August +₩3,000" in text
    # January to September: nine plan months, and usage only from July on.
    assert "Year so far (Jan–Sep): ₩27,000 (≈ $27.00)" in text
    # Three months left at 1,000 plus the July-September average of 6 dollars.
    assert "Expected for 2026: ₩48,000 (≈ $48.00) — 3 more months at about ₩7,000" in text


async def test_the_default_month_is_the_one_before_todays():
    assert "September 2026" in await report(basic())


async def test_a_month_can_be_asked_for():
    text = await report(basic(), date(2026, 8, 15))
    assert "Build costs — August 2026" in text
    assert "August: ₩7,000" in text
    assert "Year so far (Jan–Aug): ₩17,000" in text


async def test_december_has_no_months_left_to_expect():
    svc = service([tool_page("t1", "Plan", cost=12000, billing=("Yearly",))])
    text = await build_report.build_report(svc, "", date(2027, 1, 5), date(2026, 12, 1))
    assert "Year so far (Jan–Dec): ₩12,000" in text
    assert "Expected for 2026: ₩12,000 (≈ $12.00)" in text
    assert "more month" not in text


async def test_the_current_month_is_marked_as_in_progress():
    text = await report(basic(), date(2026, 10, 1))
    assert "October 2026 — so far" in text


# -- what counts for a month ---------------------------------------------------


def figures(tools, spend, month=SEP, **kw):
    return month_figures(tools, spend, RATES, month, **kw)


def test_a_yearly_plan_is_a_twelfth_in_every_month():
    plan = tool("Plan", "12000", currency="KRW", billing="Yearly", id="t1")
    assert figures([plan], [], date(2026, 3, 1)).by_tool == {"t1": 1000}


def test_usage_is_what_was_recorded_for_that_month():
    host = tool("Host", None, billing="Usage", id="t2")
    got = figures([host], [(SEP, "t2", Decimal(9)), (date(2026, 8, 1), "t2", Decimal(99))])
    assert got.by_tool == {"t2": 9000}


def test_a_month_with_no_row_counts_as_nothing_unless_it_is_the_one_reported():
    host = tool("Host", None, billing="Usage", id="t2", usage_avg=Decimal(4))
    past = figures([host], [])
    assert past.by_tool == {"t2": 0} and past.estimated == ()
    now = figures([host], [], reported=True)
    assert now.by_tool == {"t2": 4000} and now.estimated == ("Host",)


def test_a_tool_counts_from_its_since_month_or_from_january():
    plan = tool("Plan", "1000", currency="KRW", id="t1", since=date(2026, 7, 20))
    assert figures([plan], [], date(2026, 6, 1)).by_tool == {}
    assert figures([plan], [], date(2026, 7, 1)).by_tool == {"t1": 1000}
    plain = tool("Old", "1000", currency="KRW", id="t2")
    assert figures([plain], [], date(2026, 1, 1)).by_tool == {"t2": 1000}


def test_cancelled_tools_and_trials_are_not_counted():
    tools = [
        tool("Old", "1000", currency="KRW", id="t1", status="Cancelled"),
        tool("New", "1000", currency="KRW", id="t2", status="Trial"),
    ]
    assert figures(tools, []).by_tool == {}


async def test_a_tool_that_starts_later_is_expected_only_from_then():
    svc = service([tool_page("t1", "Later", cost=1000, since="2026-11-01")])
    text = await report(svc)
    # Nothing yet in September, and only November and December ahead.
    assert "Expected for 2026: ₩2,000" in text


async def test_a_missing_record_is_named_and_counted_at_the_estimate():
    svc = service(
        [tool_page("t2", "Host", currency="USD", billing=("Usage",))],
        [spend_page("s1", "2026-07", "t2", 3), spend_page("s2", "2026-08", "t2", 6)],
    )
    text = await report(svc)
    # Estimate for September: (3 + 6) / 2 over the two months since the first record.
    assert "September: ₩4,500" in text
    assert "No record for September, counted at the estimate: Host" in text


async def test_the_estimate_is_the_average_before_the_month_not_including_it():
    svc = service(
        [tool_page("t2", "Host", currency="USD", billing=("Usage",))],
        [spend_page("s1", "2026-08", "t2", 3), spend_page("s2", "2026-09", "t2", 30)],
    )
    text = await report(svc)
    assert "No record" not in text
    # Recorded 30 for September, not the average that would have taken it in.
    assert "September: ₩30,000" in text


# -- the tables ------------------------------------------------------------------


async def test_the_by_tool_table_sets_the_month_against_the_one_before():
    text = await report(basic())
    host = next(line for line in text.splitlines() if line.startswith("Host"))
    assert host.split() == ["Host", "9,000", "6,000", "+3,000"]
    plan = next(line for line in text.splitlines() if line.startswith("Plan"))
    assert plan.split() == ["Plan", "1,000", "1,000", "0"]
    total = next(line for line in text.splitlines() if line.startswith("TOTAL"))
    assert total.split() == ["TOTAL", "10,000", "7,000", "+3,000"]


async def test_the_by_project_table_splits_and_adds_back_to_the_month():
    svc = service(
        [
            tool_page("t1", "Plan", cost=12000, billing=("Yearly",), projects=("pa",)),
            tool_page("t2", "Host", currency="USD", billing=("Usage",)),
        ],
        [spend_page("s3", "2026-09", "t2", 9)],
    )
    text = await report(svc, names={"pa": "Alpha"})
    assert "**By project**" in text
    alpha = next(line for line in text.splitlines() if line.startswith("Alpha"))
    shared = next(line for line in text.splitlines() if line.startswith("Shared"))
    assert alpha.split()[-1] == "1,000" and shared.split()[-1] == "9,000"


async def test_no_project_table_without_titles_or_links():
    assert "By project" not in await report(basic())
    assert "By project" not in await report(basic(), names={"pa": "Alpha"})


# -- the live API figure ------------------------------------------------------------


async def test_the_api_adds_a_row_and_counts_into_every_total(monkeypatch):
    async def api_months(admin_key, months, usd):
        return {m: 3000 for m in months if m >= date(2026, 7, 1)}, None

    monkeypatch.setattr(build_report, "_api_months", api_months)
    text = await build_report.build_report(basic(), "k", TODAY)
    api = next(line for line in text.splitlines() if line.startswith("Anthropic API"))
    assert api.split()[-3:] == ["3,000", "3,000", "0"]
    assert "September: ₩13,000" in text
    # July to September, 3,000 each, on top of the 27,000.
    assert "Year so far (Jan–Sep): ₩36,000" in text
    # Expected adds the API's recent mean of 3,000 to each of the three months.
    assert "Expected for 2026: ₩66,000" in text


async def test_an_api_failure_is_said_and_costs_the_report_only_its_row(monkeypatch):
    async def api_months(admin_key, months, usd):
        return {}, "401 from the cost report"

    monkeypatch.setattr(build_report, "_api_months", api_months)
    text = await build_report.build_report(basic(), "k", TODAY)
    assert "⚠️ Anthropic usage unavailable: 401 from the cost report" in text
    assert "September: ₩10,000" in text


# -- the service and the commands -------------------------------------------------------


async def test_spend_rows_are_every_charge_in_the_tools_own_currency():
    svc = service(
        [], [spend_page("a", "2026-09", "t1", 4), spend_page("b", "2026-09", "t1", 5),
             spend_page("c", "September", "t1", 99)],
    )
    assert await svc.spend_rows() == [(SEP, "t1", Decimal(4)), (SEP, "t1", Decimal(5))]


async def test_without_a_spend_database_there_are_no_rows():
    client = FakeNotionClient({"ds_tools-db": []})
    assert await ToolService(client, Config(), "tools-db").spend_rows() == []


async def test_a_charge_is_recorded_and_the_months_total_returned():
    svc = service(
        [tool_page("t2", "Host", currency="USD", billing=("Usage",))],
        [spend_page("s1", "2026-09", "t2", 4)],
    )
    host = await svc.find("host")
    total = await svc.record_spend(host, SEP, Decimal("5.5"), "Monthly Invoice")
    assert total == Decimal("9.5")
    [(_, props)] = svc._client.created
    assert props["Month"]["title"][0]["text"]["content"] == "2026-09"
    assert props["Tool"] == {"relation": [{"id": "t2"}]}
    assert props["Amount"] == {"number": 5.5}
    assert props["Note"]["rich_text"][0]["text"]["content"] == "Monthly Invoice"


async def test_recording_without_a_spend_database_is_refused():
    client = FakeNotionClient({"ds_tools-db": [
        tool_page("t2", "Host", currency="USD", billing=("Usage",))
    ]})
    svc = ToolService(client, Config(), "tools-db")
    host = await svc.find("Host")
    with pytest.raises(ValueError, match="NOTION_BUILD_SPEND_DB_ID"):
        await svc.record_spend(host, SEP, Decimal(1))


async def test_a_tool_is_found_by_name_or_a_unique_part_of_it():
    svc = service([
        tool_page("t1", "Claude API Console", currency="USD", billing=("Usage",)),
        tool_page("t2", "Claude", cost=1, billing=("Yearly",)),
        tool_page("t3", "Render", currency="USD", billing=("Usage",)),
    ])
    assert (await svc.find("render")).id == "t3"
    assert (await svc.find("console")).id == "t1"
    with pytest.raises(ValueError, match="No tool called"):
        await svc.find("nothing")


async def test_a_name_that_fits_two_tools_is_refused():
    svc = service([
        tool_page("t1", "Host A", currency="USD", billing=("Usage",)),
        tool_page("t2", "Host B", currency="USD", billing=("Usage",)),
    ])
    with pytest.raises(ValueError, match="Be more specific"):
        await svc.find("host")


async def test_a_tool_not_billed_by_usage_takes_no_charge():
    svc = service([tool_page("t1", "Plan", cost=1, billing=("Monthly",))])
    with pytest.raises(ValueError, match="not billed by usage"):
        await svc.find("plan")


def test_a_month_argument_must_be_a_month():
    assert parse_month("2026-09") == SEP
    for bad in ("September", "2026-13", "2026-9", "09", ""):
        with pytest.raises(ValueError, match="YYYY-MM"):
            parse_month(bad)


# -- trips to Notion -----------------------------------------------------------------------


async def test_a_report_reads_the_tools_and_the_charges_once_each():
    """Every read is a trip to Notion, and the report looks at two months."""
    svc = basic()
    reads = []
    original = svc._client.query

    def counting(ds, **kw):
        reads.append(ds)
        return original(ds, **kw)

    svc._client.query = counting
    await report(svc)
    assert reads.count("ds_tools-db") == 1
    assert reads.count("ds_spend-db") == 1


async def test_tools_for_gives_each_month_the_average_as_it_stood_then():
    svc = basic()
    sep, oct_ = await svc.tools_for([date(2026, 9, 1), date(2026, 10, 1)])
    host = lambda tools: next(t for t in tools if t.name == "Host")  # noqa: E731
    # Before September: July and August, (3 + 6) / 2. Before October: July to September.
    assert host(sep).usage_avg == Decimal("4.5")
    assert host(oct_).usage_avg == Decimal(6)


async def test_the_api_months_are_asked_for_together(monkeypatch):
    import asyncio

    from alliegent.integrations.anthropic_usage import AnthropicUsage

    running = {"now": 0, "peak": 0}

    async def cost(self, first, stop):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return Decimal(1)

    monkeypatch.setattr(AnthropicUsage, "cost", cost)
    months = [date(2026, m, 1) for m in range(1, 10)]
    got, problem = await build_report._api_months("k", months, 1000.0)
    assert problem is None and got == {m: 1000 for m in months}
    assert running["peak"] > 1
