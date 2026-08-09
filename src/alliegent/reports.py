"""Message formatting. Pure functions over domain objects, so they're testable
without touching Notion or Discord."""

from __future__ import annotations

from datetime import date, timedelta

from .agenda import AgendaItem, Project

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DISCORD_LIMIT = 2000
MAX_LISTED = 10


def fmt_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.month}/{day.day}"


def _bullets(items: list[AgendaItem], *, numbered: bool = False) -> list[str]:
    """Render items without an empty-checkbox marker.

    An unchecked box carries no information in a list that is already all
    unfinished work, and repeating it on every line just adds noise. Only
    completion is marked, and only where done and pending items are mixed.
    """
    lines = []
    for idx, item in enumerate(items, start=1):
        prefix = f"`{idx}.` " if numbered else "• "
        mark = "✅ " if item.done else ""
        lines.append(f"{prefix}{mark}{item.title}")
    return lines


def _dated(items: list[AgendaItem], marker: str = "⚠️") -> list[str]:
    """List items with their date, truncating rather than flooding the channel."""
    lines = []
    for item in items[:MAX_LISTED]:
        when = fmt_date(item.day) if item.day else "no date"
        lines.append(f"{marker} {item.title} — {when}")
    if len(items) > MAX_LISTED:
        lines.append(f"…and {len(items) - MAX_LISTED} more")
    return lines


def daily_brief(
    today: date,
    todays: list[AgendaItem],
    overdue: list[AgendaItem],
    active_projects: list[Project],
) -> str:
    out = [f"☀️ **Daily brief — {fmt_date(today)}**", ""]

    pending = [i for i in todays if not i.done]
    if pending:
        out.append(f"**Today ({len(pending)})**")
        out += _bullets(pending, numbered=True)
    else:
        out.append("**Today** — nothing scheduled.")
    out.append("")

    if overdue:
        out.append(f"**Overdue ({len(overdue)})**")
        out += _dated(overdue)
        out.append("")

    if active_projects:
        out.append(f"**Active projects ({len(active_projects)})**")
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

    out = [f"🌙 **End of day — {fmt_date(today)}**", ""]
    if pending:
        out.append(f"**Still open ({len(pending)})**")
        out += _bullets(pending, numbered=True)
        out.append("")
    if overdue:
        out.append(f"**Past due ({len(overdue)})**")
        out += _dated(overdue)
    return "\n".join(out).strip()


def week_scaffold(
    week_start: date, created: list[tuple[str, date, str | None]]
) -> str | None:
    """None when the week already has every recurring item — nothing to say."""
    if not created:
        return None
    out = [
        f"🗓️ **Week of {fmt_date(week_start)}** — added {len(created)} recurring item(s)",
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
    out = [f"📅 **Next week — {fmt_date(week_start)} to {fmt_date(week_end)}**", ""]

    if items:
        out.append(f"{len(items)} item(s) scheduled")
        scheduled_days = {item.day for item in items if item.day}
        empty = [
            week_start + timedelta(days=offset)
            for offset in range(7)
            if (week_start + timedelta(days=offset)) not in scheduled_days
        ]
        if empty:
            out.append("Empty days — " + ", ".join(fmt_date(d) for d in empty))
    else:
        out.append("Nothing scheduled for next week yet.")
    out.append("")

    if overdue:
        out.append(f"**Carrying over ({len(overdue)})**")
        out += _dated(overdue)
        out.append("")

    out.append("_Clear what's left this week, then fill in next week._")
    return "\n".join(out).strip()


def stale_projects(items: list[tuple[Project, date | None]]) -> str | None:
    if not items:
        return None
    out = [f"🐢 **Stalled projects ({len(items)})**", ""]
    for project, last in items:
        when = f"last activity {fmt_date(last)}" if last else "no linked activity"
        tail = f"\n   Next: {project.next_action}" if project.next_action else ""
        out.append(f"• **{project.title}** — {when}{tail}")
    return "\n".join(out)


def weekly_review(start: date, end: date, items: list[AgendaItem]) -> str:
    done = [i for i in items if i.done]
    undone = [i for i in items if not i.done]
    total = len(items)
    rate = round(len(done) / total * 100) if total else 0

    out = [
        f"📋 **Weekly review — {fmt_date(start)} to {fmt_date(end)}**",
        "",
        f"Done {len(done)} of {total} ({rate}%)",
        "",
    ]
    if done:
        out.append("**Completed**")
        out += [f"✅ {i.title}" for i in done[:15]]
        if len(done) > 15:
            out.append(f"…and {len(done) - 15} more")
        out.append("")
    if undone:
        out.append("**Carrying over**")
        out += [f"• {i.title}" for i in undone[:15]]
        if len(undone) > 15:
            out.append(f"…and {len(undone) - 15} more")
        out.append("")
    out.append("_What went well, what got stuck, what to change next week._")
    return "\n".join(out).strip()


def today_list(today: date, items: list[AgendaItem]) -> str:
    if not items:
        return f"{fmt_date(today)} — nothing scheduled."
    header = f"**{fmt_date(today)} — {len(items)} item(s)**"
    return "\n".join([header, *_bullets(items, numbered=True)])


def ai_news(today: date, body: str) -> str:
    """Wrap the model-written digest in a dated header.

    The body is used verbatim — its formatting is the model's responsibility,
    so a change of format here means changing the prompt, not this function.
    """
    return f"🤖 **AI News — {fmt_date(today)}**\n\n{body.strip()}"


def overdue_list(items: list[AgendaItem]) -> str:
    if not items:
        return "🎉 Nothing overdue."
    return "\n".join([f"**Overdue ({len(items)})**", *_dated(items)])


def project_list(projects: list[Project]) -> str:
    if not projects:
        return "No active projects."
    out = [f"**Active projects ({len(projects)})**"]
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
