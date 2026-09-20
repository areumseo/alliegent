"""Message formatting. Pure functions over domain objects, so they're testable
without touching Notion or Discord."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import date, time, timedelta

from .agenda import AgendaItem, Project

TITLE_HAS_TIME = re.compile(r"\b\d{1,2}(:\d{2})?\s*[ap]m\b|\b\d{1,2}:\d{2}\b", re.IGNORECASE)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DISCORD_LIMIT = 2000
MAX_LISTED = 10


def fmt_date(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day.month}/{day.day}"


def fmt_time(at: time) -> str:
    return f"{at.hour:02d}:{at.minute:02d}"


def _with_time(item: AgendaItem) -> str:
    """The title, prefixed with its time unless the title already says it.

    Times get typed into titles by habit ("Cafe shift 11AM"), and a line reading
    "11:00 Cafe shift 11AM" is worse than either half alone.
    """
    if item.at is None or TITLE_HAS_TIME.search(item.title):
        return item.title
    return f"{fmt_time(item.at)} {item.title}"


def _mark(item: AgendaItem) -> str:
    """The one-glyph state of an item.

    No marker for work not begun: an unchecked box carries no information in a
    list that is mostly unstarted, and repeating it on every line is noise.
    Started work is marked because it needs something different from the
    reader -- finishing rather than beginning -- and that was invisible when
    the list only distinguished done from not done.
    """
    if item.done:
        return "✅ "
    if item.started:
        return "🔸 "
    return ""


# Fixed-width layout, by the same rules as the asset table: a code block with
# every cell padded, ASCII only, and widths measured from the values rather
# than fixed. Two things are specific to task lists:
#
# - Titles are Korean as often as not, and a Korean glyph occupies two cells
#   in a monospace font. Padding by len() would leave every column after the
#   title ragged, so width is counted in cells, not characters.
# - Titles are also the one cell with no natural bound, so the title column
#   is the one that gives way when the table would pass TABLE_COLS.
TABLE_COLS = 40
MIN_TASK_COLS = 12
# Ticks and diamonds are emoji, which a code block renders at an unpredictable
# width; inside the table the state is ASCII.
MARKS = {"done": "v", "started": ">", "": ""}


def _width(text: str) -> int:
    """Display width in monospace cells: East Asian wide glyphs count twice."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _fit(text: str, cells: int) -> str:
    """Cut to a display width, marking the cut. Never splits a wide glyph."""
    if _width(text) <= cells:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > cells - 2:
            break
        out += ch
    return out + ".." + " " * (cells - _width(out) - 2)


def _pad(text: str, cells: int) -> str:
    return text + " " * max(0, cells - _width(text))


def _table_mark(item: AgendaItem) -> str:
    if item.done:
        return MARKS["done"]
    if item.started:
        return MARKS["started"]
    return MARKS[""]


def _rows(
    items: list[AgendaItem],
    *,
    when: Callable[[AgendaItem], str],
    keep: set[str] | None = None,
    limit: int | None = None,
) -> tuple[list[tuple[str, ...]], int]:
    """Cells for each item, plus how many matched but were not rendered.

    Numbered against the *whole* list even when filtered or truncated: /done
    and /delete recount the full list, so renumbering a subset from 1 would
    send "2" to a different row than the one being read.
    """
    rows: list[tuple[str, ...]] = []
    matched = 0
    for idx, item in enumerate(items, start=1):
        if keep is not None and (item.status or "") not in keep:
            continue
        matched += 1
        if limit is not None and len(rows) >= limit:
            continue
        rows.append((str(idx), _table_mark(item), when(item), item.title, item.category or ""))
    return rows, matched - len(rows)


def _table(rows: list[tuple[str, ...]], when_header: str) -> list[str]:
    """Render cells as a padded code block. Empty when there is nothing."""
    if not rows:
        return []
    header = ("#", "", when_header, "Task", "Category")
    cells = [header, *rows]
    widths = [max(_width(row[col]) for row in cells) for col in range(5)]

    # The title column absorbs whatever the others leave, down to a floor:
    # past that a truncated title stops being recognisable, and a table that
    # wraps on a phone is worse than one a little too wide.
    over = sum(widths) + len(widths) - 1 - TABLE_COLS
    if over > 0:
        widths[3] = max(MIN_TASK_COLS, widths[3] - over)

    def line(row: tuple[str, ...]) -> str:
        number, mark, *rest = row
        padded = [f"{number:>{widths[0]}}", _pad(mark, widths[1])]
        padded += [
            _pad(_fit(value, width), width)
            for value, width in zip(rest, widths[2:], strict=True)
        ]
        return " ".join(padded).rstrip()

    # The rule spans the header, which is the full table width -- rows are
    # right-stripped and the last cell is often empty, so the widest row
    # understates it.
    head = line(header)
    return ["```", head, "-" * _width(head), *(line(row) for row in rows), "```"]


def _bullets(items: list[AgendaItem], *, numbered: bool = False) -> list[str]:
    lines = []
    for idx, item in enumerate(items, start=1):
        prefix = f"`{idx}.` " if numbered else "• "
        lines.append(f"{prefix}{_mark(item)}{_with_time(item)}")
    return lines


def _clock(item: AgendaItem) -> str:
    """The item's time, or a hyphen. Items without one sort to the day's end,
    and an empty cell there reads as a missing value rather than a choice."""
    return fmt_time(item.at) if item.at else "-"


def day_table(items: list[AgendaItem]) -> list[str]:
    """A whole day, finished items included."""
    rows, _ = _rows(items, when=_clock)
    return _table(rows, "Time")


def pending_lines(todays: list[AgendaItem]) -> list[str]:
    """The unfinished items, numbered against the whole day."""
    rows, _ = _rows(todays, when=_clock, keep=None)
    open_rows = [row for row, item in zip(rows, todays, strict=True) if not item.done]
    return _table(open_rows, "Time")


def overdue_lines(
    items: list[AgendaItem],
    *,
    limit: int | None = None,
    keep: set[str] | None = None,
) -> list[str]:
    """Overdue items, numbered and dated.

    Numbered because every message that shows the backlog is a place someone
    decides to clear it, and `/done 2 overdue` needs a 2 to type. The numbers
    match what /done and /delete resolve, since both count this same list --
    so the brief, the evening alert and /overdue all agree.

    Dated rather than timed: these span days by definition, and which day a
    row came from is what a reader needs before its hour.
    """
    rows, remaining = _rows(
        items,
        when=lambda i: fmt_date(i.day) if i.day else "no date",
        keep=keep,
        limit=limit,
    )
    lines = _table(rows, "Due")
    if lines and remaining > 0:
        lines.append(f"_…and {remaining} more — `/overdue` for the rest._")
    return lines


def overdue_hint() -> str:
    return "_`/done <n> overdue` or `/delete <n> overdue`._"


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


CALENDAR_PROBLEMS = {
    "auth": "⚠️ Calendar unavailable — iCloud rejected the app password.",
    "error": "⚠️ Calendar unavailable — could not be read this morning.",
}


def daily_brief(
    today: date,
    todays: list[AgendaItem],
    overdue: list[AgendaItem],
    active_projects: list[Project],
    events: list | None = None,
    *,
    calendar_problem: str | None = None,
) -> str:
    out = [f"☀️ **Daily brief — {fmt_date(today)}**", ""]
    # Calendar first: it is the part of the day already committed, and the
    # to-do list has to fit around it.
    if calendar_problem:
        # Said out loud, because an empty calendar and an unreachable one look
        # identical in a brief -- and the second kind lasted a week unnoticed.
        out += [CALENDAR_PROBLEMS.get(calendar_problem, CALENDAR_PROBLEMS["error"]), ""]
    out += calendar_block(events or [])

    # Counted from the items, not the rendered lines: the table brings its own
    # header and fences, so its length stopped being the number of tasks.
    open_count = len([i for i in todays if not i.done])
    pending = pending_lines(todays)
    if pending:
        out.append(f"**Today ({open_count})**")
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
        out += overdue_lines(overdue, limit=MAX_LISTED)
        out.append(overdue_hint())
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
    open_count = len([i for i in todays if not i.done])
    pending = pending_lines(todays)
    if not pending and not overdue:
        return None

    out = [f"🌙 **End of day — {fmt_date(today)}**", ""]
    if pending:
        out.append(f"**Still open ({open_count})**")
        out += pending
        out.append("")
    if overdue:
        out.append(f"**Past due ({len(overdue)})**")
        out += overdue_lines(overdue, limit=MAX_LISTED)
        out.append(overdue_hint())
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
        out += overdue_lines(overdue, limit=MAX_LISTED)
        out.append(overdue_hint())
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
    lines = [header, *(day_table(items) if numbered else _bullets(items))]
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
        text = f"{done} of {len(items)} done ({round(done / len(items) * 100)}%)"
        # Counted separately rather than folded into the percentage: a day
        # with three things underway is in a different state from one where
        # nothing has been touched, and both read as "2 of 7" otherwise.
        started = sum(1 for i in items if i.started and not i.done)
        if started:
            text += f", {started} in progress"
        return text

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


def overdue_list(
    items: list[AgendaItem], *, keep: set[str] | None = None, label: str = ""
) -> str:
    """The whole backlog, numbered the way the brief numbers it.

    Not truncated: a number the list doesn't show is a number nothing can
    resolve, and this is the command someone runs precisely to see the rest.

    `keep` narrows what is displayed without touching the numbering, so a
    filtered view and an unfiltered one name the same rows.
    """
    if not items:
        return "🎉 Nothing overdue."
    shown = len(items) if keep is None else len([i for i in items if (i.status or "") in keep])
    lines = overdue_lines(items, keep=keep)
    if not lines:
        return f"🎉 Nothing overdue is {label}." if label else "🎉 Nothing overdue."
    header = (
        f"**Overdue — {label} ({shown} of {len(items)})**"
        if label
        else f"**Overdue ({len(items)})**"
    )
    return "\n".join([header, *lines, overdue_hint()])


def project_list(projects: list[Project]) -> str:
    if not projects:
        return "No active projects."
    out = [f"**Active projects ({len(projects)})**"]
    for project in projects:
        tail = f" → {project.next_action}" if project.next_action else ""
        out.append(f"• {project.title}{tail}")
    return "\n".join(out)


FENCE = "```"


def chunk(message: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries to stay under Discord's per-message limit.

    A split inside a code block closes it and reopens it in the next message.
    Otherwise the break leaves one message with an unclosed fence and the next
    with none, and the table loses its alignment in exactly the case -- a long
    backlog -- where the rows are hardest to read without it.
    """
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fenced = False  # whether the lines held in `current` are inside a block

    def flush() -> None:
        nonlocal current, size
        if not current:
            return
        chunks.append("\n".join([*current, FENCE] if fenced else current))
        current = [FENCE] if fenced else []
        size = sum(len(line) + 1 for line in current)

    for line in message.split("\n"):
        # A single line longer than the limit has to be hard-split.
        while len(line) > limit:
            flush()
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            flush()
        current.append(line)
        size += len(line) + 1
        if line.startswith(FENCE):
            fenced = not fenced
    if current:
        chunks.append("\n".join(current))
    return chunks
