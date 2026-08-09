"""Message formatting. Pure functions over domain objects, so they're testable
without touching Notion or Discord."""

from __future__ import annotations

from datetime import date, timedelta

from .agenda import AgendaItem, Project

WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]
DISCORD_LIMIT = 2000


def fmt_date(day: date) -> str:
    return f"{day.month}/{day.day}({WEEKDAYS_KO[day.weekday()]})"


def _bullets(items: list[AgendaItem], *, numbered: bool = False) -> list[str]:
    lines = []
    for idx, item in enumerate(items, start=1):
        mark = "✅" if item.done else "⬜"
        prefix = f"`{idx}.` " if numbered else ""
        lines.append(f"{prefix}{mark} {item.title}")
    return lines


def daily_brief(
    today: date,
    todays: list[AgendaItem],
    overdue: list[AgendaItem],
    active_projects: list[Project],
) -> str:
    out = [f"☀️ **{today.year}년 {fmt_date(today)} 브리핑**", ""]

    pending = [i for i in todays if not i.done]
    if pending:
        out.append(f"**오늘 할 일 ({len(pending)}건)**")
        out += _bullets(pending, numbered=True)
    else:
        out.append("**오늘 할 일** — 등록된 항목이 없습니다.")
    out.append("")

    if overdue:
        out.append(f"**밀린 항목 ({len(overdue)}건)**")
        for item in overdue[:10]:
            when = fmt_date(item.day) if item.day else "날짜 없음"
            out.append(f"⚠️ {item.title} — {when}")
        if len(overdue) > 10:
            out.append(f"…외 {len(overdue) - 10}건")
        out.append("")

    if active_projects:
        out.append(f"**진행 중인 프로젝트 ({len(active_projects)}개)**")
        for project in active_projects[:5]:
            tail = f" → {project.next_action}" if project.next_action else ""
            out.append(f"• {project.title}{tail}")

    return "\n".join(out).strip()


def incomplete_alert(
    today: date, todays: list[AgendaItem], overdue: list[AgendaItem]
) -> str | None:
    """Return None when there is nothing to nag about — a silent evening is the
    correct output, not an 'all clear' ping."""
    pending = [i for i in todays if not i.done]
    if not pending and not overdue:
        return None

    out = [f"🌙 **{fmt_date(today)} 마감 점검**", ""]
    if pending:
        out.append(f"**오늘 미완료 ({len(pending)}건)**")
        out += _bullets(pending, numbered=True)
        out.append("")
    if overdue:
        out.append(f"**기한 지남 ({len(overdue)}건)**")
        for item in overdue[:10]:
            when = fmt_date(item.day) if item.day else "날짜 없음"
            out.append(f"⚠️ {item.title} — {when}")
        if len(overdue) > 10:
            out.append(f"…외 {len(overdue) - 10}건")
    return "\n".join(out).strip()


def week_scaffold(
    week_start: date, created: list[tuple[str, date, str | None]]
) -> str | None:
    """None when the week already has every recurring item — nothing to say."""
    if not created:
        return None
    out = [
        f"🗓️ **{fmt_date(week_start)} 주간 준비** — 반복 항목 {len(created)}건 추가",
        "",
    ]
    for day in sorted({d for _, d, _ in created}):
        out.append(f"**{fmt_date(day)}**")
        out += [f"• {title}" for title, d, _ in created if d == day]
    return "\n".join(out)


def weekly_planning(
    week_start: date, items: list[AgendaItem], overdue: list[AgendaItem]
) -> str:
    week_end = week_start + timedelta(days=6)
    out = [
        f"📅 **다음 주 계획 ({fmt_date(week_start)} ~ {fmt_date(week_end)})**",
        "",
    ]

    if items:
        out.append(f"등록된 항목 {len(items)}건")
        by_day: dict[date, int] = {}
        for item in items:
            if item.day:
                by_day[item.day] = by_day.get(item.day, 0) + 1
        empty = [
            week_start + timedelta(days=offset)
            for offset in range(7)
            if (week_start + timedelta(days=offset)) not in by_day
        ]
        if empty:
            out.append("비어 있는 날 — " + ", ".join(fmt_date(d) for d in empty))
    else:
        out.append("다음 주에 등록된 항목이 아직 없습니다.")
    out.append("")

    if overdue:
        out.append(f"**넘어온 것 ({len(overdue)}건)**")
        for item in overdue[:10]:
            when = fmt_date(item.day) if item.day else "날짜 없음"
            out.append(f"⚠️ {item.title} — {when}")
        if len(overdue) > 10:
            out.append(f"…외 {len(overdue) - 10}건")
        out.append("")

    out.append("_이번 주에 남은 것부터 정리하고, 다음 주 일정을 채워보세요._")
    return "\n".join(out).strip()


def stale_projects(items: list[tuple[Project, date | None]]) -> str | None:
    if not items:
        return None
    out = [f"🐢 **진행이 더딘 프로젝트 ({len(items)}개)**", ""]
    for project, last in items:
        when = f"마지막 활동 {fmt_date(last)}" if last else "관련 활동 기록 없음"
        tail = f"\n   다음 할 일: {project.next_action}" if project.next_action else ""
        out.append(f"• **{project.title}** — {when}{tail}")
    return "\n".join(out)


def weekly_review(start: date, end: date, items: list[AgendaItem]) -> str:
    done = [i for i in items if i.done]
    undone = [i for i in items if not i.done]
    total = len(items)
    rate = round(len(done) / total * 100) if total else 0

    out = [
        f"📋 **주간 회고 ({fmt_date(start)} ~ {fmt_date(end)})**",
        "",
        f"완료 {len(done)} / 전체 {total}건 (달성률 {rate}%)",
        "",
    ]
    if done:
        out.append("**완료한 것**")
        out += [f"✅ {i.title}" for i in done[:15]]
        if len(done) > 15:
            out.append(f"…외 {len(done) - 15}건")
        out.append("")
    if undone:
        out.append("**넘어가는 것**")
        out += [f"⬜ {i.title}" for i in undone[:15]]
        if len(undone) > 15:
            out.append(f"…외 {len(undone) - 15}건")
        out.append("")
    out.append("_이번 주에 잘 된 것 / 막힌 것 / 다음 주에 바꿀 것을 적어보세요._")
    return "\n".join(out).strip()


def today_list(today: date, items: list[AgendaItem]) -> str:
    if not items:
        return f"{fmt_date(today)} — 등록된 항목이 없습니다."
    header = f"**{fmt_date(today)} 아젠다 ({len(items)}건)**"
    return "\n".join([header, *_bullets(items, numbered=True)])


def project_list(projects: list[Project]) -> str:
    if not projects:
        return "진행 중인 프로젝트가 없습니다."
    out = [f"**진행 중인 프로젝트 ({len(projects)}개)**"]
    for project in projects:
        tail = f" → {project.next_action}" if project.next_action else ""
        out.append(f"• {project.title}{tail}")
    return "\n".join(out)


def chunk(message: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries to stay under Discord's per-message limit."""
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in message.split("\n"):
        # A single line longer than the limit has to be hard-split.
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
