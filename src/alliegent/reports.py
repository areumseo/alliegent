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


def _table(
    header: tuple[str, ...],
    rows: list[tuple[str, ...]],
    *,
    flex: int,
    right: tuple[int, ...] = (),
) -> list[str]:
    """Render cells as a padded code block. Empty when there is nothing.

    `flex` names the one column allowed to give way when the table would pass
    TABLE_COLS -- the free-text one, which is the only column whose width is
    not a property of the data. `right` names the columns aligned right.
    """
    if not rows:
        return []
    cells = [header, *rows]
    widths = [max(_width(row[col]) for row in cells) for col in range(len(header))]

    # Down to a floor: past that a truncated title stops being recognisable,
    # and a table a little too wide beats one that wraps on a phone.
    over = sum(widths) + len(widths) - 1 - TABLE_COLS
    if over > 0:
        widths[flex] = max(MIN_TASK_COLS, widths[flex] - over)

    def line(row: tuple[str, ...]) -> str:
        out = []
        for col, (value, width) in enumerate(zip(row, widths, strict=True)):
            fitted = _fit(value, width)
            out.append(f"{fitted:>{width}}" if col in right else _pad(fitted, width))
        return " ".join(out).rstrip()

    # The rule spans the columns, not the widest rendered line: every line is
    # right-stripped, so a short last cell -- an empty category, a header
    # narrower than its column -- would pull the rule in with it.
    span = sum(widths) + len(widths) - 1
    return ["```", line(header), "-" * span, *(line(row) for row in rows), "```"]


def _task_table(rows: list[tuple[str, ...]], when_header: str) -> list[str]:
    """Numbered task rows: number, state, when, title, category."""
    return _table(("#", "", when_header, "Task", "Category"), rows, flex=3, right=(0,))


def _project_table(projects: list[Project]) -> list[str]:
    """Projects and what each is waiting on, which is the actionable half."""
    rows = [(p.title, p.next_action or "") for p in projects]
    return _table(("Project", "Next"), rows, flex=1)


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
    return _task_table(rows, "Time")


def day_header(todays: list[AgendaItem]) -> str:
    """How the day stands, for the heading above its table.

    The whole day is listed, finished items included, so the heading has to
    say how much of it is still live -- "Today (7)" above four open rows and
    three ticked ones tells you nothing you wanted to know.
    """
    left = open_count(todays)
    return f"**Today — {left} left of {len(todays)}**"


def open_count(todays: list[AgendaItem]) -> int:
    """How many of a day's items are unfinished.

    Counted from the items rather than from the rendered lines. The rendered
    block carries a fence, a header and a rule of its own, so its length
    stopped being the number of tasks the moment lists became tables -- and
    a header reading "Left today (8)" above four rows is worse than no count.
    """
    return len([i for i in todays if not i.done])


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
    lines = _task_table(rows, "Due")
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
    rows = [
        (
            "all day" if event.all_day or event.start is None else event.start.strftime("%H:%M"),
            event.summary,
        )
        for event in events
    ]
    return ["**📅 Calendar**", *_table(("Time", "Event"), rows, flex=1), ""]


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

    # Finished items are shown rather than dropped: what you have already
    # done is context for what is left, and hiding it was also what made the
    # numbers skip -- they count the whole day, because /done resolves them
    # against the whole day.
    if open_count(todays):
        out.append(day_header(todays))
        out += day_table(todays)
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
        out += _project_table(active_projects[:5])

    return "\n".join(out).strip()


def incomplete_alert(
    today: date, todays: list[AgendaItem], overdue: list[AgendaItem]
) -> str | None:
    """Return None when there is nothing to nag about — a silent evening is the
    correct output, not an 'all clear' ping."""
    if not open_count(todays) and not overdue:
        return None

    out = [f"🌙 **End of day — {fmt_date(today)}**", ""]
    if open_count(todays):
        out.append(day_header(todays))
        out += day_table(todays)
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
    rows: list[tuple[str, ...]] = []
    for day in sorted({d for _, d, _ in created}):
        titles = [(title, category) for title, d, category in created if d == day]
        for offset, (title, category) in enumerate(titles):
            rows.append((fmt_date(day) if offset == 0 else "", title, category or ""))
    out += _table(("Day", "Task", "Category"), rows, flex=1)
    return "\n".join(out)


def weekly_planning(
    week_start: date, items: list[AgendaItem], overdue: list[AgendaItem]
) -> str:
    week_end = week_start + timedelta(days=6)
    out = [f"📅 **Next week — {fmt_date(week_start)} to {fmt_date(week_end)}**", ""]

    if items:
        out.append(f"{len(items)} item(s) scheduled")
        # Every day of the week, empty ones included: the shape of the week is
        # the thing being planned, and a day with nothing on it is the row
        # that most needs to be seen.
        counts: dict[date, int] = {}
        for item in items:
            if item.day:
                counts[item.day] = counts.get(item.day, 0) + 1
        rows = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            count = counts.get(day, 0)
            rows.append((fmt_date(day), str(count) if count else "-"))
        out += _table(("Day", "Items"), rows, flex=0, right=(1,))
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
    rows = [
        (project.title, fmt_date(last) if last else "never", project.next_action or "")
        for project, last in items
    ]
    out = [f"🐢 **Stalled projects ({len(items)})**", ""]
    out += _table(("Project", "Last", "Next"), rows, flex=2)
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

    # One table for the week rather than one per day: the date column groups
    # it just as well, and a dozen two-row blocks is harder to read down than
    # a single column of dates. The date is printed once per day, since
    # repeating it on every row is the noise the grouping was avoiding.
    # Days with nothing on them are skipped -- a rest day is not a finding.
    rows: list[tuple[str, ...]] = []
    for day in sorted(by_day):
        entries = by_day[day]
        finished = sum(1 for i in entries if i.done)
        label = fmt_date(day)
        if finished < len(entries):
            label += f" {finished}/{len(entries)}"
        for offset, entry in enumerate(entries):
            rows.append(
                (label if offset == 0 else "", _table_mark(entry), entry.title,
                 entry.category or "")
            )
    for offset, entry in enumerate(undated):
        rows.append(
            ("no date" if offset == 0 else "", _table_mark(entry), entry.title,
             entry.category or "")
        )

    out += _table(("Day", "", "Task", "Category"), rows, flex=2)
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

    if open_count(todays):
        header = (
            day_header(todays)
            if is_today
            else f"**{fmt_date(day)} — {open_count(todays)} left of {len(todays)}**"
        )
        out += ["", header, *day_table(todays)]
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
    return "\n".join([f"**Active projects ({len(projects)})**", *_project_table(projects)])


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
