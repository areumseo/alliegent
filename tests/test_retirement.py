"""Retirement income: pensions in the currency they are paid in, shown in won
at the day's rate. Names and amounts are invented."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from alliegent.config import Config
from alliegent.integrations.fx import FxError, Rates
from alliegent.retirement import RetirementService, Source, retirement_message

from .conftest import FakeNotionClient

DAY = date(2026, 9, 25)


def page(pid, name, *, currency="KRW", monthly=None, age=None, status="Unknown"):
    def select(value):
        return {"type": "select", "select": {"name": value} if value else None}

    return {
        "object": "page",
        "id": pid,
        "url": "",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name, "type": "text"}]},
            "Currency": select(currency),
            "Monthly": {"type": "number", "number": monthly},
            "Start Age": {"type": "number", "number": age},
            "Status": select(status),
            "Note": {"type": "rich_text", "rich_text": []},
        },
    }


async def test_settled_sources_come_first():
    client = FakeNotionClient({
        "ds_ret-db": [
            page("a", "Abroad", currency="JPY", monthly=20000, age=65, status="Confirmed"),
            page("b", "Home"),
            page("c", "Company", monthly=300000, status="Estimate"),
        ]
    })
    sources = await RetirementService(client, Config(), "ret-db").sources()
    assert [s.name for s in sources] == ["Abroad", "Company", "Home"]
    assert sources[0] == Source("Abroad", "JPY", 20000, 65, "Confirmed")
    assert sources[2].monthly is None


def test_each_source_is_converted_and_totalled():
    text = retirement_message(
        [Source("Abroad", "JPY", 20000, 65, "Confirmed"), Source("Company", "KRW", 300000)],
        {"KRW": 1.0, "JPY": 9.25},
        DAY,
    )
    row = next(line for line in text.splitlines() if line.startswith("Abroad"))
    assert row.split() == ["Abroad", "JPY", "20,000", "185,000", "65"]
    assert "TOTAL" in text and "485,000" in text
    assert "JPY 100 = KRW 925.0 (ECB, 2026-09-25)" in text


def test_an_unknown_amount_stays_in_the_table_and_is_named():
    """A total over what is known so far must not pass for the whole."""
    text = retirement_message(
        [Source("Abroad", "JPY", 20000, 65, "Confirmed"), Source("Home", "KRW", None)],
        {"KRW": 1.0, "JPY": 9.0},
        DAY,
    )
    row = next(line for line in text.splitlines() if line.startswith("Home"))
    assert row.split()[2:4] == ["?", "?"]
    assert "Still to look up: Home" in text


def test_without_a_rate_the_foreign_source_has_no_won_figure():
    text = retirement_message(
        [Source("Abroad", "JPY", 20000, 65, "Confirmed")],
        None,
        rate_problem="No JPY/KRW rate",
    )
    row = next(line for line in text.splitlines() if line.startswith("Abroad"))
    assert row.split()[3] == "?"
    assert "TOTAL" not in text
    assert "⚠️ No exchange rate" in text


def test_nothing_recorded_says_so():
    assert "Nothing recorded yet" in retirement_message([], None)


def rates_client(handler):
    return httpx.AsyncClient(
        base_url="https://api.frankfurter.dev/v1", transport=httpx.MockTransport(handler)
    )


async def test_rates_are_read_per_currency_into_won():
    def handler(request):
        assert request.url.params["base"] == "JPY"
        assert request.url.params["symbols"] == "KRW"
        return httpx.Response(
            200, json={"amount": 1.0, "base": "JPY", "date": "2026-09-25",
                       "rates": {"KRW": 9.25}}
        )

    rates, day = await Rates(client=rates_client(handler)).to_won({"JPY", "KRW"})
    assert rates == {"KRW": 1.0, "JPY": 9.25}
    assert day == DAY


async def test_won_alone_needs_no_lookup():
    def handler(request):
        raise AssertionError("looked up")

    assert await Rates(client=rates_client(handler)).to_won({"KRW"}) == ({"KRW": 1.0}, None)


async def test_a_failed_lookup_is_an_fx_error():
    client = rates_client(lambda request: httpx.Response(503))
    with pytest.raises(FxError, match="JPY/KRW"):
        await Rates(client=client).to_won({"JPY"})
