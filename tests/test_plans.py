"""The monthly plan check.

Every figure here is invented. The real ones live in Notion and must never
reach this repository, which is public.
"""

from __future__ import annotations

from datetime import date

import pytest

from alliegent import plans as P
from alliegent.assets import AssetService, Snapshot
from alliegent.config import Config

from .conftest import FakeNotionClient

PLANS_DS = "ds_plans-db"
ASSETS_DS = "ds_assets-db"


def plan_page(
    pid,
    month,
    *,
    total=1_000,
    liquid=600,
    locked=400,
    low=900,
    high=1_100,
    rsu=0,
    espp=0,
    dc=0,
    auto=0,
    gain=0,
    status="Tentative",
    snapshot=None,
):
    numbers = {
        "Target Total": total,
        "Target Liquid": liquid,
        "Target Locked": locked,
        "Low": low,
        "High": high,
        "RSU Vest (net)": rsu,
        "ESPP Buy": espp,
        "DC": dc,
        "Auto Invest": auto,
        "Realized Gain": gain,
    }
    props = {
        "Month": {"type": "title", "title": [{"plain_text": month, "type": "text"}]},
        "Status": {"type": "select", "select": {"name": status}},
        "Actual Snapshot": {
            "type": "relation",
            "relation": [{"id": snapshot}] if snapshot else [],
        },
    }
    for name, value in numbers.items():
        props[name] = {"type": "number", "number": value}
    return {"object": "page", "id": pid, "url": f"https://notion.so/{pid}", "properties": props}


def asset_page(pid, day, **amounts):
    names = {"stock": "Stock/Funds"}
    props = {
        "Name": {"type": "title", "title": [{"plain_text": day, "type": "text"}]},
        "Date": {"type": "date", "date": {"start": day, "end": None}},
    }
    for key, value in amounts.items():
        props[names.get(key, key.title() if key != "SNOW" else key)] = {
            "type": "number",
            "number": value,
        }
    return {"object": "page", "id": pid, "url": f"https://notion.so/{pid}", "properties": props}


def services(plan_pages, asset_pages, config=None):
    config = config or Config()
    client = FakeNotionClient({PLANS_DS: plan_pages, ASSETS_DS: asset_pages})
    return (
        client,
        P.PlanService(client, config, "plans-db"),
        AssetService(client, config, "assets-db"),
        config,
    )


def snap(day, **amounts):
    return Snapshot(f"s-{day}", date.fromisoformat(day), amounts)


def plan(month="2026-10", **overrides):
    fields = dict(
        id=f"p-{month}", month=month, target_total=1_000, target_liquid=600,
        target_locked=400, low=900, high=1_100, rsu_net=0, espp_buy=0, dc=0,
        auto_invest=0, realized_gain=0, status="Tentative", snapshot_ids=(),
    )
    fields.update(overrides)
    return P.Plan(**fields)


# -- months ----------------------------------------------------------------


def test_months_roll_over_the_year():
    assert P.previous_month(date(2027, 1, 1)) == date(2026, 12, 1)
    assert P.next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert P.month_of(date(2026, 10, 31)) == "2026-10"


def test_the_month_end_is_the_first_snapshot_on_or_after_the_1st():
    """The 1st usually has nothing on it. The next one stands in."""
    history = [snap("2026-10-26"), snap("2026-11-02"), snap("2026-11-09")]
    assert P.month_end(history, date(2026, 11, 1)).day == date(2026, 11, 2)


def test_a_snapshot_on_the_1st_is_the_month_end_itself():
    history = [snap("2026-11-01"), snap("2026-11-02")]
    assert P.month_end(history, date(2026, 11, 1)).day == date(2026, 11, 1)


def test_there_is_no_month_end_before_anything_is_recorded_after_it():
    assert P.month_end([snap("2026-10-26")], date(2026, 11, 1)) is None


# -- the three-way split ---------------------------------------------------


def test_the_parts_add_up_to_the_change_in_total():
    """The whole point of an Other line: without it, a repayment from Mom
    would be a gap nobody could account for."""
    before = snap("2026-10-05", Savings=100, **{"Stock/Funds": 200}, Vested=50,
                  Pension=80, Deposit=300, Mom=40)
    after = snap("2026-11-02", Savings=130, **{"Stock/Funds": 230}, Vested=70,
                 Pension=90, Deposit=300, Mom=30)
    parts = P.breakdown(after, before, plan(rsu_net=15, espp_buy=5, dc=7, auto_invest=9))
    assert parts.total == after.total - before.total
    assert parts.other == -10


def test_market_is_what_the_valued_holdings_did_beyond_the_inflows():
    before = snap("2026-10-05", **{"Stock/Funds": 200}, Vested=50, Pension=80)
    after = snap("2026-11-02", **{"Stock/Funds": 210}, Vested=75, Pension=90)
    # Valued holdings moved +45; the plan expected +27 to arrive.
    parts = P.breakdown(after, before, plan(rsu_net=15, espp_buy=5, dc=7))
    assert parts.inflows == 27
    assert parts.market == 18


def test_dc_is_an_inflow_not_market():
    """A pension that grew only by its contribution did not earn anything."""
    before = snap("2026-10-05", Pension=80)
    after = snap("2026-11-02", Pension=87)
    assert P.breakdown(after, before, plan(dc=7)).market == 0


def test_auto_invest_is_saving_not_market():
    """IRP and fund standing orders land in Pension and Stock/Funds. Left in
    Market, a month of doing nothing but saving would read as a good month
    for the markets."""
    before = snap("2026-10-05", **{"Stock/Funds": 200}, Pension=80)
    after = snap("2026-11-02", **{"Stock/Funds": 207}, Pension=89)
    parts = P.breakdown(after, before, plan(auto_invest=16))
    assert parts.invested == 16
    assert parts.market == 0


def test_the_report_shows_auto_invest_on_its_own_line():
    before = snap("2026-10-05", Savings=500, Pension=450)
    after = snap("2026-11-02", Savings=520, Pension=470)
    text = render(p=plan(auto_invest=20), s=after, previous=before)
    assert "Auto invest" in text and "+20" in text


# -- SNOW, the Low streak, and gains --------------------------------------


def test_snow_counts_what_was_moved_out_of_vested_too():
    """Vested alone understates it: shares sold out of Vested and kept are
    still the same company."""
    s = snap("2026-11-02", Savings=500, **{"Stock/Funds": 300}, Vested=200, SNOW=100)
    assert P.snow_share(s) == pytest.approx(0.3)


def test_three_months_under_low_make_a_streak():
    history = [snap("2026-09-01", Savings=800), snap("2026-10-01", Savings=850)]
    rows = [
        plan("2026-08", snapshot_ids=("s-2026-09-01",)),
        plan("2026-09", snapshot_ids=("s-2026-10-01",)),
        plan("2026-10"),
    ]
    assert P.low_streak(rows[2], rows, history, total=870) == 3


def test_an_unlinked_month_breaks_the_streak():
    """No evidence is not evidence of being under."""
    history = [snap("2026-09-01", Savings=800)]
    rows = [
        plan("2026-08", snapshot_ids=("s-2026-09-01",)),
        plan("2026-09"),
        plan("2026-10"),
    ]
    assert P.low_streak(rows[2], rows, history, total=870) == 1


def test_a_month_at_or_above_low_breaks_the_streak():
    history = [snap("2026-10-01", Savings=900)]
    rows = [plan("2026-09", snapshot_ids=("s-2026-10-01",)), plan("2026-10")]
    assert P.low_streak(rows[1], rows, history, total=870) == 1


def test_gains_count_this_year_up_to_this_month_only():
    rows = [
        plan("2025-12", realized_gain=500),
        plan("2026-03", realized_gain=100),
        plan("2026-09", realized_gain=200),
        plan("2026-11", realized_gain=900),
    ]
    assert P.gains_this_year(rows[2], rows) == 300


# -- the job ---------------------------------------------------------------


async def test_nothing_is_reported_before_the_month_end_snapshot_exists():
    _, plans, assets, config = services(
        [plan_page("p1", "2026-10")], [asset_page("a1", "2026-10-26", savings=900)]
    )
    assert await P.monthly_check(plans, assets, date(2026, 11, 3), config) is None


async def test_the_1st_asks_for_the_snapshot_when_there_is_none():
    _, plans, assets, config = services(
        [plan_page("p1", "2026-10")], [asset_page("a1", "2026-10-26", savings=900)]
    )
    text = await P.monthly_check(plans, assets, date(2026, 11, 1), config)
    assert "2026-10 is over" in text


async def test_the_row_is_linked_to_the_nearest_snapshot_after_the_1st():
    client, plans, assets, config = services(
        [plan_page("p1", "2026-10")],
        [
            asset_page("a0", "2026-10-05", savings=880),
            asset_page("a1", "2026-11-02", savings=950),
            asset_page("a2", "2026-11-09", savings=990),
        ],
    )
    text = await P.monthly_check(plans, assets, date(2026, 11, 2), config)
    assert ("p1", {"Actual Snapshot": {"relation": [{"id": "a1"}]}}) in client.updated
    assert "Plan check — 2026-10" in text
    assert "Since 2026-10-05" in text


async def test_a_linked_month_is_not_reported_twice():
    """Linking is the record that it went out. The job runs every day."""
    client, plans, assets, config = services(
        [plan_page("p1", "2026-10", snapshot="a1")],
        [asset_page("a1", "2026-11-02", savings=950)],
    )
    assert await P.monthly_check(plans, assets, date(2026, 11, 3), config) is None
    assert client.updated == []


async def test_a_preview_writes_nothing_and_shows_even_a_linked_month():
    client, plans, assets, config = services(
        [plan_page("p1", "2026-10", snapshot="a1")],
        [asset_page("a1", "2026-11-02", savings=950)],
    )
    text = await P.monthly_check(plans, assets, date(2026, 11, 3), config, commit=False)
    assert "Plan check — 2026-10" in text
    assert client.updated == []


async def test_the_first_month_has_nothing_to_compare_with():
    _, plans, assets, config = services(
        [plan_page("p1", "2026-10")], [asset_page("a1", "2026-11-02", savings=950)]
    )
    text = await P.monthly_check(plans, assets, date(2026, 11, 2), config)
    assert "No earlier month-end snapshot" in text


async def test_the_third_month_under_low_marks_the_row_revised():
    client, plans, assets, config = services(
        [
            plan_page("p8", "2026-08", snapshot="a8"),
            plan_page("p9", "2026-09", snapshot="a9"),
            plan_page("p10", "2026-10"),
        ],
        [
            asset_page("a8", "2026-09-01", savings=800),
            asset_page("a9", "2026-10-01", savings=850),
            asset_page("a10", "2026-11-02", savings=870),
        ],
    )
    text = await P.monthly_check(plans, assets, date(2026, 11, 2), config)
    assert ("p10", {"Status": {"select": {"name": "Revised"}}}) in client.updated
    assert "Under Low 3 months in a row" in text
    assert "marked Revised" in text


async def test_a_row_already_revised_is_not_written_again():
    client, plans, assets, config = services(
        [
            plan_page("p8", "2026-08", snapshot="a8"),
            plan_page("p9", "2026-09", snapshot="a9"),
            plan_page("p10", "2026-10", status="Revised"),
        ],
        [
            asset_page("a8", "2026-09-01", savings=800),
            asset_page("a9", "2026-10-01", savings=850),
            asset_page("a10", "2026-11-02", savings=870),
        ],
    )
    await P.monthly_check(plans, assets, date(2026, 11, 2), config)
    assert not any("Status" in props for _, props in client.updated)


async def test_a_month_outside_the_plan_says_nothing():
    _, plans, assets, config = services(
        [plan_page("p1", "2026-10")], [asset_page("a1", "2026-10-02", savings=950)]
    )
    assert await P.monthly_check(plans, assets, date(2026, 10, 2), config) is None


async def test_a_row_whose_title_is_not_a_month_is_left_out():
    _, plans, _, _ = services(
        [plan_page("p1", "2026-10"), plan_page("p2", "notes")], []
    )
    assert [p.month for p in await plans.all_plans()] == ["2026-10"]


# -- the message -----------------------------------------------------------


def render(p=None, s=None, previous=None, *, streak=0, year_gains=0, revised=False):
    p = p or plan()
    s = s or snap("2026-11-02", Savings=500, Pension=450)
    return P.report_message(
        p, s, previous, streak=streak, year_gains=year_gains, config=Config(), revised=revised
    )


def test_the_report_shows_each_half_against_its_target():
    text = render()
    assert "Liquid" in text and "Locked" in text and "Total" in text
    # 500 of 600, 450 of 400, 950 of 1,000.
    assert "83%" in text and "112%" in text and "95%" in text


def test_the_range_is_placed_between_low_and_high():
    assert "Range: 25% of the way from Low to High" in render()


def test_under_low_is_said_in_won():
    text = render(s=snap("2026-11-02", Savings=850))
    assert "Range: under Low by ₩50" in text


def test_snow_over_the_cap_is_a_warning():
    # 80 of 180 Liquid.
    text = render(s=snap("2026-11-02", Savings=100, Vested=80))
    assert "⚠️ SNOW is 44% of Liquid · cap 30% — sell down at the next vest." in text


def test_snow_under_the_cap_is_stated_without_alarm():
    text = render(s=snap("2026-11-02", Savings=900, Vested=100))
    assert "SNOW is 10% of Liquid · cap 30%" in text
    assert "⚠️ SNOW" not in text


def test_a_vesting_month_reminds_about_the_tax_sale():
    assert "35–40% was sold" in render(p=plan(rsu_net=10))
    assert "35–40%" not in render(p=plan(rsu_net=0))


def test_gains_are_shown_against_the_allowance_in_a_month_with_a_sale():
    text = render(p=plan(realized_gain=300), year_gains=1_200_000)
    assert "₩1,200,000 of ₩2,500,000 · ₩1,300,000 left" in text


def test_gains_over_the_allowance_are_a_warning():
    text = render(p=plan(realized_gain=300), year_gains=2_700_000)
    assert "⚠️" in text and "₩200,000 over" in text


def test_a_month_without_a_sale_says_nothing_about_gains():
    assert "Foreign-stock" not in render(p=plan(realized_gain=0), year_gains=900)


def test_a_streak_short_of_the_limit_is_counted_out_loud():
    assert "Under Low this month (2 of 3 before revising)" in render(streak=2)


# -- SNOW rides along with a rollover --------------------------------------


async def test_a_rollover_carries_snow_into_its_row():
    """The bonus moves on the 1st, and that row can be the month's end."""
    client = FakeNotionClient(
        {ASSETS_DS: [asset_page("a1", "2026-09-28", savings=100, bonus=50, SNOW=30)]}
    )
    service = AssetService(client, Config(), "assets-db")
    await service.roll_bonus_into_savings(date(2026, 10, 1))
    _, props = client.created[0]
    assert props["SNOW"] == {"number": 30}


async def test_a_rollover_without_snow_does_not_write_the_column():
    """So a database that has not grown the column yet still takes the row."""
    client = FakeNotionClient({ASSETS_DS: [asset_page("a1", "2026-09-28", savings=100, bonus=50)]})
    service = AssetService(client, Config(), "assets-db")
    await service.roll_bonus_into_savings(date(2026, 10, 1))
    _, props = client.created[0]
    assert "SNOW" not in props
