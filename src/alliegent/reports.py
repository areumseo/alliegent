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


def pending_lines(todays: list[AgendaItem]) -> list[str]:
    """The unfinished items, numbered against the *whole* day.

    /done and /delete resolve a number the way /today prints it, which counts
    completed rows too. Renumbering just the unfinished ones from 1 gives a
    list where "2" means a different row depending on which message you read
    it in — and completes the wrong task.
    """
    return [
        f"`{idx}.` {item.title}"
        for idx, item in enumerate(todays, start=1)
        if not item.done
    ]


def _dated(items: list[AgendaItem], marker: str = "⚠️") -> list[str]:
    """List items with their date, truncating rather than flooding the channel."""
    lines = []
    for item in items[:MAX_LISTED]:
        when = fmt_date(item.day) if item.day else "no date"
        lines.append(f"{marker} {item.title} — {when}")
    if len(items) > MAX_LISTED:
        lines.append(f"…and {len(items) - MAX_LISTED} more")
    return lines


def calendar_block(events: list) -> list[str]:
    """Render calendar events as brief lines. Empty when there are none —
    a 'no events' line every morning is noise."""
    if not events:
        return []
    out = ["**📅 Calendar**"]
    for event in events:
        when = "all day" if event.all_day or event.start is None else event.start.strftime("%H:%M")
        out.append(f"`{when:>7}`  {event.summary}")
    out.append("")
    return out


def daily_brief(
    today: date,
    todays: list[AgendaItem],
    overdue: list[AgendaItem],
    active_projects: list[Project],
    events: list | None = None,
) -> str:
    out = [f"☀️ **Daily brief — {fmt_date(today)}**", ""]
    # Calendar first: it is the part of the day already committed, and the
    # to-do list has to fit around it.
    out += calendar_block(events or [])

    pending = pending_lines(todays)
    if pending:
        out.append(f"**Today ({len(pending)})**")
        out += pending
    elif todays:
        # An empty day and a finished one both leave nothing to list, but
        # telling someone who cleared seven items that nothing was scheduled
        # reads as the bot not having noticed.
        out.append(f"**Today** — all {len(todays)} done. 🎉")
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
    pending = pending_lines(todays)
    if not pending and not overdue:
        return None

    out = [f"🌙 **End of day — {fmt_date(today)}**", ""]
    if pending:
        out.append(f"**Still open ({len(pending)})**")
        out += pending
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
    """A day-by-day account of the week.

    Grouped by date rather than split into completed and carried-over lists:
    the question a review answers is what each day held, and a flat list of
    eighteen ticks says only that the week happened. Nothing is truncated —
    a review that hides a third of the week defeats itself, and long messages
    are chunked before sending.
    """
    done = [i for i in items if i.done]
    total = len(items)
    rate = round(len(done) / total * 100) if total else 0

    out = [
        f"📋 **Weekly review — {fmt_date(start)} to {fmt_date(end)}**",
        "",
        f"Done {len(done)} of {total} ({rate}%)",
        "",
    ]

    by_day: dict[date, list[AgendaItem]] = {}
    undated: list[AgendaItem] = []
    for item in items:
        if item.day is None:
            undated.append(item)
        else:
            by_day.setdefault(item.day, []).append(item)

    for day in sorted(by_day):
        # Days with nothing on them are skipped rather than printed empty —
        # a rest day is not a finding.
        entries = by_day[day]
        finished = sum(1 for i in entries if i.done)
        header = f"**{fmt_date(day)}**"
        if finished < len(entries):
            header += f"  ({finished}/{len(entries)})"
        out.append(header)
        out += [f"{'✅' if i.done else '•'} {i.title}" for i in entries]
        out.append("")

    if undated:
        out.append("**No date**")
        out += [f"{'✅' if i.done else '•'} {i.title}" for i in undated]
        out.append("")

    out.append("_What went well, what got stuck, what to change next week._")
    return "\n".join(out).strip()


def day_list(
    day: date, items: list[AgendaItem], *, numbered: bool = True, today: date | None = None
) -> str:
    """One day's items, numbered so they can be acted on.

    Numbers are per-day, and /done and /delete take the day as an argument.
    A list for anything other than today says so, since the number alone
    doesn't carry which day it belongs to.
    """
    if not items:
        return f"{fmt_date(day)} — nothing scheduled."
    header = f"**{fmt_date(day)} — {len(items)} item(s)**"
    lines = [header, *_bullets(items, numbered=numbered)]
    if numbered and today is not None and day != today:
        lines.append(f"_`/done <n> {day.isoformat()}` to tick one off._")
    return "\n".join(lines)


def today_list(today: date, items: list[AgendaItem]) -> str:
    return day_list(today, items, numbered=True)


def status(
    day: date,
    todays: list[AgendaItem],
    overdue: list[AgendaItem],
    week: list[AgendaItem],
    *,
    today: date | None = None,
) -> str:
    """A numbers-first snapshot: how a day and its week are actually going.

    `day` is the day being reported on, which need not be today; `today` is
    the real current date, used only to word things and to say which day the
    numbers belong to.
    """
    is_today = today is None or day == today

    def ratio(items: list[AgendaItem]) -> str:
        done = sum(1 for i in items if i.done)
        if not items:
            return "nothing scheduled"
        return f"{done} of {len(items)} done ({round(done / len(items) * 100)}%)"

    label = "Today" if is_today else fmt_date(day)
    week_label = "This week" if is_today else "That week"
    out = [
        f"📊 **Status — {fmt_date(day)}**",
        "",
        f"{label} — {ratio(todays)}",
        f"{week_label} — {ratio(week)}",
    ]
    if overdue:
        # Unfinished and dated before the day in question -- for a future day
        # that includes everything still open between now and then.
        out.append(f"Overdue — {len(overdue)}")

    pending = pending_lines(todays)
    left = "Left today" if is_today else f"Left on {fmt_date(day)}"
    if pending:
        out += ["", f"**{left} ({len(pending)})**", *pending]
        if not is_today:
            out.append(f"_`/done <n> {day.isoformat()}` to tick one off._")
    elif todays:
        out += ["", f"Nothing left {'today' if is_today else 'that day'}. 🎉"]
    return "\n".join(out)


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
