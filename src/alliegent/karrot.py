"""당근 판매 목록. Notion의 🥕 Karrot 데이터베이스를 읽고 쓴다.

The Karrot channel is the one surface whose *messages* are Korean. Item names
are copied from a Korean marketplace and translating one would report on
something the reader cannot search for. The command picker and the fixed
choices (Status, Category) are English, because those are interface rather
than data -- Status always was, and Category was renamed to match.

The logic here is ported from a standalone script rather than written fresh,
and its judgement calls are kept -- they are the useful part:

- `Sold At` is stamped only on the first move into Sold, so changing the
  status twice does not push the sale date to today.
- Idleness counts from `Bumped` when there is one, else `Listed At`: bumping
  an item restarts its clock, which is what bumping is for.
- Items with no date at all are reported separately rather than as stale.
  Nineteen migrated rows have no `Listed At`, and letting them appear as
  stale every morning is how a daily alert becomes something you ignore.

What changed in the port: this goes through the shared NotionClient (one HTTP
stack, one API version) instead of its own aiohttp calls against the 2022 API,
and items are addressed by number the way the rest of the bot works, rather
than by name -- a name lookup fails on "여러 건이 걸립니다" exactly when the
list is long enough to need the help.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

# Not listed is a real state, not a missing one: the item is here, decided on,
# and not yet put up for sale. It cannot go stale -- nothing is waiting on a
# buyer -- but it is still work, so it keeps a number.
NOT_LISTED = "Not listed"
LISTED = "Listed"
RESERVED = "Reserved"
SENT = "Sent"
SOLD = "Sold"
# The order a sale actually moves through. Not listed sits before it: those are
# candidates, things being considered rather than offered.
STATUSES = (NOT_LISTED, LISTED, RESERVED, SENT, SOLD)

# Listed At and Bumped were dropped from the database on 2026-09-13: this is a
# record of what sold for how much, not of how long a listing sat. Staleness
# went with them -- it was measured from those dates and nothing else could
# stand in for them.

# Renamed from Korean on 2026-09-11, to match Status, which was always
# English. The split is by kind, not language: fixed choices are interface,
# item names are data and stay in the language they were listed in.
CATEGORIES = (
    "Games",
    "Clothing",
    "Electronics",
    "Ballet",
    "Beauty",
    "Stationery",
    "Accessories",
    "Other",
)


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    price: int | None
    status: str | None
    paid: bool
    sold_at: date | None
    category: str | None
    note: str = ""

    @property
    def needs_attention(self) -> bool:
        """번호가 붙는 대상: 아직 안 팔렸거나, 팔렸는데 입금이 안 된 것.

        하나의 번호 체계로 묶는 이유는, 손댈 일이 있는 항목이 정확히 이것들이기
        때문이다. 팔리고 입금까지 끝난 건은 읽을 일은 있어도 고칠 일이 없다.
        """
        return self.status != SOLD or not self.paid

    @property
    def won(self) -> str:
        return f"₩{self.price:,}" if self.price is not None else "가격 미정"


class KarrotService:
    def __init__(self, client: NotionClient, config: Config, db_id: str) -> None:
        self._client = client
        self._cfg = config
        self._db_id = db_id
        self._ds_id: str | None = None

    async def data_source_id(self) -> str:
        if self._ds_id is None:
            self._ds_id = await self._client.resolve_data_source(self._db_id)
        return self._ds_id

    def _to_item(self, page: dict) -> Item:
        return Item(
            id=page["id"],
            name=n.read_title(page, "Name") or "(무제)",
            price=n.read_number(page, "Price"),
            status=n.read_select(page, "Status"),
            paid=n.read_checkbox(page, "Paid"),
            sold_at=n.read_date(page, "Sold At"),
            category=n.read_select(page, "Category"),
            note=n.read_text(page, "Note"),
        )

    # -- 조회 --------------------------------------------------------------

    async def all_items(self) -> list[Item]:
        ds = await self.data_source_id()
        return [self._to_item(page) async for page in self._client.query(ds)]

    async def open_items(self, today: date) -> list[Item]:
        """번호가 붙는 목록. 오래 방치된 것부터 위로 온다.

        정렬이 결정적이어야 하는 이유는 어젠다 쪽과 같다: 한 메시지에서 읽은
        번호를 다른 명령에 입력하는데, 그 명령은 목록을 다시 계산한다.
        """
        items = [i for i in await self.all_items() if i.needs_attention]
        return sorted(
            items,
            key=lambda i: (
                i.status == SOLD,  # 미입금 건은 뒤로 -- 팔린 건 이미 손을 떠났다
                i.status == NOT_LISTED,  # 아직 안 올린 건 팔리는 중인 것 다음
                i.name,
            ),
        )

    async def by_status(self, status: str) -> list[Item]:
        if status not in STATUSES:
            raise ValueError(f"status는 {' / '.join(STATUSES)} 중 하나여야 합니다.")
        items = [i for i in await self.all_items() if i.status == status]
        return sorted(items, key=lambda i: i.name)

    # -- 쓰기 --------------------------------------------------------------

    async def add(
        self, name: str, price: int, today: date, category: str | None = None
    ) -> Item:
        """새 매물 등록. Status는 Listed, 등록일은 오늘."""
        if category and category not in CATEGORIES:
            raise ValueError(f"카테고리는 {' / '.join(CATEGORIES)} 중 하나여야 합니다.")
        props = {
            "Name": n.title(name),
            "Price": n.number(price),
            "Status": n.select(LISTED),
            "Paid": n.checkbox(False),
        }
        if category:
            props["Category"] = n.select(category)
        page = await self._client.create_page(await self.data_source_id(), props)
        return self._to_item(page)

    async def mark_sold(self, item: Item, today: date, paid: bool = False) -> None:
        props = {"Status": n.select(SOLD), "Paid": n.checkbox(paid)}
        # 판매일은 처음 Sold가 될 때만 찍는다. 상태를 두 번 바꿔도 날짜가 오늘로
        # 밀리지 않게 하려는 것.
        if item.sold_at is None:
            props["Sold At"] = n.date_prop(today)
        await self._client.update_page(item.id, props)

    async def mark_sent(self, item: Item) -> None:
        """발송함. 아직 거래완료는 아니고, 상대가 받기를 기다리는 상태."""
        await self._client.update_page(item.id, {"Status": n.select(SENT)})

    async def mark_paid(self, item: Item, paid: bool = True) -> None:
        await self._client.update_page(item.id, {"Paid": n.checkbox(paid)})

    async def set_status(self, item: Item, status: str, today: date) -> None:
        if status not in STATUSES:
            raise ValueError(f"status는 {' / '.join(STATUSES)} 중 하나여야 합니다.")
        if status == SOLD:
            await self.mark_sold(item, today)
            return
        await self._client.update_page(item.id, {"Status": n.select(status)})

    # -- 집계 --------------------------------------------------------------

    async def unpaid(self) -> list[Item]:
        """팔렸는데 아직 돈이 안 들어온 건."""
        items = await self.all_items()
        return sorted(
            (i for i in items if i.status == SOLD and not i.paid), key=lambda i: i.name
        )

    async def sales(self, today: date) -> dict:
        """팔린 금액을 기간별로. 날짜 없는 건은 따로 센다.

        기간 합계에서 빠진 돈을 숨기지 않는 게 요점이다. Sold At이 비어 있으면
        어느 주에도 어느 달에도 넣을 수 없는데, 그런 건이 109건 있는 상태에서
        "이번 달 ₩0"만 보여주면 매출이 없는 것처럼 읽힌다.
        """
        items = await self.all_items()
        sold = [i for i in items if i.status == SOLD]
        dated = [i for i in sold if i.sold_at]
        undated = [i for i in sold if not i.sold_at]

        week_start = today - timedelta(days=today.weekday())
        last_week = week_start - timedelta(days=7)
        month_start = today.replace(day=1)
        last_month_end = month_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        def between(start: date, end: date) -> list[Item]:
            return [i for i in dated if i.sold_at and start <= i.sold_at <= end]

        def money(rows: list[Item]) -> int:
            return sum(i.price or 0 for i in rows)

        periods = {
            "this_week": between(week_start, today),
            "last_week": between(last_week, week_start - timedelta(days=1)),
            "this_month": between(month_start, today),
            "last_month": between(last_month_start, last_month_end),
            "this_year": between(today.replace(month=1, day=1), today),
        }
        # 미입금은 이미 sold에 포함돼 있다. 빼지 않고 따로 알린다 -- 물건은
        # 나갔고 금액도 확정됐으니 매출이 맞고, 아직 안 들어온 것은 별개다.
        unpaid = [i for i in sold if not i.paid]
        return {
            "week_start": week_start,
            "periods": {name: (len(rows), money(rows)) for name, rows in periods.items()},
            "total": (len(sold), money(sold)),
            "undated": (len(undated), money(undated)),
            "unpaid": (len(unpaid), money(unpaid)),
        }

    async def summary(self, today: date) -> dict:
        items = await self.all_items()
        sold = [i for i in items if i.status == SOLD]
        listed = [i for i in items if i.status == LISTED]
        reserved = [i for i in items if i.status == RESERVED]
        sent = [i for i in items if i.status == SENT]
        waiting = [i for i in items if i.status == NOT_LISTED]
        unpaid = [i for i in sold if not i.paid]
        month_start = today.replace(day=1)
        this_month = [i for i in sold if i.sold_at and i.sold_at >= month_start]

        def total(rows: list[Item]) -> int:
            return sum(i.price or 0 for i in rows)

        return {
            "total": len(items),
            "sold": len(sold),
            "sold_amount": total(sold),
            "listed": len(listed),
            "listed_amount": total(listed),
            "reserved": len(reserved),
            "sent": len(sent),
            "sent_amount": total(sent),
            "not_listed": len(waiting),
            "not_listed_amount": total(waiting),
            "unpaid": len(unpaid),
            "unpaid_amount": total(unpaid),
            "month": len(this_month),
            "month_amount": total(this_month),
        }


# -- 메시지 ----------------------------------------------------------------
#
# 이 파일에만 한국어 출력이 모여 있다. reports.py는 영어 전용으로 두고, 언어가
# 갈리는 지점을 파일 경계와 맞춰 둔 것.


def _line(index: int, item: Item) -> str:
    bits = [item.won]
    if item.status == NOT_LISTED:
        bits.append("후보")
    if item.status == RESERVED:
        bits.append("예약중")
    if item.status == SENT:
        bits.append("발송함")
    if item.status == SOLD and not item.paid:
        bits.append("⚠️ 미입금")
    if item.note:
        # 메모는 본인이 적어둔 맥락이라 짧게라도 보이는 게 낫다.
        note = item.note if len(item.note) <= 40 else item.note[:39] + "…"
        bits.append(note)
    return f"`{index}.` {item.name} — {' · '.join(bits)}"


def item_list(items: list[Item], today: date, *, title: str = "판매 중") -> str:
    if not items:
        return "등록된 매물이 없습니다."
    lines = [f"🥕 **{title} ({len(items)}건)**"]
    lines += [_line(i, item) for i, item in enumerate(items, start=1)]
    lines.append("_`/karrot sold <번호>` · `/karrot sent <번호>` · `/karrot paid <번호>`_")
    return "\n".join(lines)


def read_only_list(items: list[Item], today: date, *, title: str) -> str:
    """번호 없는 목록. 손댈 수 없는 것에 번호를 붙이면 다른 목록의 번호와
    헷갈리기만 한다."""
    if not items:
        return f"{title}: 해당하는 매물이 없습니다."
    lines = [f"🥕 **{title} ({len(items)}건)**"]
    for item in items:
        lines.append(f"• {item.name} — {item.won}")
    return "\n".join(lines)


def weekly_message(data: dict, unpaid: list[Item], today: date) -> str | None:
    """일요일 주간 매출. 판 것도 없고 받을 것도 없으면 None.

    아침마다 정체 매물을 세던 자리를 대신한다. 등록일이 사라지면서 정체라는
    개념 자체가 없어졌고, 이 데이터베이스가 답하는 질문은 이제 하나다 --
    이번 주에 얼마 팔렸나.
    """
    count, amount = data["periods"]["this_week"]
    if not count and not unpaid:
        return None

    start = data["week_start"]
    end = start + timedelta(days=6)
    out = [
        f"🥕 **주간 매출 — {start.month}/{start.day}–{end.month}/{end.day}**",
        "",
        f"이번 주 {count}건 · ₩{amount:,}",
    ]
    last_count, last_amount = data["periods"]["last_week"]
    if last_count or last_amount:
        diff = amount - last_amount
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "-")
        out.append(f"지난 주 {last_count}건 · ₩{last_amount:,}  ({arrow} ₩{abs(diff):,})")
    month_count, month_amount = data["periods"]["this_month"]
    out.append(f"이번 달 {month_count}건 · ₩{month_amount:,}")
    year_count, year_amount = data["periods"]["this_year"]
    out.append(f"올해 {year_count}건 · ₩{year_amount:,}")
    total_count, total_amount = data["total"]
    out.append(f"누적 {total_count}건 · ₩{total_amount:,}")

    undated_count, undated_amount = data["undated"]
    if undated_count:
        out.append(f"_판매일이 비어 기간에 못 넣은 건 {undated_count}건 · ₩{undated_amount:,}_")

    if unpaid:
        total = sum(i.price or 0 for i in unpaid)
        out += ["", f"**미입금 {len(unpaid)}건 · ₩{total:,}**"]
        out += [f"• {i.name} — {i.won}" for i in unpaid]
    return "\n".join(out)


def sales_message(data: dict, today: date) -> str:
    """`/karrot sales`. 기간별로 나누고, 어느 기간에도 못 넣은 돈을 밝힌다."""

    def line(label: str, pair: tuple[int, int]) -> str:
        count, amount = pair
        return f"{label:<12} {count:>3}건 · ₩{amount:,}"

    week_end = data["week_start"] + timedelta(days=6)
    p = data["periods"]
    out = [
        f"🥕 **매출 — {today.month}월 {today.day}일 기준**",
        "```",
        line("이번 주", p["this_week"]),
        line("지난 주", p["last_week"]),
        line("이번 달", p["this_month"]),
        line("지난 달", p["last_month"]),
        line("올해", p["this_year"]),
        line("누적", data["total"]),
        "```",
        f"_이번 주: {data['week_start'].month}/{data['week_start'].day}"
        f"–{week_end.month}/{week_end.day}_",
    ]
    undated_count, undated_amount = data["undated"]
    if undated_count:
        out.append(
            f"⚠️ 판매일이 비어 기간 집계에서 빠진 건 {undated_count}건 · "
            f"₩{undated_amount:,}"
        )
    unpaid_count, unpaid_amount = data["unpaid"]
    if unpaid_count:
        out.append(f"⚠️ 이 중 미입금 {unpaid_count}건 · ₩{unpaid_amount:,}")
    return "\n".join(out)


def candidates_message(items: list[Item]) -> str | None:
    """월요일 아침 후보 목록. 없으면 None.

    올릴 마음만 먹고 안 올린 물건은 팔릴 수 없다. 주가 시작할 때 한 번
    보여주는 게 이 목록이 존재하는 이유다.
    """
    waiting = [i for i in items if i.status == NOT_LISTED]
    if not waiting:
        return None
    total = sum(i.price or 0 for i in waiting)
    out = [f"🥕 **이번 주에 올릴 후보 {len(waiting)}건 · ₩{total:,}**", ""]
    out += [f"• {i.name} — {i.won}" for i in waiting]
    out.append("")
    out.append("_올리셨으면 노션에서 Listed로 바꿔주세요._")
    return "\n".join(out)


def summary_message(data: dict, today: date) -> str:
    return "\n".join(
        [
            f"🥕 **당근 집계 — {today.month}월 {today.day}일**",
            "",
            f"전체 {data['total']}건",
            f"• 판매 완료 {data['sold']}건 · ₩{data['sold_amount']:,}",
            f"• 판매 중 {data['listed']}건 · ₩{data['listed_amount']:,}"
            + (f" (예약 {data['reserved']}건)" if data["reserved"] else ""),
            *(
                [f"• 발송함 {data['sent']}건 · ₩{data['sent_amount']:,}"]
                if data["sent"]
                else []
            ),
            *(
                [f"• 후보 {data['not_listed']}건 · ₩{data['not_listed_amount']:,}"]
                if data["not_listed"]
                else []
            ),
            f"• 이번 달 판매 {data['month']}건 · ₩{data['month_amount']:,}",
            f"• 미입금 {data['unpaid']}건 · ₩{data['unpaid_amount']:,}",
        ]
    )


