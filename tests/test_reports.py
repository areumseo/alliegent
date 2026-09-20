from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from alliegent import reports
from alliegent.agenda import AgendaItem, Project
from alliegent.integrations.discord_bot import parse_day

TODAY = date(2026, 8, 8)  # Saturday


def item(title, *, done=False, day=TODAY):
    return AgendaItem(
        id=title, title=title, day=day, status=None, done=done, url="", project_ids=()
    )


def row(text: str, number: int) -> str:
    """The table row a number labels, with its padding collapsed.

    Lists are rendered as padded tables, so an assertion on the exact spacing
    would be an assertion about the widest title in the fixture rather than
    about anything the test cares about.
    """
    for line in text.splitlines():
        cells = line.split()
        if cells and cells[0] == str(number):
            return " ".join(cells)
    return ""


def test_fmt_date_shows_weekday_and_day():
    assert reports.fmt_date(TODAY) == "Sat 8/8"


def test_daily_brief_lists_pending_and_overdue():
    text = reports.daily_brief(
        TODAY,
        [item("오늘 할 일"), item("이미 한 것", done=True)],
        [item("밀린 것", day=date(2026, 8, 1))],
        [Project("p", "프로젝트", "In progress", "다음 단계", "")],
    )
    assert "오늘 할 일" in text
    assert "이미 한 것" not in text  # completed items aren't repeated back
    assert "밀린 것" in text
    assert "다음 단계" in text


def test_no_empty_checkbox_anywhere():
    """An unchecked box on every line of an all-unfinished list is pure noise."""
    brief = reports.daily_brief(TODAY, [item("남은 것")], [], [])
    today = reports.today_list(TODAY, [item("한 것", done=True), item("남은 것")])
    review = reports.weekly_review(
        date(2026, 8, 2), TODAY, [item("한 것", done=True), item("남은 것")]
    )
    planning = reports.weekly_planning(date(2026, 8, 10), [item("예정")], [])
    for text in (brief, today, review, planning):
        assert "⬜" not in text


def test_done_items_stay_marked_where_they_mix_with_pending():
    text = reports.today_list(TODAY, [item("한 것", done=True), item("남은 것")])
    assert row(text, 1).startswith("1 v") and "한 것" in row(text, 1)
    assert row(text, 2).startswith("2 -") and "남은 것" in row(text, 2)


def test_a_future_day_is_numbered_and_says_which_day():
    """Numbers are per-day and /done takes the day, so a list for another day
    carries the argument you need with it."""
    tomorrow = date(2026, 8, 9)
    text = reports.day_list(tomorrow, [item("내일 할 것")], today=TODAY)
    assert "내일 할 것" in row(text, 1)
    assert "/done <n> 2026-08-09" in text


def test_todays_list_carries_no_such_hint():
    text = reports.day_list(TODAY, [item("오늘 할 것")], today=TODAY)
    assert "오늘 할 것" in row(text, 1)
    assert "/done <n>" not in text


def test_day_list_can_still_omit_numbers():
    text = reports.day_list(TODAY, [item("x")], numbered=False)
    assert "• x" in text


def test_day_list_reports_an_empty_day():
    assert "nothing scheduled" in reports.day_list(TODAY, [], numbered=False)


# -- status ----------------------------------------------------------------


def test_status_counts_today_and_the_week():
    todays = [item("a", done=True), item("b"), item("c")]
    week = [item("a", done=True), item("b"), item("c"), item("d", done=True)]
    text = reports.status(TODAY, todays, [], week)
    assert "Today — 1 of 3 done (33%)" in text
    assert "This week — 2 of 4 done (50%)" in text


def test_status_numbers_match_the_today_list():
    """The numbers must index the full day, not the pending subset — /done
    resolves them the way /today prints them."""
    todays = [item("done one", done=True), item("still open")]
    text = reports.status(TODAY, todays, [], todays)
    assert "still open" in row(text, 2)
    assert "still open" not in row(text, 1)


def test_status_celebrates_a_finished_day():
    todays = [item("a", done=True)]
    text = reports.status(TODAY, todays, [], todays)
    assert "Nothing left today" in text


def test_status_for_another_day_says_which_day_it_means():
    """A status for a day that isn't today can't say "Today" -- the numbers
    below it are only valid with that date on /done."""
    other = TODAY + timedelta(days=2)
    todays = [item("a", done=True), item("b")]
    text = reports.status(other, todays, [], todays, today=TODAY)
    assert "Today —" not in text
    assert f"{reports.fmt_date(other)} — 1 of 2 done (50%)" in text
    assert "That week —" in text


def test_status_for_another_day_carries_the_date_hint():
    other = TODAY + timedelta(days=2)
    text = reports.status(other, [item("b")], [], [item("b")], today=TODAY)
    assert f"/done <n> {other.isoformat()}" in text


def test_status_for_today_stays_worded_as_today():
    text = reports.status(TODAY, [item("b")], [], [item("b")], today=TODAY)
    assert "Today — 0 of 1 done" in text
    assert "This week —" in text
    assert "/done <n>" not in text  # today's numbers need no date


def test_status_omits_overdue_when_there_is_none():
    text = reports.status(TODAY, [item("a")], [], [item("a")])
    assert "Overdue" not in text


def test_status_shows_overdue_count():
    text = reports.status(TODAY, [], [item("x"), item("y")], [])
    assert "Overdue — 2" in text


def test_status_handles_a_completely_empty_day():
    text = reports.status(TODAY, [], [], [])
    assert "nothing scheduled" in text
    assert "Nothing left today" not in text  # nothing was scheduled to finish


# -- numbering -------------------------------------------------------------
# Every message that numbers today's unfinished items has to number them the
# way /today prints them, because that is what /done and /delete resolve
# against. Numbering the pending subset from 1 makes "2" mean a different row
# depending on which message you happened to read it in.

MIXED = [item("first", done=True), item("second"), item("third", done=True), item("fourth")]


def test_the_brief_numbers_against_the_whole_day():
    text = reports.daily_brief(TODAY, MIXED, [], [])
    assert "second" in row(text, 2)
    assert "fourth" in row(text, 4)
    assert "second" not in row(text, 1)


def test_the_evening_alert_numbers_against_the_whole_day():
    text = reports.incomplete_alert(TODAY, MIXED, [])
    assert text is not None
    assert "second" in row(text, 2)
    assert "fourth" in row(text, 4)


def test_status_numbers_the_same_way():
    text = reports.status(TODAY, MIXED, [], MIXED)
    assert "second" in row(text, 2)
    assert "fourth" in row(text, 4)


def test_all_three_agree_on_the_numbers():
    """The real requirement: read a number anywhere, /done it safely."""
    brief = reports.daily_brief(TODAY, MIXED, [], [])
    alert = reports.incomplete_alert(TODAY, MIXED, [])
    stat = reports.status(TODAY, MIXED, [], MIXED)
    for line in reports.pending_lines(MIXED):
        assert line in brief and line in alert and line in stat


def test_daily_brief_handles_an_empty_day():
    text = reports.daily_brief(TODAY, [], [], [])
    assert "nothing scheduled" in text


def test_daily_brief_distinguishes_a_finished_day_from_an_empty_one():
    """Both leave nothing to list; telling someone who cleared the day that
    nothing was scheduled reads as the bot not having noticed."""
    text = reports.daily_brief(TODAY, [item("a", done=True), item("b", done=True)], [], [])
    assert "all 2 done" in text
    assert "nothing scheduled" not in text


def test_incomplete_alert_is_silent_when_nothing_is_pending():
    assert reports.incomplete_alert(TODAY, [item("끝남", done=True)], []) is None


def test_incomplete_alert_reports_pending_work():
    text = reports.incomplete_alert(TODAY, [item("남은 것")], [])
    assert text is not None and "남은 것" in text


def test_week_scaffold_is_silent_when_nothing_was_created():
    assert reports.week_scaffold(date(2026, 8, 10), []) is None


def test_week_scaffold_groups_created_items_by_day():
    text = reports.week_scaffold(
        date(2026, 8, 10),
        [
            ("Dance class 7:10PM", date(2026, 8, 11), "Exercise"),
            ("Gym 12:30PM", date(2026, 8, 13), "Exercise"),
            ("Dance class 7:10PM", date(2026, 8, 13), "Exercise"),
        ],
    )
    assert text is not None
    assert "Tue 8/11" in text and "Thu 8/13" in text
    assert text.count("Dance class 7:10PM") == 2


def test_weekly_review_computes_completion_rate():
    text = reports.weekly_review(
        date(2026, 8, 2),
        TODAY,
        [item("a", done=True), item("b", done=True), item("c"), item("d")],
    )
    assert "Done 2 of 4" in text
    assert "50%" in text


def test_weekly_review_groups_by_day():
    """A flat list of eighteen ticks says only that the week happened; the
    question a review answers is what each day held."""
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [
            item("Cafe shift 5PM", done=True, day=date(2026, 8, 3)),
            item("Dance class", done=True, day=date(2026, 8, 4)),
            item("Diary", day=date(2026, 8, 4)),
        ],
    )
    assert "**Mon 8/3**" in text
    assert "**Tue 8/4**" in text
    assert text.index("Cafe shift 5PM") < text.index("Dance class")


def test_a_day_with_unfinished_items_shows_its_own_count():
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [
            item("a", done=True, day=date(2026, 8, 4)),
            item("b", day=date(2026, 8, 4)),
        ],
    )
    assert "**Tue 8/4**  (1/2)" in text


def test_a_fully_finished_day_carries_no_count():
    """The tick marks already say it; a (3/3) beside them is noise."""
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [item("a", done=True, day=date(2026, 8, 4))],
    )
    assert "**Tue 8/4**\n" in text
    assert "(1/1)" not in text


def test_days_with_nothing_on_them_are_skipped():
    text = reports.weekly_review(
        date(2026, 8, 3), date(2026, 8, 9), [item("a", day=date(2026, 8, 4))]
    )
    assert "8/5" not in text


def test_nothing_is_truncated():
    """A review that hides a third of the week defeats itself; long messages
    are chunked at send time instead."""
    items = [item(f"task {n}", done=True, day=date(2026, 8, 3)) for n in range(20)]
    text = reports.weekly_review(date(2026, 8, 3), date(2026, 8, 9), items)
    assert "more" not in text
    for n in range(20):
        assert f"task {n}" in text


def test_undated_items_are_kept_at_the_end():
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [item("floating", day=None), item("dated", done=True, day=date(2026, 8, 4))],
    )
    assert "**No date**" in text
    assert text.index("dated") < text.index("floating")


def test_weekly_review_handles_an_empty_week():
    text = reports.weekly_review(date(2026, 8, 2), TODAY, [])
    assert "0%" in text


def test_weekly_planning_lists_empty_days():
    text = reports.weekly_planning(
        date(2026, 8, 10),
        [item("Dance class", day=date(2026, 8, 11))],
        [],
    )
    assert "1 item(s) scheduled" in text
    assert "Tue 8/11" not in text.split("Empty days")[1]


def test_weekly_planning_prompts_when_the_week_is_empty():
    text = reports.weekly_planning(date(2026, 8, 10), [], [])
    assert "Nothing scheduled for next week yet." in text


def test_weekly_planning_surfaces_overdue_carryover():
    text = reports.weekly_planning(
        date(2026, 8, 10), [], [item("밀린 것", day=date(2026, 8, 3))]
    )
    assert "Carrying over" in text and "밀린 것" in text


def test_stale_projects_is_silent_when_none():
    assert reports.stale_projects([]) is None


def test_stale_projects_distinguishes_no_activity_from_old_activity():
    text = reports.stale_projects(
        [
            (Project("a", "옛날 것", None, "", ""), date(2026, 7, 1)),
            (Project("b", "기록 없음", None, "", ""), None),
        ]
    )
    assert "last activity Wed 7/1" in text
    assert "no linked activity" in text


# -- chunking --------------------------------------------------------------


def test_chunk_leaves_short_messages_alone():
    assert reports.chunk("짧은 메시지") == ["짧은 메시지"]


def test_chunk_splits_on_line_boundaries():
    message = "\n".join(f"line {i}" for i in range(500))
    parts = reports.chunk(message, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == message


def test_chunk_hard_splits_a_single_overlong_line():
    parts = reports.chunk("x" * 250, limit=100)
    assert [len(p) for p in parts] == [100, 100, 50]


# -- date parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, date(2026, 8, 8)),
        ("", date(2026, 8, 8)),
        ("오늘", date(2026, 8, 8)),
        ("내일", date(2026, 8, 9)),
        ("모레", date(2026, 8, 10)),
        ("2026-12-25", date(2026, 12, 25)),
        ("12-25", date(2026, 12, 25)),
        ("12/25", date(2026, 12, 25)),
        ("  내일  ", date(2026, 8, 9)),
        # Phone keyboards capitalise the first word and add punctuation; both
        # used to be parse errors.
        ("Tomorrow", date(2026, 8, 9)),
        ("TOMORROW", date(2026, 8, 9)),
        ("Tomorrow.", date(2026, 8, 9)),
        ("Today", date(2026, 8, 8)),
        ("tmr", date(2026, 8, 9)),
        ("day after tomorrow", date(2026, 8, 10)),
        ("내일모레", date(2026, 8, 10)),
    ],
)
def test_parse_day(text, expected):
    assert parse_day(text, TODAY) == expected


def test_parse_day_rejects_garbage_with_a_helpful_message():
    with pytest.raises(ValueError, match="Couldn't read that date"):
        parse_day("다음주 언젠가", TODAY)


# -- the overdue list ------------------------------------------------------


def overdue_item(title: str, day: date, at=None):
    return AgendaItem(
        id=title, title=title, day=day, status="Not started", done=False,
        url="", at=at,
    )


def test_overdue_items_are_numbered_so_they_can_be_cleared():
    """An unnumbered list is the one list no command can act on, and the
    backlog is exactly what needs clearing out."""
    text = reports.overdue_list(
        [overdue_item("old thing", date(2026, 8, 1)), overdue_item("older", date(2026, 7, 30))]
    )
    assert "old thing" in row(text, 1)
    assert "older" in row(text, 2)


def test_the_overdue_list_says_how_to_act_on_it():
    text = reports.overdue_list([overdue_item("x", date(2026, 8, 1))])
    assert "/delete <n> overdue" in text


def test_the_overdue_list_keeps_the_dates():
    """A number alone doesn't say which day an item is from, and these span
    many days by definition."""
    text = reports.overdue_list([overdue_item("x", date(2026, 8, 1))])
    assert reports.fmt_date(date(2026, 8, 1)) in text


def test_the_overdue_list_is_not_truncated():
    """Truncating would leave items numbered but unreachable: /done resolves
    a number against the full list, not against what fitted on screen."""
    items = [overdue_item(f"item {n}", date(2026, 8, 1)) for n in range(1, 16)]
    text = reports.overdue_list(items)
    assert "item 15" in row(text, 15)
    assert "more" not in text


def test_an_empty_backlog_says_so_without_a_hint():
    assert reports.overdue_list([]) == "🎉 Nothing overdue."


# -- showing what is underway ----------------------------------------------


def started_item(title: str, *, done: bool = False, started: bool = False):
    return AgendaItem(
        id=title, title=title, day=TODAY, status=None, done=done, url="",
        started=started,
    )


def test_started_items_are_marked_in_a_day_list():
    text = reports.day_list(
        TODAY, [started_item("underway", started=True), started_item("untouched")]
    )
    assert row(text, 1).startswith("1 >") and "underway" in row(text, 1)
    assert row(text, 2).startswith("2 -") and "untouched" in row(text, 2)


def test_started_items_are_marked_in_the_pending_list():
    """The brief, the evening alert and /status all read from this."""
    lines = reports.pending_lines([started_item("underway", started=True)])
    assert [line for line in lines if line.startswith("1 ")] == ["1 > -    underway"]


def test_a_finished_item_keeps_its_tick():
    text = reports.day_list(TODAY, [started_item("finished", done=True)])
    assert row(text, 1).startswith("1 v") and "finished" in row(text, 1)


def test_status_counts_what_is_underway():
    """"2 of 7 done" reads the same whether three things are half-finished or
    nothing has been touched at all."""
    items = [
        started_item("a", done=True),
        started_item("b", started=True),
        started_item("c"),
    ]
    text = reports.status(TODAY, items, [], items)
    assert "1 of 3 done (33%), 1 in progress" in text


def test_status_says_nothing_about_progress_when_nothing_is_underway():
    items = [started_item("a", done=True), started_item("b")]
    assert "in progress" not in reports.status(TODAY, items, [], items)


# -- acting on the backlog from wherever it appears -------------------------


def overdue_row(title: str, day: date):
    return AgendaItem(id=title, title=title, day=day, status=None, done=False, url="")


BACKLOG = [overdue_row("Unpacking", date(2026, 8, 6)), overdue_row("Nail", date(2026, 8, 8))]


def test_the_brief_numbers_its_overdue_block():
    """The brief is where the backlog is actually read, so it is where someone
    decides to clear it — and `/done 2 overdue` needs a 2 to type."""
    text = reports.daily_brief(TODAY, [], BACKLOG, [])
    assert "Unpacking" in row(text, 1)
    assert "Nail" in row(text, 2)


def test_every_message_numbers_the_backlog_identically():
    """The numbers are typed into a command that recounts the list itself, so
    a brief that numbered it differently from /overdue would tick off the
    wrong row."""
    brief = reports.daily_brief(TODAY, [], BACKLOG, [])
    evening = reports.incomplete_alert(TODAY, [], BACKLOG)
    listing = reports.overdue_list(BACKLOG)
    planning = reports.weekly_planning(TODAY, [], BACKLOG)
    for text in (brief, evening, listing, planning):
        assert "Unpacking" in row(text, 1)
        assert "Nail" in row(text, 2)


def test_the_backlog_says_how_to_act_on_it():
    assert "/done <n> overdue" in reports.daily_brief(TODAY, [], BACKLOG, [])


def test_a_long_backlog_is_trimmed_in_the_brief_but_points_at_the_rest():
    """The brief has a day to report as well; the numbers it does show stay
    valid, and the rest are one command away."""
    many = [overdue_row(f"item {n}", date(2026, 8, 1)) for n in range(1, 15)]
    brief = reports.daily_brief(TODAY, [], many, [])
    assert "item 10" in row(brief, 10)
    assert not row(brief, 11)
    assert "4 more" in brief and "/overdue" in brief
    # The full list is not trimmed, and keeps the same numbering.
    assert "item 14" in row(reports.overdue_list(many), 14)


# -- filtering the backlog by status ---------------------------------------


def backlog_row(title: str, day: date, status: str, *, started: bool = False):
    return AgendaItem(
        id=title, title=title, day=day, status=status, done=False, url="",
        started=started,
    )


MIXED_BACKLOG = [
    backlog_row("started one", date(2026, 8, 1), "In progress", started=True),
    backlog_row("untouched", date(2026, 8, 2), "Not started"),
    backlog_row("paused", date(2026, 8, 3), "On hold", started=True),
]


def test_filtering_keeps_the_numbers_of_the_whole_backlog():
    """The numbers are typed into /done, which recounts the full list — so a
    filtered view must not renumber from 1, or "1" means a different row than
    the one being read."""
    text = reports.overdue_list(
        MIXED_BACKLOG, keep={"In progress", "On hold"}, label="in progress"
    )
    assert row(text, 1).startswith("1 >") and "started one" in row(text, 1)
    assert row(text, 3).startswith("3 >") and "paused" in row(text, 3)
    assert "untouched" not in text


def test_a_filtered_header_says_how_much_is_hidden():
    text = reports.overdue_list(MIXED_BACKLOG, keep={"Not started"}, label="to do")
    assert "Overdue — to do (1 of 3)" in text


def test_an_unfiltered_list_is_unchanged():
    text = reports.overdue_list(MIXED_BACKLOG)
    assert "**Overdue (3)**" in text
    assert "untouched" in row(text, 2)


def test_a_filter_matching_nothing_says_so():
    """Silence would read as an empty backlog, which is the opposite of true."""
    text = reports.overdue_list(MIXED_BACKLOG, keep={"Ready"}, label="Ready")
    assert "Nothing overdue is Ready" in text


# -- the table layout -------------------------------------------------------
# Same rules as the asset table: ASCII inside a code block, widths measured
# from the values. The addition here is that titles are frequently Korean,
# where one glyph occupies two monospace cells.


def task(title, *, at=None, category=None, done=False, started=False, day=TODAY):
    return AgendaItem(
        id=title, title=title, day=day, status=None, done=done, url="",
        project_ids=(), at=at, category=category, started=started,
    )


def test_a_day_shows_time_task_and_category():
    text = reports.today_list(TODAY, [task("Cafe shift", at=time(9, 0), category="Work")])
    assert row(text, 1) == "1 09:00 Cafe shift Work"


def test_an_item_without_a_time_shows_a_hyphen():
    """An empty cell in the time column reads as a value that went missing,
    rather than a task that deliberately has no hour."""
    text = reports.today_list(TODAY, [task("Read", category="Personal")])
    assert row(text, 1) == "1 - Read Personal"


def test_an_uncategorised_item_leaves_the_column_empty():
    text = reports.today_list(TODAY, [task("Read")])
    assert row(text, 1) == "1 - Read"


def test_korean_titles_keep_the_columns_aligned():
    """A Korean glyph is two cells wide, so padding by character count would
    leave every column after the title ragged -- in the font this is actually
    read in, which is the Korean-locale one."""
    text = reports.today_list(
        TODAY,
        [
            task("당근 물건 발송", at=time(9, 0), category="Karrot"),
            task("Dentist", at=time(10, 0), category="Health"),
        ],
    )
    rows = [line for line in text.splitlines() if line.startswith(("1 ", "2 "))]
    assert reports._width(rows[0]) == reports._width(rows[1])


def test_the_table_stays_within_a_phone_screen():
    text = reports.today_list(
        TODAY,
        [task("A task with a genuinely very long title indeed", category="Personal")],
    )
    body = text.split("```")[1]
    assert max(reports._width(line) for line in body.splitlines()) <= reports.TABLE_COLS


def test_a_title_too_long_to_fit_is_cut_and_says_so():
    text = reports.today_list(
        TODAY, [task("A task with a genuinely very long title indeed", category="Personal")]
    )
    assert ".." in row(text, 1)
    assert "Personal" in row(text, 1)


def test_a_cut_never_splits_a_korean_glyph():
    """Half a wide glyph is a replacement character, and one cell of drift."""
    assert reports._width(reports._fit("당근 물건 발송 정리하기", 10)) == 10


def test_the_marks_are_ascii_inside_the_table():
    """Emoji render at an unpredictable width in a code block, which would
    shift every column after them."""
    text = reports.today_list(TODAY, [task("done", done=True), task("doing", started=True)])
    block = text.split("```")[1]
    assert "✅" not in block and "🔸" not in block


def test_a_split_inside_a_table_closes_and_reopens_the_block():
    """A long backlog is exactly the case that both overflows one message and
    most needs its columns."""
    many = [
        overdue_row(f"item number {n}", date(2026, 8, 1)) for n in range(1, 120)
    ]
    parts = reports.chunk(reports.overdue_list(many))
    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0, part[:80]
    # And no row is lost to the fences that were added.
    assert sum(part.count("item number ") for part in parts) == 119
