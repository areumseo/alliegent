"""What the build tools cost: typed subscriptions plus a live usage figure.
Tool names and amounts are invented."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from alliegent import build_costs
from alliegent.build_costs import (
    Actual,
    Api,
    Tool,
    ToolService,
    actual_for,
    cost_line,
    costs_message,
)
from alliegent.config import Config
from alliegent.integrations.anthropic_usage import AnthropicUsage, UsageError, month_bounds

from .conftest import FakeNotionClient

TODAY = date(2026, 10, 5)
RATES = {"KRW": 1.0, "USD": 1400.0}


def page(pid, name, *, currency="USD", cost=None, billing="Monthly", renews=None,
         status="Active"):
    def select(value):
        return {"type": "select", "select": {"name": value}}

    return {
        "object": "page",
        "id": pid,
        "url": "",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name, "type": "text"}]},
            "Currency": select(currency),
            "Cost": {"type": "number", "number": cost},
            "Billing": select(billing),
            "Renews": {"type": "date", "date": {"start": renews} if renews else None},
            "Status": select(status),
            "Note": {"type": "rich_text", "rich_text": []},
        },
    }


def tool(name, cost, **kw):
    """`billing` as one word or several, `usage` as a string, for brevity."""
    billing = kw.pop("billing", "Monthly")
    kw["billing"] = (billing,) if isinstance(billing, str) else tuple(billing)
    if kw.get("usage") is not None:
        kw["usage"] = Decimal(kw["usage"])
    return Tool(name, kw.pop("currency", "USD"), None if cost is None else Decimal(cost), **kw)


NAMES = {"pa": "Alpha", "pb": "Beta", "pc": "Gamma"}


def row(text, name):
    return next(line for line in text.splitlines() if line.startswith(name))


# -- reading ----------------------------------------------------------------


async def test_billing_reads_as_several_choices_or_as_the_old_single_one():
    multi = page("a", "Platform", cost=20)
    multi["properties"]["Billing"] = {
        "type": "multi_select",
        "multi_select": [{"name": "Monthly"}, {"name": "Usage"}],
    }
    multi["properties"]["Usage (mo)"] = {"type": "number", "number": 15}
    old = page("b", "Host", cost=5, billing="Yearly")  # still a single select
    empty = page("c", "Blank", cost=1)
    empty["properties"]["Billing"] = {"type": "multi_select", "multi_select": []}
    client = FakeNotionClient({"ds_tools-db": [multi, old, empty]})
    tools = {t.name: t for t in await ToolService(client, Config(), "tools-db").tools()}
    assert tools["Platform"].billing == ("Monthly", "Usage")
    assert tools["Platform"].usage == Decimal(15)
    assert tools["Host"].billing == ("Yearly",)
    assert tools["Blank"].billing == ("Monthly",)


async def test_active_tools_come_before_cancelled_ones():
    client = FakeNotionClient({
        "ds_tools-db": [
            page("a", "Zed", cost=5, status="Cancelled"),
            page("b", "Mid", cost=20),
            page("c", "Alpha", cost=10, status="Trial"),
        ]
    })
    tools = await ToolService(client, Config(), "tools-db").tools()
    # Active first; the rest follow by name.
    assert [t.name for t in tools] == ["Mid", "Alpha", "Zed"]


# -- the table --------------------------------------------------------------


def test_a_yearly_plan_is_a_twelfth_a_month_and_converted():
    text = costs_message(
        [tool("Editor", "120", billing="Yearly"), tool("Host", "5")], RATES, None, TODAY
    )
    assert row(text, "Editor").split() == ["Editor", "USD", "120", "yr", "14,000"]
    assert row(text, "Host").split()[-1] == "7,000"
    assert row(text, "FIXED").split()[-1] == "21,000"
    assert "Fixed over a year: ₩252,000" in text


def test_a_plan_with_usage_on_top_is_both_parts():
    text = costs_message(
        [tool("Platform", "20", billing=("Monthly", "Usage"), usage="15")], RATES, None, TODAY
    )
    assert row(text, "Platform").split() == ["Platform", "USD", "20+15", "mo+use", "49,000"]


def test_a_yearly_plan_with_usage_is_a_twelfth_plus_the_usage():
    text = costs_message(
        [tool("Suite", "120", billing=("Yearly", "Usage"), usage="10")], RATES, None, TODAY
    )
    # 120/12 + 10 = 20 dollars a month.
    assert row(text, "Suite").split() == ["Suite", "USD", "120+10", "yr+use", "28,000"]


def test_usage_alone_reads_cost_as_the_monthly_estimate_as_it_always_did():
    text = costs_message([tool("Host", "8", billing="Usage")], RATES, None, TODAY)
    assert row(text, "Host").split() == ["Host", "USD", "8", "use", "11,200"]


def test_usage_alone_prefers_the_usage_column_when_it_is_filled():
    text = costs_message(
        [tool("Host", "8", billing="Usage", usage="12")], RATES, None, TODAY
    )
    assert row(text, "Host").split()[-1] == "16,800"


def test_a_plan_with_usage_but_no_estimate_is_counted_at_the_fee_and_says_so():
    text = costs_message(
        [tool("Platform", "20", billing=("Monthly", "Usage"))], RATES, None, TODAY
    )
    assert row(text, "Platform").split()[-1] == "28,000"
    assert "+?" in row(text, "Platform")
    assert "Usage not estimated, counted at the plan fee: Platform" in text


def test_usage_alone_with_nothing_typed_is_unpriced():
    text = costs_message([tool("Host", None, billing="Usage")], RATES, None, TODAY)
    assert "Still to price: Host" in text


def test_a_cancelled_tool_leaves_the_table_and_the_sums():
    text = costs_message(
        [tool("Host", "5"), tool("Old", "50", status="Cancelled")], RATES, None, TODAY
    )
    assert "Old" not in text
    assert row(text, "FIXED").split()[-1] == "7,000"


def test_a_trial_is_named_and_costs_nothing_yet():
    text = costs_message(
        [tool("Host", "5"), tool("New", "30", status="Trial", renews=date(2026, 10, 9))],
        RATES, None, TODAY,
    )
    assert row(text, "New").split() == ["New", "USD", "trial", "-", "-"]
    assert row(text, "FIXED").split()[-1] == "7,000"
    assert "⏰ New ends Fri 10/9" in text


def test_an_unpriced_tool_is_named_rather_than_counted_as_free():
    text = costs_message([tool("Host", "5"), tool("Mystery", None)], RATES, None, TODAY)
    assert row(text, "Mystery").split()[-1] == "?"
    assert "Still to price: Mystery" in text


def test_a_renewal_inside_two_weeks_is_flagged_and_a_later_one_is_not():
    text = costs_message(
        [tool("Soon", "5", renews=date(2026, 10, 12)),
         tool("Later", "5", renews=date(2026, 12, 1))],
        RATES, None, TODAY,
    )
    assert "⏰ Soon renews Mon 10/12" in text
    assert "Later renews" not in text


def test_without_a_rate_there_are_no_won_figures_and_it_says_why():
    text = costs_message([tool("Host", "5")], None, None, TODAY, rate_problem="down")
    assert row(text, "Host").split()[-1] == "?"
    assert "FIXED" not in text
    assert "⚠️ No exchange rate" in text


def test_no_tools_and_no_live_figure_says_so():
    assert "No tools recorded yet" in costs_message([], RATES, None, TODAY)


# -- what a month really cost -------------------------------------------------


def spend_page(pid, month, tool_id, amount):
    return {
        "object": "page",
        "id": pid,
        "url": "",
        "properties": {
            "Month": {"type": "title", "title": [{"plain_text": month, "type": "text"}]},
            "Tool": {"type": "relation", "relation": [{"id": tool_id}]},
            "Amount": {"type": "number", "number": amount},
        },
    }


def spend_service(pages, tools=()):
    client = FakeNotionClient({"ds_tools-db": list(tools), "ds_spend-db": pages})
    return ToolService(client, Config(), "tools-db", "spend-db")


SEP = date(2026, 9, 1)


async def test_spend_is_read_for_the_month_asked_and_summed_per_tool():
    service = spend_service([
        spend_page("s1", "2026-09", "t1", 10),
        spend_page("s2", "2026-09", "t1", 2.5),   # a second invoice, not a correction
        spend_page("s3", "2026-09", "t2", 7),
        spend_page("s4", "2026-08", "t1", 99),    # another month
        spend_page("s5", "September", "t1", 99),  # not a month: skipped, not guessed
    ])
    assert await service.spend_in(SEP) == {"t1": Decimal("12.5"), "t2": Decimal(7)}


async def test_a_month_with_nothing_recorded_is_empty_not_unknown():
    assert await spend_service([]).spend_in(SEP) == {}


async def test_without_a_spend_database_there_is_nothing_to_say():
    client = FakeNotionClient({"ds_tools-db": []})
    assert await ToolService(client, Config(), "tools-db").spend_in(SEP) is None


def metered(name, tool_id, cost="20", usage="15", **kw):
    return tool(name, cost, billing=("Monthly", "Usage"), usage=usage, id=tool_id, **kw)


def test_a_recorded_month_replaces_the_estimate_and_a_missing_one_is_named():
    tools = [
        metered("Platform", "t1"),                       # plan 20 + usage
        tool("Host", "8", billing="Usage", id="t2"),     # usage alone, estimated 8
        tool("Editor", "10", id="t3"),                   # plan only
    ]
    got = actual_for(tools, {"t1": Decimal("22")}, RATES)
    # Plan fees: 20 + 10 = 30 dollars. Recorded: 22. Host estimated at 8.
    assert got == Actual(
        total=round(60 * 1400), fixed=round(30 * 1400),
        recorded=round(22 * 1400), estimated=round(8 * 1400), missing=("Host",),
    )


def test_a_cancelled_tool_and_a_plan_only_tool_are_never_missing():
    got = actual_for(
        [tool("Old", "50", billing="Usage", status="Cancelled", id="t1"),
         tool("Editor", "10", id="t2")],
        {}, RATES,
    )
    assert got.missing == () and got.recorded == 0


def test_the_actual_is_set_against_the_estimate_and_missing_records_are_said():
    tools = [metered("Platform", "t1"), metered("Render", "t2", cost="5", usage="10")]
    actual = actual_for(tools, {"t1": Decimal("30")}, RATES)
    text = costs_message(tools, RATES, None, TODAY, actual=actual, actual_label="September")
    # Estimate: (20+15) + (5+10) = 50 dollars; actual: 20+30 + 5+10(est) = 65.
    assert (
        "Actual September: ₩91,000 (≈ $65.00) — "
        "plan ₩35,000, usage ₩42,000 recorded + ₩14,000 estimated"
    ) in text
    assert "The estimate was ₩70,000: +₩21,000" in text
    assert "No record for September: Render" in text


def test_an_actual_that_matches_says_so():
    tools = [metered("Platform", "t1")]
    actual = actual_for(tools, {"t1": Decimal("15")}, RATES)
    text = costs_message(tools, RATES, None, TODAY, actual=actual, actual_label="September")
    assert "on it" in text
    assert "No record" not in text


async def test_the_settlement_uses_the_months_records(monkeypatch):
    async def rates(tools):
        return RATES, date(2026, 10, 1), None

    monkeypatch.setattr(build_costs, "_rates", rates)
    service = spend_service(
        [spend_page("s1", "2026-09", "t1", 30), spend_page("s2", "2026-10", "t1", 99)],
        tools=[page("t1", "Platform", cost=20, billing="Monthly")],
    )
    # The row above is a plain plan; make it metered with an estimate.
    service._client.pages["ds_tools-db"][0]["properties"]["Billing"] = {
        "type": "multi_select",
        "multi_select": [{"name": "Monthly"}, {"name": "Usage"}],
    }
    text = await build_costs.build_costs(service, "", date(2026, 10, 1), settle=True)
    assert "Build costs — September 2026" in text
    assert "Actual September: ₩70,000" in text   # 20 + 30 recorded, not October's 99


# -- usage averaged from what was recorded ------------------------------------

OCT = date(2026, 10, 1)


def usage_tool_pages():
    """One tool billed by usage alone, id 't1'."""
    row = page("t1", "Host", billing="Usage")
    row["properties"]["Billing"] = {"type": "multi_select", "multi_select": [{"name": "Usage"}]}
    return [row]


async def test_the_average_is_the_three_complete_months_before_the_one_asked():
    service = spend_service([
        spend_page("a", "2026-07", "t1", 6),
        spend_page("b", "2026-08", "t1", 9),
        spend_page("c", "2026-09", "t1", 12),
        spend_page("d", "2026-10", "t1", 99),   # the month in progress: left out
    ])
    assert await service.usage_averages(OCT) == {"t1": Decimal(9)}


async def test_a_month_with_no_row_counts_as_nothing_once_the_tool_has_history():
    service = spend_service([
        spend_page("a", "2026-07", "t1", 9),
        spend_page("c", "2026-09", "t1", 9),
    ])
    assert await service.usage_averages(OCT) == {"t1": Decimal(6)}   # (9 + 0 + 9) / 3


async def test_months_before_the_first_record_are_not_counted():
    service = spend_service([
        spend_page("b", "2026-08", "t1", 10),
        spend_page("c", "2026-09", "t1", 20),
    ])
    assert await service.usage_averages(OCT) == {"t1": Decimal(15)}  # over two, not three


async def test_a_tool_first_recorded_this_month_has_no_average_yet():
    service = spend_service([spend_page("d", "2026-10", "t1", 5)])
    assert await service.usage_averages(OCT) == {}


async def test_an_old_lump_does_not_carry_into_the_estimate():
    """A prepaid credit bought in May is not a typical month in October."""
    service = spend_service([
        spend_page("a", "2026-05", "t1", 60),
        spend_page("b", "2026-08", "t1", 3),
        spend_page("c", "2026-09", "t1", 6),
    ])
    assert await service.usage_averages(OCT) == {"t1": Decimal(3)}   # (0 + 3 + 6) / 3


async def test_several_rows_in_a_month_add_up_in_the_average():
    service = spend_service([
        spend_page("a", "2026-09", "t1", 4),
        spend_page("b", "2026-09", "t1", 5),
        spend_page("c", "2026-07", "t1", 0),
    ])
    assert await service.usage_averages(OCT) == {"t1": Decimal(3)}   # 9 over three months


async def test_tools_carry_the_average_only_when_asked_for_a_month():
    service = spend_service(
        [spend_page("a", "2026-09", "t1", 30)], tools=usage_tool_pages()
    )
    [plain] = await service.tools()
    [averaged] = await service.tools(asof=OCT)
    assert plain.usage_avg is None
    assert averaged.usage_avg == Decimal(30)   # one month of history, so over one


def test_a_typed_usage_beats_the_average_which_beats_the_old_cost():
    typed = tool("A", "8", billing="Usage", usage="12", usage_avg=Decimal(5))
    averaged = tool("B", "8", billing="Usage", usage_avg=Decimal(5))
    legacy = tool("C", "8", billing="Usage")
    assert typed.usage_monthly() == Decimal(12)
    assert averaged.usage_monthly() == Decimal(5)
    assert legacy.usage_monthly() == Decimal(8)


def test_a_computed_figure_is_marked_with_a_tilde_and_a_typed_one_is_not():
    text = costs_message(
        [tool("Host", None, billing="Usage", usage_avg=Decimal("7.95")),
         tool("Platform", "20", billing=("Monthly", "Usage"), usage_avg=Decimal(15)),
         tool("Typed", "20", billing=("Monthly", "Usage"), usage="15")],
        RATES, None, TODAY,
    )
    assert row(text, "Host").split()[2] == "~7.95"
    assert row(text, "Platform").split()[2] == "20+~15"
    assert row(text, "Typed").split()[2] == "20+15"


def test_an_average_makes_a_plan_with_usage_estimated_not_understated():
    text = costs_message(
        [tool("Platform", "20", billing=("Monthly", "Usage"), usage_avg=Decimal(15))],
        RATES, None, TODAY,
    )
    assert "Usage not estimated" not in text
    assert row(text, "Platform").split()[-1] == "49,000"


async def test_the_settlements_estimate_leaves_out_the_month_it_judges(monkeypatch):
    """October's actual must not be inside the average it is compared with."""
    async def rates(tools):
        return RATES, date(2026, 11, 1), None

    monkeypatch.setattr(build_costs, "_rates", rates)
    service = spend_service(
        [
            spend_page("a", "2026-08", "t1", 3),
            spend_page("b", "2026-09", "t1", 3),
            spend_page("c", "2026-10", "t1", 30),   # the month being settled
        ],
        tools=usage_tool_pages(),
    )
    text = await build_costs.build_costs(service, "", date(2026, 11, 1), settle=True)
    # Jul-Sep with history from August: (3 + 3) / 2 = 3 dollars, not the 12 an
    # average that took in October's 30 would give. The actual is 30.
    assert "The estimate was ₩4,200: +₩37,800" in text


# -- dollars, and which currencies get a rate ------------------------------------


def test_the_fixed_total_is_also_given_in_dollars():
    text = costs_message([tool("Host", "5"), tool("Plan", "5")], RATES, None, TODAY)
    # 14,000 won a month at 1,400 won to the dollar.
    assert "In dollars: ≈ $10.00 a month · ≈ $120.00 a year" in text


def test_no_dollar_line_without_a_dollar_rate():
    text = costs_message([tool("Host", "5000", currency="KRW")], {"KRW": 1.0}, None, TODAY)
    assert "In dollars" not in text


def test_a_settlement_and_a_total_with_the_api_carry_dollars_too():
    tools = [metered("Platform", "t1")]
    actual = actual_for(tools, {"t1": Decimal("15")}, RATES)
    text = costs_message(
        tools, RATES, None, TODAY, actual=actual, actual_label="September",
        api=Api(Decimal("10"), Decimal("30")),
    )
    assert "Actual September: ₩49,000 (≈ $35.00) — " in text
    assert "a month" in text and "(≈ $" in text.split("Fixed plus the last full month")[1]


async def test_a_usage_only_tool_with_no_cost_still_gets_its_currency_rate(monkeypatch):
    """Priced by an average alone, its currency must still be fetched, or its
    won figure is a question mark."""
    seen = {}

    async def to_won(self, currencies):
        seen["wanted"] = set(currencies)
        return {"KRW": 1.0, "USD": 1400.0, "EUR": 1500.0}, TODAY

    monkeypatch.setattr(build_costs.Rates, "to_won", to_won)
    eur = tool("Host", None, currency="EUR", billing="Usage", usage_avg=Decimal(4))
    await build_costs._rates([eur])
    assert seen["wanted"] == {"EUR", "USD"}


# -- by project -------------------------------------------------------------


def test_a_tool_for_several_projects_is_split_equally_and_adds_back_exactly():
    # 7,000 across three is 2,333 + 2,333 + 2,334: the odd won is not lost.
    split = build_costs.by_project(
        [tool("Editor", "5", project_ids=("pa", "pb", "pc"))], RATES, NAMES
    )
    assert sum(won for _, won in split) == 7000
    assert sorted(won for _, won in split) == [2333, 2333, 2334]


def test_a_tool_with_no_project_is_shared_and_listed_last():
    split = build_costs.by_project(
        [tool("Plan", "10"), tool("Host", "5", project_ids=("pa",)),
         tool("Analytics", "1", project_ids=("pb",))],
        RATES, NAMES,
    )
    assert split == [("Alpha", 7000), ("Beta", 1400), ("Shared", 14000)]


def test_cancelled_trial_and_unpriced_tools_are_not_split():
    split = build_costs.by_project(
        [tool("Old", "50", status="Cancelled", project_ids=("pa",)),
         tool("New", "30", status="Trial", project_ids=("pa",)),
         tool("Mystery", None, project_ids=("pa",))],
        RATES, NAMES,
    )
    assert split == []


def test_the_project_table_follows_the_tool_table_and_totals_the_fixed_figure():
    text = costs_message(
        [tool("Host", "5", project_ids=("pa",)), tool("Plan", "10")],
        RATES, None, TODAY, names=NAMES,
        api=Api(Decimal("10"), Decimal("30")),
    )
    assert "**By project**" in text
    assert row(text, "Alpha").split()[-1] == "7,000"
    assert row(text, "Shared").split()[-1] == "14,000"
    assert row(text, "TOTAL").split()[-1] == "21,000"
    assert "The API is not split by project" in text


def test_no_project_table_without_links_or_without_titles():
    linked = [tool("Host", "5", project_ids=("pa",))]
    assert "By project" not in costs_message([tool("Host", "5")], RATES, None, TODAY, names=NAMES)
    assert "By project" not in costs_message(linked, RATES, None, TODAY, names={})


async def test_the_projects_relation_is_read_from_the_row():
    row_page = page("a", "Host", cost=5)
    row_page["properties"]["Projects"] = {
        "type": "relation", "relation": [{"id": "pa"}, {"id": "pb"}]
    }
    client = FakeNotionClient({"ds_tools-db": [row_page]})
    [t] = await ToolService(client, Config(), "tools-db").tools()
    assert t.project_ids == ("pa", "pb")


# -- the live figure --------------------------------------------------------


def test_a_month_in_progress_is_kept_out_of_the_total_that_uses_last_month():
    text = costs_message(
        [tool("Host", "5")], RATES, None, TODAY, api=Api(Decimal("10"), Decimal("30"))
    )
    assert "$10.00 this month so far (₩14,000)" in text
    assert "$30.00 last month (₩42,000)" in text
    # 7,000 fixed + 42,000 for last full month, never the partial 14,000.
    assert "₩49,000 (≈ $35.00) a month" in text
    assert row(text, "FIXED").split()[-1] == "7,000"


def test_a_closed_month_is_the_one_the_total_uses():
    text = costs_message(
        [tool("Host", "5")], RATES, None, TODAY,
        api=Api(Decimal("30"), Decimal("20"), closed=True),
        api_labels=("September", "August"),
    )
    assert "$30.00 September" in text and "$20.00 August" in text
    assert "₩49,000 (≈ $35.00) a month" in text


def test_a_failed_usage_lookup_is_said_not_silent():
    text = costs_message([tool("Host", "5")], RATES, None, TODAY, api_problem="401")
    assert "⚠️ Anthropic usage unavailable: 401" in text


def usage_client(handler):
    return httpx.AsyncClient(
        base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler)
    )


async def test_the_cost_report_is_summed_from_cents_to_dollars_across_pages():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["x-api-key"] == "sk-ant-admin-test"
        assert request.headers["anthropic-version"]
        if "page" not in request.url.params:
            return httpx.Response(200, json={
                "data": [{"results": [{"currency": "USD", "amount": "1234.5"}]}],
                "has_more": True, "next_page": "p2",
            })
        assert request.url.params["page"] == "p2"
        return httpx.Response(200, json={
            "data": [{"results": [{"currency": "USD", "amount": "65.5"},
                                  {"currency": "USD", "amount": "100"}]}],
            "has_more": False, "next_page": None,
        })

    usage = AnthropicUsage("sk-ant-admin-test", client=usage_client(handler))
    assert await usage.cost(date(2026, 10, 1), date(2026, 10, 6)) == Decimal("14.00")
    assert len(seen) == 2
    assert seen[0].url.params["starting_at"] == "2026-10-01T00:00:00Z"
    assert seen[0].url.params["ending_at"] == "2026-10-06T00:00:00Z"


async def test_a_refused_key_is_a_usage_error_naming_the_status():
    usage = AnthropicUsage("bad", client=usage_client(lambda r: httpx.Response(401)))
    with pytest.raises(UsageError, match="401"):
        await usage.cost(date(2026, 10, 1), date(2026, 10, 6))


def test_month_bounds_cross_a_year():
    assert month_bounds(date(2027, 1, 15)) == (
        date(2027, 1, 1), date(2027, 2, 1), date(2026, 12, 1)
    )


async def test_no_admin_key_means_no_api_figure_and_no_complaint():
    assert await build_costs._api("", TODAY, closed=False) == (None, None)


async def test_the_settlement_reads_the_month_that_ended_and_the_one_before(monkeypatch):
    spans = []

    async def cost(self, first, stop):
        spans.append((first, stop))
        return Decimal(1)

    monkeypatch.setattr(AnthropicUsage, "cost", cost)
    api, problem = await build_costs._api("k", TODAY, closed=True)
    assert problem is None and api.closed
    assert spans == [
        (date(2026, 9, 1), date(2026, 10, 1)),
        (date(2026, 8, 1), date(2026, 9, 1)),
    ]


async def test_an_open_month_reads_this_month_so_far(monkeypatch):
    spans = []

    async def cost(self, first, stop):
        spans.append((first, stop))
        return Decimal(1)

    monkeypatch.setattr(AnthropicUsage, "cost", cost)
    await build_costs._api("k", TODAY, closed=False)
    assert spans == [
        (date(2026, 10, 1), date(2026, 10, 6)),
        (date(2026, 9, 1), date(2026, 10, 1)),
    ]


async def test_a_usage_failure_becomes_a_problem_not_an_exception(monkeypatch):
    async def cost(self, first, stop):
        raise UsageError("401 from the cost report")

    monkeypatch.setattr(AnthropicUsage, "cost", cost)
    assert await build_costs._api("k", TODAY, closed=False) == (
        None, "401 from the cost report"
    )


# -- the weekly line --------------------------------------------------------


def test_the_weekly_line_has_the_fixed_total_the_live_figure_and_renewals():
    line = cost_line(
        [tool("Host", "5", renews=date(2026, 10, 8))], RATES, TODAY,
        api=Api(Decimal("10"), None),
    )
    assert line == (
        "🛠 Build costs: ₩7,000/mo fixed · API ₩14,000 this month so far · "
        "⏰ Host renews Thu 10/8"
    )


def test_nothing_to_say_is_no_line():
    assert cost_line([], RATES, TODAY) is None


async def test_a_broken_costs_lookup_costs_the_summary_nothing():
    class Broken:
        async def tools(self):
            raise RuntimeError("notion is down")

    assert await build_costs.weekly_line(Broken(), "", TODAY) is None
