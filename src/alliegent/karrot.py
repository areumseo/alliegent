"""당근 판매 목록. Notion의 🥕 Karrot 데이터베이스를 읽고 쓴다.

The Karrot channel is the one surface that speaks Korean. Everywhere else in
this bot is English so nothing needs an input-method switch, but every item
name and category in this database is Korean already, and a report that
translated "의류" into "Clothing" would be reporting on something the reader
cannot search for.

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

LISTED = "Listed"
RESERVED = "Reserved"
SOLD = "Sold"
STATUSES = (LISTED, RESERVED, SOLD)

CATEGORIES = ("게임", "의류", "전자기기", "발레", "뷰티", "문구", "잡화", "기타")


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    price: int | None
    status: str | None
    paid: bool
    listed_at: date | None
    sold_at: date | None
    bumped: date | None
    category: str | None

    @property
    def last_shown(self) -> date | None:
        """마지막으로 노출을 갱신한 날 — 끌올했으면 그날, 아니면 등록일."""
        dates = [d for d in (self.listed_at, self.bumped) if d]
        return max(dates) if dates else None

    def days_idle(self, today: date) -> int | None:
        anchor = self.last_shown
        return None if anchor is None else (today - anchor).days

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
            listed_at=n.read_date(page, "Listed At"),
            sold_at=n.read_date(page, "Sold At"),
            bumped=n.read_date(page, "Bumped"),
            category=n.read_select(page, "Category"),
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
                -(i.days_idle(today) or 0),
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
            "Listed At": n.date_prop(today),
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

    async def mark_paid(self, item: Item, paid: bool = True) -> None:
        await self._client.update_page(item.id, {"Paid": n.checkbox(paid)})

    async def bump(self, item: Item, today: date) -> None:
        """끌올. 정체 일수가 오늘부터 다시 세어진다."""
        await self._client.update_page(item.id, {"Bumped": n.date_prop(today)})

    async def set_status(self, item: Item, status: str, today: date) -> None:
        if status not in STATUSES:
            raise ValueError(f"status는 {' / '.join(STATUSES)} 중 하나여야 합니다.")
        if status == SOLD:
            await self.mark_sold(item, today)
            return
        await self._client.update_page(item.id, {"Status": n.select(status)})

    # -- 집계 --------------------------------------------------------------

    async def stale_report(self, today: date, days: int) -> dict[str, list[Item]]:
        """정체 매물, 날짜 미상, 미입금."""
        items = await self.all_items()
        listed = [i for i in items if i.status == LISTED]

        stale, undated = [], []
        for item in listed:
            idle = item.days_idle(today)
            if idle is None:
                undated.append(item)
            elif idle >= days:
                stale.append(item)

        stale.sort(key=lambda i: i.days_idle(today) or 0, reverse=True)
        undated.sort(key=lambda i: i.name)
        unpaid = sorted(
            (i for i in items if i.status == SOLD and not i.paid), key=lambda i: i.name
        )
        return {"stale": stale, "undated": undated, "unpaid": unpaid}

    async def summary(self, today: date) -> dict:
        items = await self.all_items()
        sold = [i for i in items if i.status == SOLD]
        listed = [i for i in items if i.status == LISTED]
        reserved = [i for i in items if i.status == RESERVED]
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
            "unpaid": len(unpaid),
            "unpaid_amount": total(unpaid),
            "month": len(this_month),
            "month_amount": total(this_month),
        }


# -- 메시지 ----------------------------------------------------------------
#
# 이 파일에만 한국어 출력이 모여 있다. reports.py는 영어 전용으로 두고, 언어가
# 갈리는 지점을 파일 경계와 맞춰 둔 것.


def _line(index: int, item: Item, today: date) -> str:
    idle = item.days_idle(today)
    bits = [item.won]
    if item.status == RESERVED:
        bits.append("예약중")
    if item.status == SOLD and not item.paid:
        bits.append("⚠️ 미입금")
    elif idle is not None:
        bits.append(f"{idle}일째")
    return f"`{index}.` {item.name} — {' · '.join(bits)}"


def item_list(items: list[Item], today: date, *, title: str = "판매 중") -> str:
    if not items:
        return "등록된 매물이 없습니다."
    lines = [f"🥕 **{title} ({len(items)}건)**"]
    lines += [_line(i, item, today) for i, item in enumerate(items, start=1)]
    lines.append("_`/karrot sold <번호>` · `/karrot bump <번호>` · `/karrot paid <번호>`_")
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


def stale_message(report: dict[str, list[Item]], today: date, days: int) -> str | None:
    """아침 보고. 보고할 게 없으면 None — 매일 '이상 없음'을 보내면 채널을
    안 보게 된다."""
    stale, undated, unpaid = report["stale"], report["undated"], report["unpaid"]
    if not stale and not unpaid:
        return None

    out = ["🥕 **당근 정리**", ""]
    if stale:
        out.append(f"**{days}일 넘게 안 팔린 매물 {len(stale)}건**")
        for item in stale:
            out.append(f"• {item.name} — {item.won} · {item.days_idle(today)}일째")
        out.append("")
    if unpaid:
        amount = sum(i.price or 0 for i in unpaid)
        out.append(f"**미입금 {len(unpaid)}건 · ₩{amount:,}**")
        for item in unpaid:
            out.append(f"• {item.name} — {item.won}")
        out.append("")
    if undated:
        # 판정 못 한 것을 정체로 섞으면 매일 같은 19건이 올라온다.
        out.append(f"_등록일이 비어 판정하지 못한 항목 {len(undated)}건_")
    out.append("_`/karrot list`에서 번호로 처리하실 수 있습니다._")
    return "\n".join(out).strip()


def summary_message(data: dict, today: date) -> str:
    return "\n".join(
        [
            f"🥕 **당근 집계 — {today.month}월 {today.day}일**",
            "",
            f"전체 {data['total']}건",
            f"• 판매 완료 {data['sold']}건 · ₩{data['sold_amount']:,}",
            f"• 판매 중 {data['listed']}건 · ₩{data['listed_amount']:,}"
            + (f" (예약 {data['reserved']}건)" if data["reserved"] else ""),
            f"• 이번 달 판매 {data['month']}건 · ₩{data['month_amount']:,}",
            f"• 미입금 {data['unpaid']}건 · ₩{data['unpaid_amount']:,}",
        ]
    )


def days_since(item: Item, today: date) -> int | None:
    return item.days_idle(today)


def default_stale_days(config: Config) -> int:
    return config.karrot.stale_after_days


def month_window(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)
