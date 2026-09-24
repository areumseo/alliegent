"""Karrot listings: what gets a number, and what counts as waiting.

The numbers printed by /karrot list are typed into /karrot sold and friends,
which recompute the list — so the ordering has to be deterministic and the
membership rule has to be one anyone can predict.
"""

from __future__ import annotations

from datetime import date

from alliegent import karrot as K
from alliegent.config import Config

from .conftest import FakeNotionClient

DS = "ds_karrot-db"
TODAY = date(2026, 9, 11)


def page(pid, name, *, status="Listed", paid=False, note="", price=1000):
    props = {
        "Name": {"type": "title", "title": [{"plain_text": name, "type": "text"}]},
        "Price": {"type": "number", "number": price},
        "Status": {"type": "select", "select": {"name": status}},
        "Paid": {"type": "checkbox", "checkbox": paid},
    }
    if note:
        props["Note"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": note, "type": "text"}],
        }
    return {
        "object": "page",
        "id": pid,
        "url": f"https://notion.so/{pid}",
        "in_trash": False,
        "created_time": "2026-08-01T00:00:00.000Z",
        "properties": props,
    }


def service(pages=None):
    client = FakeNotionClient({DS: pages or []})
    return client, K.KarrotService(client, Config(), "karrot-db")


async def test_a_not_listed_item_still_gets_a_number():
    """It is not on sale, but putting it up is the work — dropping it from the
    numbered list would hide the one thing left to do with it."""
    _, svc = service([page("p1", "아직 안 올린 것", status="Not listed")])
    assert [i.name for i in await svc.open_items(TODAY)] == ["아직 안 올린 것"]


async def test_selling_stamps_the_date_once():
    client, svc = service([page("p1", "팔린 것")])
    item = (await svc.open_items(TODAY))[0]
    await svc.mark_sold(item, TODAY)
    assert client.updated[0][1]["Sold At"]["date"]["start"] == TODAY.isoformat()


async def test_selling_again_leaves_the_original_date():
    """Changing the status twice must not move the sale to today."""
    sold = page("p1", "이미 팔린 것", status="Sold")
    sold["properties"]["Sold At"] = {"type": "date", "date": {"start": "2026-08-01"}}
    client, svc = service([sold])
    item = K.Item(
        id="p1", name="x", price=1, status="Sold", paid=False,
        sold_at=date(2026, 8, 1), category=None,
    )
    await svc.mark_sold(item, TODAY)
    assert "Sold At" not in client.updated[0][1]


async def test_sold_and_paid_items_leave_the_numbered_list():
    """Read them with /karrot list Sold; there is nothing left to do to them."""
    _, svc = service([page("p1", "끝난 것", status="Sold", paid=True)])
    assert await svc.open_items(TODAY) == []


async def test_an_unpaid_sale_stays_in_the_list_but_sorts_last():
    _, svc = service(
        [
            page("p1", "안 팔린 것"),
            page("p2", "미입금", status="Sold", paid=False),
        ]
    )
    assert [i.name for i in await svc.open_items(TODAY)] == ["안 팔린 것", "미입금"]


async def test_an_unknown_category_is_refused_with_the_list():
    _, svc = service()
    try:
        await svc.add("x", 1000, TODAY, category="의류")
    except ValueError as exc:
        assert "Clothing" in str(exc)
    else:
        raise AssertionError("a category that is not in the database must be refused")


# -- Sent: posted, not yet done --------------------------------------------


async def test_a_sent_item_still_needs_following_up():
    """It keeps a number: the sale is not finished until it is marked sold."""
    _, svc = service([page("p1", "보냄", status="Sent")])
    assert [i.name for i in await svc.open_items(TODAY)] == ["보냄"]


async def test_marking_sent_leaves_the_dates_alone():
    """Sending is not selling: Sold At belongs to the completed sale."""
    client, svc = service([page("p1", "예약된 것", status="Reserved")])
    item = (await svc.open_items(TODAY))[0]
    await svc.mark_sent(item)
    written = client.updated[0][1]
    assert written["Status"]["select"]["name"] == "Sent"
    assert "Sold At" not in written


async def test_the_list_says_which_stage_an_item_is_at():
    _, svc = service(
        [
            page("p1", "후보", status="Not listed"),
            page("p2", "예약", status="Reserved"),
            page("p3", "발송", status="Sent"),
        ]
    )
    text = K.item_list(await svc.open_items(TODAY), TODAY)
    assert "candidate" in text and "reserved" in text and "sent" in text


def test_the_statuses_follow_the_order_a_sale_moves_through():
    assert K.STATUSES == ("Not listed", "Listed", "Reserved", "Sent", "Sold")


# -- revenue ---------------------------------------------------------------


def sold(pid, name, price, sold_at=None, paid=True):
    p = page(pid, name, status="Sold", paid=paid, price=price)
    if sold_at:
        p["properties"]["Sold At"] = {"type": "date", "date": {"start": sold_at}}
    return p


async def test_sales_are_counted_into_the_week_they_happened():
    """Weeks run Monday to Sunday; 2026-09-11 is the Friday of this one."""
    _, svc = service([sold("p1", "이번주", 10000, "2026-09-11")])
    data = await svc.sales(TODAY)
    assert data["periods"]["this_week"] == (1, 10000)
    assert data["periods"]["last_week"] == (0, 0)


async def test_last_week_is_its_own_bucket():
    _, svc = service([sold("p1", "지난주", 5000, "2026-09-03")])
    data = await svc.sales(TODAY)
    assert data["periods"]["last_week"] == (1, 5000)
    assert data["periods"]["this_week"] == (0, 0)


async def test_months_and_years_accumulate():
    _, svc = service(
        [
            sold("p1", "9월", 1000, "2026-09-02"),
            sold("p2", "8월", 2000, "2026-08-20"),
            sold("p3", "작년", 4000, "2025-12-31"),
        ]
    )
    data = await svc.sales(TODAY)
    assert data["periods"]["this_month"] == (1, 1000)
    assert data["periods"]["last_month"] == (1, 2000)
    assert data["periods"]["this_year"] == (2, 3000)
    assert data["total"] == (3, 7000)


async def test_undated_sales_are_counted_apart_rather_than_dropped():
    """109 sales had no Sold At when this was written. Reporting ₩0 for the
    month without saying why would read as a month with no sales."""
    _, svc = service([sold("p1", "날짜없음", 9000)])
    data = await svc.sales(TODAY)
    assert data["periods"]["this_month"] == (0, 0)
    assert data["undated"] == (1, 9000)
    assert data["total"] == (1, 9000)
    assert "have no date" in K.sales_message(data, TODAY)


async def test_an_unpaid_sale_still_counts_as_revenue():
    """The item is gone and the price is settled; the money not having arrived
    is a separate fact, and netting it out would hide the sale."""
    _, svc = service([sold("p1", "미입금", 3000, "2026-09-11", paid=False)])
    data = await svc.sales(TODAY)
    assert data["periods"]["this_week"] == (1, 3000)
    assert data["unpaid"] == (1, 3000)
    assert "unpaid" in K.sales_message(data, TODAY)


async def test_items_still_for_sale_are_not_revenue():
    _, svc = service([page("p1", "안 팔림", status="Listed", price=99000)])
    data = await svc.sales(TODAY)
    assert data["total"] == (0, 0)


async def test_a_clean_report_says_nothing_about_gaps():
    _, svc = service([sold("p1", "정상", 1000, "2026-09-11")])
    text = K.sales_message(await svc.sales(TODAY), TODAY)
    assert "have no date" not in text
    assert "unpaid" not in text.lower()


# -- the two weekly reports -------------------------------------------------


async def test_the_saturday_report_counts_the_week_and_the_running_total():
    _, svc = service(
        [sold("p1", "이번주", 10000, "2026-09-11"), sold("p2", "옛날", 5000, "2026-01-05")]
    )
    text = K.weekly_message(await svc.sales(TODAY), [], SUNDAY)
    assert "This week  1  10,000" in text
    assert "All time   2  15,000" in text


async def test_a_week_with_nothing_sold_and_nothing_owed_stays_silent():
    """A weekly report that says 0건 every week is one you stop opening."""
    _, svc = service([sold("p1", "옛날", 5000, "2026-01-05")])
    assert K.weekly_message(await svc.sales(TODAY), [], SUNDAY) is None


async def test_a_quiet_week_still_reports_money_owed():
    """Nothing sold is not nothing to do when someone owes you."""
    _, svc = service([sold("p1", "미입금", 3000, "2026-01-05", paid=False)])
    text = K.weekly_message(await svc.sales(TODAY), await svc.unpaid(), TODAY)
    assert text is not None and "Unpaid" in text


async def test_monday_lists_what_is_waiting_to_be_listed():
    _, svc = service(
        [
            page("p1", "후보1", status="Not listed", price=5000),
            page("p2", "이미 올림", status="Listed"),
        ]
    )
    text = K.candidates_message(await svc.all_items())
    assert "후보1" in text and "이미 올림" not in text
    assert "1 · ₩5,000" in text


async def test_monday_says_nothing_when_there_are_no_candidates():
    _, svc = service([page("p1", "이미 올림", status="Listed")])
    assert K.candidates_message(await svc.all_items()) is None


async def test_a_note_is_shown_beside_the_item():
    """The note is context the user wrote for themselves; a list that hides it
    sends them back to Notion to find out why an item is flagged."""
    _, svc = service([page("p1", "물건", note="어머니한테 판 것")])
    text = K.item_list(await svc.open_items(TODAY), TODAY)
    assert "어머니한테 판 것" in text


# -- selling costs ---------------------------------------------------------
# Ads and packaging come off revenue: an item that sold because an ad pushed
# it did not earn its whole price. History before 2026-09-20 is two aggregate
# rows, because the per-item receipts were never kept.

def expense(amount, kind=K.ADS, spent_at=None, name="ad"):
    return K.Expense(id=name, name=name, kind=kind, amount=amount, spent_at=spent_at)


SUNDAY = date(2026, 9, 20)


def test_costs_land_in_the_periods_they_were_spent_in():
    data = K.spending_of(
        [
            expense(2_960, spent_at=date(2026, 9, 20)),
            expense(224_011, spent_at=date(2026, 8, 15)),
            expense(63_800, K.PACKAGING, spent_at=date(2026, 8, 15)),
        ],
        SUNDAY,
    )
    assert data["periods"]["this_week"] == 2_960
    assert data["periods"]["this_month"] == 2_960
    assert data["periods"]["last_month"] == 287_811
    assert data["periods"]["this_year"] == 290_771
    assert data["total"] == 290_771


def test_costs_are_split_by_kind():
    data = K.spending_of([expense(1_000), expense(500, K.PACKAGING)], SUNDAY)
    assert data["by_kind"] == {K.ADS: 1_000, K.PACKAGING: 500}


def test_an_undated_cost_still_counts_towards_the_total():
    """The same rule the sales side uses: money that fits no period is in the
    total and named, not quietly dropped."""
    data = K.spending_of([expense(5_000, spent_at=None)], SUNDAY)
    assert data["total"] == 5_000
    assert data["undated"] == 5_000
    assert data["periods"]["this_year"] == 0


def test_the_weekly_report_nets_the_week_and_the_total():
    data = {
        "week_start": date(2026, 9, 14),
        "periods": {
            "this_week": (2, 30_000),
            "last_week": (0, 0),
            "this_month": (2, 30_000),
            "this_year": (2, 30_000),
        },
        "total": (2, 30_000),
        "undated": (0, 0),
        "unpaid": (0, 0),
    }
    spending = K.spending_of([expense(2_960, spent_at=date(2026, 9, 20))], SUNDAY)
    text = K.weekly_message(data, [], SUNDAY, spending)
    # The cost is one line under the table, not a column repeated per row:
    # it is the same few purchases being divided up over and over.
    assert "This week  2  30,000 27,040" in text
    # Named once: in this week the cost and the all-time cost are the same
    # figure, and printing it twice reads as two separate costs.
    assert "_Net is after ₩2,960 of ads and packaging._" in text
    assert text.count("2,960") == 1


def test_revenue_stays_gross_without_an_expense_database():
    """The database is optional, and a bot with no costs recorded must not
    start reporting a net that is just the gross under another name."""
    data = {
        "week_start": date(2026, 9, 14),
        "periods": {
            "this_week": (1, 10_000),
            "last_week": (0, 0),
            "this_month": (1, 10_000),
            "this_year": (1, 10_000),
        },
        "total": (1, 10_000),
        "undated": (0, 0),
        "unpaid": (0, 0),
    }
    text = K.weekly_message(data, [], SUNDAY)
    assert "Net" not in text
    assert "This week  1  10,000" in text


def test_both_costs_are_named_when_the_week_is_only_part_of_the_spending():
    data = {
        "week_start": date(2026, 9, 14),
        "periods": {
            "this_week": (1, 50_000),
            "last_week": (0, 0),
            "this_month": (1, 50_000),
            "this_year": (1, 50_000),
        },
        "total": (1, 50_000),
        "undated": (0, 0),
        "unpaid": (0, 0),
    }
    spending = K.spending_of(
        [
            expense(2_960, spent_at=date(2026, 9, 20)),
            expense(287_811, spent_at=date(2026, 8, 15)),
        ],
        SUNDAY,
    )
    text = K.weekly_message(data, [], SUNDAY, spending)
    assert "₩2,960 this week, ₩290,771 all time" in text


# -- buying on Karrot ------------------------------------------------------
# Money spent buying shares the expenses database but never the Net: packaging
# is what a sale cost to make, a purchase is not, and one Net covering both
# would move without saying which half moved it.


def week_of(revenue: int) -> dict:
    return {
        "week_start": date(2026, 9, 14),
        "periods": {
            "this_week": (1, revenue),
            "last_week": (0, 0),
            "this_month": (1, revenue),
            "this_year": (1, revenue),
        },
        "total": (1, revenue),
        "undated": (0, 0),
        "unpaid": (0, 0),
    }


def test_a_purchase_never_reaches_net():
    """The whole reason it is a separate kind. A purchase counted as a selling
    cost would read as a week that sold badly."""
    spending = K.spending_of(
        [
            expense(2_000, K.PACKAGING, spent_at=SUNDAY),
            expense(50_000, K.PURCHASE, spent_at=SUNDAY, name="산 것"),
        ],
        SUNDAY,
    )
    assert spending["periods"]["this_week"] == 2_000
    assert spending["total"] == 2_000
    assert spending["purchases"]["total"] == 50_000
    text = K.weekly_message(week_of(30_000), [], SUNDAY, spending)
    assert "This week  1  30,000 28,000" in text


def test_purchases_are_reported_on_their_own_line():
    spending = K.spending_of(
        [expense(50_000, K.PURCHASE, spent_at=SUNDAY, name="산 것")], SUNDAY
    )
    text = K.weekly_message(week_of(30_000), [], SUNDAY, spending)
    assert "Bought on Karrot: ₩50,000 this week, ₩50,000 all time" in text
    assert "Not in Net" in text


def test_a_week_with_no_purchases_says_nothing_about_buying():
    """A line reading ₩0 every week is one you stop seeing."""
    spending = K.spending_of([expense(2_000, K.PACKAGING, spent_at=SUNDAY)], SUNDAY)
    assert "Bought on Karrot" not in K.weekly_message(week_of(30_000), [], SUNDAY, spending)


def test_purchases_keep_their_own_periods():
    spending = K.spending_of(
        [
            expense(10_000, K.PURCHASE, spent_at=SUNDAY, name="이번 주"),
            expense(70_000, K.PURCHASE, spent_at=date(2026, 8, 15), name="지난달"),
        ],
        SUNDAY,
    )
    bought = spending["purchases"]
    assert bought["periods"]["this_week"] == 10_000
    assert bought["periods"]["last_month"] == 70_000
    assert bought["total"] == 80_000
    # And none of it is anywhere near the selling costs.
    assert spending["total"] == 0


def test_by_kind_stays_about_selling():
    spending = K.spending_of(
        [
            expense(1_000),
            expense(500, K.PACKAGING),
            expense(9_000, K.PURCHASE, spent_at=SUNDAY, name="산 것"),
        ],
        SUNDAY,
    )
    assert spending["by_kind"] == {K.ADS: 1_000, K.PACKAGING: 500}
    assert spending["purchases"]["by_kind"] == {K.PURCHASE: 9_000}


def test_a_purchase_is_a_kind_the_database_accepts():
    """`add` validates against KINDS, so leaving Purchase out of it would make
    /karrot bought refuse every row it was given."""
    assert K.PURCHASE in K.KINDS and K.PURCHASE not in K.SELLING_KINDS
